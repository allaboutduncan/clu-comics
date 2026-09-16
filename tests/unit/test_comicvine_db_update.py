"""Unit tests for core.comicvine_db_update.

The expensive half of this feature is a 541 MB download onto a path the user
cares about, so the rules that matter are the ones about *not* doing it: not
when the token has not moved, not when nobody opted in, and above all not
leaving a stamp behind when it failed -- a stamp written early makes the next
sweep skip a database that was never installed.
"""

import hashlib
import io
import os
import zipfile
from unittest.mock import patch

import pytest

from core import comicvine_db_update as mod


@pytest.fixture(autouse=True)
def isolated_prefs(monkeypatch):
    """Back the preference accessors with a dict, not the real database."""
    store = {}

    def _get(key, default=None):
        return store.get(key, default)

    def _set(key, value):
        store[key] = value

    monkeypatch.setattr(mod, "_get_pref", _get)
    monkeypatch.setattr(mod, "_set_pref", _set)
    return store


def _make_zip(db_bytes, name="localcv.db"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, db_bytes)
    return buf.getvalue()


def _real_db_bytes():
    """A tiny but genuine SQLite file with the tables the provider needs."""
    import sqlite3
    import tempfile

    path = os.path.join(tempfile.mkdtemp(), "seed.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cv_volume (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE cv_issue (id INTEGER PRIMARY KEY, volume_id INTEGER)")
    conn.execute("INSERT INTO cv_volume (id, name) VALUES (1, 'Batman')")
    conn.execute("INSERT INTO cv_issue (id, volume_id) VALUES (1, 1)")
    conn.commit()
    conn.close()
    with open(path, "rb") as fh:
        return fh.read()


class _FakeResponse:
    def __init__(self, text="", status_code=200, content=b"", headers=None):
        self.text = text
        self.status_code = status_code
        self._content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

class TestProbe:
    def test_parses_md5sum_output(self):
        line = "949443012718f0ac5f3aa83ca933f6c2  /home/x/localcv.db\n"
        assert mod._parse_checksum(line) == "949443012718f0ac5f3aa83ca933f6c2"

    def test_parse_rejects_junk(self):
        assert mod._parse_checksum("") is None
        assert mod._parse_checksum("not a checksum") is None
        assert mod._parse_checksum("deadbeef") is None  # too short

    def test_returns_token_on_success(self):
        token = "a" * 32
        with patch.object(mod.requests, "get",
                          return_value=_FakeResponse(text=f"{token}  x.db")):
            assert mod.probe() == (token, None)

    def test_failure_returns_none_token_and_a_message(self):
        """A failed probe must never look like 'nothing changed'.

        Collapsing the two would make every outage prove the database is
        current, and the sweep would skip it forever.
        """
        with patch.object(mod.requests, "get", side_effect=OSError("no route")):
            token, error = mod.probe()
        assert token is None
        assert error and "no route" in error

    def test_unreadable_body_is_a_failure_not_an_empty_token(self):
        with patch.object(mod.requests, "get",
                          return_value=_FakeResponse(text="<html>oops</html>")):
            token, error = mod.probe()
        assert token is None
        assert error


# ---------------------------------------------------------------------------
# needs_update
# ---------------------------------------------------------------------------

class TestNeedsUpdate:
    def test_false_when_the_token_matches_and_the_file_is_there(
        self, isolated_prefs, tmp_path, monkeypatch
    ):
        db = tmp_path / "cv.db"
        db.write_bytes(b"x")
        monkeypatch.setattr(mod, "get_database_path", lambda: str(db))
        isolated_prefs[mod.PREF_DB_TOKEN] = "a" * 32
        assert mod.needs_update("a" * 32) is False

    def test_true_when_the_token_moved(self, isolated_prefs, tmp_path, monkeypatch):
        db = tmp_path / "cv.db"
        db.write_bytes(b"x")
        monkeypatch.setattr(mod, "get_database_path", lambda: str(db))
        isolated_prefs[mod.PREF_DB_TOKEN] = "a" * 32
        assert mod.needs_update("b" * 32) is True

    def test_true_when_the_file_is_missing(self, isolated_prefs, tmp_path, monkeypatch):
        """A matching token over a missing file would never dislodge itself."""
        monkeypatch.setattr(mod, "get_database_path",
                            lambda: str(tmp_path / "gone.db"))
        isolated_prefs[mod.PREF_DB_TOKEN] = "a" * 32
        assert mod.needs_update("a" * 32) is True

    def test_force_overrides_a_matching_token(self, isolated_prefs, tmp_path, monkeypatch):
        db = tmp_path / "cv.db"
        db.write_bytes(b"x")
        monkeypatch.setattr(mod, "get_database_path", lambda: str(db))
        isolated_prefs[mod.PREF_DB_TOKEN] = "a" * 32
        assert mod.needs_update("a" * 32, force=True) is True


# ---------------------------------------------------------------------------
# The 2-week gate and the opt-in
# ---------------------------------------------------------------------------

