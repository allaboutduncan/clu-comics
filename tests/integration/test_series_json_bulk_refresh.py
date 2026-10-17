"""Refresh-all for series.json problem rows, against a real SQLite schema.

"Corrected" is the re-check's verdict, not the provider call's, so most of these
cases are about a write that succeeded and still left the file blank.
"""

import json
import os
from unittest.mock import patch

import pytest

from core import series_json_check as sjc
from core.problem_files import (
    CLASS_SERIES_JSON_BLANK,
    SOURCE_SERIES_JSON,
    get_problem,
    set_dismissed,
)


def _write(folder, **metadata):
    base = {"name": "Series", "description_text": "Text", "comic_image": "http://x/c.jpg"}
    base.update(metadata)
    path = os.path.join(folder, "series.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"metadata": base}, f)
    return path


@pytest.fixture
def library(tmp_path, db_connection):
    root = tmp_path / "library"
    root.mkdir()
    with patch("helpers.library.get_library_roots", return_value=[str(root)]), \
         patch("helpers.library.resolve_provider_library_id", return_value=None):
        yield root


@pytest.fixture(autouse=True)
def _reset_result():
    sjc._bulk_result = None
    yield
    sjc._bulk_result = None


def _blank_folder(library, name):
    folder = library / name
    folder.mkdir()
    path = _write(str(folder), description_text=None)
    sjc.recheck_file(path)  # records the row exactly as a scan would
    assert get_problem(path, SOURCE_SERIES_JSON) is not None
    return str(folder), path


def _provider(outcomes):
    """A stand-in for refresh_series_json driven by folder name."""
    def fake(folder, library_id=None, provider="auto", force=False):
        action = outcomes[os.path.basename(folder)]
        if action == "fix":
            _write(folder)
            return {"success": True}
        if action == "still_blank":
            _write(folder, description_text=None)
            return {"success": True}
        if action == "boom":
            raise RuntimeError("provider exploded")
        return {"success": False, "error": action}
    return fake


class TestRefreshAll:
    def test_corrected_rows_clear_and_the_rest_are_reported_with_reasons(self, library):
        _, fixed_path = _blank_folder(library, "Fixed")
        _, err_path = _blank_folder(library, "NoProvider")
        _, blank_path = _blank_folder(library, "StillBlank")

        outcomes = {"Fixed": "fix", "NoProvider": "No provider returned data", "StillBlank": "still_blank"}
        with patch("core.series_json_builder.refresh_series_json", side_effect=_provider(outcomes)):
            result = sjc.refresh_all()

        assert result["total"] == 3 and result["fixed"] == 1
        assert get_problem(fixed_path, SOURCE_SERIES_JSON) is None
        assert get_problem(err_path, SOURCE_SERIES_JSON) is not None
        assert get_problem(blank_path, SOURCE_SERIES_JSON) is not None

        reasons = {os.path.basename(f["folder"]): f["reason"] for f in result["failed"]}
        assert reasons["NoProvider"] == "No provider returned data"
        # A write that succeeded but left the file blank is not "corrected", and
        # the reason is the checker's, which names the field.
        assert "description_text" in reasons["StillBlank"]
        assert "Fixed" not in reasons

    def test_one_failure_does_not_stop_the_run(self, library):
        _blank_folder(library, "A")
        _, b_path = _blank_folder(library, "B")
        _, c_path = _blank_folder(library, "C")
        outcomes = {"A": "boom", "B": "fix", "C": "fix"}
        with patch("core.series_json_builder.refresh_series_json", side_effect=_provider(outcomes)):
            result = sjc.refresh_all()
        assert result["fixed"] == 2
        assert len(result["failed"]) == 1
        assert "provider exploded" in result["failed"][0]["reason"]
        assert get_problem(b_path, SOURCE_SERIES_JSON) is None

    def test_dismissed_rows_are_skipped(self, library):
        _, kept = _blank_folder(library, "Dismissed")
        _, other = _blank_folder(library, "Open")
        set_dismissed(kept, SOURCE_SERIES_JSON, True)
        calls = []

        def fake(folder, **kw):
            calls.append(os.path.basename(folder))
            _write(folder)
            return {"success": True}

        with patch("core.series_json_builder.refresh_series_json", side_effect=fake):
            result = sjc.refresh_all()
        assert calls == ["Open"]
        assert result["total"] == 1

    def test_hand_edits_are_kept_because_force_is_never_passed(self, library):
        _blank_folder(library, "A")
        seen = {}

        def fake(folder, library_id=None, provider="auto", force=False):
            seen["force"] = force
            _write(folder)
            return {"success": True}

        with patch("core.series_json_builder.refresh_series_json", side_effect=fake):
            sjc.refresh_all()
        assert seen["force"] is False

    def test_a_vanished_folder_is_reported_not_crashed_on(self, library):
        folder, path = _blank_folder(library, "Gone")
        os.remove(path)
        os.rmdir(folder)
        with patch("core.series_json_builder.refresh_series_json") as refresh:
            result = sjc.refresh_all()
            refresh.assert_not_called()
        assert result["failed"][0]["reason"] == "Folder not found"

    def test_folder_outside_a_library_is_refused(self, tmp_path, db_connection):
        outside = tmp_path / "elsewhere" / "Series"
        outside.mkdir(parents=True)
        path = _write(str(outside), description_text=None)
        sjc.recheck_file(path)
        with patch("helpers.library.get_library_roots", return_value=[str(tmp_path / "library")]), \
             patch("core.series_json_builder.refresh_series_json") as refresh:
            result = sjc.refresh_all()
            refresh.assert_not_called()
        assert "not inside a library" in result["failed"][0]["reason"]

    def test_last_result_is_kept_for_the_page(self, library):
        assert sjc.last_bulk_result() is None
        _blank_folder(library, "A")
        with patch("core.series_json_builder.refresh_series_json",
                   side_effect=_provider({"A": "fix"})):
            sjc.refresh_all()
        last = sjc.last_bulk_result()
        assert last["fixed"] == 1 and last["finished_at"] >= last["started_at"]

    def test_nothing_open_is_an_empty_run(self, library):
        result = sjc.refresh_all()
        assert result["total"] == 0 and result["failed"] == []
        assert sjc.count_open_series_json() == 0

    def test_count_ignores_dismissed(self, library):
        _, p = _blank_folder(library, "A")
        _blank_folder(library, "B")
        assert sjc.count_open_series_json() == 2
        set_dismissed(p, SOURCE_SERIES_JSON, True)
        assert sjc.count_open_series_json() == 1
