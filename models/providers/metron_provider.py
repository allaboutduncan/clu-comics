"""
Metron Provider Adapter.

Wraps the existing Metron/Mokkari implementation to conform to the BaseProvider interface.
"""

from typing import Optional, List, Dict, Any

from core.app_logging import app_logger
from .base import (
    BaseProvider,
    ProviderType,
    ProviderCredentials,
    SearchResult,
    IssueResult,
)
from . import register_provider


@register_provider
class MetronProvider(BaseProvider):
    """Metron metadata provider using the Mokkari library."""

    provider_type = ProviderType.METRON
    display_name = "Metron"
    requires_auth = True
    auth_fields = ["api_token", "username", "password"]
    auth_modes = [
        {"id": "token", "label": "API Token", "fields": ["api_token"]},
        {"id": "basic", "label": "Username & Password", "fields": ["username", "password"]},
    ]
    # A rejected Metron credential is not a free mistake: the retries it
    # provokes are what got one user's IP banned. Verify at save time so the
    # user finds out immediately instead of via a silent lockout.
    validate_on_save = True
    rate_limit = 20  # Metron rate limit

    def __init__(self, credentials: Optional[ProviderCredentials] = None):
        super().__init__(credentials)
        self._api = None
        self.last_error: Optional[str] = None

    def _has_credentials(self) -> bool:
        creds = self.credentials
        if not creds:
            return False
        return bool(creds.api_token or (creds.username and creds.password))

    def _get_api(self, ignore_auth_lock: bool = False):
        """Get or create the Mokkari API client.

        ``ignore_auth_lock`` is only for ``test_connection``: the lockout has to
        be bypassable by the one call that can clear it, or a user who fixes
        their credentials could never prove it.
        """
        if self._api is not None:
            return self._api

        if not self._has_credentials():
            return None

        try:
            from models import metron as metron_module

            api = metron_module.get_api(
                self.credentials.username or "",
                self.credentials.password or "",
                self.credentials.api_token or "",
                ignore_auth_lock=ignore_auth_lock,
            )
            if not ignore_auth_lock:
                self._api = api
            return api
        except Exception as e:
            app_logger.error(f"Failed to initialize Metron API: {e}")
            return None

    def test_connection(self) -> bool:
        """Test connection to Metron API, and report why it failed.

        Runs even while the auth lockout is latched, and clears it on success --
        this is the path a user takes to confirm corrected credentials.
        """
        self.last_error = None
        try:
            from models import metron as metron_module

            if not self._has_credentials():
                self.last_error = "No Metron credentials configured"
                return False

            api = self._get_api(ignore_auth_lock=True)
            if not api:
                self.last_error = (
                    "Could not build a Metron client from the saved credentials"
                )
                return False

            # A lightweight, paced request: publishers_list is the cheapest
            # authenticated endpoint. Paced because the burst limiter is
            # process-wide and a Test click during a sync must not jump it.
            result = metron_module._api_call(
                lambda: api.publishers_list({"name": "Marvel"}),
                "testing the Metron connection",
                default=None,
                ignore_auth_lock=True,
                raise_auth_errors=True,
            )
            if result is None:
                self.last_error = "Metron did not return a result"
                return False

            metron_module.clear_auth_block()
            return True
        except Exception as e:
            from models import metron as metron_module

            status = metron_module.auth_failure_status(e)
            if status is not None:
                detail = metron_module._auth_detail(e)
                self.last_error = (
                    f"Metron rejected the credentials (HTTP {status}). {detail}".strip()
                )
                metron_module.latch_auth_failure(
                    status, detail, "testing the Metron connection"
                )
            else:
                self.last_error = str(e)
            app_logger.error(f"Metron connection test failed: {e}")
            return False

    def search_series(
        self, query: str, year: Optional[int] = None
    ) -> List[SearchResult]:
        """Search for series on Metron."""
        try:
            api = self._get_api()
            if not api:
                return []

            from models import metron as metron_module

            result = metron_module.search_series_by_name(api, query, year)

            if not result:
                return []

            # search_series_by_name returns a single best match dict
            # Convert to SearchResult
            return [
                SearchResult(
                    provider=self.provider_type,
                    id=str(result.get("id", "")),
                    title=result.get("name", ""),
                    year=result.get("year_began"),
                    publisher=result.get("publisher_name"),
                    issue_count=result.get("issue_count"),
                    cover_url=None,  # Metron doesn't return cover in search
                    description=None,
                )
            ]
        except Exception as e:
            app_logger.error(f"Metron search_series failed: {e}")
            return []

    def get_series(self, series_id: str) -> Optional[SearchResult]:
        """Get series details by Metron series ID."""
        try:
            api = self._get_api()
            if not api:
                return None

            from models import metron as metron_module

            details = metron_module.get_series_details(api, int(series_id))

            if not details:
                return None

            return SearchResult(
                provider=self.provider_type,
                id=str(details.get("id", series_id)),
                title=details.get("name", ""),
                year=details.get("year_began"),
                publisher=details.get("publisher_name"),
                issue_count=details.get("issue_count"),
                cover_url=None,
                description=details.get("desc"),
            )
        except Exception as e:
            app_logger.error(f"Metron get_series failed: {e}")
            return None

    def get_issues(self, series_id: str) -> List[IssueResult]:
        """Get all issues for a Metron series."""
        try:
            api = self._get_api()
            if not api:
                return []

            from models import metron as metron_module

            issues = metron_module.get_all_issues_for_series(api, int(series_id))

            if not issues:
                return []

            results = []
            for issue in issues:
                # Handle both dict and object types
                if hasattr(issue, "__dict__"):
                    issue_id = getattr(issue, "id", None)
                    issue_number = getattr(issue, "number", None)
                    issue_name = getattr(issue, "name", None)
                    cover_date = getattr(issue, "cover_date", None)
                    store_date = getattr(issue, "store_date", None)
                    image = getattr(issue, "image", None)
                else:
                    issue_id = issue.get("id")
                    issue_number = issue.get("number")
                    issue_name = issue.get("name")
                    cover_date = issue.get("cover_date")
                    store_date = issue.get("store_date")
                    image = issue.get("image")

                # Handle name as array (Metron returns ["Title"]) or string
                if isinstance(issue_name, list):
                    issue_title = str(issue_name[0]) if issue_name else None
                else:
                    issue_title = str(issue_name) if issue_name else None

                results.append(
                    IssueResult(
                        provider=self.provider_type,
                        id=str(issue_id) if issue_id else "",
                        series_id=series_id,
                        issue_number=str(issue_number) if issue_number else "",
                        title=issue_title,
                        cover_date=str(cover_date) if cover_date else None,
                        store_date=str(store_date) if store_date else None,
                        cover_url=str(image) if image else None,
                        summary=None,
                    )
                )

            return results
        except Exception as e:
            app_logger.error(f"Metron get_issues failed: {e}")
            return []

    def get_issue(self, issue_id: str) -> Optional[IssueResult]:
        """Get issue details by Metron issue ID."""
        try:
            api = self._get_api()
            if not api:
                return None

            # Paced: this is a raw mokkari call, so without _api_call it would
            # take no rate-limit slot and ignore the auth lockout.
            from models import metron as metron_module

            issue = metron_module._api_call(
                lambda: api.issue(int(issue_id)),
                f"fetching issue {issue_id}",
            )
            if not issue:
                return None

            series_id = (
                getattr(issue.series, "id", None) if hasattr(issue, "series") else None
            )

            return IssueResult(
                provider=self.provider_type,
                id=str(issue.id),
                series_id=str(series_id) if series_id else "",
                issue_number=str(issue.number) if issue.number else "",
                title=str(issue.name[0]) if issue.name else None,
                cover_date=str(issue.cover_date) if issue.cover_date else None,
                store_date=str(issue.store_date) if issue.store_date else None,
                cover_url=str(issue.image) if issue.image else None,
                summary=issue.desc if hasattr(issue, "desc") else None,
            )
        except Exception as e:
            app_logger.error(f"Metron get_issue failed: {e}")
            return None

    def get_issue_metadata(
        self, series_id: str, issue_number: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get full issue metadata for a specific issue in a series.

        This is a convenience method that returns the raw Metron issue data
        suitable for conversion to ComicInfo.xml.
        """
        try:
            api = self._get_api()
            if not api:
                return None

            from models import metron as metron_module

            return metron_module.get_issue_metadata(api, int(series_id), issue_number)
        except Exception as e:
            app_logger.error(f"Metron get_issue_metadata failed: {e}")
            return None

    def to_comicinfo(
        self, issue: IssueResult, series: Optional[SearchResult] = None
    ) -> Dict[str, Any]:
        """Convert Metron issue data to ComicInfo.xml fields."""
        try:
            # For full metadata, we need to fetch the complete issue data.
            # fetch_issue_detail (not api.issue) because this body is written
            # into an archive: it drops any cached copy first, so a bulk re-tag
            # can't rewrite the same half-entered record it is meant to repair.
            api = self._get_api()
            if api and issue.id:
                from models import metron as metron_module

                full_issue = metron_module.fetch_issue_detail(api, issue.id)
                if full_issue:
                    return metron_module.map_to_comicinfo(full_issue)

            # Fallback: build from IssueResult
            comicinfo = {
                "Series": series.title if series else None,
                "Number": issue.issue_number,
                "Title": issue.title,
                "Year": int(issue.cover_date[:4])
                if issue.cover_date and len(issue.cover_date) >= 4
                else None,
                # Raw provider dates for rename templating (not written to XML)
                "CoverDate": issue.cover_date,
                "StoreDate": issue.store_date,
                "Notes": f"Metadata from Metron. Issue ID: {issue.id}",
            }

            if series:
                comicinfo["Publisher"] = series.publisher
                comicinfo["Volume"] = series.year

            return {k: v for k, v in comicinfo.items() if v is not None}
        except Exception as e:
            app_logger.error(f"Metron to_comicinfo failed: {e}")
            return {}
