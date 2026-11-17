"""Throttle repeated failed credential checks.

One shared, in-memory throttle for every place CLU checks a secret: the
``/login`` form, OPDS HTTP Basic auth and bearer tokens (``/api/v1/``,
``/api/insights``, the legacy global ``api_token``).

Two kinds of key, both scoped to one client address:

- **The pair key** *(scope, address, account)* measures **repetition**. After
  :data:`FAILURE_THRESHOLD` consecutive failures it enters a delay window that
  starts at :data:`BASE_DELAY_SECONDS` and doubles with every further failure,
  up to :data:`MAX_DELAY_SECONDS`. A success resets it. The account is the
  username for ``login``/``opds`` and a short digest of the presented token
  for ``token`` (:func:`token_account_key`), so a stale token throttles only
  itself.
- **The address key** *(scope, address)* measures **spraying**. It advances
  only when an account fails that has not already failed from that address
  within :data:`SPRAY_WINDOW_SECONDS`, i.e. it counts *distinct* failing
  accounts. At :data:`ADDRESS_FAILURE_THRESHOLD` distinct accounts the whole
  address enters a window, which doubles with each further new account.

While either key is in its window the caller refuses the request **without
checking the credential**, so a guess made during the window teaches nothing
(a correct password is refused too).

Things that look arbitrary and are not:

- **Repetition never reaches the address key.** In many installs every client
  shares one ``remote_addr`` (Docker bridge networking shows the gateway for
  the whole LAN; a proxy that sends no ``X-Forwarded-For`` does the same).
  An OPDS reader retrying one stale password, or a dashboard polling with a
  revoked token every 30 seconds, must lock out only that one pair, not every
  user behind the same address. Counting failures at the address level would
  do exactly that within minutes.
- **A success does not clear the spray state.** A guesser with a valid
  account of their own could otherwise log in between guesses and reset it.
  Failing accounts age out of the set after :data:`SPRAY_WINDOW_SECONDS`
  instead.
- **Nothing is ever keyed on the account alone.** A global per-account lock
  would let anyone who can reach the login page lock the owner out of their
  own instance. Keying on the client address means a guesser at one address
  cannot block the real owner at another.
- **Scopes are separate** (``login``, ``opds``, ``token``). An OPDS reader
  retrying a stale password in a loop must not stop the same person signing in
  to the web UI.
- **Loopback is not exempt.** Behind a reverse proxy on the same host a
  misconfigured ``ProxyFix`` makes *every* client look like 127.0.0.1, and
  exempting it would switch the throttle off for the whole internet.
- **In memory, per process.** CLU runs one gunicorn worker (``-w 1``), which
  this relies on; a restart clears every window, which is also how an owner
  who throttled themselves recovers without waiting.
- **Bounded.** At most :data:`MAX_ENTRIES` keys; the least recently touched is
  evicted first, so cycling usernames or addresses cannot grow it without
  limit. Each address key holds at most :data:`MAX_TRACKED_ACCOUNTS` account
  keys. An address that keeps spraying keeps its own key fresh, so flooding
  the table with junk usernames does not evict the key holding it back.
"""
import hashlib
import math
import threading
import time
from collections import OrderedDict

# Five consecutive misses is generous for a typo and tiny for a guesser.
FAILURE_THRESHOLD = 5
# Distinct failing usernames/tokens from one address before the whole address
# waits: far above a household's typos, and a stuck device counts once.
ADDRESS_FAILURE_THRESHOLD = 20
# The first window is short enough that a real user barely notices it...
BASE_DELAY_SECONDS = 30
# ...and the doubling stops at 15 minutes: ~100 guesses a day per pair.
MAX_DELAY_SECONDS = 15 * 60
# A pair with no failure for a day starts again from zero (old typos don't add up).
FORGET_AFTER_SECONDS = 24 * 60 * 60
# A failing account counts toward spraying for a day after its last failure.
SPRAY_WINDOW_SECONDS = 24 * 60 * 60
# Five doublings past the threshold reach the cap (30s * 2**5 > 15 min), so no
# address needs to remember more accounts than this.
MAX_TRACKED_ACCOUNTS = ADDRESS_FAILURE_THRESHOLD + 5
# Enough for every real client of a household install many times over.
MAX_ENTRIES = 10_000

