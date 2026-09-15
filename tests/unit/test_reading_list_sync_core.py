"""Tests for core.reading_list_sync -- source classification, change tokens, probes.

The point of this module is that an unchanged list costs one request. Most of
what is asserted here is that claim: the right question is asked, the answer is
compared correctly, and the expensive half does not run when it does not have to.
"""
import hashlib
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from core.reading_list_sync import (
    COMICVINE_ARC,
    GITHUB,
    METRON_ARC,
    METRON_LIST,
    _fingerprint,
    _metron_bulk_prefilter,
    _token_date,
    comicvine_arc_token,
    github_token,
    is_syncable,
    metron_arc_token,
    metron_list_token,
    parse_source,
    probe,
    stored_token,
)


class TestParseSource:

    def test_metron_reading_list(self):
        assert parse_source("metron://reading-list/42") == (METRON_LIST, 42)

    def test_metron_arc(self):
        assert parse_source("metron://arc/7") == (METRON_ARC, 7)

    def test_comicvine_arc(self):
        assert parse_source("comicvine://arc/9999") == (COMICVINE_ARC, 9999)

    def test_github_blob_url_is_converted_to_raw(self):
        kind, ident = parse_source(
            "https://github.com/DieselTech/CBL-ReadingLists/blob/main/a.cbl"
        )
        assert kind == GITHUB
        assert ident == "https://raw.githubusercontent.com/DieselTech/CBL-ReadingLists/main/a.cbl"

    def test_github_raw_url_is_left_alone(self):
        kind, ident = parse_source(
            "https://raw.githubusercontent.com/DieselTech/CBL-ReadingLists/main/a.cbl"
        )
        assert kind == GITHUB
        assert ident.startswith("https://raw.githubusercontent.com/")

    @pytest.mark.parametrize("source", [
        None,
        "",
        "uploaded_file.cbl",
        "metron://series/5",
        "comicvine://volume/5",
        "metron://arc/notanumber",
        "https://example.com/evil.cbl",
    ])
    def test_unsyncable_sources(self, source):
        assert parse_source(source) == (None, None)
        assert is_syncable(source) is False

    def test_a_lookalike_host_is_not_github(self):
        """Hostname, not substring: github.com.evil.example is not GitHub."""
        assert parse_source("https://github.com.evil.example/x.cbl") == (None, None)


class TestTokens:

    def test_github_token_is_the_content_hash(self):
        content = "<ReadingList/>"
        assert github_token(content) == hashlib.sha256(content.encode()).hexdigest()

    def test_metron_list_token_is_the_modified_stamp(self):
        assert metron_list_token({"modified": "2026-03-04 11:00:00+00:00"}) == \
            "2026-03-04 11:00:00+00:00"

    def test_metron_list_token_is_none_without_one(self):
        assert metron_list_token({}) is None
        assert metron_list_token(None) is None

    def test_arc_fingerprint_ignores_order(self):
        """Metron re-ordering its response is not a change to the arc."""
        a = metron_arc_token([{"id": 3}, {"id": 1}, {"id": 2}])
        b = metron_arc_token([{"id": 1}, {"id": 2}, {"id": 3}])
        assert a == b

    def test_arc_fingerprint_moves_when_an_issue_joins(self):
        before = metron_arc_token([{"id": 1}, {"id": 2}])
        after = metron_arc_token([{"id": 1}, {"id": 2}, {"id": 3}])
        assert before != after

    def test_arc_fingerprint_moves_when_an_issue_leaves(self):
        before = metron_arc_token([{"id": 1}, {"id": 2}])
        after = metron_arc_token([{"id": 1}])
        assert before != after

    def test_comicvine_token_covers_membership_and_date(self):
        base = {"issues": [{"id": 1}, {"id": 2}], "date_last_updated": "2026-01-01"}
        same = {"issues": [{"id": 2}, {"id": 1}], "date_last_updated": "2026-01-01"}
        new_issue = {"issues": [{"id": 1}, {"id": 2}, {"id": 3}],
                     "date_last_updated": "2026-01-01"}
        touched = {"issues": [{"id": 1}, {"id": 2}], "date_last_updated": "2026-02-02"}

        assert comicvine_arc_token(base) == comicvine_arc_token(same)
        assert comicvine_arc_token(base) != comicvine_arc_token(new_issue)
        # CV does not reliably move date_last_updated when an issue is
        # associated, which is why membership is in there too -- but when it
        # does move, that counts on its own.
        assert comicvine_arc_token(base) != comicvine_arc_token(touched)

    def test_fingerprint_drops_empties(self):
        assert _fingerprint([1, None, 2, ""]) == _fingerprint([2, 1])


