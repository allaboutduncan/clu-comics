"""Tests for models/library_automap.py -- sidecar-based auto-mapping."""
import json
import os
import time
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
        ident = library_automap._resolve_identity(folder, api=None, cv_available=False)
        assert ident["metron_id"] is None
        assert "comicvine not enabled" in ident["reason"].lower()

    def test_comicid_not_found_on_metron(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Obscure", series_json={"name": "Obscure", "comicid": 99999},
        )
        api = MagicMock()
        api.series_list.return_value = []
        ident = library_automap._resolve_identity(folder, api=api, cv_available=False)
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

    def test_one_bad_folder_does_not_abort_scan(self, tmp_path):
        # A folder that raises while resolving must be collected into `errors`,
        # and every other folder must still be classified (issue #436: one bad
        # sidecar previously discarded the whole scan).
        good = _make_folder(tmp_path, "Batman",
                            series_json={"name": "Batman", "metron_id": 555})
        bad = _make_folder(tmp_path, "Broken",
                           series_json={"name": "Broken", "metron_id": 999})
        real_resolve = library_automap._resolve_identity

        def flaky_resolve(folder, api, cv_available=False):
            if library_automap._norm(folder) == library_automap._norm(bad):
                raise ValueError("corrupt sidecar")
            return real_resolve(folder, api, cv_available=cv_available)

        p_roots, p_map = self._patch_roots_and_mapping([str(tmp_path)], [])
        with p_roots, p_map, \
             patch.object(library_automap, "_resolve_identity", side_effect=flaky_resolve):
            result = library_automap.scan_library_for_automap(api=None)

        assert [it["metron_id"] for it in result["auto"]] == [555]
        assert len(result["errors"]) == 1
        assert library_automap._norm(result["errors"][0]["folder"]) == library_automap._norm(bad)
        assert "corrupt sidecar" in result["errors"][0]["reason"]

    def test_bad_encoding_series_json_is_not_fatal(self, tmp_path):
        # End-to-end: a series.json with invalid UTF-8 bytes (the concrete crash
        # from issue #436) resolves to no id -> skipped, never raising, and the
        # good folder is still auto-mapped.
        good = _make_folder(tmp_path, "Batman",
                            series_json={"name": "Batman", "metron_id": 555})
        bad = os.path.join(str(tmp_path), "Broken")
        os.makedirs(bad, exist_ok=True)
        with open(os.path.join(bad, "series.json"), "wb") as f:
            f.write(b'{"metadata": {"name": "\xff\xfe", "metron_id": 7}}')
        p_roots, p_map = self._patch_roots_and_mapping([str(tmp_path)], [])
        with p_roots, p_map:
            result = library_automap.scan_library_for_automap(api=None)
        assert [it["metron_id"] for it in result["auto"]] == [555]
        # The unreadable sidecar is reported (not silently dropped, not fatal).
        assert len(result["errors"]) == 1
        assert library_automap._norm(result["errors"][0]["folder"]) == library_automap._norm(bad)


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

