"""core.series_names: the one table of filing-name equivalences (#780).

The filename tier and the ComicInfo tier of the filing matcher used to carry a
character table each and disagreed about a leading "The". These tests pin the
table itself and that helpers.collection holds no second copy of it.
"""
import re

import pytest

from core import series_names
from core.filename_chars import FILENAME_ILLEGAL_CHARS
import helpers.collection as collection


class TestNeverRequired:

    def test_every_sanitized_character_is_noise_in_a_series_name(self):
        # CLU removes these from every name it writes, so a name that still
        # demands one can never be matched by a file CLU renamed.
        for ch in FILENAME_ILLEGAL_CHARS:
            assert series_names.SERIES_SEP_RE.fullmatch(ch), repr(ch)

    def test_bang_is_covered(self):
        assert "!" in series_names.NEVER_REQUIRED

    def test_brackets_are_not_flattened(self):
        # _BENIGN_SERIES_EXTRA reads them to recognise "(2025)".
        assert series_names.norm_series_text("Batman (2025)") == "batman (2025)"


class TestNormSeriesText:

    @pytest.mark.parametrize("a, b", [
        ("Venom War: It's Jeff!", "Venom War - Its Jeff"),
        ("World’s Finest", "Worlds Finest"),
        ("What If...?", "What If"),
        ("Batman - The Dark Knight", "Batman: The Dark Knight"),
    ])
    def test_spellings_compare_equal(self, a, b):
        assert series_names.norm_series_text(a) == series_names.norm_series_text(b)

    def test_different_words_stay_different(self):
        assert series_names.norm_series_text("Batman") != \
            series_names.norm_series_text("Batman Beyond")

    def test_none_is_empty(self):
        assert series_names.norm_series_text(None) == ""


class TestSeriesNameRegex:

    def _match(self, series, text):
        return re.match(series_names.series_name_regex(series), text, re.IGNORECASE)

    def test_the_is_optional_in_both_directions(self):
        assert self._match("Totally Awesome Hulk", "The Totally Awesome Hulk")
        assert self._match("The Ultimates", "Ultimates")
        assert self._match("The Ultimates", "The Ultimates")

    def test_optional_words_may_be_missing(self):
        assert self._match("Magik and Colossus", "Magik Colossus")

    def test_ampersand_may_be_missing_or_present(self):
        assert self._match("Black & White", "Black White")
        assert self._match("Black & White", "Black & White")

    def test_punctuation_only_name_stays_literal(self):
        assert series_names.series_name_regex("!!!") == re.escape("!!!")


# Every character a series NAME is split on, as it could appear in a filename.
_SEPARATOR_CHARS = [
    ch
    for ch in (
        [chr(c) for c in range(32, 127)]
        + [chr(c) for c in range(0x2010, 0x2016)]
        + ["−"]
    )
    if series_names._SEPARATOR_RE.fullmatch(ch)
]


class TestSeparatorsInFilenames:
    """A separator in the name must be absorbable in the filename (#793).

    The word gap was a hand-written class beside the separator table and had
    no "," and no ";", so a download carrying CLU's own spelling of
    "Patsy Walker, A.K.A. Hellcat!" was never filed: it stayed in TARGET, the
    issue stayed wanted, and it was downloaded again.
    """

    PATTERN = "{series_name} {issue_number}"

    def _files(self, series, filename):
        pattern = collection.generate_filename_pattern(
            self.PATTERN, series, "1", strict_gap=True
        )
        return bool(pattern.match(filename))

    def test_the_table_is_not_empty(self):
        # The ratchet below is vacuous if the comprehension finds nothing.
        for ch in (",", ";", ":", ".", "-", "_", "+", " "):
            assert ch in _SEPARATOR_CHARS, repr(ch)

    @pytest.mark.parametrize("ch", _SEPARATOR_CHARS, ids=lambda c: f"U+{ord(c):04X}")
    def test_every_separator_may_sit_between_two_words(self, ch):
        name = f"Alpha{ch} Beta"
        assert self._files(name, f"{name} 001.cbz"), repr(ch)
        # ...and the same file is the same series spelled without it.
        assert self._files("Alpha Beta", f"{name} 001.cbz"), repr(ch)

    @pytest.mark.parametrize("series, filename", [
        ("Patsy Walker, A.K.A. Hellcat!", "Patsy Walker, A.K.A. Hellcat! 001 (2016).cbz"),
        ("Patsy Walker, A.K.A. Hellcat!", "Patsy Walker, A.k.a. Hellcat! 001 (2016).cbz"),
        ("Patsy Walker, A.K.A. Hellcat!", "Patsy Walker A.K.A. Hellcat 001 (2016).cbz"),
        ("Infinity, Inc.", "Infinity, Inc. 001 (1984).cbz"),
        ("Infinity Inc", "Infinity, Inc. 001 (1984).cbz"),
        ("Foo; Bar", "Foo; Bar 001.cbz"),
    ])
    def test_a_comma_or_semicolon_name_files(self, series, filename):
        assert self._files(series, filename)

    @pytest.mark.parametrize("series, filename", [
        # A longer-named series sharing the prefix is still a different series.
        ("Infinity, Inc.", "Infinity, Inc. Special 001 (1984).cbz"),
        ("Batman", "Batman, Inc. 001.cbz"),
    ])
    def test_the_spin_off_guard_still_holds(self, series, filename):
        assert not self._files(series, filename)


class TestOneTable:
    """helpers.collection re-exports; it must not grow a body of its own."""

    @pytest.mark.parametrize("alias, source", [
        ("_word_regex", "word_regex"),
        ("_norm_series_text", "norm_series_text"),
        ("_LEADING_ARTICLE", "LEADING_ARTICLE"),
        ("_SERIES_SEP_RE", "SERIES_SEP_RE"),
        ("_APOS_CLASS", "APOS_CLASS"),
    ])
    def test_collection_alias_is_the_shared_object(self, alias, source):
        assert getattr(collection, alias) is getattr(series_names, source)

    def test_comicinfo_tier_uses_the_shared_comparison(self):
        assert collection.series_names_compatible(
            "Venom War - Its Jeff", "Venom War: It's Jeff!"
        )
        assert not collection.series_names_compatible("Batman", "Batman Beyond")
