"""
Download Client Registry and Factory.

Download clients register themselves using the @register_download_client
decorator, and instances are created via get_download_client() or
get_download_client_by_name().

Usage:
    from models.download_clients import (
        register_download_client, get_download_client, ClientType,
    )

    @register_download_client
    class SABnzbdClient(BaseDownloadClient):
        client_type = ClientType.SABNZBD
        ...

    client = get_download_client(ClientType.SABNZBD, config)
    client.test_connection()
"""
from typing import Dict, List, Optional, Type

from .base import (
    BaseDownloadClient,
    ClientType,
    DownloadClientConfig,
    DownloadStatus,
    NZBSubmitResult,
)

# Registry of download client implementations
_DOWNLOAD_CLIENT_REGISTRY: Dict[ClientType, Type[BaseDownloadClient]] = {}


def register_download_client(
    client_class: Type[BaseDownloadClient],
) -> Type[BaseDownloadClient]:
    """Decorator to register a download client implementation."""
    if not hasattr(client_class, "client_type"):
        raise ValueError(
            f"Download client class {client_class.__name__} must define client_type"
        )
    _DOWNLOAD_CLIENT_REGISTRY[client_class.client_type] = client_class
    return client_class


def get_download_client(
    client_type: ClientType,
    config: Optional[DownloadClientConfig] = None,
) -> BaseDownloadClient:
    """Factory function to create a download client instance."""
    if client_type not in _DOWNLOAD_CLIENT_REGISTRY:
        raise ValueError(f"Unknown download client type: {client_type.value}")
    return _DOWNLOAD_CLIENT_REGISTRY[client_type](config)


def get_download_client_by_name(
    name: str,
    config: Optional[DownloadClientConfig] = None,
) -> BaseDownloadClient:
    """Factory function to create a download client instance by string name."""
    try:
        client_type = ClientType(name.lower())
    except ValueError:
        raise ValueError(f"Unknown download client: {name}")
    return get_download_client(client_type, config)


def get_available_download_clients() -> List[Dict]:
    """Get list of available download clients with their configuration details."""
    return [
        {
            "type": c.client_type.value,
            "name": c.display_name,
            "config_fields": c.config_fields,
            "requires_auth": c.requires_auth,
        }
        for c in _DOWNLOAD_CLIENT_REGISTRY.values()
    ]


def get_download_client_class(
    client_type: ClientType,
) -> Optional[Type[BaseDownloadClient]]:
    """Get the download client class for a given type without instantiating."""
    return _DOWNLOAD_CLIENT_REGISTRY.get(client_type)


def is_download_client_registered(client_type: ClientType) -> bool:
    """Check if a download client type is registered."""
    return client_type in _DOWNLOAD_CLIENT_REGISTRY


# Import client implementations to register them (triggers @register_download_client)
from .sabnzbd_client import SABnzbdClient
from .nzbget_client import NZBGetClient


__all__ = [
    # Base classes and types
    "BaseDownloadClient",
    "ClientType",
    "DownloadClientConfig",
    "DownloadStatus",
    "NZBSubmitResult",
    # Registry functions
    "register_download_client",
    "get_download_client",
    "get_download_client_by_name",
    "get_available_download_clients",
    "get_download_client_class",
    "is_download_client_registered",
    # Client implementations
    "SABnzbdClient",
    "NZBGetClient",
]
