"""Tests for models/dcpp.py -- search/score, token cache, grab, poller, import."""
import time

import pytest
from unittest.mock import MagicMock, patch

import models.dcpp as dc
from models.download_clients import (
    ClientType,
    DownloadClientConfig,
    DownloadStatus,
    NZBSubmitResult,
)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Isolate the module dicts, and keep the ledger writes off a real DB.

    Job state lives in module-level dicts, and every state change now also
    writes to the dcpp_jobs table. These are mocked tests, so the three
    persistence wrappers are stubbed out; tests that care about *when* a write
    happens assert on the stubs, and the real SQL round-trip is covered in
    tests/integration.
    """
    dc.dcpp_downloads.clear()
    dc._result_tokens.clear()
    dc._bundle_snapshot.clear()
    monkeypatch.setattr(dc, "_persist_new", MagicMock())
    monkeypatch.setattr(dc, "_persist_update", MagicMock())
    monkeypatch.setattr(dc, "_persist_delete", MagicMock(return_value=True))
    yield
    dc.dcpp_downloads.clear()
    dc._result_tokens.clear()
    dc._bundle_snapshot.clear()


class TestTranslateRemotePath:
    """AirDC++ and CLU frequently see the same folder under different names."""

    @pytest.mark.parametrize("path,remote,local,expected", [
        # Native Windows AirDC++ -> CLU in Docker.
        ("F:\\downloads\\temp\\x.cbz", "F:\\downloads\\temp\\", "/downloads/temp",
         "/downloads/temp/x.cbz"),
        # Trailing separators on either side are irrelevant.
        ("F:\\downloads\\temp\\x.cbz", "F:\\downloads\\temp", "/downloads/temp/",
         "/downloads/temp/x.cbz"),
        # Nested subfolders keep their shape, with separators converted.
        ("F:\\dl\\temp\\Batman\\x.cbz", "F:\\dl", "/downloads",
         "/downloads/temp/Batman/x.cbz"),
        # AirDC++ in its own container -> different mount point in CLU's.
        ("/downloads/temp/x.cbz", "/downloads", "/data/downloads",
         "/data/downloads/temp/x.cbz"),
        # CLU mounted at the filesystem root.
        ("/downloads/x.cbz", "/downloads", "/", "/x.cbz"),
        # Windows drive letters compare case-insensitively.
        ("f:\\downloads\\temp\\x.cbz", "F:\\Downloads\\Temp", "/downloads/temp",
         "/downloads/temp/x.cbz"),
        # CLU on Windows too: separators stay native.
        ("/downloads/x.cbz", "/downloads", "D:\\comics", "D:\\comics\\x.cbz"),
    ])
    def test_translates(self, path, remote, local, expected):
        assert dc.translate_remote_path(path, remote, local) == expected

    @pytest.mark.parametrize("path,remote,local", [
        # No local view configured — the correct setup when both agree.
        ("/downloads/temp/x.cbz", "/downloads/temp", None),
        ("/downloads/temp/x.cbz", "/downloads/temp", ""),
        # Path isn't under the configured root; don't invent a mapping.
        ("/elsewhere/x.cbz", "/downloads", "/data/downloads"),
        # POSIX paths are case-sensitive, unlike the Windows case above.
        ("/Downloads/x.cbz", "/downloads", "/data/downloads"),
        # Nothing to translate.
        (None, "/downloads", "/data"),
        ("", "/downloads", "/data"),
    ])
    def test_returns_path_untouched(self, path, remote, local):
        assert dc.translate_remote_path(path, remote, local) == path

    def test_root_itself_maps_to_the_local_root(self):
        assert dc.translate_remote_path(
            "F:\\downloads\\temp", "F:\\downloads\\temp\\", "/downloads/temp"
        ) == "/downloads/temp"

    def test_no_config_leaves_the_path_alone(self):
        # A client built without config must not blow up mid-poll.
        assert dc._to_local_path(MagicMock(config=None), "/x.cbz") == "/x.cbz"


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
    def test_one_search_on_the_series_name(self, mock_client):
        # A hub search costs tens of seconds and AirDC++ paces them, so DC++
        # must not fan out over zero-padding variants the way Usenet does.
        # Searching the series alone returns every padding in one go.
        client = _fake_client()
        mock_client.return_value = client
        dc.search_dcpp_for_issue("Strange Tales", "83")
        assert [c.args[0] for c in client.search.call_args_list] == ["Strange Tales"]

    @patch("models.dcpp._active_client")
    def test_narrows_only_when_results_are_truncated(self, mock_client):
        # A full page means the hub had more to say and the wanted issue could
        # be past the cut, so one narrower search earns its cost.
        client = _fake_client([
            _hit(f"Strange Tales v1 {i:03d}.cbz", result_id=str(i), tth=f"T{i}")
            for i in range(dc._TRUNCATED_RESULT_COUNT)
        ])
        mock_client.return_value = client
        dc.search_dcpp_for_issue("Strange Tales", "83")
        assert [c.args[0] for c in client.search.call_args_list] == [
            "Strange Tales", "Strange Tales 083",
        ]

    @patch("models.dcpp._active_client")
    def test_no_refinement_for_non_numeric_issues(self, mock_client):
        client = _fake_client([
            _hit(f"Series {i}.cbz", result_id=str(i), tth=f"T{i}")
            for i in range(dc._TRUNCATED_RESULT_COUNT)
        ])
        mock_client.return_value = client
        dc.search_dcpp_for_issue("Series", "1.MU")
        assert len(client.search.call_args_list) == 1


class TestNormalizeHubTitle:
    """Hub back-catalogues use a date prefix the shared scorer can't read."""

    def test_yyyymm_prefix_and_volume_token(self):
        title, vol = dc._normalize_hub_title(
            "196103 Strange Tales v1 083.cbz", "Strange Tales")
        assert title == "Strange Tales 083 (1961)"
        assert vol == 1

    def test_yyyy_prefix(self):
        title, vol = dc._normalize_hub_title(
            "1963 Strange Tales v1 114.cbr", "Strange Tales")
        assert title == "Strange Tales 114 (1963)"

    def test_short_sequence_prefix_yields_no_year(self):
        title, _ = dc._normalize_hub_title(
            "07 Strange Tales v1 120.cbr", "Strange Tales")
        assert title == "Strange Tales 120"

    def test_series_starting_with_digits_is_not_decapitated(self):
        # The guard that matters: "100" here is the series, not a prefix.
        title, _ = dc._normalize_hub_title("100 Bullets 001 (1999).cbz", "100 Bullets")
        assert title == "100 Bullets 001 (1999)"

    def test_leading_number_kept_when_series_does_not_follow(self):
        title, _ = dc._normalize_hub_title("2011 Some Other Book 001.cbz", "Strange Tales")
        assert title.startswith("2011 Some Other Book")

    def test_modern_scene_name_is_left_alone_apart_from_the_extension(self):
        title, vol = dc._normalize_hub_title(
            "Strange Tales 004 (2026) (Digital) (Shan-Empire).cbz", "Strange Tales")
        assert title == "Strange Tales 004 (2026) (Digital) (Shan-Empire)"
        assert vol is None

    def test_existing_year_is_not_duplicated(self):
        title, _ = dc._normalize_hub_title(
            "196103 Strange Tales v1 083 (1961).cbz", "Strange Tales")
        assert title.count("(1961)") == 1

    def test_handles_empty_input(self):
        assert dc._normalize_hub_title("", "Strange Tales") == ("", None)
        assert dc._normalize_hub_title("x.cbz", "") == ("x.cbz", None)


