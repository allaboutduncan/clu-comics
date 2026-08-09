"""Tests for models/dcpp.py -- search/score, token cache, grab, poller, import."""
import time

import pytest
from unittest.mock import MagicMock, patch

import models.dcpp as dc
from models.download_clients import ClientType, DownloadStatus, NZBSubmitResult


@pytest.fixture(autouse=True)
def _clean_state():
    """DC++ tracks jobs and result tokens in module dicts; isolate each test."""
    dc.dcpp_downloads.clear()
    dc._result_tokens.clear()
    yield
    dc.dcpp_downloads.clear()
    dc._result_tokens.clear()


def _fake_client(results=None, instance_id="inst-1", bundle_id="b1"):
    """A stand-in AirDC++ client returning one search page and accepting grabs."""
    client = MagicMock()
    client.client_type = ClientType.AIRDCPP
    client.last_error = None
    client.search.return_value = (instance_id, results if results is not None else [])
    client.download_result.return_value = NZBSubmitResult(
        client_id=bundle_id, success=True)
    return client


def _hit(name, result_id="1", tth=None, size=50_000_000, users=2):
    return {
        "result_id": result_id, "name": name, "size": size,
        "tth": tth if tth is not None else f"TTH{result_id}",
        "type": "file", "users": users, "relevance": 90, "path": "/",
    }


class TestDcppConfigured:

    @patch("core.database.get_active_download_client",
           return_value={"client_type": "airdcpp", "config": {}})
    @patch("core.database.get_user_preference", return_value='["dcpp"]')
    def test_enabled(self, mock_pref, mock_active):
        assert dc.dcpp_enabled_and_configured() is True

    @patch("core.database.get_active_download_client", return_value=None)
    @patch("core.database.get_user_preference", return_value='["dcpp"]')
    def test_no_active_client(self, mock_pref, mock_active):
        assert dc.dcpp_enabled_and_configured() is False

    @patch("core.database.get_active_download_client",
           return_value={"client_type": "airdcpp", "config": {}})
    @patch("core.database.get_user_preference", return_value='["getcomics","usenet"]')
    def test_not_in_source_priority(self, mock_pref, mock_active):
        assert dc.dcpp_enabled_and_configured() is False

    @patch("core.database.get_active_download_client",
           return_value={"client_type": "airdcpp", "config": {}})
    @patch("core.database.get_user_preference", return_value='["dcpp"]')
    def test_asks_for_the_dcpp_group(self, mock_pref, mock_active):
        # Must not pick up an active SABnzbd -- the groups are independent.
        dc.dcpp_enabled_and_configured()
        assert mock_active.call_args.kwargs["client_group"] == "dcpp"

    @patch("core.database.get_user_preference", return_value='["dcpp"]')
    @patch("core.database.get_active_download_client", side_effect=Exception("db down"))
    def test_errors_are_not_fatal(self, mock_active, mock_pref):
        assert dc.dcpp_enabled_and_configured() is False


class TestTokenCache:

    def test_store_and_resolve(self):
        token = dc._store_token("inst-9", _hit("Batman 001.cbz", result_id="7"))
        entry = dc._resolve_token(token)
        assert entry["instance_id"] == "inst-9"
        assert entry["result_id"] == "7"

    def test_unknown_token(self):
        assert dc._resolve_token("nope") is None

    def test_expired_token_is_dropped(self):
        token = dc._store_token("inst-9", _hit("Batman 001.cbz"))
        # Expire it by hand rather than sleeping out the real TTL.
        dc._result_tokens[token]["expires"] = time.time() - 1
        assert dc._resolve_token(token) is None
        assert token not in dc._result_tokens

    def test_prune_leaves_live_tokens(self):
        stale = dc._store_token("i", _hit("a.cbz", result_id="1"))
        live = dc._store_token("i", _hit("b.cbz", result_id="2"))
        dc._result_tokens[stale]["expires"] = time.time() - 1
        dc._prune_tokens()
        assert stale not in dc._result_tokens
        assert live in dc._result_tokens


