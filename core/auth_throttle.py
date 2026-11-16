"""Throttle repeated failed credential checks.

One shared, in-memory throttle for every place CLU checks a secret: the
``/login`` form, OPDS HTTP Basic auth and bearer tokens (``/api/v1/``,
``/api/insights``, the legacy global ``api_token``).

Failures are counted per *(scope, client address, account)* and per
*(scope, client address)*. After :data:`FAILURE_THRESHOLD` consecutive
failures the key enters a delay window that starts at
:data:`BASE_DELAY_SECONDS` and doubles with every further failure, up to
:data:`MAX_DELAY_SECONDS`. While a key is in its window the caller refuses the
request **without checking the credential**, so a guess made during the window
teaches nothing (a correct password is refused too). A success resets that
address's keys.

Things that look arbitrary and are not:

- **Nothing is ever keyed on the account alone.** A global per-account lock
  would let anyone who can reach the login page lock the owner out of their
  own instance. Keying on the client address means a guesser at one address
  cannot block the real owner at another.
- **The address-only key has a higher threshold** when an account is also
  tracked (:data:`ADDRESS_FAILURE_THRESHOLD`). Its job is to stop one address
  spraying many usernames, not to punish a household that shares one NAT
  address for a few typos.
- **Scopes are separate** (``login``, ``opds``, ``token``). An OPDS reader
  retrying a stale password in a loop must not stop the same person signing in
  to the web UI, and a Homepage widget with a revoked token must not either.
- **Loopback is not exempt.** Behind a reverse proxy on the same host a
  misconfigured ``ProxyFix`` makes *every* client look like 127.0.0.1, and
  exempting it would switch the throttle off for the whole internet.
- **In memory, per process.** CLU runs one gunicorn worker (``-w 1``), which
  this relies on; a restart clears every window, which is also how an owner
  who throttled themselves recovers without waiting.
- **Bounded.** At most :data:`MAX_ENTRIES` keys; the least recently touched is
  evicted first, so cycling usernames or addresses cannot grow it without
  limit. An address that keeps failing keeps its own key fresh, so flooding
  the table with junk usernames does not evict the address-only key that is
  holding the flooder back.
"""
import math
import threading
import time
from collections import OrderedDict

# Five consecutive misses is generous for a typo and tiny for a guesser.
FAILURE_THRESHOLD = 5
# One address trying many usernames: well above a household's typos.
ADDRESS_FAILURE_THRESHOLD = 20
# The first window is short enough that a real user barely notices it...
BASE_DELAY_SECONDS = 30
# ...and the doubling stops at 15 minutes: ~100 guesses a day per address.
MAX_DELAY_SECONDS = 15 * 60
# A key with no failure for a day starts again from zero (old typos don't add up).
FORGET_AFTER_SECONDS = 24 * 60 * 60
# Enough for every real client of a household install many times over.
MAX_ENTRIES = 10_000

SCOPE_LOGIN = "login"
SCOPE_OPDS = "opds"
SCOPE_TOKEN = "token"

# Longest account name kept in a key or a log line.
_ACCOUNT_KEY_MAX = 128


def _account_key(account):
    """Usernames match case-insensitively (``COLLATE NOCASE``), so do keys."""
    if account is None:
        return None
    return str(account).strip().lower()[:_ACCOUNT_KEY_MAX]


