"""
SABnzbd Download Client Adapter.

Talks to a SABnzbd instance over its HTTP API. PR 1 implements
``test_connection``; submission/status are PR 2 seams.

API reference: https://sabnzbd.org/wiki/advanced/api
"""
from typing import Optional, Union

from core.app_logging import app_logger
from .base import BaseDownloadClient, ClientType, NZBSubmitResult
from . import register_download_client

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

    def add_nzb(self, nzb: Union[bytes, str], name, category=None, priority=None) -> NZBSubmitResult:
        # PR2: POST mode=addfile (nzb bytes) or GET mode=addurl&name=<nzburl>,
        # params apikey, cat=<category>, priority. Read the completed path from
        # mode=history&output=json (the `storage` field) for the mover.
        raise NotImplementedError("add_nzb is implemented in PR 2")

    def get_history(self):
        # PR2: GET mode=history&output=json -> slots[].storage (completed path).
        raise NotImplementedError("get_history is implemented in PR 2")

    def get_status(self, client_id: str):
        # PR2: GET mode=queue / mode=history filtered by nzo_id.
        raise NotImplementedError("get_status is implemented in PR 2")
