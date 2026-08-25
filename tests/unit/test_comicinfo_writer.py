"""Tests for the single ComicInfo.xml serializer, core.comicinfo.

`generate_comicinfo_xml` used to be two near-duplicates -- one in
routes/metadata.py and one in models/comicvine.py -- that had drifted apart, so
a field added to one was silently discarded on the paths using the other. These
tests are the union of both old suites, plus one per behaviour the merge had to
reconcile.
"""
import xml.etree.ElementTree as ET

import pytest

from core.comicinfo import _as_text, generate_comicinfo_xml


def gen(data):
    return ET.fromstring(generate_comicinfo_xml(data))


class TestAsText:
    """Providers hand credits through as either a joined string or a list."""

    def test_none(self):
        assert _as_text(None) is None

    def test_string(self):
        assert _as_text("hello") == "hello"

    def test_int(self):
        assert _as_text(42) == "42"

    def test_list_joined(self):
        assert _as_text(["a", "b", "c"]) == "a, b, c"

    def test_list_with_none_members(self):
        assert _as_text(["a", None, "c"]) == "a, c"

    def test_tuple_joined(self):
        assert _as_text(("a", "b")) == "a, b"

    def test_empty_list(self):
        assert _as_text([]) == ""


class TestBasics:

    def test_generate_basic(self):
        issue_data = {
            "Title": "The Origin",
            "Series": "Batman",
            "Number": "1",
            "Volume": "2020",
            "Summary": "The Dark Knight rises",
            "Year": "2020",
            "Month": "3",
            "Writer": "Tom King",
            "Penciller": "David Finch",
            "Publisher": "DC Comics",
        }
        xml_bytes = generate_comicinfo_xml(issue_data)
        assert isinstance(xml_bytes, bytes)

        root = ET.fromstring(xml_bytes)
        assert root.tag == "ComicInfo"
        assert root.attrib == {}, "ComicRack chokes on xmlns/xsi attributes"
        assert root.find("Series").text == "Batman"
        assert root.find("Writer").text == "Tom King"
        assert root.find("Publisher").text == "DC Comics"

    def test_generate_empty_data(self):
        root = gen({})
        # Only the two unconditional defaults
        assert {c.tag for c in root} == {"LanguageISO", "Manga"}

    def test_omits_none_values(self):
        xml_bytes = generate_comicinfo_xml(
            {"Series": "Test", "Writer": None, "Publisher": None}
        )
        assert b"<Writer>" not in xml_bytes
        assert b"<Publisher>" not in xml_bytes


class TestIssueNumbers:

    def test_decimal_preserved(self):
        """Decimal issue numbers like 12.1 should not be truncated to 12."""
        assert gen({"Series": "Avengers", "Number": "12.1"}).find("Number").text == "12.1"

    def test_decimal_preserves_leading_zeros(self):
        """012.1 should stay '012.1', not be stripped to '12.1' via float()."""
        assert gen({"Series": "Avengers", "Number": "012.1"}).find("Number").text == "012.1"

    def test_whole_number_as_float_drops_decimal(self):
        """12.0 should be stored as '12', not '12.0'."""
        assert gen({"Series": "Batman", "Number": "12.0"}).find("Number").text == "12"

    def test_whole_number_drops_leading_zeros(self):
        assert gen({"Series": "Batman", "Number": "003"}).find("Number").text == "3"

    def test_non_numeric_preserved(self):
        """Non-numeric issue numbers like '12.HU' pass through unchanged."""
        assert gen({"Series": "Batman", "Number": "12.HU"}).find("Number").text == "12.HU"

    def test_exotic_digit_does_not_raise(self):
        """str.isdigit() is True for characters int() rejects, e.g. superscript 2."""
        sup2 = chr(0xB2)
        assert gen({"Series": "Batman", "Number": sup2}).find("Number").text == sup2


class TestCredits:

    def test_list_credits_joined(self):
        """The models/comicvine copy used str(value), writing a Python repr."""
        root = gen({"Series": "X-Men",
                    "Writer": ["Chris Claremont", "Fabian Nicieza"]})
        assert root.find("Writer").text == "Chris Claremont, Fabian Nicieza"

    def test_empty_list_credit_omitted(self):
        assert gen({"Series": "X", "Writer": []}).find("Writer") is None

    def test_writes_editor_translator_and_agerating(self):
        """Mapped by the ComicVine and GCD providers but long dropped at
        serialization because neither writer had an add() line."""
        root = gen({
            "Series": "Swords of Texas", "Number": "4",
            "Editor": "Tim Truman", "Translator": "A. Translator",
            "AgeRating": "Teen",
        })
        assert root.find("Editor").text == "Tim Truman"
        assert root.find("Translator").text == "A. Translator"
        assert root.find("AgeRating").text == "Teen"

    def test_whitespace_only_value_omitted(self):
        assert gen({"Series": "X", "Writer": "   "}).find("Writer") is None


