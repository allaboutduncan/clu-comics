"""Tests for models/usenet.py -- search/score, grab, import, source priority."""
import pytest
from unittest.mock import MagicMock, patch

import models.usenet as un
from models.indexers import NZBSearchResult
from models.download_clients import NZBSubmitResult, DownloadStatus


class TestSourcePriority:

    @patch("core.database.get_user_preference", return_value=None)
    def test_default(self, mock_pref):
        assert un.get_source_priority() == ["getcomics"]

    @patch("core.database.get_user_preference", return_value='["usenet","getcomics"]')
    def test_parsed_json_string(self, mock_pref):
        assert un.get_source_priority() == ["usenet", "getcomics"]
        assert un.usenet_precedes_getcomics() is True

    @patch("core.database.get_user_preference", return_value=["getcomics", "usenet"])
    def test_list_value(self, mock_pref):
        assert un.usenet_precedes_getcomics() is False


class TestUsenetConfigured:

    @patch("core.database.get_enabled_indexers", return_value=[{"id": 1}])
    @patch("core.database.get_active_download_client", return_value={"client_type": "sabnzbd", "config": {}})
    @patch("core.database.get_user_preference", return_value='["usenet"]')
    def test_configured(self, mock_pref, mock_active, mock_idx):
        assert un.usenet_enabled_and_configured() is True

    @patch("core.database.get_enabled_indexers", return_value=[])
    @patch("core.database.get_active_download_client", return_value={"client_type": "sabnzbd", "config": {}})
    @patch("core.database.get_user_preference", return_value='["usenet"]')
    def test_no_indexer(self, mock_pref, mock_active, mock_idx):
        assert un.usenet_enabled_and_configured() is False

    @patch("core.database.get_user_preference", return_value='["getcomics"]')
    def test_usenet_not_a_source(self, mock_pref):
        assert un.usenet_enabled_and_configured() is False


class TestBuildQueries:

    def test_pads_numeric_issue(self):
        qs = un._build_queries("Treehouse of Horror", "4")
        assert qs == ["Treehouse of Horror 4", "Treehouse of Horror 04",
                      "Treehouse of Horror 004"]

    def test_non_numeric_issue(self):
        qs = un._build_queries("Batman", "1.MU")
        assert qs == ["Batman 1.MU"]


