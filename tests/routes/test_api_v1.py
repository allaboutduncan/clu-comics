"""Tests for routes/api_v1.py -- the /api/v1/* JSON API for the offline client."""
import os
import json
import pytest

from core.database import (
    get_api_token,
    rotate_api_token,
    set_user_preference,
    get_db_connection,
    save_reading_position,
    mark_issue_read,
)


TOKEN = "test-token-abc123"


@pytest.fixture
def with_token(db_connection):
    """Pre-set a known API token in user_preferences."""
    set_user_preference("api_token", TOKEN, category="security")
    return TOKEN


@pytest.fixture
def auth_headers(with_token):
    return {"Authorization": f"Bearer {with_token}"}


@pytest.fixture
def seeded_file(db_connection, create_cbz):
    """Create a real CBZ on disk and a matching file_index row."""
    cbz_path = create_cbz("Batman 001 (2020).cbz", num_images=4)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO file_index
        (name, path, type, size, parent, has_thumbnail, modified_at,
         ci_title, ci_series, ci_number, ci_year, ci_publisher, has_comicinfo)
        VALUES (?, ?, 'file', ?, ?, 0, ?, 'Origin', 'Batman', '1', '2020',
                'DC Comics', 1)
        """,
        (
            os.path.basename(cbz_path),
            cbz_path,
            os.path.getsize(cbz_path),
            os.path.dirname(cbz_path),
            os.path.getmtime(cbz_path),
        ),
    )
    conn.commit()
    file_id = c.lastrowid
    conn.close()
    return {"id": file_id, "path": cbz_path}


# =============================================================================
# Auth
# =============================================================================


class TestAuth:

    def test_no_token_set_returns_503(self, db_connection, client):
        # No api_token row in user_preferences
        resp = client.get("/api/v1/auth/ping")
        assert resp.status_code == 503
        body = resp.get_json()
        assert body["error"] == "api_disabled"

    def test_token_set_no_header_returns_401(self, with_token, client):
        resp = client.get("/api/v1/auth/ping")
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "unauthorized"

    def test_token_set_wrong_header_returns_401(self, with_token, client):
        resp = client.get(
            "/api/v1/auth/ping",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_token_set_no_bearer_prefix_returns_401(self, with_token, client):
        resp = client.get(
            "/api/v1/auth/ping",
            headers={"Authorization": with_token},
        )
        assert resp.status_code == 401

    def test_token_set_correct_header_returns_200(self, auth_headers, client):
        resp = client.get("/api/v1/auth/ping", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert "version" in body


# =============================================================================
# Token helpers
# =============================================================================


class TestTokenHelpers:

    def test_rotate_generates_distinct_tokens(self, db_connection):
        t1 = rotate_api_token()
        t2 = rotate_api_token()
        assert t1 and t2
        assert t1 != t2
        # Latest one wins
        assert get_api_token() == t2

    def test_get_api_token_none_when_unset(self, db_connection):
        assert get_api_token() is None


# =============================================================================
# Library browsing
# =============================================================================


class TestLibrary:

    def test_publishers_empty(self, auth_headers, client):
        resp = client.get("/api/v1/library/publishers", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert "items" in body
        assert "total" in body
        assert body["page"] == 1

    def test_series_missing_filter_ok(self, auth_headers, client):
        # No publisher filter is valid; just returns all series.
        resp = client.get("/api/v1/library/series", headers=auth_headers)
        assert resp.status_code == 200
        assert "items" in resp.get_json()

    def test_issues_requires_series(self, auth_headers, client):
        resp = client.get("/api/v1/library/issues", headers=auth_headers)
        assert resp.status_code == 400
        assert "series" in resp.get_json()["error"].lower()

    def test_issues_with_series_returns_progress_metadata(
        self, auth_headers, seeded_file, client
    ):
        # Save progress for our seeded issue first
        save_reading_position(seeded_file["path"], page_number=2, total_pages=4)

        resp = client.get(
            "/api/v1/library/issues?series=Batman", headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["total"] >= 1
        match = next(
            (i for i in body["items"] if i["path"] == seeded_file["path"]),
            None,
        )
        assert match is not None
        assert match["has_progress"] is True
        assert match["last_page"] == 2
        assert match["id"] == seeded_file["id"]


# =============================================================================
# Issue detail / cover / download
# =============================================================================


class TestIssueDetail:

    def test_issue_not_found(self, auth_headers, client):
        resp = client.get("/api/v1/issue/99999", headers=auth_headers)
        assert resp.status_code == 404

    def test_issue_metadata_round_trip(self, auth_headers, seeded_file, client):
        resp = client.get(
            f"/api/v1/issue/{seeded_file['id']}", headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["id"] == seeded_file["id"]
        assert body["path"] == seeded_file["path"]
        assert body["metadata"]["series"] == "Batman"
        assert body["metadata"]["publisher"] == "DC Comics"
        assert body["progress"] is None

    def test_issue_metadata_includes_progress(
        self, auth_headers, seeded_file, client
    ):
        save_reading_position(seeded_file["path"], page_number=3, total_pages=4)
        resp = client.get(
            f"/api/v1/issue/{seeded_file['id']}", headers=auth_headers
        )
        body = resp.get_json()
        assert body["progress"]["page_number"] == 3
        assert body["progress"]["total_pages"] == 4

    def test_cover_returns_jpeg(self, auth_headers, seeded_file, client):
        resp = client.get(
            f"/api/v1/issue/{seeded_file['id']}/cover", headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.mimetype == "image/jpeg"
        assert len(resp.data) > 0

    def test_cover_404_for_unknown_id(self, auth_headers, client):
        resp = client.get("/api/v1/issue/99999/cover", headers=auth_headers)
        assert resp.status_code == 404


# =============================================================================
# Download with Range support
# =============================================================================


class TestDownload:

    def test_download_full_file(self, auth_headers, seeded_file, client):
        resp = client.get(
            f"/api/v1/issue/{seeded_file['id']}/download", headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.headers.get("Accept-Ranges") == "bytes"
        on_disk = os.path.getsize(seeded_file["path"])
        assert len(resp.data) == on_disk

    def test_download_range_returns_206(self, auth_headers, seeded_file, client):
        resp = client.get(
            f"/api/v1/issue/{seeded_file['id']}/download",
            headers={**auth_headers, "Range": "bytes=0-15"},
        )
        assert resp.status_code == 206
        assert resp.headers.get("Content-Range", "").startswith("bytes 0-15/")
        assert len(resp.data) == 16

    def test_download_unsatisfiable_range_returns_416(
        self, auth_headers, seeded_file, client
    ):
        size = os.path.getsize(seeded_file["path"])
        resp = client.get(
            f"/api/v1/issue/{seeded_file['id']}/download",
            headers={**auth_headers, "Range": f"bytes={size + 100}-"},
        )
        assert resp.status_code == 416
        assert resp.headers.get("Content-Range") == f"bytes */{size}"


# =============================================================================
# Reading-progress endpoints
# =============================================================================


class TestProgress:

    def test_get_progress_missing_param(self, auth_headers, client):
        resp = client.get("/api/v1/progress", headers=auth_headers)
        assert resp.status_code == 400

    def test_get_progress_unknown_path_returns_null(self, auth_headers, client):
        resp = client.get(
            "/api/v1/progress?path=/data/nonexistent.cbz", headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.get_json() is None

    def test_put_progress_round_trip(self, auth_headers, seeded_file, client):
        body = {
            "path": seeded_file["path"],
            "page_number": 5,
            "total_pages": 10,
            "time_spent": 120,
        }
        resp = client.put(
            "/api/v1/progress",
            data=json.dumps(body),
            content_type="application/json",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        saved = resp.get_json()
        assert saved["page_number"] == 5
        assert saved["total_pages"] == 10

        # Round-trip via GET
        resp2 = client.get(
            f"/api/v1/progress?path={seeded_file['path']}", headers=auth_headers
        )
        assert resp2.status_code == 200
        assert resp2.get_json()["page_number"] == 5

    def test_put_progress_missing_fields(self, auth_headers, client):
        resp = client.put(
            "/api/v1/progress",
            data=json.dumps({"path": "/data/x.cbz"}),
            content_type="application/json",
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_progress_since_filters_correctly(
        self, auth_headers, seeded_file, client
    ):
        save_reading_position(seeded_file["path"], page_number=1, total_pages=4)

        resp = client.get("/api/v1/progress/since?ts=0", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["count"] >= 1
        assert any(
            i["comic_path"] == seeded_file["path"] for i in body["items"]
        )

        # Future ts → nothing
        future = 9999999999
        resp2 = client.get(
            f"/api/v1/progress/since?ts={future}", headers=auth_headers
        )
        assert resp2.get_json()["count"] == 0


# =============================================================================
# Mark-as-read
# =============================================================================


class TestIssuesRead:

    def test_post_marks_issue_read(self, auth_headers, seeded_file, client):
        resp = client.post(
            "/api/v1/issues/read",
            data=json.dumps({
                "path": seeded_file["path"],
                "page_count": 4,
                "time_spent": 300,
            }),
            content_type="application/json",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "SELECT page_count, time_spent FROM issues_read WHERE issue_path = ?",
            (seeded_file["path"],),
        )
        row = c.fetchone()
        conn.close()
        assert row is not None
        assert row["page_count"] == 4
        assert row["time_spent"] == 300

    def test_post_missing_path(self, auth_headers, client):
        resp = client.post(
            "/api/v1/issues/read",
            data=json.dumps({"page_count": 4}),
            content_type="application/json",
            headers=auth_headers,
        )
        assert resp.status_code == 400
