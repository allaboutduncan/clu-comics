"""
GCD REST API Provider Adapter.

Uses the GCD REST API at comics.org to provide metadata,
separate from the MySQL-based GCD provider.
"""
import re
from datetime import datetime
from typing import Optional, List, Dict, Any

from core.app_logging import app_logger
from .base import BaseProvider, ProviderType, ProviderCredentials, SearchResult, IssueResult
from . import register_provider


def _extract_id_from_url(api_url: str) -> Optional[str]:
    """Extract numeric ID from a GCD API URL like 'https://www.comics.org/api/series/12345/'."""
    if not api_url:
        return None
    match = re.search(r'/(\d+)/?$', api_url)
    return match.group(1) if match else None


def _clean_issue_number(descriptor: str) -> str:
    """Clean a GCD descriptor like '3 [Jorge Jiménez Cover]' to just '3'."""
    if not descriptor:
        return ''
    # Remove bracketed content: [Variant Cover], [Jorge Jiménez Cover], etc.
    cleaned = re.sub(r'\s*\[.*?\]\s*', '', str(descriptor)).strip()
    # Also remove trailing parenthetical variant info: (2nd printing), etc.
    cleaned = re.sub(r'\s*\(.*?\)\s*$', '', cleaned).strip()
    return cleaned


def _parse_credits_text(credits_text: str) -> List[str]:
    """Parse a GCD credits string like 'John Doe; Jane Smith' into a list of names."""
    if not credits_text or credits_text.strip() in ('?', 'None', ''):
        return []
    # Strip parenthetical annotations BEFORE splitting on semicolons,
    # because annotations can contain semicolons e.g. "(signed as ADKINS [long stroke "A"; capital letters])"
    cleaned = re.sub(r'\s*\([^()]*(?:\([^()]*\)[^()]*)*\)', '', credits_text)
    # Also strip standalone square bracket annotations
    cleaned = re.sub(r'\s*\[[^\[\]]*\]', '', cleaned)
    # GCD separates multiple creators with semicolons
    names = []
    for name in re.split(r'[;]', cleaned):
        name = name.strip()
        # Strip trailing "?" — GCD uses this to mark uncertain credits
        name = re.sub(r'\s*\?\s*$', '', name).strip()
        if name and name not in ('?', 'None', 'typeset', 'various') and name not in names:
            names.append(name)
    return names