class TestSearchAndScore:

    @patch("models.indexers.get_indexer_impl")
    @patch("core.database.get_enabled_indexers", return_value=[
        {"id": 5, "name": "NZBgeek", "url": "https://x", "api_key": "k",
         "categories": None, "enabled": True, "indexer_type": "newznab"},
    ])
    def test_finds_and_scores_match(self, mock_idx, mock_impl):
        impl = MagicMock()
        impl.search.return_value = [
            NZBSearchResult(indexer_id=5, indexer_name="NZBgeek",
                            title="Batman 001 (2020)",
                            nzb_url="https://x/getnzb/a.nzb", size=100),
        ]
        mock_impl.return_value = impl
        res = un.search_usenet_for_issue("Batman", "1", issue_year=2020)
        assert res["chosen"] is not None
        assert res["chosen"][0].nzb_url == "https://x/getnzb/a.nzb"
        assert res["all_results"][0]["decision"] == "ACCEPT"

    @patch("models.indexers.get_indexer_impl")
    @patch("core.database.get_enabled_indexers", return_value=[
        {"id": 5, "name": "NZBgeek", "url": "https://x", "api_key": "k",
         "categories": None, "enabled": True, "indexer_type": "newznab"},
    ])
    def test_no_match(self, mock_idx, mock_impl):
        impl = MagicMock()
        impl.search.return_value = [
            NZBSearchResult(indexer_id=5, indexer_name="NZBgeek",
                            title="Completely Different Comic 99",
                            nzb_url="https://x/z.nzb"),
        ]
        mock_impl.return_value = impl
        res = un.search_usenet_for_issue("Batman", "1", issue_year=2020)
        assert res["chosen"] is None

    @patch("models.indexers.get_indexer_impl")
    @patch("core.database.get_enabled_indexers", return_value=[
        {"id": 5, "name": "NZBgeek", "url": "https://x", "api_key": "k",
         "categories": None, "enabled": True, "indexer_type": "newznab"},
    ])
    def test_wrong_bare_issue_rejected(self, mock_idx, mock_impl):
        """A bare wrong issue (001 while wanting #2) must not be chosen.

        Regression for the reported bug: this exact title was auto-downloaded
        for a wanted issue #002 purely on series+year.
        """
        impl = MagicMock()
        impl.search.return_value = [
            NZBSearchResult(
                indexer_id=5, indexer_name="NZBgeek",
                title="Only the Savage Are Left 001 [2026] [digital] [Son of Ultron-Empire]",
                nzb_url="https://x/wrong.nzb", size=100),
        ]
        mock_impl.return_value = impl
        res = un.search_usenet_for_issue(
            "Only the Savage Are Left", "2", issue_year=2026)
        assert res["chosen"] is None
        assert res["all_results"][0]["decision"] == "REJECT"

    @patch("models.indexers.get_indexer_impl")
    @patch("core.database.get_enabled_indexers", return_value=[
        {"id": 5, "name": "NZBgeek", "url": "https://x", "api_key": "k",
         "categories": None, "enabled": True, "indexer_type": "newznab"},
    ])
    def test_correct_bare_issue_accepted(self, mock_idx, mock_impl):
        """The correct bare issue (002) must still be chosen."""
        impl = MagicMock()
        impl.search.return_value = [
            NZBSearchResult(
                indexer_id=5, indexer_name="NZBgeek",
                title="Only the Savage Are Left 002 [2026] [digital] [Son of Ultron-Empire]",
                nzb_url="https://x/right.nzb", size=100),
        ]
        mock_impl.return_value = impl
        res = un.search_usenet_for_issue(
            "Only the Savage Are Left", "2", issue_year=2026)
        assert res["chosen"] is not None
        assert res["chosen"][0].nzb_url == "https://x/right.nzb"
        assert res["all_results"][0]["decision"] == "ACCEPT"

    @patch("models.indexers.get_indexer_impl")
    @patch("core.database.get_enabled_indexers", return_value=[
        {"id": 5, "name": "NZBgeek", "url": "https://x", "api_key": "k",
         "categories": None, "enabled": True, "indexer_type": "newznab"},
    ])
    def test_no_issue_number_not_auto_accepted(self, mock_idx, mock_impl):
        """A title with no confirmable issue number must not auto-download.

        Usenet requires a positively confirmed issue for the direct-match tier,
        so series+year alone cannot trigger an auto-grab.
        """
        impl = MagicMock()
        impl.search.return_value = [
            NZBSearchResult(indexer_id=5, indexer_name="NZBgeek",
                            title="Only the Savage Are Left (2026)",
                            nzb_url="https://x/noissue.nzb", size=100),
        ]
        mock_impl.return_value = impl
        res = un.search_usenet_for_issue(
            "Only the Savage Are Left", "2", issue_year=2026)
        assert res["chosen"] is None


class TestIssueYearSeparatesVolumes:
    """The manual search UI now sends the issue's year (see series.html /
    wanted.html). Without it every volume's #8 scored identically."""

    TITLES = [
        "Iron Man 008 (2026) (Digital) (Zone-Empire)",
        "Iron Man 008 (2025-07) (c2c) (SHELL-HEAD)",
        "Iron Man 008 (2021) (Digital) (Zone-Empire)",
        "Iron Man 008 (1968) (digital) (Minutemen-Slayer)",
        "Iron Man 2.0 008 (2011) (Digital) (Shadowcat-Empire)",
    ]

    def _results(self):
        return [
            NZBSearchResult(indexer_id=5, indexer_name="NZBgeek", title=t,
                            nzb_url=f"https://x/{i}.nzb", size=100)
            for i, t in enumerate(self.TITLES)
        ]

    @patch("models.indexers.get_indexer_impl")
    @patch("core.database.get_enabled_indexers", return_value=[
        {"id": 5, "name": "NZBgeek", "url": "https://x", "api_key": "k",
         "categories": None, "enabled": True, "indexer_type": "newznab"},
    ])
    def test_without_year_every_volume_matches(self, mock_idx, mock_impl):
        impl = MagicMock()
        impl.search.return_value = self._results()
        mock_impl.return_value = impl

        res = un.search_usenet_for_issue("Iron Man", "8")
        accepted = [r for r in res["all_results"] if r["decision"] == "ACCEPT"]
        # The reported bug: reprints and reboots all badge as "Match".
        assert len(accepted) > 1

    @patch("models.indexers.get_indexer_impl")
    @patch("core.database.get_enabled_indexers", return_value=[
        {"id": 5, "name": "NZBgeek", "url": "https://x", "api_key": "k",
         "categories": None, "enabled": True, "indexer_type": "newznab"},
    ])
    def test_year_leaves_only_the_right_volume(self, mock_idx, mock_impl):
        impl = MagicMock()
        impl.search.return_value = self._results()
        mock_impl.return_value = impl

        res = un.search_usenet_for_issue("Iron Man", "8", issue_year=2026)
        accepted = [r for r in res["all_results"] if r["decision"] == "ACCEPT"]
        assert [r["title"] for r in accepted] == [
            "Iron Man 008 (2026) (Digital) (Zone-Empire)"
        ]
        # And it outscores every wrong-year release.
        best = max(res["all_results"], key=lambda r: r["score"])
        assert best["title"] == "Iron Man 008 (2026) (Digital) (Zone-Empire)"