class TestAddFolderToPullList:
    """Single-folder add — the File Manager / collection dropdown action."""

    def _patch(self, roots, mapped_rows):
        # api defaults to metron.get_flask_api() like the rest of the module, so
        # stub it out — there is no Flask app context in these tests.
        return (
            patch.object(library_automap, "get_library_roots", return_value=roots),
            patch("core.database.get_all_mapped_series", return_value=mapped_rows),
            patch.object(library_automap.metron, "get_flask_api", return_value=None),
        )

    def test_sidecar_folder_is_applied(self, tmp_path):
        folder = _make_folder(tmp_path, "Batman",
                              series_json={"name": "Batman", "metron_id": 555})
        p_roots, p_map, p_api = self._patch([str(tmp_path)], [])
        with p_roots, p_map, p_api, \
             patch.object(library_automap, "apply_and_sync",
                          return_value={"applied": 1, "failed": [],
                                        "applied_ids": [555]}) as apply_mock:
            result = library_automap.add_folder_to_pull_list(folder, cv_available=False)
        assert result["status"] == "applied"
        assert result["series_id"] == 555
        assert result["series_name"] == "Batman"
        # Same apply path as the library-wide scan.
        item = apply_mock.call_args[0][0][0]
        assert item["folder"] == folder
        assert item["metron_id"] == 555

    def test_folder_already_mapped_is_a_no_op(self, tmp_path):
        folder = _make_folder(tmp_path, "Batman",
                              series_json={"name": "Batman", "metron_id": 555})
        mapped = [{"id": 555, "name": "Batman",
                   "mapped_path": library_automap._norm(folder)}]
        p_roots, p_map, p_api = self._patch([str(tmp_path)], mapped)
        with p_roots, p_map, p_api, \
             patch.object(library_automap, "apply_and_sync") as apply_mock:
            result = library_automap.add_folder_to_pull_list(folder, cv_available=False)
        assert result["status"] == "already_mapped"
        assert result["series_id"] == 555
        apply_mock.assert_not_called()

    def test_series_mapped_elsewhere_is_a_conflict(self, tmp_path):
        folder = _make_folder(tmp_path, "Batman",
                              series_json={"name": "Batman", "metron_id": 555})
        mapped = [{"id": 555, "name": "Batman", "mapped_path": "/data/OtherBatman"}]
        p_roots, p_map, p_api = self._patch([str(tmp_path)], mapped)
        with p_roots, p_map, p_api, \
             patch.object(library_automap, "apply_and_sync") as apply_mock:
            result = library_automap.add_folder_to_pull_list(folder, cv_available=False)
        assert result["status"] == "conflict"
        assert result["mapped_to"] == "/data/OtherBatman"
        apply_mock.assert_not_called()

    def test_no_sidecar_needs_a_manual_match(self, tmp_path):
        folder = _make_folder(tmp_path, "Some Series", comics=2)
        p_roots, p_map, p_api = self._patch([str(tmp_path)], [])
        with p_roots, p_map, p_api:
            result = library_automap.add_folder_to_pull_list(folder, cv_available=False)
        assert result["status"] == "needs_match"
        assert result["suggested_name"] == "Some Series"

    def test_idless_sidecar_needs_a_manual_match(self, tmp_path):
        folder = _make_folder(tmp_path, "Mystery", series_json={"name": "Mystery"})
        p_roots, p_map, p_api = self._patch([str(tmp_path)], [])
        with p_roots, p_map, p_api:
            result = library_automap.add_folder_to_pull_list(folder, cv_available=False)
        assert result["status"] == "needs_match"
        assert result["suggested_name"] == "Mystery"
        assert "no Metron or ComicVine ID" in result["reason"]

    def test_unreadable_sidecar_needs_a_manual_match(self, tmp_path):
        # A corrupt sidecar must not 500 the dropdown — the user can still pick.
        folder = _make_folder(tmp_path, "Broken", series_json={"name": "Broken"})
        p_roots, p_map, p_api = self._patch([str(tmp_path)], [])
        with p_roots, p_map, p_api, \
             patch.object(library_automap, "_resolve_identity",
                          side_effect=ValueError("corrupt sidecar")):
            result = library_automap.add_folder_to_pull_list(folder, cv_available=False)
        assert result["status"] == "needs_match"
        assert "corrupt sidecar" in result["reason"]

    def test_manual_series_id_skips_the_sidecar_cascade(self, tmp_path):
        folder = _make_folder(tmp_path, "Some Series", comics=1)
        p_roots, p_map, p_api = self._patch([str(tmp_path)], [])
        with p_roots, p_map, p_api, \
             patch.object(library_automap, "_resolve_identity") as resolve, \
             patch.object(library_automap, "apply_and_sync",
                          return_value={"applied": 1, "failed": [],
                                        "applied_ids": [42]}) as apply_mock:
            result = library_automap.add_folder_to_pull_list(
                folder, series_id=42, cv_available=False,
                fallback={"series_name": "Chosen", "publisher_name": "DC",
                          "year": 2011},
            )
        resolve.assert_not_called()
        assert result["status"] == "applied"
        assert result["series_name"] == "Chosen"
        item = apply_mock.call_args[0][0][0]
        assert item["metron_id"] == 42
        assert item["publisher_name"] == "DC"
        assert item["year"] == 2011
        assert item["source"] == "manual"

    def test_folder_outside_a_library_is_rejected(self, tmp_path):
        library = tmp_path / "library"
        library.mkdir()
        outside = _make_folder(tmp_path, "downloads",
                               series_json={"name": "X", "metron_id": 1})
        p_roots, p_map, p_api = self._patch([str(library)], [])
        with p_roots, p_map, p_api, \
             patch.object(library_automap, "apply_and_sync") as apply_mock:
            result = library_automap.add_folder_to_pull_list(outside, cv_available=False)
        assert result["status"] == "failed"
        assert "not inside a configured library" in result["message"]
        apply_mock.assert_not_called()

    def test_missing_folder_is_rejected(self, tmp_path):
        p_roots, p_map, p_api = self._patch([str(tmp_path)], [])
        with p_roots, p_map, p_api:
            result = library_automap.add_folder_to_pull_list(
                str(tmp_path / "gone"), cv_available=False
            )
        assert result["status"] == "failed"
        assert "no longer exists" in result["message"]

    def test_apply_failure_is_reported(self, tmp_path):
        folder = _make_folder(tmp_path, "Batman",
                              series_json={"name": "Batman", "metron_id": 555})
        p_roots, p_map, p_api = self._patch([str(tmp_path)], [])
        with p_roots, p_map, p_api, \
             patch.object(library_automap, "apply_and_sync",
                          return_value={"applied": 0,
                                        "failed": [{"folder": folder,
                                                    "error": "Failed to save mapping"}],
                                        "applied_ids": []}):
            result = library_automap.add_folder_to_pull_list(folder, cv_available=False)
        assert result["status"] == "failed"
        assert result["message"] == "Failed to save mapping"


