"""One definition each for normalisations that used to be pasted.

Stage 3 of the parser/normaliser consolidation. Nothing here is a new rule:
each test pins that the callers of a shared function still produce what their
own copy produced, and that the copies are gone.
"""
import inspect

import pytest

from core.series_names import ascii_words


class TestAsciiWords:
    @pytest.mark.parametrize("value,expected", [
        ("Spider-Man: Blue!", "spider man blue"),
        ("  The   Flash  ", "the flash"),
        ("Batman/Superman", "batman superman"),
        ("It's Jeff", "it s jeff"),
        # Non-ASCII goes with the punctuation. Inducks folds accents itself
        # for exactly this reason and is not on this function.
        ("Élektra", "lektra"),
        ("", ""),
        (None, ""),
    ])
    def test_examples(self, value, expected):
        assert ascii_words(value) == expected

    @pytest.mark.parametrize("name", [
        "Spider-Man: Blue!", "Batman & Robin", "X-Men '92", "  2000 AD  ",
        "Shaolin Cowboy - Staying A.I. Live", "",
    ])
    def test_four_callers_agree(self, name):
        from core.bulk_metadata import _normalize_series
        from core.library_health import _norm_text
        from helpers.collection import _normalize_for_keywords
        from models.gcd import normalize_title

        expected = ascii_words(name)
        assert _norm_text(name) == expected
        assert normalize_title(name) == expected
        assert _normalize_series(name) == expected
        # The keyword form is the same words, space-padded for whole-word tests.
        assert _normalize_for_keywords(name) == " " + expected + " "

    def test_bulk_series_still_drops_a_year_group(self):
        from core.bulk_metadata import _normalize_series
        assert _normalize_series("Avengers Spotlight (1989)") == "avengers spotlight"

    def test_the_expression_has_one_copy(self):
        import core.bulk_metadata
        import core.library_health
        import helpers.collection
        import models.gcd
        for module in (core.bulk_metadata, core.library_health,
                       helpers.collection, models.gcd):
            assert "[^a-z0-9]+" not in inspect.getsource(module), module.__name__


class TestGetcomicsStoredKey:
    """`series_norm_norm` and the alias table are joined on one key."""

    @pytest.mark.parametrize("name", [
        "Spider-Man", "Batman – Year One", "Batman — Year One",
        "  Saga  ", "Batman -",
    ])
    def test_alias_key_is_the_series_key(self, name):
        from models.getcomics import _normalize_alias, _norm_series_key
        assert _normalize_alias(name) == _norm_series_key(name)

    def test_empty_alias_key_stays_a_string(self):
        from models.getcomics import _normalize_alias
        assert _normalize_alias("") == ""

    def test_key_expression_is_not_pasted(self):
        # The em-dash replace is the tail of the key expression. It may appear
        # in _norm_series_key and in the alias-CSV writer in
        # update_series_aliases (stored user input, deliberately untouched).
        import models.getcomics as getcomics
        source = inspect.getsource(getcomics)
        pasted = source.count(".replace('—', ' ')") + source.count(
            ".replace('\\u2014', ' ')"
        )
        assert pasted == 2

    def test_aliases_attach_to_a_series_whose_name_ends_in_a_dash(self, db_connection):
        """"Batman - Vol 3" normalizes to "Batman -". The pasted copies of the
        key skipped .strip(), so they looked for "batman  " while the writers
        stored "batman": the alias UPDATE matched no rows and the read missed
        the row that had them."""
        from core.database import get_db_connection, set_user_preference
        from models.getcomics import (
            _ensure_alias_table, _ensure_urls_table, _norm_series_key,
            get_series_aliases, normalize_series_name, update_series_aliases,
        )

        set_user_preference("getcomics_scrape_aliases_cleared_v1", "1", category="getcomics")
        _ensure_urls_table()
        _ensure_alias_table()

        norm_series, _ = normalize_series_name("Batman - Vol 3")
        assert norm_series == "Batman -"
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO getcomics_urls (url, full_url, series_norm, series_norm_norm, title) "
            "VALUES (?, ?, ?, ?, ?)",
            ("https://getcomics.org/b3", "https://getcomics.org/b3",
             norm_series, _norm_series_key(norm_series), "Batman Vol. 3 #1"),
        )
        conn.commit()
        conn.close()

        assert update_series_aliases("Batman - Vol 3", "Dark Knight") == 1
        assert get_series_aliases("Batman - Vol 3") == "dark knight"


class TestMetadataRouteHelpers:
    @pytest.mark.parametrize("name,expected", [
        ("Batman: Year One", "Batman Year One"),
        ("Spider-Man", "Spider Man"),
        ("Batman – Year One", "Batman Year One"),
        ("It's Jeff!", "It s Jeff"),
        ("What If...?", "What If"),
        ("", ""),
        (None, ""),
    ])
    def test_comicvine_search_term(self, name, expected):
        from routes.metadata import comicvine_search_term
        assert comicvine_search_term(name) == expected

    def test_same_title_is_case_and_whitespace_only(self):
        from routes.metadata import _same_title
        assert _same_title(" Batman ", "batman")
        # Strict on purpose: these pick a series without asking the user.
        assert not _same_title("Batman: Year One", "Batman - Year One")
        assert not _same_title("The Flash", "Flash")
        assert not _same_title(None, "Batman")

    def test_neither_is_pasted_in_the_routes(self):
        import routes.metadata as routes_metadata
        source = inspect.getsource(routes_metadata)
        assert source.count("r'[:\\-") == 1
        assert ".lower().strip() ==" in inspect.getsource(routes_metadata._same_title)
        assert source.count(".lower().strip() ==") == 1