class TestTryDownloadForIssue:

    @patch("models.usenet.search_usenet_for_issue")
    def test_dry_run_does_not_grab(self, mock_search):
        result = NZBSearchResult(indexer_id=5, indexer_name="NZBgeek",
                                 title="Batman 1", nzb_url="https://x/a.nzb")
        mock_search.return_value = {
            "chosen": (result, 90), "tier": "direct match",
            "best_accept": (result, 90), "best_fallback": None, "all_results": [{}],
        }
        out = un.try_download_for_issue("Batman", "1", dry_run=True)
        assert out["status"] == "match_found"
        assert out["submitted"] is False
        assert out["chosen"]["filename"] == "Batman 1.cbz"


class TestGrabNzb:

    @patch("models.usenet._fetch_nzb", return_value=b"<nzb/>")
    @patch("models.usenet._ensure_poller")
    @patch("models.download_clients.get_download_client_by_name")
    @patch("core.database.get_active_download_client",
           return_value={"client_type": "sabnzbd", "config": {"api_key": "k"}})
    def test_grab_submits_fetched_bytes(self, mock_active, mock_get_client, mock_poller, mock_fetch):
        client = MagicMock()
        client.add_nzb.return_value = NZBSubmitResult(client_id="nzo_1", success=True)
        mock_get_client.return_value = client
        un.usenet_downloads.clear()
        did = un.grab_nzb("https://x/a.nzb", "Batman 1.cbz", series="Batman", issue="1")
        assert did is not None
        assert un.usenet_downloads[did]["client_id"] == "nzo_1"
        # Real NZB bytes were submitted, not the URL.
        assert client.add_nzb.call_args[0][0] == b"<nzb/>"
        # The job name must NOT carry a comic extension: the client names the
        # completed folder after it, and a "Batman 1.cbz" directory then gets
        # imported as if it were a comic file.
        assert client.add_nzb.call_args[0][1] == "Batman 1"
        # The tracked/display filename still keeps the extension.
        assert un.usenet_downloads[did]["filename"] == "Batman 1.cbz"
        mock_poller.assert_called_once()

    @patch("models.usenet._fetch_nzb", return_value=None)
    @patch("models.usenet._ensure_poller")
    @patch("models.download_clients.get_download_client_by_name")
    @patch("core.database.get_active_download_client",
           return_value={"client_type": "sabnzbd", "config": {"api_key": "k"}})
    def test_grab_falls_back_to_url(self, mock_active, mock_get_client, mock_poller, mock_fetch):
        client = MagicMock()
        client.add_nzb.return_value = NZBSubmitResult(client_id="nzo_1", success=True)
        mock_get_client.return_value = client
        un.usenet_downloads.clear()
        did = un.grab_nzb("https://x/a.nzb", "Batman 1.cbz")
        assert did is not None
        assert client.add_nzb.call_args[0][0] == "https://x/a.nzb"

    @patch("core.database.get_active_download_client", return_value=None)
    def test_grab_no_active_client(self, mock_active):
        assert un.grab_nzb("https://x/a.nzb", "x.cbz") is None

    @pytest.mark.parametrize("filename,expected", [
        ("Heavy Metal 5.cbz", "Heavy Metal 5"),
        ("Batman 001.cbr", "Batman 001"),
        ("Some Pack.nzb", "Some Pack"),
        ("Batman v2 001", "Batman v2 001"),       # already extension-free
        ("Saga Vol. 1", "Saga Vol. 1"),           # a dotted stem is not an extension
        ("", ""),
        (None, ""),
    ])
    def test_job_name_strips_comic_extensions(self, filename, expected):
        assert un._job_name(filename) == expected

    @patch("models.usenet._fetch_nzb", return_value=b"<nzb/>")
    @patch("models.usenet._ensure_poller")
    @patch("models.download_clients.get_download_client_by_name")
    @patch("core.database.get_active_download_client",
           return_value={"client_type": "sabnzbd", "config": {"api_key": "k"}})
    def test_grab_submit_failure(self, mock_active, mock_get_client, mock_poller, mock_fetch):
        client = MagicMock()
        client.add_nzb.return_value = NZBSubmitResult(success=False, error="bad")
        mock_get_client.return_value = client
        assert un.grab_nzb("https://x/a.nzb", "x.cbz") is None


