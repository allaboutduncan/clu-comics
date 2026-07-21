"""
NZBGet Download Client Adapter.

Talks to an NZBGet instance over its JSON-RPC API. PR 1 implements
``test_connection``; submission/status are PR 2 seams.

API reference: https://nzbget.net/api/
"""
import base64
from typing import List, Optional, Union

from core.app_logging import app_logger
from .base import BaseDownloadClient, ClientType, DownloadStatus, NZBSubmitResult
from . import register_download_client

# Short timeout so a wrong host/port fails fast instead of hanging the UI.
_TIMEOUT = 10


def _normalize_nzbget_status(raw: str) -> str:
    """Map an NZBGet history Status string to complete/failed/downloading."""
    up = (raw or "").upper()
    if up.startswith("SUCCESS"):
        return "complete"
    if up.startswith(("FAILURE", "DELETED", "WARNING")):
        return "failed"
    return "downloading"


@register_download_client
class NZBGetClient(BaseDownloadClient):
    """NZBGet download client using the JSON-RPC API."""

    client_type = ClientType.NZBGET
    display_name = "NZBGet"
    requires_auth = True
    config_fields = [
        "host",
        "port",
        "username",
        "password",
        "category",
        "priority",
        "use_ssl",
        "url_base",
    ]

    def test_connection(self) -> bool:
        """Verify the NZBGet host is reachable and credentials are valid.

        Calls the ``version`` JSON-RPC method. Username/password are optional
        (NZBGet may run with authentication disabled, as with Sonarr/Radarr);
        Basic auth is only sent when credentials are provided.
        """
        self.last_error = None
        cfg = self.config
        if not cfg:
            self.last_error = "No configuration provided"
            return False

        url = f"{self._base_url()}/jsonrpc"
        try:
            import requests

            auth = None
            if cfg.username or cfg.password:
                auth = (cfg.username or "", cfg.password or "")

            resp = requests.post(
                url,
                json={"method": "version", "params": [], "id": 1},
                auth=auth,
                timeout=_TIMEOUT,
            )
            if resp.status_code == 401:
                self.last_error = "401 Unauthorized — check username/password"
                return False
            if resp.status_code != 200:
                self.last_error = f"HTTP {resp.status_code} from {url}"
                return False
            try:
                data = resp.json()
            except ValueError:
                self.last_error = (
                    f"Non-JSON response from {url} — is this an NZBGet JSON-RPC endpoint?"
                )
                return False
            if isinstance(data, dict) and data.get("result") is not None:
                return True
            self.last_error = f"Unexpected JSON-RPC response: {data}"
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
            app_logger.error(f"NZBGet connection test failed: {e}")
            return False

    def _rpc(self, method: str, params: list):
        """Call an NZBGet JSON-RPC method; return the result or raise."""
        import requests

        cfg = self.config
        auth = None
        if cfg and (cfg.username or cfg.password):
            auth = (cfg.username or "", cfg.password or "")
        resp = requests.post(
            f"{self._base_url()}/jsonrpc",
            json={"method": method, "params": params, "id": 1},
            auth=auth,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(f"NZBGet {method} error: {msg}")
        return data.get("result")

    def add_nzb(
        self,
        nzb: Union[bytes, str],
        name: str,
        category: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> NZBSubmitResult:
        """Submit an NZB via append(). ``nzb`` may be raw bytes or a URL string."""
        self.last_error = None
        cfg = self.config
        cat = category if category is not None else (cfg.category if cfg else None)
        prio = priority if priority is not None else (cfg.priority if cfg else None)
        # NZBGet append() detects a URL in the content field; bytes must be base64.
        content = nzb if isinstance(nzb, str) else base64.b64encode(nzb).decode("ascii")
        # append(NZBFilename, NZBContent, Category, Priority, AddToTop,
        #        AddPaused, DupeKey, DupeScore, DupeMode, PPParameters)
        # The trailing PPParameters array is required by modern NZBGet (v13+);
        # omitting it fails with "Invalid parameter (Parameters)".
        params = [name, content, cat or "", int(prio) if prio is not None else 0,
                  False, False, "", 0, "SCORE", []]
        try:
            nzbid = self._rpc("append", params)
            if isinstance(nzbid, int) and nzbid > 0:
                return NZBSubmitResult(client_id=str(nzbid), success=True)
            self.last_error = f"NZBGet append returned {nzbid}"
            return NZBSubmitResult(success=False, error=self.last_error)
        except Exception as e:
            self.last_error = str(e)
            app_logger.error(f"NZBGet add_nzb failed: {e}")
            return NZBSubmitResult(success=False, error=str(e))

    def get_history(self) -> List[DownloadStatus]:
        """Return completed/failed downloads from NZBGet history."""
        try:
            rows = self._rpc("history", [True]) or []
            out = []
            for r in rows:
                out.append(DownloadStatus(
                    client_id=str(r.get("NZBID", "")),
                    name=r.get("Name"),
                    status=_normalize_nzbget_status(r.get("Status", "")),
                    category=r.get("Category") or None,
                    storage_path=r.get("FinalDir") or r.get("DestDir") or None,
                ))
            return out
        except Exception as e:
            app_logger.error(f"NZBGet get_history failed: {e}")
            return []

    def get_status(self, client_id: str) -> Optional[DownloadStatus]:
        """Return the status of a single download by NZBID (active then history)."""
        try:
            for g in self._rpc("listgroups", [0]) or []:
                if str(g.get("NZBID", "")) == str(client_id):
                    remaining = g.get("RemainingSizeMB") or 0
                    total = (g.get("FileSizeMB") or 0) or 1
                    pct = max(0.0, min(100.0, (1 - remaining / total) * 100))
                    return DownloadStatus(
                        client_id=str(client_id), name=g.get("NZBName"),
                        status="downloading", percent=pct, category=g.get("Category"),
                    )
        except Exception as e:
            app_logger.error(f"NZBGet get_status listgroups failed: {e}")

        for h in self.get_history():
            if h.client_id == str(client_id):
                return h
        return None
