"""
AniList Provider Adapter.

Uses the AniList GraphQL API for manga/anime metadata.
API Documentation: https://anilist.gitbook.io/anilist-apiv2-docs/
"""
import requests
from typing import Optional, List, Dict, Any

from core.app_logging import app_logger
from .base import BaseProvider, ProviderType, ProviderCredentials, SearchResult, IssueResult
from . import register_provider


@register_provider
class AniListProvider(BaseProvider):
    """AniList metadata provider using GraphQL API.

    Note: AniList's public API works WITHOUT authentication for searching manga.
    OAuth tokens are only needed for updating lists or accessing private data.
    For basic metadata lookups, leave credentials empty.
    """

    provider_type = ProviderType.ANILIST
    display_name = "AniList"
    requires_auth = False  # Public API works without auth
    auth_fields = []  # No credentials needed for public searches
    rate_limit = 90  # AniList allows 90 requests per minute

    GRAPHQL_URL = "https://graphql.anilist.co"

    def __init__(self, credentials: Optional[ProviderCredentials] = None):
        super().__init__(credentials)

    def _make_request(self, query: str, variables: Dict[str, Any] = None) -> Optional[Dict]:
        """Make a GraphQL request to AniList."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # Add authorization if we have an API key/token
        if self.credentials and self.credentials.api_key:
            headers["Authorization"] = f"Bearer {self.credentials.api_key}"

        try:
            response = requests.post(
                self.GRAPHQL_URL,
                json={"query": query, "variables": variables or {}},
                headers=headers,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                app_logger.error(f"AniList GraphQL errors: {data['errors']}")
                return None

            return data.get("data")
        except requests.RequestException as e:
            app_logger.error(f"AniList request failed: {e}")
            return None

    def test_connection(self) -> bool:
        """Test connection to AniList API."""
        try:
            # Simple query to verify API is accessible
            query = """
            query {
                Page(perPage: 1) {
                    media(type: MANGA) {
                        id
                    }
                }
            }
            """
            result = self._make_request(query)
            return result is not None
        except Exception as e:
            app_logger.error(f"AniList connection test failed: {e}")
            return False

    def search_series(self, query: str, year: Optional[int] = None) -> List[SearchResult]:
        """Search for manga series on AniList."""
        try:
            # Build query with optional year filter
            # AniList uses FuzzyDateInt for date filtering (YYYYMMDD format)
            if year:
                graphql_query = """
                query ($search: String, $startDate: FuzzyDateInt, $endDate: FuzzyDateInt) {
                    Page(perPage: 20) {
                        media(search: $search, startDate_greater: $startDate, startDate_lesser: $endDate, type: MANGA, sort: POPULARITY_DESC) {
                            id
                            title {
                                romaji
                                english
                                native
                            }
                            startDate {
                                year
                                month
                            }
                            coverImage {
                                large
                                medium
                            }
                            description(asHtml: false)
                            chapters
                            volumes
                            status
                            genres
                            averageScore
                        }
                    }
                }
                """
                variables = {
                    "search": query,
                    "startDate": year * 10000,  # YYYY0000 format
                    "endDate": (year + 1) * 10000 - 1  # YYYY1231 format
                }
            else:
                graphql_query = """
                query ($search: String) {
                    Page(perPage: 20) {
                        media(search: $search, type: MANGA, sort: POPULARITY_DESC) {
                            id
                            title {
                                romaji
                                english
                                native
                            }
                            startDate {
                                year
                                month
                            }
                            coverImage {
                                large
                                medium
                            }
                            description(asHtml: false)
                            chapters
                            volumes
                            status
                            genres
                            averageScore
                        }
                    }
                }
                """
                variables = {"search": query}

            data = self._make_request(graphql_query, variables)
            if not data:
                return []

            results = []
            media_list = data.get("Page", {}).get("media", [])

            for media in media_list:
                # Get best available title (prefer English, fallback to romaji)
                titles = media.get("title", {})
                title = titles.get("english") or titles.get("romaji") or titles.get("native") or ""

                # Get year from startDate
                start_date = media.get("startDate", {})
                start_year = start_date.get("year")

                # Get cover image
                cover_image = media.get("coverImage", {})
                cover_url = cover_image.get("large") or cover_image.get("medium")

                # Clean description (remove HTML if any slipped through)
                description = media.get("description", "")
                if description:
                    # Basic HTML tag removal
                    import re
                    description = re.sub(r'<[^>]+>', '', description)
                    description = description[:500] + "..." if len(description) > 500 else description

                results.append(SearchResult(
                    provider=self.provider_type,
                    id=str(media.get("id", "")),
                    title=title,
                    year=start_year,
                    publisher=None,  # AniList doesn't provide publisher info
                    issue_count=media.get("chapters") or media.get("volumes"),
                    cover_url=cover_url,
                    description=description
                ))

            return results
        except Exception as e:
            app_logger.error(f"AniList search_series failed: {e}")
            return []

    def get_series(self, series_id: str) -> Optional[SearchResult]:
        """Get manga details by AniList media ID."""
        try:
            query = """
            query ($id: Int) {
                Media(id: $id, type: MANGA) {
                    id
                    title {
                        romaji
                        english
                        native
                    }
                    startDate {
                        year
                        month
                    }
                    coverImage {
                        large
                        medium
                    }
                    description(asHtml: false)
                    chapters
                    volumes
                    status
                    genres
                }
            }
            """

            data = self._make_request(query, {"id": int(series_id)})
            if not data:
                return None

            media = data.get("Media")
            if not media:
                return None

            titles = media.get("title", {})
            title = titles.get("english") or titles.get("romaji") or titles.get("native") or ""

            start_date = media.get("startDate", {})
            cover_image = media.get("coverImage", {})

            description = media.get("description", "")
            if description:
                import re
                description = re.sub(r'<[^>]+>', '', description)

            return SearchResult(
                provider=self.provider_type,
                id=str(media.get("id", "")),
                title=title,
                year=start_date.get("year"),
                publisher=None,
                issue_count=media.get("chapters") or media.get("volumes"),
                cover_url=cover_image.get("large") or cover_image.get("medium"),
                description=description
            )
        except Exception as e:
            app_logger.error(f"AniList get_series failed: {e}")
            return None

    def get_issues(self, series_id: str) -> List[IssueResult]:
        """
        Get chapters/issues for an AniList manga.

        Note: AniList doesn't provide individual chapter data through their API.
        This returns a synthetic list based on the total chapter count.
        """
        try:
            series = self.get_series(series_id)
            if not series:
                return []

            # AniList doesn't have individual chapter endpoints
            # We can only return synthetic chapter entries based on total count
            chapter_count = series.issue_count or 0
            if chapter_count == 0:
                return []

            results = []
            for i in range(1, min(chapter_count + 1, 501)):  # Cap at 500 chapters
                results.append(IssueResult(
                    provider=self.provider_type,
                    id=f"{series_id}-{i}",  # Synthetic ID
                    series_id=series_id,
                    issue_number=str(i),
                    title=None,
                    cover_date=None,
                    store_date=None,
                    cover_url=series.cover_url,  # Use series cover
                    summary=None
                ))

            return results
        except Exception as e:
            app_logger.error(f"AniList get_issues failed: {e}")
            return []

    def get_issue(self, issue_id: str) -> Optional[IssueResult]:
        """
        Get chapter/issue details by ID.

        Note: AniList doesn't have individual chapter data.
        This parses the synthetic ID format "series_id-chapter_number".
        """
        try:
            # Parse synthetic ID
            if "-" not in issue_id:
                return None

            parts = issue_id.rsplit("-", 1)
            if len(parts) != 2:
                return None

            series_id, chapter_num = parts

            series = self.get_series(series_id)
            if not series:
                return None

            return IssueResult(
                provider=self.provider_type,
                id=issue_id,
                series_id=series_id,
                issue_number=chapter_num,
                title=None,
                cover_date=None,
                store_date=None,
                cover_url=series.cover_url,
                summary=None
            )
        except Exception as e:
            app_logger.error(f"AniList get_issue failed: {e}")
            return None

    def get_issue_metadata(self, series_id: str, issue_number: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a specific chapter in a series."""
        try:
            series = self.get_series(series_id)
            if not series:
                return None

            # Prefix with 'v' for volume (manga convention)
            volume_number = f"v{issue_number}" if not issue_number.startswith('v') else issue_number

            return {
                "Series": series.title,
                "Number": volume_number,
                "Volume": series.year,
                "Summary": series.description,
                "Web": f"https://anilist.co/manga/{series_id}",
                "Notes": f"Metadata from AniList. Media ID: {series_id}",
                "Manga": "YesAndRightToLeft"  # AniList is primarily for manga
            }
        except Exception as e:
            app_logger.error(f"AniList get_issue_metadata failed: {e}")
            return None

    def to_comicinfo(self, issue: IssueResult, series: Optional[SearchResult] = None) -> Dict[str, Any]:
        """Convert AniList data to ComicInfo.xml fields."""
        try:
            if not series and issue.series_id:
                series = self.get_series(issue.series_id)

            # Prefix with 'v' for volume (manga convention)
            volume_number = issue.issue_number
            if volume_number and not volume_number.startswith('v'):
                volume_number = f"v{volume_number}"

            comicinfo = {
                "Number": volume_number,
                "Notes": f"Metadata from AniList. Media ID: {issue.series_id}",
                "Web": f"https://anilist.co/manga/{issue.series_id}",
                "Manga": "YesAndRightToLeft"
            }

            if series:
                comicinfo["Series"] = series.title
                comicinfo["Volume"] = series.year
                comicinfo["Summary"] = series.description

                if series.year:
                    comicinfo["Year"] = series.year

            return {k: v for k, v in comicinfo.items() if v is not None}
        except Exception as e:
            app_logger.error(f"AniList to_comicinfo failed: {e}")
            return {}
