"""
Tests for /api/reading-position (routes/reading.py).

This is the endpoint the browser comic reader uses to save and restore "resume
where I left off". It previously lived in app.py, where the route test suite
could not reach it at all.
"""
import json
from unittest.mock import patch

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
    renames are followed by move_path_references().
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


class TestBackfillReadingMetadata:
    """/api/backfill-reading-metadata: started off-request, one run at a time."""

    @pytest.fixture(autouse=True)
    def _idle(self):
        import core.reading_metadata_backfill as bf
        with bf._last_result_lock:
            bf._last_result = None
        yield
        _wait_idle()
        with bf._last_result_lock:
            bf._last_result = None

    def test_starts_a_background_run_and_reports_it(self, client, create_cbz):
        path = str(create_cbz(
            "Backfill 001.cbz",
            comicinfo_xml="<ComicInfo><Writer>Ann Nocenti</Writer></ComicInfo>"))
        conn = get_db_connection()
        conn.execute("INSERT INTO issues_read (user_id, issue_path) VALUES (1, ?)", (path,))
        conn.commit()
        conn.close()

        r = client.post("/api/backfill-reading-metadata")
        assert r.status_code == 200
        body = r.get_json()
        assert body["success"] is True and body["op_id"]
        _wait_idle()

        r = client.get("/api/backfill-reading-metadata/result")
        result = r.get_json()["result"]
        assert result["updated"] == 1 and result["skipped"] == 0

        conn = get_db_connection()
        writer = conn.execute(
            "SELECT writer FROM issues_read WHERE issue_path = ?", (path,)
        ).fetchone()[0]
        conn.close()
        assert writer == "Ann Nocenti"

    def test_second_start_while_running_is_409(self, client):
        import core.reading_metadata_backfill as bf

        assert bf._run_lock.acquire(blocking=False)
        try:
            r = client.post("/api/backfill-reading-metadata")
            assert r.status_code == 409
            assert r.get_json()["success"] is False
            assert client.get(
                "/api/backfill-reading-metadata/result").get_json()["running"] is True
        finally:
            bf._run_lock.release()

    def test_result_is_null_before_any_run(self, client):
        body = client.get("/api/backfill-reading-metadata/result").get_json()
        assert body == {"success": True, "running": False, "result": None}


def _wait_idle(timeout=10):
    import time
    import core.reading_metadata_backfill as bf

    deadline = time.time() + timeout
    while bf.is_running() and time.time() < deadline:
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# /api/mark-comic-read (moved from app.py)
# ---------------------------------------------------------------------------


def _read_row(path=COMIC):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM issues_read WHERE issue_path = ?", (path,)
    ).fetchone()
    conn.close()
    return row


class TestMarkComicRead:
    def test_missing_path_is_400(self, client):
        r = client.post("/api/mark-comic-read", json={})
        assert r.status_code == 400

    def test_records_the_read(self, client):
        r = client.post(
            "/api/mark-comic-read",
            json={"path": COMIC, "page_count": 22, "time_spent": 600},
        )
        assert r.status_code == 200
        assert r.get_json() == {"success": True}
        row = _read_row()
        assert (row["page_count"], row["time_spent"]) == (22, 600)

    def test_read_at_backfills_the_date(self, client):
        client.post(
            "/api/mark-comic-read",
            json={"path": COMIC, "read_at": "2021-03-04T05:06:07"},
        )
        assert _read_row()["read_at"].startswith("2021-03-04")

    def test_credits_come_from_comicinfo(self, client):
        info = {
            "Writer": "Tom King", "Penciller": "David Finch",
            "Characters": "Batman, Robin", "Publisher": "DC Comics", "Number": "1",
        }
        with patch("routes.reading.os.path.exists", return_value=True),              patch("core.comicinfo.read_comicinfo_from_zip", return_value=info):
            r = client.post("/api/mark-comic-read", json={"path": COMIC})
        assert r.status_code == 200
        row = _read_row()
        assert row["writer"] == "Tom King"
        assert row["penciller"] == "David Finch"
        assert row["publisher"] == "DC Comics"
        assert "Batman" in row["characters"]

    def test_unreadable_comicinfo_still_records_the_read(self, client):
        with patch("routes.reading.os.path.exists", return_value=True),              patch("core.comicinfo.read_comicinfo_from_zip", side_effect=OSError("bad zip")):
            r = client.post("/api/mark-comic-read", json={"path": COMIC})
        assert r.status_code == 200
        assert _read_row() is not None

    def test_clears_the_users_stats_cache(self, client):
        with patch("routes.reading.clear_stats_cache_prefix") as clear:
            client.post("/api/mark-comic-read", json={"path": COMIC})
        clear.assert_called_once()
        assert clear.call_args.args[0].startswith("u")

    def test_a_failed_save_is_a_500(self, client):
        with patch("routes.reading.mark_issue_read", side_effect=RuntimeError("locked")):
            r = client.post("/api/mark-comic-read", json={"path": COMIC})
        assert r.status_code == 500
        assert r.get_json() == {"error": "locked"}

    def test_a_failed_metron_scrobble_does_not_fail_the_read(self, client):
        with patch("models.metron.get_flask_api", side_effect=RuntimeError("down")):
            r = client.post("/api/mark-comic-read", json={"path": COMIC})
        assert r.status_code == 200
        assert _read_row() is not None


# ---------------------------------------------------------------------------
# Reading sessions recorded from the reader's own saves
# ---------------------------------------------------------------------------

SESSION = "3f2b6c1e-8a44-4d0b-9a57-0c1f5f1f0a11"


def _sessions():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM reading_sessions ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


class TestSessionsFromSaves:
    def _save(self, client, page, session=SESSION):
        return client.post("/api/reading-position", json={
            "comic_path": COMIC, "page_number": page, "total_pages": 20,
            "time_spent": 60, "session_id": session,
        })

    def test_saves_in_one_reader_open_are_one_session(self, client):
        self._save(client, 3)
        self._save(client, 7)
        (session,) = _sessions()
        assert session["comic_path"] == COMIC
        assert session["client_session_id"] == SESSION
        assert (session["start_page"], session["max_page"]) == (3, 7)

    def test_page_one_is_not_a_sitting(self, client):
        # Page 1 clears the bookmark; opening to the cover is not reading.
        assert self._save(client, 1).get_json()["cleared"] is True
        assert _sessions() == []

    def test_a_save_without_an_id_still_records(self, client):
        # An older cached reader.js sends no session id.
        self._save(client, 4, session=None)
        (session,) = _sessions()
        assert session["client_session_id"] is None

    def test_a_session_failure_does_not_fail_the_save(self, client):
        with patch("core.reading_sessions.record_reading_activity",
                   side_effect=RuntimeError("database is locked")):
            r = self._save(client, 5)
        assert r.get_json() == {"success": True}
        assert get_reading_position(COMIC)["page_number"] == 5

    def test_finishing_in_the_reader_closes_the_session(self, client):
        self._save(client, 3)
        client.post("/api/mark-comic-read", json={
            "path": COMIC, "page_count": 20, "time_spent": 600, "session_id": SESSION,
        })
        (session,) = _sessions()
        assert session["finished"] == 1
        assert session["max_page"] == 20

    def test_mark_as_read_from_the_grid_is_not_a_sitting(self, client):
        client.post("/api/mark-comic-read", json={"path": COMIC})
        assert _read_row() is not None
        assert _sessions() == []

