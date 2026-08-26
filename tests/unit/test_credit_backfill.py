"""Unit tests for the Metron credit backfill sweep.

Metron records are routinely completed after a comic ships, so a file tagged on
release morning can carry a valid ComicInfo.xml with no creators at all -- and
once ComicInfo has Notes, every automatic tagging path skips the file forever.
These cover the sweep that repairs those files, and the two rules that keep it
from churning: it only rewrites when Metron actually has credits now, and it
never touches a file it can't identify.
"""
import os
import zipfile
from unittest.mock import patch

import pytest

from core.comicinfo import read_comicinfo_from_zip
from core.credit_backfill import _has_credits, backfill_file, run_credit_backfill


CREDIT_LESS_XML = b"""<?xml version='1.0' encoding='utf-8'?>
<ComicInfo>
  <Series>Absolute Catwoman</Series>
  <Number>3</Number>
  <Genre>Super-Hero</Genre>
  <Characters>Catwoman (Earth Alpha)</Characters>
  <MetronId>172615</MetronId>
  <Notes>Metadata from Metron. Resource URL: https://metron.cloud/issue/absolute-catwoman-2026-3/ - modified 2026-08-26T06:20:00.</Notes>
</ComicInfo>
"""

CREDITED_METADATA = {
    "Series": "Absolute Catwoman",
    "Number": "3",
    "Writer": "Che Grayson, Scott Snyder",
    "Penciller": "Bengal",
    "Colorist": "Giovanna Niro",
    "Letterer": "Lucas Gattoni",
    "MetronId": 172615,
    "Notes": "Metadata from Metron. Resource URL: https://metron.cloud/issue/absolute-catwoman-2026-3/ - modified 2026-08-26T08:35:11.",
}


def _make_cbz(tmp_path, name="Absolute Catwoman 003 (2026).cbz", xml=CREDIT_LESS_XML):
    path = os.path.join(str(tmp_path), name)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("001.jpg", b"not-really-an-image")
        if xml is not None:
            z.writestr("ComicInfo.xml", xml)
    return path


@pytest.fixture
def no_index_write():
    """The sweep updates file_index after a rewrite; unit tests have no DB."""
    with patch("core.database.update_file_index_from_comicinfo", return_value=True):
        yield


class TestHasCredits:

    def test_any_credit_counts(self):
        assert _has_credits({"CoverArtist": "Eric Canete"}) is True

    def test_blank_and_missing_do_not(self):
        assert _has_credits({"Writer": "   ", "Penciller": None}) is False
        assert _has_credits({}) is False

    def test_editor_alone_is_not_a_credit(self):
        """The Metron mapper never emits Editor, so it can't prove completeness."""
        assert _has_credits({"Editor": "Andrew Marino"}) is False