class TestComicVineResolution:
    def test_cv_only_resolves_when_comicvine_available(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Saga", series_json={"name": "Saga", "comicid": 18705},
        )
        ident = library_automap._resolve_identity(folder, api=None, cv_available=True)
        assert ident["metron_id"] == make_comicvine_series_id(18705)
        assert ident["source"] == "comicvine"
        assert ident["cv_id"] == 18705

    def test_cv_only_skipped_when_comicvine_unavailable(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Saga", series_json={"name": "Saga", "comicid": 18705},
        )
        ident = library_automap._resolve_identity(folder, api=None, cv_available=False)
        assert ident["metron_id"] is None
        assert "ComicVine not enabled" in ident["reason"]

    def test_prefers_metron_over_comicvine(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Saga", series_json={"name": "Saga", "comicid": 18705},
        )
        api = MagicMock()
        result = MagicMock()
        result.id = 999
        api.series_list.return_value = [result]
        ident = library_automap._resolve_identity(folder, api=api, cv_available=True)
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

    # --- force / progress_cb: the "Re-match Files" path -------------------

    def test_force_rematches_already_matched_series(self):
        rows = [{"id": 1, "name": "Batman"}, {"id": 2, "name": "Saga"}]
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.get_collection_status_for_series",
                   return_value=[{"issue_number": "1"}]), \
             patch("core.database.invalidate_collection_status_for_series") as inv, \
             patch.object(library_automap, "_sync_and_match") as sm:
            n = library_automap.match_unmatched_mapped_series(api=None, force=True)
        assert n == 2
        assert [c.args[1] for c in sm.call_args_list] == [1, 2]
        assert [c.args[0] for c in inv.call_args_list] == [1, 2]

    def test_force_invalidates_before_matching(self):
        # Order matters: the DELETE must precede the re-match so rows for issues
        # that no longer exist can't survive the INSERT OR REPLACE.
        calls = []
        rows = [{"id": 7, "name": "Batman"}]
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.get_collection_status_for_series", return_value=None), \
             patch("core.database.invalidate_collection_status_for_series",
                   side_effect=lambda sid: calls.append(("invalidate", sid))), \
             patch.object(library_automap, "_sync_and_match",
                          side_effect=lambda api, sid: calls.append(("match", sid))):
            library_automap.match_unmatched_mapped_series(api=None, force=True)
        assert calls == [("invalidate", 7), ("match", 7)]

    def test_non_force_does_not_invalidate(self):
        rows = [{"id": 1, "name": "Batman"}]
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.get_collection_status_for_series", return_value=None), \
             patch("core.database.invalidate_collection_status_for_series") as inv, \
             patch.object(library_automap, "_sync_and_match"):
            library_automap.match_unmatched_mapped_series(api=None)
        inv.assert_not_called()

    def test_progress_cb_receives_each_series(self):
        seen = []
        rows = [{"id": 1, "name": "Batman"}, {"id": 2, "name": "Saga"}]
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.get_collection_status_for_series", return_value=None), \
             patch("core.database.invalidate_collection_status_for_series"), \
             patch.object(library_automap, "_sync_and_match"):
            library_automap.match_unmatched_mapped_series(
                api=None, force=True,
                progress_cb=lambda c, t, n: seen.append((c, t, n)),
            )
        assert seen == [(0, 2, "Batman"), (1, 2, "Saga")]

    def test_force_label_is_rematch(self):
        rows = [{"id": 1, "name": "Batman"}]
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.get_collection_status_for_series", return_value=None), \
             patch("core.database.invalidate_collection_status_for_series"), \
             patch.object(library_automap, "_sync_and_match"), \
             patch("core.app_state.register_operation", return_value="op1") as reg, \
             patch("core.app_state.update_operation"), \
             patch("core.app_state.complete_operation"):
            library_automap.match_unmatched_mapped_series(api=None, force=True)
        assert reg.call_args.args[1] == "Re-matching library to issues"


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


