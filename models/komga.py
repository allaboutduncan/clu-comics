"""
Komga API client for syncing reading history and progress.

Komga is a media server for comics/mangas. This module provides
a client to fetch reading history (completed reads) and reading
progress (in-progress books) from a Komga server via its REST API.

Authentication uses HTTP Basic Auth with username/password.

Key Komga API endpoints used:
- GET /api/v1/libraries - Test connectivity
- POST /api/v1/books/list - Search/filter books by read status (V2 condition format)
"""
import os

import requests
from requests.auth import HTTPBasicAuth
from core.app_logging import app_logger


class KomgaClient:
    """Client for interacting with the Komga REST API."""

    def __init__(self, base_url, username, password):
        """
        Initialize the Komga client.

        Args:
            base_url: Full URL to Komga server (e.g., http://komga:25600)
            username: Komga username (usually an email)
            password: Komga password
        """
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(username, password)
        self.session.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        })

    def test_connection(self):
        """
        Test connectivity by fetching the libraries list (lightweight, auth-required).

        Returns:
            Tuple of (success: bool, details: str)
        """
        try:
            url = f"{self.base_url}/api/v1/libraries"
            app_logger.info(f"Komga test: GET {url}")
            resp = self.session.get(url, timeout=10)
            app_logger.info(f"Komga test: status={resp.status_code}")
            if resp.status_code == 200:
                return True, "Connected successfully"
            elif resp.status_code == 401:
                return False, "Authentication failed (HTTP 401). Check username/password."
            elif resp.status_code == 403:
                return False, "Access forbidden (HTTP 403). Check user permissions."
            else:
                return False, f"Server returned HTTP {resp.status_code}"
        except requests.ConnectionError as e:
            msg = f"Cannot connect to {self.base_url} - is the server running?"
            app_logger.warning(f"Komga connection error: {e}")
            return False, msg
        except requests.Timeout:
            msg = f"Connection to {self.base_url} timed out"
            app_logger.warning(msg)
            return False, msg
        except requests.RequestException as e:
            app_logger.warning(f"Komga connection test failed: {e}")
            return False, str(e)

    def _books_query(self, read_status, page=0, size=500):
        """
        Query books using the V2 condition format.

        Args:
            read_status: One of 'READ', 'IN_PROGRESS', 'UNREAD'
            page: Page number (0-indexed)
            size: Number of results per page

        Returns:
            Tuple of (list_of_books, total_pages, total_elements)
        """
        params = {
            "page": page,
            "size": size,
        }
        body = {
            "condition": {
                "readStatus": {
                    "operator": "is",
                    "value": read_status
                }
            },
            "sort": {
                "lastModified": "desc"
            }
        }
        resp = self.session.post(
            f"{self.base_url}/api/v1/books/list",
            params=params,
            json=body,
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        return (
            data.get('content', []),
            data.get('totalPages', 1),
            data.get('totalElements', 0)
        )

    def get_read_books(self, page=0, size=500):
        """Fetch books with READ status."""
        return self._books_query("READ", page, size)

    def get_in_progress_books(self, page=0, size=500):
        """Fetch books with IN_PROGRESS status."""
        return self._books_query("IN_PROGRESS", page, size)

    def get_all_read_books(self):
        """
        Iterator that handles pagination to yield all READ books.

        Yields:
            Individual book dicts from the Komga API
        """
        page = 0
        while True:
            books, total_pages, total = self.get_read_books(page=page)
            if page == 0:
                app_logger.info(f"Komga: {total} completed books to process")
            for book in books:
                yield book
            page += 1
            if page >= total_pages:
                break

    def get_all_in_progress_books(self):
        """
        Iterator that handles pagination to yield all IN_PROGRESS books.

        Yields:
            Individual book dicts from the Komga API
        """
        page = 0
        while True:
            books, total_pages, total = self.get_in_progress_books(page=page)
            if page == 0:
                app_logger.info(f"Komga: {total} in-progress books to process")
            for book in books:
                yield book
            page += 1
            if page >= total_pages:
                break


def komga_file_name(url):
    """
    Extract the filename, with extension, from a Komga book's `url` field.

    `url` is the only book field carrying the extension. Komga returns the full
    server path to admins but strips the directory for everyone else
    (BookDto.restrictUrl), so the filename is always the last path segment.
    The `name` field is unusable here -- Komga sets it to the stem, without the
    extension, while CLU's file_index stores names with it.

    Both separators are normalized: os.path.basename() ignores '\\' on POSIX, so
    a Komga host running Windows would otherwise yield the whole path.
    """
    return (url or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _is_absolute_path(path):
    """
    True for a POSIX-absolute or natively-absolute path.

    os.path.isabs() alone is not enough: on Windows it rejects '/data/x' (since
    Python 3.13 a lone leading slash is no longer absolute), and the paths CLU
    actually runs on in Docker are POSIX.
    """
    return path.startswith("/") or os.path.isabs(path)


def _parent_dir_name(path):
    """Name of the directory containing `path`, or None if it has no parent."""
    parts = (path or "").replace("\\", "/").rstrip("/").split("/")
    if len(parts) < 2 or not parts[-2]:
        return None
    return parts[-2]


def map_komga_path(komga_path, komga_prefix, clu_prefix):
    """
    Convert a Komga file path to a CLU file path using prefix mapping.

    Returns the path unchanged when the prefix doesn't apply.

    Example:
        komga_path:   /comics/Marvel/Spider-Man 001.cbz
        komga_prefix: /comics
        clu_prefix:   /data
        result:       /data/Marvel/Spider-Man 001.cbz
    """
    if not komga_prefix or not clu_prefix:
        return komga_path

    # Normalize path separators
    komga_path = komga_path.replace("\\", "/")
    komga_prefix = komga_prefix.rstrip("/").replace("\\", "/")
    clu_prefix = clu_prefix.rstrip("/").replace("\\", "/")

    # Match on a path boundary: prefix '/comics' must not swallow
    # '/comics-archive/X.cbz' and rewrite it to '/data-archive/X.cbz'.
    if komga_path == komga_prefix or komga_path.startswith(komga_prefix + "/"):
        relative = komga_path[len(komga_prefix):]
        return clu_prefix + relative

    return komga_path


def map_komga_path_multi(komga_path, mappings):
    """
    Try each library mapping; return first match or original path.
    Mappings should be sorted by prefix length descending so longer prefixes match first.

    Args:
        komga_path: The file path as Komga sees it
        mappings: List of dicts with 'komga_prefix' and 'clu_prefix'

    Returns:
        Mapped CLU path, or original path if no mapping matches
    """
    for m in mappings:
        result = map_komga_path(komga_path, m["komga_prefix"], m["clu_prefix"])
        if result != komga_path:
            return result
    return komga_path


def resolve_komga_book_path(info, mappings, lookup=None):
    """
    Resolve a Komga book to a file in CLU's library.

    Tries the configured path prefixes first (exact and unambiguous), then falls
    back to an exact filename match against the file index -- which is the only
    thing that works for non-admin Komga accounts, or when no prefixes are set.

    Args:
        info: Dict from extract_book_info()
        mappings: List of dicts with 'komga_prefix' and 'clu_prefix', sorted by
            prefix length descending
        lookup: Filename lookup, injectable for tests. Defaults to
            core.database.find_file_index_paths_by_name.

    Returns:
        Tuple of (clu_path or None, reason) where reason is one of
        'mapping', 'file_index', 'no_match', 'ambiguous'.
    """
    if lookup is None:
        from core.database import find_file_index_paths_by_name as lookup

    url = (info.get("url") or "").replace("\\", "/")
    file_name = info.get("file_name") or komga_file_name(url)

    # Prefix mapping only means anything when the url carries a directory.
    # A non-admin's bare filename has nothing to map, and os.path.exists()
    # would resolve it against CLU's working directory.
    if "/" in url:
        mapped = map_komga_path_multi(url, mappings)
        # exists() is what separates "mapped correctly" from "no mapping
        # applied" -- map_komga_path_multi returns its input on no match, and
        # a Komga server path must never reach the database as a CLU path.
        if mapped and _is_absolute_path(mapped) and os.path.exists(mapped):
            return mapped, "mapping"

    if not file_name:
        return None, "no_match"

    # No exists() check on this branch: the path came out of CLU's own index,
    # so it is a CLU path by construction. A stale row costs one orphan history
    # entry; a stat() per book would re-introduce the environment coupling that
    # made every match fail in the first place.
    candidates = lookup(file_name)
    if len(candidates) == 1:
        return candidates[0], "file_index"
    if not candidates:
        return None, "no_match"

    # The same filename in two libraries. Try the directory Komga reports, then
    # give up: marking the wrong comic read silently corrupts reading history
    # and cannot be undone.
    parent = _parent_dir_name(url)
    if parent:
        narrowed = [p for p in candidates if _parent_dir_name(p) == parent]
        if len(narrowed) == 1:
            return narrowed[0], "file_index"
    return None, "ambiguous"


def extract_book_info(book):
    """
    Extract relevant fields from a Komga book object.

    Args:
        book: Book dict from Komga API response

    Returns:
        Dict with normalized fields: id, url, name, file_name, page_count,
        current_page, completed, read_date, last_modified
    """
    media = book.get('media', {})
    read_progress = book.get('readProgress') or {}
    url = book.get('url', '')

    return {
        'id': book.get('id', ''),
        'url': url,
        'name': book.get('name', ''),
        'file_name': komga_file_name(url),
        'page_count': media.get('pagesCount', 0),
        'current_page': read_progress.get('page', 0),
        'completed': read_progress.get('completed', False),
        'read_date': read_progress.get('readDate'),
        'last_modified': read_progress.get('lastModified'),
    }
