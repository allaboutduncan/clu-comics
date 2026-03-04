"""
ComicVine Provider Adapter.

Wraps the existing ComicVine/Simyan implementation to conform to the BaseProvider interface.
"""
from typing import Optional, List, Dict, Any

from app_logging import app_logger
from .base import BaseProvider, ProviderType, ProviderCredentials, SearchResult, IssueResult
from . import register_provider


@register_provider
class ComicVineProvider(BaseProvider):
    """ComicVine metadata provider using the Simyan library."""

    provider_type = ProviderType.COMICVINE
    display_name = "ComicVine"
    requires_auth = True
    auth_fields = ["api_key"]
    rate_limit = 200  # ComicVine allows ~200 requests per resource per hour

    def __init__(self, credentials: Optional[ProviderCredentials] = None):
        super().__init__(credentials)
        self._cv = None

    def _get_client(self):
        """Get or create the Simyan ComicVine client."""
        if self._cv is not None:
            return self._cv

        if not self.credentials or not self.credentials.api_key:
            return None

        try:
            from models import comicvine as cv_module
            if not cv_module.is_simyan_available():
                app_logger.warning("Simyan library not available")
                return None

            from simyan.comicvine import Comicvine
            self._cv = Comicvine(api_key=self.credentials.api_key, cache=None)
            return self._cv
        except Exception as e:
            app_logger.error(f"Failed to initialize ComicVine client: {e}")
            return None

    def _get_api_key(self) -> Optional[str]:
        """Get the API key from credentials."""
        if self.credentials and self.credentials.api_key:
            return self.credentials.api_key
        return None

    def test_connection(self) -> bool:
        """Test connection to ComicVine API."""
        try:
            cv = self._get_client()
            if not cv:
                return False

            # Try to search for a known volume to verify credentials
            from simyan.comicvine import ComicvineResource
            results = cv.search(resource=ComicvineResource.VOLUME, query="Batman")
            return results is not None
        except Exception as e:
            app_logger.error(f"ComicVine connection test failed: {e}")
            return False

    def search_series(self, query: str, year: Optional[int] = None) -> List[SearchResult]:
        """Search for volumes (series) on ComicVine."""
        try:
            api_key = self._get_api_key()
            if not api_key:
                return []

            from models import comicvine as cv_module
            volumes = cv_module.search_volumes(api_key, query, year)

            if not volumes:
                return []

            results = []
            for vol in volumes:
                results.append(SearchResult(
                    provider=self.provider_type,
                    id=str(vol.get('id', '')),
                    title=vol.get('name', ''),
                    year=vol.get('start_year'),
                    publisher=vol.get('publisher_name'),
                    issue_count=vol.get('count_of_issues'),
                    cover_url=vol.get('image_url'),
                    description=vol.get('description')
                ))

            return results
        except Exception as e:
            app_logger.error(f"ComicVine search_series failed: {e}")
            return []

    def get_series(self, series_id: str) -> Optional[SearchResult]:
        """Get volume details by ComicVine volume ID."""
        try:
            api_key = self._get_api_key()
            if not api_key:
                return None

            from models import comicvine as cv_module
            details = cv_module.get_volume_details(api_key, int(series_id))

            if not details:
                return None

            return SearchResult(
                provider=self.provider_type,
                id=str(details.get('id', series_id)),
                title=details.get('name', ''),
                year=details.get('start_year'),
                publisher=details.get('publisher_name'),
                issue_count=details.get('count_of_issues'),
                cover_url=details.get('image_url'),
                description=details.get('description')
            )
        except Exception as e:
            app_logger.error(f"ComicVine get_series failed: {e}")
            return None

    def get_issues(self, series_id: str) -> List[IssueResult]:
        """Get all issues for a ComicVine volume."""
        try:
            cv = self._get_client()
            if not cv:
                return []

            # Get volume issues through API
            issues = cv.list_issues(params={"filter": f"volume:{series_id}"})

            if not issues:
                return []

            results = []
            for issue in issues:
                # Extract cover date components
                cover_date = None
                if hasattr(issue, 'cover_date') and issue.cover_date:
                    cover_date = str(issue.cover_date)

                # Get image URL
                image_url = None
                if hasattr(issue, 'image') and issue.image:
                    if hasattr(issue.image, 'small_url'):
                        image_url = str(issue.image.small_url)
                    elif hasattr(issue.image, 'thumb_url'):
                        image_url = str(issue.image.thumb_url)

                results.append(IssueResult(
                    provider=self.provider_type,
                    id=str(issue.id),
                    series_id=series_id,
                    issue_number=str(issue.issue_number) if issue.issue_number else '',
                    title=issue.name if hasattr(issue, 'name') else None,
                    cover_date=cover_date,
                    store_date=str(issue.store_date) if hasattr(issue, 'store_date') and issue.store_date else None,
                    cover_url=image_url,
                    summary=None  # Full description requires individual issue fetch
                ))

            return results
        except Exception as e:
            app_logger.error(f"ComicVine get_issues failed: {e}")
            return []

    def get_issue(self, issue_id: str) -> Optional[IssueResult]:
        """Get issue details by ComicVine issue ID."""
        try:
            cv = self._get_client()
            if not cv:
                return None

            issue = cv.issue(int(issue_id))
            if not issue:
                return None

            # Get volume/series ID
            series_id = None
            if hasattr(issue, 'volume') and issue.volume:
                series_id = str(issue.volume.id)

            # Get image URL
            image_url = None
            if hasattr(issue, 'image') and issue.image:
                if hasattr(issue.image, 'small_url'):
                    image_url = str(issue.image.small_url)

            # Get cover date
            cover_date = None
            if hasattr(issue, 'cover_date') and issue.cover_date:
                cover_date = str(issue.cover_date)

            return IssueResult(
                provider=self.provider_type,
                id=str(issue.id),
                series_id=series_id or '',
                issue_number=str(issue.issue_number) if issue.issue_number else '',
                title=issue.name if hasattr(issue, 'name') else None,
                cover_date=cover_date,
                store_date=str(issue.store_date) if hasattr(issue, 'store_date') and issue.store_date else None,
                cover_url=image_url,
                summary=issue.description if hasattr(issue, 'description') else None
            )
        except Exception as e:
            app_logger.error(f"ComicVine get_issue failed: {e}")
            return None

    def get_issue_metadata(self, volume_id: str, issue_number: str, start_year: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Get full issue metadata for a specific issue in a volume.

        This is a convenience method that returns the metadata dict
        suitable for conversion to ComicInfo.xml.
        """
        try:
            api_key = self._get_api_key()
            if not api_key:
                return None

            from models import comicvine as cv_module
            return cv_module.get_metadata_by_volume_id(
                api_key, int(volume_id), issue_number, start_year=start_year
            )
        except Exception as e:
            app_logger.error(f"ComicVine get_issue_metadata failed: {e}")
            return None

    def to_comicinfo(self, issue: IssueResult, series: Optional[SearchResult] = None) -> Dict[str, Any]:
        """Convert ComicVine issue data to ComicInfo.xml fields."""
        try:
            api_key = self._get_api_key()
            if api_key and issue.series_id and issue.issue_number:
                # Get full metadata using the existing function
                from models import comicvine as cv_module
                metadata = cv_module.get_metadata_by_volume_id(
                    api_key,
                    int(issue.series_id),
                    issue.issue_number,
                    start_year=series.year if series else None
                )
                if metadata:
                    # Get volume data for mapping
                    volume_data = {'id': issue.series_id}
                    if series:
                        volume_data['name'] = series.title
                        volume_data['start_year'] = series.year
                        volume_data['publisher_name'] = series.publisher
                    return cv_module.map_to_comicinfo(metadata, volume_data, series.year if series else None)

            # Fallback: build from IssueResult
            comicinfo = {
                'Series': series.title if series else None,
                'Number': issue.issue_number,
                'Title': issue.title,
                'Summary': issue.summary,
                'Notes': f'Metadata from ComicVine. Issue ID: {issue.id}',
            }

            if series:
                comicinfo['Publisher'] = series.publisher
                comicinfo['Volume'] = series.year

            # Parse year from cover_date
            if issue.cover_date and len(issue.cover_date) >= 4:
                try:
                    comicinfo['Year'] = int(issue.cover_date[:4])
                except ValueError:
                    pass

            return {k: v for k, v in comicinfo.items() if v is not None}
        except Exception as e:
            app_logger.error(f"ComicVine to_comicinfo failed: {e}")
            return {}
