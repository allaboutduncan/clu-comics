"""Guided Setup preflight checks (``core/setup_preflight.py``).

The checks import their dependencies inside the function, so paths are patched
at their real home (``core.config.get_*``) and each test points them at
``tmp_path``. Unwritable folders are simulated by making ``mkstemp`` raise:
``chmod`` cannot take write access away on Windows.
"""
import os
import time

import pytest

import core.setup_preflight as pf


@pytest.fixture
def dirs(tmp_path, monkeypatch, db_connection):
    """Point every path the preflight reads at a tmp tree, all writable.

    ``init_db`` seeds a default ``/data`` library when that folder exists on
    the machine running the tests, so the table is emptied first.
    """
    db_connection.execute("DELETE FROM libraries")
    db_connection.commit()
    paths = {}
    for name in ("config", "cache", "data", "watch", "target"):
        p = tmp_path / name
        p.mkdir()
        paths[name] = str(p)
    monkeypatch.setattr("core.config.get_config_dir", lambda: paths["config"])
    monkeypatch.setattr("core.config.get_cache_dir", lambda: paths["cache"])
    monkeypatch.setattr("core.config.get_data_dir", lambda: paths["data"])
    monkeypatch.setattr("core.config.get_watch_dir", lambda: paths["watch"])
    monkeypatch.setattr("core.config.get_target_dir", lambda: paths["target"])
    return paths


def _by_id(payload):
    return {c["id"]: c for c in payload["checks"]}


def _fail_mkstemp(monkeypatch, only_dir=None):
    real = pf.tempfile.mkstemp

    def fake(*args, **kwargs):
        if only_dir is None or os.path.normpath(kwargs.get("dir")) == os.path.normpath(only_dir):
            raise PermissionError(13, "Permission denied")
        return real(*args, **kwargs)

    monkeypatch.setattr(pf.tempfile, "mkstemp", fake)