class TestScanJobResilience:
    """_run_scan_job_inner: a successful scan must stay `done` (issue #436)."""

    def _seed_job(self, op_id):
        library_automap._jobs[op_id] = {
            "id": op_id, "status": "running", "current": 0, "total": 2,
            "detail": "", "result": None, "error": None,
        }

    def test_tail_failure_keeps_job_done(self):
        op_id = "test-tail-fail"
        self._seed_job(op_id)
        scan_result = {
            "auto": [], "review": [], "skipped": [],
            "errors": [{"folder": "/data/Bad", "reason": "boom"}],
            "total_candidates": 2,
        }
        try:
            with patch.object(library_automap.metron, "get_flask_api", return_value=None), \
                 patch.object(library_automap.comicvine, "get_cv_api_key", return_value=None), \
                 patch.object(library_automap, "scan_library_for_automap",
                              return_value=scan_result), \
                 patch.object(library_automap, "apply_automap",
                              return_value={"applied": 1, "failed": [], "applied_ids": [1]}), \
                 patch.object(library_automap, "repair_volume_named_series",
                              side_effect=RuntimeError("rate limited")) as repair, \
                 patch.object(library_automap, "match_unmatched_mapped_series",
                              side_effect=RuntimeError("rate limited")):
                library_automap._run_scan_job_inner(op_id, app=MagicMock())
            job = library_automap._jobs[op_id]
            assert job["status"] == "done"          # NOT flipped to error by the tail
            assert job["error"] is None
            assert job["result"]["applied"] == 1
            # errors from the scan are surfaced in the job result for the UI.
            assert job["result"]["errors"] == scan_result["errors"]
            repair.assert_called_once()             # tail actually ran (and raised)
        finally:
            library_automap._jobs.pop(op_id, None)

    def test_scan_failure_marks_job_error(self):
        # A genuine failure in the scan phase (before the result is stored) still
        # marks the job error, so real problems aren't hidden.
        op_id = "test-scan-fail"
        self._seed_job(op_id)
        try:
            with patch.object(library_automap.metron, "get_flask_api", return_value=None), \
                 patch.object(library_automap.comicvine, "get_cv_api_key", return_value=None), \
                 patch.object(library_automap, "scan_library_for_automap",
                              side_effect=RuntimeError("walk blew up")):
                library_automap._run_scan_job_inner(op_id, app=MagicMock())
            job = library_automap._jobs[op_id]
            assert job["status"] == "error"
            assert "walk blew up" in job["error"]
        finally:
            library_automap._jobs.pop(op_id, None)