class TestScheduledGate:
    def test_off_by_default(self, isolated_prefs):
        assert mod.is_auto_update_enabled() is False

    def test_a_truthy_non_bool_does_not_enable_it(self, isolated_prefs):
        isolated_prefs[mod.PREF_AUTO_UPDATE] = "no"
        assert mod.is_auto_update_enabled() is False

    def test_scheduled_run_does_nothing_when_disabled(self, isolated_prefs):
        with patch.object(mod, "run_update") as run:
            mod.run_scheduled_update()
        run.assert_not_called()

    def test_scheduled_run_does_nothing_without_a_path(self, isolated_prefs, monkeypatch):
        isolated_prefs[mod.PREF_AUTO_UPDATE] = True
        monkeypatch.setattr(mod, "get_database_path", lambda: None)
        with patch.object(mod, "run_update") as run:
            mod.run_scheduled_update()
        run.assert_not_called()

    def test_scheduled_run_waits_out_the_window(self, isolated_prefs, monkeypatch):
        from datetime import datetime, timedelta, timezone

        isolated_prefs[mod.PREF_AUTO_UPDATE] = True
        isolated_prefs[mod.PREF_LAST_CHECK] = (
            datetime.now(timezone.utc) - timedelta(days=3)
        ).isoformat()
        monkeypatch.setattr(mod, "get_database_path", lambda: "/tmp/cv.db")
        with patch.object(mod, "run_update") as run:
            mod.run_scheduled_update()
        run.assert_not_called()

    def test_scheduled_run_fires_once_the_window_passes(self, isolated_prefs, monkeypatch):
        from datetime import datetime, timedelta, timezone

        isolated_prefs[mod.PREF_AUTO_UPDATE] = True
        isolated_prefs[mod.PREF_LAST_CHECK] = (
            datetime.now(timezone.utc) - timedelta(days=15)
        ).isoformat()
        monkeypatch.setattr(mod, "get_database_path", lambda: "/tmp/cv.db")
        with patch.object(mod, "run_update",
                          return_value={"success": True, "updated": False}) as run:
            mod.run_scheduled_update()
        run.assert_called_once()

    def test_first_ever_run_is_not_blocked(self, isolated_prefs, monkeypatch):
        isolated_prefs[mod.PREF_AUTO_UPDATE] = True
        monkeypatch.setattr(mod, "get_database_path", lambda: "/tmp/cv.db")
        with patch.object(mod, "run_update",
                          return_value={"success": True, "updated": False}) as run:
            mod.run_scheduled_update()
        run.assert_called_once()

    def test_scheduled_run_never_raises(self, isolated_prefs, monkeypatch):
        """It runs on an APScheduler thread, where an exception is swallowed."""
        isolated_prefs[mod.PREF_AUTO_UPDATE] = True
        monkeypatch.setattr(mod, "get_database_path", lambda: "/tmp/cv.db")
        with patch.object(mod, "run_update", side_effect=RuntimeError("boom")):
            mod.run_scheduled_update()  # must not propagate


# ---------------------------------------------------------------------------
# run_update
# ---------------------------------------------------------------------------

