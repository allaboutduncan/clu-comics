"""
Newznab Indexer Adapter.

Queries a Newznab-compatible indexer over its HTTP API. PR 1 implements
``test_connection``; ``search`` is a PR 2 seam.

API reference: https://newznab.readthedocs.io/en/latest/misc/api/
"""
from typing import List, Optional

from core.app_logging import app_logger
from .base import BaseIndexer, IndexerType, NZBSearchResult
from . import register_indexer

# Short timeout so an unreachable indexer fails fast instead of hanging the UI.
_TIMEOUT = 15


@register_indexer
class NewznabIndexer(BaseIndexer):
    """Newznab-compatible indexer."""

    indexer_type = IndexerType.NEWZNAB
    display_name = "Newznab"

    def test_connection(self) -> bool:
        """Verify the indexer URL is reachable and the API key is valid.

        Runs an empty ``t=search`` (which validates the API key); a bad
        key returns a Newznab ``<error code="100" .../>`` element.
        """
        self.last_error = None
        cfg = self.config
        if not cfg or not cfg.url:
            self.last_error = "Indexer URL is required"
            return False

        url = f"{cfg.url.rstrip('/')}/api"
        try:
            import requests
            from defusedxml import ElementTree

            resp = requests.get(
                url,
                params={
                    "t": "search",
                    "q": "",
                    "apikey": cfg.api_key or "",
                    "o": "xml",
                    "limit": 1,
                },
                timeout=_TIMEOUT,
            )
            if resp.status_code != 200:
                self.last_error = f"HTTP {resp.status_code} from {url}"
                return False

            root = ElementTree.fromstring(resp.content)
            # A Newznab error response is a top-level <error .../> element.
            tag = root.tag.split("}")[-1].lower()
            if tag == "error":
                desc = root.get("description") or root.get("code") or "indexer error"
                self.last_error = f"Indexer rejected the request: {desc}"
                return False
            return True
        except requests.exceptions.ConnectionError:
            self.last_error = f"Could not connect to {url}"
            return False
        except requests.exceptions.Timeout:
            self.last_error = f"Timed out connecting to {url}"
            return False
        except Exception as e:
            self.last_error = str(e)
            app_logger.error(f"Newznab connection test failed: {e}")
            return False

    def search(
        self,
        query: str,
        categories: Optional[List[str]] = None,
        limit: int = 100,
    ) -> List[NZBSearchResult]:
        # PR2: GET /api?t=search&q=<query>&apikey=<key>&cat=<categories>&o=xml.
        # Parse <item> -> <enclosure url=... type="application/x-nzb"/> for
        # nzb_url and <newznab:attr name="size"> into NZBSearchResult, then
        # feed titles into the existing GetComics scorer.
        raise NotImplementedError("search is implemented in PR 2")
