"""Tests for models/komga.py -- mocked HTTP calls via requests.Session."""
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
import requests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client():
    """Create a KomgaClient with a mocked session (no real HTTP)."""
    with patch("models.komga.requests.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        from models.komga import KomgaClient
        client = KomgaClient("http://komga:25600", "user@example.com", "secret")
    # The session was replaced during __init__; return both for assertions
    return client, mock_session


def _make_page_response(content, total_pages=1, total_elements=None):
    """Build a mock Response whose .json() returns paginated Komga data."""
    if total_elements is None:
        total_elements = len(content)
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "content": content,
        "totalPages": total_pages,
        "totalElements": total_elements,
    }
    resp.raise_for_status = MagicMock()
    return resp


def _make_book(book_id="abc-123", name="Batman #1", pages_count=24,
               page=0, completed=False, read_date=None, last_modified=None):
    """Return a dict mimicking a Komga book JSON object."""
    book = {
        "id": book_id,
        "url": f"/api/v1/books/{book_id}",
        "name": name,
        "media": {"pagesCount": pages_count},
    }
    if completed or page or read_date or last_modified:
        book["readProgress"] = {
            "page": page,
            "completed": completed,
            "readDate": read_date,
            "lastModified": last_modified,
        }
    return book


# ---------------------------------------------------------------------------
# KomgaClient.__init__
# ---------------------------------------------------------------------------

class TestKomgaClientInit:

    def test_base_url_trailing_slash_stripped(self):
        """Trailing slashes on the base URL must be removed."""
        with patch("models.komga.requests.Session"):
            from models.komga import KomgaClient
            client = KomgaClient("http://komga:25600/", "u", "p")
            assert client.base_url == "http://komga:25600"

    def test_base_url_no_trailing_slash(self):
        """A URL without trailing slash is stored as-is."""
        with patch("models.komga.requests.Session"):
            from models.komga import KomgaClient
            client = KomgaClient("http://komga:25600", "u", "p")
            assert client.base_url == "http://komga:25600"


# ---------------------------------------------------------------------------
# KomgaClient.test_connection
# ---------------------------------------------------------------------------

class TestTestConnection:

    def test_success_200(self):
        client, session = _make_client()
        resp = MagicMock()
        resp.status_code = 200
        session.get.return_value = resp

        ok, msg = client.test_connection()

        assert ok is True
        assert "success" in msg.lower()
        session.get.assert_called_once_with(
            "http://komga:25600/api/v1/libraries", timeout=10
        )

    def test_auth_failure_401(self):
        client, session = _make_client()
        resp = MagicMock()
        resp.status_code = 401
        session.get.return_value = resp

        ok, msg = client.test_connection()

        assert ok is False
        assert "401" in msg

    def test_forbidden_403(self):
        client, session = _make_client()
        resp = MagicMock()
        resp.status_code = 403
        session.get.return_value = resp

        ok, msg = client.test_connection()

        assert ok is False
        assert "403" in msg

    def test_connection_error(self):
        client, session = _make_client()
        session.get.side_effect = requests.ConnectionError("refused")

        ok, msg = client.test_connection()

        assert ok is False
        assert "komga:25600" in msg.lower()

    def test_timeout(self):
        client, session = _make_client()
        session.get.side_effect = requests.Timeout("timed out")

        ok, msg = client.test_connection()

        assert ok is False
        assert "timed out" in msg.lower()


# ---------------------------------------------------------------------------
# KomgaClient._books_query
# ---------------------------------------------------------------------------

class TestBooksQuery:

    def test_sends_correct_payload(self):
        """Verify the POST body and query-string params."""
        client, session = _make_client()
        session.post.return_value = _make_page_response(
            [_make_book()], total_pages=1, total_elements=1,
        )

        books, total_pages, total_elements = client._books_query("READ", page=0, size=100)

        session.post.assert_called_once()
        call_kwargs = session.post.call_args
        assert call_kwargs[0][0] == "http://komga:25600/api/v1/books/list"
        assert call_kwargs[1]["params"] == {"page": 0, "size": 100}
        body = call_kwargs[1]["json"]
        assert body["condition"]["readStatus"]["operator"] == "is"
        assert body["condition"]["readStatus"]["value"] == "READ"
        assert body["sort"] == {"lastModified": "desc"}

        assert len(books) == 1
        assert total_pages == 1
        assert total_elements == 1

    def test_returns_empty_on_no_content(self):
        client, session = _make_client()
        resp = MagicMock()
        resp.json.return_value = {}
        resp.raise_for_status = MagicMock()
        session.post.return_value = resp

        books, total_pages, total_elements = client._books_query("UNREAD")

        assert books == []
        assert total_pages == 1
        assert total_elements == 0


# ---------------------------------------------------------------------------
# get_read_books / get_in_progress_books delegation
# ---------------------------------------------------------------------------

