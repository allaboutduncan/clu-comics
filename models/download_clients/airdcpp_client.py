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
import os
import time
from typing import List, Optional, Tuple

from core.app_logging import app_logger
from .base import BaseDownloadClient, ClientType, DownloadStatus, NZBSubmitResult
from . import register_download_client

# Short timeout so a wrong host/port fails fast instead of hanging the UI.
_TIMEOUT = 10

# A hub search is slow in a way a Newznab query is not. AirDC++ *queues*
# searches and paces them out to each hub to respect hub flood limits, then
# results trickle back over several more seconds. Measured against a live
# instance on a 2-hub setup: queue_time ~17s before the search was even sent,
# first results at ~t+20s, settling at ~t+30s.
#
# So the wait is driven by the instance's own metadata (queued_count reaching
# zero, then result_count going quiet) rather than a flat sleep, with a hard
# cap. An 8s budget — the previous value — routinely expired before the hub
# had been sent the query at all, and returned nothing.
_SEARCH_WAIT_TOTAL = 45.0
_SEARCH_POLL_INTERVAL = 2.0
# Consecutive unchanged polls (after dispatch) that mean the hubs are done.
_SEARCH_SETTLE_ROUNDS = 3

# How many bundles to pull per queue listing call.
_PAGE_SIZE = 100
# Results are cheap to page and a broad series search returns a lot of them
# (192 for a two-hub "Strange Tales"), so pull well past the queue page size.
_RESULT_PAGE_SIZE = 250

# Restrict hub searches to comic files. DC++ shares hold these directly, and
# without the filter a bare series name returns mostly unrelated media. This
# also means directory (whole-run) hits are not returned — CLU wants single
# issues, and a directory download on DC++ is rarely what the user intended.
_COMIC_EXTENSIONS = ["cbz", "cbr", "cbt", "pdf"]

# "not asked yet", distinct from "asked, and this version doesn't say" (None).
_UNPROBED = object()


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


def _normalize_target_directory(path: Optional[str], sep: Optional[str] = None) -> Optional[str]:
    """Ensure a target directory ends with a path separator.

    AirDC++ concatenates ``target_directory`` with the result's filename
    rather than joining them, so "/downloads/temp" plus a result named
    "Marvel Classics Comics 017.cbz" becomes the single path
    "/downloads/tempMarvel Classics Comics 017.cbz". Directory paths in the
    AirDC++ API always carry a trailing separator, so add one when the
    configured value omits it.

    ``sep`` is the AirDC++ host's own separator when known (see
    :meth:`AirDCPPClient.path_separator`); otherwise it is inferred from the
    path, so a Windows-style target ("C:\\downloads\\temp") keeps its
    backslash.
    """
    target = (path or "").strip()
    if not target:
        return None
    if target.endswith(("/", "\\")):
        return target
    if sep not in ("/", "\\"):
        sep = "\\" if ("\\" in target and "/" not in target) else "/"
    return target + sep


def _wrong_separator(target: Optional[str], sep: Optional[str]) -> bool:
    """True when ``target`` is not a valid path shape for the AirDC++ host.

    AirDC++ does not reject a target directory it cannot parse. On a Windows
    host, ``/`` is a forbidden filename character rather than a separator, so
    the whole POSIX path is swallowed into the bundle *name* and sanitized:
    "/downloads/temp/Farmhouse 007.cbr" is queued as the single file
    "_downloads_temp_Farmhouse 007.cbr" in AirDC++'s default download
    directory. The reverse (a Windows path on a Linux host) fails the same
    way. Neither is recoverable by CLU — the configured path has to be one
    the AirDC++ host itself understands — so this is only used to refuse the
    download with an explanation.
    """
    if not target or sep not in ("/", "\\"):
        return False
    return "/" in target if sep == "\\" else "\\" in target