class TestProbeWrite:
    def test_writable_dir_leaves_nothing_behind(self, tmp_path):
        assert pf._probe_write(str(tmp_path)) == (True, None)
        assert os.listdir(tmp_path) == []

    def test_missing_dir(self, tmp_path):
        ok, error = pf._probe_write(str(tmp_path / "nope"))
        assert ok is False and error == "does not exist"

    def test_file_is_not_a_directory(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        ok, error = pf._probe_write(str(f))
        assert ok is False and error == "is not a directory"

    def test_permission_error_is_reported(self, tmp_path, monkeypatch):
        _fail_mkstemp(monkeypatch)
        ok, error = pf._probe_write(str(tmp_path))
        assert ok is False and "Permission denied" in error


class TestWritableVolumes:
    def test_config_and_cache_ok(self, dirs):
        assert pf.check_config_writable()["status"] == "ok"
        assert pf.check_cache_writable()["status"] == "ok"

    def test_unwritable_cache_fails(self, dirs, monkeypatch):
        _fail_mkstemp(monkeypatch, only_dir=dirs["cache"])
        result = pf.check_cache_writable()
        assert result["status"] == "fail"
        assert result["blocking"] is False
        assert "Permission denied" in result["message"]
        assert pf.check_config_writable()["status"] == "ok"

    def test_missing_config_fails(self, dirs, monkeypatch, tmp_path):
        monkeypatch.setattr("core.config.get_config_dir",
                            lambda: str(tmp_path / "gone"))
        assert pf.check_config_writable()["status"] == "fail"


class TestDataMount:
    def test_empty_data_warns(self, dirs):
        result = pf.check_data_mount(libraries=[])
        assert result["status"] == "warn"
        assert result["details"]["subfolders"] == []

    def test_lists_subfolders_and_skips_hidden(self, dirs):
        for name in ("Marvel", "DC", ".snapshots", "_trash"):
            os.mkdir(os.path.join(dirs["data"], name))
        open(os.path.join(dirs["data"], "loose.cbz"), "w").close()
        result = pf.check_data_mount(libraries=[])
        assert result["status"] == "ok"
        names = [os.path.basename(p) for p in result["details"]["subfolders"]]
        assert names == ["DC", "Marvel"]

    def test_missing_data_with_library_elsewhere_is_ok(self, dirs, monkeypatch, tmp_path):
        monkeypatch.setattr("core.config.get_data_dir", lambda: str(tmp_path / "nodata"))
        result = pf.check_data_mount(libraries=[{"id": 1, "path": "/comics"}])
        assert result["status"] == "ok"

    def test_missing_data_without_libraries_warns(self, dirs, monkeypatch, tmp_path):
        monkeypatch.setattr("core.config.get_data_dir", lambda: str(tmp_path / "nodata"))
        assert pf.check_data_mount(libraries=[])["status"] == "warn"

    def test_listing_is_capped(self, dirs, monkeypatch):
        monkeypatch.setattr(pf, "MAX_LISTED_SUBFOLDERS", 2)
        for name in ("a", "b", "c"):
            os.mkdir(os.path.join(dirs["data"], name))
        details = pf.check_data_mount(libraries=[])["details"]
        assert len(details["subfolders"]) == 2
        assert details["subfolder_count"] == 3
        assert details["truncated"] is True


class TestStagingDirs:
    def test_configured_and_writable(self, dirs):
        result = pf.check_watch_dir()
        assert result["status"] == "ok"
        assert result["details"] == {"path": dirs["watch"], "configured": True}

    def test_unset_falls_back_to_default(self, dirs, monkeypatch):
        monkeypatch.setattr("core.config.get_target_dir", lambda: "")
        result = pf.check_target_dir()
        assert result["details"]["path"] == pf.DEFAULT_TARGET
        assert result["details"]["configured"] is False

    def test_missing_warns(self, dirs, monkeypatch, tmp_path):
        monkeypatch.setattr("core.config.get_watch_dir", lambda: str(tmp_path / "gone"))
        result = pf.check_watch_dir()
        assert result["status"] == "warn"
        assert "does not exist" in result["message"]

    def test_unwritable_warns(self, dirs, monkeypatch):
        _fail_mkstemp(monkeypatch, only_dir=dirs["target"])
        assert pf.check_target_dir()["status"] == "warn"

    def test_blocked_verdict_fails(self, dirs, monkeypatch):
        monkeypatch.setattr(
            "helpers.library.watch_target_verdict",
            lambda label, path: (True, f"{label} cannot be /data or a subdirectory of it."),
        )
        result = pf.check_watch_dir()
        assert result["status"] == "fail"
        assert "/data" in result["message"]

    def test_verdict_warning_is_passed_on(self, dirs, monkeypatch):
        monkeypatch.setattr(
            "helpers.library.watch_target_verdict",
            lambda label, path: (False, f"{label} is inside the library."),
        )
        result = pf.check_target_dir()
        assert result["status"] == "warn"
        assert "inside the library" in result["message"]


class TestLibraries:
    def test_healthy_library(self, tmp_path):
        result = pf.check_library({"id": 3, "name": "Comics", "path": str(tmp_path)})
        assert result["id"] == "library:3"
        assert result["status"] == "ok"

    def test_missing_library_fails(self, tmp_path):
        result = pf.check_library({"id": 3, "name": "Gone", "path": str(tmp_path / "x")})
        assert result["status"] == "fail"

    def test_read_only_library_warns(self, tmp_path, monkeypatch):
        _fail_mkstemp(monkeypatch)
        result = pf.check_library({"id": 3, "name": "RO", "path": str(tmp_path)})
        assert result["status"] == "warn"
        assert "not written" in result["message"]

    def test_one_check_per_library(self, dirs, tmp_path):
        from core.database import add_library

        a = tmp_path / "libA"
        b = tmp_path / "libB"
        a.mkdir()
        b.mkdir()
        add_library("A", str(a))
        add_library("B", str(b))
        checks = _by_id(pf.run_preflight())
        lib_ids = [k for k in checks if k.startswith("library:")]
        assert len(lib_ids) == 2
        assert "libraries" not in checks

    def test_no_libraries_is_one_warning(self, dirs):
        checks = _by_id(pf.run_preflight())
        assert checks["libraries"]["status"] == "warn"
        assert not any(k.startswith("library:") for k in checks)


class TestDatabase:
    def test_healthy(self, db_connection, monkeypatch):
        monkeypatch.setattr("core.app_state.get_db_integrity",
                            lambda: {"ok": True, "error": None, "checked_at": 0})
        monkeypatch.setattr("core.db_health.error_summary",
                            lambda: {"latched": False, "corruption": 0})
        result = pf.check_database()
        assert result["status"] == "ok"
        assert result["blocking"] is True

    def test_latched_corruption_fails(self, db_connection, monkeypatch):
        monkeypatch.setattr("core.db_health.error_summary",
                            lambda: {"latched": True, "corruption": 2})
        result = pf.check_database()
        assert result["status"] == "fail"
        assert result["blocking"] is True

    def test_failed_integrity_fails(self, db_connection, monkeypatch):
        monkeypatch.setattr("core.app_state.get_db_integrity",
                            lambda: {"ok": False, "error": "malformed", "checked_at": 1})
        monkeypatch.setattr("core.db_health.error_summary",
                            lambda: {"latched": False, "corruption": 0})
        result = pf.check_database()
        assert result["status"] == "fail"
        assert "malformed" in result["message"]

    def test_unreachable_fails(self, monkeypatch):
        monkeypatch.setattr("core.database.get_db_connection", lambda: None)
        result = pf.check_database()
        assert result["status"] == "fail"
        assert result["blocking"] is True


class TestConfigStorage:
    @pytest.mark.parametrize("risk,status", [
        ("ok", "ok"), ("warn", "warn"), ("danger", "fail"), ("unknown", "skip"),
    ])
    def test_risk_mapping(self, db_connection, monkeypatch, risk, status):
        monkeypatch.setattr(
            "core.db_health.describe_storage",
            lambda path: {"risk": risk, "note": "a note", "fstype": "x",
                          "mountpoint": "/config", "source": "s"},
        )
        result = pf.check_config_storage()
        assert result["status"] == status
        assert result["blocking"] is False


class TestOwnerAccount:
    @pytest.fixture(autouse=True)
    def _no_env_gate(self, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)

    def test_no_owner_fails(self, db_connection):
        assert pf.check_owner_account()["status"] == "fail"

    def test_placeholder_login_free_is_ok(self, db_connection):
        from core.database import seed_owner_if_needed

        seed_owner_if_needed()
        result = pf.check_owner_account()
        assert result["status"] == "ok"
        assert result["details"]["needs_setup"] is True
        assert result["details"]["login_required"] is False

    def test_placeholder_with_login_required_warns(self, db_connection, monkeypatch):
        from core.database import seed_owner_if_needed

        seed_owner_if_needed()
        monkeypatch.setenv("CLU_USERNAME", "boss")
        monkeypatch.setenv("CLU_PASSWORD", "pw")
        assert pf.check_owner_account()["status"] == "warn"

    def test_owner_with_password_is_ok(self, db_connection):
        from core.database import create_user

        create_user("boss", password="pw", role="owner")
        result = pf.check_owner_account()
        assert result["status"] == "ok"
        assert result["details"]["needs_setup"] is False


class TestRunAsUser:
    def test_skips_without_getuid(self, monkeypatch):
        monkeypatch.delattr(os, "getuid", raising=False)
        assert pf.check_run_as_user(libraries=[])["status"] == "skip"

    def test_root_warns(self, monkeypatch):
        monkeypatch.setattr(os, "getuid", lambda: 0, raising=False)
        result = pf.check_run_as_user(libraries=[])
        assert result["status"] == "warn"
        assert "root" in result["message"]

    def test_puid_mismatch_warns(self, monkeypatch):
        monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
        monkeypatch.setenv("PUID", "99")
        assert pf.check_run_as_user(libraries=[])["status"] == "warn"

    def test_library_owner_mismatch_warns(self, monkeypatch, tmp_path):
        monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
        monkeypatch.delenv("PUID", raising=False)
        real_stat = os.stat
        monkeypatch.setattr(
            pf.os, "stat",
            lambda p, *a, **k: type("S", (), {"st_uid": 0})()
            if p == str(tmp_path) else real_stat(p, *a, **k),
        )
        result = pf.check_run_as_user(libraries=[{"path": str(tmp_path)}])
        assert result["status"] == "warn"
        assert result["details"]["path_owner_uid"] == 0

    def test_matching_owner_is_ok(self, monkeypatch, tmp_path):
        monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
        monkeypatch.setenv("PUID", "1000")
        real_stat = os.stat
        monkeypatch.setattr(
            pf.os, "stat",
            lambda p, *a, **k: type("S", (), {"st_uid": 1000})()
            if p == str(tmp_path) else real_stat(p, *a, **k),
        )
        assert pf.check_run_as_user(libraries=[{"path": str(tmp_path)}])["status"] == "ok"


class TestRunner:
    def test_payload_shape(self, dirs):
        payload = pf.run_preflight()
        assert payload["success"] is True
        assert set(payload["summary"]) == set(pf.STATUSES)
        assert sum(payload["summary"].values()) == len(payload["checks"])
        for c in payload["checks"]:
            assert set(c) == {"id", "label", "status", "blocking", "message",
                              "fix", "docs", "details"}
            assert c["status"] in pf.STATUSES
            assert c["docs"].startswith(pf.DOCS_BASE)

    def test_only_database_is_blocking(self, dirs, monkeypatch):
        _fail_mkstemp(monkeypatch)  # every write probe fails
        payload = pf.run_preflight()
        assert payload["summary"]["fail"] >= 2
        assert payload["blocking"] is False
        assert [c["id"] for c in payload["checks"] if c["blocking"]] == ["database"]

    def test_database_failure_blocks(self, dirs, monkeypatch):
        monkeypatch.setattr("core.db_health.error_summary",
                            lambda: {"latched": True, "corruption": 1})
        payload = pf.run_preflight()
        assert payload["blocking"] is True

    def test_a_raising_check_fails_alone(self, dirs, monkeypatch):
        def boom():
            raise RuntimeError("kaboom")

        monkeypatch.setattr(pf, "check_cache_writable", boom)
        checks = _by_id(pf.run_preflight())
        assert checks["cache_writable"]["status"] == "fail"
        assert "kaboom" in checks["cache_writable"]["message"]
        assert checks["config_writable"]["status"] == "ok"

    def test_a_hung_check_times_out(self, dirs, monkeypatch):
        monkeypatch.setattr(pf, "CHECK_TIMEOUT_SECONDS", 0.2)
        monkeypatch.setattr(pf, "check_target_dir", lambda: time.sleep(2))
        started = time.monotonic()
        checks = _by_id(pf.run_preflight())
        assert time.monotonic() - started < 2
        assert checks["target_dir"]["status"] == "warn"
        assert "did not respond" in checks["target_dir"]["message"].lower()
