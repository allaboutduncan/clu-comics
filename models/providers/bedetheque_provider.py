"""
Bedetheque Provider Adapter.

Web scraping provider for French/Belgian comics database.
Website: https://www.bedetheque.com/
"""
import re
import time
import requests
from typing import Optional, List, Dict, Any
from urllib.parse import quote, urljoin

from core.app_logging import app_logger
from .base import BaseProvider, ProviderType, ProviderCredentials, SearchResult, IssueResult
from . import register_provider

# Check if BeautifulSoup is available
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False
    app_logger.warning("BeautifulSoup not available. Bedetheque provider will be limited.")


@register_provider
class BedethequeProvider(BaseProvider):
    """Bedetheque metadata provider using web scraping."""

    provider_type = ProviderType.BEDETHEQUE
    display_name = "Bedetheque"
    requires_auth = False  # Web scraping, no auth needed
    auth_fields = []  # No authentication required
    rate_limit = 10  # Very conservative - respect the website

    BASE_URL = "https://www.bedetheque.com"

    # Minimum delay between requests (seconds)
    MIN_REQUEST_DELAY = 2.0
    _last_request_time = 0

    def __init__(self, credentials: Optional[ProviderCredentials] = None):
        super().__init__(credentials)
        self._session = None

    def _get_session(self) -> requests.Session:
        """Get or create a requests session with proper headers."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            })
        return self._session

    def _respect_rate_limit(self):
        """Ensure we don't make requests too quickly."""
        current_time = time.time()
        elapsed = current_time - BedethequeProvider._last_request_time
        if elapsed < self.MIN_REQUEST_DELAY:
            time.sleep(self.MIN_REQUEST_DELAY - elapsed)
        BedethequeProvider._last_request_time = time.time()

    def _make_request(self, url: str) -> Optional[str]:
        """Make a request with rate limiting and error handling."""
        if not BS4_AVAILABLE:
            app_logger.warning("BeautifulSoup not available for Bedetheque provider")
            return None

        try:
            self._respect_rate_limit()
            session = self._get_session()
            response = session.get(url, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            app_logger.error(f"Bedetheque request failed: {e}")
            return None

    def _is_configured(self) -> bool:
        """Check if BeautifulSoup is available."""
        return BS4_AVAILABLE

    def test_connection(self) -> bool:
        """Test connection to Bedetheque website."""
        try:
            if not BS4_AVAILABLE:
                return False

            html = self._make_request(self.BASE_URL)
            return html is not None and len(html) > 0
        except Exception as e:
            app_logger.error(f"Bedetheque connection test failed: {e}")
            return False

    def search_series(self, query: str, year: Optional[int] = None) -> List[SearchResult]:
        """Search for comic series on Bedetheque."""
        if not BS4_AVAILABLE:
            return []

        try:
            # Bedetheque search URL format
            search_url = f"{self.BASE_URL}/search/albums?RechIdSerie=&RechSerie={quote(query)}&RechAuteur=&csrf_token_bedetheque="

            html = self._make_request(search_url)
            if not html:
                return []

            soup = BeautifulSoup(html, "html.parser")
            results = []

            # Look for series results in the search page
            # The structure may vary - this is a best-effort implementation
            series_items = soup.select(".item-list .item, .search-results .result-item, .liste-albums li")

            for item in series_items[:20]:  # Limit results
                try:
                    # Try to extract series information
                    title_elem = item.select_one("a.titre, .title a, h3 a, a[href*='/serie']")
                    if not title_elem:
                        continue

                    title = title_elem.get_text(strip=True)
                    href = title_elem.get("href", "")

                    # Extract series ID from URL (format: /serie-12345-BD/...)
                    series_id = None
                    id_match = re.search(r'/serie-(\d+)', href)
                    if id_match:
                        series_id = id_match.group(1)
                    else:
                        # Try album format
                        id_match = re.search(r'/album-(\d+)', href)
                        if id_match:
                            series_id = f"album-{id_match.group(1)}"

                    if not series_id:
                        continue

                    # Try to get cover image
                    img_elem = item.select_one("img")
                    cover_url = None
                    if img_elem:
                        cover_url = img_elem.get("src") or img_elem.get("data-src")
                        if cover_url and not cover_url.startswith("http"):
                            cover_url = urljoin(self.BASE_URL, cover_url)

                    # Try to get year
                    year_elem = item.select_one(".year, .date, .annee")
                    series_year = None
                    if year_elem:
                        year_text = year_elem.get_text(strip=True)
                        year_match = re.search(r'\b(19|20)\d{2}\b', year_text)
                        if year_match:
                            series_year = int(year_match.group(0))

                    # Apply year filter if specified
                    if year and series_year and series_year != year:
                        continue

                    # Try to get description
                    desc_elem = item.select_one(".description, .resume, .synopsis")
                    description = desc_elem.get_text(strip=True) if desc_elem else None

                    results.append(SearchResult(
                        provider=self.provider_type,
                        id=series_id,
                        title=title,
                        year=series_year,
                        publisher=None,
                        issue_count=None,
                        cover_url=cover_url,
                        description=description
                    ))
                except Exception as e:
                    app_logger.debug(f"Error parsing Bedetheque search item: {e}")
                    continue

            # If no results from list parsing, try alternative search
            if not results:
                results = self._search_alternative(query, year)

            return results
        except Exception as e:
            app_logger.error(f"Bedetheque search_series failed: {e}")
            return []

    def _search_alternative(self, query: str, year: Optional[int] = None) -> List[SearchResult]:
        """Alternative search method using different page structure."""
        try:
            # Try the series search page
            search_url = f"{self.BASE_URL}/recherche?RechSerie={quote(query)}"

            html = self._make_request(search_url)
            if not html:
                return []

            soup = BeautifulSoup(html, "html.parser")
            results = []

            # Look for links to series pages
            series_links = soup.select("a[href*='/serie-']")

            seen_ids = set()
            for link in series_links[:20]:
                try:
                    href = link.get("href", "")
                    title = link.get_text(strip=True)

                    if not title or len(title) < 2:
                        continue

                    id_match = re.search(r'/serie-(\d+)', href)
                    if not id_match:
                        continue

                    series_id = id_match.group(1)
                    if series_id in seen_ids:
                        continue
                    seen_ids.add(series_id)

                    results.append(SearchResult(
                        provider=self.provider_type,
                        id=series_id,
                        title=title,
                        year=None,
                        publisher=None,
                        issue_count=None,
                        cover_url=None,
                        description=None
                    ))
                except Exception:
                    continue

            return results
        except Exception as e:
            app_logger.error(f"Bedetheque alternative search failed: {e}")
            return []

    def get_series(self, series_id: str) -> Optional[SearchResult]:
        """Get comic series details by Bedetheque series ID."""
        if not BS4_AVAILABLE:
            return None

        try:
            # Handle album IDs
            if series_id.startswith("album-"):
                return self._get_album_as_series(series_id.replace("album-", ""))

            # Construct series URL
            series_url = f"{self.BASE_URL}/serie-{series_id}-BD/"

            html = self._make_request(series_url)
            if not html:
                return None

            soup = BeautifulSoup(html, "html.parser")

            # Extract title
            title_elem = soup.select_one("h1, .serie-titre, .title")
            title = title_elem.get_text(strip=True) if title_elem else ""

            # Extract cover image
            cover_elem = soup.select_one(".couverture img, .cover img, img[src*='couv']")
            cover_url = None
            if cover_elem:
                cover_url = cover_elem.get("src") or cover_elem.get("data-src")
                if cover_url and not cover_url.startswith("http"):
                    cover_url = urljoin(self.BASE_URL, cover_url)

            # Extract year
            year = None
            info_section = soup.select_one(".info-serie, .series-info, .details")
            if info_section:
                year_match = re.search(r'\b(19|20)\d{2}\b', info_section.get_text())
                if year_match:
                    year = int(year_match.group(0))

            # Extract publisher
            publisher = None
            publisher_elem = soup.select_one("a[href*='editeur'], .editeur, .publisher")
            if publisher_elem:
                publisher = publisher_elem.get_text(strip=True)

            # Extract description
            description = None
            desc_elem = soup.select_one(".resume, .synopsis, .description")
            if desc_elem:
                description = desc_elem.get_text(strip=True)

            # Count albums/issues
            album_count = None
            album_list = soup.select(".liste-albums li, .album-item, tr[id*='album']")
            if album_list:
                album_count = len(album_list)

            return SearchResult(
                provider=self.provider_type,
                id=series_id,
                title=title,
                year=year,
                publisher=publisher,
                issue_count=album_count,
                cover_url=cover_url,
                description=description
            )
        except Exception as e:
            app_logger.error(f"Bedetheque get_series failed: {e}")
            return None

    def _get_album_as_series(self, album_id: str) -> Optional[SearchResult]:
        """Get album details and return as a SearchResult."""
        try:
            album_url = f"{self.BASE_URL}/album-{album_id}-BD.html"

            html = self._make_request(album_url)
            if not html:
                return None

            soup = BeautifulSoup(html, "html.parser")

            title_elem = soup.select_one("h1, .album-titre")
            title = title_elem.get_text(strip=True) if title_elem else ""

            cover_elem = soup.select_one(".couverture img, img[src*='couv']")
            cover_url = None
            if cover_elem:
                cover_url = cover_elem.get("src")
                if cover_url and not cover_url.startswith("http"):
                    cover_url = urljoin(self.BASE_URL, cover_url)

            return SearchResult(
                provider=self.provider_type,
                id=f"album-{album_id}",
                title=title,
                year=None,
                publisher=None,
                issue_count=1,
                cover_url=cover_url,
                description=None
            )
        except Exception as e:
            app_logger.error(f"Bedetheque _get_album_as_series failed: {e}")
            return None

    def get_issues(self, series_id: str) -> List[IssueResult]:
        """Get albums/issues for a Bedetheque series."""
        if not BS4_AVAILABLE:
            return []

        try:
            # Handle album IDs (single issue)
            if series_id.startswith("album-"):
                album_id = series_id.replace("album-", "")
                return [IssueResult(
                    provider=self.provider_type,
                    id=album_id,
                    series_id=series_id,
                    issue_number="1",
                    title=None,
                    cover_date=None,
                    store_date=None,
                    cover_url=None,
                    summary=None
                )]

            series_url = f"{self.BASE_URL}/serie-{series_id}-BD/"

            html = self._make_request(series_url)
            if not html:
                return []

            soup = BeautifulSoup(html, "html.parser")
            results = []

            # Find album list
            albums = soup.select(".liste-albums li, .album-item, tr[id*='album'], a[href*='/album-']")

            seen_ids = set()
            issue_num = 1

            for album in albums:
                try:
                    # Find album link
                    if album.name == "a":
                        link = album
                    else:
                        link = album.select_one("a[href*='/album-']")

                    if not link:
                        continue

                    href = link.get("href", "")
                    album_id_match = re.search(r'/album-(\d+)', href)
                    if not album_id_match:
                        continue

                    album_id = album_id_match.group(1)
                    if album_id in seen_ids:
                        continue
                    seen_ids.add(album_id)

                    # Get title
                    title = link.get_text(strip=True)

                    # Try to extract issue number from title
                    num_match = re.search(r'(?:^|[^\d])(\d+)(?:[^\d]|$)', title)
                    if num_match:
                        issue_number = num_match.group(1)
                    else:
                        issue_number = str(issue_num)
                        issue_num += 1

                    # Try to get cover
                    img = album.select_one("img") if album.name != "a" else None
                    cover_url = None
                    if img:
                        cover_url = img.get("src") or img.get("data-src")
                        if cover_url and not cover_url.startswith("http"):
                            cover_url = urljoin(self.BASE_URL, cover_url)

                    results.append(IssueResult(
                        provider=self.provider_type,
                        id=album_id,
                        series_id=series_id,
                        issue_number=issue_number,
                        title=title if title != issue_number else None,
                        cover_date=None,
                        store_date=None,
                        cover_url=cover_url,
                        summary=None
                    ))
                except Exception as e:
                    app_logger.debug(f"Error parsing Bedetheque album: {e}")
                    continue

            return results
        except Exception as e:
            app_logger.error(f"Bedetheque get_issues failed: {e}")
            return []

    def get_issue(self, issue_id: str) -> Optional[IssueResult]:
        """Get album/issue details by Bedetheque album ID."""
        if not BS4_AVAILABLE:
            return None

        try:
            album_url = f"{self.BASE_URL}/album-{issue_id}-BD.html"

            html = self._make_request(album_url)
            if not html:
                return None

            soup = BeautifulSoup(html, "html.parser")

            # Extract title
            title_elem = soup.select_one("h1, .album-titre")
            title = title_elem.get_text(strip=True) if title_elem else ""

            # Try to find series ID
            series_link = soup.select_one("a[href*='/serie-']")
            series_id = None
            if series_link:
                href = series_link.get("href", "")
                id_match = re.search(r'/serie-(\d+)', href)
                if id_match:
                    series_id = id_match.group(1)

            # Extract issue number
            issue_number = "1"
            num_match = re.search(r'(?:Tome|T\.|#|Vol\.?)\s*(\d+)', title, re.IGNORECASE)
            if num_match:
                issue_number = num_match.group(1)

            # Get cover
            cover_elem = soup.select_one(".couverture img, img[src*='couv']")
            cover_url = None
            if cover_elem:
                cover_url = cover_elem.get("src")
                if cover_url and not cover_url.startswith("http"):
                    cover_url = urljoin(self.BASE_URL, cover_url)

            # Get summary
            summary = None
            summary_elem = soup.select_one(".resume, .synopsis")
            if summary_elem:
                summary = summary_elem.get_text(strip=True)

            return IssueResult(
                provider=self.provider_type,
                id=issue_id,
                series_id=series_id or f"album-{issue_id}",
                issue_number=issue_number,
                title=title,
                cover_date=None,
                store_date=None,
                cover_url=cover_url,
                summary=summary
            )
        except Exception as e:
            app_logger.error(f"Bedetheque get_issue failed: {e}")
            return None

    def get_issue_metadata(self, series_id: str, issue_number: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a specific issue in a series."""
        try:
            series = self.get_series(series_id)
            issues = self.get_issues(series_id)

            # Find the matching issue
            issue = None
            for i in issues:
                if i.issue_number == issue_number:
                    issue = i
                    break

            if not issue:
                return None

            # Get full issue details
            full_issue = self.get_issue(issue.id)

            metadata = {
                "Number": issue_number,
                "Web": f"{self.BASE_URL}/album-{issue.id}-BD.html",
                "Notes": f"Metadata from Bedetheque. Album ID: {issue.id}",
                "LanguageISO": "fr"
            }

            if series:
                metadata["Series"] = series.title
                metadata["Publisher"] = series.publisher
                if series.year:
                    metadata["Year"] = series.year

            if full_issue:
                if full_issue.title:
                    metadata["Title"] = full_issue.title
                if full_issue.summary:
                    metadata["Summary"] = full_issue.summary

            return {k: v for k, v in metadata.items() if v is not None}
        except Exception as e:
            app_logger.error(f"Bedetheque get_issue_metadata failed: {e}")
            return None

    def to_comicinfo(self, issue: IssueResult, series: Optional[SearchResult] = None) -> Dict[str, Any]:
        """Convert Bedetheque data to ComicInfo.xml fields."""
        try:
            if not series and issue.series_id:
                series = self.get_series(issue.series_id)

            comicinfo = {
                "Number": issue.issue_number,
                "Title": issue.title,
                "Summary": issue.summary,
                "Notes": f"Metadata from Bedetheque. Album ID: {issue.id}",
                "Web": f"{self.BASE_URL}/album-{issue.id}-BD.html",
                "LanguageISO": "fr"
            }

            if series:
                comicinfo["Series"] = series.title
                comicinfo["Publisher"] = series.publisher
                comicinfo["Volume"] = series.year

                if series.year:
                    comicinfo["Year"] = series.year

            return {k: v for k, v in comicinfo.items() if v is not None}
        except Exception as e:
            app_logger.error(f"Bedetheque to_comicinfo failed: {e}")
            return {}