class TestHubNamingIsMatchable:
    """End-to-end scoring of real filenames seen on a live comic hub."""

    @patch("models.dcpp._active_client")
    def test_date_prefixed_back_catalogue_matches(self, mock_client):
        mock_client.return_value = _fake_client([
            _hit("196103 Strange Tales v1 083.cbz", result_id="1"),
            _hit("196104 Strange Tales v1 084.cbz", result_id="2"),
        ])
        res = dc.search_dcpp_for_issue("Strange Tales", "83", issue_year=1961)
        assert res["chosen"] is not None
        assert res["chosen"][0]["decision"] == "ACCEPT"
        # The user still sees the real filename, not the normalized form.
        assert res["chosen"][0]["title"] == "196103 Strange Tales v1 083.cbz"

    @patch("models.dcpp._active_client")
    def test_wrong_volume_is_rejected(self, mock_client):
        # Normalizing strips the vN token, so the volume check has to happen
        # here or a v1 file would satisfy a request for v2.
        mock_client.return_value = _fake_client([
            _hit("196103 Strange Tales v1 083.cbz", result_id="1"),
        ])
        res = dc.search_dcpp_for_issue(
            "Strange Tales", "83", issue_year=1961, series_volume=2)
        assert res["chosen"] is None
        assert res["all_results"][0]["decision"] == "REJECT"

    @patch("models.dcpp._active_client")
    def test_matching_volume_still_accepted(self, mock_client):
        mock_client.return_value = _fake_client([
            _hit("196103 Strange Tales v1 083.cbz", result_id="1"),
        ])
        res = dc.search_dcpp_for_issue(
            "Strange Tales", "83", issue_year=1961, series_volume=1)
        assert res["chosen"] is not None


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

    def _client_with_paths(self, state, status, remote, local):
        client = self._client(state, status)
        client.config = DownloadClientConfig(
            target_directory=remote, local_target_directory=local)
        return client

    @patch("models.dcpp._import_completed", return_value=True)
    def test_windows_host_path_is_translated_for_import(self, mock_import):
        # AirDC++ running natively on Windows reports a path no Linux
        # container can open.
        job = self._job()
        status = DownloadStatus(
            client_id="b1", status="complete",
            storage_path="F:\\downloads\\temp\\Batman 1.cbz")
        dc._poll_one(
            self._client_with_paths("found", status,
                                    "F:\\downloads\\temp\\", "/downloads/temp"),
            "d1", job)
        mock_import.assert_called_once_with("/downloads/temp/Batman 1.cbz", "Batman 1.cbz")
        assert dc.dcpp_downloads["d1"]["status"] == "complete"

    @patch("models.dcpp._import_completed", return_value=True)
    def test_containerized_airdcpp_path_is_translated(self, mock_import):
        # Both sides Linux, but AirDC++'s container mounts the share somewhere
        # else than CLU's does.
        job = self._job()
        status = DownloadStatus(client_id="b1", status="complete",
                                storage_path="/downloads/temp/Batman 1.cbz")
        dc._poll_one(
            self._client_with_paths("found", status, "/downloads", "/data/downloads"),
            "d1", job)
        mock_import.assert_called_once_with(
            "/data/downloads/temp/Batman 1.cbz", "Batman 1.cbz")

    @patch("models.dcpp._import_completed", return_value=True)
    def test_gone_path_is_translated_too(self, mock_import):
        # The cached target takes the same route when AirDC++ drops the bundle.
        job = self._job(target="F:\\downloads\\temp\\Batman 1.cbz")
        dc._poll_one(
            self._client_with_paths("gone", None,
                                    "F:\\downloads\\temp", "/downloads/temp"),
            "d1", job)
        mock_import.assert_called_once_with("/downloads/temp/Batman 1.cbz", "Batman 1.cbz")

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


