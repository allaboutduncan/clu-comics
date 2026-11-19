"""
Base classes and data types for metadata providers.

This module defines the abstract base class that all metadata providers must implement,
along with unified data classes for search results and issue data.
"""
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from enum import Enum

from core.filename_parse import SIGN_CHARS, SIGN_CLASS, prefix_before_first_group


# Browser-like User-Agent for provider HTTP clients. Some upstreams (e.g. ComicVine
# and comics.org, both fronted by Cloudflare) reject default library User-Agents like
# ``python-requests/x`` or ``Simyan/x`` with a 403. Overridable via PROVIDER_USER_AGENT.
DEFAULT_PROVIDER_USER_AGENT = os.environ.get(
    "PROVIDER_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)


class ProviderType(Enum):
    """Enumeration of supported metadata providers."""
    METRON = "metron"
    COMICVINE = "comicvine"
    COMICVINE_SQLITE = "comicvine_sqlite"
    GCD = "gcd"
    GCD_API = "gcd_api"
    INDUCKS = "inducks"
    ANILIST = "anilist"
    BEDETHEQUE = "bedetheque"
    MANGADEX = "mangadex"
    MANGAUPDATES = "mangaupdates"


@dataclass
class SearchResult:
    """Unified search result across all providers."""
    provider: ProviderType
    id: str
    title: str
    year: Optional[int] = None
    publisher: Optional[str] = None
    issue_count: Optional[int] = None
    cover_url: Optional[str] = None
    description: Optional[str] = None
    alternate_title: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "provider": self.provider.value,
            "id": self.id,
            "title": self.title,
            "year": self.year,
            "publisher": self.publisher,
            "issue_count": self.issue_count,
            "cover_url": self.cover_url,
            "description": self.description,
            "alternate_title": self.alternate_title
        }


@dataclass
class IssueResult:
    """Unified issue data across all providers."""
    provider: ProviderType
    id: str
    series_id: str
    issue_number: str
    title: Optional[str] = None
    cover_date: Optional[str] = None
    store_date: Optional[str] = None
    cover_url: Optional[str] = None
    summary: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "provider": self.provider.value,
            "id": self.id,
            "series_id": self.series_id,
            "issue_number": self.issue_number,
            "title": self.title,
            "cover_date": self.cover_date,
            "store_date": self.store_date,
            "cover_url": self.cover_url,
            "summary": self.summary
        }


