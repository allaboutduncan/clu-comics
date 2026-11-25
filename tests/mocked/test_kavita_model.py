"""models/kavita.py: the Kavita client and the chapter normaliser."""
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from models.kavita import (
    FORMAT_EPUB,
    KavitaAuthError,
    KavitaClient,
    extract_chapter_info,
    iter_chapters,
    normalize_timestamp,
)


def _client():
    with patch("models.kavita.requests.Session") as session_cls:
        session = MagicMock()
        session.headers = {}
        session_cls.return_value = session
        client = KavitaClient("http://kavita.local:5000/", "the-key")
    return client, session


def _response(status=200, payload=None, total_pages=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload if payload is not None else []
    resp.headers = {}
    if total_pages is not None:
        resp.headers["Pagination"] = json.dumps({"currentPage": 1, "totalPages": total_pages})
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    return resp


def _series(series_id, pages_read=5):
    return {"id": series_id, "pagesRead": pages_read, "pages": 20}


class TestClient:
    def test_the_key_goes_in_the_api_key_header(self):
        client, session = _client()
        assert session.headers["x-api-key"] == "the-key"
        assert client.base_url == "http://kavita.local:5000"

    def test_connection_reports_the_kavita_username(self):
        client, session = _client()
        session.get.return_value = _response(payload={"username": "kavita-alice"})
        assert client.test_connection() == (True, "Connected successfully", "kavita-alice")
        assert session.get.call_args[0][0] == "http://kavita.local:5000/api/Account"

    @pytest.mark.parametrize("status,fragment", [
        (401, "rejected the auth key"),
        (403, "forbidden"),
        (500, "HTTP 500"),
    ])
    def test_connection_failures_are_explained(self, status, fragment):
        client, session = _client()
        session.get.return_value = _response(status=status)
        ok, details, username = client.test_connection()
        assert ok is False and username is None
        assert fragment in details

    @pytest.mark.parametrize("error", [
        requests.ConnectionError("refused"), requests.Timeout("slow"),
    ])
    def test_connection_never_raises(self, error):
        client, session = _client()
        session.get.side_effect = error
        ok, details, _username = client.test_connection()
        assert ok is False and details

    def test_the_series_query_filters_on_read_progress(self):
        client, session = _client()
        session.post.return_value = _response(payload=[_series(1)], total_pages=1)

        assert [s["id"] for s in client.iter_series_with_progress()] == [1]

        call = session.post.call_args
        assert call[0][0] == "http://kavita.local:5000/api/Series/v2"
        assert call[1]["params"] == {"PageNumber": 1, "PageSize": 200}
        statement = call[1]["json"]["statements"][0]
        # SeriesFilterField.ReadProgress > 0
        assert (statement["field"], statement["comparison"], statement["value"]) == (20, 1, "0")

    def test_paging_follows_the_pagination_header(self):
        client, session = _client()
        session.post.side_effect = [
            _response(payload=[_series(1)], total_pages=3),
            _response(payload=[_series(2)], total_pages=3),
            _response(payload=[_series(3)], total_pages=3),
        ]
        assert [s["id"] for s in client.iter_series_with_progress(page_size=1)] == [1, 2, 3]
        assert [c[1]["params"]["PageNumber"] for c in session.post.call_args_list] == [1, 2, 3]

    def test_without_a_header_a_short_page_ends_the_listing(self):
        client, session = _client()
        session.post.side_effect = [
            _response(payload=[_series(1), _series(2)]),
            _response(payload=[_series(3)]),
        ]
        assert [s["id"] for s in client.iter_series_with_progress(page_size=2)] == [1, 2, 3]
        assert session.post.call_count == 2

    def test_a_server_that_ignores_paging_does_not_loop(self):
        client, session = _client()
        session.post.return_value = _response(payload=[_series(1), _series(2)])
        assert [s["id"] for s in client.iter_series_with_progress(page_size=2)] == [1, 2]
        assert session.post.call_count == 2

    def test_series_with_nothing_read_are_dropped(self):
        """A server that ignores the filter must not cost a request per series."""
        client, session = _client()
        session.post.return_value = _response(
            payload=[_series(1, pages_read=0), _series(2)], total_pages=1)
        assert [s["id"] for s in client.iter_series_with_progress()] == [2]

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_rejected_key_raises_auth_error(self, status):
        client, session = _client()
        session.post.return_value = _response(status=status)
        with pytest.raises(KavitaAuthError):
            list(client.iter_series_with_progress())
        session.get.return_value = _response(status=status)
        with pytest.raises(KavitaAuthError):
            client.get_volumes(1)

    def test_other_http_errors_raise_request_errors(self):
        client, session = _client()
        session.get.return_value = _response(status=500)
        with pytest.raises(requests.HTTPError):
            client.get_volumes(1)

    def test_volumes_are_fetched_by_series(self):
        client, session = _client()
        session.get.return_value = _response(payload=[{"id": 9, "chapters": []}])
        assert client.get_volumes(7) == [{"id": 9, "chapters": []}]
        assert session.get.call_args[1]["params"] == {"seriesId": 7}


def _chapter(**overrides):
    chapter = {
        "id": 42,
        "pages": 20,
        "pagesRead": 7,
        "lastReadingProgressUtc": "2025-03-04T05:06:07.1234567",
        "files": [{"filePath": "/kavita/DC/Batman/Batman 001.cbz", "format": 1, "pages": 20}],
    }
    chapter.update(overrides)
    return chapter


class TestExtractChapterInfo:
    def test_an_in_progress_chapter(self):
        info = extract_chapter_info(_chapter())
        assert info == {
            "chapter_id": 42,
            "file_path": "/kavita/DC/Batman/Batman 001.cbz",
            "file_name": "Batman 001.cbz",
            "page_count": 20,
            "pages_read": 7,
            "completed": False,
            # Kavita's 0-based page index 7 is the eighth page.
            "current_page": 8,
            "read_date": "2025-03-04 05:06:07",
        }

    def test_a_finished_chapter(self):
        info = extract_chapter_info(_chapter(pagesRead=20))
        assert info["completed"] is True
        assert info["current_page"] == 20

    def test_a_windows_server_path_keeps_its_filename(self):
        info = extract_chapter_info(_chapter(
            files=[{"filePath": "D:\\Comics\\Batman\\Batman 001.cbz", "format": 1}]))
        assert info["file_name"] == "Batman 001.cbz"

    def test_page_count_falls_back_to_the_file(self):
        info = extract_chapter_info(_chapter(
            pages=0, files=[{"filePath": "/k/a.cbz", "format": 1, "pages": 30}]))
        assert info["page_count"] == 30

    def test_no_page_count_is_never_completed(self):
        info = extract_chapter_info(_chapter(
            pages=0, pagesRead=5, files=[{"filePath": "/k/a.cbz", "format": 1}]))
        assert info["completed"] is False

    @pytest.mark.parametrize("files", [
        [],
        None,
        [{"filePath": "/k/a.cbz", "format": 1}, {"filePath": "/k/b.cbz", "format": 1}],
        [{"filePath": "", "format": 1}],
        [{"filePath": "/k/a.epub", "format": FORMAT_EPUB}],
    ])
    def test_chapters_that_are_not_one_comic_file_are_refused(self, files):
        assert extract_chapter_info(_chapter(files=files)) is None


class TestHelpers:
    @pytest.mark.parametrize("value,expected", [
        ("2025-03-04T05:06:07.1234567", "2025-03-04 05:06:07"),
        ("2025-03-04T05:06:07Z", "2025-03-04 05:06:07"),
        ("2025-03-04 05:06:07", "2025-03-04 05:06:07"),
        # Kavita's "never".
        ("0001-01-01T00:00:00", None),
        ("", None),
        (None, None),
        ("not a date", None),
    ])
    def test_normalize_timestamp(self, value, expected):
        assert normalize_timestamp(value) == expected

    def test_iter_chapters_flattens_volumes(self):
        volumes = [{"chapters": [{"id": 1}, {"id": 2}]}, {"chapters": None}, {"chapters": [{"id": 3}]}]
        assert [c["id"] for c in iter_chapters(volumes)] == [1, 2, 3]
        assert list(iter_chapters(None)) == []