SCOPE_LOGIN = "login"
SCOPE_OPDS = "opds"
SCOPE_TOKEN = "token"

# Longest account name kept in a key or a log line.
_ACCOUNT_KEY_MAX = 128
# Hex digits of sha256 that identify a presented token (64 bits).
_TOKEN_DIGEST_HEX = 16


def token_account_key(presented):
    """The account key for a presented bearer token: a short sha256 digest.

    Never the token itself, so neither the table nor a log line holds a secret.
    Distinct tokens get distinct keys, which is what lets a stale token
    throttle only itself.
    """
    digest = hashlib.sha256(str(presented or "").encode("utf-8")).hexdigest()
    return "token:" + digest[:_TOKEN_DIGEST_HEX]


def _account_key(account):
    """Usernames match case-insensitively (``COLLATE NOCASE``), so do keys."""
    if account is None:
        return ""
    return str(account).strip().lower()[:_ACCOUNT_KEY_MAX]


def _printable(value, limit=64):
    """A client-supplied value made safe for a log line."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(value or ""))
    return text[:limit]


class _PairEntry:
    __slots__ = ("failures", "blocked_until", "last_failure")

    def __init__(self):
        self.failures = 0
        self.blocked_until = 0.0
        self.last_failure = 0.0


class _AddressEntry:
    """Distinct failing accounts from one address, oldest first."""
    __slots__ = ("accounts", "blocked_until")

    def __init__(self):
        self.accounts = OrderedDict()  # account key -> time of its last failure
        self.blocked_until = 0.0


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
        spray_window=SPRAY_WINDOW_SECONDS,
        max_tracked_accounts=None,
        max_entries=MAX_ENTRIES,
        clock=time.monotonic,
        logger=None,
    ):
        self.threshold = threshold
        self.address_threshold = address_threshold
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.forget_after = forget_after
        self.spray_window = spray_window
        self.max_tracked_accounts = (
            max_tracked_accounts if max_tracked_accounts is not None
            else address_threshold + 5
        )
        self.max_entries = max_entries
        self.clock = clock
        self._logger = logger
        self._entries = OrderedDict()
        self._lock = threading.Lock()

    # -- keys ---------------------------------------------------------------

    @staticmethod
    def _pair_key(scope, address, account):
        return ("pair", scope, address or "unknown", _account_key(account))

    @staticmethod
    def _address_key(scope, address):
        return ("addr", scope, address or "unknown")

    def _live_pair(self, key, now):
        """The pair entry unless it has been idle long enough to forget."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if (entry.blocked_until <= now
                and now - entry.last_failure >= self.forget_after):
            del self._entries[key]
            return None
        return entry

    def _live_address(self, key, now):
        """The address entry with aged-out accounts dropped, or None."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        accounts = entry.accounts
        while accounts:
            oldest, seen = next(iter(accounts.items()))
            if now - seen < self.spray_window:
                break
            del accounts[oldest]
        if not accounts and entry.blocked_until <= now:
            del self._entries[key]
            return None
        return entry

    def _window(self, count, threshold):
        """Delay once ``count`` reaches ``threshold`` (0 below it)."""
        if count < threshold:
            return 0
        steps = count - threshold
        # Cap the exponent too: 2 ** a huge number is a waste of a bignum.
        if steps >= 32:
            return self.max_delay
        return min(self.base_delay * (2 ** steps), self.max_delay)

    def _remaining(self, entry, now):
        if entry is not None and entry.blocked_until > now:
            return entry.blocked_until - now
        return 0.0

    # -- public API -----------------------------------------------------------

    def retry_after(self, scope, address, account=None):
        """Seconds the caller must still wait (an int >= 1), or 0 if allowed."""
        with self._lock:
            now = self.clock()
            pair = self._live_pair(self._pair_key(scope, address, account), now)
            spray = self._live_address(self._address_key(scope, address), now)
            wait = max(self._remaining(pair, now), self._remaining(spray, now))
        return int(math.ceil(wait)) if wait > 0 else 0

    def record_failure(self, scope, address, account=None):
        """Count one failed check. Returns the longest window it opened (0 if none)."""
        opened = []
        account_key = _account_key(account)
        with self._lock:
            now = self.clock()

            # Repetition: this pair's own counter and window.
            pair_key = self._pair_key(scope, address, account)
            pair = self._live_pair(pair_key, now)
            if pair is None:
                pair = _PairEntry()
                self._entries[pair_key] = pair
            pair.failures += 1
            pair.last_failure = now
            self._entries.move_to_end(pair_key)
            window = self._window(pair.failures, self.threshold)
            if window:
                pair.blocked_until = now + window
                opened.append(("pair", pair.failures, self.threshold, window))

            # Spraying: advances only for an account new to this address.
            addr_key = self._address_key(scope, address)
            spray = self._live_address(addr_key, now)
            if spray is None:
                spray = _AddressEntry()
                self._entries[addr_key] = spray
            is_new = account_key not in spray.accounts
            spray.accounts[account_key] = now
            spray.accounts.move_to_end(account_key)
            while len(spray.accounts) > self.max_tracked_accounts:
                spray.accounts.popitem(last=False)
            self._entries.move_to_end(addr_key)
            if is_new:
                window = self._window(len(spray.accounts), self.address_threshold)
                if window:
                    spray.blocked_until = max(spray.blocked_until, now + window)
                    opened.append(("addr", len(spray.accounts),
                                   self.address_threshold, window))

            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

        for kind, count, threshold, window in opened:
            self._announce(kind, scope, address, account, count, threshold, window)
        return max((w for *_rest, w in opened), default=0)

    def record_success(self, scope, address, account=None):
        """A credential checked out: reset this pair.

        The address's spray state is deliberately left alone (module docstring).
        """
        with self._lock:
            self._entries.pop(self._pair_key(scope, address, account), None)

    def reset(self):
        """Forget everything (tests; there is no admin action for this)."""
        with self._lock:
            self._entries.clear()

    def __len__(self):
        with self._lock:
            return len(self._entries)

    def keys(self):
        """A snapshot of the stored keys and tracked account keys (tests)."""
        with self._lock:
            out = []
            for key, entry in self._entries.items():
                out.append(key)
                if isinstance(entry, _AddressEntry):
                    out.extend(entry.accounts.keys())
            return out

    # -- logging --------------------------------------------------------------

    def _get_logger(self):
        if self._logger is not None:
            return self._logger
        try:
            from core.app_logging import app_logger
        except Exception:
            return None
        return app_logger

    def _announce(self, kind, scope, address, account, count, threshold, window):
        """One line when a key enters a window, never one per refused request.

        The first window is a WARNING; each later one (the window doubling)
        is one INFO line. Passwords and tokens are never passed in here: for
        the token scope ``account`` is already the digest.
        """
        if kind == "pair":
            label = "token" if scope == SCOPE_TOKEN else "username"
            what = (f"{count} consecutive failed attempts from "
                    f"{_printable(address)} for {label} {_printable(account)!r}")
        else:
            label = "tokens" if scope == SCOPE_TOKEN else "usernames"
            what = (f"failed attempts for {count} different {label} from "
                    f"{_printable(address)}")
        message = f"Auth throttle ({scope}): {what}; refusing for {int(window)}s"
        logger = self._get_logger()
        if logger is None:
            return
        try:
            if count == threshold:
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