def _printable(value, limit=64):
    """A client-supplied value made safe for a log line."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(value or ""))
    return text[:limit]


class _Entry:
    __slots__ = ("failures", "blocked_until", "last_failure", "threshold")

    def __init__(self, threshold):
        self.failures = 0
        self.blocked_until = 0.0
        self.last_failure = 0.0
        self.threshold = threshold


class AuthThrottle:
    """Thread-safe failure counter with growing delay windows.

    ``clock`` is a monotonic seconds function; tests inject a fake one so no
    test ever sleeps.
    """

    def __init__(
        self,
        *,
        threshold=FAILURE_THRESHOLD,
        address_threshold=ADDRESS_FAILURE_THRESHOLD,
        base_delay=BASE_DELAY_SECONDS,
        max_delay=MAX_DELAY_SECONDS,
        forget_after=FORGET_AFTER_SECONDS,
        max_entries=MAX_ENTRIES,
        clock=time.monotonic,
        logger=None,
    ):
        self.threshold = threshold
        self.address_threshold = address_threshold
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.forget_after = forget_after
        self.max_entries = max_entries
        self.clock = clock
        self._logger = logger
        self._entries = OrderedDict()
        self._lock = threading.Lock()

    # -- keys ---------------------------------------------------------------

    def _keys(self, scope, address, account):
        """``[(key, threshold), ...]`` that a check on these values touches."""
        address = address or "unknown"
        account = _account_key(account)
        if account is None:
            return [((scope, address, None), self.threshold)]
        return [
            ((scope, address, account), self.threshold),
            ((scope, address, None), self.address_threshold),
        ]

    def _live(self, key, now):
        """The entry for ``key`` unless it has been idle long enough to forget."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if (entry.blocked_until <= now
                and now - entry.last_failure >= self.forget_after):
            del self._entries[key]
            return None
        return entry

    def _window(self, failures, threshold):
        """Delay after ``failures`` consecutive misses (0 below the threshold)."""
        if failures < threshold:
            return 0
        steps = failures - threshold
        # Cap the exponent too: 2 ** a huge number is a waste of a bignum.
        if steps >= 32:
            return self.max_delay
        return min(self.base_delay * (2 ** steps), self.max_delay)

    # -- public API -----------------------------------------------------------

    def retry_after(self, scope, address, account=None):
        """Seconds the caller must still wait (an int >= 1), or 0 if allowed."""
        with self._lock:
            now = self.clock()
            wait = 0.0
            for key, _threshold in self._keys(scope, address, account):
                entry = self._live(key, now)
                if entry is not None and entry.blocked_until > now:
                    wait = max(wait, entry.blocked_until - now)
        return int(math.ceil(wait)) if wait > 0 else 0

    def record_failure(self, scope, address, account=None):
        """Count one failed check. Returns the window it opened (0 if none)."""
        opened = []
        with self._lock:
            now = self.clock()
            longest = 0
            for key, threshold in self._keys(scope, address, account):
                entry = self._live(key, now)
                if entry is None:
                    entry = _Entry(threshold)
                    self._entries[key] = entry
                entry.failures += 1
                entry.last_failure = now
                self._entries.move_to_end(key)
                window = self._window(entry.failures, entry.threshold)
                if window:
                    entry.blocked_until = now + window
                    longest = max(longest, window)
                    opened.append((key, entry.failures, entry.threshold, window))
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        for key, failures, threshold, window in opened:
            self._announce(key, failures, threshold, window, account)
        return longest

    def record_success(self, scope, address, account=None):
        """A credential checked out: forget this address's failures."""
        with self._lock:
            for key, _threshold in self._keys(scope, address, account):
                self._entries.pop(key, None)

    def reset(self):
        """Forget everything (tests; there is no admin action for this)."""
        with self._lock:
            self._entries.clear()

    def __len__(self):
        with self._lock:
            return len(self._entries)

    # -- logging --------------------------------------------------------------

    def _get_logger(self):
        if self._logger is not None:
            return self._logger
        try:
            from core.app_logging import app_logger
        except Exception:
            return None
        return app_logger

    def _announce(self, key, failures, threshold, window, account):
        """One line when a key enters a window, never one per refused request.

        The first window is a WARNING; each later one (the window doubling)
        is one INFO line. Passwords and tokens are never passed in here.
        """
        scope, address, account_key = key
        if account_key is not None:
            who = f" for username {_printable(account)!r}"
        elif account is not None:
            who = " across usernames"
        else:
            who = ""
        message = (
            f"Auth throttle ({scope}): {failures} consecutive failed attempts "
            f"from {_printable(address)}{who}; refusing for {int(window)}s"
        )
        logger = self._get_logger()
        if logger is None:
            return
        try:
            if failures == threshold:
                logger.warning(message)
            else:
                logger.info(message)
        except Exception:
            pass


# The process-wide instance every credential check uses.
throttle = AuthThrottle()


def client_address():
    """The client as ProxyFix reports it (``"unknown"`` outside a request).

    Loopback gets no exemption: see the module docstring.
    """
    from flask import has_request_context, request

    if not has_request_context():
        return "unknown"
    return request.remote_addr or "unknown"


def retry_after_text(seconds):
    """``"Try again in N minutes."`` for the login page."""
    minutes = max(1, int(math.ceil(seconds / 60)))
    unit = "minute" if minutes == 1 else "minutes"
    return f"Too many attempts. Try again in {minutes} {unit}."


def too_many_attempts_json(seconds):
    """The JSON 429 every token endpoint answers while throttled."""
    from flask import jsonify

    response = jsonify({
        "error": "too_many_attempts",
        "retry_after": int(seconds),
    })
    response.status_code = 429
    response.headers["Retry-After"] = str(int(seconds))
    return response