@register_provider
class GCDApiProvider(BaseProvider):
    """GCD metadata provider using the REST API at comics.org."""

    provider_type = ProviderType.GCD_API
    display_name = "Grand Comics Database (API)"
    requires_auth = True
    auth_fields = ["username", "password"]
    rate_limit = 30

    def __init__(self, credentials: Optional[ProviderCredentials] = None):
        super().__init__(credentials)
        self._client_instance = None

    def _get_client(self):
        """Get or create the GCD API client."""
        if self._client_instance is not None:
            return self._client_instance

        username = None
        password = None

        # Try passed credentials first
        if self.credentials and self.credentials.username and self.credentials.password:
            username = self.credentials.username
            password = self.credentials.password
        else:
            # Try saved credentials from DB
            try:
                from core.database import get_provider_credentials
                saved = get_provider_credentials('gcd_api')
                if saved:
                    username = saved.get('username')
                    password = saved.get('password')
            except Exception:
                pass

        if not username or not password:
            return None

        from models.gcd_api import GCDApiClient
        self._client_instance = GCDApiClient(username, password)
        return self._client_instance

    def test_connection(self) -> bool:
        """Test connection to GCD REST API."""
        try:
            client = self._get_client()
            if not client:
                return False
            results = client.search_series("Batman")
            return len(results) > 0
        except Exception as e:
            app_logger.error(f"GCD API connection test failed: {e}")
            return False

    def search_series(self, query: str, year: Optional[int] = None) -> List[SearchResult]:
        """Search for series using the GCD REST API."""
        try:
            client = self._get_client()
            if not client:
                return []

            raw_results = client.search_series(query, year)
            if not raw_results:
                return []

            results = []
            for series in raw_results:
                series_id = _extract_id_from_url(series.get('api_url')) or str(series.get('id', ''))
                publisher_name = None
                publisher_url = series.get('publisher')
                if isinstance(publisher_url, dict):
                    publisher_name = publisher_url.get('name')
                elif isinstance(publisher_url, str) and publisher_url:
                    # Publisher might be a URL; we could fetch it, but for search results
                    # just extract what we have
                    publisher_name = None

                results.append(SearchResult(
                    provider=self.provider_type,
                    id=series_id,
                    title=series.get('name', series.get('series_name', '')),
                    year=series.get('year_began'),
                    publisher=publisher_name,
                    issue_count=series.get('issue_count'),
                    cover_url=None,
                    description=series.get('notes')
                ))

            return results
        except Exception as e:
            app_logger.error(f"GCD API search_series failed: {e}")
            return []

    def get_series(self, series_id: str) -> Optional[SearchResult]:
        """Get series details by GCD series ID."""
        try:
            client = self._get_client()
            if not client:
                return None

            series = client.get_series(int(series_id))
            if not series:
                return None

            publisher_name = None
            publisher_data = series.get('publisher')
            if isinstance(publisher_data, dict):
                publisher_name = publisher_data.get('name')

            return SearchResult(
                provider=self.provider_type,
                id=series_id,
                title=series.get('name', ''),
                year=series.get('year_began'),
                publisher=publisher_name,
                issue_count=series.get('issue_count'),
                cover_url=None,
                description=series.get('notes')
            )
        except Exception as e:
            app_logger.error(f"GCD API get_series failed: {e}")
            return None

    def get_issues(self, series_id: str) -> List[IssueResult]:
        """Get all issues for a series from the GCD API."""
        try:
            client = self._get_client()
            if not client:
                return []

            series = client.get_series(int(series_id))
            if not series:
                return []

            results = []
            # Series response has parallel arrays: active_issues (URLs) and issue_descriptors (labels)
            active_issues = series.get('active_issues', [])
            issue_descriptors = series.get('issue_descriptors', [])

            for i, issue_url in enumerate(active_issues):
                issue_id = _extract_id_from_url(issue_url) if isinstance(issue_url, str) else None
                descriptor = issue_descriptors[i] if i < len(issue_descriptors) else ''

                if issue_id:
                    results.append(IssueResult(
                        provider=self.provider_type,
                        id=issue_id,
                        series_id=series_id,
                        issue_number=_clean_issue_number(descriptor),
                        title=None,
                        cover_date=None,
                        store_date=None,
                        cover_url=None,
                        summary=None
                    ))

            return results
        except Exception as e:
            app_logger.error(f"GCD API get_issues failed: {e}")
            return []

    def get_issue(self, issue_id: str) -> Optional[IssueResult]:
        """Get full issue details by GCD issue ID."""
        try:
            client = self._get_client()
            if not client:
                return None

            issue = client.get_issue(int(issue_id))
            if not issue:
                return None

            series_url = issue.get('series')
            series_id = ''
            if isinstance(series_url, dict):
                series_id = _extract_id_from_url(series_url.get('api_url')) or ''
            elif isinstance(series_url, str):
                series_id = _extract_id_from_url(series_url) or ''

            cover_url = issue.get('cover')
            if not cover_url:
                # Try to extract from story with sequence_number 0 (cover)
                stories = issue.get('story_set', issue.get('stories', []))
                for story in stories:
                    if isinstance(story, dict) and story.get('sequence_number') == 0:
                        cover_url = story.get('cover') or story.get('image')
                        break

            return IssueResult(
                provider=self.provider_type,
                id=issue_id,
                series_id=series_id,
                issue_number=_clean_issue_number(issue.get('descriptor', issue.get('number', ''))),
                title=issue.get('title'),
                cover_date=issue.get('publication_date', issue.get('key_date')),
                store_date=issue.get('on_sale_date'),
                cover_url=cover_url,
                summary=None
            )
        except Exception as e:
            app_logger.error(f"GCD API get_issue failed: {e}")
            return None

    def get_issue_metadata(self, series_id: str, issue_number: str) -> Optional[Dict[str, Any]]:
        """
        Get full issue metadata suitable for ComicInfo.xml.

        Uses the series name + issue number search endpoint to find the issue,
        then fetches full details with credits.
        """
        try:
            client = self._get_client()
            if not client:
                return None

            # Get series info
            series = client.get_series(int(series_id))
            if not series:
                return None

            series_name = series.get('name', '')
            start_year = series.get('year_began')

            # Find the issue using the search_issue endpoint (more reliable than
            # parsing the parallel active_issues/issue_descriptors arrays, which
            # contain variant descriptors like "3 [Jim Lee Cover]")
            target_issue_id = None

            # Try with year filter first for precision, then without
            for search_year in ([start_year, None] if start_year else [None]):
                issue_results = client.search_issue(series_name, issue_number, year=search_year)
                if issue_results:
                    # Find the main printing (not a variant) — prefer entries without variant_of
                    for ir in issue_results:
                        if not ir.get('variant_of'):
                            # Check it belongs to our series
                            ir_series_url = ir.get('series', '')
                            ir_series_id = _extract_id_from_url(ir_series_url)
                            if ir_series_id == series_id:
                                target_issue_id = _extract_id_from_url(ir.get('api_url'))
                                break

                    # Fallback: take first result matching our series
                    if not target_issue_id:
                        for ir in issue_results:
                            ir_series_url = ir.get('series', '')
                            ir_series_id = _extract_id_from_url(ir_series_url)
                            if ir_series_id == series_id:
                                target_issue_id = _extract_id_from_url(ir.get('api_url'))
                                break

                if target_issue_id:
                    break

            # Last resort: match from parallel arrays in series detail
            if not target_issue_id:
                active_issues = series.get('active_issues', [])
                issue_descriptors = series.get('issue_descriptors', [])
                issue_num_stripped = issue_number.lstrip('0') or '0'
                for i, descriptor in enumerate(issue_descriptors):
                    # Descriptor may be "3" or "3 [Variant Cover]" — match the number part
                    desc_num = str(descriptor).split('[')[0].split('(')[0].strip()
                    if desc_num == issue_number or desc_num.lstrip('0') == issue_num_stripped:
                        if i < len(active_issues):
                            target_issue_id = _extract_id_from_url(active_issues[i])
                            break

            if not target_issue_id:
                app_logger.info(f"GCD API: Issue #{issue_number} not found in series {series_id} ({series_name})")
                return None

            # Fetch full issue details
            issue = client.get_issue(int(target_issue_id))
            if not issue:
                return None

            return self._build_comicinfo_from_api(issue, series)
        except Exception as e:
            app_logger.error(f"GCD API get_issue_metadata failed: {e}")
            return None

    def _build_comicinfo_from_api(self, issue: Dict, series: Dict) -> Dict[str, Any]:
        """Build ComicInfo-compatible dict from API issue and series responses."""
        writers = []
        pencillers = []
        inkers = []
        colorists = []
        letterers = []
        editors = []
        cover_artists = []
        characters_set = set()
        genres_set = set()
        title = None
        summary = None

        stories = issue.get('story_set', issue.get('stories', []))
        for story in stories:
            if not isinstance(story, dict):
                continue

            seq = story.get('sequence_number')

            # Collect credits from all stories
            for name in _parse_credits_text(story.get('script', '')):
                if name not in writers:
                    writers.append(name)
            for name in _parse_credits_text(story.get('pencils', '')):
                if name not in pencillers:
                    pencillers.append(name)
            for name in _parse_credits_text(story.get('inks', '')):
                if name not in inkers:
                    inkers.append(name)
            for name in _parse_credits_text(story.get('colors', '')):
                if name not in colorists:
                    colorists.append(name)
            for name in _parse_credits_text(story.get('letters', '')):
                if name not in letterers:
                    letterers.append(name)
            for name in _parse_credits_text(story.get('editing', '')):
                if name not in editors:
                    editors.append(name)

            # Characters and genre (GCD uses "None" string for empty values)
            chars = story.get('characters', '')
            if chars and chars not in ('?', 'None', ''):
                for c in re.split(r'[;]', chars):
                    c = c.strip()
                    if c and c != 'None':
                        characters_set.add(c)

            genre = story.get('genre', '')
            if genre and genre not in ('?', 'None', ''):
                for g in re.split(r'[;,]', genre):
                    g = g.strip()
                    if g and g != 'None':
                        genres_set.add(g)

            # Cover story (sequence 0) - get cover artists
            if seq == 0:
                for name in _parse_credits_text(story.get('pencils', '')):
                    if name not in cover_artists:
                        cover_artists.append(name)
            else:
                # Use first non-cover story title and synopsis
                if not title:
                    t = story.get('title', '')
                    if t and t.strip() and t.strip() != 'None':
                        title = t.strip()
                if not summary:
                    s = story.get('synopsis', '') or story.get('notes', '')
                    if s and s.strip() and s.strip() != 'None':
                        summary = s.strip()

        # Parse date fields — prefer key_date (ISO format "YYYY-MM-DD")
        # over publication_date (human format "June 2016")
        key_date = issue.get('key_date', '')
        pub_date = issue.get('publication_date', '')
        year = None
        month = None
        # Try key_date first (ISO format)
        if key_date and len(str(key_date)) >= 4:
            try:
                year = int(str(key_date)[:4])
            except ValueError:
                pass
            if len(str(key_date)) >= 7:
                try:
                    month = int(str(key_date)[5:7])
                except ValueError:
                    pass
        # Fallback: parse publication_date like "June 2016"
        if not year and pub_date:
            import calendar
            parts = str(pub_date).strip().split()
            for part in parts:
                try:
                    y = int(part)
                    if 1900 <= y <= 2100:
                        year = y
                        break
                except ValueError:
                    continue
            if not month:
                month_names = {name.lower(): num for num, name in enumerate(calendar.month_name) if num}
                month_abbrs = {name.lower(): num for num, name in enumerate(calendar.month_abbr) if num}
                for part in parts:
                    m = month_names.get(part.lower()) or month_abbrs.get(part.lower().rstrip('.'))
                    if m:
                        month = m
                        break

        # Publisher
        publisher_name = None
        publisher_data = series.get('publisher')
        if isinstance(publisher_data, dict):
            publisher_name = publisher_data.get('name')

        # Cover URL
        cover_url = issue.get('cover')

        current_date = datetime.now().strftime('%Y-%m-%d')

        metadata = {
            'Series': series.get('name', ''),
            'Number': _clean_issue_number(issue.get('descriptor', issue.get('number', ''))),
            'Volume': series.get('year_began'),
            'Title': title,
            'Summary': summary,
            'Publisher': publisher_name,
            'Year': year,
            'Month': month,
            'Writer': ', '.join(writers) if writers else None,
            'Penciller': ', '.join(pencillers) if pencillers else None,
            'Inker': ', '.join(inkers) if inkers else None,
            'Colorist': ', '.join(colorists) if colorists else None,
            'Letterer': ', '.join(letterers) if letterers else None,
            'Editor': ', '.join(editors) if editors else None,
            'CoverArtist': ', '.join(cover_artists) if cover_artists else None,
            'Characters': '; '.join(sorted(characters_set)) if characters_set else None,
            'Genre': ', '.join(sorted(genres_set)) if genres_set else None,
            'PageCount': issue.get('page_count'),
            'LanguageISO': 'en',
            'Web': f"https://www.comics.org/issue/{issue.get('id', '')}/",
            'Notes': f'Metadata from GCD REST API. Issue ID: {issue.get("id", "")} — retrieved {current_date}.',
        }

        if cover_url:
            metadata['_cover_url'] = cover_url

        # GCD uses "?" for unknown/uncertain values — strip these out
        def _is_valid(v):
            if v is None:
                return False
            if isinstance(v, str) and v.strip() in ('?', 'None', ''):
                return False
            return True

        return {k: v for k, v in metadata.items() if _is_valid(v)}

    def to_comicinfo(self, issue: IssueResult, series: Optional[SearchResult] = None) -> Dict[str, Any]:
        """Convert GCD API issue data to ComicInfo.xml fields."""
        try:
            # Try full metadata fetch
            if issue.series_id and issue.issue_number:
                metadata = self.get_issue_metadata(issue.series_id, issue.issue_number)
                if metadata:
                    return metadata

            # Fallback: try fetching full issue by ID
            client = self._get_client()
            if client and issue.id:
                full_issue = client.get_issue(int(issue.id))
                if full_issue:
                    series_data = {}
                    if issue.series_id:
                        series_data = client.get_series(int(issue.series_id)) or {}
                    return self._build_comicinfo_from_api(full_issue, series_data)

            # Minimal fallback from IssueResult
            comicinfo = {
                'Series': series.title if series else None,
                'Number': issue.issue_number,
                'Title': issue.title,
                'Notes': f'Metadata from GCD REST API. Issue ID: {issue.id}',
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
            app_logger.error(f"GCD API to_comicinfo failed: {e}")
            return {}
