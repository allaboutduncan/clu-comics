"""
Torznab Indexer Adapter.

Queries a Torznab-compatible indexer over its HTTP API — the torrent-tracker
counterpart to Newznab, using the same RSS-based protocol with torrent-
specific attributes (``seeders``/``peers``) layered on top. This is exactly
the protocol Prowlarr serves for each torrent tracker it manages, but nothing
here is Prowlarr-specific: any Torznab-compatible indexer works.

API reference: https://newznab.readthedocs.io/en/latest/misc/api/ (Torznab
reuses the Newznab API shape; category 5000-5999 book/comic ranges vary by
tracker, so — unlike Newznab's comics category 7030 — there is no single
default category to fall back to here).
"""
from typing import List, Optional

from core.app_logging import app_logger
from .base import BaseIndexer, IndexerType, TorrentSearchResult
from . import register_indexer

# Short timeout so an unreachable indexer fails fast instead of hanging the UI.
_TIMEOUT = 15


@register_indexer
class TorznabIndexer(BaseIndexer):
    """Torznab-compatible indexer."""

    indexer_type = IndexerType.TORZNAB
    display_name = "Torznab"

    def test_connection(self) -> bool:
        """Verify the indexer URL is reachable and the API key is valid.

        Uses ``t=caps`` (the Newznab/Torznab capabilities call), which
        requires no query — some indexers reject an empty ``t=search`` with a
        "Missing parameter" error. A bad API key returns a ``<error .../>``.
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
            # A Newznab/Torznab error response is a top-level <error .../> element.
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
            app_logger.error(f"Torznab connection test failed: {e}")
            return False

    def search(
        self,
        query: str,
        categories: Optional[List[str]] = None,
        limit: int = 100,
        indexer_id: int = 0,
    ) -> List[TorrentSearchResult]:
        """Run a Torznab ``t=search`` and parse results into TorrentSearchResult."""
        self.last_error = None
        cfg = self.config
        if not cfg or not cfg.url:
            self.last_error = "Indexer URL is required"
            return []

        # Unlike Newznab's comics category (7030), torrent trackers don't
        # agree on one comics category number, so there is no default here —
        # an unconfigured category just omits the ``cat`` filter entirely.
        cats = categories or (
            [c.strip() for c in cfg.categories.split(",") if c.strip()]
            if cfg.categories else []
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
                    f"Torznab '{cfg.name}': q='{query}' -> {self.last_error}"
                )
                return []
            root = ElementTree.fromstring(resp.content)
            if root.tag.split("}")[-1].lower() == "error":
                self.last_error = (
                    root.get("description") or root.get("code") or "indexer error"
                )
                app_logger.warning(
                    f"Torznab '{cfg.name}': q='{query}' -> error: {self.last_error}"
                )
                return []
            items, results = self._parse_items(root, indexer_id)
            app_logger.info(
                f"Torznab '{cfg.name}': q='{query}' -> {items} item(s), "
                f"{len(results)} usable"
            )
            if items and not results:
                self.last_error = (
                    f"{items} item(s) returned but none had a usable download link"
                )
            return results
        except Exception as e:
            self.last_error = str(e)
            app_logger.error(f"Torznab search failed for '{cfg.name}': {e}")
            return []

    def _parse_items(self, root, indexer_id: int):
        """Parse RSS <item> nodes; return (item_count, [TorrentSearchResult])."""
        def _local(tag):
            return tag.split("}")[-1].lower()

        def _to_int(val):
            try:
                return int(val)
            except (TypeError, ValueError):
                return None

        results = []
        item_count = 0
        for item in root.iter():
            if _local(item.tag) != "item":
                continue
            item_count += 1
            title = None
            download_url = None
            link = None
            size = None
            pubdate = None
            guid = None
            seeders = None
            peers = None
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
                    if "torrent" in ctype or "x-bittorrent" in ctype or not download_url:
                        download_url = child.get("url")
                    length = child.get("length")
                    if length and size is None:
                        size = _to_int(length)
                elif name == "attr":
                    # <torznab:attr name="size"/"seeders"/"peers" value="..."/>
                    attr_name = child.get("name")
                    value = child.get("value")
                    if attr_name == "size" and size is None:
                        size = _to_int(value)
                    elif attr_name == "seeders":
                        seeders = _to_int(value)
                    elif attr_name == "peers":
                        peers = _to_int(value)
                    elif attr_name == "magneturl" and not download_url:
                        download_url = value
            # Some Torznab variants omit <enclosure> and put the download URL
            # (often a magnet link) in <link> instead — fall back to it so
            # items aren't dropped.
            if not download_url and link:
                download_url = link
            if title and download_url:
                results.append(TorrentSearchResult(
                    indexer_id=indexer_id,
                    indexer_name=self.config.name,
                    title=title,
                    download_url=download_url,
                    size=size,
                    categories=self.config.categories,
                    pubdate=pubdate,
                    guid=guid,
                    seeders=seeders,
                    peers=peers,
                ))
        return item_count, results