class TestReadAndInProgressBooks:

    def test_get_read_books_delegates(self):
        client, session = _make_client()
        session.post.return_value = _make_page_response([_make_book()], 1, 1)

        books, tp, te = client.get_read_books(page=2, size=50)

        call_kwargs = session.post.call_args
        body = call_kwargs[1]["json"]
        assert body["condition"]["readStatus"]["value"] == "READ"
        assert call_kwargs[1]["params"]["page"] == 2
        assert call_kwargs[1]["params"]["size"] == 50

    def test_get_in_progress_books_delegates(self):
        client, session = _make_client()
        session.post.return_value = _make_page_response([_make_book()], 1, 1)

        books, tp, te = client.get_in_progress_books(page=1, size=25)

        call_kwargs = session.post.call_args
        body = call_kwargs[1]["json"]
        assert body["condition"]["readStatus"]["value"] == "IN_PROGRESS"
        assert call_kwargs[1]["params"]["page"] == 1
        assert call_kwargs[1]["params"]["size"] == 25


# ---------------------------------------------------------------------------
# get_all_read_books / get_all_in_progress_books (pagination generators)
# ---------------------------------------------------------------------------

class TestGetAllReadBooks:

    def test_pagination_two_pages(self):
        """Generator should yield books across two pages then stop."""
        client, _ = _make_client()

        page0_books = [_make_book(book_id="b1"), _make_book(book_id="b2")]
        page1_books = [_make_book(book_id="b3")]

        with patch.object(client, "get_read_books") as mock_grb:
            mock_grb.side_effect = [
                (page0_books, 2, 3),   # page 0: 2 total pages, 3 total
                (page1_books, 2, 3),   # page 1
            ]
            result = list(client.get_all_read_books())

        assert len(result) == 3
        assert result[0]["id"] == "b1"
        assert result[2]["id"] == "b3"
        assert mock_grb.call_count == 2

    def test_single_page(self):
        client, _ = _make_client()

        with patch.object(client, "get_read_books") as mock_grb:
            mock_grb.return_value = ([_make_book()], 1, 1)
            result = list(client.get_all_read_books())

        assert len(result) == 1
        assert mock_grb.call_count == 1


class TestGetAllInProgressBooks:

    def test_single_page(self):
        client, _ = _make_client()

        with patch.object(client, "get_in_progress_books") as mock_gip:
            mock_gip.return_value = ([_make_book(book_id="ip1")], 1, 1)
            result = list(client.get_all_in_progress_books())

        assert len(result) == 1
        assert result[0]["id"] == "ip1"
        assert mock_gip.call_count == 1

    def test_empty_results(self):
        client, _ = _make_client()

        with patch.object(client, "get_in_progress_books") as mock_gip:
            mock_gip.return_value = ([], 1, 0)
            result = list(client.get_all_in_progress_books())

        assert result == []


# ---------------------------------------------------------------------------
# extract_book_info (pure function)
# ---------------------------------------------------------------------------

class TestExtractBookInfo:

    def test_full_data(self):
        from models.komga import extract_book_info

        book = {
            "id": "abc-123",
            "url": "/api/v1/books/abc-123",
            "name": "Batman #1",
            "media": {"pagesCount": 24},
            "readProgress": {
                "page": 24,
                "completed": True,
                "readDate": "2024-06-15T12:00:00Z",
                "lastModified": "2024-06-15T12:30:00Z",
            },
        }
        info = extract_book_info(book)

        assert info["id"] == "abc-123"
        assert info["url"] == "/api/v1/books/abc-123"
        assert info["name"] == "Batman #1"
        assert info["page_count"] == 24
        assert info["current_page"] == 24
        assert info["completed"] is True
        assert info["read_date"] == "2024-06-15T12:00:00Z"
        assert info["last_modified"] == "2024-06-15T12:30:00Z"

    def test_missing_fields_use_defaults(self):
        from models.komga import extract_book_info

        book = {}
        info = extract_book_info(book)

        assert info["id"] == ""
        assert info["url"] == ""
        assert info["name"] == ""
        assert info["page_count"] == 0
        assert info["current_page"] == 0
        assert info["completed"] is False
        assert info["read_date"] is None
        assert info["last_modified"] is None

    def test_no_read_progress(self):
        """When readProgress is None (not started), defaults apply."""
        from models.komga import extract_book_info

        book = {
            "id": "xyz",
            "url": "/api/v1/books/xyz",
            "name": "Saga #1",
            "media": {"pagesCount": 32},
            "readProgress": None,
        }
        info = extract_book_info(book)

        assert info["page_count"] == 32
        assert info["current_page"] == 0
        assert info["completed"] is False
        assert info["read_date"] is None

    def test_partial_read_progress(self):
        """readProgress exists but only has a page field."""
        from models.komga import extract_book_info

        book = {
            "id": "p1",
            "name": "Issue 5",
            "media": {"pagesCount": 20},
            "readProgress": {"page": 10},
        }
        info = extract_book_info(book)

        assert info["current_page"] == 10
        assert info["completed"] is False
        assert info["read_date"] is None