class TestPollerProgress:

    def test_update_progress_caches_live_fields(self):
        un.usenet_downloads.clear()
        un.usenet_downloads["d1"] = {
            "client_type": "sabnzbd", "client_id": "nzo_1", "filename": "Batman 1.cbz",
            "status": "downloading", "error": None, "series": "Batman", "issue": "1",
            "percent": 0, "stage": "Queued", "bytes_total": None, "bytes_downloaded": None,
        }
        st = DownloadStatus(client_id="nzo_1", status="downloading", percent=55.0,
                            stage="Repairing", bytes_total=100, bytes_downloaded=55)
        un._update_progress("d1", st)
        job = un.usenet_downloads["d1"]
        assert job["percent"] == 55.0
        assert job["stage"] == "Repairing"
        assert job["bytes_total"] == 100
        assert job["bytes_downloaded"] == 55
        # The status endpoint helper surfaces the cached fields.
        snap = {d["download_id"]: d for d in un.get_usenet_downloads()}
        assert snap["d1"]["stage"] == "Repairing"
        assert snap["d1"]["percent"] == 55.0

    def test_set_status_terminal_sets_percent(self):
        un.usenet_downloads.clear()
        un.usenet_downloads["d1"] = {"status": "downloading", "error": None, "percent": 40}
        un._set_status("d1", "complete", percent=100)
        assert un.usenet_downloads["d1"]["status"] == "complete"
        assert un.usenet_downloads["d1"]["percent"] == 100
        un._set_status("d1", "failed", error="boom")
        assert un.usenet_downloads["d1"]["status"] == "failed"
        assert un.usenet_downloads["d1"]["error"] == "boom"

    @patch("models.download_clients.get_download_client_by_name")
    @patch("core.database.get_download_client_config", return_value={"api_key": "k"})
    def test_statuses_for_merges_queue_and_history(self, mock_cfg, mock_get_client):
        client = MagicMock()
        client.get_queue.return_value = [
            DownloadStatus(client_id="active1", status="downloading", percent=30, stage="Downloading"),
            DownloadStatus(client_id="both", status="downloading", percent=99, stage="Moving"),
        ]
        client.get_history.return_value = [
            DownloadStatus(client_id="both", status="complete", storage_path="/done"),
            DownloadStatus(client_id="done1", status="complete", storage_path="/done1"),
        ]
        mock_get_client.return_value = client
        merged = un._statuses_for("sabnzbd")
        # History wins for an item present in both queue and history.
        assert merged["both"].status == "complete"
        assert merged["both"].storage_path == "/done"
        assert merged["active1"].status == "downloading"
        assert merged["active1"].stage == "Downloading"
        assert merged["done1"].status == "complete"

    @patch("core.database.get_download_client_config", return_value=None)
    def test_statuses_for_no_config_returns_empty(self, mock_cfg):
        assert un._statuses_for("sabnzbd") == {}


class TestImportCompleted:

    def test_moves_comic_into_watch(self, tmp_path, monkeypatch):
        watch = tmp_path / "watch"
        watch.mkdir()
        done = tmp_path / "done" / "Batman 1"
        done.mkdir(parents=True)
        (done / "Batman 1.cbz").write_bytes(b"x")
        (done / "readme.txt").write_bytes(b"junk")
        monkeypatch.setattr(un, "_watch_dir", lambda: str(watch))

        assert un._import_completed(str(done), "Batman 1.cbz") is True
        assert (watch / "Batman 1.cbz").exists()
        assert not (watch / "readme.txt").exists()

    def test_already_in_watch_is_noop(self, tmp_path, monkeypatch):
        watch = tmp_path / "watch"
        (watch / "sub").mkdir(parents=True)
        monkeypatch.setattr(un, "_watch_dir", lambda: str(watch))
        assert un._import_completed(str(watch / "sub"), "x.cbz") is True

    def test_inaccessible_path_returns_false(self, tmp_path, monkeypatch):
        watch = tmp_path / "watch"
        watch.mkdir()
        monkeypatch.setattr(un, "_watch_dir", lambda: str(watch))
        assert un._import_completed(str(tmp_path / "nope"), "x.cbz") is False
