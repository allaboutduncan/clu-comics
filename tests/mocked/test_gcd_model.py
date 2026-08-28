"""Tests for models/gcd.py -- runs the ported SQL against a real temp SQLite DB."""
import sqlite3
import pytest
from unittest.mock import patch, MagicMock

from models.gcd import EXPECTED_GCD_TABLES, GCD_CORE_TABLES
from tests.mocked.conftest import build_gcd_sqlite


@pytest.fixture
def not_configured(monkeypatch):
    """No saved credentials and no env var -> GCD is unconfigured."""
    monkeypatch.setattr("models.gcd._get_saved_credentials", lambda: None)
    monkeypatch.delenv("GCD_DATABASE_PATH", raising=False)


class TestDatabaseStatus:

    def test_available_when_file_exists(self, gcd_configured):
        from models.gcd import check_database_status, is_database_available
        status = check_database_status()
        assert status["gcd_available"] is True
        assert status["gcd_path_configured"] is True
        assert is_database_available() is True

    def test_not_available_when_unconfigured(self, not_configured):
        from models.gcd import check_database_status, is_database_available
        status = check_database_status()
        assert status["gcd_available"] is False
        assert status["gcd_path_configured"] is False
        assert is_database_available() is False

    def test_not_available_when_path_missing(self, tmp_path, monkeypatch):
        from models.gcd import check_database_status
        missing = tmp_path / "nope.db"
        monkeypatch.setattr("models.gcd._get_saved_credentials",
                            lambda: {"database_path": str(missing)})
        status = check_database_status()
        assert status["gcd_available"] is False
        # Path is configured but the file is absent.
        assert status["gcd_path_configured"] is True

    def test_env_var_fallback(self, gcd_db_path, monkeypatch):
        from models.gcd import get_connection_params
        monkeypatch.setattr("models.gcd._get_saved_credentials", lambda: None)
        monkeypatch.setenv("GCD_DATABASE_PATH", str(gcd_db_path))
        params = get_connection_params()
        assert params == {"database_path": str(gcd_db_path)}


class TestGetConnection:

    def test_opens_read_only(self, gcd_configured):
        from models.gcd import get_connection
        conn = get_connection()
        assert conn is not None
        try:
            # Read-only: writes must fail.
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE should_fail (x INTEGER)")
        finally:
            conn.close()

    def test_regexp_registered(self, gcd_configured):
        from models.gcd import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            # Case-sensitive by design (search_series lowercases both operands).
            cur.execute("SELECT 'Batman' REGEXP ? AS m", ("Bat",))
            assert cur.fetchone()["m"] == 1
            cur.execute("SELECT 'Batman' REGEXP ? AS m", ("^zzz",))
            assert cur.fetchone()["m"] == 0
        finally:
            conn.close()

    def test_none_when_unconfigured(self, not_configured):
        from models.gcd import get_connection
        assert get_connection() is None


class TestSearchSeries:

    def test_finds_series(self, gcd_configured):
        from models.gcd import search_series
        result = search_series("Batman")
        assert result is not None
        assert result["name"] == "Batman"
        assert result["year_began"] == 1940

    def test_finds_series_with_year(self, gcd_configured):
        from models.gcd import search_series
        result = search_series("Batman", year=1945)
        assert result is not None
        assert result["name"] == "Batman"

    def test_no_match(self, gcd_configured):
        from models.gcd import search_series
        assert search_series("NonexistentSeries") is None

    def test_not_configured(self, not_configured):
        from models.gcd import search_series
        assert search_series("Batman") is None

    def test_main_word_fallback_still_matches_a_narrow_token(self, gcd_configured):
        """`Batman` matches one series here, so the fallback stays useful."""
        from models.gcd import search_series
        result = search_series("Batman Adventures Special Edition")
        assert result is not None
        assert result["name"] == "Batman"

    def test_main_word_fallback_declines_an_over_broad_token(self, gcd_configured,
                                                             monkeypatch):
        """Beyond the candidate cap the first row is an arbitrary pick, not a match.

        Simulated by lowering the cap rather than loading thousands of series:
        the real dump has 18,526 series containing 'le'.
        """
        import models.gcd as gcd
        monkeypatch.setattr(gcd, "MAIN_WORD_MAX_CANDIDATES", 0)
        assert gcd.search_series("Batman Adventures Special Edition") is None

    def test_the_cap_does_not_apply_to_the_precise_variations(self, gcd_configured,
                                                              monkeypatch):
        """An exact hit must survive a cap of zero -- only the fallback is guarded."""
        import models.gcd as gcd
        monkeypatch.setattr(gcd, "MAIN_WORD_MAX_CANDIDATES", 0)
        result = gcd.search_series("Batman")
        assert result is not None
        assert result["name"] == "Batman"


