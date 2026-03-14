"""
MangaDex Provider Adapter.

Uses the MangaDex public REST API V5 for manga metadata.
API Documentation: https://api.mangadex.org/docs/
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
class MangaDexProvider(BaseProvider):
    """MangaDex metadata provider using the public REST API.

    MangaDex API is public and does not require authentication for basic searches.
    Uses direct REST API calls with rate limiting.
    """

    provider_type = ProviderType.MANGADEX
    display_name = "MangaDex"
    requires_auth = False
    auth_fields = []
    rate_limit = 60

    API_BASE = "https://api.mangadex.org"

    # Class-level rate limiting
    _last_request_time = 0.0

    def __init__(self, credentials: Optional[ProviderCredentials] = None):
        super().__init__(credentials)

    def _make_request(self, method: str, endpoint: str, params: Dict = None) -> Optional[Dict]:
        """Make an HTTP request to the MangaDex API with rate limiting."""
        # Rate limiting: minimum 0.25s between requests (MangaDex allows 5 req/s)
        now = time.monotonic()
        elapsed = now - MangaDexProvider._last_request_time
        if elapsed < 0.25:
            time.sleep(0.25 - elapsed)
        MangaDexProvider._last_request_time = time.monotonic()

        headers = {
            "User-Agent": "ComicUtils/1.0 (comic-utils metadata provider)",
        }

        url = f"{self.API_BASE}{endpoint}"

        try:
            response = requests.request(
                method,
                url,
                params=params,
                headers=headers,
                timeout=30
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            app_logger.error(f"MangaDex request failed: {e}")
            return None

    def _get_localized_value(self, attr_dict: Dict, preferred_lang: str = 'en') -> Optional[str]:
        """
        Extract value from localized attribute dictionary.
        MangaDex stores titles/descriptions as {'en': 'Title', 'ja': 'タイトル', ...}
        """
        if not attr_dict:
            return None
        if isinstance(attr_dict, str):
            return attr_dict
        # Prefer English, fallback to first available
        if preferred_lang in attr_dict:
            return attr_dict[preferred_lang]
        # Try common fallbacks
        for lang in ['en', 'en-us', 'ja-ro', 'ja']:
            if lang in attr_dict:
                return attr_dict[lang]
        # Return first available
        if attr_dict:
            return next(iter(attr_dict.values()), None)
        return None

    def _get_cover_url(self, manga_id: str, cover_filename: str) -> str:
        """Construct cover image URL from manga ID and cover filename."""
        if not cover_filename:
            return None
        return f"https://uploads.mangadex.org/covers/{manga_id}/{cover_filename}"

    def _extract_relationships(self, relationships: List[Dict]) -> Dict[str, Any]:
        """Extract author/artist names and cover filename from relationships array."""
        result = {"authors": [], "artists": [], "cover_filename": None}
        if not relationships:
            return result

        for rel in relationships:
            rel_type = rel.get("type")
            attrs = rel.get("attributes", {})
            if rel_type == "author" and attrs:
                name = attrs.get("name")
                if name:
                    result["authors"].append(name)
            elif rel_type == "artist" and attrs:
                name = attrs.get("name")
                if name:
                    result["artists"].append(name)
            elif rel_type == "cover_art" and attrs:
                filename = attrs.get("fileName")
                if filename:
                    result["cover_filename"] = filename

        return result

    def test_connection(self) -> bool:
        """Test connection by fetching a single manga."""
        try:
            result = self._make_request("GET", "/manga", {"limit": 1})
            if result and result.get("result") == "ok":
                return True
            return False
        except Exception as e:
            app_logger.error(f"MangaDex connection test failed: {e}")
            return False

    def search_series(self, query: str, year: Optional[int] = None) -> List[SearchResult]:
        """Search for manga on MangaDex."""
        try:
            params = {
                "title": query,
                "includes[]": ["author", "artist", "cover_art"],
                "limit": 20,
            }

            data = self._make_request("GET", "/manga", params)
            if not data:
                return []

            results = []
            for manga in data.get("data", []):
                try:
                    manga_id = manga.get("id")
                    if not manga_id:
                        continue

                    attrs = manga.get("attributes", {})

                    # Get title (localized)
                    title = self._get_localized_value(attrs.get("title", {}))

                    # Get year
                    manga_year = attrs.get("year")

                    # Filter by year if specified
                    if year and manga_year and manga_year != year:
                        continue

                    # Get description
                    description = self._get_localized_value(attrs.get("description", {}))
                    if description and len(description) > 500:
                        description = description[:500] + "..."

                    # Extract alternate title from altTitles (native language title)
                    alternate_title = None
                    alt_titles_list = attrs.get("altTitles", [])
                    original_lang = attrs.get("originalLanguage")
                    if original_lang and alt_titles_list:
                        for alt_entry in alt_titles_list:
                            if isinstance(alt_entry, dict) and original_lang in alt_entry:
                                native_title = alt_entry[original_lang]
                                if native_title and native_title != title:
                                    alternate_title = native_title
                                    break

                    # Get cover URL from relationships
                    rels = self._extract_relationships(manga.get("relationships", []))
                    cover_url = None
                    if rels["cover_filename"]:
                        cover_url = self._get_cover_url(manga_id, rels["cover_filename"])

                    # Get volume count from lastVolume
                    issue_count = None
                    last_volume = attrs.get("lastVolume")
                    if last_volume:
                        try:
                            issue_count = int(last_volume)
                        except (ValueError, TypeError):
                            pass

                    results.append(SearchResult(
                        provider=self.provider_type,
                        id=manga_id,
                        title=title or "Unknown Title",
                        year=manga_year,
                        publisher=None,
                        issue_count=issue_count,
                        cover_url=cover_url,
                        description=description,
                        alternate_title=alternate_title
                    ))
                except Exception as e:
                    app_logger.warning(f"Error parsing manga result: {e}")
                    continue

            return results
        except Exception as e:
            app_logger.error(f"MangaDex search failed: {e}")
            return []

    def get_series(self, series_id: str) -> Optional[SearchResult]:
        """Get manga details by MangaDex ID."""
        try:
            params = {"includes[]": ["author", "artist", "cover_art"]}
            data = self._make_request("GET", f"/manga/{series_id}", params)
            if not data:
                return None

            manga = data.get("data", {})
            attrs = manga.get("attributes", {})

            title = self._get_localized_value(attrs.get("title", {}))
            manga_year = attrs.get("year")

            description = self._get_localized_value(attrs.get("description", {}))

            # Extract alternate title from altTitles
            alternate_title = None
            alt_titles_list = attrs.get("altTitles", [])
            original_lang = attrs.get("originalLanguage")
            if original_lang and alt_titles_list:
                for alt_entry in alt_titles_list:
                    if isinstance(alt_entry, dict) and original_lang in alt_entry:
                        native_title = alt_entry[original_lang]
                        if native_title and native_title != title:
                            alternate_title = native_title
                            break

            # Get cover URL and relationships
            rels = self._extract_relationships(manga.get("relationships", []))
            cover_url = None
            if rels["cover_filename"]:
                cover_url = self._get_cover_url(series_id, rels["cover_filename"])

            # Get volume count from lastVolume
            issue_count = None
            last_volume = attrs.get("lastVolume")
            if last_volume:
                try:
                    issue_count = int(last_volume)
                except (ValueError, TypeError):
                    pass

            return SearchResult(
                provider=self.provider_type,
                id=series_id,
                title=title or "Unknown Title",
                year=manga_year,
                publisher=None,
                issue_count=issue_count,
                cover_url=cover_url,
                description=description,
                alternate_title=alternate_title
            )
        except Exception as e:
            app_logger.error(f"MangaDex get_series failed: {e}")
            return None

    def get_issues(self, series_id: str) -> List[IssueResult]:
        """Get volumes for a manga using the aggregate endpoint."""
        try:
            params = {"translatedLanguage[]": "en"}
            data = self._make_request("GET", f"/manga/{series_id}/aggregate", params)
            if not data:
                return []

            volumes = data.get("volumes", {})
            if not volumes:
                return []

            results = []
            for vol_key, vol_data in volumes.items():
                # Skip "none" volume key
                if vol_key == "none":
                    continue

                results.append(IssueResult(
                    provider=self.provider_type,
                    id=f"{series_id}-v{vol_key}",
                    series_id=series_id,
                    issue_number=str(vol_key),
                    title=None,
                    cover_date=None,
                    store_date=None,
                    cover_url=None,
                    summary=None
                ))

            # Sort by volume number numerically
            try:
                results.sort(key=lambda x: float(x.issue_number) if x.issue_number else 0)
            except (ValueError, TypeError):
                pass

            return results
        except Exception as e:
            app_logger.error(f"MangaDex get_issues failed: {e}")
            return []

    def get_issue(self, issue_id: str) -> Optional[IssueResult]:
        """Get volume details by synthetic ID.

        Parses the synthetic ID format "series_id-v{vol_num}".
        """
        try:
            if "-v" not in issue_id:
                return None

            parts = issue_id.rsplit("-v", 1)
            if len(parts) != 2:
                return None

            series_id, vol_num = parts

            return IssueResult(
                provider=self.provider_type,
                id=issue_id,
                series_id=series_id,
                issue_number=vol_num,
                title=None,
                cover_date=None,
                store_date=None,
                cover_url=None,
                summary=None
            )
        except Exception as e:
            app_logger.error(f"MangaDex get_issue failed: {e}")
            return None

    def get_issue_metadata(self, series_id: str, issue_number: str,
                           preferred_title: str = None,
                           alternate_title: str = None) -> Optional[Dict[str, Any]]:
        """Get metadata for a specific volume in a series."""
        try:
            # Fetch manga details
            params = {"includes[]": ["author", "artist", "cover_art"]}
            detail = self._make_request("GET", f"/manga/{series_id}", params)
            if not detail:
                return None

            manga = detail.get("data", {})
            attrs = manga.get("attributes", {})

            title = self._get_localized_value(attrs.get("title", {}))

            # Use preferred_title if provided
            series_name = preferred_title if preferred_title else (title or "Unknown Title")

            # Prefix with 'v' for volume (manga convention)
            volume_number = f"v{issue_number}" if not issue_number.startswith('v') else issue_number

            # Build metadata
            metadata = {
                "Series": series_name,
                "Number": volume_number,
                "Web": f"https://mangadex.org/title/{series_id}",
            }

            # Year
            series_year = attrs.get("year")
            if series_year:
                metadata["Year"] = series_year

            # Summary
            description = self._get_localized_value(attrs.get("description", {}))
            if description:
                metadata["Summary"] = description

            # Author/Artist from relationships
            rels = self._extract_relationships(manga.get("relationships", []))
            if rels["authors"]:
                metadata["Writer"] = ", ".join(rels["authors"])
            if rels["artists"]:
                metadata["Penciller"] = ", ".join(rels["artists"])

            # Genres from tags
            tags = attrs.get("tags", [])
            if tags:
                genre_names = []
                for tag in tags:
                    tag_attrs = tag.get("attributes", {})
                    if tag_attrs.get("group") == "genre":
                        tag_name = self._get_localized_value(tag_attrs.get("name", {}))
                        if tag_name:
                            genre_names.append(tag_name)
                if genre_names:
                    metadata["Genre"] = ", ".join(genre_names)

            # Alternate series: collect alt titles, deduplicating
            alt_titles = []
            seen = set()

            # Add alternate_title param (native title when preferred was used)
            native_for_alt = alternate_title if alternate_title else (title if title != series_name else None)
            if native_for_alt:
                alt_titles.append(native_for_alt)
                seen.add(native_for_alt.lower())

            # Add altTitles from API
            alt_titles_list = attrs.get("altTitles", [])
            for alt_entry in alt_titles_list:
                if isinstance(alt_entry, dict):
                    for lang, val in alt_entry.items():
                        if val and val.lower() not in seen:
                            alt_titles.append(val)
                            seen.add(val.lower())

            if alt_titles:
                metadata["AlternateSeries"] = "; ".join(alt_titles)

            # Manga type detection based on originalLanguage
            original_lang = attrs.get("originalLanguage")
            if original_lang in ("ja", "ko", "zh"):
                metadata["Manga"] = "Yes"

            # Volume count from lastVolume
            last_volume = attrs.get("lastVolume")
            if last_volume:
                try:
                    metadata["Count"] = int(last_volume)
                except (ValueError, TypeError):
                    pass

            # Status in Notes
            status = attrs.get("status", "")
            if status:
                metadata["Notes"] = f"Status: {status}. Metadata from MangaDex."
            else:
                metadata["Notes"] = "Metadata from MangaDex."

            return metadata
        except Exception as e:
            app_logger.error(f"MangaDex get_issue_metadata failed: {e}")
            return None

    def to_comicinfo(self, issue: IssueResult, series: Optional[SearchResult] = None) -> Dict[str, Any]:
        """Convert MangaDex data to ComicInfo.xml fields."""
        try:
            kwargs = {}
            if series:
                kwargs['preferred_title'] = series.title
                kwargs['alternate_title'] = getattr(series, 'alternate_title', None)
            return self.get_issue_metadata(issue.series_id, issue.issue_number, **kwargs) or {}
        except Exception as e:
            app_logger.error(f"MangaDex to_comicinfo failed: {e}")
            return {}
