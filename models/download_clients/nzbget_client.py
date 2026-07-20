"""
NZBGet Download Client Adapter.

Talks to an NZBGet instance over its JSON-RPC API. PR 1 implements
``test_connection``; submission/status are PR 2 seams.

API reference: https://nzbget.net/api/
"""
from typing import Optional, Union

from core.app_logging import app_logger
from .base import BaseDownloadClient, ClientType, NZBSubmitResult
from . import register_download_client

# Short timeout so a wrong host/port fails fast instead of hanging the UI.
_TIMEOUT = 10


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

    def add_nzb(self, nzb: Union[bytes, str], name, category=None, priority=None) -> NZBSubmitResult:
        # PR2: JSON-RPC append(filename, base64_nzb, category, priority, ...).
        raise NotImplementedError("add_nzb is implemented in PR 2")

    def get_history(self):
        # PR2: JSON-RPC history(True) + listgroups(); DestDir/FinalDir =
        # completed path consumed by the mover.
        raise NotImplementedError("get_history is implemented in PR 2")

    def get_status(self, client_id: str):
        # PR2: JSON-RPC listgroups() filtered by NZBID, then history(True).
        raise NotImplementedError("get_status is implemented in PR 2")