class TestConfiguredLanguages:
    """The gcd_metadata_languages preference must reach every lookup path."""

    def test_defaults_to_english(self, monkeypatch):
        from models.gcd import get_configured_languages
        monkeypatch.setattr("core.database.get_user_preference",
                            lambda key, default=None: default)
        assert get_configured_languages() == ["en"]

    def test_parses_and_normalises_the_preference(self, monkeypatch):
        from models.gcd import get_configured_languages
        monkeypatch.setattr("core.database.get_user_preference",
                            lambda key, default=None: " IT , en ")
        assert get_configured_languages() == ["it", "en"]

    def test_empty_preference_falls_back_to_english(self, monkeypatch):
        from models.gcd import get_configured_languages
        monkeypatch.setattr("core.database.get_user_preference",
                            lambda key, default=None: " , ")
        assert get_configured_languages() == ["en"]

    def test_unreadable_preference_falls_back_to_english(self, monkeypatch):
        from models.gcd import get_configured_languages

        def boom(key, default=None):
            raise RuntimeError("no database")

        monkeypatch.setattr("core.database.get_user_preference", boom)
        assert get_configured_languages() == ["en"]


class TestSearchSeriesLanguageFilter:
    """Regression: the automatic path used to hardcode ['en'] (issue #510)."""

    def test_non_english_series_hidden_from_english_only_search(self, gcd_configured):
        from models.gcd import search_series
        assert search_series("Diabolik", language_codes=["en"]) is None

    def test_non_english_series_found_when_configured(self, gcd_configured, monkeypatch):
        from models.gcd import search_series
        monkeypatch.setattr("core.database.get_user_preference",
                            lambda key, default=None: "it,en")
        result = search_series("Diabolik")
        assert result is not None
        assert result["name"] == "Diabolik"
        assert result["publisher_name"] == "Astorina"

    def test_default_honours_the_preference_not_english(self, gcd_configured, monkeypatch):
        """No explicit language_codes: the preference decides, not a hardcoded 'en'."""
        from models.gcd import search_series
        monkeypatch.setattr("core.database.get_user_preference",
                            lambda key, default=None: "en")
        assert search_series("Diabolik") is None

        monkeypatch.setattr("core.database.get_user_preference",
                            lambda key, default=None: "it")
        assert search_series("Diabolik") is not None


class TestGetIssueMetadata:

    def test_returns_metadata(self, gcd_configured):
        from models.gcd import get_issue_metadata
        result = get_issue_metadata(200, "1")
        assert result is not None
        assert result["Series"] == "Batman"
        assert result["Number"] == "1"
        assert result["Publisher"] == "DC Comics"
        assert result["Title"] == "The Beginning"
        assert result["Year"] == 1940
        assert result["Month"] == 4
        # Normalized credit: Bob Kane credited as 'script' -> Writer.
        assert result["Writer"] == "Bob Kane"

    def test_issue_not_found(self, gcd_configured):
        from models.gcd import get_issue_metadata
        assert get_issue_metadata(200, "999") is None

    def test_language_iso_comes_from_the_series(self, gcd_configured):
        """Regression: LanguageISO was hardcoded to 'en' (issue #510)."""
        from models.gcd import get_issue_metadata
        assert get_issue_metadata(200, "1")["LanguageISO"] == "en"
        italian = get_issue_metadata(201, "1")
        assert italian is not None
        assert italian["Series"] == "Diabolik"
        assert italian["LanguageISO"] == "it"

    def test_core_only_db_has_no_credits(self, gcd_core_only_db_path, monkeypatch):
        """A dump missing the credit tables still returns core fields."""
        from models.gcd import get_issue_metadata
        monkeypatch.setattr("models.gcd._get_saved_credentials",
                            lambda: {"database_path": str(gcd_core_only_db_path)})
        result = get_issue_metadata(200, "1")
        assert result is not None
        assert result["Series"] == "Batman"
        assert "Writer" not in result  # None values are dropped

    def test_falls_back_to_legacy_text_columns(self, tmp_path, monkeypatch):
        """When gcd_story_credit is absent, legacy text columns on gcd_story win."""
        from models.gcd import get_issue_metadata
        path = build_gcd_sqlite(tmp_path / "legacy.db", core_only=True)
        conn = sqlite3.connect(path)
        conn.execute("UPDATE gcd_story SET script = 'Bill Finger', pencils = 'Bob Kane' WHERE id = 900")
        conn.commit()
        conn.close()
        monkeypatch.setattr("models.gcd._get_saved_credentials",
                            lambda: {"database_path": path})
        result = get_issue_metadata(200, "1")
        assert result is not None
        assert result["Writer"] == "Bill Finger"
        assert result["Penciller"] == "Bob Kane"

    def test_not_configured(self, not_configured):
        from models.gcd import get_issue_metadata
        assert get_issue_metadata(200, "1") is None


