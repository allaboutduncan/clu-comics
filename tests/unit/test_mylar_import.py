"""models.mylar_import: path mapping, preview, settings and the series import."""

import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.mylar_reader import MylarReadError
from models import mylar_import


@pytest.fixture
def roots(tmp_path):
    a = tmp_path / "lib_a"
    b = tmp_path / "lib_b"
    a.mkdir()
    b.mkdir()
    with patch.object(mylar_import, "get_library_roots", return_value=[str(a), str(b)]):
        yield str(a), str(b)


@pytest.fixture
def cache(tmp_path):
    folder = tmp_path / "cache"
    folder.mkdir()
    with patch.object(mylar_import, "get_cache_dir", return_value=str(folder)):
        yield str(folder)


def series(cv_id, name, location, status="Active", issues=None, year=2016):
    return {"cv_id": cv_id, "name": name, "year": year, "publisher": "DC",
            "location": location, "status": status, "issues": issues or []}


def record(watchlist_series, mapping, config=None):
    return {"watchlist": {"series": watchlist_series, "excluded": [], "annual_count": 0},
            "config": config, "mapping": mapping}


# ---------------------------------------------------------------------------
# Path mapping
# ---------------------------------------------------------------------------

class TestDetectPathMapping:
    def test_picks_the_root_holding_the_most_folders(self, roots):
        a, b = roots
        os.makedirs(os.path.join(b, "DC", "Batman (2016)"))
        os.makedirs(os.path.join(b, "Saga"))
        os.makedirs(os.path.join(a, "Saga"))
        mapping = mylar_import.detect_path_mapping([
            series(1, "Batman", "/comics/DC/Batman (2016)"),
            series(2, "Saga", "/comics/Saga"),
            series(3, "New", "/comics/New"),
        ], "/comics/")
        assert mapping == [{"mylar_root": "/comics", "clu_root": b, "found": 2, "total": 3}]

    def test_no_hits_proposes_the_first_root(self, roots):
        a, _ = roots
        mapping = mylar_import.detect_path_mapping([series(1, "X", "/comics/X")], "/comics")
        assert mapping[0]["clu_root"] == a
        assert mapping[0]["found"] == 0

    def test_locations_outside_destination_are_grouped_by_parent(self, roots):
        _, b = roots
        os.makedirs(os.path.join(b, "Manga"))
        mapping = mylar_import.detect_path_mapping([
            series(1, "Batman", "/comics/Batman"),
            series(2, "Manga", "/other/Manga"),
        ], "/comics")
        by_root = {m["mylar_root"]: m for m in mapping}
        assert by_root["/other"]["clu_root"] == b
        assert by_root["/other"]["found"] == 1

    def test_windows_mylar_paths(self, roots):
        _, b = roots
        os.makedirs(os.path.join(b, "Batman"))
        mapping = mylar_import.detect_path_mapping(
            [series(1, "Batman", "D:\\Comics\\Batman")], "D:\\Comics"
        )
        assert mapping[0] == {"mylar_root": "D:/Comics", "clu_root": b, "found": 1, "total": 1}


class TestTranslateLocation:
    @pytest.mark.skipif(os.name == "nt", reason="':' cannot appear in a Windows folder name")
    def test_existing_folder_keeps_its_literal_name(self, roots):
        a, _ = roots
        os.makedirs(os.path.join(a, "Batman: Year One"))
        mapping = [{"mylar_root": "/comics", "clu_root": a}]
        assert mylar_import.translate_location("/comics/Batman: Year One", mapping) == \
            os.path.join(a, "Batman: Year One")

    def test_new_folder_is_sanitized(self, roots):
        a, _ = roots
        mapping = [{"mylar_root": "/comics", "clu_root": a}]
        path = mylar_import.translate_location("/comics/Batman: Year Two", mapping)
        assert path == os.path.join(a, "Batman - Year Two")

    def test_longest_root_wins(self, roots):
        a, b = roots
        mapping = [{"mylar_root": "/comics", "clu_root": a},
                   {"mylar_root": "/comics/Manga", "clu_root": b}]
        assert mylar_import.translate_location("/comics/Manga/Berserk", mapping) == \
            os.path.join(b, "Berserk")

    def test_unmapped(self, roots):
        assert mylar_import.translate_location("/elsewhere/X", [
            {"mylar_root": "/comics", "clu_root": roots[0]}]) is None
        assert mylar_import.translate_location(None, []) is None


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