class TestRematchJob:
    """_run_rematch_job_inner mirrors the scan job's resilience contract."""

    def _seed_job(self, op_id):
        library_automap._jobs[op_id] = {
            "id": op_id, "status": "running", "current": 0, "total": 0,
            "detail": "", "result": None, "error": None,
        }

    def test_job_done_with_rematched_count(self):
        op_id = "test-rematch-done"
        self._seed_job(op_id)
        try:
            with patch.object(library_automap.metron, "get_flask_api", return_value=None), \
                 patch.object(library_automap, "match_unmatched_mapped_series",
                              return_value=7) as m, \
                 patch.object(library_automap, "_refresh_wanted_cache"):
                library_automap._run_rematch_job_inner(op_id, app=MagicMock())
            assert m.call_args.kwargs["force"] is True
            job = library_automap._jobs[op_id]
            assert job["status"] == "done"
            assert job["result"]["rematched"] == 7
        finally:
            library_automap._jobs.pop(op_id, None)

    def test_wanted_cache_is_rebuilt_on_success(self):
        op_id = "test-rematch-wanted"
        self._seed_job(op_id)
        try:
            with patch.object(library_automap.metron, "get_flask_api", return_value=None), \
                 patch.object(library_automap, "match_unmatched_mapped_series",
                              return_value=1), \
                 patch.object(library_automap, "_refresh_wanted_cache") as refresh:
                library_automap._run_rematch_job_inner(op_id, app=MagicMock())
            refresh.assert_called_once()
        finally:
            library_automap._jobs.pop(op_id, None)

    def test_wanted_rebuild_failure_keeps_job_done(self):
        # The tail runs OUTSIDE the try; a wanted-rebuild blow-up must not
        # discard a completed re-match.
        op_id = "test-rematch-tail-fail"
        self._seed_job(op_id)
        try:
            with patch.object(library_automap.metron, "get_flask_api", return_value=None), \
                 patch.object(library_automap, "match_unmatched_mapped_series",
                              return_value=3), \
                 patch.object(library_automap, "_refresh_wanted_cache",
                              side_effect=RuntimeError("boom")):
                library_automap._run_rematch_job_inner(op_id, app=MagicMock())
            job = library_automap._jobs[op_id]
            assert job["status"] == "done"
            assert job["error"] is None
            assert job["result"]["rematched"] == 3
        finally:
            library_automap._jobs.pop(op_id, None)

    def test_match_failure_marks_job_error(self):
        op_id = "test-rematch-fail"
        self._seed_job(op_id)
        try:
            with patch.object(library_automap.metron, "get_flask_api", return_value=None), \
                 patch.object(library_automap, "match_unmatched_mapped_series",
                              side_effect=RuntimeError("db gone")):
                library_automap._run_rematch_job_inner(op_id, app=MagicMock())
            job = library_automap._jobs[op_id]
            assert job["status"] == "error"
            assert "db gone" in job["error"]
        finally:
            library_automap._jobs.pop(op_id, None)

    def test_start_rematch_job_registers_and_returns_id(self):
        with patch.object(library_automap.threading, "Thread") as T:
            op_id = library_automap.start_rematch_job(MagicMock())
        try:
            assert library_automap._jobs[op_id]["status"] == "running"
            T.assert_called_once()
        finally:
            library_automap._jobs.pop(op_id, None)