class TestGetAvailableGcdTables:
    """The cached helper that detects which expected GCD tables exist."""

    def test_full_db_reports_all_tables(self, gcd_configured):
        from models.gcd import get_available_gcd_tables
        assert get_available_gcd_tables() == set(EXPECTED_GCD_TABLES)

    def test_core_only_db_reports_core(self, gcd_core_only_db_path, monkeypatch):
        from models.gcd import get_available_gcd_tables
        monkeypatch.setattr("models.gcd._get_saved_credentials",
                            lambda: {"database_path": str(gcd_core_only_db_path)})
        present = get_available_gcd_tables()
        assert GCD_CORE_TABLES.issubset(present)
        assert 'gcd_story_credit' not in present

    def _mock_conn(self, table_names):
        cursor = MagicMock()
        cursor.fetchall.return_value = [{'name': t} for t in table_names]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn, cursor

    def test_caches_after_first_call(self):
        from models.gcd import get_available_gcd_tables, invalidate_gcd_table_cache
        invalidate_gcd_table_cache()
        conn, cursor = self._mock_conn(['gcd_series', 'gcd_issue'])
        with patch("models.gcd.get_connection", return_value=conn):
            first = get_available_gcd_tables()
            second = get_available_gcd_tables()
        assert first == second == {'gcd_series', 'gcd_issue'}
        assert cursor.execute.call_count == 1

    def test_force_refresh_requeries(self):
        from models.gcd import get_available_gcd_tables, invalidate_gcd_table_cache
        invalidate_gcd_table_cache()
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [{'name': 'gcd_series'}],
            [{'name': 'gcd_series'}, {'name': 'gcd_issue'}],
        ]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        with patch("models.gcd.get_connection", return_value=conn):
            first = get_available_gcd_tables()
            second = get_available_gcd_tables(force_refresh=True)
        assert first == {'gcd_series'}
        assert second == {'gcd_series', 'gcd_issue'}
        assert cursor.execute.call_count == 2

    def test_returns_empty_set_on_error(self):
        from models.gcd import get_available_gcd_tables, invalidate_gcd_table_cache
        invalidate_gcd_table_cache()
        conn = MagicMock()
        conn.cursor.side_effect = Exception("boom")
        with patch("models.gcd.get_connection", return_value=conn):
            assert get_available_gcd_tables() == set()

    def test_returns_empty_set_when_no_connection(self):
        from models.gcd import get_available_gcd_tables, invalidate_gcd_table_cache
        invalidate_gcd_table_cache()
        with patch("models.gcd.get_connection", return_value=None):
            assert get_available_gcd_tables() == set()

    def test_warns_once_when_tables_missing(self, caplog):
        import logging
        from models.gcd import get_available_gcd_tables, invalidate_gcd_table_cache
        invalidate_gcd_table_cache()
        present = sorted(set(EXPECTED_GCD_TABLES) - {'gcd_creator', 'gcd_issue_credit'})
        conn, cursor = self._mock_conn(present)
        with caplog.at_level(logging.WARNING, logger="app_logger"):
            with patch("models.gcd.get_connection", return_value=conn):
                get_available_gcd_tables()
                get_available_gcd_tables()
                get_available_gcd_tables()
        warnings = [r for r in caplog.records if "missing from dump" in r.getMessage()]
        assert len(warnings) == 1


class TestGetDatabaseStats:

    def test_counts(self, gcd_configured):
        from models.gcd import get_database_stats
        stats = get_database_stats()
        assert stats is not None
        # Batman (en) + Diabolik (it); Diabolik adds a fifth issue.
        assert stats["series"] == 2
        assert stats["issues"] == 5
        assert stats["stories"] == 1
        assert stats["publishers"] == 2
        assert stats["creators"] == 1
        assert stats["core_ok"] is True
        assert stats["missing_tables"] == []

    def test_none_when_unconfigured(self, not_configured):
        from models.gcd import get_database_stats
        assert get_database_stats() is None


class TestValidateIssue:

    def test_valid_issue(self, gcd_configured):
        from models.gcd import validate_issue
        result = validate_issue(200, "1")
        assert result["success"] is True
        assert result["valid"] is True

    def test_invalid_issue(self, gcd_configured):
        from models.gcd import validate_issue
        result = validate_issue(200, "999")
        assert result["success"] is True
        assert result["valid"] is False

    def test_missing_args(self):
        from models.gcd import validate_issue
        result = validate_issue(None, None)
        assert result["success"] is False