class TestSeriesRows:
    def test_states(self, db_connection, roots, tmp_path):
        from core.database import save_series_mapping

        a, _ = roots
        os.makedirs(os.path.join(a, "Exists"))
        os.makedirs(os.path.join(a, "Taken"))
        save_series_mapping({"id": 900, "name": "Mapped", "cv_id": 10}, os.path.join(a, "Mapped"))
        save_series_mapping({"id": 901, "name": "Other", "cv_id": 99}, os.path.join(a, "Taken"))

        rec = record([
            series(10, "Mapped", "/comics/Mapped"),
            series(11, "Exists", "/comics/Exists", status="Paused",
                   issues=[{"number": "1", "status": "Skipped"},
                           {"number": "2", "status": "Archived"},
                           {"number": "3", "status": "Wanted"}]),
            series(12, "New", "/comics/New"),
            series(13, "Taken", "/comics/Taken"),
            series(14, "Dupe", "/comics/New"),
            series(15, "Elsewhere", "/nowhere/X"),
            series(16, "NoLocation", None),
        ], [{"mylar_root": "/comics", "clu_root": a}])

        rows = {r["name"]: r for r in mylar_import.series_rows(rec)}
        assert rows["Mapped"]["state"] == "already_mapped"
        assert rows["Exists"]["state"] == "exists"
        assert rows["Exists"]["paused"] is True
        assert (rows["Exists"]["skipped_count"], rows["Exists"]["owned_count"]) == (1, 1)
        assert rows["New"]["state"] == "will_create"
        assert rows["Taken"]["state"] == "path_taken"
        assert rows["Dupe"]["state"] == "duplicate"
        assert rows["Elsewhere"]["state"] == "invalid"
        assert rows["NoLocation"]["state"] == "invalid"

    def test_offset_comicvine_series_counts_as_mapped(self, db_connection, roots):
        from core.database import save_series_mapping
        from helpers.comicvine_ids import make_comicvine_series_id

        a, _ = roots
        save_series_mapping({"id": make_comicvine_series_id(55), "name": "CV"},
                            os.path.join(a, "CV"))
        rec = record([series(55, "CV", "/comics/CV2")], [{"mylar_root": "/comics", "clu_root": a}])
        assert mylar_import.series_rows(rec)[0]["state"] == "already_mapped"


SAB = {"host": "sab", "port": 8080, "use_ssl": False, "url_base": None,
       "api_key": "k", "username": None, "password": None, "category": "comics",
       "priority": None}


def config(**overrides):
    base = {"comicvine_api": "cvkey", "nzb_downloader": "sabnzbd", "sabnzbd": dict(SAB),
            "nzbget": None,
            "newznabs": [{"name": "Geek", "url": "https://api.nzbgeek.info", "api_key": "gk",
                          "categories": "7030", "enabled": True}],
            "torznab_count": 0}
    base.update(overrides)
    return base


class TestSettingsPreview:
    def test_everything_new(self, db_connection):
        preview = mylar_import.settings_preview(config())
        assert preview["comicvine"] == {"state": "new"}
        assert preview["client"]["state"] == "new"
        assert preview["client"]["has_api_key"] is True
        assert "api_key" not in preview["client"]
        assert preview["indexers"][0]["state"] == "new"
        assert "api_key" not in preview["indexers"][0]

    def test_same_and_differs(self, db_connection):
        from core.database import add_indexer, save_download_client_config, save_provider_credentials

        save_provider_credentials("comicvine", {"api_key": "other"})
        save_download_client_config("sabnzbd", dict(SAB))
        add_indexer("Geek", "https://api.nzbgeek.info/api/", {"api_key": "gk", "categories": "7030"})

        preview = mylar_import.settings_preview(config())
        assert preview["comicvine"]["state"] == "differs"
        assert preview["client"]["state"] == "same"
        # De-duplicated by URL, ignoring the scheme, a trailing slash and /api.
        assert preview["indexers"][0]["state"] == "same"
        assert preview["indexers"][0]["existing_id"]

        changed = config(newznabs=[dict(config()["newznabs"][0], api_key="new")],
                         sabnzbd=dict(SAB, port=9090))
        preview = mylar_import.settings_preview(changed)
        assert preview["client"]["state"] == "differs"
        assert preview["indexers"][0]["state"] == "differs"