class TestSearchAndScore:

    @patch("models.dcpp._active_client")
    def test_scores_and_picks_direct_match(self, mock_client):
        mock_client.return_value = _fake_client([
            _hit("Batman 001 (2016).cbz", result_id="1"),
            _hit("Superman 001 (2016).cbz", result_id="2"),
        ])
        res = dc.search_dcpp_for_issue("Batman", "1", issue_year=2016)

        assert res["chosen"] is not None
        assert res["tier"] == "direct match"
        assert res["chosen"][0]["title"].startswith("Batman 001")
        # Every result is returned so the UI can show the rejects too.
        assert len(res["all_results"]) == 2
        # Each carries a grabbable token.
        assert all(r["result_token"] for r in res["all_results"])

    @patch("models.dcpp._active_client")
    def test_dedupes_on_tth(self, mock_client):
        # The same file from two users, matched by several query variants.
        mock_client.return_value = _fake_client([
            _hit("Batman 001 (2016).cbz", result_id="1", tth="SAME"),
            _hit("Batman 001 (2016).cbz", result_id="2", tth="SAME"),
        ])
        res = dc.search_dcpp_for_issue("Batman", "1", issue_year=2016)
        assert len(res["all_results"]) == 1

    @patch("models.dcpp._active_client")
    def test_unconfirmed_issue_is_rejected(self, mock_client):
        # DC++ filenames rarely carry a '#', so series+year alone must not be
        # enough to auto-accept the wrong issue.
        mock_client.return_value = _fake_client([
            _hit("Batman 099 (2016).cbz", result_id="1"),
        ])
        res = dc.search_dcpp_for_issue("Batman", "1", issue_year=2016)
        assert res["chosen"] is None
        assert res["all_results"][0]["decision"] == "REJECT"

    @patch("models.dcpp._active_client", return_value=None)
    def test_no_client_returns_empty(self, mock_client):
        res = dc.search_dcpp_for_issue("Batman", "1")
        assert res["all_results"] == []
        assert res["errors"]

    @patch("models.dcpp._active_client")
    def test_search_exception_is_collected(self, mock_client):
        client = _fake_client()
        client.search.side_effect = Exception("hub offline")
        mock_client.return_value = client
        res = dc.search_dcpp_for_issue("Batman", "1")
        assert res["all_results"] == []
        assert any("hub offline" in e for e in res["errors"])

    @patch("models.dcpp._active_client")
    def test_tries_zero_padded_queries(self, mock_client):
        client = _fake_client()
        mock_client.return_value = client
        dc.search_dcpp_for_issue("Batman", "1")
        queried = [c.args[0] for c in client.search.call_args_list]
        assert queried == ["Batman 1", "Batman 01", "Batman 001"]


class TestTryDownloadForIssue:

    @patch("models.dcpp.grab_dcpp", return_value="dl-1")
    @patch("models.dcpp._active_client")
    def test_submits_on_match(self, mock_client, mock_grab):
        mock_client.return_value = _fake_client([_hit("Batman 001 (2016).cbz")])
        out = dc.try_download_for_issue("Batman", "1", issue_year=2016)
        assert out["source"] == "dcpp"
        assert out["status"] == "submitted"
        assert out["submitted"] is True
        assert out["download_id"] == "dl-1"
        assert out["chosen"]["filename"] == "Batman 1.cbz"

    @patch("models.dcpp.grab_dcpp")
    @patch("models.dcpp._active_client")
    def test_dry_run_does_not_submit(self, mock_client, mock_grab):
        mock_client.return_value = _fake_client([_hit("Batman 001 (2016).cbz")])
        out = dc.try_download_for_issue("Batman", "1", issue_year=2016, dry_run=True)
        assert out["status"] == "match_found"
        assert out["submitted"] is False
        mock_grab.assert_not_called()

    @patch("models.dcpp._active_client")
    def test_no_results(self, mock_client):
        mock_client.return_value = _fake_client([])
        out = dc.try_download_for_issue("Batman", "1")
        assert out["status"] == "no_results"

    @patch("models.dcpp._active_client")
    def test_no_match(self, mock_client):
        mock_client.return_value = _fake_client([_hit("Spider-Man 005.cbz")])
        out = dc.try_download_for_issue("Batman", "1", issue_year=2016)
        assert out["status"] == "no_match"

    @patch("models.dcpp.grab_dcpp", return_value=None)
    @patch("models.dcpp._active_client")
    def test_submit_failure(self, mock_client, mock_grab):
        mock_client.return_value = _fake_client([_hit("Batman 001 (2016).cbz")])
        out = dc.try_download_for_issue("Batman", "1", issue_year=2016)
        assert out["status"] == "submit_failed"
        assert out["submitted"] is False

    def test_signature_matches_usenet(self):
        # The auto-download loop calls both interchangeably.
        import inspect
        import models.usenet as un
        assert (inspect.signature(dc.try_download_for_issue)
                == inspect.signature(un.try_download_for_issue))