class TestStoredToken:

    def test_prefers_source_version(self):
        assert stored_token({"source_version": "new", "source_hash": "old"}) == "new"

    def test_falls_back_to_source_hash(self):
        """Upgraded GitHub rows have only source_hash; they are not 'changed'."""
        assert stored_token({"source_version": None, "source_hash": "old"}) == "old"

    def test_none_when_never_synced(self):
        assert stored_token({}) is None


class TestTokenDate:

    def test_reads_the_date_off_a_metron_stamp(self):
        assert _token_date("2026-03-04 11:00:00+00:00") == date(2026, 3, 4)

    @pytest.mark.parametrize("token", [None, "", "deadbeef" * 8, "not-a-date"])
    def test_unparseable_means_probe_it(self, token):
        assert _token_date(token) is None


class TestProbe:

    def _cv_row(self, stored=None):
        return {"id": 1, "name": "Arc", "source": "comicvine://arc/55",
                "source_version": stored}

    def test_unsyncable_source_is_an_error_not_a_crash(self):
        result = probe({"id": 1, "source": "uploaded.cbl"})
        assert result.ok is False
        assert "synced" in result.error

    @patch("models.comicvine.fetch_cv_arc_detail")
    @patch("models.comicvine.get_cv_api_key", return_value="key")
    def test_unchanged_comicvine_arc_never_resolves_its_issues(self, _key, mock_detail):
        """The whole saving. fetch_cv_arc_issues is one request per issue."""
        detail = {"id": 55, "issues": [{"id": 1}, {"id": 2}],
                  "date_last_updated": "2026-01-01"}
        mock_detail.return_value = detail

        with patch("models.comicvine.fetch_cv_arc_issues") as mock_issues:
            result = probe(self._cv_row(stored=comicvine_arc_token(detail)))
            assert mock_issues.called is False

        assert result.ok
        assert result.changed is False
        assert mock_detail.call_count == 1

    @patch("models.comicvine.fetch_cv_arc_detail")
    @patch("models.comicvine.get_cv_api_key", return_value="key")
    def test_changed_comicvine_arc_reports_changed(self, _key, mock_detail):
        mock_detail.return_value = {"id": 55, "issues": [{"id": 1}, {"id": 2}],
                                    "date_last_updated": "2026-01-01"}
        result = probe(self._cv_row(stored="something-else"))
        assert result.changed is True

    @patch("models.comicvine.fetch_cv_arc_detail")
    @patch("models.comicvine.get_cv_api_key", return_value="key")
    def test_force_reports_changed_on_a_matching_token(self, _key, mock_detail):
        detail = {"id": 55, "issues": [{"id": 1}], "date_last_updated": "2026-01-01"}
        mock_detail.return_value = detail
        result = probe(self._cv_row(stored=comicvine_arc_token(detail)), force=True)
        assert result.changed is True
        # The token it found is still what gets stored afterwards.
        assert result.token == comicvine_arc_token(detail)

    @patch("models.comicvine.get_cv_api_key", return_value=None)
    def test_unconfigured_provider_is_an_error(self, _key):
        result = probe(self._cv_row())
        assert result.ok is False
        assert "ComicVine" in result.error

    @patch("core.reading_list_sync._metron_api", return_value=None)
    def test_metron_lockout_is_an_error_not_a_change(self, _api):
        """is_metron_configured returns False while the auth lockout is latched,
        so a sweep on a rejected credential must not decide the list changed."""
        result = probe({"id": 1, "source": "metron://reading-list/3"})
        assert result.ok is False
        assert result.changed is False

    @patch("models.metron.fetch_reading_list_detail")
    @patch("core.reading_list_sync._metron_api")
    def test_metron_list_probe_uses_the_modified_stamp(self, mock_api, mock_detail):
        mock_api.return_value = MagicMock()
        mock_detail.return_value = {"id": 3, "name": "Crisis",
                                    "modified": "2026-05-05 09:00:00+00:00"}
        row = {"id": 1, "source": "metron://reading-list/3",
               "source_version": "2026-05-05 09:00:00+00:00"}
        result = probe(row)
        assert result.changed is False
        assert result.kind == METRON_LIST
        assert result.ident == 3

    @patch("models.metron.fetch_arc_issues")
    @patch("core.reading_list_sync._metron_api")
    def test_metron_arc_probe_keeps_the_issues_it_fetched(self, mock_api, mock_issues):
        """apply() must not pay for the same listing twice."""
        mock_api.return_value = MagicMock()
        mock_issues.return_value = [{"id": 1}, {"id": 2}]
        result = probe({"id": 1, "source": "metron://arc/7"})
        assert result.changed is True
        assert result.payload["issues"] == [{"id": 1}, {"id": 2}]

    @patch("core.reading_list_sync.requests.get", side_effect=OSError("boom"))
    def test_a_dead_source_is_reported_not_raised(self, _get):
        result = probe({"id": 1, "name": "x",
                        "source": "https://raw.githubusercontent.com/a/b/main/c.cbl"})
        assert result.ok is False
        assert result.changed is False