class TestDates:

    def test_writes_year_month_day(self):
        root = gen({"Series": "X", "Year": "1988", "Month": "3", "Day": "1"})
        assert root.find("Year").text == "1988"
        assert root.find("Month").text == "3"
        assert root.find("Day").text == "1"

    @pytest.mark.parametrize("tag,value", [
        ("Day", "45"), ("Day", "0"), ("Month", "13"), ("Month", "0"),
    ])
    def test_out_of_range_dropped(self, tag, value):
        assert gen({"Series": "X", tag: value}).find(tag) is None

    def test_year_zero_omitted(self):
        assert gen({"Series": "X", "Year": 0}).find("Year") is None


class TestNumericRobustness:
    """The routes copy used bare int() here, so a provider returning a Volume of
    "v2" raised ValueError and 500'd the route. Five call sites had no
    try/except of their own."""

    @pytest.mark.parametrize("field,value", [
        ("Volume", "v2"), ("Count", "12.5"), ("Count", "abc"),
        ("Year", "n.d."), ("Month", "Spring"), ("Day", "??"),
        ("PageCount", "n/a"), ("Number", "1"),
    ])
    def test_junk_never_raises(self, field, value):
        assert gen({"Series": "X", field: value}).tag == "ComicInfo"

    def test_unparseable_volume_passes_through(self):
        assert gen({"Series": "X", "Volume": "v2"}).find("Volume").text == "v2"

    def test_volume_coerced_to_int(self):
        assert gen({"Series": "X", "Volume": 2016.0}).find("Volume").text == "2016"

    def test_count_coerced_to_int(self):
        """The models/comicvine copy did no coercion, so 12.0 wrote '12.0'."""
        assert gen({"Series": "X", "Count": 12.0}).find("Count").text == "12"

    def test_page_count_accepts_float_string(self):
        """The models/comicvine copy used int("24.0"), which raised and dropped
        the tag."""
        assert gen({"Series": "X", "PageCount": "24.0"}).find("PageCount").text == "24"

    def test_page_count_zero_omitted(self):
        assert gen({"Series": "X", "PageCount": 0}).find("PageCount") is None


class TestDefaults:

    def test_language_and_manga_defaults(self):
        root = gen({"Series": "X"})
        assert root.find("LanguageISO").text == "en"
        assert root.find("Manga").text == "No"

    def test_explicit_values_win(self):
        root = gen({"Series": "X", "Manga": "YesAndRightToLeft", "LanguageISO": "ja"})
        assert root.find("Manga").text == "YesAndRightToLeft"
        assert root.find("LanguageISO").text == "ja"

    def test_no_notes_fallback(self):
        """The routes copy invented a GCD-worded Notes for any dict without one.
        That mislabels every other provider, and Notes doubles as the
        already-tagged sentinel several callers read -- a file with a fabricated
        Notes could never be re-tagged. The two GCD-SQLite builders now set
        their own."""
        xml_bytes = generate_comicinfo_xml({"Series": "X", "id": 44192})
        assert b"<Notes>" not in xml_bytes
        assert b"Grand Comic" not in xml_bytes

    def test_notes_written_when_supplied(self):
        assert gen({"Series": "X", "Notes": "From ComicVine"}).find("Notes").text == "From ComicVine"


class TestSingleImplementation:
    """Both former homes re-export the one writer, so existing imports and
    mock.patch targets keep working."""

    def test_routes_reexport_is_the_same_object(self):
        import core.comicinfo
        import routes.metadata
        assert routes.metadata.generate_comicinfo_xml is core.comicinfo.generate_comicinfo_xml
        assert routes.metadata._as_text is core.comicinfo._as_text

    def test_comicvine_reexport_is_the_same_object(self):
        import core.comicinfo
        import models.comicvine
        assert models.comicvine.generate_comicinfo_xml is core.comicinfo.generate_comicinfo_xml
