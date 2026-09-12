"""Tests for the reading-list year policy.

Covers the two primitives in core/metadata_dates.py (``years_in`` /
``years_in_path``) and the decision function in models/cbl.py
(``year_verdict``) that decides whether a candidate file could plausibly be
the issue a reading list is asking for.

The bug these exist to prevent: a reading list searches the whole library for
"series + issue number", which for a rebooted character has many
correct-looking answers. "Batwoman (2026) #7" matched "Batwoman 007 (2012)"
and "Batman (2025) #14" matched "Batman 014 (1942)", because no year was ever
allowed to rule a candidate out.
"""
import pytest

from core.metadata_dates import years_in, years_in_path
from models.cbl import (
    year_verdict,
    VERDICT_ISSUE,
    VERDICT_VOLUME,
    VERDICT_UNKNOWN,
    VERDICT_NONE,
    _as_year,
)


class TestYearsIn:

    def test_plain_year(self):
        assert years_in("Batman 001 (2016).cbz") == {2016}

    def test_every_year_is_kept(self):
        # Unlike issue_year_from_filename, which abstains on two candidates,
        # this keeps both -- each is a separate chance to match.
        assert years_in("Batman 001 (2016) (2019 reprint).cbz") == {2016, 2019}

    def test_pixel_height_is_not_a_year(self):
        assert years_in("Batman 001 (2016) (Digital) (1920px).cbz") == {2016}

    def test_scanner_credit_is_not_a_year(self):
        assert years_in("Batman 001 (2016) (Hal2008).cbz") == {2016}

    def test_volume_marker_excluded_by_default(self):
        assert years_in("Batman v2016 001.cbz") == set()

    def test_volume_marker_included_on_request(self):
        assert years_in("Batman v2016 001.cbz", include_volume_marker=True) == {2016}

    def test_implausible_years_dropped(self):
        assert years_in("Weird 001 (1850) (2525).cbz") == set()

    def test_sole_candidate_equal_to_issue_number_is_dropped(self):
        # Topolino reached #3600; "1904" here is the issue, not the year.
        assert years_in("Topolino 1904.cbz", issue_number="1904") == set()

    def test_issue_number_lookalike_kept_when_not_alone(self):
        # Two years means we cannot be confident either way, so neither is
        # discarded -- dropping one would manufacture false confidence.
        assert years_in("Topolino 1904 (c2c) (2008).cbz",
                        issue_number="1904") == {1904, 2008}

    def test_empty_input(self):
        assert years_in(None) == set()
        assert years_in("") == set()


class TestYearsInPath:

    def test_reads_folder_and_filename(self):
        assert years_in_path("/data/DC/Batman (2025)/Batman 014 (2026).cbz") == {2025, 2026}

    def test_folder_year_only(self):
        assert years_in_path("/data/DC/Batman (2025)/Batman 014.cbz") == {2025}

    def test_mylar_volume_folder(self):
        assert years_in_path("/data/DC/Batman/v2025/Batman 014.cbz") == {2025}

    def test_windows_separators(self):
        assert years_in_path(r"C:\data\DC\Batman (2025)\Batman 014 (2026).cbz") == {2025, 2026}

    def test_no_year_anywhere(self):
        assert years_in_path("/data/DC/Batman/Batman 014.cbz") == set()

    def test_empty_input(self):
        assert years_in_path(None) == set()


class TestAsYear:

    @pytest.mark.parametrize("value", ["2016", 2016, " 2016 "])
    def test_accepts_four_digit_years(self, value):
        assert _as_year(value) == 2016

    @pytest.mark.parametrize("value", [None, "", "3", 3, "42721", "v2016", "abc"])
    def test_rejects_non_years(self, value):
        # Metron hands us series.volume (an ordinal) and ComicVine a volume id;
        # neither may be mistaken for a year.
        assert _as_year(value) is None


class TestYearVerdict:

    def test_no_candidate_years_is_unknown(self):
        # Yearless libraries (and every CBR, which nothing tags) must keep
        # matching exactly as they did before.
        assert year_verdict(set(), volume_year=2025, issue_years={2026}) == VERDICT_UNKNOWN

    def test_no_hints_is_unknown(self):
        assert year_verdict({2016}) == VERDICT_UNKNOWN

    def test_issue_year_match(self):
        assert year_verdict({2026}, volume_year=2025, issue_years={2026}) == VERDICT_ISSUE

    def test_volume_year_match_when_file_has_no_issue_year(self):
        assert year_verdict({2025}, volume_year=2025, issue_years={2026}) == VERDICT_VOLUME

    def test_any_matching_year_is_enough(self):
        # Folder carries the volume year, filename the issue year.
        assert year_verdict({2025, 2026}, volume_year=2025, issue_years={2026}) == VERDICT_ISSUE

    def test_unrelated_path_year_cannot_force_rejection(self):
        assert year_verdict({2024, 2025, 2026}, volume_year=2025,
                            issue_years={2026}) == VERDICT_ISSUE

    def test_cover_and_store_year_both_accepted(self):
        # An issue shipping in November under a January cover date is honestly
        # labelled either way.
        assert year_verdict({2025}, volume_year=2025, issue_years={2025, 2026}) == VERDICT_ISSUE
        assert year_verdict({2026}, volume_year=2025, issue_years={2025, 2026}) == VERDICT_ISSUE

    # ── The reported bugs ────────────────────────────────────────────────────

    def test_rejects_batwoman_2012_for_2026_volume(self):
        assert year_verdict({2012}, volume_year=2026, issue_years={2026}) == VERDICT_NONE

    def test_rejects_batman_1942_for_2025_volume(self):
        assert year_verdict({1942}, volume_year=2025, issue_years={2026}) == VERDICT_NONE

    def test_issue_year_is_authoritative_over_the_volume_floor(self):
        # Searching the 1942 volume must not accept a 2026 file just because
        # 2026 clears a 1942 floor.
        assert year_verdict({2026}, volume_year=1942, issue_years={1943}) == VERDICT_NONE

    # ── Volume year alone: a floor, not a window ─────────────────────────────

    def test_later_year_accepted_when_only_volume_year_known(self):
        # Issue #40 of a 2015 volume is honestly named "(2024)". A +/-1 window
        # would throw it away; that is the false negative this must not have.
        assert year_verdict({2024}, volume_year=2015) == VERDICT_VOLUME

    def test_year_before_the_volume_is_rejected(self):
        assert year_verdict({2011}, volume_year=2015) == VERDICT_NONE

    def test_one_year_of_slack_below_the_volume_year(self):
        # A volume whose first issues shipped the previous December.
        assert year_verdict({2014}, volume_year=2015) == VERDICT_VOLUME
        assert year_verdict({2013}, volume_year=2015) == VERDICT_NONE
