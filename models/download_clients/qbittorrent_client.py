"""
qBittorrent Download Client Adapter.

Talks to a qBittorrent instance over its Web API v2. Unlike SABnzbd/NZBGet,
authentication is cookie-session based rather than an API key: a login call
exchanges username/password for a session cookie, which every subsequent
request must carry.

qBittorrent's ``torrents/add`` does not return the hash of what it just
added, so grabbing a torrent works like this: submit with a fresh, unique
``tags`` value, then look the torrent back up by that tag to read its hash —
this avoids hand-rolling a magnet/bencode hash parser, and lets ``urls``
carry a magnet URI or a ``.torrent`` link straight through — qBittorrent
fetches either itself, the same way SABnzbd's ``addurl`` mode does.

API reference: https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)
"""
import time
import uuid
from typing import List, Optional

from core.app_logging import app_logger
from .base import BaseDownloadClient, ClientType, DownloadStatus, NZBSubmitResult
from . import register_download_client

# Short timeout so a wrong host/port fails fast instead of hanging the UI.
_TIMEOUT = 10

# States meaning "the download itself is finished" (seeding/queued-to-seed).
# qBittorrent keeps a completed torrent in the same list, seeding — there is
# no separate history call the way SABnzbd has one. Covers both the classic
# "paused*" state names and the "stopped*" names qBittorrent 5.0+ (WebAPI
# 2.11+) renamed them to.
_COMPLETE_STATES = {
    "uploading", "stalledup", "queuedup", "forcedup", "pausedup", "stoppedup",
    "checkingup",
}
_FAILED_STATES = {"error", "missingfiles"}

# How many times / how often to poll for the hash right after submitting,
# since torrents/add returns before the new entry is guaranteed queryable.
_TAG_RESOLVE_ATTEMPTS = 5
_TAG_RESOLVE_INTERVAL = 0.5


def _normalize_qbt_status(state, progress) -> str:
    """Map a qBittorrent torrent ``state`` (+ progress) to complete/failed/downloading."""
    low = (state or "").lower()
    if low in _FAILED_STATES:
        return "failed"
    if low in _COMPLETE_STATES:
        return "complete"
    try:
        if progress is not None and float(progress) >= 1.0:
            return "complete"
    except (TypeError, ValueError):
        pass
    return "downloading"