class TestApplySettings:
    def test_writes_tests_and_enables_usenet(self, db_connection):
        from core.database import (
            get_active_download_client,
            get_all_indexers,
            get_download_client_config,
            get_provider_credentials,
            get_user_preference,
        )

        client = MagicMock()
        client.test_connection.return_value = True
        indexer = MagicMock()
        indexer.test_connection.return_value = False
        indexer.last_error = "HTTP 401"
        with patch("models.download_clients.get_download_client_by_name", return_value=client), \
                patch("models.indexers.get_indexer_impl", return_value=indexer), \
                patch("core.config.load_flask_config") as reload_config:
            report = mylar_import.apply_settings(
                SimpleNamespace(), config(), {"comicvine": True, "client": True, "indexers": [0]}
            )

        assert get_provider_credentials("comicvine")["api_key"] == "cvkey"
        reload_config.assert_called_once()
        assert get_download_client_config("sabnzbd")["host"] == "sab"
        assert get_active_download_client("usenet")["client_type"] == "sabnzbd"
        assert report["client"] == {"type": "sabnzbd", "saved": True, "valid": True,
                                    "error": None, "activated": True}
        assert [i["name"] for i in get_all_indexers()] == ["Geek"]
        assert report["indexers"][0]["valid"] is False
        assert report["indexers"][0]["error"] == "HTTP 401"
        assert report["source_priority"] == ["getcomics", "usenet"]
        assert get_user_preference("download_source_priority") == ["getcomics", "usenet"]

    def test_unticked_settings_are_left_alone(self, db_connection):
        from core.database import get_all_indexers, get_provider_credentials

        report = mylar_import.apply_settings(
            SimpleNamespace(), config(), {"comicvine": False, "client": False, "indexers": []}
        )
        assert get_provider_credentials("comicvine") is None
        assert get_all_indexers() == []
        assert report["source_priority"] is None

    def test_existing_active_client_is_not_replaced(self, db_connection):
        from core.database import get_active_download_client, save_download_client_config, set_active_download_client

        save_download_client_config("nzbget", {"host": "nzbget"})
        set_active_download_client("nzbget")
        client = MagicMock()
        client.test_connection.return_value = True
        with patch("models.download_clients.get_download_client_by_name", return_value=client):
            report = mylar_import.apply_settings(
                SimpleNamespace(), config(), {"client": True}
            )
        assert report["client"]["activated"] is False
        assert get_active_download_client("usenet")["client_type"] == "nzbget"


# ---------------------------------------------------------------------------
# Series import
# ---------------------------------------------------------------------------

def fake_apply_automap(items, api=None):
    from core.database import save_series_mapping

    item = items[0]
    save_series_mapping({"id": item["metron_id"], "name": item["series_name"],
                         "cv_id": item["cv_id"]}, item["folder"])
    return {"applied": 1, "failed": [], "applied_ids": [item["metron_id"]]}