class TestCollectionStatusRebuild:
    """rebuild_collection_status_if_empty repairs the cache the one-time
    boundary purge emptied. Without it On the Stack, the Pull List counts and
    the series pages stay blank until every series page is opened by hand."""

    def test_no_op_when_cache_still_has_rows(self):
        with patch("core.database.count_collection_status_rows", return_value=42), \
             patch.object(library_automap, "match_unmatched_mapped_series") as m:
            rebuilt = library_automap.rebuild_collection_status_if_empty(MagicMock())
        assert rebuilt == 0
        m.assert_not_called()

    def test_rebuilds_when_cache_is_empty(self):
        with patch("core.database.count_collection_status_rows", return_value=0), \
             patch.object(library_automap.metron, "get_flask_api", return_value=None), \
             patch.object(library_automap, "match_unmatched_mapped_series",
                          return_value=4) as m, \
             patch.object(library_automap, "_refresh_wanted_cache") as refresh:
            rebuilt = library_automap.rebuild_collection_status_if_empty(MagicMock())
        assert rebuilt == 4
        # Not a forced re-match: the purge already emptied the table, so the
        # plain "series without cached status" worklist covers everything.
        assert m.call_args.kwargs.get("force") is None
        refresh.assert_called_once()

    def test_nothing_to_rebuild_skips_the_wanted_refresh(self):
        with patch("core.database.count_collection_status_rows", return_value=0), \
             patch.object(library_automap.metron, "get_flask_api", return_value=None), \
             patch.object(library_automap, "match_unmatched_mapped_series",
                          return_value=0), \
             patch.object(library_automap, "_refresh_wanted_cache") as refresh:
            rebuilt = library_automap.rebuild_collection_status_if_empty(MagicMock())
        assert rebuilt == 0
        refresh.assert_not_called()

    def test_match_failure_is_swallowed(self):
        # Runs on the startup thread: a failure must never take the app down.
        with patch("core.database.count_collection_status_rows", return_value=0), \
             patch.object(library_automap.metron, "get_flask_api", return_value=None), \
             patch.object(library_automap, "match_unmatched_mapped_series",
                          side_effect=RuntimeError("db gone")):
            rebuilt = library_automap.rebuild_collection_status_if_empty(MagicMock())
        assert rebuilt == 0

    def test_wanted_refresh_failure_keeps_the_rebuild(self):
        with patch("core.database.count_collection_status_rows", return_value=0), \
             patch.object(library_automap.metron, "get_flask_api", return_value=None), \
             patch.object(library_automap, "match_unmatched_mapped_series",
                          return_value=2), \
             patch.object(library_automap, "_refresh_wanted_cache",
                          side_effect=RuntimeError("boom")):
            rebuilt = library_automap.rebuild_collection_status_if_empty(MagicMock())
        assert rebuilt == 2

    def test_start_collection_status_rebuild_spawns_a_daemon_thread(self):
        with patch.object(library_automap.threading, "Thread") as T:
            library_automap.start_collection_status_rebuild(MagicMock())
        T.assert_called_once()
        assert T.call_args.kwargs["daemon"] is True


