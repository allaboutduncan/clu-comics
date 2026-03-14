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


def extract_book_info(book):
    """
    Extract relevant fields from a Komga book object.

    Args:
        book: Book dict from Komga API response

    Returns:
        Dict with normalized fields: id, url, name, page_count,
        current_page, completed, read_date, last_modified
    """
    media = book.get('media', {})
    read_progress = book.get('readProgress') or {}

    return {
        'id': book.get('id', ''),
        'url': book.get('url', ''),
        'name': book.get('name', ''),
        'page_count': media.get('pagesCount', 0),
        'current_page': read_progress.get('page', 0),
        'completed': read_progress.get('completed', False),
        'read_date': read_progress.get('readDate'),
        'last_modified': read_progress.get('lastModified'),
    }
