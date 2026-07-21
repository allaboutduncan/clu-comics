"""Tests for models/usenet.py -- search/score, grab, import, source priority."""
import pytest
from unittest.mock import MagicMock, patch

import models.usenet as un
from models.indexers import NZBSearchResult
from models.download_clients import NZBSubmitResult


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