class TestMainWordYearConstraint:
    """A year in the filename must actually filter the main-word fallback."""

    def test_year_outside_the_series_run_is_rejected(self, gcd_configured):
        from models.gcd import search_series
        # Batman began in 1940; a 1930 file predates it, so the fallback that
        # would otherwise return it must find nothing.
        assert search_series("Batman Adventures Special Edition", year=1930) is None

    def test_year_inside_the_series_run_still_matches(self, gcd_configured):
        from models.gcd import search_series
        result = search_series("Batman Adventures Special Edition", year=1975)
        assert result is not None
        assert result["name"] == "Batman"

    def test_no_year_leaves_the_fallback_unconstrained(self, gcd_configured):
        from models.gcd import search_series
        result = search_series("Batman Adventures Special Edition")
        assert result is not None
        assert result["name"] == "Batman"


class TestMainWordProbeIgnoresTheYear:
    """The breadth probe must measure the token, not the year-filtered slice.

    Regression guard: when the probe was run against the year-constrained query,
    a year clause could shrink an over-broad token under the cap, and the
    fallback went back to returning an arbitrary series from whatever happened
    to be running that year. In the real dump '%diabolik%' matches 15 series but
    only 4 running in 2014.
    """

    @pytest.fixture
    def many_zorros(self, tmp_path, monkeypatch):
        path = build_gcd_sqlite(tmp_path / "zorro.db")
        conn = sqlite3.connect(path)
        conn.executemany(
            "INSERT INTO gcd_series (id, name, year_began, year_ended, "
            "publisher_id, language_id) VALUES (?, ?, ?, ?, ?, ?)",
            [(300, "Zorro", 1950, None, 10, 1),
             (301, "Zorro Rides Again", 1990, 1995, 10, 1),
             (302, "Zorro Returns", 2020, None, 10, 1)],
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr("models.gcd._get_saved_credentials",
                            lambda: {"database_path": str(path)})
        return path

    def test_year_filter_cannot_rescue_an_over_broad_token(self, many_zorros,
                                                           monkeypatch):
        import models.gcd as gcd
        # Three series contain 'zorro'; only one of them was running in 1955.
        monkeypatch.setattr(gcd, "MAIN_WORD_MAX_CANDIDATES", 2)
        assert gcd.search_series("Zorro Special Annual Edition", year=1955) is None

    def test_a_token_within_the_cap_is_still_year_constrained(self, many_zorros,
                                                              monkeypatch):
        import models.gcd as gcd
        monkeypatch.setattr(gcd, "MAIN_WORD_MAX_CANDIDATES", 5)
        result = gcd.search_series("Zorro Special Annual Edition", year=1955)
        assert result is not None
        # 'Zorro Returns' (2020) is the newest, but it was not running in 1955.
        assert result["name"] == "Zorro"

    def test_exactly_the_cap_is_within_it(self, many_zorros, monkeypatch):
        """Three series contain 'zorro', so a cap of three must still allow it.

        Pins the boundary: with only the over/under tests, flipping the
        comparison to `>=` passes.
        """
        import models.gcd as gcd
        monkeypatch.setattr(gcd, "MAIN_WORD_MAX_CANDIDATES", 3)
        result = gcd.search_series("Zorro Special Annual Edition", year=1955)
        assert result is not None
        assert result["name"] == "Zorro"

    def test_one_over_the_cap_is_declined(self, many_zorros, monkeypatch):
        """The other side of the same boundary."""
        import models.gcd as gcd
        monkeypatch.setattr(gcd, "MAIN_WORD_MAX_CANDIDATES", 2)
        assert gcd.search_series("Zorro Special Annual Edition", year=1955) is None


class TestMainWordTokenTooBroad:
    """The probe helper is shared with routes/metadata.py, so test it directly."""

    def _cursor(self, tmp_path):
        path = build_gcd_sqlite(tmp_path / "probe.db")
        conn = sqlite3.connect(path)
        conn.row_factory = lambda c, r: {d[0]: r[i] for i, d in enumerate(c.description)}
        return conn.cursor()

    def test_a_narrow_token_is_not_too_broad(self, tmp_path):
        from models.gcd import main_word_token_too_broad
        assert main_word_token_too_broad(self._cursor(tmp_path), "%batman%", ["en"]) is False

    def test_the_language_filter_applies_to_the_probe(self, tmp_path, monkeypatch):
        """Diabolik is Italian, so an English-only probe must not count it."""
        import models.gcd as gcd
        cursor = self._cursor(tmp_path)
        monkeypatch.setattr(gcd, "MAIN_WORD_MAX_CANDIDATES", 0)
        assert gcd.main_word_token_too_broad(cursor, "%diabolik%", ["en"]) is False
        assert gcd.main_word_token_too_broad(cursor, "%diabolik%", ["it"]) is True

    def test_no_configured_languages_matches_nothing(self, tmp_path):
        """Empty codes must produce "IN (NULL)", not a SQL syntax error."""
        from models.gcd import main_word_token_too_broad
        assert main_word_token_too_broad(self._cursor(tmp_path), "%batman%", []) is False
