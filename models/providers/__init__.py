"""
Metadata Provider Registry and Factory.

This module provides the registration system and factory functions for
metadata providers. Providers register themselves using the @register_provider
decorator, and instances are created via get_provider() or get_provider_by_name().

Usage:
    from models.providers import register_provider, get_provider, ProviderType

    @register_provider
    class MyProvider(BaseProvider):
        provider_type = ProviderType.METRON
        ...

    # Get a provider instance
    provider = get_provider(ProviderType.METRON, credentials)
    results = provider.search_series("Batman", 2020)
"""
from typing import Dict, Type, List, Optional

from .base import (
    BaseProvider,
    ProviderType,
    ProviderCredentials,
    ProviderRateLimitError,
    SearchResult,
    IssueResult,
    extract_issue_number
)

# Registry of provider implementations
_PROVIDER_REGISTRY: Dict[ProviderType, Type[BaseProvider]] = {}


def register_provider(provider_class: Type[BaseProvider]) -> Type[BaseProvider]:
    """
    Decorator to register a provider implementation.

    Usage:
        @register_provider
        class MetronProvider(BaseProvider):
            provider_type = ProviderType.METRON
            ...
    """
    if not hasattr(provider_class, 'provider_type'):
        raise ValueError(f"Provider class {provider_class.__name__} must define provider_type")
    _PROVIDER_REGISTRY[provider_class.provider_type] = provider_class
    return provider_class


def get_provider(
    provider_type: ProviderType,
    credentials: Optional[ProviderCredentials] = None
) -> BaseProvider:
    """
    Factory function to create a provider instance.

    Args:
        provider_type: The type of provider to create
        credentials: Optional credentials for the provider

    Returns:
        An instance of the requested provider

    Raises:
        ValueError: If the provider type is not registered
    """
    if provider_type not in _PROVIDER_REGISTRY:
        raise ValueError(f"Unknown provider type: {provider_type.value}")
    return _PROVIDER_REGISTRY[provider_type](credentials)


def get_provider_by_name(
    name: str,
    credentials: Optional[ProviderCredentials] = None
) -> BaseProvider:
    """
    Factory function to create a provider instance using string name.

    Args:
        name: The provider name (e.g., "metron", "comicvine")
        credentials: Optional credentials for the provider

    Returns:
        An instance of the requested provider

    Raises:
        ValueError: If the provider name is not recognized
    """
    try:
        provider_type = ProviderType(name.lower())
    except ValueError:
        raise ValueError(f"Unknown provider: {name}")
    return get_provider(provider_type, credentials)


def get_registered_providers() -> List[Type[BaseProvider]]:
    """
    Get list of registered provider classes.

    Returns:
        List of provider class types
    """
    return list(_PROVIDER_REGISTRY.values())


def get_available_providers() -> List[Dict]:
    """
    Get list of available providers with their configuration details.

    Returns:
        List of dictionaries containing provider metadata
    """
    return [
        {
            "type": p.provider_type.value,
            "name": p.display_name,
            "requires_auth": p.requires_auth,
            "auth_fields": p.auth_fields,
            "rate_limit": p.rate_limit
        }
        for p in _PROVIDER_REGISTRY.values()
    ]


def is_provider_registered(provider_type: ProviderType) -> bool:
    """
    Check if a provider type is registered.

    Args:
        provider_type: The provider type to check

    Returns:
        True if the provider is registered
    """
    return provider_type in _PROVIDER_REGISTRY


def get_provider_class(provider_type: ProviderType) -> Optional[Type[BaseProvider]]:
    """
    Get the provider class for a given type without instantiating.

    Args:
        provider_type: The provider type

    Returns:
        The provider class, or None if not registered
    """
    return _PROVIDER_REGISTRY.get(provider_type)


# Import provider implementations to register them
# These imports trigger the @register_provider decorator
from .metron_provider import MetronProvider
from .comicvine_provider import ComicVineProvider
from .gcd_provider import GCDProvider
from .anilist_provider import AniListProvider
from .bedetheque_provider import BedethequeProvider
from .mangadex_provider import MangaDexProvider


# Re-export commonly used items for convenience
__all__ = [
    # Base classes and types
    'BaseProvider',
    'ProviderType',
    'ProviderCredentials',
    'ProviderRateLimitError',
    'SearchResult',
    'IssueResult',
    'extract_issue_number',
    # Registry functions
    'register_provider',
    'get_provider',
    'get_provider_by_name',
    'get_registered_providers',
    'get_available_providers',
    'is_provider_registered',
    'get_provider_class',
    # Provider implementations
    'MetronProvider',
    'ComicVineProvider',
    'GCDProvider',
    'AniListProvider',
    'BedethequeProvider',
    'MangaDexProvider'
]
