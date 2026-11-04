"""
GCD REST API client for comics.org.

Provides HTTP client for the Grand Comics Database REST API,
separate from the MySQL-based GCD integration in gcd.py.

Every request goes through ``core.http_client.HttpClient`` (#704): one paced
send at a time at the rate Settings shows, and a 401 latches an
``AuthLockout`` that stops all GCD API traffic until a human changes
something. Before that, a wrong password was re-sent on every lookup -- the
pattern that got a user's IP banned by Metron's fail2ban.
"""
import threading
from urllib.parse import urlparse, parse_qs, quote
from typing import Optional, Dict, List, Tuple

from core.http_client import HttpClient
from core.provider_client import AuthLockout, QuotaLatch
from models.providers.base import DEFAULT_PROVIDER_USER_AGENT


BASE_URL = "https://www.comics.org/api"
DISPLAY_NAME = "Grand Comics Database (API)"

_auth_block = AuthLockout("gcd_api", DISPLAY_NAME)
_quota = QuotaLatch(DISPLAY_NAME, "request")


def auth_blocked() -> bool:
    """True while comics.org has rejected the saved credentials."""
    return _auth_block.blocked()


def clear_auth_block() -> None:
    _auth_block.clear()


def latch_auth_failure(status: Optional[int], detail: str = "", context: str = "") -> None:
    _auth_block.latch(status, detail, context)


def _per_minute() -> int:
    from models.providers.gcd_api_provider import GCDApiProvider

    return GCDApiProvider.rate_limit


class GCDApiClient:
    """HTTP client for the GCD REST API at comics.org.

    The methods raise on a connection or HTTP error, as they always have, and
    return None (or an empty list) when the call was skipped because the
    lockout or quota latch is set, or when it was the credentials that were
    rejected.
    """

    def __init__(self, username: str, password: str):
        self._http = HttpClient(
            "gcd_api",
            DISPLAY_NAME,
            BASE_URL,
            _per_minute(),
            # comics.org is fronted by Cloudflare, which 403s library UAs.
            headers={
                "Accept": "application/json",
                "User-Agent": DEFAULT_PROVIDER_USER_AGENT,
            },
            auth=(username, password),
            lockout=_auth_block,
            quota=_quota,
        )

    @property
    def session(self):
        return self._http.session

    def _get(self, path: str, params: dict = None, **opts) -> Optional[Dict]:
        """Make one authenticated, paced GET request to the GCD API."""
        opts.setdefault("raise_api_errors", True)
        resp = self._http.get(path, params=params or {}, context=f"for {path}", **opts)
        if resp is None:
            return None
        return resp.json()

    def check_credentials(self) -> bool:
        """One request that ignores the lockout and raises a rejection.

        For the connection test only: a user who fixed their password must be
        able to prove it while the lockout is still latched.
        """
        data = self._get(
            f"/series/name/{quote('Batman', safe='')}/",
            ignore_auth_lock=True,
            raise_auth_errors=True,
        )
        return bool(data and data.get("results"))

    def search_series(self, name: str, year: int = None, max_pages: int = 5) -> List[Dict]:
        """Search series by name, optionally filtered by year."""
        encoded_name = quote(name, safe="")
        if year:
            path = f"/series/name/{encoded_name}/year/{year}/"
        else:
            path = f"/series/name/{encoded_name}/"
        return self._get_all_pages(path, max_pages=max_pages)

    def get_series(self, series_id: int) -> Optional[Dict]:
        """Get series details by ID, including issue list."""
        return self._get(f"/series/{series_id}/")

    def get_issue(self, issue_id: int) -> Optional[Dict]:
        """Get full issue details including stories and credits."""
        return self._get(f"/issue/{issue_id}/")

    def search_issue(self, series_name: str, issue_number: str, year: int = None) -> List[Dict]:
        """Search for an issue by series name and issue number."""
        encoded_name = quote(series_name, safe="")
        encoded_number = quote(str(issue_number), safe="")
        if year:
            path = f"/series/name/{encoded_name}/issue/{encoded_number}/year/{year}/"
        else:
            path = f"/series/name/{encoded_name}/issue/{encoded_number}/"
        return self._get_all_pages(path)

    def get_publisher(self, publisher_id: int) -> Optional[Dict]:
        """Get publisher details by ID."""
        return self._get(f"/publisher/{publisher_id}/")

    def _get_all_pages(self, path: str, max_pages: int = 5) -> List[Dict]:
        """Fetch paginated results up to max_pages, one paced send per page."""
        results = []
        params = {}
        for _ in range(max_pages):
            data = self._get(path, params)
            if not data:
                break
            results.extend(data.get("results", []))
            next_url = data.get("next")
            if not next_url:
                break
            parsed = parse_qs(urlparse(next_url).query)
            page = parsed.get("page", [None])[0]
            if page:
                params["page"] = page
            else:
                break
        return results


# One client per credential pair, shared by every thread: the pacing is shared
# per provider anyway, and this keeps one connection pool instead of one per
# lookup.
_CLIENTS: Dict[Tuple[str, str], GCDApiClient] = {}
_CLIENTS_LOCK = threading.Lock()


def get_gcd_client(username: str, password: str) -> GCDApiClient:
    key = (username, password)
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            client = GCDApiClient(username, password)
            _CLIENTS[key] = client
        return client


def invalidate_gcd_client_cache() -> None:
    """Drop the shared clients (credential change, tests)."""
    with _CLIENTS_LOCK:
        clients = list(_CLIENTS.values())
        _CLIENTS.clear()
    for client in clients:
        try:
            client.session.close()
        except Exception:
            pass
    _auth_block.forget()
