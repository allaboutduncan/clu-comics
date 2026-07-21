"""Tests for models/library_automap.py -- sidecar-based auto-mapping."""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

# Import `sync` at collection time (unpatched) so its top-level
# `from core.database import get_series_by_id` binds the real function. A test
# below patches "sync.sync_series_from_api"; if that were sync's first import it
# would happen while core.database is patched and permanently freeze the mock
# into sync's namespace, breaking later tests that rely on the real fetch.
import sync  # noqa: F401
from helpers.comicvine_ids import make_comicvine_series_id
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
        ident = library_automap._resolve_identity(folder, api=None, cv_api_key=None)
        assert ident["metron_id"] is None
        assert "comicvine api not enabled" in ident["reason"].lower()

    def test_comicid_not_found_on_metron(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Obscure", series_json={"name": "Obscure", "comicid": 99999},
        )
        api = MagicMock()
        api.series_list.return_value = []
        ident = library_automap._resolve_identity(folder, api=api, cv_api_key=None)
        assert ident["metron_id"] is None
        assert "not in metron" in ident["reason"].lower()

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

class TestComicVineResolution:
    def test_cv_only_resolves_with_cv_key(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Saga", series_json={"name": "Saga", "comicid": 18705},
        )
        ident = library_automap._resolve_identity(folder, api=None, cv_api_key="key")
        assert ident["metron_id"] == make_comicvine_series_id(18705)
        assert ident["source"] == "comicvine_api"
        assert ident["cv_id"] == 18705

    def test_cv_only_skipped_without_key(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Saga", series_json={"name": "Saga", "comicid": 18705},
        )
        ident = library_automap._resolve_identity(folder, api=None, cv_api_key=None)
        assert ident["metron_id"] is None
        assert "ComicVine API not enabled" in ident["reason"]

    def test_prefers_metron_over_comicvine(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Saga", series_json={"name": "Saga", "comicid": 18705},
        )
        api = MagicMock()
        result = MagicMock()
        result.id = 999
        api.series_list.return_value = [result]
        ident = library_automap._resolve_identity(folder, api=api, cv_api_key="key")
        assert ident["metron_id"] == 999
        assert ident["source"] == "comicvine_id"


class TestComicVineApply:
    def test_fetches_details_from_comicvine(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Saga", series_json={"name": "Saga", "comicid": 18705},
        )
        cv_series_id = make_comicvine_series_id(18705)
        item = {
            "folder": folder, "metron_id": cv_series_id, "series_name": "Saga",
            "cv_id": 18705, "publisher_name": "Image",
        }
        cv_details = {
            "id": 18705, "name": "Saga", "publisher_name": "Image",
            "start_year": 2012, "count_of_issues": 60,
            "description": "<p>An epic.</p>", "image_url": "http://x/cover.jpg",
        }
        with patch("models.comicvine.get_cv_api_key", return_value="key"), \
             patch("models.comicvine.get_volume_details", return_value=cv_details), \
             patch("models.metron.get_flask_api", return_value=None), \
             patch("core.database.save_series_mapping", return_value=True) as save, \
             patch("core.database.save_publisher"), \
             patch("core.database.upsert_publisher_by_name", return_value=7):
            result = library_automap.apply_automap([item], api=None)
        assert result["applied"] == 1
        saved = save.call_args[0][0]
        assert saved["id"] == cv_series_id
        assert saved["cv_id"] == 18705
        assert saved["name"] == "Saga"
        assert "comicvine.gamespot.com/volume/4050-18705" in saved["resource_url"]
        assert saved["desc"] == "An epic."  # HTML stripped
        assert saved["publisher_id"] == 7

    def test_backfill_skips_comicvine_series(self, tmp_path):
        # A ComicVine offset id must never be written into a sidecar as a Metron id.
        with patch("models.comicvine.find_cvinfo_in_folder") as find, \
             patch("models.library_automap.write_series_json") as write:
            library_automap._backfill_sidecars(
                str(tmp_path), {"id": make_comicvine_series_id(1)},
                make_comicvine_series_id(1), api=None,
            )
        find.assert_not_called()
        write.assert_not_called()