class TestLedgerWrites:
    """When a job state change reaches the dcpp_jobs table, and when it doesn't."""

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

    @patch("models.dcpp._ensure_poller")
    @patch("models.dcpp._active_client")
    def test_grab_persists_before_starting_the_poller(self, mock_client, mock_poller):
        # The bundle is already live in AirDC++ by this point, so a crash
        # before the first poll must not lose our only record of it.
        client = _fake_client(bundle_id="bundle-7")
        mock_client.return_value = client
        token = dc._store_token("inst-1", _hit("Batman 001.cbz", result_id="5"))

        download_id = dc.grab_dcpp(token, "Batman 1.cbz", series="Batman", issue="1")

        dc._persist_new.assert_called_once()
        saved_id, saved_job = dc._persist_new.call_args[0]
        assert saved_id == download_id
        assert saved_job["client_id"] == "bundle-7"
        assert saved_job["series"] == "Batman"

    @patch("models.dcpp._ensure_poller")
    @patch("models.dcpp._active_client")
    def test_a_failed_grab_writes_nothing(self, mock_client, mock_poller):
        client = _fake_client()
        client.download_result.return_value = NZBSubmitResult(
            success=False, error="no target")
        mock_client.return_value = client
        token = dc._store_token("inst-1", _hit("Batman 001.cbz"))

        assert dc.grab_dcpp(token, "Batman 1.cbz") is None
        dc._persist_new.assert_not_called()

    def test_progress_writes_only_when_something_moved(self):
        self._job(percent=40, stage="Downloading", target="/dl/Batman 1.cbz")
        unchanged = DownloadStatus(
            client_id="b1", status="downloading", percent=40,
            stage="Downloading", storage_path="/dl/Batman 1.cbz")

        dc._update_progress("d1", unchanged)
        dc._persist_update.assert_not_called()

    @pytest.mark.parametrize("field,value", [
        ("percent", 41),
        ("stage", "Hashing"),
        ("storage_path", "/dl/elsewhere/Batman 1.cbz"),
    ])
    def test_progress_writes_when_a_tracked_field_changes(self, field, value):
        self._job(percent=40, stage="Downloading", target="/dl/Batman 1.cbz")
        kwargs = {"percent": 40, "stage": "Downloading",
                  "storage_path": "/dl/Batman 1.cbz"}
        kwargs[field] = value

        dc._update_progress("d1", DownloadStatus(
            client_id="b1", status="downloading", **kwargs))
        dc._persist_update.assert_called_once()

    def test_target_is_always_persisted_with_progress(self):
        # The cached target is the only thing that makes a completed-while-down
        # bundle importable, so it must reach the ledger, not just memory.
        self._job()
        dc._update_progress("d1", DownloadStatus(
            client_id="b1", status="downloading", percent=10,
            storage_path="/dl/Batman 1.cbz"))
        assert dc._persist_update.call_args.kwargs["target"] == "/dl/Batman 1.cbz"

    def test_clean_completion_clears_the_row(self):
        self._job()
        dc._set_status("d1", "complete", percent=100)
        dc._persist_delete.assert_called_once_with("d1")
        dc._persist_update.assert_not_called()

    @pytest.mark.parametrize("status", ["failed", "complete_no_move"])
    def test_unresolved_terminals_keep_their_row(self, status):
        # These are the jobs that still need a human; purging them would mean a
        # restart silently swallows exactly the ones worth seeing.
        self._job()
        dc._set_status("d1", status, error="boom")
        dc._persist_delete.assert_not_called()
        assert dc._persist_update.call_args.kwargs["status"] == status

    def test_status_change_on_an_unknown_job_writes_nothing(self):
        dc._set_status("missing", "complete")
        dc._persist_delete.assert_not_called()
        dc._persist_update.assert_not_called()


