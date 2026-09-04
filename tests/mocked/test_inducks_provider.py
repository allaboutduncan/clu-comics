"""Tests for the INDUCKS model and provider adapter.

Runs against a real temporary SQLite database rather than mocked cursors: the
queries here have traps in them (an empty-string date that sorts before every
real one, a character-name join that multiplies rows) that only real rows catch.
"""
import sqlite3

import pytest

from models.providers.base import ProviderType, SearchResult, IssueResult
from tests.mocked.conftest import build_inducks_sqlite


class TestInducksProviderInit:

    def test_provider_attributes(self):
        from models.providers.inducks_provider import InducksProvider

        p = InducksProvider()
        assert p.provider_type == ProviderType.INDUCKS
        assert p.display_name == "INDUCKS (Disney)"
        assert p.requires_auth is True
        assert p.auth_fields == ["database_path"]

    def test_registered(self):
        from models.providers import get_provider_class
        from models.providers.inducks_provider import InducksProvider

        assert get_provider_class(ProviderType.INDUCKS) is InducksProvider


class TestInducksConnection:

    def test_available_when_configured(self, inducks_configured):
        from models.inducks import check_database_status, is_database_available

        assert is_database_available() is True
        assert check_database_status()["inducks_available"] is True

    def test_unavailable_when_unconfigured(self, monkeypatch):
        from models.inducks import check_database_status

        monkeypatch.setattr("models.inducks._get_saved_credentials", lambda: None)
        monkeypatch.delenv("INDUCKS_DATABASE_PATH", raising=False)
        status = check_database_status()
        assert status["inducks_available"] is False
        assert status["inducks_path_configured"] is False

    def test_env_var_fallback(self, inducks_db_path, monkeypatch):
        from models.inducks import get_connection_params

        monkeypatch.setattr("models.inducks._get_saved_credentials", lambda: None)
        monkeypatch.setenv("INDUCKS_DATABASE_PATH", str(inducks_db_path))
        assert get_connection_params() == {"database_path": str(inducks_db_path)}

    def test_opens_read_only(self, inducks_configured):
        """The build is 700 MB and shared; a write would also drop -wal files beside it."""
        from models.inducks import get_connection

        conn = get_connection()
        assert conn is not None
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE scribble (x INTEGER)")
        conn.close()

    def test_successful_connection_test(self, inducks_configured, inducks_creds):
        from models.providers.inducks_provider import InducksProvider

        assert InducksProvider(credentials=inducks_creds).test_connection() is True

    def test_missing_core_tables(self, tmp_path, monkeypatch):
        from models.providers.inducks_provider import InducksProvider
        import models.inducks as inducks_module

        path = str(tmp_path / "broken.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE inducks_publication (publicationcode TEXT)")
        conn.commit()
        conn.close()

        inducks_module.invalidate_inducks_table_cache()
        monkeypatch.setattr("models.inducks._get_saved_credentials",
                            lambda: {"database_path": path})
        try:
            assert InducksProvider().test_connection() is False
        finally:
            inducks_module.invalidate_inducks_table_cache()


class TestStartYear:
    """The derived start year, which the auto-accept gate depends on.

    ``core.bulk_metadata._years_match`` returns False the moment either side is
    None, so a publication with no year can never auto-accept -- every file
    lands in the review queue with nothing in the log to explain why.
    """

    def test_ignores_blank_and_unknown_dates(self, inducks_configured):
        """it/TL has an empty oldestdate and a 9999 sentinel among its issues.

        A bare MIN(SUBSTR(oldestdate, 1, 4)) returns the empty string here,
        because '' sorts before every real date. Measured on the real database
        that mistake blanks the year for 44 of 664 Italian publications instead
        of 13 -- including Topolino itself.
        """
        from models.inducks import search_series

        results = search_series("Topolino (libretto)")
        assert len(results) == 1
        assert results[0]["year_began"] == 1949

    def test_naive_min_would_have_failed(self, inducks_db_path):
        """Pin the trap itself, so nobody 'simplifies' the query back."""
        conn = sqlite3.connect(inducks_db_path)
        naive = conn.execute(
            "SELECT MIN(SUBSTR(oldestdate, 1, 4)) FROM inducks_issue "
            "WHERE publicationcode = 'it/TL'"
        ).fetchone()[0]
        conn.close()
        assert naive == ""

    def test_no_usable_date_yields_none(self, inducks_configured):
        """2% of Italian publications genuinely have no date. That is not a bug."""
        from models.inducks import search_series

        results = search_series("Senza data")
        assert len(results) == 1
        assert results[0]["year_began"] is None

    def test_issue_count_is_reported(self, inducks_configured):
        from models.inducks import search_series

        assert search_series("Topolino (libretto)")[0]["issue_count"] == 4