class TestComicVineSyncMatch:
    def test_syncs_issues_from_comicvine_then_matches(self, tmp_path):
        folder = str(tmp_path)
        cv_series_id = make_comicvine_series_id(18705)
        cv_issues = [{"id": 1, "number": "1"}, {"id": 2, "number": "2"}]
        with patch("core.database.get_series_mapping", return_value=folder), \
             patch("core.database.get_issues_for_series", side_effect=[[], cv_issues]), \
             patch("core.database.get_series_by_id", return_value={"id": cv_series_id, "name": "Saga"}), \
             patch("core.database.delete_issues_for_series"), \
             patch("core.database.save_issues_bulk") as save_issues, \
             patch("core.database.update_series_sync_time"), \
             patch("models.comicvine.get_cv_api_key", return_value="key"), \
             patch("models.comicvine.get_all_issues_for_volume", return_value=cv_issues) as fetch, \
             patch("helpers.collection.match_issues_to_collection") as match:
            library_automap._sync_and_match(api=None, series_id=cv_series_id)
        fetch.assert_called_once_with("key", 18705)
        save_issues.assert_called_once()
        match.assert_called_once()


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

    def test_match_unmatched_reports_progress(self):
        rows = [{"id": 1, "name": "Batman"}, {"id": 2, "name": "Saga"}]
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.get_collection_status_for_series", return_value=None), \
             patch.object(library_automap, "_sync_and_match"), \
             patch("core.app_state.register_operation", return_value="op1") as reg, \
             patch("core.app_state.update_operation") as upd, \
             patch("core.app_state.complete_operation") as done:
            library_automap.match_unmatched_mapped_series(api=None)
        reg.assert_called_once()
        assert reg.call_args.args[0] == "match"
        assert reg.call_args.kwargs.get("total") == 2
        assert upd.call_count == 2
        done.assert_called_once_with("op1")

    def test_match_unmatched_no_operation_when_all_matched(self):
        rows = [{"id": 1, "name": "Batman"}]
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.get_collection_status_for_series",
                   return_value=[{"issue_number": "1"}]), \
             patch("core.app_state.register_operation") as reg:
            library_automap.match_unmatched_mapped_series(api=None)
        reg.assert_not_called()


class TestDefaultMonitorOff:
    """On import, a fully-owned Cancelled/Completed series defaults Monitor off."""

    def _sync(self, tmp_path, series, match_status, *, monitored=True):
        folder = str(tmp_path)
        with patch("core.database.get_series_mapping", return_value=folder), \
             patch("core.database.get_issues_for_series", return_value=[{"number": "1"}]), \
             patch("core.database.get_series_by_id", return_value=series), \
             patch("helpers.collection.match_issues_to_collection",
                   return_value=match_status), \
             patch("core.database.get_series_monitored", return_value=monitored), \
             patch("core.database.set_series_monitored") as set_mon:
            library_automap._sync_and_match(api=None, series_id=series["id"])
        return set_mon

    def test_off_when_complete_and_cancelled(self, tmp_path):
        series = {"id": 1, "name": "Y The Last Man", "status": "Cancelled"}
        status = {"1": {"found": True}, "2": {"found": True}}
        set_mon = self._sync(tmp_path, series, status)
        set_mon.assert_called_once_with(1, False)

    def test_off_when_complete_and_completed(self, tmp_path):
        series = {"id": 2, "name": "Watchmen", "status": "Completed"}
        status = {"1": {"found": True}}
        set_mon = self._sync(tmp_path, series, status)
        set_mon.assert_called_once_with(2, False)

    def test_left_on_when_ongoing(self, tmp_path):
        series = {"id": 3, "name": "Batman", "status": "Ongoing"}
        status = {"1": {"found": True}}
        set_mon = self._sync(tmp_path, series, status)
        set_mon.assert_not_called()

    def test_left_on_when_issue_missing(self, tmp_path):
        series = {"id": 4, "name": "Saga", "status": "Cancelled"}
        status = {"1": {"found": True}, "2": {"found": False}}
        set_mon = self._sync(tmp_path, series, status)
        set_mon.assert_not_called()

    def test_no_change_when_already_unmonitored(self, tmp_path):
        series = {"id": 5, "name": "Preacher", "status": "Completed"}
        status = {"1": {"found": True}}
        set_mon = self._sync(tmp_path, series, status, monitored=False)
        set_mon.assert_not_called()

    def test_status_match_is_case_insensitive(self, tmp_path):
        series = {"id": 6, "name": "Sandman", "status": "COMPLETED"}
        status = {"1": {"found": True}}
        set_mon = self._sync(tmp_path, series, status)
        set_mon.assert_called_once_with(6, False)