class TestRecoverDcppJobs:
    """Re-adopting bundles left in flight by a previous CLU process."""

    ROW = {
        "download_id": "d1", "client_type": "airdcpp", "client_id": "b1",
        "filename": "Batman 1.cbz", "series": "Batman", "issue": "1",
        "status": "downloading", "error": None, "percent": 55,
        "stage": "Downloading", "bytes_total": 100, "bytes_downloaded": 55,
        "target": "F:\\downloads\\temp\\Batman 1.cbz",
    }

    @patch("models.dcpp._ensure_poller")
    @patch("core.database.get_active_dcpp_jobs")
    def test_rehydrates_and_starts_the_poller(self, mock_rows, mock_poller):
        mock_rows.return_value = [dict(self.ROW)]

        assert dc.recover_dcpp_jobs() == 1

        job = dc.dcpp_downloads["d1"]
        assert job["client_id"] == "b1"
        assert job["percent"] == 55
        # The target is what makes a completed-while-down bundle importable.
        assert job["target"] == "F:\\downloads\\temp\\Batman 1.cbz"
        mock_poller.assert_called_once()

    @patch("models.dcpp._ensure_poller")
    @patch("core.database.get_active_dcpp_jobs")
    def test_makes_no_network_calls(self, mock_rows, mock_poller):
        # Recovery runs at import time under Gunicorn; a 10s-timeout HTTP call
        # per bundle would stall boot. The poller does the reconcile instead.
        mock_rows.return_value = [dict(self.ROW)]
        with patch("models.dcpp._active_client") as mock_client:
            dc.recover_dcpp_jobs()
        mock_client.assert_not_called()

    @patch("models.dcpp._ensure_poller")
    @patch("core.database.get_active_dcpp_jobs")
    def test_does_not_clobber_a_live_job(self, mock_rows, mock_poller):
        dc.dcpp_downloads["d1"] = {"status": "downloading", "percent": 99}
        mock_rows.return_value = [dict(self.ROW)]

        assert dc.recover_dcpp_jobs() == 0
        assert dc.dcpp_downloads["d1"]["percent"] == 99

    @patch("models.dcpp.dcpp_enabled_and_configured", return_value=False)
    @patch("models.dcpp._ensure_poller")
    @patch("core.database.get_active_dcpp_jobs", return_value=[])
    def test_nothing_to_recover_and_dcpp_off(self, mock_rows, mock_poller, mock_cfg):
        assert dc.recover_dcpp_jobs() == 0
        mock_poller.assert_not_called()

    @patch("models.dcpp.dcpp_enabled_and_configured", return_value=True)
    @patch("models.dcpp._ensure_poller")
    @patch("core.database.get_active_dcpp_jobs", return_value=[])
    def test_empty_ledger_still_polls_when_dcpp_is_on(self, mock_rows, mock_poller,
                                                     mock_cfg):
        # The snapshot backing the "untracked bundles" view needs the poller
        # running even with nothing of our own in flight.
        assert dc.recover_dcpp_jobs() == 0
        mock_poller.assert_called_once()

    @patch("models.dcpp._ensure_poller")
    @patch("core.database.get_active_dcpp_jobs", side_effect=Exception("db down"))
    def test_unreadable_ledger_is_not_fatal(self, mock_rows, mock_poller):
        assert dc.recover_dcpp_jobs() == 0

    @patch("models.dcpp._ensure_poller")
    @patch("core.database.get_active_dcpp_jobs")
    def test_recovered_job_completes_on_the_first_poll(self, mock_rows, mock_poller):
        # The whole point: AirDC++ finished and dropped the bundle while CLU
        # was down, so the only signal is a 404 plus the persisted target.
        mock_rows.return_value = [dict(self.ROW)]
        dc.recover_dcpp_jobs()

        client = MagicMock()
        client.get_bundle_state.return_value = ("gone", None)
        client.config = DownloadClientConfig(
            target_directory="F:\\downloads\\temp",
            local_target_directory="/downloads/temp")

        with patch("models.dcpp._import_completed", return_value=True) as mock_import:
            dc._poll_one(client, "d1", dc.dcpp_downloads["d1"])

        mock_import.assert_called_once_with(
            "/downloads/temp/Batman 1.cbz", "Batman 1.cbz")
        assert dc.dcpp_downloads["d1"]["status"] == "complete"
        dc._persist_delete.assert_called_once_with("d1")