class TestSearchSeries:

    def test_exact_title_wins(self, inducks_configured):
        from models.inducks import search_series

        results = search_series("Topolino (giornale)")
        assert [r["id"] for r in results] == ["it/TG"]

    def test_ambiguous_name_returns_every_candidate(self, inducks_configured):
        """Two publications strip to "Topolino"; guessing between them is how a
        library ends up confidently mislabelled."""
        from models.inducks import search_series

        results = search_series("Topolino")
        assert {r["id"] for r in results} == {"it/TL", "it/TG"}

    def test_country_scoping(self, inducks_configured):
        """us/WDC exists but is out of scope for an Italian library."""
        from models.inducks import search_series

        assert search_series("Walt Disney's Comics and Stories") == []
        assert len(search_series("Walt Disney's Comics and Stories",
                                 country_codes=["us"])) == 1

    def test_accents_and_punctuation_are_folded(self, inducks_configured):
        from models.inducks import search_series

        assert [r["id"] for r in search_series("zio  paperone!")] == ["it/ZP"]

    def test_no_match(self, inducks_configured):
        from models.inducks import search_series

        assert search_series("Nonexistent") == []
        assert search_series("") == []

    def test_year_orders_candidates(self, inducks_configured):
        from models.inducks import search_series

        assert search_series("Topolino", 1932)[0]["id"] == "it/TG"
        assert search_series("Topolino", 1949)[0]["id"] == "it/TL"

    def test_qualifier_kept_when_stripping_would_collide(self, inducks_configured):
        """Both Topolinos keep their qualifier; Zio Paperone has none to lose."""
        from models.inducks import search_series

        assert search_series("Topolino (libretto)")[0]["name"] == "Topolino (libretto)"
        assert search_series("Zio Paperone")[0]["name"] == "Zio Paperone"


class TestNameNormalisation:
    """A Disney folder is named after a slice of a run far more often than
    after the publication, and INDUCKS spells the run marker differently again."""

    def test_strip_run_marker(self):
        from models.inducks import strip_run_marker

        assert strip_run_marker("Topolino anno 1975") == "Topolino"
        assert strip_run_marker("Topolino Anno 1984 voilumi 1466-1518") == "Topolino"
        # Nothing but markers: the folder names a slice of a run and no
        # publication at all, so the caller has to fall back to the filename.
        assert strip_run_marker("Anno 1986 vol 1571-1622") == ""
        assert strip_run_marker("Albo d'oro v2") == "Albo d'oro"
        assert strip_run_marker("Diabolik 001-100") == "Diabolik"
        assert strip_run_marker("Anno 1970 - numeri 736-787  Repack") == ""

    def test_strip_run_marker_leaves_a_real_title_alone(self):
        """The marker only ever sits at the end, and a title that ends in a
        number is a title."""
        from models.inducks import strip_run_marker

        assert strip_run_marker("Topolino 1000") == "Topolino 1000"
        assert strip_run_marker("Topolino (libretto)") == "Topolino (libretto)"
        assert strip_run_marker("100 Anni di Fumetto Italiano") == "100 Anni di Fumetto Italiano"

    def test_normalise_ordinals(self):
        from models.inducks import normalise_ordinals

        assert normalise_ordinals("i grandi classici disney ii serie") == \
            "i grandi classici disney seconda serie"
        assert normalise_ordinals("i classici di walt disney 2a serie") == \
            "i classici di walt disney seconda serie"
        # Only the token before "serie" is an ordinal; a leading article is not.
        assert normalise_ordinals("i classici disney") == "i classici disney"

    def test_significant_tokens_drop_articles_and_walt(self):
        from models.inducks import significant_tokens, normalize_title

        assert (significant_tokens(normalize_title("Le grandi storie di Walt Disney"))
                == significant_tokens(normalize_title("Le grandi storie Disney")))
        # "disney" is never dropped: it is what separates these from everything
        # else in a library.
        assert "disney" in significant_tokens(normalize_title("Le grandi storie Disney"))