class TestGrabDcpp:

    @patch("models.dcpp._ensure_poller")
    @patch("models.dcpp._active_client")
    def test_tracks_the_bundle(self, mock_client, mock_poller):
        client = _fake_client(bundle_id="bundle-7")
        mock_client.return_value = client
        token = dc._store_token("inst-1", _hit("Batman 001.cbz", result_id="5"))

        download_id = dc.grab_dcpp(token, "Batman 1.cbz", series="Batman", issue="1")

        assert download_id
        job = dc.dcpp_downloads[download_id]
        assert job["client_type"] == "airdcpp"
        assert job["client_id"] == "bundle-7"
        assert job["status"] == "downloading"
        assert job["series"] == "Batman"
        client.download_result.assert_called_once_with("inst-1", "5")
        mock_poller.assert_called_once()

    @patch("models.dcpp._ensure_poller")
    @patch("models.dcpp._active_client")
    def test_expired_token_refuses(self, mock_client, mock_poller):
        assert dc.grab_dcpp("stale-token", "Batman 1.cbz") is None
        mock_client.assert_not_called()

    @patch("models.dcpp._ensure_poller")
    @patch("models.dcpp._active_client", return_value=None)
    def test_no_active_client(self, mock_client, mock_poller):
        token = dc._store_token("inst-1", _hit("Batman 001.cbz"))
        assert dc.grab_dcpp(token, "Batman 1.cbz") is None
        assert dc.dcpp_downloads == {}

    @patch("models.dcpp._ensure_poller")
    @patch("models.dcpp._active_client")
    def test_client_rejects(self, mock_client, mock_poller):
        client = _fake_client()
        client.download_result.return_value = NZBSubmitResult(
            success=False, error="no target")
        mock_client.return_value = client
        token = dc._store_token("inst-1", _hit("Batman 001.cbz"))
        assert dc.grab_dcpp(token, "Batman 1.cbz") is None
        assert dc.dcpp_downloads == {}


class TestPollerProgress:

    def test_update_progress_caches_live_fields(self):
        dc.dcpp_downloads["d1"] = {
            "client_type": "airdcpp", "client_id": "b1", "filename": "Batman 1.cbz",
            "status": "downloading", "error": None, "series": "Batman", "issue": "1",
            "percent": 0, "stage": "Queued", "bytes_total": None, "bytes_downloaded": None,
        }
        st = DownloadStatus(client_id="b1", status="downloading", percent=42.0,
                            stage="Downloading", bytes_total=100, bytes_downloaded=42)
        dc._update_progress("d1", st)

        snap = {d["download_id"]: d for d in dc.get_dcpp_downloads()}
        assert snap["d1"]["percent"] == 42.0
        assert snap["d1"]["stage"] == "Downloading"
        assert snap["d1"]["bytes_downloaded"] == 42

    def test_set_status_terminal(self):
        dc.dcpp_downloads["d1"] = {"status": "downloading", "error": None, "percent": 40}
        dc._set_status("d1", "complete", percent=100)
        assert dc.dcpp_downloads["d1"]["status"] == "complete"
        assert dc.dcpp_downloads["d1"]["percent"] == 100
        dc._set_status("d1", "failed", error="boom")
        assert dc.dcpp_downloads["d1"]["error"] == "boom"

    def test_update_progress_caches_target(self):
        # Once AirDC++ drops a finished bundle there is nothing left to ask
        # where the file landed, so the target must be captured while running.
        dc.dcpp_downloads["d1"] = {"percent": 0, "stage": None, "bytes_total": None,
                                   "bytes_downloaded": None, "target": None}
        dc._update_progress("d1", DownloadStatus(
            client_id="b1", status="downloading", percent=10,
            storage_path="/downloads/dcpp/Batman 1.cbz"))
        assert dc.dcpp_downloads["d1"]["target"] == "/downloads/dcpp/Batman 1.cbz"