class TestBackfillFile:

    def test_adds_credits_and_keeps_existing_tags(self, tmp_path, no_index_write):
        path = _make_cbz(tmp_path)

        with patch("models.metron.fetch_issue_detail", return_value={"id": 172615}), \
             patch("models.metron.map_to_comicinfo", return_value=CREDITED_METADATA):
            assert backfill_file(object(), path) == "updated"

        result = read_comicinfo_from_zip(path)
        assert result["Writer"] == "Che Grayson, Scott Snyder"
        assert result["Penciller"] == "Bengal"
        # merge_existing: Metron supplied no Genre this time, and the sweep must
        # add credits rather than replace what the file already had.
        assert result["Genre"] == "Super-Hero"
        # The images are still there -- the archive was rebuilt, not replaced.
        with zipfile.ZipFile(path) as z:
            assert "001.jpg" in z.namelist()

    def test_fetches_by_the_metron_id_in_the_file(self, tmp_path, no_index_write):
        path = _make_cbz(tmp_path)

        with patch("models.metron.fetch_issue_detail", return_value={"id": 172615}) as fetch, \
             patch("models.metron.map_to_comicinfo", return_value=CREDITED_METADATA):
            backfill_file(object(), path)

        assert fetch.call_args[0][1] == 172615

    def test_still_empty_leaves_the_file_untouched(self, tmp_path, no_index_write):
        """Metron still has no creators: don't rewrite, don't move the mtime."""
        path = _make_cbz(tmp_path)
        before = open(path, "rb").read()

        with patch("models.metron.fetch_issue_detail", return_value={"id": 172615}), \
             patch("models.metron.map_to_comicinfo", return_value={"Series": "Absolute Catwoman"}):
            assert backfill_file(object(), path) == "still-empty"

        assert open(path, "rb").read() == before

    def test_dry_run_writes_nothing(self, tmp_path, no_index_write):
        path = _make_cbz(tmp_path)
        before = open(path, "rb").read()

        with patch("models.metron.fetch_issue_detail", return_value={"id": 172615}), \
             patch("models.metron.map_to_comicinfo", return_value=CREDITED_METADATA):
            assert backfill_file(object(), path, dry_run=True) == "updated"

        assert open(path, "rb").read() == before

    def test_already_credited_file_is_skipped(self, tmp_path, no_index_write):
        """The index row can be stale; the archive is the authority."""
        xml = CREDIT_LESS_XML.replace(
            b"<Number>3</Number>", b"<Number>3</Number>\n  <Writer>Che Grayson</Writer>"
        )
        path = _make_cbz(tmp_path, xml=xml)

        with patch("models.metron.fetch_issue_detail") as fetch:
            assert backfill_file(object(), path) == "skipped"
        fetch.assert_not_called()

    def test_non_metron_file_is_skipped(self, tmp_path, no_index_write):
        xml = b"""<?xml version='1.0' encoding='utf-8'?>
<ComicInfo>
  <Series>Absolute Catwoman</Series>
  <Number>3</Number>
  <Notes>Metadata from ComicVine CVDB. [Issue ID 4000-1189465]</Notes>
</ComicInfo>
"""
        path = _make_cbz(tmp_path, xml=xml)

        with patch("models.metron.fetch_issue_detail") as fetch:
            assert backfill_file(object(), path) == "skipped"
        fetch.assert_not_called()

    def test_file_without_comicinfo_is_skipped(self, tmp_path, no_index_write):
        path = _make_cbz(tmp_path, xml=None)

        with patch("models.metron.fetch_issue_detail") as fetch:
            assert backfill_file(object(), path) == "skipped"
        fetch.assert_not_called()

    def test_missing_file_is_skipped(self, tmp_path):
        assert backfill_file(object(), os.path.join(str(tmp_path), "gone.cbz")) == "skipped"

    def test_failed_fetch_is_an_error_not_a_rewrite(self, tmp_path, no_index_write):
        path = _make_cbz(tmp_path)
        before = open(path, "rb").read()

        with patch("models.metron.fetch_issue_detail", return_value=None):
            assert backfill_file(object(), path) == "error"

        assert open(path, "rb").read() == before

    def test_old_file_without_metron_id_resolves_via_cvinfo(self, tmp_path, no_index_write):
        """Files tagged before <MetronId> existed only carry the Notes line."""
        xml = CREDIT_LESS_XML.replace(b"  <MetronId>172615</MetronId>\n", b"")
        path = _make_cbz(tmp_path, xml=xml)
        cvinfo = os.path.join(str(tmp_path), "cvinfo")
        with open(cvinfo, "w", encoding="utf-8") as f:
            f.write("https://comicvine.gamespot.com/absolute-catwoman/4050-165432/\nseries_id: 15845\n")

        with patch("models.metron.get_issue_metadata", return_value={"id": 172615}) as lookup, \
             patch("models.metron.fetch_issue_detail") as fetch, \
             patch("models.metron.map_to_comicinfo", return_value=CREDITED_METADATA):
            assert backfill_file(object(), path) == "updated"

        assert lookup.call_args[0][1] == 15845
        assert lookup.call_args[0][2] == "3"
        # That lookup already fetched the issue live (get_issue_metadata purges
        # the cached body itself), so re-fetching would only burn Metron quota.
        fetch.assert_not_called()