class TestUntrackedBundles:
    """AirDC++'s own queue, surfaced read-only alongside CLU's jobs."""

    def _status(self, client_id="b9", name="Some Other Thing.rar"):
        return DownloadStatus(client_id=client_id, name=name, status="downloading",
                              percent=12.0, stage="Downloading",
                              bytes_total=200, bytes_downloaded=24)

    def _client(self, queue):
        client = MagicMock()
        client.client_type = ClientType.AIRDCPP
        client.get_queue.return_value = queue
        return client

    def test_snapshot_surfaces_bundles_clu_never_submitted(self):
        dc._refresh_bundle_snapshot(self._client([self._status()]))

        rows = dc.get_dcpp_downloads()
        assert len(rows) == 1
        assert rows[0]["untracked"] is True
        assert rows[0]["download_id"] is None
        assert rows[0]["filename"] == "Some Other Thing.rar"
        assert rows[0]["client_type"] == "airdcpp"
        assert rows[0]["percent"] == 12.0

    def test_a_tracked_bundle_is_never_listed_twice(self):
        dc.dcpp_downloads["d1"] = {
            "client_type": "airdcpp", "client_id": "b9", "filename": "Batman 1.cbz",
            "status": "downloading", "percent": 50,
        }
        dc._refresh_bundle_snapshot(self._client([self._status(client_id="b9")]))

        rows = dc.get_dcpp_downloads()
        assert len(rows) == 1
        assert rows[0]["untracked"] is False
        assert rows[0]["filename"] == "Batman 1.cbz"

    def test_a_failed_listing_leaves_the_last_snapshot_alone(self):
        dc._refresh_bundle_snapshot(self._client([self._status()]))
        broken = MagicMock()
        broken.client_type = ClientType.AIRDCPP
        broken.get_queue.side_effect = Exception("unreachable")

        dc._refresh_bundle_snapshot(broken)
        assert len(dc.get_dcpp_downloads()) == 1

    def test_bundles_without_an_id_are_skipped(self):
        dc._refresh_bundle_snapshot(
            self._client([DownloadStatus(client_id="", status="downloading")]))
        assert dc.get_dcpp_downloads() == []


