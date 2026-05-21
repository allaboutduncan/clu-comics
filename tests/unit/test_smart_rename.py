"""Tests for cbz_ops/smart_rename.py -- metadata-driven bulk rename."""
import json
import os
from types import SimpleNamespace
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def _mock_rename_deps():
    """Stub the few module-level deps so importing smart_rename is cheap."""
    # Force-import so patch() can resolve the target.
    import cbz_ops.rename  # noqa: F401
    import cbz_ops.smart_rename  # noqa: F401
    with patch("cbz_ops.smart_rename.app_logger"), \
         patch("cbz_ops.rename.app_logger"), \
         patch("cbz_ops.rename.is_hidden", return_value=False):
        yield


def _write_series_json(folder, name="Sandman", volume=2, year=1989):
    payload = {
        "metadata": {
            "type": "comicSeries",
            "name": name,
            "volume": volume,
            "year": year,
            "publisher": "Vertigo",
            "status": "Ended",
        }
    }
    with open(os.path.join(folder, "series.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _write_cvinfo(folder, cv_id=12345):
    with open(os.path.join(folder, "cvinfo"), "w", encoding="utf-8") as f:
        f.write(f"https://comicvine.gamespot.com/volume/4050-{cv_id}/\n")


def _enable_custom_rename(
    pattern="{series_name} {volume_number} {issue_number} ({year})",
    exclude_terms="Annual,Special",
):
    """Patch the pref reader used by smart_rename to enable a custom pattern.

    cbz_ops.rename does ``from core.database import get_user_preference``
    inside functions, so patching ``core.database.get_user_preference`` is
    the safest interception point.
    """
    return patch(
        "core.database.get_user_preference",
        side_effect=lambda key, default=None: {
            "enable_custom_rename": True,
            "custom_rename_pattern": pattern,
            "rename_clean_spaces_enabled": False,
            "rename_clean_specials_enabled": False,
            "smart_rename_exclude_terms": exclude_terms,
        }.get(key, default),
    )


class TestPlanSmartRenameSetup:

    def test_missing_directory_returns_error(self):
        from cbz_ops.smart_rename import plan_smart_rename
        with _enable_custom_rename():
            plan = plan_smart_rename("/no/such/dir")
        assert "error" in plan
        assert "does not exist" in plan["error"].lower()

    def test_custom_pattern_disabled_returns_error(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        # Pref reader returns False/empty for every key (default)
        with patch(
            "core.database.get_user_preference",
            side_effect=lambda key, default=None: default,
        ):
            plan = plan_smart_rename(str(tmp_path))
        assert "error" in plan
        assert "Custom rename pattern" in plan["error"]


class TestPlanSmartRenameNeedsCvinfo:

    def test_missing_cvinfo_marks_directory(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "series"
        d.mkdir()
        (d / "Issue 001.cbz").write_bytes(b"x")  # one comic file
        with _enable_custom_rename():
            plan = plan_smart_rename(str(d))
        assert len(plan["directories"]) == 1
        assert plan["directories"][0]["status"] == "needs_cvinfo"
        assert plan["directories"][0]["files"] == []


class TestPlanSmartRenameSeriesJsonPath:

    def test_happy_path_files_get_renamed(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        (d / "Sandman 1.cbz").write_bytes(b"x")
        (d / "Sandman 12.cbz").write_bytes(b"x")
        (d / "ignore.txt").write_bytes(b"x")

        with _enable_custom_rename():
            plan = plan_smart_rename(str(d), recursive=False)

        assert len(plan["directories"]) == 1
        dir_entry = plan["directories"][0]
        assert dir_entry["status"] == "ok"
        names = [f["new_name"] for f in dir_entry["files"] if f["status"] == "ok"]
        assert "Sandman v2 001 (1989).cbz" in names
        assert "Sandman v2 012 (1989).cbz" in names

    def test_file_with_no_issue_number_is_skipped(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d)
        (d / "no-numbers-here.cbz").write_bytes(b"x")

        with _enable_custom_rename():
            plan = plan_smart_rename(str(d), recursive=False)

        files = plan["directories"][0]["files"]
        assert len(files) == 1
        assert files[0]["status"] == "no_issue"

    def test_collision_gets_suffix(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        # Existing file at the planned target
        (d / "Sandman v2 001 (1989).cbz").write_bytes(b"existing")
        (d / "Sandman 1.cbz").write_bytes(b"x")

        with _enable_custom_rename():
            plan = plan_smart_rename(str(d), recursive=False)

        renames = [f for f in plan["directories"][0]["files"] if f["status"] == "ok"]
        assert len(renames) == 1
        assert renames[0]["new_name"] == "Sandman v2 001 (1989) (2).cbz"

    def test_colon_in_series_name_is_replaced_with_dash(self, tmp_path):
        """Match the existing convention: ':' -> ' -' (Windows-illegal char)."""
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Batman Year One"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Batman: Year One", volume=1, year=1987)
        (d / "Batman 1.cbz").write_bytes(b"x")

        with _enable_custom_rename():
            plan = plan_smart_rename(str(d), recursive=False)

        names = [f["new_name"] for f in plan["directories"][0]["files"] if f["status"] == "ok"]
        assert names == ["Batman - Year One v1 001 (1987).cbz"]
        assert ":" not in names[0]

    def test_excluded_term_skips_annual(self, tmp_path):
        """Default exclude list ('Annual,Special') keeps Annuals out of the main namespace."""
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Punisher War Zone"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Punisher War Zone", volume=1, year=1992)
        (d / "Punisher War Zone 001.cbz").write_bytes(b"x")
        (d / "Punisher War Zone - Annual 001.cbz").write_bytes(b"x")

        with _enable_custom_rename():
            plan = plan_smart_rename(str(d), recursive=False)

        files = plan["directories"][0]["files"]
        statuses = {f["old_name"]: f["status"] for f in files}
        assert statuses["Punisher War Zone 001.cbz"] == "ok"
        assert statuses["Punisher War Zone - Annual 001.cbz"] == "excluded_term"
        excluded = next(f for f in files if f["status"] == "excluded_term")
        assert excluded["matched_term"] == "annual"

    def test_excluded_term_case_insensitive(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        (d / "Sandman SPECIAL 5.cbz").write_bytes(b"x")

        with _enable_custom_rename(exclude_terms="Special"):
            plan = plan_smart_rename(str(d), recursive=False)

        files = plan["directories"][0]["files"]
        assert files[0]["status"] == "excluded_term"
        assert files[0]["matched_term"] == "special"

    def test_empty_exclude_terms_disables_filter(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Punisher War Zone"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Punisher War Zone", volume=1, year=1992)
        (d / "Punisher War Zone - Annual 001.cbz").write_bytes(b"x")

        with _enable_custom_rename(exclude_terms=""):
            plan = plan_smart_rename(str(d), recursive=False)

        files = plan["directories"][0]["files"]
        assert files[0]["status"] == "ok"
        assert files[0]["new_name"] == "Punisher War Zone v1 001 (1992).cbz"

    def test_recursive_walks_subdirs(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        # Subdir A
        a = tmp_path / "Sandman v1"
        a.mkdir()
        _write_cvinfo(a, cv_id=1)
        _write_series_json(a, name="Sandman", volume=1, year=1989)
        (a / "Sandman 1.cbz").write_bytes(b"x")
        # Subdir B
        b = tmp_path / "Sandman v2"
        b.mkdir()
        _write_cvinfo(b, cv_id=2)
        _write_series_json(b, name="Sandman", volume=2, year=2022)
        (b / "Sandman 1.cbz").write_bytes(b"x")
        # Empty top-level dir (no comic files) -- should be skipped

        with _enable_custom_rename():
            plan = plan_smart_rename(str(tmp_path), recursive=True)

        # Two directories with comics, neither one is the root (which has no comics).
        dirs = {d["dir"]: d for d in plan["directories"]}
        assert str(a) in dirs and str(b) in dirs
        a_names = [f["new_name"] for f in dirs[str(a)]["files"] if f["status"] == "ok"]
        b_names = [f["new_name"] for f in dirs[str(b)]["files"] if f["status"] == "ok"]
        assert a_names == ["Sandman v1 001 (1989).cbz"]
        assert b_names == ["Sandman v2 001 (2022).cbz"]


class TestPlanSmartRenameAutoCreateSeriesJson:

    def test_metron_provider_creates_series_json(self, tmp_path):
        """When series.json is missing, provider lookup builds and writes it."""
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d, cv_id=999)
        (d / "Sandman 5.cbz").write_bytes(b"x")

        # Fake the Metron series object returned by api.series(metron_id).
        # Use SimpleNamespace so getattr() on unset fields returns AttributeError
        # (instead of MagicMock leaking non-JSON-serializable values into series.json).
        series_obj = SimpleNamespace(
            id=42,
            cv_id=999,
            name="Sandman",
            volume=1,
            year_began=1989,
            year_end=1996,
            publisher=SimpleNamespace(name="Vertigo"),
            imprint=None,
            desc="",
            issue_count=75,
            status="Ended",
            cover_image=None,
            image=None,
        )

        fake_api = MagicMock()
        fake_api.series.return_value = series_obj

        fake_metron = MagicMock()
        fake_metron.is_metron_configured.return_value = True
        fake_metron.get_flask_api.return_value = fake_api
        fake_metron.get_series_id.return_value = 42

        with _enable_custom_rename(), \
             patch("cbz_ops.smart_rename._get_metron_mod", return_value=fake_metron), \
             patch("core.database.get_library_providers",
                   return_value=[{"provider_type": "metron", "enabled": True}]):
            plan = plan_smart_rename(str(d), recursive=False, library_id=1)

        assert (d / "series.json").exists()
        dir_entry = plan["directories"][0]
        assert dir_entry["status"] == "ok"
        renames = [f for f in dir_entry["files"] if f["status"] == "ok"]
        assert renames[0]["new_name"] == "Sandman v1 005 (1989).cbz"

    def test_all_providers_fail_marks_series_json_failed(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        (d / "Sandman 5.cbz").write_bytes(b"x")

        fake_metron = MagicMock()
        fake_metron.is_metron_configured.return_value = False

        with _enable_custom_rename(), \
             patch("cbz_ops.smart_rename._get_metron_mod", return_value=fake_metron), \
             patch("core.database.get_library_providers",
                   return_value=[{"provider_type": "metron", "enabled": True}]):
            plan = plan_smart_rename(str(d), recursive=False, library_id=1)

        assert plan["directories"][0]["status"] == "series_json_failed"


class TestApplySmartRename:

    def test_apply_renames_files_on_disk(self, tmp_path):
        from cbz_ops.smart_rename import plan_smart_rename, apply_smart_rename
        d = tmp_path / "Sandman"
        d.mkdir()
        _write_cvinfo(d)
        _write_series_json(d, name="Sandman", volume=2, year=1989)
        (d / "Sandman 1.cbz").write_bytes(b"x")
        (d / "Sandman 2.cbz").write_bytes(b"x")

        with _enable_custom_rename():
            plan = plan_smart_rename(str(d), recursive=False)
            summary = apply_smart_rename(plan)

        assert summary["renamed"] == 2
        assert summary["failed"] == 0
        assert (d / "Sandman v2 001 (1989).cbz").exists()
        assert (d / "Sandman v2 002 (1989).cbz").exists()
        assert not (d / "Sandman 1.cbz").exists()

    def test_apply_skips_non_ok_entries(self, tmp_path):
        from cbz_ops.smart_rename import apply_smart_rename
        # Hand-built plan with a no-issue entry
        plan = {
            "directories": [
                {
                    "dir": str(tmp_path),
                    "status": "ok",
                    "files": [
                        {"status": "no_issue", "old_path": str(tmp_path / "a.cbz"),
                         "old_name": "a.cbz"},
                    ],
                }
            ]
        }
        summary = apply_smart_rename(plan)
        assert summary["renamed"] == 0
        assert summary["skipped"] >= 1
