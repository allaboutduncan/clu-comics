"""Tests for schedules, weekly packs, browse cache, user preferences, reading lists."""
import pytest
import json
from tests.factories.db_factories import create_reading_list, create_reading_list_entry, create_user_preference


class TestUnifiedSchedules:

    def test_get_schedule(self, db_connection):
        from database import get_schedule

        sched = get_schedule("rebuild")
        assert sched is not None
        assert sched["frequency"] == "disabled"

    def test_save_schedule(self, db_connection):
        from database import save_schedule, get_schedule

        save_schedule("rebuild", frequency="daily", time="04:00", weekday=0)

        sched = get_schedule("rebuild")
        assert sched["frequency"] == "daily"
        assert sched["time"] == "04:00"

    def test_update_last_run(self, db_connection):
        from database import update_schedule_last_run, get_schedule

        update_schedule_last_run("rebuild")
        sched = get_schedule("rebuild")
        assert sched["last_run"] is not None

    def test_all_schedule_names_exist(self, db_connection):
        from database import get_schedule

        for name in ["rebuild", "sync", "getcomics", "weekly_packs", "komga"]:
            sched = get_schedule(name)
            assert sched is not None, f"Schedule '{name}' not found"


class TestLegacySchedules:

    def test_get_rebuild_schedule(self, db_connection):
        from database import get_rebuild_schedule

        sched = get_rebuild_schedule()
        assert sched is not None

    def test_save_rebuild_schedule(self, db_connection):
        from database import save_rebuild_schedule, get_rebuild_schedule

        save_rebuild_schedule("weekly", "03:00", weekday=1)
        sched = get_rebuild_schedule()
        assert sched["frequency"] == "weekly"

    def test_sync_schedule(self, db_connection):
        from database import get_sync_schedule, save_sync_schedule

        save_sync_schedule("daily", "06:00")
        sched = get_sync_schedule()
        assert sched["frequency"] == "daily"

    def test_getcomics_schedule(self, db_connection):
        from database import get_getcomics_schedule, save_getcomics_schedule

        save_getcomics_schedule("daily", "08:00")
        sched = get_getcomics_schedule()
        assert sched["frequency"] == "daily"


class TestWeeklyPacks:

    def test_get_default_config(self, db_connection):
        from database import get_weekly_packs_config

        config = get_weekly_packs_config()
        assert config is not None
        assert config["enabled"] == 0
        assert config["format"] == "JPG"

    def test_save_config(self, db_connection):
        from database import save_weekly_packs_config, get_weekly_packs_config

        ok = save_weekly_packs_config(
            enabled=True,
            format_pref="CBZ",
            publishers=["DC", "Marvel"],
            weekday=3,
            time="12:00",
            retry_enabled=True,
        )
        assert ok is True

        config = get_weekly_packs_config()
        assert config["enabled"] == 1
        assert config["format"] == "CBZ"
        assert config["weekday"] == 3

    def test_log_download(self, db_connection):
        from database import log_weekly_pack_download, get_weekly_packs_history

        ok = log_weekly_pack_download(
            pack_date="2024-01-15",
            publisher="DC",
            format_pref="CBZ",
            download_url="https://example.com/pack.cbz",
        )
        assert ok is True

        history = get_weekly_packs_history()
        assert len(history) >= 1
        assert history[0]["publisher"] == "DC"

    def test_update_pack_status(self, db_connection):
        from database import (
            log_weekly_pack_download,
            update_weekly_pack_status,
            is_weekly_pack_downloaded,
        )

        log_weekly_pack_download("2024-01-15", "Marvel", "JPG", "https://example.com")
        update_weekly_pack_status("2024-01-15", "Marvel", "JPG", "completed")
        assert is_weekly_pack_downloaded("2024-01-15", "Marvel", "JPG") is True

    def test_not_downloaded_by_default(self, db_connection):
        from database import is_weekly_pack_downloaded

        assert is_weekly_pack_downloaded("2099-01-01", "DC", "CBZ") is False


class TestBrowseCache:

    def test_save_and_get(self, db_connection):
        from database import save_browse_cache, get_browse_cache

        data = {"files": ["a.cbz", "b.cbz"], "count": 2}
        save_browse_cache("/data/DC", data)

        cached = get_browse_cache("/data/DC")
        assert cached is not None
        # cached may be a JSON string or already-parsed dict depending on impl
        if isinstance(cached, str):
            parsed = json.loads(cached)
        else:
            parsed = cached
        assert parsed["count"] == 2

    def test_invalidate(self, db_connection):
        from database import save_browse_cache, invalidate_browse_cache, get_browse_cache

        save_browse_cache("/data/X", {"test": True})
        invalidate_browse_cache("/data/X")
        assert get_browse_cache("/data/X") is None

    def test_clear_all(self, db_connection):
        from database import save_browse_cache, clear_browse_cache, get_browse_cache

        save_browse_cache("/data/A", {"a": 1})
        save_browse_cache("/data/B", {"b": 2})

        clear_browse_cache()
        assert get_browse_cache("/data/A") is None
        assert get_browse_cache("/data/B") is None


