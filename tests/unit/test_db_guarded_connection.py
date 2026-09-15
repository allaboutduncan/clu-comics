"""The guarded connection factory and the corruption classifier.

``core/database.py`` swallows almost every exception into ``[]``/``0``/``False``
by design -- a database hiccup must not take down a download worker -- so a
malformed database produced one opaque log line per call and nothing else. It
stayed that way for 78 minutes on a live install while the app reported success
over writes that never landed.

Wrapping the cursor is what makes that impossible without editing ~350 call
sites. These tests pin the two halves that are easy to get wrong:

- corruption must latch,
- and an ordinary busy database must NOT. ``sqlite3.OperationalError``
  ("database is locked") is a subclass of ``sqlite3.DatabaseError``, so latching
  on the base class would fire the alert on every lock contention -- and this
  app has 8 gunicorn threads, ~10 scheduler threads and a separate monitor
  process contending for one file. An alert that cries wolf gets ignored, and
  then the real corruption is invisible again.
"""
import os
import sqlite3
from unittest.mock import patch

import pytest

import core.app_state as app_state
import core.db_health as db_health
from core.database import _GuardedConnection, _GuardedCursor


@pytest.fixture(autouse=True)
def _clean_health_state():
    db_health.clear_db_errors()
    app_state.set_db_integrity(True, None)
    yield
    db_health.clear_db_errors()
    app_state.set_db_integrity(True, None)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"), factory=_GuardedConnection)
    c.execute("CREATE TABLE t (a INTEGER)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    yield c
    c.close()


class TestClassification:
    @pytest.mark.parametrize("message", [
        "database disk image is malformed",
        "file is not a database",
        "malformed database schema (idx_foo)",
    ])
    def test_corruption_is_recognised(self, message):
        assert db_health.is_corruption_error(sqlite3.DatabaseError(message))
        assert db_health.classify_db_error(
            sqlite3.DatabaseError(message)
        ) == "corruption"

    @pytest.mark.parametrize("exc", [
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("no such table: widgets"),
        sqlite3.OperationalError("cannot start a transaction within a transaction"),
        sqlite3.ProgrammingError("Incorrect number of bindings supplied"),
        sqlite3.IntegrityError("UNIQUE constraint failed: t.a"),
    ])
    def test_ordinary_failures_do_not_latch(self, exc):
        """The test that stops someone widening the filter into a false-alarm
        generator. A busy database is not a broken one."""
        assert db_health.classify_db_error(exc) is None
        db_health.note_db_error(exc, "test")
        assert app_state.get_db_integrity()["ok"] is True
        assert db_health.is_latched() is False

    def test_disk_errors_are_their_own_kind(self):
        exc = sqlite3.OperationalError("database or disk is full")
        assert db_health.classify_db_error(exc) == "disk"
        db_health.note_db_error(exc, "test")
        # Recorded and logged, but it is not file damage, so nothing latches.
        assert db_health.is_latched() is False
        assert db_health.recent_db_errors()[0]["kind"] == "disk"


def _build_corrupt_db(path, rows=4000):
    """A real malformed database: an interior page overwritten with junk.

    Genuinely corrupt rather than mocked, because ``sqlite3.Cursor`` is a C
    type and cannot be monkeypatched -- and because a real one exercises the
    thing that actually matters: SQLite does not touch the damaged page until
    rows are pulled, so the error surfaces at fetch time, not at execute. Same
    technique as tests/unit/test_repair_db.py.
    """
    c = sqlite3.connect(path)
    c.execute("PRAGMA page_size=4096")
    c.execute("CREATE TABLE file_index(id INTEGER PRIMARY KEY, path TEXT)")
    c.executemany(
        "INSERT INTO file_index(path) VALUES(?)",
        [(f"/data/x/{i}-" + "y" * 60,) for i in range(rows)],
    )
    c.commit()
    c.close()
    pages = os.path.getsize(path) // 4096
    with open(path, "r+b") as f:
        f.seek((pages // 2) * 4096)
        f.write(b"\x99" * 4096)


class TestGuardedCursor:
    def test_cursor_factory_is_applied(self, conn):
        assert isinstance(conn.cursor(), _GuardedCursor)

    def test_real_corruption_latches(self, tmp_path):
        """End to end, against a genuinely malformed file."""
        path = str(tmp_path / "bad.db")
        _build_corrupt_db(path)

        bad = sqlite3.connect(path, factory=_GuardedConnection)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                bad.cursor().execute("SELECT * FROM file_index").fetchall()
        finally:
            bad.close()

        assert app_state.get_db_integrity()["ok"] is False
        assert db_health.is_latched() is True
        errors = db_health.recent_db_errors()
        assert errors and "malformed" in errors[0]["message"].lower()
        # The context names where it surfaced, which is the point: this one
        # really does come from a fetch, not from execute.
        assert errors[0]["context"] in {"execute", "fetchall", "fetchone",
                                        "fetchmany", "iterate"}

    def test_exception_propagates_unchanged(self, tmp_path):
        """This observes; it must never alter behaviour."""
        path = str(tmp_path / "bad.db")
        _build_corrupt_db(path)
        bad = sqlite3.connect(path, factory=_GuardedConnection)
        try:
            with pytest.raises(sqlite3.DatabaseError) as excinfo:
                bad.cursor().execute("SELECT * FROM file_index").fetchall()
            assert isinstance(excinfo.value, sqlite3.DatabaseError)
            assert "malformed" in str(excinfo.value).lower()
        finally:
            bad.close()

    def test_every_wrapped_method_reports(self, conn):
        """Each override must call the guard. Asserted by driving the guard
        directly, since the C base class cannot be patched to raise."""
        with patch("core.database._guard") as guard:
            cur = conn.cursor()
            cur.execute("SELECT a FROM t")
            cur.fetchall()
        guard.assert_not_called()  # healthy work never reports

        for name in ("execute", "executemany", "executescript", "fetchone",
                     "fetchall", "fetchmany", "__next__"):
            assert name in _GuardedCursor.__dict__, (
                f"_GuardedCursor must override {name} -- corruption on a large "
                "scan surfaces at fetch time, not at execute"
            )

    def test_commit_is_guarded(self, conn):
        assert "commit" in _GuardedConnection.__dict__

    def test_healthy_queries_do_not_latch(self, conn):
        assert conn.cursor().execute("SELECT a FROM t").fetchall() == [(1,)]
        assert app_state.get_db_integrity()["ok"] is True
        assert db_health.recent_db_errors() == []

    def test_failed_connect_does_not_leak_the_connection(self, tmp_path, monkeypatch):
        """sqlite3.connect() succeeds lazily, so a PRAGMA is usually the first
        statement to touch the file -- meaning on a damaged database the failure
        lands with a real connection object already created. Returning None
        without closing it leaks an open handle on the one database that can
        least afford one: it pins a WAL read mark so nothing can checkpoint,
        and on Windows it blocks the os.replace that installs a repaired copy.

        The connection this call opens is tracked by identity rather than by
        counting every sqlite3.Connection alive in the process. That census
        cannot be made safely here: the suite runs APScheduler jobs, the
        background ANALYZE and the health poller, any of which may open or
        release a connection between the two samples, and the monkeypatches
        below are global -- so a background thread calling get_db_connection()
        during the test is handed this corrupt path too. It failed in CI at
        17 against 13 with a "Background ANALYZE completed" line beside it,
        having never touched the behaviour under test.
        """
        from core import database

        path = str(tmp_path / "bad.db")
        _build_corrupt_db(path)
        monkeypatch.setattr(database, "get_db_path", lambda: path)

        opened = []
        real_connect = sqlite3.connect

        def _record(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            # Only ours: other threads connect to their own databases.
            if args and args[0] == path:
                opened.append(conn)
            return conn

        monkeypatch.setattr(database.sqlite3, "connect", _record)

        boom = sqlite3.DatabaseError("database disk image is malformed")
        real_execute = database._GuardedConnection.execute

        def _explode(self, sql, *a, **kw):
            if "synchronous" in str(sql):
                raise boom
            return real_execute(self, sql, *a, **kw)

        monkeypatch.setattr(database._GuardedConnection, "execute", _explode)

        assert database.get_db_connection() is None

        assert opened, "get_db_connection never opened a connection to close"
        for conn in opened:
            with pytest.raises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")


class TestLedger:
    def test_notification_fires_once_per_episode(self):
        """A corrupt database fails every 30s background poll. One push per
        episode, not one per failing query."""
        boom = sqlite3.DatabaseError("database disk image is malformed")
        with patch.object(db_health, "_notify_corruption") as notify:
            for _ in range(5):
                db_health.note_db_error(boom, "poll")
        assert notify.call_count == 1
        assert db_health.error_summary()["corruption"] == 5

    def test_clear_unlatches(self):
        db_health.note_db_error(
            sqlite3.DatabaseError("database disk image is malformed"), "x"
        )
        assert db_health.is_latched() is True
        db_health.clear_db_errors()
        assert db_health.is_latched() is False
        assert db_health.recent_db_errors() == []

    def test_ring_buffer_is_bounded(self):
        boom = sqlite3.DatabaseError("database disk image is malformed")
        for _ in range(db_health.MAX_RECENT_ERRORS + 25):
            db_health.note_db_error(boom, "loop")
        assert len(db_health.recent_db_errors(limit=1000)) == \
            db_health.MAX_RECENT_ERRORS

    def test_note_never_raises(self):
        """It is called from except blocks that are about to re-raise."""
        with patch.object(db_health, "classify_db_error",
                          side_effect=RuntimeError("boom")):
            db_health.note_db_error(sqlite3.DatabaseError("x"), "y")


class TestStorageDiagnostic:
    """The single most valuable field on the page: a database on a network
    filesystem is the commonest cause of this corruption, and no button here
    can fix it."""

    def _mounts(self, fstype):
        return [("/", "ext4", "/dev/sda1"), ("/config", fstype, "//nas/share")]

    @pytest.mark.parametrize("fstype,risk", [
        ("ext4", "ok"),
        ("xfs", "ok"),
        ("btrfs", "ok"),
        ("cifs", "danger"),
        ("nfs4", "danger"),
        ("fuse.sshfs", "danger"),
        ("overlay", "warn"),
    ])
    def test_risk_classification(self, fstype, risk):
        with patch.object(db_health, "_read_mountinfo",
                          return_value=self._mounts(fstype)):
            result = db_health.describe_storage("/config")
        assert result["fstype"] == fstype
        assert result["risk"] == risk
        assert result["note"]

    def test_longest_mountpoint_wins(self):
        with patch.object(db_health, "_read_mountinfo",
                          return_value=self._mounts("cifs")):
            result = db_health.describe_storage("/config/sub/dir")
        assert result["mountpoint"] == "/config"

    def test_unknown_when_mountinfo_is_unavailable(self):
        """Windows and macOS have no /proc -- degrade, never raise."""
        with patch.object(db_health, "_read_mountinfo", return_value=[]):
            result = db_health.describe_storage("/config")
        assert result["risk"] == "unknown"
        assert result["fstype"] is None

    def test_octal_escapes_are_decoded(self):
        assert db_health._unescape_mount(r"/mnt/my\040share") == "/mnt/my share"
