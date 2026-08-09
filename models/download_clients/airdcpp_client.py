"""
AirDC++ Download Client Adapter (DC++ / Direct Connect).

Talks to an AirDC++ Web Client over its JSON REST API (``/api/v1``) using
HTTP Basic auth. DC++ itself has no remote API; AirDC++ Web Client is the
headless client that exposes one, so it is what CLU connects to — the same
way it connects to SABnzbd/NZBGet for Usenet.

Searching is a three-step asynchronous flow, unlike the Usenet clients:

    POST /search                          -> create a search instance
    POST /search/{id}/hub_search          -> run the query on the hubs
    GET  /search/{id}/results/{start}/{n} -> collect what the hubs returned

Results are only addressable as ``(instance_id, result_id)``, so queueing a
result means posting back to the same instance:

    POST /search/{id}/results/{result_id}/download

Progress for queued items comes from ``GET /queue/bundles/{start}/{n}``.

API reference: https://airdcpp.docs.apiary.io/

Field names below follow the published API docs. Every read is defensive
(``.get`` with fallbacks) so an AirDC++ version that renames or drops a
field degrades to "no progress shown" rather than raising inside the poller.
"""
import time
from typing import List, Optional, Tuple

from core.app_logging import app_logger
from .base import BaseDownloadClient, ClientType, DownloadStatus, NZBSubmitResult
from . import register_download_client

# Short timeout so a wrong host/port fails fast instead of hanging the UI.
_TIMEOUT = 10

# Hubs answer a search asynchronously. Poll the result list until it stops
# growing (or the budget runs out) rather than sleeping a flat interval, so a
# fast hub returns quickly and a slow one still gets a chance.
_SEARCH_WAIT_TOTAL = 8.0
_SEARCH_POLL_INTERVAL = 1.0

# How many results/bundles to pull per listing call.
_PAGE_SIZE = 100

# Restrict hub searches to comic files. DC++ shares hold these directly, and
# without the filter a bare series name returns mostly unrelated media. This
# also means directory (whole-run) hits are not returned — CLU wants single
# issues, and a directory download on DC++ is rarely what the user intended.
_COMIC_EXTENSIONS = ["cbz", "cbr", "cbt", "pdf"]


def _normalize_airdcpp_status(status: dict) -> str:
    """Map an AirDC++ bundle status object to complete/failed/downloading.

    The boolean flags are authoritative; the ``id`` string is only consulted
    when they are absent.
    """
    status = status or {}
    if status.get("failed"):
        return "failed"
    if status.get("completed"):
        return "complete"

    sid = str(status.get("id") or "").lower()
    if sid in ("download_failed", "failed", "validation_error", "shared_failed"):
        return "failed"
    if sid in ("completed", "finished", "shared", "downloaded"):
        return "complete"
    return "downloading"


def _to_int(val) -> Optional[int]:
    """Parse a value to int, or None if not numeric."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _percent(downloaded, total) -> Optional[float]:
    """Percentage complete from byte counts, or None when unknown."""
    d, t = _to_int(downloaded), _to_int(total)
    if d is None or not t:
        return None
    return max(0.0, min(100.0, (d / t) * 100.0))


def _result_type(raw) -> str:
    """Extract a result's type, which may be a string or a {id: ...} object."""
    if isinstance(raw, dict):
        return str(raw.get("id") or raw.get("str") or "")
    return str(raw or "")


def _user_count(raw) -> Optional[int]:
    """Extract the number of users sharing a result.

    AirDC++ reports this as a plain count on some versions and as a
    ``{"user": ..., "count": N}`` object on others.
    """
    if isinstance(raw, dict):
        return _to_int(raw.get("count"))
    return _to_int(raw)