class TestUserPreferences:

    def test_set_and_get(self, db_connection):
        from database import set_user_preference, get_user_preference

        set_user_preference("theme", "darkly", category="ui")
        assert get_user_preference("theme") == "darkly"

    def test_default_value(self, db_connection):
        from database import get_user_preference

        result = get_user_preference("nonexistent", default="fallback")
        assert result == "fallback"

    def test_overwrite(self, db_connection):
        from database import set_user_preference, get_user_preference

        set_user_preference("key", "old")
        set_user_preference("key", "new")
        assert get_user_preference("key") == "new"


class TestStatsCache:

    def test_save_and_get(self, db_connection):
        from database import save_cached_stats, get_cached_stats

        save_cached_stats("reading_stats", {"total": 100})
        cached = get_cached_stats("reading_stats")
        assert cached is not None

    def test_clear_all(self, db_connection):
        from database import save_cached_stats, clear_stats_cache, get_cached_stats

        save_cached_stats("key1", "val1")
        clear_stats_cache()
        assert get_cached_stats("key1") is None

    def test_clear_specific_keys(self, db_connection):
        from database import save_cached_stats, clear_stats_cache_keys, get_cached_stats

        save_cached_stats("keep", "yes")
        save_cached_stats("remove", "no")

        clear_stats_cache_keys(["remove"])
        assert get_cached_stats("keep") is not None
        assert get_cached_stats("remove") is None


class TestReadingLists:

    def test_create_and_get(self, db_connection):
        from database import get_reading_lists

        list_id = create_reading_list(name="DC Essentials")
        create_reading_list_entry(list_id, series="Batman", issue_number="1")
        create_reading_list_entry(list_id, series="Superman", issue_number="1")

        lists = get_reading_lists()
        assert len(lists) >= 1
        dc_list = next((l for l in lists if l["name"] == "DC Essentials"), None)
        assert dc_list is not None

    def test_get_single_list(self, db_connection):
        from database import get_reading_list

        list_id = create_reading_list(name="My List")
        create_reading_list_entry(list_id, series="X-Men", issue_number="1")

        result = get_reading_list(list_id)
        assert result is not None
        assert result["name"] == "My List"

    def test_delete_list(self, db_connection):
        from database import delete_reading_list, get_reading_list

        list_id = create_reading_list(name="Delete Me")
        delete_reading_list(list_id)
        assert get_reading_list(list_id) is None

    def test_update_name(self, db_connection):
        from database import update_reading_list_name, get_reading_list

        list_id = create_reading_list(name="Old Name")
        update_reading_list_name(list_id, "New Name")

        result = get_reading_list(list_id)
        assert result["name"] == "New Name"

    def test_update_thumbnail(self, db_connection):
        from database import update_reading_list_thumbnail

        list_id = create_reading_list()
        ok = update_reading_list_thumbnail(list_id, "/path/to/thumb.jpg")
        assert ok is not False  # Could be True or None depending on impl

    def test_tags(self, db_connection):
        from database import update_reading_list_tags, get_all_reading_list_tags

        list_id = create_reading_list()
        update_reading_list_tags(list_id, ["dc", "batman"])

        tags = get_all_reading_list_tags()
        assert "dc" in tags
        assert "batman" in tags


class TestLibraryCRUD:

    def test_add_and_get(self, db_connection):
        from database import add_library, get_libraries

        lib_id = add_library("Comics", "/data/comics")
        assert lib_id is not None

        libs = get_libraries(enabled_only=False)
        assert any(l["id"] == lib_id for l in libs)

    def test_get_by_id(self, db_connection):
        from database import add_library, get_library_by_id

        lib_id = add_library("Test Lib", "/data/test")
        lib = get_library_by_id(lib_id)
        assert lib is not None
        assert lib["name"] == "Test Lib"

    def test_update_library(self, db_connection):
        from database import add_library, update_library, get_library_by_id

        lib_id = add_library("OldName", "/data/old")
        update_library(lib_id, name="NewName")

        lib = get_library_by_id(lib_id)
        assert lib["name"] == "NewName"

    def test_delete_library(self, db_connection):
        from database import add_library, delete_library, get_library_by_id

        lib_id = add_library("DeleteMe", "/data/delete")
        delete_library(lib_id)
        assert get_library_by_id(lib_id) is None

    def test_duplicate_path_returns_none(self, db_connection):
        from database import add_library

        add_library("Lib1", "/data/same")
        result = add_library("Lib2", "/data/same")
        assert result is None
