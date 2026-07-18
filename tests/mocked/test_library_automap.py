"""Tests for models/library_automap.py -- sidecar-based auto-mapping."""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from models import library_automap


def _make_folder(root, name, *, series_json=None, wrap=True, cvinfo=None, comics=0):
    """Create a series folder with optional sidecars and dummy comic files.

    ``series_json`` is written in the real Mylar/CLU ``{"metadata": {...}}``
    shape by default; pass ``wrap=False`` to write a flat/legacy file.
    """
    folder = os.path.join(str(root), name)
    os.makedirs(folder, exist_ok=True)
    if series_json is not None:
        payload = {"metadata": series_json} if wrap else series_json
        with open(os.path.join(folder, "series.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
    if cvinfo is not None:
        with open(os.path.join(folder, "cvinfo"), "w", encoding="utf-8") as f:
            f.write(cvinfo)
    for i in range(comics):
        with open(os.path.join(folder, f"issue {i}.cbz"), "w") as f:
            f.write("x")
    return folder


class TestResolveIdentity:
    def test_series_json_metron_id_is_direct(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Batman",
            series_json={"name": "Batman", "metron_id": 555, "comicid": 4050,
                         "publisher": "DC", "year": 2016},
        )
        ident = library_automap._resolve_identity(folder, api=None)
        assert ident["metron_id"] == 555
        assert ident["source"] == "series.json:metron_id"
        assert ident["reason"] is None
        assert ident["series_name"] == "Batman"

    def test_cvinfo_series_id_is_direct(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Flash",
            cvinfo="https://comicvine.gamespot.com/flash/4050-1234/\nseries_id: 777\n",
        )
        ident = library_automap._resolve_identity(folder, api=None)
        assert ident["metron_id"] == 777
        assert ident["source"] == "cvinfo:series_id"

    def test_comicid_resolves_via_metron_lookup(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Saga", series_json={"name": "Saga", "comicid": 18705},
        )
        api = MagicMock()
        result = MagicMock()
        result.id = 4242
        api.series_list.return_value = [result]
        ident = library_automap._resolve_identity(folder, api=api)
        assert ident["metron_id"] == 4242
        assert ident["source"] == "comicvine_id"

    def test_comicid_unresolved_when_no_api(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Saga", series_json={"name": "Saga", "comicid": 18705},
        )
        ident = library_automap._resolve_identity(folder, api=None)
        assert ident["metron_id"] is None
        assert "unavailable" in ident["reason"].lower()

    def test_comicid_not_found_on_metron(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Obscure", series_json={"name": "Obscure", "comicid": 99999},
        )
        api = MagicMock()
        api.series_list.return_value = []
        ident = library_automap._resolve_identity(folder, api=api)
        assert ident["metron_id"] is None
        assert "not found" in ident["reason"].lower()

    def test_flat_legacy_series_json_still_resolves(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Legacy",
            series_json={"name": "Legacy", "metron_id": 321}, wrap=False,
        )
        ident = library_automap._resolve_identity(folder, api=None)
        assert ident["metron_id"] == 321
        assert ident["series_name"] == "Legacy"

    def test_idless_sidecar_is_skipped(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Mystery", series_json={"name": "Mystery"},
        )
        ident = library_automap._resolve_identity(folder, api=None)
        assert ident["metron_id"] is None
        assert ident["reason"]

    def test_no_sidecar_returns_none(self, tmp_path):
        folder = _make_folder(tmp_path, "Empty", comics=2)
        assert library_automap._resolve_identity(folder, api=None) is None


class TestScan:
    def _patch_roots_and_mapping(self, roots, mapped_rows):
        return (
            patch.object(library_automap, "get_library_roots", return_value=roots),
            patch("core.database.get_all_mapped_series", return_value=mapped_rows),
        )

    def test_direct_id_goes_to_auto(self, tmp_path):
        _make_folder(tmp_path, "Batman",
                     series_json={"name": "Batman", "metron_id": 555}, comics=3)
        p_roots, p_map = self._patch_roots_and_mapping([str(tmp_path)], [])
        with p_roots, p_map:
            result = library_automap.scan_library_for_automap(api=None)
        assert len(result["auto"]) == 1
        assert result["auto"][0]["metron_id"] == 555
        assert result["auto"][0]["comic_count"] == 3
        assert not result["review"]
        assert not result["skipped"]

    def test_already_mapped_series_goes_to_review(self, tmp_path):
        folder = _make_folder(tmp_path, "Batman",
                              series_json={"name": "Batman", "metron_id": 555})
        mapped = [{"id": 555, "mapped_path": "/data/OtherBatman"}]
        p_roots, p_map = self._patch_roots_and_mapping([str(tmp_path)], mapped)
        with p_roots, p_map:
            result = library_automap.scan_library_for_automap(api=None)
        assert not result["auto"]
        assert len(result["review"]) == 1
        assert result["review"][0]["conflict_with"] == "/data/OtherBatman"

    def test_folder_already_mapped_is_skipped_silently(self, tmp_path):
        folder = _make_folder(tmp_path, "Batman",
                              series_json={"name": "Batman", "metron_id": 555})
        mapped = [{"id": 555, "mapped_path": library_automap._norm(folder)}]
        p_roots, p_map = self._patch_roots_and_mapping([str(tmp_path)], mapped)
        with p_roots, p_map:
            result = library_automap.scan_library_for_automap(api=None)
        assert not result["auto"]
        assert not result["review"]
        assert not result["skipped"]

    def test_duplicate_series_in_scan_goes_to_review(self, tmp_path):
        _make_folder(tmp_path, "Batman-A",
                     series_json={"name": "Batman", "metron_id": 555})
        _make_folder(tmp_path, "Batman-B",
                     series_json={"name": "Batman", "metron_id": 555})
        p_roots, p_map = self._patch_roots_and_mapping([str(tmp_path)], [])
        with p_roots, p_map:
            result = library_automap.scan_library_for_automap(api=None)
        assert len(result["auto"]) == 1
        assert len(result["review"]) == 1

    def test_idless_folder_goes_to_skipped(self, tmp_path):
        _make_folder(tmp_path, "Mystery", series_json={"name": "Mystery"})
        p_roots, p_map = self._patch_roots_and_mapping([str(tmp_path)], [])
        with p_roots, p_map:
            result = library_automap.scan_library_for_automap(api=None)
        assert not result["auto"]
        assert len(result["skipped"]) == 1


class TestApply:
    def test_applies_and_saves_mapping(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Batman",
            series_json={"name": "Batman", "metron_id": 555},
            cvinfo="series_id: 555\n",
        )
        api = MagicMock()
        series_obj = MagicMock()
        series_obj.model_dump.return_value = {
            "id": 555, "name": "Batman", "publisher": {"id": 10, "name": "DC"},
            "year_began": 2016, "cv_id": 4050,
        }
        api.series.return_value = series_obj

        with patch("core.database.save_series_mapping", return_value=True) as save, \
             patch("core.database.save_publisher") as save_pub:
            result = library_automap.apply_automap(
                [{"folder": folder, "metron_id": 555, "series_name": "Batman"}], api=api
            )
        assert result["applied"] == 1
        assert result["applied_ids"] == [555]
        saved_dict, saved_path = save.call_args[0][:2]
        assert saved_path == folder
        assert saved_dict["id"] == 555
        save_pub.assert_called_once_with(10, "DC")

    def test_missing_folder_is_failure(self, tmp_path):
        api = MagicMock()
        with patch("core.database.save_series_mapping", return_value=True), \
             patch("core.database.save_publisher"):
            result = library_automap.apply_automap(
                [{"folder": str(tmp_path / "gone"), "metron_id": 1}], api=api
            )
        assert result["applied"] == 0
        assert len(result["failed"]) == 1

    def test_missing_metron_id_is_failure(self, tmp_path):
        folder = _make_folder(tmp_path, "X", series_json={"name": "X"})
        with patch("core.database.save_series_mapping", return_value=True), \
             patch("core.database.save_publisher"):
            result = library_automap.apply_automap(
                [{"folder": folder, "metron_id": None}], api=MagicMock()
            )
        assert result["applied"] == 0
        assert len(result["failed"]) == 1

class TestSyncAndMatch:
    def test_matches_when_issues_cached(self, tmp_path):
        folder = str(tmp_path)
        issues = [{"number": "1"}]
        series = {"id": 555, "name": "Batman"}
        with patch("core.database.get_series_mapping", return_value=folder), \
             patch("core.database.get_issues_for_series", return_value=issues), \
             patch("core.database.get_series_by_id", return_value=series), \
             patch("helpers.collection.match_issues_to_collection") as match:
            library_automap._sync_and_match(api=None, series_id=555)
        match.assert_called_once()
        args, kwargs = match.call_args
        assert args[0] == folder
        assert args[1] == issues
        assert kwargs.get("use_cache") is False

    def test_syncs_when_no_issues_then_matches(self, tmp_path):
        folder = str(tmp_path)
        api = MagicMock()
        call_state = {"synced": False}

        def get_issues(_sid):
            return [{"number": "1"}] if call_state["synced"] else []

        def do_sync(_api, _sid):
            call_state["synced"] = True

        with patch("core.database.get_series_mapping", return_value=folder), \
             patch("core.database.get_issues_for_series", side_effect=get_issues), \
             patch("core.database.get_series_by_id", return_value={"id": 9, "name": "X"}), \
             patch("sync.sync_series_from_api", side_effect=do_sync) as sync_fn, \
             patch("helpers.collection.match_issues_to_collection") as match:
            library_automap._sync_and_match(api=api, series_id=9)
        sync_fn.assert_called_once()
        match.assert_called_once()

    def test_skips_when_folder_missing(self, tmp_path):
        with patch("core.database.get_series_mapping", return_value=str(tmp_path / "gone")), \
             patch("helpers.collection.match_issues_to_collection") as match:
            library_automap._sync_and_match(api=MagicMock(), series_id=1)
        match.assert_not_called()

    def test_match_unmatched_skips_already_matched(self):
        rows = [{"id": 1}, {"id": 2}]

        def cached(sid):
            return [{"issue_number": "1"}] if sid == 1 else None

        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.get_collection_status_for_series", side_effect=cached), \
             patch.object(library_automap, "_sync_and_match") as sm:
            library_automap.match_unmatched_mapped_series(api=None)
        called_ids = [c.args[1] for c in sm.call_args_list]
        assert called_ids == [2]


class TestApplyExtra:
    def test_falls_back_to_sidecar_when_no_api(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Batman",
            series_json={"name": "Batman", "metron_id": 555},
            cvinfo="series_id: 555\n",
        )
        with patch("core.database.save_series_mapping", return_value=True) as save, \
             patch("core.database.save_publisher"), \
             patch("models.metron.get_flask_api", return_value=None):
            result = library_automap.apply_automap(
                [{"folder": folder, "metron_id": 555, "series_name": "Batman"}], api=None
            )
        assert result["applied"] == 1
        saved_dict = save.call_args[0][0]
        assert saved_dict["name"] == "Batman"