def _separator_error(target: str, sep: str) -> str:
    """Actionable message for a target directory the AirDC++ host can't use."""
    style = "Windows" if sep == "\\" else "Unix"
    example = "C:\\Downloads\\comics\\" if sep == "\\" else "/downloads/comics/"
    return (
        f"Target Directory '{target}' is not a valid path on the AirDC++ host, "
        f"which reports {style}-style paths separated by '{sep}'. Set it to a "
        f"path as AirDC++ itself sees it (e.g. {example}), and make sure CLU can "
        f"read that same folder — either map the AirDC++ download folder into "
        f"CLU's docker-compose, or use the same path as CLU's WATCH folder. "
        f"Left as-is, AirDC++ folds the whole path into the filename and "
        f"downloads it to its own default directory."
    )


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
        "local_target_directory",
        "hub_urls",
    ]

    # Per-instance cache of the host's path separator (see path_separator).
    _path_sep = _UNPROBED

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

        # The connection works; the target directory is the other thing that
        # has to be right, and it fails silently at download time rather than
        # here, so check it while the user is looking at the result.
        target = (cfg.target_directory or "").strip()
        sep = self.path_separator()
        if _wrong_separator(target, sep):
            self.last_error = _separator_error(target, sep)
            return False

        # The local view is CLU's own namespace, so it can simply be checked.
        # Getting it wrong otherwise surfaces only much later, as a finished
        # download that never reaches the library.
        local = (cfg.local_target_directory or "").strip()
        if local and not os.path.isdir(local):
            self.last_error = (
                f"CLU cannot see '{local}'. This is the Target Directory as CLU "
                f"sees it, so it has to be a path inside CLU's own container — "
                f"check the volume mapping in CLU's docker-compose covers the "
                f"folder AirDC++ downloads into."
            )
            return False
        return True

    def get_system_info(self) -> Optional[dict]:
        """Return AirDC++'s ``/system/system_info``, or None when unreadable.

        Note the ``system`` module prefix: the API answers a request to a bare
        ``/system_info`` with ``{"message": "Section not found"}``.
        """
        data = self._json(self._request("GET", "/system/system_info"))
        return data if isinstance(data, dict) else None

    def path_separator(self) -> Optional[str]:
        """The AirDC++ host's own path separator ('/' or '\\'), cached.

        None when the endpoint is unreachable or the running AirDC++ version
        doesn't report one — callers then skip the separator check rather than
        guess at the host's platform.
        """
        if self._path_sep is _UNPROBED:
            # Probing must not clobber a caller's in-flight error state.
            prior_error = self.last_error
            info = self.get_system_info() or {}
            probe_error = self.last_error
            self.last_error = prior_error
            sep = info.get("path_separator")
            self._path_sep = sep if sep in ("/", "\\") else None
            if self._path_sep is None:
                # Say so out loud: an unreadable separator silently disables
                # the Target Directory check, which is how a bad path reaches
                # AirDC++ and comes back as a mangled filename.
                app_logger.warning(
                    f"AirDC++ did not report a path separator"
                    f"{f' ({probe_error})' if probe_error else ''} — the Target "
                    f"Directory check is disabled for this client"
                )
        return self._path_sep

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
        """Wait for the hubs to answer, then fetch the results.

        Polls the search instance rather than the result list: ``queued_count``
        tells us whether the search has even been dispatched to every hub yet
        (AirDC++ paces them), and ``result_count`` tells us when answers have
        stopped arriving. Only then is the result page worth fetching.
        """
        deadline = time.monotonic() + _SEARCH_WAIT_TOTAL
        last_count = -1
        stable = 0

        while time.monotonic() < deadline:
            time.sleep(_SEARCH_POLL_INTERVAL)
            meta = self._json(self._request("GET", f"/search/{instance_id}"))
            if not isinstance(meta, dict):
                break  # can't read progress; fetch whatever is there

            count = _to_int(meta.get("result_count")) or 0
            queued = _to_int(meta.get("queued_count")) or 0

            if queued > 0 or count != last_count:
                # Still being dispatched, or answers are still arriving.
                stable = 0
            else:
                stable += 1
                if stable >= _SEARCH_SETTLE_ROUNDS:
                    break
            last_count = count

        found = self._json(
            self._request("GET", f"/search/{instance_id}/results/0/{_RESULT_PAGE_SIZE}")
        )
        if not isinstance(found, list):
            return []
        if len(found) >= _RESULT_PAGE_SIZE:
            app_logger.info(
                f"AirDC++ search returned at least {_RESULT_PAGE_SIZE} results; "
                f"the list may be truncated"
            )
        return [self._parse_result(r) for r in found if isinstance(r, dict)]

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
        sep = self.path_separator()
        target = _normalize_target_directory(
            target_directory or (self.config.target_directory if self.config else None),
            sep,
        )
        if _wrong_separator(target, sep):
            # Queueing anyway produces a bundle named after the mangled path,
            # landed somewhere CLU can't import from. Refuse and say why.
            self.last_error = _separator_error(target, sep)
            app_logger.error(f"AirDC++ download refused: {self.last_error}")
            return NZBSubmitResult(success=False, error=self.last_error)

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

        # The real response nests the id: {"bundle_info": {"id": N, "merged": false}}.
        # Reading a top-level "id" made every successful grab look like a
        # failure, while the download actually started at the client.
        info = data.get("bundle_info")
        if not isinstance(info, dict):
            info = data
        bundle_id = info.get("id") or info.get("bundle_id")
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