def _to_float(val):
    """Parse a value to float, or None if not numeric."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_int(val):
    """Parse a value to a positive int, or None (qBittorrent reports -1 for
    an unknown size, e.g. a magnet whose metadata hasn't arrived yet)."""
    try:
        n = int(val)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


@register_download_client
class QBittorrentClient(BaseDownloadClient):
    """qBittorrent download client using the Web API v2."""

    client_type = ClientType.QBITTORRENT
    display_name = "qBittorrent"
    requires_auth = True
    client_group = "torrent"
    config_fields = [
        "host",
        "port",
        "username",
        "password",
        "category",
        "use_ssl",
        "url_base",
        "target_directory",
        "local_target_directory",
    ]

    def __init__(self, config=None):
        super().__init__(config)
        self._session = None

    # ------------------------------------------------------------------
    # Request plumbing
    # ------------------------------------------------------------------

    def _api_url(self, path: str) -> str:
        return f"{self._base_url()}/api/v2{path}"

    def _login(self):
        """Authenticate and return a session with the SID cookie set, or None."""
        import requests

        cfg = self.config
        session = requests.Session()
        url = self._api_url("/auth/login")
        try:
            resp = session.post(
                url,
                data={"username": cfg.username or "", "password": cfg.password or ""},
                timeout=_TIMEOUT,
            )
        except requests.exceptions.ConnectionError:
            self.last_error = (
                f"Could not connect to {url} — check the host/port are reachable "
                f"from the CLU container (localhost inside Docker is the container "
                f"itself, not your homeserver)"
            )
            return None
        except requests.exceptions.Timeout:
            self.last_error = f"Timed out connecting to {url}"
            return None
        except Exception as e:
            self.last_error = str(e)
            app_logger.error(f"qBittorrent login failed: {e}")
            return None

        if resp.status_code in (401, 403):
            self.last_error = "Authentication failed — check the username/password"
            return None
        if resp.status_code not in (200, 204):
            self.last_error = f"HTTP {resp.status_code} from {url}"
            return None
        # WebAPI <2.11 answers 200 on both success and bad credentials,
        # distinguished only by body text ("Ok." vs "Fails."). 2.11+
        # (qBittorrent 5.x) instead answers 204 with an empty body on success
        # and 401 on bad credentials — confirmed against a real 5.2.3 /
        # WebAPI 2.15.1 instance, where the old "== 200" check rejected every
        # login, valid credentials included.
        if resp.status_code == 200 and (resp.text or "").strip() != "Ok.":
            self.last_error = "Authentication failed — check the username/password"
            return None
        return session

    def _session_for_request(self):
        """Return a cached logged-in session, logging in on first use."""
        if self._session is None:
            self._session = self._login()
        return self._session

    def _request(self, method: str, path: str, retry_on_auth: bool = True, **kwargs):
        """Issue an authenticated request; return the Response or None.

        Sets ``self.last_error`` on failure. A cached session that has
        expired answers with a 403; one retry re-logs in and replays the
        request before giving up, since qBittorrent expires sessions on its
        own schedule independent of CLU.
        """
        cfg = self.config
        if not cfg or not cfg.username or not cfg.password:
            self.last_error = "Username and password are required"
            return None

        session = self._session_for_request()
        if session is None:
            return None

        import requests

        url = self._api_url(path)
        try:
            resp = session.request(method, url, timeout=kwargs.pop("timeout", _TIMEOUT), **kwargs)
        except requests.exceptions.ConnectionError:
            self.last_error = (
                f"Could not connect to {url} — check the host/port are reachable "
                f"from the CLU container (localhost inside Docker is the container "
                f"itself, not your homeserver)"
            )
            return None
        except requests.exceptions.Timeout:
            self.last_error = f"Timed out connecting to {url}"
            return None
        except Exception as e:
            self.last_error = str(e)
            app_logger.error(f"qBittorrent request to {url} failed: {e}")
            return None

        if resp.status_code == 403 and retry_on_auth:
            self._session = None
            return self._request(method, path, retry_on_auth=False, **kwargs)
        if resp.status_code != 200:
            self.last_error = f"HTTP {resp.status_code} from {url}"
            return None
        return resp

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def test_connection(self) -> bool:
        """Verify qBittorrent is reachable and credentials work."""
        self.last_error = None
        self._session = None  # force a fresh login rather than trust a cached one
        resp = self._request("GET", "/app/version")
        if resp is None:
            return False
        if not (resp.text or "").strip().lower().startswith("v"):
            self.last_error = (
                f"Non-qBittorrent response from {self._api_url('/app/version')} — "
                f"is this a qBittorrent Web UI endpoint?"
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    def add_torrent(
        self,
        payload: str,
        name: str,
        category: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> NZBSubmitResult:
        """Submit a magnet URI or a .torrent URL; returns the resolved hash.

        Reuses ``NZBSubmitResult`` — it is protocol-neutral despite the name
        (see ``AirDCPPClient.download_result``), carrying only
        ``client_id``/``success``/``error``.

        ``payload`` is handed straight to qBittorrent's own ``urls`` field, so
        it fetches magnet URIs and ``.torrent`` links itself — CLU never
        downloads or parses a .torrent file. ``priority`` has no qBittorrent
        add-time equivalent (unlike SABnzbd's numeric queue priority) and is
        accepted only for interface parity.
        """
        self.last_error = None
        cfg = self.config
        if not cfg or not cfg.username or not cfg.password:
            return NZBSubmitResult(success=False, error="Username and password are required")

        cat = category if category is not None else cfg.category
        tag = f"clu-{uuid.uuid4().hex[:12]}"
        data = {"urls": payload, "tags": tag}
        if cat:
            data["category"] = cat

        resp = self._request("POST", "/torrents/add", data=data)
        if resp is None:
            return NZBSubmitResult(success=False, error=self.last_error)
        if (resp.text or "").strip() != "Ok.":
            self.last_error = "qBittorrent rejected the torrent"
            return NZBSubmitResult(success=False, error=self.last_error)

        torrent_hash = self._resolve_hash_for_tag(tag)
        if not torrent_hash:
            self.last_error = (
                "qBittorrent accepted the torrent but it could not be found "
                "again by its tag — it may still appear in the client"
            )
            return NZBSubmitResult(success=False, error=self.last_error)
        return NZBSubmitResult(client_id=torrent_hash, success=True)

    def _resolve_hash_for_tag(self, tag: str) -> Optional[str]:
        """Poll ``torrents/info`` by tag until the new torrent appears."""
        for _ in range(_TAG_RESOLVE_ATTEMPTS):
            resp = self._request("GET", "/torrents/info", params={"tag": tag})
            if resp is not None:
                try:
                    found = resp.json()
                except ValueError:
                    found = None
                if isinstance(found, list) and found:
                    h = found[0].get("hash")
                    if h:
                        return h
            time.sleep(_TAG_RESOLVE_INTERVAL)
        return None

    # ------------------------------------------------------------------
    # Queue / progress
    # ------------------------------------------------------------------

    def _list_torrents(self, params: Optional[dict] = None) -> list:
        """Return the raw torrent list from ``torrents/info``, or []."""
        resp = self._request("GET", "/torrents/info", params=params or {})
        if resp is None:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    def _torrent_to_status(self, t: dict) -> DownloadStatus:
        total = _to_int(t.get("size")) or _to_int(t.get("total_size"))
        progress = _to_float(t.get("progress"))
        downloaded = _to_int(t.get("downloaded"))
        if downloaded is None and total is not None and progress is not None:
            downloaded = int(total * progress)
        # content_path is a newer field (qBittorrent >= 4.3.x); fall back to
        # save_path, which every version reports.
        storage_path = t.get("content_path") or t.get("save_path") or None
        return DownloadStatus(
            client_id=str(t.get("hash") or ""),
            name=t.get("name"),
            status=_normalize_qbt_status(t.get("state"), progress),
            percent=(progress * 100.0) if progress is not None else None,
            category=t.get("category"),
            storage_path=storage_path,
            stage=(t.get("state") or "").title() or None,
            bytes_total=total,
            bytes_downloaded=downloaded,
        )

    def get_queue(self) -> List[DownloadStatus]:
        """Return torrents still downloading, with live percent/stage/bytes."""
        try:
            return [
                self._torrent_to_status(t)
                for t in self._list_torrents()
                if isinstance(t, dict)
                and _normalize_qbt_status(t.get("state"), t.get("progress")) == "downloading"
            ]
        except Exception as e:
            app_logger.error(f"qBittorrent get_queue failed: {e}")
            return []

    def get_history(self) -> List[DownloadStatus]:
        """Return torrents that finished downloading (seeding) or failed.

        qBittorrent keeps a completed torrent in the same list, seeding —
        there is no separate history endpoint the way SABnzbd has one.
        """
        try:
            return [
                self._torrent_to_status(t)
                for t in self._list_torrents()
                if isinstance(t, dict)
                and _normalize_qbt_status(t.get("state"), t.get("progress")) != "downloading"
            ]
        except Exception as e:
            app_logger.error(f"qBittorrent get_history failed: {e}")
            return []

    def get_status(self, client_id: str) -> Optional[DownloadStatus]:
        """Return the status of a single torrent by hash, or None if absent."""
        try:
            found = self._list_torrents(params={"hashes": client_id})
            return self._torrent_to_status(found[0]) if found else None
        except Exception as e:
            app_logger.error(f"qBittorrent get_status failed: {e}")
            return None