class TestNarrowToIssue:
    """The issue number is evidence the caller already has, and it is what makes
    a loose name safe to try."""

    def test_issue_number_picks_one_of_eleven(self, inducks_configured):
        from models.inducks import search_series, narrow_to_issue

        candidates = search_series("Topolino")
        assert len(candidates) == 2
        # Only it/TL ever had an issue 3200.
        assert [c["id"] for c in narrow_to_issue(candidates, "3200")] == ["it/TL"]

    def test_padded_numbers_match(self, inducks_configured):
        from models.inducks import search_series, narrow_to_issue

        candidates = search_series("Topolino")
        assert [c["id"] for c in narrow_to_issue(candidates, "03200")] == ["it/TL"]

    def test_a_run_continuing_into_a_second_series(self, inducks_configured):
        """Both publications are indexed under the same stripped title, so the
        issue number is the only thing that says which holds the album."""
        from models.inducks import search_series, narrow_to_issue

        candidates = search_series("Super Almanacco Paperino")
        assert {c["id"] for c in candidates} == {"it/SA", "it/SAP"}
        assert [c["id"] for c in narrow_to_issue(candidates, "1")] == ["it/SA"]
        assert [c["id"] for c in narrow_to_issue(candidates, "3")] == ["it/SAP"]

    def test_missing_issue_narrows_to_nothing(self, inducks_configured):
        """Better than answering with the wrong run: the caller declines and the
        next provider gets its turn."""
        from models.inducks import search_series, narrow_to_issue

        candidates = search_series("Super Almanacco Paperino")
        assert narrow_to_issue(candidates, "99") == []

    def test_year_separates_two_runs_holding_the_same_number(self, inducks_configured):
        from models.inducks import search_series, narrow_to_issue

        candidates = search_series("Topolino")
        assert [c["id"] for c in narrow_to_issue(candidates, "1", 1949)] == ["it/TL"]
        assert [c["id"] for c in narrow_to_issue(candidates, "1", 1932)] == ["it/TG"]

    def test_ambiguity_survives_when_nothing_separates_them(self, inducks_configured):
        """Both Topolinos have an issue 1 and no year is offered. Two survivors
        is the honest answer."""
        from models.inducks import search_series, narrow_to_issue

        candidates = search_series("Topolino")
        assert len(narrow_to_issue(candidates, "1")) == 2

    def test_no_issue_number_changes_nothing(self, inducks_configured):
        from models.inducks import search_series, narrow_to_issue

        candidates = search_series("Topolino")
        assert narrow_to_issue(candidates, "") == candidates

    def test_issue_dates(self, inducks_configured):
        from models.inducks import issue_dates

        found = issue_dates("it/TL", ["1", "3200", "0002", "9999"])
        assert found["1"] == "1949-04-07"
        assert found["3200"] == "2017-06-27"
        assert found["2"] is None  # present, but with no usable date
        assert "9999" not in found


