"""Tests for core/metadata_dates.py — the issue-level date cross-check."""
import pytest

from core.metadata_dates import (
    year_is_issue_level,
    MODE_ENFORCE,
    MODE_LOG,
    MODE_OFF,
    date_check_mode,
    date_check_tolerance,
    date_conflict,
    evaluate,
    evaluate_series,
    issue_year_from_filename,
    series_conflict_message,
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
        # Pixel-height tags are extremely common on digital releases, and
        # "1920px" is a plausible-looking year.
        "Batman 001 (1920px).cbz",
        "Batman 001 (2000px).cbz",
        "X-Men 012 (1920dpi).cbz",
    ])
    def test_rejects_scan_credits(self, name):
        assert issue_year_from_filename(name) is None

    def test_a_pixel_tag_does_not_mask_the_real_year(self):
        """The regression this guards: a following-letter tag used to either be
        read as the year, or make the filename ambiguous so the check silently
        skipped a file that did carry a usable date."""
        assert issue_year_from_filename(
            "Batman 001 (2016) (Digital) (1920px).cbz") == 2016

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


class TestIssueNumberReadAsAYear:
    """A four-digit issue number looks exactly like a year, and used to be read
    as one.

    "Topolino 1904.cbz" is issue #1904 of a run that has passed #3,600. Nothing
    in the string distinguishes those digits from the year in "Batman 001
    (2016).cbz" -- a space before and a dot after, in both -- so the check
    compared 1904 against the issue's real 1992 and rejected a correct match.
    The only thing that tells them apart is that the caller has already read
    those digits as the issue number.
    """

    @pytest.mark.parametrize("name,number", [
        ("Topolino 1904.cbz", "1904"),
        ("Topolino 2024.cbz", "2024"),
        ("Diabolik 1985.cbr", "1985"),
        # However the caller spells the number.
        ("Topolino 1904.cbz", "01904"),
        ("Topolino 1904.cbz", 1904),
    ])
    def test_the_issue_number_is_not_a_year(self, name, number):
        assert issue_year_from_filename(name, number) is None

    @pytest.mark.parametrize("name,number,expected", [
        # The ordinary case: the year and the issue number are different
        # numbers, and nothing changes.
        ("Batman 001 (1940).cbz", "1", 1940),
        ("Topolino 3221 (2017).cbz", "3221", 2017),
        ("001 - Il re del terrore (Mondadori 1962-11).cbz", "1", 1962),
    ])
    def test_a_real_year_is_untouched(self, name, number, expected):
        assert issue_year_from_filename(name, number) == expected

    def test_it_also_rescues_a_file_the_check_used_to_skip(self):
        """Discarding the number before counting candidates, not after.

        "Topolino 1904 (1992).cbz" names two plausible years, so the check used
        to abstain as ambiguous. One of them is the issue number, so once it is
        dropped the remaining candidate is the year the file actually states and
        the check can do its job.
        """
        assert issue_year_from_filename("Topolino 1904 (1992).cbz") is None
        assert issue_year_from_filename("Topolino 1904 (1992).cbz", "1904") == 1992

    def test_a_year_shaped_number_that_really_is_the_year_is_given_up(self):
        """The deliberate cost of the fix, recorded so it is a decision and not
        a surprise.

        A yearbook numbered by its year -- "L'economia di Zio Paperone 1992" is
        issue 1992, published in 1992 -- loses the check rather than passing it.
        Giving up a check means the file is tagged as it was before the check
        existed, which is a far smaller price than rejecting a correct match,
        and there is no way to tell the two cases apart from the filename.
        """
        assert issue_year_from_filename("L'economia di Zio Paperone 1992.cbz",
                                        "1992") is None

    @pytest.mark.parametrize("number", [None, "", "  ", "1904A", "1904.1", "abc"])
    def test_an_unusable_number_changes_nothing(self, number):
        """Anything that is not a bare run of digits leaves the old behaviour
        exactly as it was -- including a suffixed number, which never produced a
        year anyway since _YEAR requires a non-alphanumeric on both sides."""
        assert issue_year_from_filename("Batman 001 (1940).cbz", number) == 1940

    def test_omitting_the_number_is_the_old_behaviour(self):
        """Every existing caller that does not pass one keeps what it had."""
        assert issue_year_from_filename("Topolino 1904.cbz") == 1904

    def test_evaluate_passes_the_number_through(self, monkeypatch):
        """The whole point: no conflict is raised for a correct match whose
        issue number happens to look like a year."""
        monkeypatch.setattr("core.metadata_dates.date_check_mode",
                            lambda: MODE_ENFORCE)
        monkeypatch.setattr("core.metadata_dates.date_check_tolerance", lambda: 2)

        mode, conflicted, year = evaluate("Topolino 1904.cbz", "1992-05-24")
        assert conflicted is True and year == 1904

        mode, conflicted, year = evaluate("Topolino 1904.cbz", "1992-05-24", "1904")
        assert conflicted is False and year is None


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
        monkeypatch.setattr("core.database.get_user_preference",
                            lambda key, default=None: raw)
        assert date_check_mode() == expected

    def test_mode_defaults_off_when_the_preference_is_unreadable(self, monkeypatch):
        def boom(key, default=None):
            raise RuntimeError("no database")
        monkeypatch.setattr("core.database.get_user_preference", boom)
        assert date_check_mode() == MODE_OFF

    def test_tolerance_rejects_negative(self, monkeypatch):
        monkeypatch.setattr("core.database.get_user_preference",
                            lambda key, default=None: -5)
        assert date_check_tolerance() == 2

    def test_tolerance_defaults_when_the_value_is_not_a_number(self, monkeypatch):
        monkeypatch.setattr("core.database.get_user_preference",
                            lambda key, default=None: "abc")
        assert date_check_tolerance() == 2

    def test_tolerance_accepts_a_stored_string(self, monkeypatch):
        """Preferences round-trip through JSON, so a value may come back as str."""
        monkeypatch.setattr("core.database.get_user_preference",
                            lambda key, default=None: "5")
        assert date_check_tolerance() == 5


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
        assert self._fn()(metadata, "gcd") == expected

    @pytest.mark.parametrize("metadata", [
        {}, None, {"Month": 6}, {"Year": None}, {"Year": "not a year"},
    ])
    def test_returns_none_without_a_year(self, metadata):
        assert self._fn()(metadata, "gcd") is None

    @pytest.mark.parametrize("provider", ["mangadex", "bedetheque", None, "unknown"])
    def test_series_level_year_is_not_an_issue_date(self, provider):
        """One decision point: if the provider only has a series year, there is
        no issue date to return, so callers cannot accidentally use it."""
        assert self._fn()({"Year": 1989, "Month": 1}, provider) is None