class TestMetronBulkPrefilter:
    """One modified_gt call stands in for a detail request per list."""

    def _rows(self, *pairs):
        return [{"id": i, "source": f"metron://reading-list/{ident}",
                 "source_version": token}
                for i, (ident, token) in enumerate(pairs, start=1)]

    @patch("models.metron.list_reading_lists_modified_since")
    @patch("core.reading_list_sync._metron_api")
    def test_a_list_metron_does_not_mention_is_skipped(self, mock_api, mock_changed):
        mock_api.return_value = MagicMock()
        mock_changed.return_value = {}
        rows = self._rows((10, "2026-05-01 00:00:00+00:00"),
                          (11, "2026-05-02 00:00:00+00:00"))
        assert _metron_bulk_prefilter(rows) == {10, 11}

    @patch("models.metron.list_reading_lists_modified_since")
    @patch("core.reading_list_sync._metron_api")
    def test_a_list_metron_reports_changed_is_not_skipped(self, mock_api, mock_changed):
        mock_api.return_value = MagicMock()
        mock_changed.return_value = {10: "2026-06-09 00:00:00+00:00"}
        rows = self._rows((10, "2026-05-01 00:00:00+00:00"),
                          (11, "2026-05-02 00:00:00+00:00"))
        assert _metron_bulk_prefilter(rows) == {11}

    @patch("models.metron.list_reading_lists_modified_since")
    @patch("core.reading_list_sync._metron_api")
    def test_same_stamp_in_the_window_is_still_skipped(self, mock_api, mock_changed):
        """modified_gt is date-granular, so a list can be listed without having
        moved since we stored it."""
        mock_api.return_value = MagicMock()
        stamp = "2026-05-01 08:00:00+00:00"
        mock_changed.return_value = {10: stamp}
        assert _metron_bulk_prefilter(self._rows((10, stamp))) == {10}

    @patch("models.metron.list_reading_lists_modified_since")
    @patch("core.reading_list_sync._metron_api")
    def test_the_window_steps_back_a_day(self, mock_api, mock_changed):
        """Metron's filter is exclusive on the date: asking for
        modified_gt=<the day we synced> hides a list edited later that day."""
        mock_api.return_value = MagicMock()
        mock_changed.return_value = {}
        _metron_bulk_prefilter(self._rows((10, "2026-05-10 00:00:00+00:00")))
        since = mock_changed.call_args[0][1]
        assert since == "2026-05-09"

    @patch("models.metron.list_reading_lists_modified_since", return_value=None)
    @patch("core.reading_list_sync._metron_api")
    def test_a_failed_call_skips_nothing(self, mock_api, _changed):
        """None is 'the call failed'; an empty dict is 'nothing changed'. Only
        the second is evidence, and only evidence lets a list be skipped."""
        mock_api.return_value = MagicMock()
        assert _metron_bulk_prefilter(self._rows((10, "2026-05-01 00:00:00+00:00"))) == set()

    @patch("core.reading_list_sync._metron_api")
    def test_a_never_synced_list_is_not_skipped(self, mock_api):
        mock_api.return_value = MagicMock()
        assert _metron_bulk_prefilter(self._rows((10, None))) == set()

    @patch("models.metron.list_reading_lists_modified_since")
    @patch("core.reading_list_sync._metron_api")
    def test_a_list_older_than_the_window_is_not_skipped(self, mock_api, mock_changed):
        """The clamped call cannot speak for it, so it gets probed instead."""
        mock_api.return_value = MagicMock()
        mock_changed.return_value = {}
        ancient = (date.today() - timedelta(days=900)).strftime("%Y-%m-%d") + " 00:00:00+00:00"
        recent = (date.today() - timedelta(days=5)).strftime("%Y-%m-%d") + " 00:00:00+00:00"
        skip = _metron_bulk_prefilter(self._rows((10, ancient), (11, recent)))
        assert 10 not in skip
        assert 11 in skip