class TestPollOne:
    """One poll of a tracked bundle, incl. the 404 completion path."""

    def _job(self, **over):
        job = {
            "client_type": "airdcpp", "client_id": "b1", "filename": "Batman 1.cbz",
            "status": "downloading", "error": None, "series": "Batman", "issue": "1",
            "percent": 0, "stage": "Queued", "bytes_total": None,
            "bytes_downloaded": None, "target": None,
        }
        job.update(over)
        dc.dcpp_downloads["d1"] = job
        return job

    def _client(self, state, status=None):
        client = MagicMock()
        client.get_bundle_state.return_value = (state, status)
        return client

    @patch("models.dcpp._import_completed", return_value=True)
    def test_gone_imports_from_cached_target(self, mock_import):
        job = self._job(target="/downloads/dcpp/Batman 1.cbz")
        dc._poll_one(self._client("gone"), "d1", job)
        mock_import.assert_called_once_with("/downloads/dcpp/Batman 1.cbz", "Batman 1.cbz")
        assert dc.dcpp_downloads["d1"]["status"] == "complete"
        assert dc.dcpp_downloads["d1"]["percent"] == 100

    @patch("models.dcpp._import_completed", return_value=False)
    def test_gone_without_importable_file(self, mock_import):
        # A hand-cancelled bundle looks the same as a removed finished one;
        # it just has nothing to import.
        job = self._job()
        dc._poll_one(self._client("gone"), "d1", job)
        assert dc.dcpp_downloads["d1"]["status"] == "complete_no_move"

    @patch("models.dcpp._import_completed")
    def test_error_leaves_job_untouched(self, mock_import):
        # A 500 or an unreachable client must never read as completion.
        job = self._job()
        dc._poll_one(self._client("error"), "d1", job)
        mock_import.assert_not_called()
        assert dc.dcpp_downloads["d1"]["status"] == "downloading"

    @patch("models.dcpp._import_completed", return_value=True)
    def test_completed_flag_imports_from_live_target(self, mock_import):
        job = self._job()
        status = DownloadStatus(client_id="b1", status="complete",
                                storage_path="/downloads/dcpp/Batman 1.cbz")
        dc._poll_one(self._client("found", status), "d1", job)
        mock_import.assert_called_once_with("/downloads/dcpp/Batman 1.cbz", "Batman 1.cbz")
        assert dc.dcpp_downloads["d1"]["status"] == "complete"

    def test_failed(self):
        job = self._job()
        dc._poll_one(self._client(
            "found", DownloadStatus(client_id="b1", status="failed")), "d1", job)
        assert dc.dcpp_downloads["d1"]["status"] == "failed"

    def test_active_updates_progress(self):
        job = self._job()
        dc._poll_one(self._client("found", DownloadStatus(
            client_id="b1", status="downloading", percent=60, stage="Downloading",
            storage_path="/downloads/dcpp/Batman 1.cbz")), "d1", job)
        assert dc.dcpp_downloads["d1"]["status"] == "downloading"
        assert dc.dcpp_downloads["d1"]["percent"] == 60
        assert dc.dcpp_downloads["d1"]["target"] == "/downloads/dcpp/Batman 1.cbz"

    @patch("models.dcpp._import_completed")
    def test_client_exception_is_not_fatal(self, mock_import):
        client = MagicMock()
        client.get_bundle_state.side_effect = Exception("boom")
        job = self._job()
        dc._poll_one(client, "d1", job)
        mock_import.assert_not_called()
        assert dc.dcpp_downloads["d1"]["status"] == "downloading"


class TestImportCompleted:

    def test_moves_comic_into_watch(self, tmp_path, monkeypatch):
        import models.usenet as un

        watch = tmp_path / "watch"
        watch.mkdir()
        done = tmp_path / "done" / "Batman 1"
        done.mkdir(parents=True)
        (done / "Batman 1.cbz").write_bytes(b"x")
        (done / "notes.txt").write_bytes(b"junk")
        # DC++ shares the Usenet mover, so the WATCH lookup is patched there.
        monkeypatch.setattr(un, "_watch_dir", lambda: str(watch))

        assert dc._import_completed(str(done), "Batman 1.cbz") is True
        assert (watch / "Batman 1.cbz").exists()
        assert not (watch / "notes.txt").exists()

    def test_inaccessible_path_returns_false(self, tmp_path, monkeypatch):
        import models.usenet as un

        watch = tmp_path / "watch"
        watch.mkdir()
        monkeypatch.setattr(un, "_watch_dir", lambda: str(watch))
        assert dc._import_completed(str(tmp_path / "nope"), "x.cbz") is False