class TestScanResultRetention:
    """A whole-library scan's review/skipped/errors lists are held in the module
    job store until the *next* job starts -- forever if the user closes the tab.
    Cap what's retained, but report the true totals so nothing looks complete
    when it isn't."""

    def _seed_job(self, op_id):
        library_automap._jobs[op_id] = {
            "id": op_id, "status": "running", "current": 0, "total": 2,
            "detail": "", "result": None, "error": None,
        }

    def _run(self, op_id, scan_result):
        with patch.object(library_automap.metron, "get_flask_api", return_value=None), \
             patch.object(library_automap.comicvine, "get_cv_api_key", return_value=None), \
             patch.object(library_automap, "scan_library_for_automap",
                          return_value=scan_result), \
             patch.object(library_automap, "apply_automap",
                          return_value={"applied": 0, "failed": [], "applied_ids": []}), \
             patch.object(library_automap, "repair_volume_named_series"), \
             patch.object(library_automap, "match_unmatched_mapped_series"):
            library_automap._run_scan_job_inner(op_id, app=MagicMock())
        return library_automap._jobs[op_id]["result"]

    def test_caps_long_lists_and_reports_true_totals(self):
        op_id = "test-cap"
        self._seed_job(op_id)
        cap = library_automap._JOB_LIST_CAP
        oversize = [{"folder": f"/data/S{i}", "reason": "dupe"} for i in range(cap + 250)]
        try:
            result = self._run(op_id, {
                "auto": [], "review": oversize, "skipped": oversize,
                "errors": oversize, "total_candidates": len(oversize),
            })
            for key in ("review", "skipped", "errors"):
                assert len(result[key]) == cap
                assert result[f"{key}_total"] == cap + 250
                assert result[f"{key}_truncated"] is True
        finally:
            library_automap._jobs.pop(op_id, None)

    def test_short_lists_are_untouched_and_not_flagged(self):
        op_id = "test-nocap"
        self._seed_job(op_id)
        items = [{"folder": "/data/A", "reason": "dupe"}]
        try:
            result = self._run(op_id, {
                "auto": [], "review": items, "skipped": [], "errors": [],
                "total_candidates": 1,
            })
            assert result["review"] == items
            assert result["review_total"] == 1
            assert result["review_truncated"] is False
            assert result["skipped_total"] == 0
            assert result["errors_truncated"] is False
        finally:
            library_automap._jobs.pop(op_id, None)

    def test_prune_hard_caps_finished_jobs(self):
        """Backstop in case the TTL sweep is ever bypassed."""
        saved = dict(library_automap._jobs)
        library_automap._jobs.clear()
        try:
            now = time.time()
            for i in range(library_automap._MAX_FINISHED_JOBS + 4):
                # Recent enough that the TTL sweep leaves them alone, so this
                # exercises the hard cap rather than the TTL.
                library_automap._jobs[f"job-{i}"] = {
                    "id": f"job-{i}", "status": "done", "finished_at": now + i,
                    "result": {}, "error": None,
                }
            library_automap._prune_jobs_locked()

            assert len(library_automap._jobs) == library_automap._MAX_FINISHED_JOBS
            # The newest survive.
            assert "job-8" in library_automap._jobs
            assert "job-0" not in library_automap._jobs
        finally:
            library_automap._jobs.clear()
            library_automap._jobs.update(saved)

    def test_prune_keeps_running_jobs(self):
        saved = dict(library_automap._jobs)
        library_automap._jobs.clear()
        try:
            library_automap._jobs["live"] = {
                "id": "live", "status": "running", "result": None, "error": None,
            }
            now = time.time()
            for i in range(library_automap._MAX_FINISHED_JOBS + 3):
                library_automap._jobs[f"job-{i}"] = {
                    "id": f"job-{i}", "status": "done", "finished_at": now + i,
                    "result": {}, "error": None,
                }
            library_automap._prune_jobs_locked()

            assert "live" in library_automap._jobs
        finally:
            library_automap._jobs.clear()
            library_automap._jobs.update(saved)


class TestSweepMemoryHygiene:

    def test_worklist_does_not_retain_full_db_rows(self):
        """Holding every mapped series' full row for the length of a sweep is
        pure overhead -- only the id and name are used."""
        rows = [
            {"id": 1, "name": "Batman", "payload": "x" * 64},
            {"id": 2, "name": "Saga", "payload": "y" * 64},
        ]
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.get_collection_status_for_series", return_value=None), \
             patch.object(library_automap, "_sync_and_match") as sm:
            n = library_automap.match_unmatched_mapped_series(api=None)

        assert n == 2
        assert [c.args[1] for c in sm.call_args_list] == [1, 2]

    def test_falls_back_to_series_id_when_name_missing(self):
        seen = []
        rows = [{"id": 42}]
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.get_collection_status_for_series", return_value=None), \
             patch.object(library_automap, "_sync_and_match"):
            library_automap.match_unmatched_mapped_series(
                api=None, progress_cb=lambda c, t, n: seen.append(n)
            )

        assert seen == ["Series 42"]

    def test_collects_periodically_during_a_long_sweep(self):
        """A long sweep never trips the generational thresholds on its own and
        the background monitor only collects once every five minutes."""
        every = library_automap._GC_EVERY_N_SERIES
        # ids start at 1 -- a 0 id is falsy and gets filtered out of the worklist.
        rows = [{"id": i, "name": f"S{i}"} for i in range(1, every * 2 + 1)]
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.get_collection_status_for_series", return_value=None), \
             patch.object(library_automap, "_sync_and_match"), \
             patch.object(library_automap.gc, "collect") as collect:
            library_automap.match_unmatched_mapped_series(api=None)

        assert collect.call_count == 2

    def test_no_collection_for_a_short_sweep(self):
        rows = [{"id": 1, "name": "Batman"}]
        with patch("core.database.get_all_mapped_series", return_value=rows), \
             patch("core.database.get_collection_status_for_series", return_value=None), \
             patch.object(library_automap, "_sync_and_match"), \
             patch.object(library_automap.gc, "collect") as collect:
            library_automap.match_unmatched_mapped_series(api=None)

        collect.assert_not_called()