class TestApplyExtra:
    def test_falls_back_to_sidecar_when_no_api(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Batman",
            series_json={"name": "Batman", "metron_id": 555},
            cvinfo="series_id: 555\n",
        )
        with patch("core.database.save_series_mapping", return_value=True) as save, \
             patch("core.database.save_publisher"), \
             patch("core.database.get_series_by_id", return_value=None), \
             patch("models.metron.get_flask_api", return_value=None):
            result = library_automap.apply_automap(
                [{"folder": folder, "metron_id": 555, "series_name": "Batman"}], api=None
            )
        assert result["applied"] == 1
        saved_dict = save.call_args[0][0]
        assert saved_dict["name"] == "Batman"

    def test_populates_publisher_and_status_from_sidecar_without_api(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Batman",
            series_json={"name": "Batman", "metron_id": 555,
                         "publisher": "DC Comics", "status": "Continuing"},
            cvinfo="series_id: 555\npublisher_name: DC Comics\n",
        )
        item = {
            "folder": folder, "metron_id": 555, "series_name": "Batman",
            "publisher_name": "DC Comics", "status": "Continuing",
        }
        with patch("core.database.save_series_mapping", return_value=True) as save, \
             patch("core.database.save_publisher"), \
             patch("core.database.get_series_by_id", return_value=None), \
             patch("core.database.upsert_publisher_by_name", return_value=42) as upsert, \
             patch("models.metron.get_flask_api", return_value=None):
            result = library_automap.apply_automap([item], api=None)
        assert result["applied"] == 1
        upsert.assert_called_once_with("DC Comics")
        saved_dict = save.call_args[0][0]
        assert saved_dict["publisher_id"] == 42
        assert saved_dict["status"] == "Continuing"

    def test_scan_candidate_carries_status(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Batman",
            series_json={"name": "Batman", "metron_id": 555, "status": "Ended"},
        )
        ident = library_automap._resolve_identity(folder, api=None)
        assert ident["status"] == "Ended"


class TestSeriesNameFromFolder:
    def test_volume_leaf_uses_parent(self):
        assert (
            library_automap._series_name_from_folder("/data/DC Comics/Mister Miracle/v2017")
            == "Mister Miracle"
        )

    def test_short_volume_leaf_uses_parent(self):
        assert library_automap._series_name_from_folder("/data/Image/Saga/v2") == "Saga"

    def test_normal_leaf_kept(self):
        assert (
            library_automap._series_name_from_folder("/data/DC/Batman (2016)")
            == "Batman (2016)"
        )

    def test_volume_leaf_at_data_root_falls_back_to_leaf(self):
        # No meaningful series name above the volume folder -- keep the leaf.
        assert library_automap._series_name_from_folder("/data/v2017") == "v2017"

    def test_trailing_slash_tolerated(self):
        assert (
            library_automap._series_name_from_folder("/data/DC/Mister Miracle/v2017/")
            == "Mister Miracle"
        )


