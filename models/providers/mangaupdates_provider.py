"""
MangaUpdates Provider Adapter.

Uses the MangaUpdates public REST API for manga metadata.
API Documentation: https://api.mangaupdates.com
"""
import html
import re
import time
import requests
from typing import Optional, List, Dict, Any

from core.app_logging import app_logger
from .base import BaseProvider, ProviderType, ProviderCredentials, SearchResult, IssueResult
from . import register_provider


@register_provider
class MangaUpdatesProvider(BaseProvider):
    """MangaUpdates metadata provider using the public REST API.

    The MangaUpdates API is public and does not require authentication.
    It is series-based (no individual chapter/volume endpoints), so
    volumes are synthesized from the series volume count.
    """

    provider_type = ProviderType.MANGAUPDATES
    display_name = "MangaUpdates"
    requires_auth = False
    auth_fields = []
    rate_limit = 30

    API_BASE = "https://api.mangaupdates.com/v1"

    # Class-level rate limiting
    _last_request_time = 0.0

    def __init__(self, credentials: Optional[ProviderCredentials] = None):
        super().__init__(credentials)

    def _make_request(self, method: str, endpoint: str, json_data: Dict = None) -> Optional[Dict]:
        """Make an HTTP request to the MangaUpdates API with rate limiting."""
        # Rate limiting: minimum 1.5s between requests
        now = time.monotonic()
        elapsed = now - MangaUpdatesProvider._last_request_time
        if elapsed < 1.5:
            time.sleep(1.5 - elapsed)
        MangaUpdatesProvider._last_request_time = time.monotonic()

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ComicUtils/1.0 (comic-utils metadata provider)",
        }

        url = f"{self.API_BASE}{endpoint}"

        try:
            response = requests.request(
                method,
                url,
                json=json_data,
                headers=headers,
                timeout=30
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            app_logger.error(f"MangaUpdates request failed: {e}")
            return None

    def test_connection(self) -> bool:
        """Test connection to MangaUpdates API."""
        try:
            result = self._make_request("POST", "/series/search", {"search": "test", "perpage": 1})
            return result is not None
        except Exception as e:
            app_logger.error(f"MangaUpdates connection test failed: {e}")
            return False

    def search_series(self, query: str, year: Optional[int] = None) -> List[SearchResult]:
        """Search for manga series on MangaUpdates.

        Note: The year parameter is accepted for interface compatibility but
        is NOT used for filtering. MangaUpdates 'year' is the series start
        year, which rarely matches the volume publication year from filenames.
        The API's own relevance ranking is used instead.
        """
        try:
            data = self._make_request("POST", "/series/search", {"search": query, "perpage": 20})
            if not data:
                return []

            results = []
            for item in data.get("results", []):
                record = item.get("record", {})
                series_id = str(record.get("series_id", ""))
                if not series_id:
                    continue

                title = record.get("title", "Unknown Title")
                # Clean HTML from title
                title = re.sub(r'<[^>]+>', '', title)
                title = html.unescape(title)

                # Prefer hit_title (the matched English title) over native title
                hit_title = item.get("hit_title", "")
                alternate_title = None
                if hit_title:
                    hit_title = re.sub(r'<[^>]+>', '', hit_title)
                    hit_title = html.unescape(hit_title)
                    if hit_title != title:
                        alternate_title = title
                        title = hit_title

                series_year = record.get("year")
                if isinstance(series_year, str):
                    try:
                        series_year = int(series_year)
                    except (ValueError, TypeError):
                        series_year = None

                description = record.get("description", "")
                if description:
                    description = re.sub(r'<[^>]+>', '', description)
                    description = html.unescape(description)
                    if len(description) > 500:
                        description = description[:500] + "..."

                cover_url = None
                image = record.get("image", {})
                if isinstance(image, dict):
                    cover_url = image.get("url", {}).get("original")

                results.append(SearchResult(
                    provider=self.provider_type,
                    id=series_id,
                    title=title,
                    year=series_year,
                    publisher=None,
                    issue_count=None,
                    cover_url=cover_url,
                    description=description,
                    alternate_title=alternate_title
                ))

            return results
        except Exception as e:
            app_logger.error(f"MangaUpdates search_series failed: {e}")
            return []

    def get_series(self, series_id: str) -> Optional[SearchResult]:
        """Get manga details by MangaUpdates series ID."""
        try:
            data = self._make_request("GET", f"/series/{series_id}")
            if not data:
                return None

            title = data.get("title", "Unknown Title")
            title = re.sub(r'<[^>]+>', '', title)
            title = html.unescape(title)

            series_year = data.get("year")
            if isinstance(series_year, str):
                try:
                    series_year = int(series_year)
                except (ValueError, TypeError):
                    series_year = None

            description = data.get("description", "")
            if description:
                description = re.sub(r'<[^>]+>', '', description)
                description = html.unescape(description)

            cover_url = None
            image = data.get("image", {})
            if isinstance(image, dict):
                cover_url = image.get("url", {}).get("original")

            # Extract publisher from publishers array
            publisher = None
            publishers = data.get("publishers", [])
            if publishers:
                for pub in publishers:
                    pub_name = pub.get("publisher_name")
                    if pub_name:
                        publisher = pub_name
                        break

            # Get volume count
            issue_count = None
            status = data.get("status", "")
            # Try latest_chapter for volume count
            latest_chapter = data.get("latest_chapter")
            if latest_chapter:
                try:
                    issue_count = int(latest_chapter)
                except (ValueError, TypeError):
                    pass

            return SearchResult(
                provider=self.provider_type,
                id=series_id,
                title=title,
                year=series_year,
                publisher=publisher,
                issue_count=issue_count,
                cover_url=cover_url,
                description=description
            )
        except Exception as e:
            app_logger.error(f"MangaUpdates get_series failed: {e}")
            return None

    def get_issues(self, series_id: str) -> List[IssueResult]:
        """
        Get synthetic volumes for a MangaUpdates manga.

        MangaUpdates doesn't have individual volume/chapter endpoints.
        Returns synthetic volume entries based on the series data.
        """
        try:
            series = self.get_series(series_id)
            if not series:
                return []

            volume_count = series.issue_count or 0
            if volume_count == 0:
                return []

            results = []
            for i in range(1, min(volume_count + 1, 501)):  # Cap at 500
                results.append(IssueResult(
                    provider=self.provider_type,
                    id=f"{series_id}-{i}",
                    series_id=series_id,
                    issue_number=str(i),
                    title=None,
                    cover_date=None,
                    store_date=None,
                    cover_url=series.cover_url,
                    summary=None
                ))

            return results
        except Exception as e:
            app_logger.error(f"MangaUpdates get_issues failed: {e}")
            return []

    def get_issue(self, issue_id: str) -> Optional[IssueResult]:
        """
        Get volume details by synthetic ID.

        Parses the synthetic ID format "series_id-volume_number".
        """
        try:
            if "-" not in issue_id:
                return None

            parts = issue_id.rsplit("-", 1)
            if len(parts) != 2:
                return None

            series_id, vol_num = parts

            series = self.get_series(series_id)
            if not series:
                return None

            return IssueResult(
                provider=self.provider_type,
                id=issue_id,
                series_id=series_id,
                issue_number=vol_num,
                title=None,
                cover_date=None,
                store_date=None,
                cover_url=series.cover_url,
                summary=None
            )
        except Exception as e:
            app_logger.error(f"MangaUpdates get_issue failed: {e}")
            return None

    def get_issue_metadata(self, series_id: str, issue_number: str, preferred_title: str = None, alternate_title: str = None) -> Optional[Dict[str, Any]]:
        """Get metadata for a specific volume in a series.

        Follows the ComicTagger approach:
        1. Fetch series info from the API for title, authors, genres, etc.
        2. Inject the volume number from the filename (passed as issue_number).
        3. The API is series-based — there are no per-volume endpoints.
        """
        try:
            # Single API call for all series data
            detail = self._make_request("GET", f"/series/{series_id}")
            if not detail:
                return None

            title = detail.get("title", "Unknown Title")
            title = re.sub(r'<[^>]+>', '', title)
            title = html.unescape(title)

            # Prefix with 'v' for volume (manga convention)
            volume_number = f"v{issue_number}" if not issue_number.startswith('v') else issue_number

            # Parse year
            series_year = detail.get("year")
            if isinstance(series_year, str):
                try:
                    series_year = int(series_year)
                except (ValueError, TypeError):
                    series_year = None

            # Clean description
            description = detail.get("description", "")
            if description:
                description = re.sub(r'<[^>]+>', '', description)
                description = html.unescape(description)

            # Extract publisher from publishers array
            publisher = None
            publishers = detail.get("publishers", [])
            if publishers:
                for pub in publishers:
                    pub_name = pub.get("publisher_name")
                    if pub_name:
                        publisher = pub_name
                        break

            # Use preferred_title (e.g. hit_title from search) if provided
            series_name = preferred_title if preferred_title else title

            # Build metadata
            metadata = {
                "Series": series_name,
                "Number": volume_number,
                "Web": f"https://www.mangaupdates.com/series/{series_id}",
            }

            # Year
            if series_year:
                metadata["Year"] = series_year

            # Summary (cleaned)
            if description:
                metadata["Summary"] = description

            # Publisher
            if publisher:
                metadata["Publisher"] = publisher

            # Authors -> both Writer and Penciller (mangaka convention)
            authors = detail.get("authors", [])
            if authors:
                author_names = []
                for author in authors:
                    name = author.get("name")
                    if name:
                        name = re.sub(r'<[^>]+>', '', name)
                        name = html.unescape(name)
                        author_names.append(name)
                if author_names:
                    author_str = ", ".join(author_names)
                    metadata["Writer"] = author_str
                    metadata["Penciller"] = author_str

            # Genres
            genres = detail.get("genres", [])
            if genres:
                genre_names = []
                for genre in genres:
                    gname = genre.get("genre") if isinstance(genre, dict) else str(genre)
                    if gname:
                        genre_names.append(gname)
                if genre_names:
                    metadata["Genre"] = ", ".join(genre_names)

            # Alternate series: start with the native title if we used a preferred title,
            # then append associated titles from the API, deduplicating
            alt_titles = []
            seen = set()

            # Add alternate_title (native title when hit_title was preferred)
            native_for_alt = alternate_title if alternate_title else (title if title != series_name else None)
            if native_for_alt:
                alt_titles.append(native_for_alt)
                seen.add(native_for_alt.lower())

            associated = detail.get("associated", [])
            if associated:
                for assoc in associated:
                    at = assoc.get("title") if isinstance(assoc, dict) else str(assoc)
                    if at:
                        at = re.sub(r'<[^>]+>', '', at)
                        at = html.unescape(at)
                        if at.lower() not in seen:
                            alt_titles.append(at)
                            seen.add(at.lower())
            if alt_titles:
                metadata["AlternateSeries"] = "; ".join(alt_titles)

            # Manga type detection
            series_type = detail.get("type", "")
            if isinstance(series_type, str) and series_type.lower() in ("manga", "manhwa", "manhua"):
                metadata["Manga"] = "Yes"

            # Volume count from latest_chapter
            latest_chapter = detail.get("latest_chapter")
            if latest_chapter:
                try:
                    metadata["Count"] = int(latest_chapter)
                except (ValueError, TypeError):
                    pass

            # Status in Notes
            status = detail.get("status", "")
            if status:
                metadata["Notes"] = f"Status: {status}. Metadata from MangaUpdates."
            else:
                metadata["Notes"] = "Metadata from MangaUpdates."

            return metadata
        except Exception as e:
            app_logger.error(f"MangaUpdates get_issue_metadata failed: {e}")
            return None

    def to_comicinfo(self, issue: IssueResult, series: Optional[SearchResult] = None) -> Dict[str, Any]:
        """Convert MangaUpdates data to ComicInfo.xml fields."""
        try:
            kwargs = {}
            if series:
                kwargs['preferred_title'] = series.title
                kwargs['alternate_title'] = getattr(series, 'alternate_title', None)
            return self.get_issue_metadata(issue.series_id, issue.issue_number, **kwargs) or {}
        except Exception as e:
            app_logger.error(f"MangaUpdates to_comicinfo failed: {e}")
            return {}
