"""
ComicVine (Local SQLite) Provider Adapter.

Wraps the local ComicVine SQLite database (a user-provided file on a mapped path)
to conform to the BaseProvider interface. Output is identical to the ComicVine API
provider because both funnel through models.comicvine.map_to_comicinfo.
"""
from typing import Optional, List, Dict, Any

from core.app_logging import app_logger
from .base import BaseProvider, ProviderType, ProviderCredentials, SearchResult, IssueResult
from . import register_provider


@register_provider
class ComicVineSqliteProvider(BaseProvider):
    """ComicVine metadata provider backed by a local SQLite database file."""

    provider_type = ProviderType.COMICVINE_SQLITE
    display_name = "ComicVine (Local DB)"
    requires_auth = True
    auth_fields = ["database_path"]
    rate_limit = 1000  # Local database, no API rate limits

    def __init__(self, credentials: Optional[ProviderCredentials] = None):
        super().__init__(credentials)

    def _is_configured(self) -> bool:
        """Check if the ComicVine SQLite database is configured and present."""
        from models import comicvine_sqlite as cv_sqlite
        return cv_sqlite.check_database_status().get('cv_sqlite_available', False)

    def test_connection(self) -> bool:
        """Test the DB — open it and verify the cv_volume/cv_issue tables exist."""
        try:
            if not self._is_configured():
                return False

            from models import comicvine_sqlite as cv_sqlite
            conn = cv_sqlite.get_connection()
            if not conn:
                return False
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name IN ('cv_volume', 'cv_issue')"
                )
                present = {row['name'] for row in cursor.fetchall()}
                return {'cv_volume', 'cv_issue'}.issubset(present)
            finally:
                conn.close()
        except Exception as e:
            app_logger.error(f"ComicVine SQLite connection test failed: {e}")
            return False

    def search_series(self, query: str, year: Optional[int] = None) -> List[SearchResult]:
        """Search for volumes (series) in the local ComicVine database."""
        try:
            if not self._is_configured():
                return []

            from models import comicvine_sqlite as cv_sqlite
            volumes = cv_sqlite.search_volumes(query, year)

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
                    description=vol.get('description'),
                ))
            return results
        except Exception as e:
            app_logger.error(f"ComicVine SQLite search_series failed: {e}")
            return []

    def get_series(self, series_id: str) -> Optional[SearchResult]:
        """Get volume details by ComicVine volume ID."""
        try:
            if not self._is_configured():
                return None

            from models import comicvine_sqlite as cv_sqlite
            details = cv_sqlite.get_volume_details(int(series_id))
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
                description=details.get('description'),
            )
        except Exception as e:
            app_logger.error(f"ComicVine SQLite get_series failed: {e}")
            return None

    def get_issues(self, series_id: str) -> List[IssueResult]:
        """Get all issues for a volume from the local database."""
        try:
            if not self._is_configured():
                return []

            from models import comicvine_sqlite as cv_sqlite
            conn = cv_sqlite.get_connection()
            if not conn:
                return []
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, volume_id, name, issue_number, cover_date, "
                    "       store_date, image_url"
                    " FROM cv_issue WHERE volume_id = ?"
                    " ORDER BY CAST(issue_number AS REAL), issue_number",
                    (int(series_id),),
                )
                rows = cursor.fetchall()
            finally:
                conn.close()

            results = []
            for row in rows:
                results.append(IssueResult(
                    provider=self.provider_type,
                    id=str(row.get('id')),
                    series_id=series_id,
                    issue_number=str(row['issue_number']) if row.get('issue_number') is not None else '',
                    title=row.get('name'),
                    cover_date=str(row['cover_date']) if row.get('cover_date') else None,
                    store_date=str(row['store_date']) if row.get('store_date') else None,
                    cover_url=row.get('image_url'),
                    summary=None,
                ))
            return results
        except Exception as e:
            app_logger.error(f"ComicVine SQLite get_issues failed: {e}")
            return []

    def get_issue(self, issue_id: str) -> Optional[IssueResult]:
        """Get issue details by ComicVine issue ID."""
        try:
            if not self._is_configured():
                return None

            from models import comicvine_sqlite as cv_sqlite
            conn = cv_sqlite.get_connection()
            if not conn:
                return None
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, volume_id, name, issue_number, cover_date, "
                    "       store_date, description, image_url"
                    " FROM cv_issue WHERE id = ? LIMIT 1",
                    (int(issue_id),),
                )
                row = cursor.fetchone()
            finally:
                conn.close()

            if not row:
                return None

            return IssueResult(
                provider=self.provider_type,
                id=str(row.get('id')),
                series_id=str(row.get('volume_id')) if row.get('volume_id') is not None else '',
                issue_number=str(row['issue_number']) if row.get('issue_number') is not None else '',
                title=row.get('name'),
                cover_date=str(row['cover_date']) if row.get('cover_date') else None,
                store_date=str(row['store_date']) if row.get('store_date') else None,
                cover_url=row.get('image_url'),
                summary=row.get('description'),
            )
        except Exception as e:
            app_logger.error(f"ComicVine SQLite get_issue failed: {e}")
            return None

    def get_issue_metadata(self, volume_id: str, issue_number: str, start_year: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Return the ComicInfo-ready metadata dict for an issue in a volume."""
        try:
            if not self._is_configured():
                return None

            from models import comicvine_sqlite as cv_sqlite
            return cv_sqlite.get_issue_metadata(int(volume_id), issue_number, start_year=start_year)
        except Exception as e:
            app_logger.error(f"ComicVine SQLite get_issue_metadata failed: {e}")
            return None

    def to_comicinfo(self, issue: IssueResult, series: Optional[SearchResult] = None) -> Dict[str, Any]:
        """Convert a local ComicVine issue into ComicInfo.xml fields."""
        try:
            if issue.series_id and issue.issue_number:
                from models import comicvine_sqlite as cv_sqlite
                metadata = cv_sqlite.get_issue_metadata(
                    int(issue.series_id),
                    issue.issue_number,
                    start_year=series.year if series else None,
                )
                if metadata:
                    metadata.pop('_image_url', None)
                    return metadata

            # Fallback: build from IssueResult
            comicinfo = {
                'Series': series.title if series else None,
                'Number': issue.issue_number,
                'Title': issue.title,
                'Summary': issue.summary,
                'CoverDate': issue.cover_date,
                'StoreDate': issue.store_date,
                'Notes': f'Metadata from ComicVine (Local DB). Issue ID: {issue.id}',
            }
            if series:
                comicinfo['Publisher'] = series.publisher
                comicinfo['Volume'] = series.year
            if issue.cover_date and len(issue.cover_date) >= 4:
                try:
                    comicinfo['Year'] = int(issue.cover_date[:4])
                except ValueError:
                    pass

            return {k: v for k, v in comicinfo.items() if v is not None}
        except Exception as e:
            app_logger.error(f"ComicVine SQLite to_comicinfo failed: {e}")
            return {}
