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

        Uses ``t=caps`` (the Newznab capabilities call), which requires no
        query — some indexers reject an empty ``t=search`` with a "Missing
        parameter" error. A bad API key returns a Newznab ``<error .../>``.
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
                    "t": "caps",
                    "apikey": cfg.api_key or "",
                    "o": "xml",
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
        indexer_id: int = 0,
    ) -> List[NZBSearchResult]:
        """Run a Newznab ``t=search`` and parse results into NZBSearchResult."""
        self.last_error = None
        cfg = self.config
        if not cfg or not cfg.url:
            self.last_error = "Indexer URL is required"
            return []

        # Default to the Newznab comics category (7030) — CLU only wants comics,
        # and some indexers reject an uncategorised search.
        cats = categories or (
            [c.strip() for c in cfg.categories.split(",") if c.strip()]
            if cfg.categories else ["7030"]
        )
        url = f"{cfg.url.rstrip('/')}/api"
        params = {
            "t": "search",
            "q": query,
            "apikey": cfg.api_key or "",
            "o": "xml",
            "limit": limit,
        }
        if cats:
            params["cat"] = ",".join(cats)
        try:
            import requests
            from defusedxml import ElementTree

            resp = requests.get(url, params=params, timeout=_TIMEOUT)
            if resp.status_code != 200:
                self.last_error = f"HTTP {resp.status_code} from {url}"
                app_logger.warning(
                    f"Newznab '{cfg.name}': q='{query}' -> {self.last_error}"
                )
                return []
            root = ElementTree.fromstring(resp.content)
            if root.tag.split("}")[-1].lower() == "error":
                self.last_error = (
                    root.get("description") or root.get("code") or "indexer error"
                )
                app_logger.warning(
                    f"Newznab '{cfg.name}': q='{query}' -> error: {self.last_error}"
                )
                return []
            items, results = self._parse_items(root, indexer_id)
            app_logger.info(
                f"Newznab '{cfg.name}': q='{query}' -> {items} item(s), "
                f"{len(results)} usable"
            )
            if items and not results:
                self.last_error = (
                    f"{items} item(s) returned but none had a usable NZB link"
                )
            return results
        except Exception as e:
            self.last_error = str(e)
            app_logger.error(f"Newznab search failed for '{cfg.name}': {e}")
            return []

    def _parse_items(self, root, indexer_id: int):
        """Parse RSS <item> nodes; return (item_count, [NZBSearchResult])."""
        def _local(tag):
            return tag.split("}")[-1].lower()

        results = []
        item_count = 0
        for item in root.iter():
            if _local(item.tag) != "item":
                continue
            item_count += 1
            title = None
            nzb_url = None
            link = None
            size = None
            pubdate = None
            guid = None
            for child in item:
                name = _local(child.tag)
                if name == "title":
                    title = (child.text or "").strip()
                elif name == "pubdate":
                    pubdate = (child.text or "").strip()
                elif name == "guid":
                    guid = (child.text or "").strip()
                elif name == "link":
                    link = (child.text or "").strip()
                elif name == "enclosure":
                    ctype = child.get("type", "")
                    if "nzb" in ctype or not nzb_url:
                        nzb_url = child.get("url")
                    length = child.get("length")
                    if length and size is None:
                        try:
                            size = int(length)
                        except ValueError:
                            pass
                elif name == "attr":
                    # <newznab:attr name="size" value="123"/>
                    if child.get("name") == "size":
                        try:
                            size = int(child.get("value"))
                        except (TypeError, ValueError):
                            pass
            # Some Newznab variants omit <enclosure> and put the NZB download
            # URL in <link> instead — fall back to it so items aren't dropped.
            if not nzb_url and link:
                nzb_url = link
            if title and nzb_url:
                results.append(NZBSearchResult(
                    indexer_id=indexer_id,
                    indexer_name=self.config.name,
                    title=title,
                    nzb_url=nzb_url,
                    size=size,
                    categories=self.config.categories,
                    pubdate=pubdate,
                    guid=guid,
                ))
        return item_count, results
