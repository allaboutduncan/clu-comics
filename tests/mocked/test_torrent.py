"""Tests for models/torrent.py -- search/score, grab, poller, ledger, import."""
import pytest
from unittest.mock import MagicMock, patch

import models.torrent as tr
from models.indexers import TorrentSearchResult
from models.download_clients import ClientType, DownloadStatus, NZBSubmitResult


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Isolate the module dict, and keep the ledger writes off a real DB.

    Mirrors tests/mocked/test_dcpp.py's fixture: job state lives in a
    module-level dict, and every state change also writes to the
    torrent_jobs table. These are mocked tests, so the three persistence
    wrappers are stubbed out; the real SQL round-trip is covered in
    tests/integration.
    """
    tr.torrent_downloads.clear()
    monkeypatch.setattr(tr, "_persist_new", MagicMock())
    monkeypatch.setattr(tr, "_persist_update", MagicMock())
    monkeypatch.setattr(tr, "_persist_delete", MagicMock(return_value=True))
    yield
    tr.torrent_downloads.clear()


def _idx(**over):
    idx = {"id": 5, "name": "MyTracker", "url": "https://x", "api_key": "k",
           "categories": None, "enabled": True, "indexer_type": "torznab"}
    idx.update(over)
    return idx


class TestTorrentConfigured:

    @patch("core.database.get_active_download_client",
           return_value={"client_type": "qbittorrent", "config": {}})
    @patch("models.download_sources.enabled_indexers_of_type", return_value=[_idx()])
    @patch("core.database.get_user_preference", return_value='["torrent"]')
    def test_configured(self, mock_pref, mock_idx, mock_active):
        assert tr.torrent_enabled_and_configured() is True

    @patch("core.database.get_active_download_client",
           return_value={"client_type": "qbittorrent", "config": {}})
    @patch("models.download_sources.enabled_indexers_of_type", return_value=[])
    @patch("core.database.get_user_preference", return_value='["torrent"]')
    def test_no_indexer(self, mock_pref, mock_idx, mock_active):
        assert tr.torrent_enabled_and_configured() is False

    @patch("core.database.get_active_download_client", return_value=None)
    @patch("models.download_sources.enabled_indexers_of_type", return_value=[_idx()])
    @patch("core.database.get_user_preference", return_value='["torrent"]')
    def test_no_active_client(self, mock_pref, mock_idx, mock_active):
        assert tr.torrent_enabled_and_configured() is False

    @patch("core.database.get_user_preference", return_value='["getcomics"]')
    def test_torrent_not_a_source(self, mock_pref):
        assert tr.torrent_enabled_and_configured() is False


class TestSearchAndScore:

    @patch("models.indexers.get_indexer_impl")
    @patch("models.download_sources.enabled_indexers_of_type", return_value=[_idx()])
    def test_finds_and_scores_match(self, mock_idx, mock_impl):
        impl = MagicMock()
        impl.search.return_value = [
            TorrentSearchResult(indexer_id=5, indexer_name="MyTracker",
                                title="Batman 001 (2020)",
                                download_url="magnet:?xt=urn:btih:a", size=100,
                                seeders=10, peers=2),
        ]
        mock_impl.return_value = impl
        res = tr.search_torrent_for_issue("Batman", "1", issue_year=2020)
        assert res["chosen"] is not None
        assert res["chosen"][0].download_url == "magnet:?xt=urn:btih:a"
        assert res["all_results"][0]["decision"] == "ACCEPT"
        assert res["all_results"][0]["seeders"] == 10

    @patch("models.indexers.get_indexer_impl")
    @patch("models.download_sources.enabled_indexers_of_type", return_value=[_idx()])
    def test_zero_seeders_dropped_before_scoring(self, mock_idx, mock_impl):
        impl = MagicMock()
        impl.search.return_value = [
            TorrentSearchResult(indexer_id=5, indexer_name="MyTracker",
                                title="Batman 001 (2020)",
                                download_url="magnet:?xt=urn:btih:dead", seeders=0),
        ]
        mock_impl.return_value = impl
        res = tr.search_torrent_for_issue("Batman", "1", issue_year=2020)
        assert res["all_results"] == []
        assert res["chosen"] is None

    @patch("models.indexers.get_indexer_impl")
    @patch("models.download_sources.enabled_indexers_of_type", return_value=[_idx()])
    def test_unknown_seeder_count_is_not_dropped(self, mock_idx, mock_impl):
        # Some Torznab indexers omit seeders entirely — None must not be
        # treated the same as a confirmed dead torrent.
        impl = MagicMock()
        impl.search.return_value = [
            TorrentSearchResult(indexer_id=5, indexer_name="MyTracker",
                                title="Batman 001 (2020)",
                                download_url="magnet:?xt=urn:btih:a", seeders=None),
        ]
        mock_impl.return_value = impl
        res = tr.search_torrent_for_issue("Batman", "1", issue_year=2020)
        assert len(res["all_results"]) == 1

    @patch("models.indexers.get_indexer_impl")
    @patch("models.download_sources.enabled_indexers_of_type", return_value=[_idx()])
    def test_no_match(self, mock_idx, mock_impl):
        impl = MagicMock()
        impl.search.return_value = [
            TorrentSearchResult(indexer_id=5, indexer_name="MyTracker",
                                title="Completely Different Comic 99",
                                download_url="magnet:?xt=urn:btih:z", seeders=5),
        ]
        mock_impl.return_value = impl
        res = tr.search_torrent_for_issue("Batman", "1", issue_year=2020)
        assert res["chosen"] is None

    @patch("models.indexers.get_indexer_impl")
    @patch("models.download_sources.enabled_indexers_of_type", return_value=[_idx()])
    def test_no_issue_number_not_auto_accepted(self, mock_idx, mock_impl):
        # Same guard as Usenet/DC++: series+year alone must not auto-accept.
        impl = MagicMock()
        impl.search.return_value = [
            TorrentSearchResult(indexer_id=5, indexer_name="MyTracker",
                                title="Only the Savage Are Left (2026)",
                                download_url="magnet:?xt=urn:btih:n", seeders=5),
        ]
        mock_impl.return_value = impl
        res = tr.search_torrent_for_issue("Only the Savage Are Left", "2", issue_year=2026)
        assert res["chosen"] is None


class TestTryDownloadForIssue:

    @patch("models.torrent.search_torrent_for_issue")
    def test_dry_run_does_not_grab(self, mock_search):
        result = TorrentSearchResult(indexer_id=5, indexer_name="MyTracker",
                                     title="Batman 1", download_url="magnet:?xt=urn:btih:a")
        mock_search.return_value = {
            "chosen": (result, 90), "tier": "direct match",
            "best_accept": (result, 90), "best_fallback": None, "all_results": [{}],
        }
        out = tr.try_download_for_issue("Batman", "1", dry_run=True)
        assert out["status"] == "match_found"
        assert out["submitted"] is False
        assert out["chosen"]["filename"] == "Batman 1.cbz"


class TestGrabTorrent:

    @patch("models.torrent._ensure_poller")
    @patch("models.torrent._active_client")
    def test_tracks_the_torrent(self, mock_client, mock_poller):
        client = MagicMock()
        client.client_type = ClientType.QBITTORRENT
        client.add_torrent.return_value = NZBSubmitResult(client_id="hash-7", success=True)
        mock_client.return_value = client

        download_id = tr.grab_torrent(
            "magnet:?xt=urn:btih:a", "Batman 1.cbz", series="Batman", issue="1")

        assert download_id
        job = tr.torrent_downloads[download_id]
        assert job["client_type"] == "qbittorrent"
        assert job["client_id"] == "hash-7"
        assert job["status"] == "downloading"
        assert job["series"] == "Batman"
        # The job name must not carry a comic extension, same reason as Usenet.
        client.add_torrent.assert_called_once_with("magnet:?xt=urn:btih:a", "Batman 1")
        mock_poller.assert_called_once()

    @patch("models.torrent._ensure_poller")
    @patch("models.torrent._active_client", return_value=None)
    def test_no_active_client(self, mock_client, mock_poller):
        errors = []
        assert tr.grab_torrent("magnet:?xt=urn:btih:a", "x.cbz", errors=errors) is None
        assert tr.torrent_downloads == {}
        assert errors

    @patch("models.torrent._ensure_poller")
    @patch("models.torrent._active_client")
    def test_client_rejects(self, mock_client, mock_poller):
        client = MagicMock()
        client.add_torrent.return_value = NZBSubmitResult(success=False, error="no category")
        mock_client.return_value = client
        errors = []
        assert tr.grab_torrent("magnet:?xt=urn:btih:a", "x.cbz", errors=errors) is None
        assert tr.torrent_downloads == {}
        assert errors == ["no category"]

    @pytest.mark.parametrize("filename,expected", [
        ("Heavy Metal 5.cbz", "Heavy Metal 5"),
        ("Batman 001.cbr", "Batman 001"),
        ("Some Pack.torrent", "Some Pack"),
        ("Batman v2 001", "Batman v2 001"),
        ("", ""),
        (None, ""),
    ])
    def test_job_name_strips_comic_extensions(self, filename, expected):
        assert tr._job_name(filename) == expected


class TestPollerProgress:

    def test_update_progress_caches_live_fields(self):
        tr.torrent_downloads["d1"] = {
            "client_type": "qbittorrent", "client_id": "h1", "filename": "Batman 1.cbz",
            "status": "downloading", "error": None, "series": "Batman", "issue": "1",
            "percent": 0, "stage": "Queued", "bytes_total": None, "bytes_downloaded": None,
            "target": None,
        }
        st = DownloadStatus(client_id="h1", status="downloading", percent=55.0,
                            stage="Downloading", bytes_total=100, bytes_downloaded=55)
        tr._update_progress("d1", st)
        job = tr.torrent_downloads["d1"]
        assert job["percent"] == 55.0
        assert job["stage"] == "Downloading"
        snap = {d["download_id"]: d for d in tr.get_torrent_downloads()}
        assert snap["d1"]["percent"] == 55.0

    def test_set_status_terminal_sets_percent(self):
        tr.torrent_downloads["d1"] = {"status": "downloading", "error": None, "percent": 40,
                                      "filename": "Batman 1.cbz"}
        tr._set_status("d1", "complete", percent=100)
        assert tr.torrent_downloads["d1"]["status"] == "complete"
        assert tr.torrent_downloads["d1"]["percent"] == 100
        tr.torrent_downloads["d1"]["status"] = "downloading"
        tr._set_status("d1", "failed", error="boom")
        assert tr.torrent_downloads["d1"]["status"] == "failed"
        assert tr.torrent_downloads["d1"]["error"] == "boom"

    def test_set_status_on_unknown_job_writes_nothing(self):
        # Mirrors models.dcpp: a status change for a job nothing tracks must
        # not touch the ledger or send a filename-less notification.
        tr._set_status("missing", "complete")
        tr._persist_delete.assert_not_called()
        tr._persist_update.assert_not_called()


class TestPollOne:

    def _job(self, **over):
        job = {
            "client_type": "qbittorrent", "client_id": "h1", "filename": "Batman 1.cbz",
            "status": "downloading", "error": None, "series": "Batman", "issue": "1",
            "percent": 0, "stage": "Queued", "bytes_total": None,
            "bytes_downloaded": None, "target": None,
        }
        job.update(over)
        tr.torrent_downloads["d1"] = job
        return job

    def _client(self, status):
        client = MagicMock()
        client.get_status.return_value = status
        return client

    @patch("models.torrent._import_completed", return_value=True)
    def test_complete_imports_and_resolves(self, mock_import):
        job = self._job()
        status = DownloadStatus(client_id="h1", status="complete", storage_path="/dl/Batman 1.cbz")
        tr._poll_one(self._client(status), "d1", job)
        mock_import.assert_called_once_with("/dl/Batman 1.cbz", "Batman 1.cbz")
        assert tr.torrent_downloads["d1"]["status"] == "complete"

    @patch("models.torrent._import_completed", return_value=False)
    def test_complete_but_unimportable_is_complete_no_move(self, mock_import):
        job = self._job()
        status = DownloadStatus(client_id="h1", status="complete", storage_path="/dl/x")
        tr._poll_one(self._client(status), "d1", job)
        assert tr.torrent_downloads["d1"]["status"] == "complete_no_move"

    def test_failed_status(self):
        job = self._job()
        status = DownloadStatus(client_id="h1", status="failed")
        tr._poll_one(self._client(status), "d1", job)
        assert tr.torrent_downloads["d1"]["status"] == "failed"

    def test_still_downloading_updates_progress_only(self):
        job = self._job()
        status = DownloadStatus(client_id="h1", status="downloading", percent=30)
        tr._poll_one(self._client(status), "d1", job)
        assert tr.torrent_downloads["d1"]["status"] == "downloading"
        assert tr.torrent_downloads["d1"]["percent"] == 30

    @patch("models.torrent._import_completed", return_value=True)
    def test_gone_from_client_imports_from_cached_target(self, mock_import):
        # qBittorrent no longer holds the torrent (removed by hand, or an
        # auto-remove-on-completion rule) — the last cached target is the
        # only remaining signal of where the file landed.
        job = self._job(target="/dl/Batman 1.cbz")
        tr._poll_one(self._client(None), "d1", job)
        mock_import.assert_called_once_with("/dl/Batman 1.cbz", "Batman 1.cbz")
        assert tr.torrent_downloads["d1"]["status"] == "complete"

    @patch("models.torrent._import_completed", return_value=False)
    def test_gone_from_client_with_no_cached_target(self, mock_import):
        job = self._job(target=None)
        tr._poll_one(self._client(None), "d1", job)
        mock_import.assert_called_once_with(None, "Batman 1.cbz")
        assert tr.torrent_downloads["d1"]["status"] == "complete_no_move"


class TestLedgerWrites:

    @patch("models.torrent._ensure_poller")
    @patch("models.torrent._active_client")
    def test_grab_persists_before_starting_the_poller(self, mock_client, mock_poller):
        client = MagicMock()
        client.client_type = ClientType.QBITTORRENT
        client.add_torrent.return_value = NZBSubmitResult(client_id="hash-7", success=True)
        mock_client.return_value = client

        download_id = tr.grab_torrent(
            "magnet:?xt=urn:btih:a", "Batman 1.cbz", series="Batman", issue="1")

        tr._persist_new.assert_called_once()
        saved_id, saved_job = tr._persist_new.call_args[0]
        assert saved_id == download_id
        assert saved_job["client_id"] == "hash-7"

    @patch("models.torrent._ensure_poller")
    @patch("models.torrent._active_client")
    def test_a_failed_grab_writes_nothing(self, mock_client, mock_poller):
        client = MagicMock()
        client.add_torrent.return_value = NZBSubmitResult(success=False, error="rejected")
        mock_client.return_value = client
        assert tr.grab_torrent("magnet:?xt=urn:btih:a", "x.cbz") is None
        tr._persist_new.assert_not_called()

    def test_clean_completion_clears_the_row(self):
        tr.torrent_downloads["d1"] = {"status": "downloading", "error": None,
                                      "percent": 0, "filename": "x.cbz"}
        tr._set_status("d1", "complete", percent=100)
        tr._persist_delete.assert_called_once_with("d1")
        tr._persist_update.assert_not_called()

    @pytest.mark.parametrize("status", ["failed", "complete_no_move"])
    def test_unresolved_terminals_keep_their_row(self, status):
        tr.torrent_downloads["d1"] = {"status": "downloading", "error": None,
                                      "percent": 0, "filename": "x.cbz"}
        tr._set_status("d1", status, error="boom")
        tr._persist_delete.assert_not_called()
        assert tr._persist_update.call_args.kwargs["status"] == status


class TestRecoverTorrentJobs:

    ROW = {
        "download_id": "d1", "client_type": "qbittorrent", "client_id": "h1",
        "filename": "Batman 1.cbz", "series": "Batman", "issue": "1",
        "status": "downloading", "error": None, "percent": 10,
        "stage": "Downloading", "bytes_total": 100, "bytes_downloaded": 10,
        "target": "/dl/Batman 1.cbz",
    }

    @patch("models.torrent._ensure_poller")
    @patch("core.database.get_active_torrent_jobs")
    def test_recovers_jobs_into_memory(self, mock_rows, mock_poller):
        mock_rows.return_value = [dict(self.ROW)]
        recovered = tr.recover_torrent_jobs()
        assert recovered == 1
        assert tr.torrent_downloads["d1"]["client_id"] == "h1"
        assert tr.torrent_downloads["d1"]["target"] == "/dl/Batman 1.cbz"
        mock_poller.assert_called_once()

    @patch("models.torrent._ensure_poller")
    @patch("core.database.get_active_torrent_jobs")
    def test_makes_no_network_calls(self, mock_rows, mock_poller):
        # Recovery runs at import time under Gunicorn; a 10s-timeout HTTP call
        # per torrent would stall boot. The poller does the reconcile instead.
        mock_rows.return_value = [dict(self.ROW)]
        with patch("models.torrent._active_client") as mock_client:
            tr.recover_torrent_jobs()
        mock_client.assert_not_called()

    @patch("models.torrent._ensure_poller")
    @patch("core.database.get_active_torrent_jobs")
    def test_does_not_clobber_a_live_job(self, mock_rows, mock_poller):
        tr.torrent_downloads["d1"] = {"status": "downloading", "percent": 99}
        mock_rows.return_value = [dict(self.ROW)]
        assert tr.recover_torrent_jobs() == 0
        assert tr.torrent_downloads["d1"]["percent"] == 99

    @patch("models.torrent.torrent_enabled_and_configured", return_value=False)
    @patch("models.torrent._ensure_poller")
    @patch("core.database.get_active_torrent_jobs", return_value=[])
    def test_nothing_to_recover_and_torrent_off(self, mock_rows, mock_poller, mock_cfg):
        assert tr.recover_torrent_jobs() == 0
        mock_poller.assert_not_called()

    @patch("models.torrent.torrent_enabled_and_configured", return_value=True)
    @patch("models.torrent._ensure_poller")
    @patch("core.database.get_active_torrent_jobs", return_value=[])
    def test_empty_ledger_still_polls_when_torrent_is_on(self, mock_rows, mock_poller, mock_cfg):
        assert tr.recover_torrent_jobs() == 0
        mock_poller.assert_called_once()

    @patch("models.torrent._ensure_poller")
    @patch("core.database.get_active_torrent_jobs", side_effect=Exception("db down"))
    def test_unreadable_ledger_is_not_fatal(self, mock_rows, mock_poller):
        assert tr.recover_torrent_jobs() == 0

    @patch("models.torrent._ensure_poller")
    @patch("core.database.get_active_torrent_jobs")
    def test_recovered_job_completes_on_the_first_poll(self, mock_rows, mock_poller):
        # The whole point: qBittorrent finished (or auto-removed) the torrent
        # while CLU was down, so the persisted target is the only signal left.
        mock_rows.return_value = [dict(self.ROW)]
        tr.recover_torrent_jobs()

        client = MagicMock()
        client.get_status.return_value = None

        with patch("models.torrent._import_completed", return_value=True) as mock_import:
            tr._poll_one(client, "d1", tr.torrent_downloads["d1"])

        mock_import.assert_called_once_with("/dl/Batman 1.cbz", "Batman 1.cbz")
        assert tr.torrent_downloads["d1"]["status"] == "complete"
        tr._persist_delete.assert_called_once_with("d1")


class TestDismissTorrentJob:

    def test_dismiss_known_job(self):
        tr.torrent_downloads["d1"] = {"status": "failed"}
        assert tr.dismiss_torrent_job("d1") is True
        assert "d1" not in tr.torrent_downloads
        tr._persist_delete.assert_called_once_with("d1")

    def test_dismiss_unknown_job_falls_back_to_ledger_result(self):
        tr._persist_delete.return_value = False
        assert tr.dismiss_torrent_job("ghost") is False


class TestImportCompleted:

    def test_delegates_to_the_shared_usenet_mover(self):
        with patch("models.usenet._import_completed", return_value=True) as mock_shared:
            assert tr._import_completed("/dl/x", "x.cbz") is True
            mock_shared.assert_called_once_with("/dl/x", "x.cbz", source="Torrent")