class TestPollLoopLifecycle:
    """The loop no longer exits the moment nothing is pending."""

    @pytest.fixture(autouse=True)
    def _no_sleeping(self, monkeypatch):
        monkeypatch.setattr(dc.time, "sleep", MagicMock())

    @patch("models.dcpp.dcpp_enabled_and_configured", return_value=False)
    @patch("models.dcpp._active_client", return_value=None)
    def test_exits_when_dcpp_is_off_and_nothing_pending(self, mock_client, mock_cfg):
        dc._poll_loop()  # returns rather than hanging
        assert dc.get_dcpp_downloads() == []

    @patch("models.dcpp.dcpp_enabled_and_configured", return_value=True)
    @patch("models.dcpp._active_client")
    def test_keeps_running_with_nothing_pending(self, mock_client, mock_cfg):
        # The idle heartbeat is what keeps AirDC++'s own queue visible.
        client = MagicMock()
        client.client_type = ClientType.AIRDCPP
        client.get_queue.return_value = [
            DownloadStatus(client_id="b9", name="Other.rar", status="downloading")
        ]
        # Second round: DC++ has been switched off, so the loop may exit.
        mock_client.side_effect = [client, None]
        mock_cfg.side_effect = [False]

        dc._poll_loop()

        assert client.get_queue.called
        assert dc.time.sleep.call_args_list[0][0][0] == dc._IDLE_POLL_INTERVAL

    @patch("models.dcpp.dcpp_enabled_and_configured")
    @patch("models.dcpp._pending_jobs")
    @patch("models.dcpp._active_client", return_value=None)
    def test_a_broken_round_does_not_kill_the_thread(self, mock_client, mock_pending,
                                                     mock_cfg):
        # If an exception escaped, nothing would ever restart the poller and
        # every tracked bundle would stall for the life of the process.
        mock_pending.side_effect = [Exception("boom"), {}]
        mock_cfg.side_effect = [False]

        dc._poll_loop()
        assert mock_pending.call_count == 2

    @patch("models.dcpp.dcpp_enabled_and_configured")
    @patch("models.dcpp._active_client", return_value=None)
    def test_backs_off_when_pending_but_clientless(self, mock_client, mock_cfg):
        # Nothing can be polled, so don't spin the fast cadence; and once DC++
        # is deconfigured the loop must exit even with a job still pending,
        # rather than holding the thread open forever.
        dc.dcpp_downloads["d1"] = {"status": "downloading"}
        mock_cfg.side_effect = [True, False]

        dc._poll_loop()
        assert dc.time.sleep.call_args_list[0][0][0] == dc._IDLE_POLL_INTERVAL
        assert mock_cfg.call_count == 2


class TestDismissDcppJob:

    def test_forgets_a_tracked_job(self):
        dc.dcpp_downloads["d1"] = {"status": "failed"}
        assert dc.dismiss_dcpp_job("d1") is True
        assert "d1" not in dc.dcpp_downloads
        dc._persist_delete.assert_called_once_with("d1")

    def test_row_only_job_is_still_dismissable(self):
        # A restart hydrates from the ledger, but a dismiss can race a restart.
        assert dc.dismiss_dcpp_job("d-ghost") is True

    def test_unknown_job_reports_miss(self):
        dc._persist_delete.return_value = False
        assert dc.dismiss_dcpp_job("nope") is False
