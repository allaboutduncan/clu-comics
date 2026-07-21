"""
Base classes and data types for Usenet download clients.

This module defines the abstract base class that all download clients
(SABnzbd, NZBGet) must implement, along with unified data classes for
client configuration and download status.

PR 1 implements ``test_connection`` fully. NZB submission and status
polling (``add_nzb``, ``get_history``, ``get_status``) are defined as
forward-compatible seams for PR 2 and raise ``NotImplementedError``.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Union


class ClientType(Enum):
    """Enumeration of supported download clients."""
    SABNZBD = "sabnzbd"
    NZBGET = "nzbget"


@dataclass
class DownloadClientConfig:
    """Connection/config for a Usenet download client."""
    host: Optional[str] = None
    port: Optional[int] = None
    api_key: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    category: Optional[str] = None
    priority: Optional[int] = None
    use_ssl: bool = False
    url_base: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, excluding None values."""
        return {k: v for k, v in {
            "host": self.host,
            "port": self.port,
            "api_key": self.api_key,
            "username": self.username,
            "password": self.password,
            "category": self.category,
            "priority": self.priority,
            "use_ssl": self.use_ssl,
            "url_base": self.url_base,
        }.items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DownloadClientConfig":
        """Create from dictionary."""
        return cls(
            host=data.get("host"),
            port=data.get("port"),
            api_key=data.get("api_key"),
            username=data.get("username"),
            password=data.get("password"),
            category=data.get("category"),
            priority=data.get("priority"),
            use_ssl=bool(data.get("use_ssl", False)),
            url_base=data.get("url_base"),
        )


@dataclass
class NZBSubmitResult:
    """Result of submitting an NZB to a download client (PR 2 seam)."""
    client_id: Optional[str] = None
    success: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client_id": self.client_id,
            "success": self.success,
            "error": self.error,
        }


@dataclass
class DownloadStatus:
    """Status of a download tracked by a client (PR 2 seam).

    ``storage_path`` is the completed file/directory the PR 2 mover will
    consume to hand the finished download to CLU's WATCH folder.
    """
    client_id: str
    name: Optional[str] = None
    status: Optional[str] = None
    percent: Optional[float] = None
    category: Optional[str] = None
    storage_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client_id": self.client_id,
            "name": self.name,
            "status": self.status,
            "percent": self.percent,
            "category": self.category,
            "storage_path": self.storage_path,
        }


class BaseDownloadClient(ABC):
    """
    Abstract base class for all Usenet download clients.

    Subclasses set the class attributes and implement ``test_connection``.
    """

    # Class attributes to be overridden by subclasses
    client_type: ClientType
    display_name: str
    config_fields: List[str] = []
    requires_auth: bool = True

    def __init__(self, config: Optional[DownloadClientConfig] = None):
        """Initialize the client with optional connection config."""
        self.config = config
        # Populated by test_connection() on failure so callers can surface a
        # human-readable reason (connection refused, 401, bad response, ...).
        self.last_error: Optional[str] = None

    def _base_url(self) -> str:
        """Build the base URL ``scheme://host:port[/url_base]`` from config.

        If the user already embedded a scheme and/or ``:port`` in the host
        field, those are honored and not duplicated.
        """
        cfg = self.config or DownloadClientConfig()
        host = (cfg.host or "localhost").strip().rstrip("/")

        # Honor a scheme embedded in the host field, else derive from use_ssl.
        if "://" in host:
            scheme, host = host.split("://", 1)
        else:
            scheme = "https" if cfg.use_ssl else "http"

        base = f"{scheme}://{host}"
        # Only append the port if the host does not already carry one.
        if cfg.port and ":" not in host:
            base = f"{base}:{cfg.port}"
        if cfg.url_base:
            base = f"{base}/{cfg.url_base.strip('/')}"
        return base

    @abstractmethod
    def test_connection(self) -> bool:
        """
        Verify connectivity and credentials to the download client.

        Returns:
            True if connection is successful, False otherwise
        """
        pass

    def add_nzb(
        self,
        nzb: Union[bytes, str],
        name: str,
        category: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> NZBSubmitResult:
        """Submit an NZB (bytes) or NZB URL (str) to the client. PR 2 seam."""
        raise NotImplementedError("add_nzb is implemented in PR 2")

    def get_history(self) -> List[DownloadStatus]:
        """Return completed downloads (for the PR 2 poller/mover). PR 2 seam."""
        raise NotImplementedError("get_history is implemented in PR 2")

    def get_status(self, client_id: str) -> Optional[DownloadStatus]:
        """Return the status of a single download by client id. PR 2 seam."""
        raise NotImplementedError("get_status is implemented in PR 2")

    def get_client_info(self) -> Dict[str, Any]:
        """Get client metadata for API responses."""
        return {
            "type": self.client_type.value,
            "name": self.display_name,
            "config_fields": self.config_fields,
            "requires_auth": self.requires_auth,
        }
