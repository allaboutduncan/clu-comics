"""
Indexer Registry and Factory.

Indexers register themselves using the @register_indexer decorator, and
instances are created via get_indexer_impl().

Usage:
    from models.indexers import register_indexer, get_indexer_impl, IndexerType

    @register_indexer
    class NewznabIndexer(BaseIndexer):
        indexer_type = IndexerType.NEWZNAB
        ...

    indexer = get_indexer_impl(IndexerType.NEWZNAB, config)
    indexer.test_connection()
"""
from typing import Dict, List, Type

from .base import (
    BaseIndexer,
    IndexerConfig,
    IndexerType,
    NZBSearchResult,
)

# Registry of indexer implementations
_INDEXER_REGISTRY: Dict[IndexerType, Type[BaseIndexer]] = {}


def register_indexer(indexer_class: Type[BaseIndexer]) -> Type[BaseIndexer]:
    """Decorator to register an indexer implementation."""
    if not hasattr(indexer_class, "indexer_type"):
        raise ValueError(
            f"Indexer class {indexer_class.__name__} must define indexer_type"
        )
    _INDEXER_REGISTRY[indexer_class.indexer_type] = indexer_class
    return indexer_class


def get_indexer_impl(indexer_type: IndexerType, config: IndexerConfig) -> BaseIndexer:
    """Factory function to create an indexer instance."""
    if indexer_type not in _INDEXER_REGISTRY:
        raise ValueError(f"Unknown indexer type: {indexer_type.value}")
    return _INDEXER_REGISTRY[indexer_type](config)


def get_available_indexer_types() -> List[Dict]:
    """Get list of available indexer types."""
    return [
        {"type": i.indexer_type.value, "name": i.display_name}
        for i in _INDEXER_REGISTRY.values()
    ]


# Import indexer implementations to register them (triggers @register_indexer)
from .newznab_indexer import NewznabIndexer


__all__ = [
    "BaseIndexer",
    "IndexerConfig",
    "IndexerType",
    "NZBSearchResult",
    "register_indexer",
    "get_indexer_impl",
    "get_available_indexer_types",
    "NewznabIndexer",
]