class TestComicVineSourceIndependence:
    """ComicVine mapping used to be gated on the API key alone, so a user
    running only the local ComicVine dump had every ComicVine-id sidecar
    skipped. Either source must enable it."""

    def test_resolves_with_local_db_and_no_api_key(self, tmp_path):
        folder = _make_folder(
            tmp_path, "Saga", series_json={"name": "Saga", "comicid": 18705},
        )
        with patch("models.comicvine_source._local_available", return_value=True), \
             patch("models.comicvine_source._api_key", return_value=None):
            available = library_automap.comicvine_source.is_available()
            ident = library_automap._resolve_identity(
                folder, api=None, cv_available=available
            )

        assert available is True
        assert ident["metron_id"] == make_comicvine_series_id(18705)
        assert ident["source"] == "comicvine"

    def test_scan_detects_the_source_when_not_told(self, tmp_path):
        """scan_library_for_automap defaults cv_available rather than requiring
        the caller to know which source is configured."""
        _make_folder(tmp_path, "Saga", series_json={"name": "Saga", "comicid": 18705})
        with patch.object(library_automap, "get_library_roots",
                          return_value=[str(tmp_path)]), \
             patch("core.database.get_all_mapped_series", return_value=[]), \
             patch("models.comicvine_source._local_available", return_value=True), \
             patch("models.comicvine_source._api_key", return_value=None):
            result = library_automap.scan_library_for_automap(api=None)

        assert len(result["auto"]) == 1
        assert result["auto"][0]["metron_id"] == make_comicvine_series_id(18705)

    def test_scan_skips_comicvine_ids_when_no_source_configured(self, tmp_path):
        _make_folder(tmp_path, "Saga", series_json={"name": "Saga", "comicid": 18705})
        with patch.object(library_automap, "get_library_roots",
                          return_value=[str(tmp_path)]), \
             patch("core.database.get_all_mapped_series", return_value=[]), \
             patch("models.comicvine_source._local_available", return_value=False), \
             patch("models.comicvine_source._api_key", return_value=None):
            result = library_automap.scan_library_for_automap(api=None)

        assert not result["auto"]
        assert len(result["skipped"]) == 1

    def test_issue_sync_uses_the_local_db_without_an_api_key(self):
        """The per-volume issue fetch is the most expensive call in a sweep;
        answering it from the dump spends no API budget at all."""
        cv_issues = [{"id": 1, "cv_id": 500, "number": "1"}]
        with patch("models.comicvine_source._local_available", return_value=True), \
             patch("models.comicvine_source._api_key", return_value=None), \
             patch("models.comicvine_sqlite.get_all_issues_for_volume",
                   return_value=cv_issues) as local, \
             patch("models.comicvine.get_all_issues_for_volume") as api, \
             patch("core.database.delete_issues_for_series"), \
             patch("core.database.save_issues_bulk") as save, \
             patch("core.database.update_series_sync_time"):
            library_automap._sync_comicvine_issues(make_comicvine_series_id(18705))

        local.assert_called_once_with(18705)
        api.assert_not_called()
        assert save.call_args.args[0] == cv_issues

    def test_issue_sync_no_ops_without_any_comicvine_source(self):
        with patch("models.comicvine_source._local_available", return_value=False), \
             patch("models.comicvine_source._api_key", return_value=None), \
             patch("core.database.save_issues_bulk") as save:
            library_automap._sync_comicvine_issues(make_comicvine_series_id(18705))

        save.assert_not_called()
