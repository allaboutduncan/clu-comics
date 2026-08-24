"""Tests for core/metadata_dates.py — the issue-level date cross-check."""
import pytest

from core.metadata_dates import (
    MODE_ENFORCE,
    MODE_LOG,
    MODE_OFF,
    date_check_mode,
    date_check_tolerance,
    date_conflict,
    evaluate,
    issue_year_from_filename,
)


@pytest.fixture
def mode(monkeypatch):
    """Set DATE_CHECK_MODE / tolerance without touching config.ini."""
    def _set(value, tolerance=2):
        monkeypatch.setattr("core.metadata_dates.date_check_mode", lambda: value)
        monkeypatch.setattr("core.metadata_dates.date_check_tolerance", lambda: tolerance)
    return _set


class TestIssueYearFromFilename:

    @pytest.mark.parametrize("name,expected", [
        ("Batman 001 (1940).cbz", 1940),
        ("Topolino 3221 (2017).cbz", 2017),
        # The scene-release form extract_year_from_name misses entirely.
        ("001 - Il re del terrore (Mondadori 1962-11).cbz", 1962),
        ("Diabolik - Nero Su Nero #001 (2014).cbz", 2014),
        ("Speciale Nathan Never 001 1992.cbz", 1992),
    ])
    def test_accepts_a_standalone_year(self, name, expected):
        assert issue_year_from_filename(name) == expected

    @pytest.mark.parametrize("name", [
        # Scan credits are not publication dates.
        "Batman 001 (Hal2008).cbz",
        "Batman 001 [bud_666].cbz",
        "Batman 001 by Scanner2019x.cbz",
    ])
    def test_rejects_scan_credits(self, name):
        assert issue_year_from_filename(name) is None

    @pytest.mark.parametrize("name", [
        "Batman 001.cbz",
        "",
        None,
        "Spider-Man 100000 pages.cbz",
        "Series 1850 (1850).cbz",   # before the plausible range
    ])
    def test_returns_none_without_a_usable_year(self, name):
        assert issue_year_from_filename(name) is None

    @pytest.mark.parametrize("name", [
        "X-Men v1998 012.cbz",
        "X-Men v2011 012.cbz",
    ])
    def test_volume_marker_is_not_an_issue_date(self, name):
        """vYYYY names the volume/series year, not this issue's date.

        Reading it as an issue date would make every later issue of a long run
        look like a conflict.
        """
        assert issue_year_from_filename(name) is None

    def test_volume_marker_does_not_mask_a_real_date(self):
        assert issue_year_from_filename("X-Men v1998 012 (2003).cbz") == 2003

    def test_two_different_years_is_not_a_claim(self):
        """Ambiguity is never guessed — two candidates disambiguate nothing."""
        assert issue_year_from_filename("Batman (1940) reprint (2011).cbz") is None

    def test_same_year_twice_is_still_one_claim(self):
        assert issue_year_from_filename("Batman (1940) 001 1940.cbz") == 1940


class TestDateConflict:

    @pytest.mark.parametrize("issue_date", ["1999-06-01", "1999-06", "1999", 1999])
    def test_accepts_every_shape_a_provider_returns(self, issue_date):
        assert date_conflict(2014, issue_date, tolerance=2) is True

    def test_within_tolerance_is_not_a_conflict(self):
        assert date_conflict(2014, "2013-12-01", tolerance=2) is False
        assert date_conflict(2014, "2016-01-01", tolerance=2) is False

    def test_exactly_at_tolerance_is_allowed(self):
        assert date_conflict(2014, "2012", tolerance=2) is False

    def test_one_past_tolerance_conflicts(self):
        assert date_conflict(2014, "2011", tolerance=2) is True

    @pytest.mark.parametrize("filename_year,issue_date", [
        (None, "1999"),
        (2014, None),
        (2014, ""),
        (2014, "not a date"),
        (None, None),
    ])
    def test_missing_or_unparseable_is_never_a_conflict(self, filename_year, issue_date):
        assert date_conflict(filename_year, issue_date, tolerance=2) is False


class TestEvaluate:

    def test_off_short_circuits_without_parsing(self, mode, monkeypatch):
        """The disabled path must not even look at the filename."""
        mode(MODE_OFF)
        called = []
        monkeypatch.setattr("core.metadata_dates.issue_year_from_filename",
                            lambda name: called.append(name))
        assert evaluate("Diabolik 001 (2014).cbz", "1999") == (MODE_OFF, False, None)
        assert called == []

    def test_log_reports_the_conflict(self, mode):
        mode(MODE_LOG)
        result_mode, conflicted, year = evaluate("Diabolik 001 (2014).cbz", "1999")
        assert (result_mode, conflicted, year) == (MODE_LOG, True, 2014)

    def test_enforce_reports_the_conflict(self, mode):
        mode(MODE_ENFORCE)
        assert evaluate("Diabolik 001 (2014).cbz", "1999") == (MODE_ENFORCE, True, 2014)

    def test_agreeing_dates_are_not_a_conflict(self, mode):
        mode(MODE_ENFORCE)
        assert evaluate("Diabolik 001 (2014).cbz", "2014-05-01") == (MODE_ENFORCE, False, 2014)

    def test_no_year_in_filename_is_not_a_conflict(self, mode):
        mode(MODE_ENFORCE)
        assert evaluate("Diabolik 001.cbz", "1999") == (MODE_ENFORCE, False, None)


class TestSettings:
    """Settings are read at call time, and bad values fall back safely."""

    @pytest.mark.parametrize("raw,expected", [
        ("off", MODE_OFF),
        ("log", MODE_LOG),
        ("enforce", MODE_ENFORCE),
        ("  ENFORCE  ", MODE_ENFORCE),
        ("nonsense", MODE_OFF),
        ("", MODE_OFF),
    ])
    def test_mode_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setattr("core.metadata_dates.config.get",
                            lambda *a, **k: raw)
        assert date_check_mode() == expected

    def test_mode_defaults_off_when_config_raises(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("no config")
        monkeypatch.setattr("core.metadata_dates.config.get", boom)
        assert date_check_mode() == MODE_OFF

    def test_tolerance_rejects_negative(self, monkeypatch):
        monkeypatch.setattr("core.metadata_dates.config.getint", lambda *a, **k: -5)
        assert date_check_tolerance() == 2

    def test_tolerance_defaults_when_config_raises(self, monkeypatch):
        def boom(*a, **k):
            raise ValueError("not an int")
        monkeypatch.setattr("core.metadata_dates.config.getint", boom)
        assert date_check_tolerance() == 2


class TestIssueDateOfComicInfo:
    """routes/metadata.py turns a ComicInfo dict back into a comparable date."""

    def _fn(self):
        from routes.metadata import _issue_date_of
        return _issue_date_of

    @pytest.mark.parametrize("metadata,expected", [
        ({"Year": 1999, "Month": 6}, "1999-06"),
        ({"Year": 1999}, "1999"),
        ({"Year": "1999", "Month": "6"}, "1999-06"),
        ({"Year": 1999, "Month": None}, "1999"),
    ])
    def test_builds_a_date(self, metadata, expected):
        assert self._fn()(metadata) == expected

    @pytest.mark.parametrize("metadata", [
        {}, None, {"Month": 6}, {"Year": None}, {"Year": "not a year"},
    ])
    def test_returns_none_without_a_year(self, metadata):
        assert self._fn()(metadata) is None