class TestApply:
    """The expensive half: what the source now holds, diffed into the list."""

    def _probe_result(self, **kw):
        from core.reading_list_sync import ProbeResult
        return ProbeResult(**kw)

    @patch("core.database.update_reading_list_source_version")
    @patch("core.database.update_reading_list_description")
    @patch("core.database.sync_reading_list_entries", return_value={"added": 2, "removed": 1})
    @patch("models.cbl.CBLLoader.match_file", return_value=None)
    @patch("models.cbl.CBLLoader.prefetch_metron_ids")
    @patch("models.metron.fetch_reading_list_items")
    def test_metron_list_entries_are_diffed_not_rebuilt(
            self, mock_items, _prefetch, _match, mock_sync, _desc, mock_stamp):
        from core.reading_list_sync import apply

        mock_items.return_value = [
            {"order": 2, "issue": {"id": 2, "number": "2", "cover_date": "2026-02-01",
                                   "series": {"name": "Batman", "year_began": 2025}}},
            {"order": 1, "issue": {"id": 1, "number": "1", "cover_date": "2026-01-01",
                                   "series": {"name": "Batman", "year_began": 2025}}},
        ]
        result = self._probe_result(
            kind=METRON_LIST, ident=42, changed=True, token="tok",
            payload={"api": MagicMock(), "detail": {"desc": "<p>hi</p>"}},
        )

        outcome = apply({"id": 9, "name": "x"}, result, "{series_name} {issue_number}")

        assert outcome == {"added": 2, "removed": 1}
        entries = mock_sync.call_args[0][1]
        # The order Metron gives, not the order the API happened to answer in.
        assert [e["issue_number"] for e in entries] == ["1", "2"]
        assert [e["metron_id"] for e in entries] == [1, 2]
        mock_stamp.assert_called_once_with(9, "tok")

    @patch("core.database.update_reading_list_source_version")
    @patch("core.database.sync_reading_list_entries", return_value=None)
    @patch("models.cbl.CBLLoader.match_file", return_value=None)
    @patch("models.cbl.CBLLoader.prefetch_metron_ids")
    def test_a_failed_diff_does_not_stamp_the_token(self, _prefetch, _match,
                                                    _sync, mock_stamp):
        """Stamping a token the entries never reached would make the next sweep
        skip a list that was never actually updated."""
        from core.reading_list_sync import apply

        result = self._probe_result(kind=METRON_ARC, ident=7, changed=True,
                                    token="tok", payload={"issues": [{"id": 1}]})
        assert apply({"id": 9, "name": "x"}, result, None) is None
        assert mock_stamp.called is False

    @patch("core.database.update_reading_list_source_version")
    @patch("core.database.sync_reading_list_entries", return_value={"added": 0, "removed": 0})
    @patch("models.cbl.CBLLoader.match_file", return_value=None)
    @patch("models.comicvine.fetch_cv_arc_issues")
    def test_comicvine_issues_are_only_resolved_once_changed(
            self, mock_issues, _match, mock_sync, _stamp):
        from core.reading_list_sync import apply

        mock_issues.return_value = [
            {"series_name": "Batman", "issue_number": "1",
             "volume_year": 2016, "cover_date": "2016-06-01"},
        ]
        result = self._probe_result(
            kind=COMICVINE_ARC, ident=55, changed=True, token="tok",
            payload={"api_key": "k", "detail": {"issues": [{"id": 1}]}},
        )
        apply({"id": 9, "name": "x"}, result, None)

        entry = mock_sync.call_args[0][1][0]
        # A CV volume id is an identifier, never a year -- the CBL export
        # writes this column as Volume.
        assert entry["volume"] is None
        assert entry["year"] == 2016