class TestVolumeNameResolution:
    def test_cvinfo_only_volume_folder_uses_parent_name(self, tmp_path):
        # cvinfo carries the id but no name; the leaf is the volume, so the
        # series name must come from the parent folder, not "v2017".
        folder = _make_folder(
            tmp_path, os.path.join("Mister Miracle", "v2017"),
            cvinfo="https://comicvine.gamespot.com/mm/4050-111/\nseries_id: 888\n",
        )
        ident = library_automap._resolve_identity(folder, api=None)
        assert ident["metron_id"] == 888
        assert ident["series_name"] == "Mister Miracle"


class TestApplyDoesNotClobberName:
    def _failing_api(self):
        api = MagicMock()
        api.series.side_effect = RuntimeError("rate limited")
        return api

    def test_failed_fetch_keeps_existing_good_name(self, tmp_path):
        folder = _make_folder(
            tmp_path, os.path.join("Mister Miracle", "v2017"),
            cvinfo="series_id: 888\n",
        )
        # The scan candidate carries the folder-derived guess; without the fix
        # this would overwrite the good DB name.
        item = {"folder": folder, "metron_id": 888, "series_name": "Mister Miracle"}
        with patch("core.database.save_series_mapping", return_value=True) as save, \
             patch("core.database.save_publisher"), \
             patch("core.database.upsert_publisher_by_name", return_value=None), \
             patch("core.database.get_series_by_id",
                   return_value={"id": 888, "name": "Mister Miracle"}), \
             patch("models.library_automap.write_series_json") as write:
            result = library_automap.apply_automap([item], api=self._failing_api())
        assert result["applied"] == 1
        saved = save.call_args[0][0]
        assert saved["name"] == "Mister Miracle"
        # Unverified data must not be baked into series.json.
        write.assert_not_called()

    def test_failed_fetch_does_not_preserve_volume_token_name(self, tmp_path):
        # If the DB name is itself a volume token, don't keep it -- fall through
        # to the (best-effort) fallback name so it can be repaired later.
        folder = _make_folder(tmp_path, os.path.join("Saga", "v2012"),
                              cvinfo="series_id: 42\n")
        item = {"folder": folder, "metron_id": 42, "series_name": "Saga"}
        with patch("core.database.save_series_mapping", return_value=True) as save, \
             patch("core.database.save_publisher"), \
             patch("core.database.upsert_publisher_by_name", return_value=None), \
             patch("core.database.get_series_by_id",
                   return_value={"id": 42, "name": "v2012"}), \
             patch("models.library_automap.write_series_json"):
            library_automap.apply_automap([item], api=self._failing_api())
        saved = save.call_args[0][0]
        assert saved["name"] == "Saga"


