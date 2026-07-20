"""
Base classes and data types for Usenet indexers.

Defines the abstract base class that all indexers (Newznab-compatible)
must implement, plus unified data classes for indexer config and search
results.

PR 1 implements ``test_connection`` fully. ``search`` is a forward-
compatible seam for PR 2 and raises ``NotImplementedError``.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class IndexerType(Enum):
    """Enumeration of supported indexer protocols."""
    NEWZNAB = "newznab"


@dataclass
class IndexerConfig:
    """Configuration for a single indexer."""
    name: str
    url: str
    api_key: Optional[str] = None
    categories: Optional[str] = None  # comma-separated, e.g. "7000,7030"
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "api_key": self.api_key,
            "categories": self.categories,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IndexerConfig":
        return cls(
            name=data.get("name", ""),
            url=data.get("url", ""),
            api_key=data.get("api_key"),
            categories=data.get("categories"),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class NZBSearchResult:
    """A single NZB search result (PR 2 scoring input).

    Field names are kept close to what the GetComics scorer consumes
    (``title``, ``size``) so PR 2 needs only a thin adapter.
    """
    indexer_id: int
    indexer_name: str
    title: str
    nzb_url: str
    size: Optional[int] = None
    categories: Optional[str] = None
    pubdate: Optional[str] = None
    guid: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "indexer_id": self.indexer_id,
            "indexer_name": self.indexer_name,
            "title": self.title,
            "nzb_url": self.nzb_url,
            "size": self.size,
            "categories": self.categories,
            "pubdate": self.pubdate,
            "guid": self.guid,
        }


class BaseIndexer(ABC):
    """Abstract base class for all indexers."""

    # Class attributes to be overridden by subclasses
    indexer_type: IndexerType
    display_name: str

    def __init__(self, config: IndexerConfig):
        self.config = config
        # Populated by test_connection() on failure for a readable reason.
        self.last_error: Optional[str] = None

    @abstractmethod
    def test_connection(self) -> bool:
        """Verify the indexer URL is reachable and the API key is valid."""
        pass

    @abstractmethod
    def search(
        self,
        query: str,
        categories: Optional[List[str]] = None,
        limit: int = 100,
    ) -> List[NZBSearchResult]:
        """Search the indexer for NZBs matching the query. PR 2 seam."""
        pass
