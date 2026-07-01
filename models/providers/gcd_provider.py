"""
GCD (Grand Comics Database) Provider Adapter.

Wraps the GCD SQLite implementation (a user-provided database file downloaded
from comics.org) to conform to the BaseProvider interface.
"""
from typing import Optional, List, Dict, Any

from core.app_logging import app_logger
from .base import BaseProvider, ProviderType, ProviderCredentials, SearchResult, IssueResult
from . import register_provider


@register_provider
class GCDProvider(BaseProvider):
    """GCD metadata provider using a local SQLite database file."""

    provider_type = ProviderType.GCD
    display_name = "Grand Comics Database"
    requires_auth = True
    auth_fields = ["database_path"]
    rate_limit = 1000  # Local database, high rate limit

    def __init__(self, credentials: Optional[ProviderCredentials] = None):
        super().__init__(credentials)

    def _is_configured(self) -> bool:
        """Check if the GCD SQLite database is configured and present."""
        from models import gcd as gcd_module
        status = gcd_module.check_database_status()
        return status.get('gcd_available', False)

    def test_connection(self) -> bool:
        """Test the GCD SQLite database — open it and verify the core tables."""
        try:
            if not self._is_configured():
                return False

            from models import gcd as gcd_module
            conn = gcd_module.get_connection()
            if not conn:
                return False
            try:
                available = gcd_module.get_available_gcd_tables(conn=conn)
                return gcd_module.GCD_CORE_TABLES.issubset(available)
            finally:
                conn.close()
        except Exception as e:
            app_logger.error(f"GCD connection test failed: {e}")
            return False

    def search_series(self, query: str, year: Optional[int] = None) -> List[SearchResult]:
        """Search for series in GCD database."""
        try:
            if not self._is_configured():
                return []

            from models import gcd as gcd_module
            result = gcd_module.search_series(query, year)

            if not result:
                return []

            # GCD search_series returns a single best match dict
            return [SearchResult(
                provider=self.provider_type,
                id=str(result.get('id', '')),
                title=result.get('name', ''),
                year=result.get('year_began'),
                publisher=result.get('publisher_name'),
                issue_count=result.get('issue_count'),
                cover_url=None,  # GCD doesn't provide cover images
                description=None
            )]
        except Exception as e:
            app_logger.error(f"GCD search_series failed: {e}")
            return []

    def get_series(self, series_id: str) -> Optional[SearchResult]:
        """Get series details by GCD series ID."""
        try:
            if not self._is_configured():
                return None

            from models import gcd as gcd_module
            conn = gcd_module.get_connection()
            if not conn:
                return None

            try:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT
                        s.id,
                        s.name,
                        s.year_began,
                        s.year_ended,
                        p.name AS publisher_name,
                        (SELECT COUNT(*) FROM gcd_issue i WHERE i.series_id = s.id) AS issue_count
                    FROM gcd_series s
                    LEFT JOIN gcd_publisher p ON s.publisher_id = p.id
                    WHERE s.id = ?
                ''', (int(series_id),))

                row = cursor.fetchone()
                cursor.close()

                if not row:
                    return None

                return SearchResult(
                    provider=self.provider_type,
                    id=str(row['id']),
                    title=row['name'],
                    year=row['year_began'],
                    publisher=row['publisher_name'],
                    issue_count=row['issue_count'],
                    cover_url=None,
                    description=None
                )
            finally:
                conn.close()
        except Exception as e:
            app_logger.error(f"GCD get_series failed: {e}")
            return None

    def get_issues(self, series_id: str) -> List[IssueResult]:
        """Get all issues for a GCD series."""
        try:
            if not self._is_configured():
                return []

            from models import gcd as gcd_module
            conn = gcd_module.get_connection()
            if not conn:
                return []

            try:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT
                        i.id,
                        i.number,
                        i.title,
                        i.key_date,
                        i.on_sale_date
                    FROM gcd_issue i
                    WHERE i.series_id = ? AND i.deleted = 0
                    ORDER BY
                        CASE
                            WHEN i.number REGEXP '^[0-9]+$' THEN printf('%010d', CAST(i.number AS INTEGER))
                            ELSE i.number
                        END
                ''', (int(series_id),))

                rows = cursor.fetchall()
                cursor.close()

                results = []
                for row in rows:
                    # Parse key_date for cover_date (format: YYYY-MM-DD or YYYY-MM or YYYY)
                    cover_date = row.get('key_date')
                    if cover_date:
                        cover_date = str(cover_date)

                    results.append(IssueResult(
                        provider=self.provider_type,
                        id=str(row['id']),
                        series_id=series_id,
                        issue_number=str(row['number']) if row['number'] else '',
                        title=row.get('title'),
                        cover_date=cover_date,
                        store_date=str(row['on_sale_date']) if row.get('on_sale_date') else None,
                        cover_url=None,  # GCD doesn't provide covers
                        summary=None
                    ))

                return results
            finally:
                conn.close()
        except Exception as e:
            app_logger.error(f"GCD get_issues failed: {e}")
            return []

    def get_issue(self, issue_id: str) -> Optional[IssueResult]:
        """Get issue details by GCD issue ID."""
        try:
            if not self._is_configured():
                return None

            from models import gcd as gcd_module
            conn = gcd_module.get_connection()
            if not conn:
                return None

            try:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT
                        i.id,
                        i.series_id,
                        i.number,
                        i.title,
                        i.key_date,
                        i.on_sale_date
                    FROM gcd_issue i
                    WHERE i.id = ? AND i.deleted = 0
                ''', (int(issue_id),))

                row = cursor.fetchone()
                cursor.close()

                if not row:
                    return None

                cover_date = row.get('key_date')
                if cover_date:
                    cover_date = str(cover_date)

                return IssueResult(
                    provider=self.provider_type,
                    id=str(row['id']),
                    series_id=str(row['series_id']),
                    issue_number=str(row['number']) if row['number'] else '',
                    title=row.get('title'),
                    cover_date=cover_date,
                    store_date=str(row['on_sale_date']) if row.get('on_sale_date') else None,
                    cover_url=None,
                    summary=None
                )
            finally:
                conn.close()
        except Exception as e:
            app_logger.error(f"GCD get_issue failed: {e}")
            return None

    def get_issue_metadata(self, series_id: str, issue_number: str) -> Optional[Dict[str, Any]]:
        """
        Get full issue metadata for a specific issue in a series.

        This uses the existing GCD get_issue_metadata function which
        returns detailed metadata including credits.
        """
        try:
            if not self._is_configured():
                return None

            from models import gcd as gcd_module
            return gcd_module.get_issue_metadata(int(series_id), issue_number)
        except Exception as e:
            app_logger.error(f"GCD get_issue_metadata failed: {e}")
            return None

    def to_comicinfo(self, issue: IssueResult, series: Optional[SearchResult] = None) -> Dict[str, Any]:
        """Convert GCD issue data to ComicInfo.xml fields."""
        try:
            # Try to get full metadata using existing function
            if issue.series_id and issue.issue_number:
                from models import gcd as gcd_module
                metadata = gcd_module.get_issue_metadata(int(issue.series_id), issue.issue_number)
                if metadata:
                    # GCD get_issue_metadata already returns ComicInfo-compatible dict
                    return metadata

            # Fallback: build from IssueResult
            comicinfo = {
                'Series': series.title if series else None,
                'Number': issue.issue_number,
                'Title': issue.title,
                'Notes': f'Metadata from Grand Comics Database. Issue ID: {issue.id}',
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
            app_logger.error(f"GCD to_comicinfo failed: {e}")
            return {}