class TestRunCreditBackfill:

    def test_no_metron_reports_and_stops(self):
        with patch("models.metron.get_flask_api", return_value=None):
            summary = run_credit_backfill()
        assert summary["skipped_reason"] == "metron-unavailable"
        assert summary["checked"] == 0

    def test_counts_each_outcome(self, tmp_path):
        paths = ["a.cbz", "b.cbz", "c.cbz", "d.cbz"]
        outcomes = iter(["updated", "still-empty", "skipped", "error"])

        with patch("models.metron.get_flask_api", return_value=object()), \
             patch("core.credit_backfill.find_credit_less_files", return_value=paths), \
             patch("core.credit_backfill.backfill_file", side_effect=lambda *a, **k: next(outcomes)):
            summary = run_credit_backfill()

        assert summary["checked"] == 4
        assert summary["updated"] == 1
        assert summary["still_empty"] == 1
        assert summary["skipped"] == 1
        assert summary["errors"] == 1
        assert summary["stopped_early"] is False

    def test_stops_when_the_daily_quota_is_spent(self):
        """Every remaining fetch would be a no-op; don't churn through them."""
        from models import metron

        with patch("models.metron.get_flask_api", return_value=object()), \
             patch("core.credit_backfill.find_credit_less_files", return_value=["a.cbz", "b.cbz"]), \
             patch.object(metron._metron_pacer, "daily_limit_reached", return_value=True), \
             patch("core.credit_backfill.backfill_file") as work:
            summary = run_credit_backfill()

        work.assert_not_called()
        assert summary["stopped_early"] is True

    def test_scans_wider_than_it_fetches(self):
        """``limit`` caps Metron fetches, not files examined. Skips are nearly
        free, and letting them fill the query's LIMIT would starve the files
        that actually need a fetch."""
        from core.credit_backfill import SCAN_MULTIPLIER

        with patch("models.metron.get_flask_api", return_value=object()), \
             patch("core.credit_backfill.find_credit_less_files", return_value=[]) as find:
            run_credit_backfill(days=7, limit=25)

        assert find.call_args.kwargs == {"days": 7, "limit": 25 * SCAN_MULTIPLIER}

    def test_skips_do_not_consume_the_fetch_budget(self):
        paths = [f"{i}.cbz" for i in range(10)]

        with patch("models.metron.get_flask_api", return_value=object()), \
             patch("core.credit_backfill.find_credit_less_files", return_value=paths), \
             patch("core.credit_backfill.backfill_file", return_value="skipped"):
            summary = run_credit_backfill(limit=2)

        assert summary["checked"] == 10
        assert summary["stopped_early"] is False

    def test_stops_once_the_fetch_budget_is_spent(self):
        paths = [f"{i}.cbz" for i in range(10)]

        with patch("models.metron.get_flask_api", return_value=object()), \
             patch("core.credit_backfill.find_credit_less_files", return_value=paths), \
             patch("core.credit_backfill.backfill_file", return_value="updated"):
            summary = run_credit_backfill(limit=2)

        assert summary["updated"] == 2
        assert summary["stopped_early"] is True


class TestFindCreditLessFiles:

    def test_query_window_and_limit(self):
        """Recency uses mtime, not metadata_scanned_at: the scanner refreshes
        the latter, which would keep a file in the window forever."""
        from core.credit_backfill import find_credit_less_files

        captured = {}

        class _Conn:
            def execute(self, sql, params):
                captured["sql"] = sql
                captured["params"] = params
                return self

            def fetchall(self):
                return [("/data/DC/a.cbz",)]

            def close(self):
                pass

        with patch("core.database.get_db_connection", return_value=_Conn()):
            assert find_credit_less_files(days=10, limit=5) == ["/data/DC/a.cbz"]

        assert "modified_at >= ?" in captured["sql"]
        assert "ci_writer" in captured["sql"] and "ci_coverartist" in captured["sql"]
        assert captured["params"][1] == 5

    def test_database_failure_returns_nothing(self):
        from core.credit_backfill import find_credit_less_files

        with patch("core.database.get_db_connection", return_value=None):
            assert find_credit_less_files() == []
