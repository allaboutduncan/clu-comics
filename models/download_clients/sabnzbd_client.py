"""
SABnzbd Download Client Adapter.

Talks to a SABnzbd instance over its HTTP API. PR 1 implements
``test_connection``; submission/status are PR 2 seams.

API reference: https://sabnzbd.org/wiki/advanced/api
"""
from typing import List, Optional, Union

from core.app_logging import app_logger
from .base import BaseDownloadClient, ClientType, DownloadStatus, NZBSubmitResult
from . import register_download_client


def _normalize_sab_status(raw: str) -> str:
    """Map a SABnzbd status string to complete/failed/downloading."""
    low = (raw or "").lower()
    if low == "completed":
        return "complete"
    if low == "failed":
        return "failed"
    return "downloading"

# Short timeout so a wrong host/port fails fast instead of hanging the UI.
_TIMEOUT = 10


@register_download_client
class SABnzbdClient(BaseDownloadClient):
    """SABnzbd download client using the HTTP API."""

    client_type = ClientType.SABNZBD
    display_name = "SABnzbd"
    requires_auth = True
    config_fields = [
        "host",
        "port",
        "api_key",
        "category",
        "priority",
        "use_ssl",
        "url_base",
    ]

    def test_connection(self) -> bool:
        """Verify the SABnzbd host is reachable and the API key is valid.

        Queries ``mode=queue``; a bad API key returns ``{"error": ...}``
        while a good one returns a payload containing ``"queue"``.
        """
        self.last_error = None
        cfg = self.config
        if not cfg or not cfg.api_key:
            self.last_error = "API key is required"
            return False

        url = f"{self._base_url()}/api"
        try:
            import requests

            resp = requests.get(
                url,
                params={
                    "mode": "queue",
                    "output": "json",
                    "apikey": cfg.api_key,
                },
                timeout=_TIMEOUT,
            )
            if resp.status_code != 200:
                self.last_error = f"HTTP {resp.status_code} from {url}"
                return False
            try:
                data = resp.json()
            except ValueError:
                self.last_error = (
                    f"Non-JSON response from {url} — is this a SABnzbd API endpoint?"
                )
                return False
            if not isinstance(data, dict):
                self.last_error = f"Unexpected response from {url}"
                return False
            if data.get("error"):
                self.last_error = str(data["error"])
                return False
            if "queue" in data:
                return True
            self.last_error = "Response did not contain a queue — check the API key"
            return False
        except requests.exceptions.ConnectionError:
            self.last_error = (
                f"Could not connect to {url} — check the host/port are reachable "
                f"from the CLU container (localhost inside Docker is the container "
                f"itself, not your homeserver)"
            )
            return False
        except requests.exceptions.Timeout:
            self.last_error = f"Timed out connecting to {url}"
            return False
        except Exception as e:
            self.last_error = str(e)
            app_logger.error(f"SABnzbd connection test failed: {e}")
            return False

    def add_nzb(
        self,
        nzb: Union[bytes, str],
        name: str,
        category: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> NZBSubmitResult:
        """Submit an NZB URL (str, via addurl) or NZB bytes (via addfile)."""
        self.last_error = None
        cfg = self.config
        if not cfg or not cfg.api_key:
            return NZBSubmitResult(success=False, error="API key is required")

        cat = category if category is not None else cfg.category
        prio = priority if priority is not None else cfg.priority
        url = f"{self._base_url()}/api"
        params = {"apikey": cfg.api_key, "output": "json", "nzbname": name}
        if cat:
            params["cat"] = cat
        if prio is not None:
            params["priority"] = prio
        try:
            import requests

            if isinstance(nzb, str):
                params["mode"] = "addurl"
                params["name"] = nzb
                resp = requests.get(url, params=params, timeout=_TIMEOUT)
            else:
                params["mode"] = "addfile"
                files = {"name": (name, nzb, "application/x-nzb")}
                resp = requests.post(url, params=params, files=files, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("status", False):
                self.last_error = str(data.get("error") or data)
                return NZBSubmitResult(success=False, error=self.last_error)
            nzo_ids = data.get("nzo_ids") or []
            return NZBSubmitResult(client_id=nzo_ids[0] if nzo_ids else None, success=True)
        except Exception as e:
            self.last_error = str(e)
            app_logger.error(f"SABnzbd add_nzb failed: {e}")
            return NZBSubmitResult(success=False, error=str(e))

    def get_history(self) -> List[DownloadStatus]:
        """Return completed/failed downloads from SABnzbd history."""
        cfg = self.config
        if not cfg or not cfg.api_key:
            return []
        url = f"{self._base_url()}/api"
        try:
            import requests

            resp = requests.get(
                url,
                params={"mode": "history", "output": "json", "apikey": cfg.api_key},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            slots = (resp.json().get("history") or {}).get("slots") or []
            out = []
            for s in slots:
                out.append(DownloadStatus(
                    client_id=s.get("nzo_id", ""),
                    name=s.get("name"),
                    status=_normalize_sab_status(s.get("status", "")),
                    category=s.get("category"),
                    storage_path=s.get("storage") or None,
                ))
            return out
        except Exception as e:
            app_logger.error(f"SABnzbd get_history failed: {e}")
            return []

    def get_status(self, client_id: str) -> Optional[DownloadStatus]:
        """Return the status of a single download by nzo_id (queue then history)."""
        cfg = self.config
        if not cfg or not cfg.api_key:
            return None
        url = f"{self._base_url()}/api"
        try:
            import requests

            resp = requests.get(
                url,
                params={"mode": "queue", "output": "json", "apikey": cfg.api_key},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            for s in (resp.json().get("queue") or {}).get("slots") or []:
                if s.get("nzo_id") == client_id:
                    pct = None
                    try:
                        pct = float(s.get("percentage"))
                    except (TypeError, ValueError):
                        pct = None
                    return DownloadStatus(
                        client_id=client_id, name=s.get("filename"),
                        status="downloading", percent=pct, category=s.get("cat"),
                    )
        except Exception as e:
            app_logger.error(f"SABnzbd get_status queue lookup failed: {e}")

        for h in self.get_history():
            if h.client_id == client_id:
                return h
        return None