@register_download_client
class AirDCPPClient(BaseDownloadClient):
    """AirDC++ Web Client adapter using the /api/v1 REST API."""

    client_type = ClientType.AIRDCPP
    display_name = "AirDC++"
    requires_auth = True
    client_group = "dcpp"
    config_fields = [
        "host",
        "port",
        "username",
        "password",
        "use_ssl",
        "url_base",
        "target_directory",
        "hub_urls",
    ]

    # ------------------------------------------------------------------
    # Request plumbing
    # ------------------------------------------------------------------

    def _api_url(self, path: str) -> str:
        """Build a full ``/api/v1`` URL for a path like ``/hubs``."""
        return f"{self._base_url()}/api/v1{path}"

    def _auth(self):
        cfg = self.config
        return (cfg.username or "", cfg.password or "") if cfg else ("", "")

    def _request(self, method: str, path: str, allow_missing: bool = False, **kwargs):
        """Issue an authenticated request; return the Response or None.

        Sets ``self.last_error`` on failure so callers can surface a reason.
        With ``allow_missing``, a 404 is returned to the caller instead of
        being treated as an error — AirDC++ answers a poll for a bundle it no
        longer holds with ``404 {"message": "Bundle <id> was not found"}``,
        which is meaningful rather than broken.
        """
        import requests

        url = self._api_url(path)
        try:
            resp = requests.request(
                method, url, auth=self._auth(), timeout=kwargs.pop("timeout", _TIMEOUT),
                **kwargs
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
            app_logger.error(f"AirDC++ request to {url} failed: {e}")
            return None

        if resp.status_code == 401:
            self.last_error = "Authentication failed — check the username/password"
            return None
        if resp.status_code == 404 and allow_missing:
            return resp
        if resp.status_code >= 400:
            self.last_error = f"HTTP {resp.status_code} from {url}"
            return None
        return resp

    @staticmethod
    def _json(resp):
        """Parse a JSON body, returning None when the body is not JSON."""
        if resp is None:
            return None
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def test_connection(self) -> bool:
        """Verify the AirDC++ Web Client is reachable and credentials work.

        Queries ``/hubs``, which requires a valid session and returns a list
        (possibly empty, if the user has no hubs connected yet).
        """
        self.last_error = None
        cfg = self.config
        if not cfg or not cfg.username or not cfg.password:
            self.last_error = "Username and password are required"
            return False

        resp = self._request("GET", "/hubs")
        if resp is None:
            return False

        data = self._json(resp)
        if data is None:
            self.last_error = (
                f"Non-JSON response from {self._api_url('/hubs')} — is this an "
                f"AirDC++ Web Client?"
            )
            return False
        if not isinstance(data, list):
            self.last_error = f"Unexpected response from {self._api_url('/hubs')}"
            return False
        return True

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, pattern: str, hub_urls: Optional[list] = None) -> Tuple[Optional[str], list]:
        """Run a hub search and return ``(instance_id, results)``.

        ``instance_id`` is needed to queue any of the returned results, so the
        caller must keep it for as long as the results are actionable. Returns
        ``(None, [])`` on failure with ``last_error`` set.
        """
        self.last_error = None

        # The instance id comes back as a number (e.g. 5); the URLs need a str.
        created = self._json(self._request("POST", "/search"))
        if not isinstance(created, dict) or created.get("id") in (None, ""):
            self.last_error = self.last_error or "AirDC++ did not return a search instance"
            return None, []
        instance_id = str(created["id"])

        query = {
            "pattern": pattern,
            "extensions": _COMIC_EXTENSIONS,
            "file_type": "file",
        }
        body = {"query": query}
        hubs = hub_urls if hub_urls is not None else self._configured_hub_urls()
        if hubs:
            body["hub_urls"] = hubs

        if self._request("POST", f"/search/{instance_id}/hub_search", json=body) is None:
            return instance_id, []

        return instance_id, self._collect_results(instance_id)

    def _configured_hub_urls(self) -> list:
        """Parse the optional comma-separated hub filter from config."""
        cfg = self.config
        raw = (cfg.hub_urls if cfg else None) or ""
        return [h.strip() for h in str(raw).split(",") if h.strip()]

    def _collect_results(self, instance_id: str) -> list:
        """Poll the instance until the result count stops growing."""
        deadline = time.monotonic() + _SEARCH_WAIT_TOTAL
        results: list = []
        while True:
            time.sleep(_SEARCH_POLL_INTERVAL)
            found = self._json(
                self._request("GET", f"/search/{instance_id}/results/0/{_PAGE_SIZE}")
            )
            if isinstance(found, list):
                # Hubs answer at different speeds; keep the largest set seen.
                if len(found) > len(results):
                    results = found
                elif results:
                    break  # stopped growing — the hubs are done answering
            if time.monotonic() >= deadline:
                break
        return [self._parse_result(r) for r in results if isinstance(r, dict)]

    @staticmethod
    def _parse_result(raw: dict) -> dict:
        """Normalize one AirDC++ search result into a plain dict."""
        return {
            "result_id": str(raw.get("id") or ""),
            "name": raw.get("name") or "",
            "size": _to_int(raw.get("size")),
            "tth": raw.get("tth"),
            "type": _result_type(raw.get("type")),
            "users": _user_count(raw.get("users")),
            "relevance": raw.get("relevance"),
            "path": raw.get("path"),
        }

    def download_result(
        self,
        instance_id: str,
        result_id: str,
        target_directory: Optional[str] = None,
        priority: Optional[str] = None,
    ) -> NZBSubmitResult:
        """Queue a search result for download; returns the bundle id.

        Reuses ``NZBSubmitResult`` — it is protocol-neutral despite the name,
        carrying only ``client_id`` / ``success`` / ``error``.
        """
        self.last_error = None
        target = target_directory or (self.config.target_directory if self.config else None)

        body = {}
        if target:
            body["target_directory"] = target
        if priority:
            body["priority"] = priority

        resp = self._request(
            "POST", f"/search/{instance_id}/results/{result_id}/download", json=body
        )
        if resp is None:
            return NZBSubmitResult(success=False, error=self.last_error)

        data = self._json(resp)
        if not isinstance(data, dict):
            # A 2xx with an unparseable body means it was queued but we cannot
            # track it; report failure rather than inventing a bundle id.
            self.last_error = "AirDC++ accepted the download but returned no bundle id"
            return NZBSubmitResult(success=False, error=self.last_error)

        bundle_id = data.get("id") or data.get("bundle_id")
        if bundle_id in (None, ""):
            self.last_error = "AirDC++ accepted the download but returned no bundle id"
            return NZBSubmitResult(success=False, error=self.last_error)
        return NZBSubmitResult(client_id=str(bundle_id), success=True)

    # ------------------------------------------------------------------
    # Queue / progress
    # ------------------------------------------------------------------

    def _bundles(self) -> list:
        """Return the raw bundle list from the queue, or []."""
        data = self._json(self._request("GET", f"/queue/bundles/0/{_PAGE_SIZE}"))
        return data if isinstance(data, list) else []

    @staticmethod
    def _bundle_to_status(b: dict) -> DownloadStatus:
        """Map one AirDC++ bundle to a unified DownloadStatus.

        ``size`` and ``downloaded_bytes`` come back as floats (27964355.0),
        and ``id`` as an int, so both are coerced. Note ``status.downloaded``
        is a *boolean* flag, not a byte count — it must never be used as a
        progress fallback.
        """
        status = b.get("status") or {}
        total = _to_int(b.get("size"))
        downloaded = _to_int(b.get("downloaded_bytes"))

        return DownloadStatus(
            client_id=str(b.get("id") or ""),
            name=b.get("name"),
            status=_normalize_airdcpp_status(status),
            percent=_percent(downloaded, total),
            storage_path=b.get("target") or None,
            stage=status.get("str") or None,
            bytes_total=total,
            bytes_downloaded=downloaded,
        )

    def get_queue(self) -> List[DownloadStatus]:
        """Return bundles still in progress, with live percent/stage/bytes."""
        try:
            return [
                self._bundle_to_status(b)
                for b in self._bundles()
                if isinstance(b, dict)
                and _normalize_airdcpp_status(b.get("status") or {}) == "downloading"
            ]
        except Exception as e:
            app_logger.error(f"AirDC++ get_queue failed: {e}")
            return []

    def get_history(self) -> List[DownloadStatus]:
        """Return finished (completed or failed) bundles still in the queue.

        AirDC++ can be configured to drop finished bundles from the queue, in
        which case this is legitimately empty. Completion is therefore tracked
        per bundle via :meth:`get_bundle_state`, not by diffing this list.
        """
        try:
            return [
                self._bundle_to_status(b)
                for b in self._bundles()
                if isinstance(b, dict)
                and _normalize_airdcpp_status(b.get("status") or {}) != "downloading"
            ]
        except Exception as e:
            app_logger.error(f"AirDC++ get_history failed: {e}")
            return []

    def get_bundle_state(self, client_id: str):
        """Poll one bundle by id. Returns ``(state, DownloadStatus | None)``.

        ``state`` is one of:

        ``"found"``  the bundle exists; the status carries live progress.
        ``"gone"``   AirDC++ 404s for it. Either it finished and was removed
                     from the queue (AirDC++ can be set to do that, and then
                     a completed bundle is never observable any other way) or
                     the user removed it by hand. The caller decides.
        ``"error"``  the client was unreachable — say nothing, try later.

        Polling by id rather than scanning the queue listing matters: a real
        queue can be longer than one page, and a paged scan would lose sight
        of a tracked bundle sitting past the end of it.
        """
        resp = self._request("GET", f"/queue/bundles/{client_id}", allow_missing=True)
        if resp is None:
            return "error", None
        if resp.status_code == 404:
            return "gone", None
        data = self._json(resp)
        if not isinstance(data, dict):
            return "error", None
        return "found", self._bundle_to_status(data)

    def get_status(self, client_id: str) -> Optional[DownloadStatus]:
        """Return the status of a single bundle by id, or None if absent."""
        try:
            state, status = self.get_bundle_state(client_id)
            return status if state == "found" else None
        except Exception as e:
            app_logger.error(f"AirDC++ get_status failed: {e}")
            return None
