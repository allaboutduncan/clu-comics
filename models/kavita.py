"""
Kavita API client for importing reading history and progress.

Kavita keeps progress per user and its API only ever answers for the user the
key belongs to, so one client is one Kavita user. Authentication is an Auth Key
(User Settings -> Manage Auth Keys, Kavita 0.9 and newer) sent in the
``x-api-key`` header.

Endpoints used (checked against Kavita's OpenAPI spec, v0.9.1):
- GET  /api/Account                    - who the key belongs to
- POST /api/Series/v2                  - paged series list (SeriesFilterV2Dto)
- GET  /api/Series/volumes?seriesId=   - volumes, chapters, files and progress
"""
import json
import re

import requests

from core.app_logging import app_logger
from core.external_library_paths import file_name_of

# SeriesFilterField / FilterComparison / FilterCombination / SeriesSortField
# members, by their integer codes in the spec.
FIELD_READ_PROGRESS = 20
COMPARISON_GREATER_THAN = 1
COMBINATION_AND = 1
SORT_NAME = 1

# MangaFormat.Epub. An EPUB's "pages" are not comic pages and CLU does not
# read them.
FORMAT_EPUB = 3

DEFAULT_PAGE_SIZE = 200
# A ceiling on one listing, so a server that ignores paging cannot loop forever.
MAX_PAGES = 500

_TIMESTAMP = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")


class KavitaAuthError(Exception):
    """Kavita rejected the auth key (HTTP 401 or 403)."""


def normalize_timestamp(value):
    """Kavita's UTC timestamp as ``YYYY-MM-DD HH:MM:SS``, or None.

    That is the form SQLite's CURRENT_TIMESTAMP writes, so imported and native
    rows sort and compare together. Kavita reports "never" as year 0001, which
    is treated as no timestamp.
    """
    match = _TIMESTAMP.match(str(value or ""))
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    if int(year) < 1970:
        return None
    return f"{year}-{month}-{day} {hour}:{minute}:{second}"


class KavitaClient:
    """Client for the parts of the Kavita REST API the sync needs."""

    def __init__(self, base_url, auth_key):
        """
        Args:
            base_url: Full URL to the Kavita server (e.g. http://kavita:5000)
            auth_key: A Kavita user's Auth Key
        """
        self.base_url = (base_url or "").rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": auth_key or "",
        })

    def test_connection(self):
        """
        Ask Kavita who the key belongs to.

        Returns:
            Tuple of (success: bool, details: str, username: str or None)
        """
        try:
            resp = self.session.get(f"{self.base_url}/api/Account", timeout=10)
            if resp.status_code == 200:
                username = None
                try:
                    username = (resp.json() or {}).get("username")
                except ValueError:
                    pass
                return True, "Connected successfully", username
            if resp.status_code == 401:
                return (
                    False,
                    "Kavita rejected the auth key (HTTP 401). Create a new key "
                    "under User Settings > Manage Auth Keys. Kavita 0.9 or "
                    "newer is required.",
                    None,
                )
            if resp.status_code == 403:
                return False, "Access forbidden (HTTP 403). Check the Kavita account.", None
            return False, f"Server returned HTTP {resp.status_code}", None
        except requests.ConnectionError as e:
            app_logger.warning(f"Kavita connection error: {e}")
            return False, f"Cannot connect to {self.base_url} - is the server running?", None
        except requests.Timeout:
            return False, f"Connection to {self.base_url} timed out", None
        except requests.RequestException as e:
            app_logger.warning(f"Kavita connection test failed: {e}")
            return False, str(e), None

    @staticmethod
    def _check(resp):
        if resp.status_code in (401, 403):
            raise KavitaAuthError(f"Kavita rejected the auth key (HTTP {resp.status_code})")
        resp.raise_for_status()

    def _series_page(self, page, page_size):
        """One page of series the user has progress on.

        Returns:
            Tuple of (series list, total_pages or None when the server sent no
            Pagination header)
        """
        body = {
            "statements": [{
                "field": FIELD_READ_PROGRESS,
                "comparison": COMPARISON_GREATER_THAN,
                "value": "0",
            }],
            "combination": COMBINATION_AND,
            "sortOptions": {"sortField": SORT_NAME, "isAscending": True},
            "limitTo": 0,
        }
        resp = self.session.post(
            f"{self.base_url}/api/Series/v2",
            params={"PageNumber": page, "PageSize": page_size},
            json=body,
            timeout=30,
        )
        self._check(resp)
        total_pages = None
        try:
            header = resp.headers.get("Pagination")
            if header:
                total_pages = int(json.loads(header).get("totalPages"))
        except (TypeError, ValueError):
            total_pages = None
        data = resp.json()
        return (data if isinstance(data, list) else []), total_pages

    def iter_series_with_progress(self, page_size=DEFAULT_PAGE_SIZE):
        """
        Yield every series the user has read anything of.

        The server filters on read progress, and each row is checked again
        here: a server that ignores the filter must not turn the sync into a
        request per series in the library.
        """
        page = 1
        first_id_of_previous = object()
        while page <= MAX_PAGES:
            series, total_pages = self._series_page(page, page_size)
            if not series:
                return
            # A server that ignores PageNumber returns the same page forever.
            first_id = series[0].get("id")
            if first_id == first_id_of_previous:
                return
            first_id_of_previous = first_id

            for row in series:
                if (row.get("pagesRead") or 0) > 0:
                    yield row

            if total_pages is not None:
                if page >= total_pages:
                    return
            elif len(series) < page_size:
                return
            page += 1

    def get_volumes(self, series_id):
        """Volumes of a series, each with its chapters, files and progress."""
        resp = self.session.get(
            f"{self.base_url}/api/Series/volumes",
            params={"seriesId": series_id},
            timeout=30,
        )
        self._check(resp)
        data = resp.json()
        return data if isinstance(data, list) else []


def iter_chapters(volumes):
    """Every chapter dict in a /api/Series/volumes response."""
    for volume in volumes or []:
        for chapter in volume.get("chapters") or []:
            yield chapter


def extract_chapter_info(chapter):
    """
    Normalize a Kavita chapter (one issue) into what the sync needs.

    Returns None when the chapter cannot be tied to one comic file: it has no
    file, it has several (Kavita's progress counts pages across all of them, so
    it cannot be assigned to any one), or it is an EPUB.

    Returns:
        Dict with chapter_id, file_path, file_name, page_count, pages_read,
        completed, current_page (1-indexed) and read_date.
    """
    files = chapter.get("files") or []
    if len(files) != 1:
        return None
    file = files[0] or {}
    if file.get("format") == FORMAT_EPUB or chapter.get("format") == FORMAT_EPUB:
        return None

    file_path = file.get("filePath") or ""
    if not file_path:
        return None

    page_count = int(chapter.get("pages") or file.get("pages") or 0)
    pages_read = int(chapter.get("pagesRead") or 0)
    completed = page_count > 0 and pages_read >= page_count

    # Kavita stores the 0-based index of the page being viewed and sets it to
    # the page count on the last page, so the page the reader is on, counted
    # from 1 as CLU does, is one more.
    current_page = min(pages_read + 1, page_count) if page_count else pages_read + 1

    return {
        "chapter_id": chapter.get("id"),
        "file_path": file_path,
        "file_name": file_name_of(file_path),
        "page_count": page_count,
        "pages_read": pages_read,
        "completed": completed,
        "current_page": current_page,
        "read_date": normalize_timestamp(
            chapter.get("lastReadingProgressUtc") or chapter.get("lastReadingProgress")
        ),
    }