def fake_sync(api, series_id):
    from core.database import get_db_connection

    conn = get_db_connection()
    conn.executemany(
        "INSERT INTO issues (id, series_id, number) VALUES (?, ?, ?)",
        [(series_id * 10 + n, series_id, num) for n, num in enumerate(["1", "2", "3", "4.MU"])],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def metron_env():
    """Metron and ComicVine patched; ``env.lookup`` maps cv_id -> Metron id."""
    env = SimpleNamespace(lookup={}, cv_available=True, daily_limit=False, blocked=False)
    with patch("models.metron.get_flask_api", return_value=object()), \
            patch("models.metron.get_series_id_by_comicvine_id",
                  side_effect=lambda api, cv: env.lookup.get(cv)), \
            patch("models.metron.auth_blocked", side_effect=lambda: env.blocked), \
            patch("models.metron._metron_pacer") as pacer, \
            patch("models.comicvine_source.is_available", side_effect=lambda app=None: env.cv_available), \
            patch("models.library_automap.apply_automap", side_effect=fake_apply_automap), \
            patch("models.library_automap._sync_and_match", side_effect=fake_sync), \
            patch.object(mylar_import, "_refresh_wanted_cache") as refresh:
        pacer.daily_limit_reached.side_effect = lambda: env.daily_limit
        env.refresh = refresh
        yield env


def row(cv_id, name, path):
    return {"cv_id": cv_id, "name": name, "year": 2016, "publisher": "DC", "path": path}


class TestImportSeries:
    def test_maps_creates_marks_and_pauses(self, db_connection, roots, metron_env):
        from core.database import (
            get_manual_status_for_series,
            get_series_mapping,
            get_series_monitored,
            set_manual_status,
        )

        a, _ = roots
        metron_env.lookup = {10: 500}
        path = os.path.join(a, "Batman")
        mylar = {10: series(10, "Batman", "/comics/Batman", status="Paused", issues=[
            {"number": "001", "status": "Skipped"},
            {"number": "2", "status": "Archived"},
            {"number": "3", "status": "Ignored"},
            {"number": "4.mu", "status": "Skipped"},
            {"number": "5", "status": "Wanted"},
        ])}

        # A mark the user set in CLU before importing wins over Mylar's.
        def sync_then_mark(api, sid):
            fake_sync(api, sid)
            set_manual_status(sid, "3", "owned")

        with patch("models.library_automap._sync_and_match", side_effect=sync_then_mark):
            out = mylar_import.import_series(None, [row(10, "Batman", path)], mylar)

        assert os.path.isdir(path)
        assert out["folders_created"] == 1
        assert get_series_mapping(500) == path
        assert out["mapped"][0]["source"] == "metron"
        marks = {n: v["status"] for n, v in get_manual_status_for_series(500).items()}
        assert marks == {"1": "skipped", "2": "owned", "3": "owned", "4.MU": "skipped"}
        assert get_series_monitored(500) is False
        metron_env.refresh.assert_called_once()

    def test_comicvine_fallback_and_unresolved(self, db_connection, roots, metron_env):
        from helpers.comicvine_ids import make_comicvine_series_id

        a, _ = roots
        out = mylar_import.import_series(None, [row(20, "CV Only", os.path.join(a, "CV"))], {})
        assert out["mapped"][0]["series_id"] == make_comicvine_series_id(20)
        assert out["mapped"][0]["source"] == "comicvine"

        metron_env.cv_available = False
        out = mylar_import.import_series(None, [row(21, "Nowhere", os.path.join(a, "N"))], {})
        assert out["mapped"] == []
        assert "ComicVine is not configured" in out["unresolved"][0]["reason"]

    def test_rate_limit_defers_the_rest(self, db_connection, roots, metron_env):
        a, _ = roots
        metron_env.lookup = {30: 600}

        calls = []

        def lookup(api, cv):
            calls.append(cv)
            if cv == 31:
                metron_env.daily_limit = True  # this call tripped the limit
            return metron_env.lookup.get(cv)

        with patch("models.metron.get_series_id_by_comicvine_id", side_effect=lookup):
            out = mylar_import.import_series(None, [
                row(30, "A", os.path.join(a, "A")),
                row(31, "B", os.path.join(a, "B")),
                row(32, "C", os.path.join(a, "C")),
            ], {})
        assert [m["cv_id"] for m in out["mapped"]] == [30]
        # Never misfiled as a ComicVine-only series.
        assert [d["cv_id"] for d in out["deferred"]] == [31, 32]
        assert calls == [30, 31]
        assert not os.path.exists(os.path.join(a, "B"))

    def test_resolved_series_mapped_elsewhere_is_skipped(self, db_connection, roots, metron_env):
        from core.database import get_series_mapping, save_series_mapping

        a, _ = roots
        save_series_mapping({"id": 700, "name": "Kept"}, os.path.join(a, "Kept"))
        metron_env.lookup = {40: 700}
        out = mylar_import.import_series(None, [row(40, "Kept", os.path.join(a, "Other"))], {})
        assert out["skipped"][0]["cv_id"] == 40
        assert get_series_mapping(700) == os.path.join(a, "Kept")
        metron_env.refresh.assert_not_called()


class TestComicVineWait:
    """A ComicVine-only series waits out the hourly budget visibly, not silently."""

    def test_waits_with_progress_then_resumes(self):
        from helpers.rate_limit import SlidingWindowRateLimiter

        limiter = SlidingWindowRateLimiter(3, 3600.0)
        for _ in range(3):
            limiter.acquire()
        details = []
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                limiter.reset()  # the hour passes

        with patch("models.comicvine._comicvine_rate_limiter", limiter), \
                patch("models.comicvine_source._local_available", return_value=False), \
                patch.object(mylar_import, "_sleep", side_effect=fake_sleep), \
                patch.object(mylar_import, "_progress",
                             side_effect=lambda op, c, t, d: details.append(d)):
            mylar_import._wait_for_comicvine("op", 4, 10, "Saga (2012)")

        assert len(sleeps) == 2
        assert all(s <= mylar_import._WAIT_POLL_SECONDS for s in sleeps)
        assert all("ComicVine's hourly request limit" in d for d in details)
        assert "Saga (2012)" in details[0]

    def test_no_wait_with_the_local_dump_or_free_slots(self):
        from helpers.rate_limit import SlidingWindowRateLimiter

        full = SlidingWindowRateLimiter(1, 3600.0)
        full.acquire()
        with patch("models.comicvine._comicvine_rate_limiter", full), \
                patch("models.comicvine_source._local_available", return_value=True), \
                patch.object(mylar_import, "_sleep") as sleep:
            mylar_import._wait_for_comicvine(None, 0, 1, "X")
        sleep.assert_not_called()

        with patch("models.comicvine._comicvine_rate_limiter", SlidingWindowRateLimiter(9, 3600.0)), \
                patch("models.comicvine_source._local_available", return_value=False), \
                patch.object(mylar_import, "_sleep") as sleep:
            mylar_import._wait_for_comicvine(None, 0, 1, "X")
        sleep.assert_not_called()

    def test_only_comicvine_series_wait(self, db_connection, roots, metron_env):
        a, _ = roots
        metron_env.lookup = {50: 800}
        with patch.object(mylar_import, "_wait_for_comicvine") as wait:
            mylar_import.import_series(None, [
                row(50, "On Metron", os.path.join(a, "M")),
                row(51, "CV Only", os.path.join(a, "C")),
            ], {})
        assert wait.call_count == 1
        assert wait.call_args[0][3] == "CV Only (2016)"


class TestLatestJob:
    def test_prefers_the_running_job(self):
        with patch.dict(mylar_import._jobs, clear=True):
            assert mylar_import.latest_job() is None
            mylar_import._jobs["old"] = {"id": "old", "status": "done", "started_at": 1,
                                        "finished_at": time.time()}
            mylar_import._jobs["new"] = {"id": "new", "status": "done", "started_at": 2,
                                        "finished_at": time.time()}
            assert mylar_import.latest_job()["id"] == "new"
            mylar_import._jobs["run"] = {"id": "run", "status": "running", "started_at": 0}
            assert mylar_import.latest_job()["id"] == "run"


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

class TestStaging:
    def _write_ini(self, folder):
        with open(os.path.join(folder, "config.ini"), "w", encoding="utf-8") as f:
            f.write("[CV]\ncomicvine_api = abc\n")

    def test_staged_files_are_deleted_once_parsed(self, db_connection, cache, roots):
        import_id, folder = mylar_import.new_import()
        self._write_ini(folder)
        rec = mylar_import.load_import(import_id)
        assert rec["config"]["comicvine_api"] == "abc"
        assert os.listdir(folder) == []
        mylar_import.discard_import(import_id)
        assert not os.path.exists(folder)
        assert mylar_import.get_import(import_id) is None

    def test_drop_folder_files_are_copied_not_moved(self, db_connection, cache, roots):
        drop = mylar_import.drop_folder()
        os.makedirs(drop)
        self._write_ini(drop)
        assert mylar_import.drop_folder_status()["config_ini"] is True

        import_id, folder = mylar_import.new_import()
        assert mylar_import.stage_from_drop_folder(folder) == ["config.ini"]
        mylar_import.load_import(import_id)
        assert os.path.isfile(os.path.join(drop, "config.ini"))
        mylar_import.discard_import(import_id)

    def test_bad_file_discards_the_import(self, cache):
        import_id, folder = mylar_import.new_import()
        with open(os.path.join(folder, "mylar.db"), "wb") as f:
            f.write(b"not sqlite" * 50)
        with pytest.raises(MylarReadError):
            mylar_import.load_import(import_id)
        assert mylar_import.get_import(import_id) is None
        assert not os.path.exists(folder)

    def test_expired_imports_are_swept(self, cache):
        import_id, folder = mylar_import.new_import()
        with mylar_import._imports_lock:
            mylar_import._imports[import_id]["created"] -= mylar_import.IMPORT_TTL + 1
        assert mylar_import.get_import(import_id) is None
        assert not os.path.exists(folder)
