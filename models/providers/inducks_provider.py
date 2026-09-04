"""
INDUCKS Provider Adapter.

Wraps the INDUCKS SQLite implementation (a user-provided database file built
from the dumps at https://inducks.org/) to conform to the BaseProvider
interface. INDUCKS is the reference index of Disney comics worldwide, which no
other provider covers usefully: a Disney issue is an anthology of a dozen
unrelated stories, and the general-purpose databases model it as one book.
"""
from typing import Optional, List, Dict, Any

from core.app_logging import app_logger
from .base import BaseProvider, ProviderType, ProviderCredentials, SearchResult, IssueResult
from . import register_provider


@register_provider
class InducksProvider(BaseProvider):
    """INDUCKS metadata provider using a local SQLite database file."""

    provider_type = ProviderType.INDUCKS
    display_name = "INDUCKS (Disney)"
    requires_auth = True
    auth_fields = ["database_path"]
    rate_limit = 1000  # Local database, high rate limit

    def __init__(self, credentials: Optional[ProviderCredentials] = None):
        super().__init__(credentials)

    def _is_configured(self) -> bool:
        """Check if the INDUCKS SQLite database is configured and present."""
        from models import inducks as inducks_module
        status = inducks_module.check_database_status()
        return status.get('inducks_available', False)

    def test_connection(self) -> bool:
        """Test the INDUCKS SQLite database — open it and verify the core tables."""
        try:
            if not self._is_configured():
                return False

            from models import inducks as inducks_module
            conn = inducks_module.get_connection()
            if not conn:
                return False
            try:
                available = inducks_module.get_available_inducks_tables(conn=conn)
                return inducks_module.INDUCKS_CORE_TABLES.issubset(available)
            finally:
                conn.close()
        except Exception as e:
            app_logger.error(f"INDUCKS connection test failed: {e}")
            return False

    def search_series(self, query: str, year: Optional[int] = None) -> List[SearchResult]:
        """Search for publications in the INDUCKS database.

        Returns every publication the name resolves to, not a single best guess.
        A Disney title routinely names several runs, and ``_resolve_series_auto``
        is built to send an ambiguous name to review rather than pick one.
        """
        try:
            if not self._is_configured():
                return []

            from models import inducks as inducks_module
            return [SearchResult(
                provider=self.provider_type,
                id=result['id'],
                title=result['name'],
                year=result.get('year_began'),
                publisher=None,  # INDUCKS records the publisher per issue, not per publication
                issue_count=result.get('issue_count'),
                cover_url=None,  # INDUCKS does not distribute cover images
                description=None,
                alternate_title=result.get('title') if result.get('title') != result['name'] else None,
            ) for result in inducks_module.search_series(query, year)]
        except Exception as e:
            app_logger.error(f"INDUCKS search_series failed: {e}")
            return []

    def get_series(self, series_id: str) -> Optional[SearchResult]:
        """Get publication details by INDUCKS publication code, e.g. 'it/TL'."""
        try:
            if not self._is_configured():
                return None

            from models import inducks as inducks_module
            result = inducks_module.get_series(series_id)
            if not result:
                return None

            return SearchResult(
                provider=self.provider_type,
                id=result['id'],
                title=result['name'],
                year=result.get('year_began'),
                publisher=None,
                issue_count=result.get('issue_count'),
                cover_url=None,
                description=None,
                alternate_title=result.get('title') if result.get('title') != result['name'] else None,
            )
        except Exception as e:
            app_logger.error(f"INDUCKS get_series failed: {e}")
            return None

    def get_issues(self, series_id: str) -> List[IssueResult]:
        """Get all issues for an INDUCKS publication."""
        try:
            if not self._is_configured():
                return []

            from models import inducks as inducks_module
            return [IssueResult(
                provider=self.provider_type,
                id=issue['id'],
                series_id=series_id,
                issue_number=issue['issue_number'],
                title=issue.get('title'),
                cover_date=issue.get('cover_date'),
                store_date=None,
                cover_url=None,
                summary=None,
            ) for issue in inducks_module.get_issues(series_id)]
        except Exception as e:
            app_logger.error(f"INDUCKS get_issues failed: {e}")
            return []

    def get_issue(self, issue_id: str) -> Optional[IssueResult]:
        """Get issue details by INDUCKS issue code, e.g. 'it/TL 3200'.

        The issue code embeds its publication code, so the publication is read
        back off the row rather than asked for.
        """
        try:
            if not self._is_configured():
                return None

            from models import inducks as inducks_module
            conn = inducks_module.get_connection()
            if not conn:
                return None

            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT issuecode, publicationcode, issuenumber, title, "
                    "       NULLIF(NULLIF(oldestdate, ''), '9999-12-31') AS oldestdate "
                    "FROM inducks_issue WHERE issuecode = ?",
                    (issue_id,),
                )
                row = cursor.fetchone()
                cursor.close()

                if not row:
                    return None

                return IssueResult(
                    provider=self.provider_type,
                    id=row['issuecode'],
                    series_id=row['publicationcode'],
                    issue_number=(row['issuenumber'] or '').strip(),
                    title=(row['title'] or '').strip() or None,
                    cover_date=row['oldestdate'],
                    store_date=None,
                    cover_url=None,
                    summary=None,
                )
            finally:
                conn.close()
        except Exception as e:
            app_logger.error(f"INDUCKS get_issue failed: {e}")
            return None

    def get_issue_metadata(self, series_id: str, issue_number: str) -> Optional[Dict[str, Any]]:
        """
        Get full issue metadata for one issue of a publication.

        Declared here rather than inherited: ``BaseProvider`` does not require
        it, but every batch-path lookup calls it.
        """
        try:
            if not self._is_configured():
                return None

            from models import inducks as inducks_module
            return inducks_module.get_issue_metadata(series_id, issue_number)
        except Exception as e:
            app_logger.error(f"INDUCKS get_issue_metadata failed: {e}")
            return None

    def to_comicinfo(self, issue: IssueResult, series: Optional[SearchResult] = None) -> Dict[str, Any]:
        """Convert INDUCKS issue data to ComicInfo.xml fields."""
        try:
            if issue.series_id and issue.issue_number:
                from models import inducks as inducks_module
                metadata = inducks_module.get_issue_metadata(issue.series_id, issue.issue_number)
                if metadata:
                    # Already a ComicInfo-compatible dict.
                    return metadata

            # Fallback: build from IssueResult alone. Reached only when the
            # issue has since gone from the database, so it carries no stories,
            # no credits and no characters.
            comicinfo = {
                'Series': series.title if series else None,
                'Number': issue.issue_number,
                'Title': issue.title,
                'Notes': f'Metadata from INDUCKS. Issue: {issue.id}',
            }

            if issue.cover_date and len(issue.cover_date) >= 4:
                try:
                    comicinfo['Year'] = int(issue.cover_date[:4])
                except ValueError:
                    pass

            return {k: v for k, v in comicinfo.items() if v is not None}
        except Exception as e:
            app_logger.error(f"INDUCKS to_comicinfo failed: {e}")
            return {}