class TestIssueNumberPaddingSymmetry:
    """narrow_to_issue and get_issue_metadata used to normalize issue numbers
    differently -- a publication narrow_to_issue accepted on a padded number
    could return None when fetched through get_issue_metadata on the unpadded
    form, or vice versa. Both now key off models.inducks._normalize_issue_number.
    """

    @pytest.mark.parametrize("query", ["1", "01", "001", "0001"])
    def test_get_issue_metadata_accepts_every_padding_of_an_unpadded_row(
        self, inducks_configured, query
    ):
        """it/TL stores issue 1 unpadded ("1"); every width CLU might supply
        must still resolve to it."""
        from models.inducks import get_issue_metadata

        md = get_issue_metadata("it/TL", query)
        assert md is not None
        assert md["Number"] == "1"

    @pytest.mark.parametrize("query", ["3200", "03200"])
    def test_narrow_to_issue_and_get_issue_metadata_agree(self, inducks_configured, query):
        """The exact regression from the PR review: narrow_to_issue accepts
        '03200' against it/TL, but the un-fixed get_issue_metadata('it/TL',
        '03200') returned None -- a real near-miss dead-end."""
        from models.inducks import search_series, narrow_to_issue, get_issue_metadata

        candidates = search_series("Topolino")
        narrowed = narrow_to_issue(candidates, query)
        assert [c["id"] for c in narrowed] == ["it/TL"]
        assert get_issue_metadata(narrowed[0]["id"], query) is not None

    def _add_padded_publication(self, inducks_db_path):
        """The shared fixture only ever stores unpadded issue numbers. Add a
        publication whose issuenumber column is genuinely zero-padded, the
        direction the shared fixture can't otherwise exercise."""
        conn = sqlite3.connect(str(inducks_db_path))
        conn.execute(
            "INSERT INTO inducks_publication (publicationcode, countrycode, "
            "languagecode, title) VALUES (?, ?, ?, ?)",
            ("it/PAD", "it", "it", "Paperinik Padded"),
        )
        conn.execute(
            "INSERT INTO inducks_issue (issuecode, publicationcode, "
            "issuenumber, title, pages, price, oldestdate) VALUES "
            "(?, ?, ?, ?, ?, ?, ?)",
            ("it/PAD    7", "it/PAD", "0007", "", "32", "", "2001-01-01"),
        )
        conn.commit()
        conn.close()

    @pytest.mark.parametrize("query", ["7", "07", "007", "0007"])
    def test_padded_storage_fetched_by_every_query_width(
        self, inducks_configured, inducks_db_path, query
    ):
        from models.inducks import get_issue_metadata

        self._add_padded_publication(inducks_db_path)
        md = get_issue_metadata("it/PAD", query)
        assert md is not None
        assert md["Number"] == "0007"

    def test_padded_storage_narrows_on_an_unpadded_query(self, inducks_configured, inducks_db_path):
        from models.inducks import search_series, narrow_to_issue

        self._add_padded_publication(inducks_db_path)
        candidates = search_series("Paperinik Padded")
        assert [c["id"] for c in narrow_to_issue(candidates, "7")] == ["it/PAD"]


class TestRelaxedTitleMatching:

    def test_ordinal_spelled_the_other_way(self, inducks_configured):
        """A folder says "II Serie" where INDUCKS says "(Seconda Serie)"."""
        from models.inducks import search_series

        assert [r["id"] for r in search_series("I grandi classici Disney II Serie")] == ["it/GCDN"]
        assert [r["id"] for r in search_series("I Grandi Classici Disney 2a serie")] == ["it/GCDN"]

    def test_same_words_one_preposition_apart(self, inducks_configured):
        from models.inducks import search_series

        assert [r["id"] for r in search_series(
            "Le grandi storie di Walt Disney - L'opera omnia di Romano Scarpa")] == ["it/GSD"]

    def test_token_match_needs_the_whole_set(self, inducks_configured):
        """A subset must not match, or it becomes a fuzzy search."""
        from models.inducks import search_series

        assert search_series("Le grandi storie") == []
        assert search_series("Romano Scarpa") == []

    def test_a_spelled_out_qualifier_still_wins(self, inducks_configured):
        """Relaxing the name must not let the bare title answer for a qualified
        one: keys are tried most specific first and the first that hits wins."""
        from models.inducks import search_series

        assert [r["id"] for r in search_series("Topolino (giornale)")] == ["it/TG"]