class TestSyncAll:
    """The nightly sweep."""

    def _rows(self):
        return [
            {"id": 1, "name": "A", "source": "metron://reading-list/10",
             "source_version": "2026-05-01 00:00:00+00:00"},
            {"id": 2, "name": "B", "source": "metron://reading-list/11",
             "source_version": "2026-05-02 00:00:00+00:00"},
            {"id": 3, "name": "C", "source": "uploaded.cbl", "source_version": None},
        ]

    @patch("core.reading_list_sync.sync_one")
    @patch("core.reading_list_sync._metron_bulk_prefilter")
    @patch("core.database.get_syncable_reading_lists")
    def test_prefiltered_lists_are_never_probed(self, mock_rows, mock_prefilter,
                                                mock_sync_one):
        """The point of the modified_gt call: a list Metron says is unchanged
        costs no request of its own."""
        from core.reading_list_sync import sync_all

        mock_rows.return_value = self._rows()
        mock_prefilter.return_value = {10, 11}

        summary = sync_all()

        assert mock_sync_one.called is False
        assert summary["checked"] == 0
        assert summary["unchanged"] == 2

    @patch("core.reading_list_sync.sync_one")
    @patch("core.reading_list_sync._metron_bulk_prefilter", return_value=set())
    @patch("core.database.get_syncable_reading_lists")
    def test_an_unsyncable_row_is_skipped_entirely(self, mock_rows, _pre, mock_sync_one):
        """An uploaded .cbl is in the table but has nowhere to go back to."""
        from core.reading_list_sync import sync_all

        mock_rows.return_value = self._rows()
        mock_sync_one.return_value = {"success": True, "changed": False}

        sync_all()

        synced_ids = [c.args[0] for c in mock_sync_one.call_args_list]
        assert synced_ids == [1, 2]

    @patch("core.reading_list_sync.sync_one", side_effect=RuntimeError("boom"))
    @patch("core.reading_list_sync._metron_bulk_prefilter", return_value=set())
    @patch("core.database.get_syncable_reading_lists")
    def test_one_bad_list_does_not_stop_the_sweep(self, mock_rows, _pre, _sync_one):
        from core.reading_list_sync import sync_all

        mock_rows.return_value = self._rows()
        summary = sync_all()
        assert summary["checked"] == 2
        assert summary["failed"] == 2

    @patch("core.reading_list_sync.sync_one")
    @patch("core.reading_list_sync._metron_bulk_prefilter",
           side_effect=RuntimeError("metron down"))
    @patch("core.database.get_syncable_reading_lists")
    def test_a_failed_prefilter_falls_back_to_probing(self, mock_rows, _pre, mock_sync_one):
        from core.reading_list_sync import sync_all

        mock_rows.return_value = self._rows()
        mock_sync_one.return_value = {"success": True, "changed": False}
        summary = sync_all()
        assert summary["checked"] == 2


class TestSyncOne:

    @patch("core.database.update_reading_list_source_version")
    @patch("core.reading_list_sync.probe")
    @patch("core.database.get_reading_list")
    def test_unchanged_list_still_learns_a_missing_token(self, mock_get, mock_probe,
                                                         mock_stamp):
        from core.reading_list_sync import ProbeResult, sync_one

        mock_get.return_value = {"id": 4, "name": "x", "source": "metron://arc/7"}
        mock_probe.return_value = ProbeResult(kind=METRON_ARC, ident=7, changed=False,
                                              token="tok", stored=None)
        outcome = sync_one(4)
        assert outcome["changed"] is False
        mock_stamp.assert_called_once_with(4, "tok")

    @patch("core.database.get_reading_list", return_value=None)
    def test_missing_list_is_reported_not_raised(self, _get):
        from core.reading_list_sync import sync_one
        assert sync_one(999)["success"] is False