class TestRepairVolumeNamedSeries:
    def test_renames_metron_series_and_rewrites_sidecar(self, tmp_path):
        rows = [{"id": 555, "name": "v2017", "mapped_path": str(tmp_path)}]
        series_obj = MagicMock()
        series_obj.model_dump.return_value = {
            "id": 555, "name": "Mister Miracle",
            "publisher": {"id": 10, "name": "DC"}, "year_began": 2017,
        }
        api = MagicMock()
        api.series.return_value = series_obj
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.save_series_mapping", return_value=True) as save, \
             patch("core.database.save_publisher"), \
             patch("models.library_automap.write_series_json") as write:
            repaired = library_automap.repair_volume_named_series(api)
        assert repaired == 1
        saved, saved_path = save.call_args[0][:2]
        assert saved["name"] == "Mister Miracle"
        assert saved_path == str(tmp_path)
        write.assert_called_once()

    def test_uses_blocking_get_series(self, tmp_path):
        # Repair must fetch through the rate-limit-aware metron.get_series so a
        # bulk run doesn't error out and skip the rename.
        rows = [{"id": 555, "name": "v2017", "mapped_path": str(tmp_path)}]
        model = MagicMock()
        model.model_dump.return_value = {"id": 555, "name": "Batman", "cv_id": 1}
        api = MagicMock()
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.save_series_mapping", return_value=True) as save, \
             patch("core.database.save_publisher"), \
             patch("models.metron.get_series", return_value=model) as gs, \
             patch("models.library_automap.write_series_json"):
            repaired = library_automap.repair_volume_named_series(api)
        assert repaired == 1
        gs.assert_called_once_with(api, 555)
        assert save.call_args[0][0]["name"] == "Batman"

    def test_skips_when_api_unavailable(self, tmp_path):
        # metron.get_series returns None when the API is exhausted/down -> the
        # row is left as-is for the next scan (exact-name-only, no fallback).
        rows = [{"id": 555, "name": "v2017", "mapped_path": str(tmp_path)}]
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.save_series_mapping", return_value=True) as save, \
             patch("models.metron.get_series", return_value=None), \
             patch("models.library_automap.write_series_json"):
            repaired = library_automap.repair_volume_named_series(MagicMock())
        assert repaired == 0
        save.assert_not_called()

    def test_skips_non_volume_names(self, tmp_path):
        rows = [{"id": 555, "name": "Batman", "mapped_path": str(tmp_path)}]
        api = MagicMock()
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.save_series_mapping", return_value=True) as save:
            repaired = library_automap.repair_volume_named_series(api)
        assert repaired == 0
        api.series.assert_not_called()
        save.assert_not_called()

    def test_reports_progress_in_ops_indicator(self, tmp_path):
        # A repairable row should register + complete an operation for the nav.
        rows = [{"id": 555, "name": "v2017", "mapped_path": str(tmp_path)}]
        series_obj = MagicMock()
        series_obj.model_dump.return_value = {"id": 555, "name": "Batman", "cv_id": 1}
        api = MagicMock()
        api.series.return_value = series_obj
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.save_series_mapping", return_value=True), \
             patch("core.database.save_publisher"), \
             patch("models.library_automap.write_series_json"), \
             patch("core.app_state.register_operation", return_value="op1") as reg, \
             patch("core.app_state.update_operation") as upd, \
             patch("core.app_state.complete_operation") as done:
            library_automap.repair_volume_named_series(api)
        reg.assert_called_once()
        assert reg.call_args.args[0] == "repair"
        assert reg.call_args.kwargs.get("total") == 1
        upd.assert_called()
        done.assert_called_once_with("op1")

    def test_no_operation_when_nothing_to_repair(self, tmp_path):
        rows = [{"id": 555, "name": "Batman", "mapped_path": str(tmp_path)}]
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.app_state.register_operation") as reg:
            library_automap.repair_volume_named_series(api=MagicMock())
        reg.assert_not_called()

    def test_comicvine_offset_id_repaired_without_sidecar_write(self, tmp_path):
        cv_id = 18705
        cv_series_id = make_comicvine_series_id(cv_id)
        rows = [{"id": cv_series_id, "name": "v2012", "mapped_path": str(tmp_path),
                 "publisher_name": "Image"}]
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.save_series_mapping", return_value=True) as save, \
             patch("core.database.upsert_publisher_by_name", return_value=7), \
             patch("models.comicvine.get_cv_api_key", return_value="key"), \
             patch("models.comicvine.get_volume_details",
                   return_value={"id": cv_id, "name": "Saga", "publisher_name": "Image",
                                 "start_year": 2012}), \
             patch("models.library_automap.write_series_json") as write:
            repaired = library_automap.repair_volume_named_series(api=None)
        assert repaired == 1
        saved = save.call_args[0][0]
        assert saved["name"] == "Saga"
        assert saved["id"] == cv_series_id
        # ComicVine offset id must not be stamped into series.json as a Metron id.
        write.assert_not_called()