class TestGetIssueMetadata:

    def test_maps_the_anthology(self, inducks_configured):
        from models.inducks import get_issue_metadata

        md = get_issue_metadata("it/TL", "1")
        assert md["Series"] == "Topolino (libretto)"
        assert md["Number"] == "1"
        assert md["Title"] == "Il primo numero"
        assert md["Year"] == 1949
        assert md["Month"] == 4
        assert md["Day"] == 7
        assert md["PageCount"] == "68"
        assert md["LanguageISO"] == "it"
        assert md["Web"] == "https://inducks.org/issue.php?c=it/TL++++1"

    def test_summary_is_the_table_of_contents(self, inducks_configured):
        from models.inducks import get_issue_metadata

        md = get_issue_metadata("it/TL", "1")
        assert md["Summary"] == (
            "- Topolino e il cobra bianco\n- Paperino e il ladro di uova"
        )

    def test_credits_merge_across_stories_in_order(self, inducks_configured):
        from models.inducks import get_issue_metadata

        md = get_issue_metadata("it/TL", "1")
        assert md["Writer"] == "Luciano Bottaro, Romano Scarpa"
        assert md["Penciller"] == "Luciano Bottaro, Giorgio Cavazzano"
        assert md["Inker"] == "Sandro Zemolin"

    def test_cover_credits_are_excluded(self, inducks_configured):
        """A cover artist is not the penciller of the book."""
        from models.inducks import get_issue_metadata

        md = get_issue_metadata("it/TL", "1")
        assert "Coverist" not in md.get("Penciller", "")

    def test_characters_prefer_the_localised_name(self, inducks_configured):
        from models.inducks import get_issue_metadata

        md = get_issue_metadata("it/TL", "1")
        assert md["Characters"] == "Topolino, Paperino"

    def test_most_recent_publisher_wins(self, inducks_configured):
        """Topolino carries Mondadori then Panini; the modern imprint produced it."""
        from models.inducks import get_issue_metadata

        assert get_issue_metadata("it/TL", "1")["Publisher"] == "Panini Comics"

    def test_notes_names_the_source(self, inducks_configured):
        """Notes doubles as the 'already tagged, skip this file' sentinel."""
        from models.inducks import get_issue_metadata

        assert get_issue_metadata("it/TL", "1")["Notes"].startswith("Metadata from INDUCKS.")

    def test_unknown_date_sentinel_is_not_a_year(self, inducks_configured):
        """9999-12-31 must not reach ComicInfo, or the date check reads it as a
        seven-thousand-year conflict and rejects the match."""
        from models.inducks import get_issue_metadata

        md = get_issue_metadata("it/TL", "3")
        assert "Year" not in md

    def test_issue_not_found(self, inducks_configured):
        from models.inducks import get_issue_metadata

        assert get_issue_metadata("it/TL", "9999") is None
        assert get_issue_metadata("it/TL", "") is None
        assert get_issue_metadata("", "1") is None

    def test_publisher_order_survives_an_index_backed_scan(self, tmp_path):
        """The old query had no ORDER BY, so 'the last row wins' silently
        depended on SQLite happening to walk the table in insertion order.
        The bare-table fixture used everywhere else can never disagree with
        that -- this builds a covering index on (issuecode, publisherid),
        which the planner prefers over a table scan (confirmed via EXPLAIN
        QUERY PLAN) and which visits publisherid alphabetically. Choosing
        publisherid values that sort opposite to insertion order makes the
        unordered scan return the WRONG publisher last; the explicit
        ``ORDER BY j.rowid`` in _publisher must still return the right one.
        """
        from models.inducks import _publisher, _dict_factory

        path = str(tmp_path / "publisher_order.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE inducks_publisher (publisherid TEXT PRIMARY KEY, publishername TEXT);
            CREATE TABLE inducks_publishingjob (publisherid TEXT, issuecode TEXT);
            """
        )
        conn.executemany(
            "INSERT INTO inducks_publisher (publisherid, publishername) VALUES (?, ?)",
            [("zzz_old", "Arnoldo Mondadori Editore"), ("aaa_new", "Panini Comics")],
        )
        # Inserted oldest-first, matching the docstring's "listed oldest
        # first" and giving "aaa_new" (Panini, the modern imprint) the
        # higher rowid -- the correct answer is the last-INSERTED row.
        conn.executemany(
            "INSERT INTO inducks_publishingjob (publisherid, issuecode) VALUES (?, ?)",
            [("zzz_old", "it/TL    1"), ("aaa_new", "it/TL    1")],
        )
        # This index lets an unordered scan visit "aaa_new" before
        # "zzz_old" (alphabetical), the reverse of insertion order.
        conn.execute(
            "CREATE INDEX idx_publishingjob_issuecode "
            "ON inducks_publishingjob (issuecode, publisherid)"
        )
        conn.commit()
        conn.close()

        ro_conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        ro_conn.row_factory = _dict_factory
        try:
            assert _publisher(ro_conn, "it/TL    1") == "Panini Comics"
        finally:
            ro_conn.close()

    def test_incomplete_build_is_rejected_not_silently_degraded(self, tmp_path, monkeypatch):
        """A build missing tables must fail the connection test, loudly.

        INDUCKS ships as one tarball holding the whole database, so a build
        without ``inducks_story`` is broken rather than merely reduced. Reporting
        that is more useful than tagging a whole library with no credits in it.
        """
        import models.inducks as inducks_module
        from models.providers.inducks_provider import InducksProvider

        path = build_inducks_sqlite(tmp_path / "core.db", core_only=True)
        inducks_module.invalidate_inducks_table_cache()
        monkeypatch.setattr("models.inducks._get_saved_credentials",
                            lambda: {"database_path": path})
        monkeypatch.setattr("models.inducks.get_configured_countries", lambda: ["it"])
        try:
            assert InducksProvider().test_connection() is False
        finally:
            inducks_module.invalidate_inducks_table_cache()


class TestComicInfoRoundTrip:
    """Every mapped field must survive the allowlist in core.comicinfo.

    A key a provider maps but that has no ``add()`` line there is computed and
    then silently discarded, which is invisible until someone reads the XML.
    """

    def test_every_field_reaches_the_xml(self, inducks_configured):
        from core.comicinfo import generate_comicinfo_xml
        from models.inducks import get_issue_metadata

        md = get_issue_metadata("it/TL", "1")
        xml = generate_comicinfo_xml(md).decode("utf-8")

        for tag in ("Series", "Number", "Title", "Summary", "Publisher", "Year",
                    "Month", "Day", "PageCount", "LanguageISO", "Writer",
                    "Penciller", "Inker", "Characters", "Web", "Notes"):
            assert f"<{tag}>" in xml, f"{tag} was mapped but never written"


class TestProviderAdapter:

    def test_search_series_returns_search_results(self, inducks_configured, inducks_creds):
        from models.providers.inducks_provider import InducksProvider

        results = InducksProvider(credentials=inducks_creds).search_series("Zio Paperone")
        assert len(results) == 1
        assert isinstance(results[0], SearchResult)
        assert results[0].provider == ProviderType.INDUCKS
        assert results[0].id == "it/ZP"
        assert results[0].year == 1987

    def test_get_series(self, inducks_configured, inducks_creds):
        from models.providers.inducks_provider import InducksProvider

        result = InducksProvider(credentials=inducks_creds).get_series("it/TL")
        assert result.title == "Topolino (libretto)"
        assert result.year == 1949
        assert result.issue_count == 4
        assert InducksProvider(credentials=inducks_creds).get_series("it/NOPE") is None

    def test_get_issues_sorts_numerically(self, inducks_configured, inducks_creds):
        """#2 before #3200, not lexicographically."""
        from models.providers.inducks_provider import InducksProvider

        issues = InducksProvider(credentials=inducks_creds).get_issues("it/TL")
        assert [i.issue_number for i in issues] == ["1", "2", "3", "3200"]

    def test_get_issue_by_code(self, inducks_configured, inducks_creds):
        from models.providers.inducks_provider import InducksProvider

        issue = InducksProvider(credentials=inducks_creds).get_issue("it/TL 3200")
        assert isinstance(issue, IssueResult)
        assert issue.series_id == "it/TL"
        assert issue.issue_number == "3200"
        assert issue.cover_date == "2017-06-27"

    def test_to_comicinfo_uses_full_metadata(self, inducks_configured, inducks_creds):
        from models.providers.inducks_provider import InducksProvider

        issue = IssueResult(provider=ProviderType.INDUCKS, id="it/TL    1",
                            series_id="it/TL", issue_number="1")
        result = InducksProvider(credentials=inducks_creds).to_comicinfo(issue)
        assert result["Series"] == "Topolino (libretto)"
        assert result["Writer"] == "Luciano Bottaro, Romano Scarpa"

    def test_to_comicinfo_falls_back(self, inducks_configured, inducks_creds, monkeypatch):
        from models.providers.inducks_provider import InducksProvider

        monkeypatch.setattr("models.inducks.get_issue_metadata", lambda *a, **k: None)
        issue = IssueResult(provider=ProviderType.INDUCKS, id="it/TL    1",
                            series_id="it/TL", issue_number="1",
                            title="Il primo numero", cover_date="1949-04-07")
        series = SearchResult(provider=ProviderType.INDUCKS, id="it/TL",
                              title="Topolino (libretto)", year=1949)
        result = InducksProvider(credentials=inducks_creds).to_comicinfo(issue, series)
        assert result["Series"] == "Topolino (libretto)"
        assert result["Year"] == 1949

    def test_unconfigured_provider_is_inert(self, monkeypatch):
        """Registration alone must not change behaviour for a library with no
        INDUCKS database configured."""
        from models.providers.inducks_provider import InducksProvider

        monkeypatch.setattr("models.inducks._get_saved_credentials", lambda: None)
        monkeypatch.delenv("INDUCKS_DATABASE_PATH", raising=False)
        p = InducksProvider()
        assert p.test_connection() is False
        assert p.search_series("Topolino") == []
        assert p.get_series("it/TL") is None
        assert p.get_issues("it/TL") == []
        assert p.get_issue_metadata("it/TL", "1") is None


class TestDateCheckIntegration:
    """The issue-level date check from #513, which is what made this provider
    worth building rather than keeping a separate tagger."""

    def test_classified_as_issue_level(self):
        from core.metadata_dates import year_is_issue_level

        assert year_is_issue_level("inducks") is True
        # The batch path passes a display string, matched on lowered substring.
        assert year_is_issue_level("INDUCKS") is True

    def test_a_wrong_series_is_caught(self, inducks_configured):
        from core.metadata_dates import date_conflict
        from models.inducks import get_issue_metadata

        md = get_issue_metadata("it/TL", "3200")
        # A file claiming 1949 matched against a 2017 issue is a wrong series.
        assert date_conflict(1949, md["Year"], tolerance=2) is True
        assert date_conflict(2017, md["Year"], tolerance=2) is False

    def _add_issue_2000(self, inducks_db_path, oldestdate="1993-05-01"):
        """it/TL numbered past #3600 in reality; add a #2000 issue so a
        filename literally named 'Topolino 2000.cbz' is realistic."""
        conn = sqlite3.connect(str(inducks_db_path))
        conn.execute(
            "INSERT INTO inducks_issue (issuecode, publicationcode, "
            "issuenumber, title, pages, price, oldestdate) VALUES "
            "(?, ?, ?, ?, ?, ?, ?)",
            ("it/TL 2000", "it/TL", "2000", "", "164", "", oldestdate),
        )
        conn.commit()
        conn.close()

    def test_bare_filename_year_equal_to_the_issue_number_is_accepted(
        self, inducks_configured, inducks_db_path, monkeypatch
    ):
        """The bug this closes: Topolino issue numbers run past #3600, so
        'Topolino 2000.cbz' reads as both a plausible year (2000) and the
        issue number, 18+ years from the real 1993 publication date --
        past tolerance, so date_check_mode=enforce discarded a correct match
        for every issue numbered 1900-2099 before the exemption existed.
        """
        from core.metadata_dates import evaluate
        from models.inducks import get_issue_metadata

        self._add_issue_2000(inducks_db_path)
        md = get_issue_metadata("it/TL", "2000")
        assert md["Year"] == 1993

        monkeypatch.setattr("core.metadata_dates.date_check_mode", lambda: "enforce")
        monkeypatch.setattr("core.metadata_dates.date_check_tolerance", lambda: 2)
        _mode, conflicted, _year = evaluate(
            "Topolino 2000.cbz", md["Year"], issue_number="2000"
        )
        assert conflicted is False

    def test_filename_year_alongside_the_issue_number_still_short_circuits(
        self, inducks_configured, monkeypatch
    ):
        """Regression guard: 'Topolino 1975 (1993).cbz' already carries two
        distinct plausible years (1975 and 1993) and was already unresolved
        (no check ran) before the exemption -- it must stay that way rather
        than being narrowed down to 1993 by the issue_number exemption.
        """
        from core.metadata_dates import evaluate

        monkeypatch.setattr("core.metadata_dates.date_check_mode", lambda: "enforce")
        monkeypatch.setattr("core.metadata_dates.date_check_tolerance", lambda: 2)
        _mode, conflicted, filename_year = evaluate(
            "Topolino 1975 (1993).cbz", "1993", issue_number="1975"
        )
        assert conflicted is False
        assert filename_year is None

    def test_country_preference_default(self, monkeypatch):
        from models.inducks import get_configured_countries

        monkeypatch.setattr("core.database.get_user_preference",
                            lambda *a, **k: None, raising=False)
        assert get_configured_countries() == ["us"]


class TestCollidingTitlesCache:
    """search_series computes _colliding_stripped_titles once and threads it
    through via the `colliding` parameter -- get_issue_metadata does not, so
    every call re-scanned the whole publication table (7,310 rows on the real
    build, each put through normalize_title's NFKD decomposition) via
    series_name_for. Cached at module scope instead, invalidated alongside
    the existing table cache.
    """

    SCAN_SQL = "SELECT title FROM inducks_publication WHERE title IS NOT NULL AND title <> ''"

    def test_scan_runs_once_across_repeated_get_issue_metadata_calls(
        self, inducks_configured, monkeypatch
    ):
        """sqlite3.Cursor is a C type and can't be monkeypatched directly, so
        this spies via sqlite3's own trace callback on the connections
        models.inducks opens."""
        import models.inducks as inducks_module

        inducks_module._COLLIDING_TITLES_CACHE = None
        calls = {"n": 0}
        real_get_connection = inducks_module.get_connection

        def traced_get_connection():
            conn = real_get_connection()
            if conn is not None:
                def _trace(sql):
                    if sql.strip() == self.SCAN_SQL:
                        calls["n"] += 1
                conn.set_trace_callback(_trace)
            return conn

        monkeypatch.setattr(inducks_module, "get_connection", traced_get_connection)

        inducks_module.get_issue_metadata("it/TL", "1")
        inducks_module.get_issue_metadata("it/TL", "3200")
        inducks_module.get_issue_metadata("it/TG", "1")

        assert calls["n"] == 1

    def test_search_series_and_get_issue_metadata_share_the_cache(self, inducks_configured):
        """search_series already computes this once per call; the two paths
        must not each maintain their own copy."""
        import models.inducks as inducks_module

        inducks_module._COLLIDING_TITLES_CACHE = None
        inducks_module.search_series("Topolino")
        populated = inducks_module._COLLIDING_TITLES_CACHE
        assert populated is not None

        inducks_module.get_issue_metadata("it/TL", "1")
        assert inducks_module._COLLIDING_TITLES_CACHE is populated

    def test_invalidate_table_cache_also_clears_it(self, inducks_configured):
        import models.inducks as inducks_module

        inducks_module.get_issue_metadata("it/TL", "1")
        assert inducks_module._COLLIDING_TITLES_CACHE is not None

        inducks_module.invalidate_inducks_table_cache()
        assert inducks_module._COLLIDING_TITLES_CACHE is None