class TestRunUpdate:
    def test_refuses_without_a_configured_path(self, isolated_prefs, monkeypatch):
        monkeypatch.setattr(mod, "get_database_path", lambda: None)
        result = mod.run_update()
        assert result["success"] is False
        assert "path" in result["error"].lower()

    def test_a_second_concurrent_run_stands_down(self, isolated_prefs):
        mod._run_lock.acquire()
        try:
            result = mod.run_update()
        finally:
            mod._run_lock.release()
        assert result["success"] is False
        assert "already running" in result["error"]

    def test_no_op_when_the_token_has_not_moved(self, isolated_prefs, tmp_path, monkeypatch):
        db = tmp_path / "cv.db"
        db.write_bytes(b"original")
        token = "c" * 32
        isolated_prefs[mod.PREF_DB_TOKEN] = token
        monkeypatch.setattr(mod, "get_database_path", lambda: str(db))
        monkeypatch.setattr(mod, "probe", lambda: (token, None))

        with patch.object(mod, "_download_zip") as dl:
            result = mod.run_update()

        dl.assert_not_called()
        assert result["success"] is True
        assert result["updated"] is False
        assert db.read_bytes() == b"original"

    def test_probe_failure_leaves_everything_alone(self, isolated_prefs, tmp_path, monkeypatch):
        db = tmp_path / "cv.db"
        db.write_bytes(b"original")
        isolated_prefs[mod.PREF_DB_TOKEN] = "c" * 32
        monkeypatch.setattr(mod, "get_database_path", lambda: str(db))
        monkeypatch.setattr(mod, "probe", lambda: (None, "server down"))

        result = mod.run_update()

        assert result["success"] is False
        assert db.read_bytes() == b"original"
        assert isolated_prefs[mod.PREF_DB_TOKEN] == "c" * 32
        assert mod.PREF_LAST_UPDATE not in isolated_prefs

    def test_checksum_mismatch_leaves_the_original_intact(
        self, isolated_prefs, tmp_path, monkeypatch
    ):
        db = tmp_path / "cv.db"
        db.write_bytes(b"original")
        monkeypatch.setattr(mod, "get_database_path", lambda: str(db))
        monkeypatch.setattr(mod, "probe", lambda: ("d" * 32, None))

        payload = _make_zip(_real_db_bytes())

        def _fake_download(dest, say):
            with open(dest, "wb") as fh:
                fh.write(payload)
            return len(payload)

        monkeypatch.setattr(mod, "_download_zip", _fake_download)

        result = mod.run_update()

        assert result["success"] is False
        assert "checksum" in result["error"].lower()
        assert db.read_bytes() == b"original"
        # Nothing is stamped on failure: a token written here would make the
        # next sweep skip a database that was never installed.
        assert mod.PREF_DB_TOKEN not in isolated_prefs
        assert mod.PREF_LAST_UPDATE not in isolated_prefs

    def test_archive_without_a_db_member_fails_cleanly(
        self, isolated_prefs, tmp_path, monkeypatch
    ):
        db = tmp_path / "cv.db"
        db.write_bytes(b"original")
        monkeypatch.setattr(mod, "get_database_path", lambda: str(db))
        monkeypatch.setattr(mod, "probe", lambda: ("d" * 32, None))

        payload = _make_zip(b"nope", name="readme.txt")
        monkeypatch.setattr(
            mod, "_download_zip",
            lambda dest, say: (open(dest, "wb").write(payload), len(payload))[1],
        )

        result = mod.run_update()

        assert result["success"] is False
        assert "database file" in result["error"]
        assert db.read_bytes() == b"original"

    def test_a_file_that_is_not_a_database_is_refused(
        self, isolated_prefs, tmp_path, monkeypatch
    ):
        """The MD5 proves the bytes arrived; it cannot prove they are the
        database this provider expects."""
        db = tmp_path / "cv.db"
        db.write_bytes(b"original")
        junk = b"I am not a SQLite file" * 10
        digest = hashlib.md5(junk, usedforsecurity=False).hexdigest()

        monkeypatch.setattr(mod, "get_database_path", lambda: str(db))
        monkeypatch.setattr(mod, "probe", lambda: (digest, None))

        payload = _make_zip(junk)
        monkeypatch.setattr(
            mod, "_download_zip",
            lambda dest, say: (open(dest, "wb").write(payload), len(payload))[1],
        )

        result = mod.run_update()

        assert result["success"] is False
        assert db.read_bytes() == b"original"

    def test_successful_update_installs_and_stamps(
        self, isolated_prefs, tmp_path, monkeypatch
    ):
        db = tmp_path / "cv.db"
        db.write_bytes(b"original")
        fresh = _real_db_bytes()
        digest = hashlib.md5(fresh, usedforsecurity=False).hexdigest()

        monkeypatch.setattr(mod, "get_database_path", lambda: str(db))
        monkeypatch.setattr(mod, "probe", lambda: (digest, None))

        payload = _make_zip(fresh)
        monkeypatch.setattr(
            mod, "_download_zip",
            lambda dest, say: (open(dest, "wb").write(payload), len(payload))[1],
        )

        result = mod.run_update()

        assert result["success"] is True, result.get("error")
        assert result["updated"] is True
        assert db.read_bytes() == fresh
        assert isolated_prefs[mod.PREF_DB_TOKEN] == digest
        assert isolated_prefs[mod.PREF_LAST_UPDATE]
        assert isolated_prefs[mod.PREF_LAST_ERROR] is None

    def test_bootstraps_when_no_file_exists_yet(
        self, isolated_prefs, tmp_path, monkeypatch
    ):
        db = tmp_path / "cv.db"  # deliberately not created
        fresh = _real_db_bytes()
        digest = hashlib.md5(fresh, usedforsecurity=False).hexdigest()

        monkeypatch.setattr(mod, "get_database_path", lambda: str(db))
        monkeypatch.setattr(mod, "probe", lambda: (digest, None))

        payload = _make_zip(fresh)
        monkeypatch.setattr(
            mod, "_download_zip",
            lambda dest, say: (open(dest, "wb").write(payload), len(payload))[1],
        )

        result = mod.run_update()

        assert result["success"] is True, result.get("error")
        assert db.read_bytes() == fresh


class TestStatus:
    def test_status_never_carries_the_source_url(self, isolated_prefs, monkeypatch):
        monkeypatch.setattr(mod, "get_database_path", lambda: None)
        assert "nerdfire" not in repr(mod.get_status())

    def test_status_reports_a_missing_file(self, isolated_prefs, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "get_database_path",
                            lambda: str(tmp_path / "gone.db"))
        status = mod.get_status()
        assert status["path_configured"] is True
        assert status["database_present"] is False
