"""
Tests for /api/reading-position (routes/reading.py).

This is the endpoint the browser comic reader uses to save and restore "resume
where I left off". It previously lived in app.py, where the route test suite
could not reach it at all.
"""
import json

import pytest

from core.database import get_db_connection, get_reading_position


COMIC = "/data/Batman/Batman 001.cbz"


def _save(client, path=COMIC, page=5, total=20, time_spent=120):
    return client.post(
        "/api/reading-position",
        json={
            "comic_path": path,
            "page_number": page,
            "total_pages": total,
            "time_spent": time_spent,
        },
    )


class TestGet:
    def test_missing_path_is_400(self, client):
        r = client.get("/api/reading-position")
        assert r.status_code == 400
        assert "error" in r.get_json()

    def test_unknown_path_returns_null_page(self, client):
        r = client.get("/api/reading-position?path=/data/nope.cbz")
        assert r.status_code == 200
        assert r.get_json() == {"page_number": None}


class TestRoundTrip:
    def test_post_then_get(self, client):
        assert _save(client).status_code == 200

        r = client.get(f"/api/reading-position?path={COMIC}")
        body = r.get_json()
        assert body["page_number"] == 5
        assert body["total_pages"] == 20
        assert body["time_spent"] == 120
        assert body["updated_at"]

    def test_post_is_idempotent(self, client):
        _save(client)
        _save(client)

        conn = get_db_connection()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM reading_positions WHERE comic_path = ?",
            (COMIC,),
        ).fetchone()["n"]
        conn.close()
        assert count == 1

    def test_later_save_overwrites(self, client):
        _save(client, page=5)
        _save(client, page=12)
        assert client.get(
            f"/api/reading-position?path={COMIC}"
        ).get_json()["page_number"] == 12


class TestValidation:
    def test_missing_comic_path_is_400(self, client):
        r = client.post("/api/reading-position", json={"page_number": 4})
        assert r.status_code == 400

    def test_missing_page_number_is_400(self, client):
        r = client.post("/api/reading-position", json={"comic_path": COMIC})
        assert r.status_code == 400

    def test_non_integer_page_number_is_400(self, client):
        r = client.post(
            "/api/reading-position",
            json={"comic_path": COMIC, "page_number": "banana", "total_pages": 20},
        )
        assert r.status_code == 400
        assert "page_number" in r.get_json()["error"]

    def test_non_integer_total_pages_is_400(self, client):
        r = client.post(
            "/api/reading-position",
            json={"comic_path": COMIC, "page_number": 4, "total_pages": "many"},
        )
        assert r.status_code == 400
        assert "total_pages" in r.get_json()["error"]

    def test_numeric_string_page_number_is_accepted(self, client):
        r = client.post(
            "/api/reading-position",
            json={"comic_path": COMIC, "page_number": "7", "total_pages": "20"},
        )
        assert r.status_code == 200
        assert client.get(
            f"/api/reading-position?path={COMIC}"
        ).get_json()["page_number"] == 7

    def test_page_number_clamped_to_total_pages(self, client):
        _save(client, page=999, total=20)
        assert client.get(
            f"/api/reading-position?path={COMIC}"
        ).get_json()["page_number"] == 20

    def test_invalid_body_is_400(self, client):
        r = client.post(
            "/api/reading-position", data="not json", content_type="text/plain"
        )
        assert r.status_code == 400


class TestStartOverClearsBookmark:
    """page_number <= 1 means "at the start", which is not a bookmark.

    The reader depends on this: sendBeacon can only POST, so an unload flush
    must be able to clear a bookmark without issuing a DELETE.
    """

    @pytest.mark.parametrize("page", [0, 1])
    def test_page_at_start_deletes_row(self, client, page):
        _save(client, page=8)
        assert get_reading_position(COMIC) is not None

        r = client.post(
            "/api/reading-position",
            json={"comic_path": COMIC, "page_number": page, "total_pages": 20},
        )
        assert r.status_code == 200
        assert r.get_json() == {"success": True, "cleared": True}
        assert client.get(
            f"/api/reading-position?path={COMIC}"
        ).get_json()["page_number"] is None

    def test_clearing_a_missing_row_still_succeeds(self, client):
        r = client.post(
            "/api/reading-position",
            json={"comic_path": "/data/never-read.cbz", "page_number": 1},
        )
        assert r.status_code == 200
        assert r.get_json()["cleared"] is True


class TestBeaconTransport:
    """navigator.sendBeacon posts a Blob; some user agents drop its type and the
    body arrives as text/plain. Rejecting those would silently lose exactly the
    saves this endpoint exists to capture, so the handler must not depend on
    Content-Type. This is the contract test for the unload flush.
    """

    def test_json_body_sent_as_text_plain_is_accepted(self, client):
        payload = {
            "comic_path": COMIC,
            "page_number": 9,
            "total_pages": 20,
            "time_spent": 45,
        }
        r = client.post(
            "/api/reading-position",
            data=json.dumps(payload),
            content_type="text/plain",
        )
        assert r.status_code == 200

        body = client.get(f"/api/reading-position?path={COMIC}").get_json()
        assert body["page_number"] == 9
        assert body["time_spent"] == 45

    def test_json_body_sent_with_no_content_type_is_accepted(self, client):
        r = client.post(
            "/api/reading-position",
            data=json.dumps({"comic_path": COMIC, "page_number": 6, "total_pages": 20}),
        )
        assert r.status_code == 200
        assert client.get(
            f"/api/reading-position?path={COMIC}"
        ).get_json()["page_number"] == 6


class TestDelete:
    def test_missing_path_is_400(self, client):
        r = client.delete("/api/reading-position")
        assert r.status_code == 400

    def test_delete_removes_row(self, client):
        _save(client)
        r = client.delete(f"/api/reading-position?path={COMIC}")
        assert r.status_code == 200
        assert client.get(
            f"/api/reading-position?path={COMIC}"
        ).get_json()["page_number"] is None

    def test_delete_is_idempotent(self, client):
        r = client.delete("/api/reading-position?path=/data/never-read.cbz")
        assert r.status_code == 200


class TestPathIsStoredVerbatim:
    """comic_path is stored and matched byte-exactly, and is joined against
    file_index.path (get_continue_reading_items, _progress_map_for_paths).
    file_index stores the OS-native path verbatim, so this table must NOT
    normalise separators or case -- doing so on one side of those comparisons
    silently breaks the join. These tests pin that contract so nobody
    'helpfully' adds normalisation here again.

    Path *divergence* is prevented upstream instead: the server emits the
    indexed path (routes/collection.py) and the client uses it verbatim, and
    renames are followed by move_reading_data().
    """

    def test_path_round_trips_unmodified(self, client):
        weird = "/data/Comics/100% Marvel/Issue +1 (2020).cbz"
        _save(client, path=weird, page=4)

        conn = get_db_connection()
        stored = conn.execute(
            "SELECT comic_path FROM reading_positions"
        ).fetchone()["comic_path"]
        conn.close()
        assert stored == weird

    def test_separator_variants_are_distinct_keys(self, client):
        _save(client, path="/data/Batman/Batman 001.cbz", page=4)
        _save(client, path="\\data\\Batman\\Batman 001.cbz", page=9)

        conn = get_db_connection()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM reading_positions"
        ).fetchone()["n"]
        conn.close()
        assert count == 2

    def test_lookup_is_case_sensitive(self, client):
        _save(client, path=COMIC, page=4)
        assert client.get(
            "/api/reading-position?path=/data/batman/batman 001.cbz"
        ).get_json()["page_number"] is None