class TestYearIsIssueLevel:
    """Manga providers put the *series* year in ComicInfo Year.

    MangaDex (mangadex_provider.py:376), AniList (anilist_provider.py:381) and
    MangaUpdates all assign series_year to Year, and leave IssueResult.cover_date
    as None. Comparing that against a filename would reject every volume of a
    long-running series.
    """

    @pytest.mark.parametrize("provider", [
        "mangadex", "MangaDex", "anilist", "AniList",
        "mangaupdates", "MangaUpdates",
    ])
    def test_manga_providers_are_excluded(self, provider):
        assert year_is_issue_level(provider) is False

    @pytest.mark.parametrize("provider", [
        "gcd", "GCD", "GCD API", "comicvine", "ComicVine",
        "ComicVine (Local DB)", "Metron",
    ])
    def test_comic_providers_are_included(self, provider):
        assert year_is_issue_level(provider) is True

    @pytest.mark.parametrize("provider", [None, "", "SomeNewProvider"])
    def test_unknown_provider_is_excluded(self, provider):
        """An unrecognised source is not worth rejecting a match over."""
        assert year_is_issue_level(provider) is False

    def test_bedetheque_is_excluded(self):
        """Bedetheque assigns series.year to Year, same as the manga providers
        (bedetheque_provider.py:537 and :571), and carries no cover_date."""
        assert year_is_issue_level("bedetheque") is False

    def test_every_registered_provider_is_classified(self):
        """A provider added later must be classified deliberately.

        Without this, a new provider reporting a series year in ComicInfo Year
        would silently start producing false rejections -- or, if the default
        went the other way, would silently never be checked.
        """
        from core.metadata_dates import (
            _ISSUE_YEAR_PROVIDERS, _SERIES_YEAR_PROVIDERS,
        )
        from models.providers import ProviderType

        classified = _ISSUE_YEAR_PROVIDERS | _SERIES_YEAR_PROVIDERS
        registered = {p.value for p in ProviderType}
        assert registered - classified == set(), (
            "unclassified providers: "
            f"{sorted(registered - classified)} — add each to "
            "_ISSUE_YEAR_PROVIDERS or _SERIES_YEAR_PROVIDERS in "
            "core/metadata_dates.py"
        )
        assert classified - registered == set(), (
            f"classified but not registered: {sorted(classified - registered)}"
        )

    def test_the_two_sets_are_disjoint(self):
        from core.metadata_dates import (
            _ISSUE_YEAR_PROVIDERS, _SERIES_YEAR_PROVIDERS,
        )
        assert _ISSUE_YEAR_PROVIDERS & _SERIES_YEAR_PROVIDERS == set()

    def test_a_real_manga_volume_would_otherwise_conflict(self):
        """The case this guard exists for."""
        from core.metadata_dates import date_conflict, issue_year_from_filename
        filename_year = issue_year_from_filename("Berserk v22 (2003).cbz")
        assert filename_year == 2003
        # Series began 1989 — fourteen years out, and entirely correct.
        assert date_conflict(filename_year, 1989, tolerance=2) is True
        assert year_is_issue_level("mangadex") is False