@dataclass
class ProviderCredentials:
    """Credentials for a metadata provider."""
    api_key: Optional[str] = None
    api_token: Optional[str] = None  # Metron: bearer token, preferred over user/pass
    username: Optional[str] = None
    password: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    database_path: Optional[str] = None  # GCD: path to the local SQLite dump

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, excluding None values."""
        return {k: v for k, v in {
            "api_key": self.api_key,
            "api_token": self.api_token,
            "username": self.username,
            "password": self.password,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "database_path": self.database_path
        }.items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderCredentials":
        """Create from dictionary."""
        return cls(
            api_key=data.get("api_key"),
            api_token=data.get("api_token"),
            username=data.get("username"),
            password=data.get("password"),
            host=data.get("host"),
            port=data.get("port"),
            database=data.get("database"),
            database_path=data.get("database_path")
        )


def extract_issue_number(filename: str) -> Optional[str]:
    """
    Extract issue number from a comic filename.

    Handles patterns like:
    - "Amazing Spider-Man (2018) Issue 080.BEY.cbz" -> "80.BEY"
    - "Amazing Spider-Man (1999) Issue 700.1.cbz" -> "700.1"
    - "Amazing Spider-Man 078.BEY (2022).cbz" -> "78.BEY"
    - "Amazing Spider-Man 001.cbz" -> "1"
    - "Batman #42.cbz" -> "42"
    - "X-Men v2 012.cbz" -> "12"
    - "Gen 13 013A.cbz" -> "13A"  (letter-suffixed variant, stored literally
      by providers like ComicVine)
    - "Spider-Man -1 (1990).cbz" -> "-1"  (the sign is part of the number)
    - "Detective Comics 156 (1937) - Batmobile of 1950.cbz" -> "156"
    - "Absolute Flash V01 - Of Two Worlds 2025.cbz" -> "1"

    Args:
        filename: Comic filename

    Returns:
        Issue number as string, or None if not found
    """
    # Remove extension
    name = os.path.splitext(filename)[0]

    # Strip parenthetical groups for digit-based patterns so that
    # year groups don't interfere with finding the last digit sequence.
    # e.g. "Spider-Man 2099 001 (1992)" -> "Spider-Man 2099 001"
    name_clean = re.sub(r'\s*\([^)]*\)', '', name).strip()

    # Issue-number suffix: an optional decimal/alpha point-suffix (".1",
    # ".BEY") and/or an optional directly-attached letter suffix ("A" in
    # "13A", "AU" in "1AU" — variant designations providers store literally).
    # The negative lookahead keeps ordinal endings ("5th", "100th", "1st")
    # from being mistaken for a letter suffix.
    _SUFFIX = (
        r'(?:\.\w+)?'
        r'(?:(?![sS][tT]\b|[nN][dD]\b|[rR][dD]\b|[tT][hH]\b)[A-Za-z]{1,3})?'
    )

    # Try various patterns (ordered by specificity)
    # Uses lookbehind (?<=\s) instead of \s+ so finditer can find all matches
    #
    # A sign glued to the digits is part of the number (issue -1, #778): it is
    # accepted after "Issue ", after "#" and after whitespace, never after a
    # space of its own -- the dash of "Black Cat - 001" is a separator.
    _S = rf'{SIGN_CLASS}?'
    explicit = [
        rf'\b[Ii]ssue\s+({_S}\d+{_SUFFIX})',     # Issue 080, Issue 700.1, Issue -1
        rf'#({_S}\d+{_SUFFIX})',                 # #42 or #42.1 or #42.BEY or #13A
    ]
    for pattern in explicit:
        match = re.search(pattern, name)
        if match:
            return _normalize_issue_number(match.group(1))

    # The first "(...)"/"[...]" group is an anchor: the issue number is the
    # LAST number before it, whatever its width, and everything after it is
    # title text. Reading the whole name instead took the year out of
    # "Detective Comics 156 (1937) - Batmobile of 1950" (#778).
    prefix, has_group = prefix_before_first_group(name)
    if has_group:
        # "01 of 12 (2020)": the count is not the issue.
        prefix = re.sub(r'\s+of\s+\d+\s*$', '', prefix, flags=re.IGNORECASE)
        matches = list(re.finditer(rf'(?<=\s)({_S}\d+{_SUFFIX})(?:\s|$)', prefix))
        if matches:
            return _normalize_issue_number(matches[-1].group(1))

    # No usable anchor. 3+ digits: use the cleaned name and take the LAST
    # match, which avoids capturing series numbers like "2099" in
    # "Spider-Man 2099 001".
    wide = list(re.finditer(rf'(?<=\s)({_S}\d{{3,}}{_SUFFIX})(?:\s|$)', name_clean))
    narrow = re.search(rf'(?<=\s)({_S}\d{{1,2}}{_SUFFIX})(?:\s|$)', name)

    # Collected-edition naming, "Absolute Flash V01 - Of Two Worlds 2025": the
    # only bare number is a publication year at the very end, and the volume
    # token is the number. Any other candidate ("X-Men v2 012") wins as before.
    if not has_group and not narrow and len(wide) == 1:
        only = wide[0]
        if only.end() == len(name_clean) and re.fullmatch(r'(?:19|20)\d{2}', only.group(1)):
            volume = re.search(
                r'(?<=\s)[Vv](?:ol(?:ume)?\.?\s*)?(\d{1,3})(?=\s|$)',
                name_clean[:only.start()],
            )
            if volume:
                return _normalize_issue_number(volume.group(1))

    if wide:
        return _normalize_issue_number(wide[-1].group(1))
    if narrow:
        return _normalize_issue_number(narrow.group(1))

    # Dash/underscore + digits ("Comic-001"). No sign here: this dash IS the
    # separator.
    match = re.search(rf'[-_](\d+{_SUFFIX})(?:\s|$)', name)
    if match:
        return _normalize_issue_number(match.group(1))

    return None


def _normalize_issue_number(num_str: str) -> str:
    """Strip leading zeros from the integer part while preserving a decimal/
    alpha point-suffix ('.1', '.BEY') or a directly-attached letter suffix
    ('A' in '013A'). e.g. '013A' -> '13A', '080.BEY' -> '80.BEY', '001' -> '1'.
    A leading sign survives, as ASCII: '-001' -> '-1'.
    """
    sign = ''
    if num_str and num_str[0] in SIGN_CHARS:
        sign, num_str = '-', num_str[1:]

    # Peel off an optional dot-suffix first ('80.BEY' -> '80' + '.BEY').
    if '.' in num_str:
        int_part, dot_suffix = num_str.split('.', 1)
        dot_suffix = '.' + dot_suffix
    else:
        int_part, dot_suffix = num_str, ''

    # Then a directly-attached letter suffix ('13A' -> '13' + 'A').
    letter_suffix = ''
    m = re.match(r'(\d+)([A-Za-z]+)$', int_part)
    if m:
        int_part, letter_suffix = m.group(1), m.group(2)

    return sign + str(int(int_part)) + letter_suffix + dot_suffix


class BaseProvider(ABC):
    """
    Abstract base class for all metadata providers.

    All metadata providers must inherit from this class and implement
    the required abstract methods to provide a consistent interface.
    """

    # Class attributes to be overridden by subclasses
    provider_type: ProviderType
    display_name: str
    requires_auth: bool = True
    auth_fields: List[str] = []  # e.g., ["api_key"] or ["username", "password"]

    # Optional alternative ways to authenticate, when a provider accepts more
    # than one. Each entry is {"id", "label", "fields"}, and every field named
    # must also appear in ``auth_fields`` -- the settings page renders a mode
    # picker from this and posts the chosen id back, so the server knows which
    # of the other modes' fields to clear. Empty means "one way only".
    auth_modes: List[Dict[str, Any]] = []

    # Credential fields this provider used to accept and no longer does. They
    # are stripped from every save and probe, so a value left over from before
    # the change can never be written back. See MetronProvider.
    retired_auth_fields: List[str] = []

    # The PyPI distribution of the client library this provider wraps, if any.
    # Its installed version is shown on the settings page, because a library
    # upgrade can break a provider outright (mokkari 5.0 dropped basic auth).
    client_library: Optional[str] = None

    # Verify the credentials against the provider as part of saving them.
    # Only worth it where a rejection has a lasting cost; see MetronProvider.
    validate_on_save: bool = False

    # Set on an instance built from credentials that are *not* saved (the
    # Guided Setup tests before it saves). Its test must leave every piece of
    # process-wide auth state alone: those belong to the saved credentials.
    probe_only: bool = False

    # Advertised rate limit: `rate_limit` requests per `rate_limit_window_seconds`.
    # The window is explicit because providers don't agree on one -- ComicVine
    # publishes an hourly budget, Metron and AniList per-minute ones -- and a bare
    # number under a "per minute" comment was silently wrong for ComicVine.
    #
    # This pair is metadata for the settings UI, and the number shown should be
    # the number enforced. Enforcement lives with each provider's own client and
    # runs per HTTP send (see core/provider_client.py): Metron's session limiter
    # is seeded from MetronProvider.rate_limit; ComicVine is paced by Simyan's
    # own bucket; the plain-requests providers by core.http_client.
    rate_limit: int = 30
    rate_limit_window_seconds: int = 60

    def __init__(self, credentials: Optional[ProviderCredentials] = None):
        """
        Initialize the provider with optional credentials.

        Args:
            credentials: Provider credentials for authentication
        """
        self.credentials = credentials
        self._client = None

    @abstractmethod
    def test_connection(self) -> bool:
        """
        Verify credentials and connectivity to the provider.

        Returns:
            True if connection is successful, False otherwise
        """
        pass

    @abstractmethod
    def search_series(self, query: str, year: Optional[int] = None) -> List[SearchResult]:
        """
        Search for series/volumes matching the query.

        Args:
            query: Search string (series name)
            year: Optional year to filter results

        Returns:
            List of matching SearchResult objects
        """
        pass

    @abstractmethod
    def get_series(self, series_id: str) -> Optional[SearchResult]:
        """
        Get series details by provider-specific ID.

        Args:
            series_id: The provider's series/volume ID

        Returns:
            SearchResult with series details, or None if not found
        """
        pass

    @abstractmethod
    def get_issues(self, series_id: str) -> List[IssueResult]:
        """
        Get all issues for a series.

        Args:
            series_id: The provider's series/volume ID

        Returns:
            List of IssueResult objects for all issues in the series
        """
        pass

    @abstractmethod
    def get_issue(self, issue_id: str) -> Optional[IssueResult]:
        """
        Get issue details by provider-specific ID.

        Args:
            issue_id: The provider's issue ID

        Returns:
            IssueResult with issue details, or None if not found
        """
        pass

    @abstractmethod
    def to_comicinfo(self, issue: IssueResult, series: Optional[SearchResult] = None) -> Dict[str, Any]:
        """
        Convert provider data to ComicInfo.xml field mapping.

        Args:
            issue: The issue data to convert
            series: Optional series data for additional fields

        Returns:
            Dictionary mapping ComicInfo.xml field names to values
        """
        pass

    def get_provider_info(self) -> Dict[str, Any]:
        """Get provider metadata for API responses."""
        return {
            "type": self.provider_type.value,
            "name": self.display_name,
            "requires_auth": self.requires_auth,
            "auth_fields": self.auth_fields,
            "auth_modes": self.auth_modes,
            "rate_limit": self.rate_limit,
            "rate_limit_window_seconds": self.rate_limit_window_seconds
        }