class TestEvaluateSeries:
    """The folder-level comparison: folder year vs the series' start year.

    Kept apart from `evaluate` because the two years mean different things — a
    folder year names the year the series began, a filename year names the year
    one issue came out.
    """

    def test_off_short_circuits(self, mode):
        mode(MODE_OFF)
        assert evaluate_series("Diabolik (2014)", 2014, 1999) == (MODE_OFF, False)

    def test_log_reports_the_conflict(self, mode):
        mode(MODE_LOG)
        assert evaluate_series("Diabolik (2014)", 2014, 1999) == (MODE_LOG, True)

    def test_enforce_reports_the_conflict(self, mode):
        mode(MODE_ENFORCE)
        assert evaluate_series("Diabolik (2014)", 2014, 1999) == (MODE_ENFORCE, True)

    def test_agreeing_years_are_not_a_conflict(self, mode):
        mode(MODE_ENFORCE)
        assert evaluate_series("Diabolik (2014)", 2014, 2014) == (MODE_ENFORCE, False)

    def test_within_tolerance_is_not_a_conflict(self, mode):
        """Providers disagree by a year on when a series started."""
        mode(MODE_ENFORCE)
        assert evaluate_series("Diabolik (2014)", 2014, 2013) == (MODE_ENFORCE, False)

    @pytest.mark.parametrize("folder_year,series_year", [
        (None, 1999),
        (2014, None),
        (2014, ""),
        (None, None),
    ])
    def test_missing_either_side_is_never_a_conflict(self, mode, folder_year, series_year):
        mode(MODE_ENFORCE)
        _mode, conflicted = evaluate_series("Diabolik", folder_year, series_year)
        assert conflicted is False

    def test_accepts_a_string_start_year(self, mode):
        """ComicVine hands start_year back as a string."""
        mode(MODE_ENFORCE)
        assert evaluate_series("Diabolik (2014)", 2014, "1999") == (MODE_ENFORCE, True)


class TestSeriesConflictMessage:

    def test_names_both_sides(self):
        msg = series_conflict_message("Diabolik (2014)", 2014, "1999")
        assert "Diabolik (2014)" in msg
        assert "2014" in msg and "1999" in msg
