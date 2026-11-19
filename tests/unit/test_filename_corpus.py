"""One table of filenames, read by every filename parser that has been moved
onto core/filename_parse.py.

The parsers used to carry a regex ladder each and disagreed about the same
file: #778 (extract_issue_number read a title year, and dropped the minus of
issue -1) and #790 (get_series_name_from_files kept "001 - Title" in the
series name) are one mistake -- strip "(YYYY)", then guess by position.

A parser folded onto the shared primitive gets a column here, so a filename
added for one bug is checked against all of them. ``None`` in a column means
"not asserted for this parser", never "expected to return None".
"""
import pytest

from core.filename_parse import (
    SIGN_CHARS,
    ends_with_issue_number,
    prefix_before_first_group,
)
from helpers.collection import get_series_name_from_files
from models.providers.base import extract_issue_number

# (filename, series from get_series_name_from_files, issue from extract_issue_number)
CORPUS = [
    # --- #790: an issue title after the year ---------------------------------
    ("Avengers Assemble Featuring Captain Citrus 001 (2014) - Choose Wisely.cbr",
     "Avengers Assemble Featuring Captain Citrus", "1"),
    ("Batman 497 (1993) - Knightfall, Part 11.cbz", "Batman", "497"),
    ("Batman 497 (1993) Knightfall Part 11.cbz", "Batman", "497"),
    ("100 Bullets 001 (1999) - First Shot.cbz", "100 Bullets", "1"),
    # --- #778: a year in the title, and collected-edition naming -------------
    ("Detective Comics 156 (1937) - Batmobile of 1950.cbz", "Detective Comics", "156"),
    ("Absolute Flash V01 - Of Two Worlds 2025.cbz", None, "1"),
    ("Absolute Superman V02 - Son of the Demon 2026.cbz", None, "2"),
    # --- #778: the sign is part of the number --------------------------------
    ("Spider-Man -1 (1990).cbz", "Spider-Man", "-1"),
    ("What If... -001 (1997).cbz", "What If...", "-1"),
    ("X-Force (1991) Issue -1.cbz", None, "-1"),
    ("Amazing Spider-Man -001 (1997) - Flashback.cbz", "Amazing Spider-Man", "-1"),
    ("Amazing Spider-Man #-1.cbz", "Amazing Spider-Man", "-1"),
    # A dash with a space after it is a separator, never a sign.
    ("Black Cat - 001 (October, 2025).cbz", "Black Cat", "1"),
    # --- a number inside the series name must survive ------------------------
    # The regex suggested in #790 cut each of these at the first number.
    ("Spider-Man 2099 - Dark Genesis 001 (2023).cbz", "Spider-Man 2099 - Dark Genesis", "1"),
    ("Spider-Man 2099 001 (1992).cbz", "Spider-Man 2099", "1"),
    ("Batman 66 - The Lost Episode 001 (2014).cbz", "Batman 66 - The Lost Episode", "1"),
    ("X-Men 92 - House of XCII 001 (2022).cbz", "X-Men 92 - House of XCII", "1"),
    ("Area 51 - The Helix 002.cbz", "Area 51 - The Helix", "2"),
    ("Batman - Year 100 - 001 (2006).cbz", "Batman - Year 100", "1"),
    ("Fantastic Four 2099 001.cbz", "Fantastic Four 2099", "1"),
    # The last number before the group wins, whatever its width.
    ("Area 51 05 (2000).cbz", "Area 51", "5"),
    # --- point issues, counts, suffixes --------------------------------------
    ("Sandman 001.5 (2020) - Title.cbz", "Sandman", "1.5"),
    ("Is Ted Ok 001 of 5.cbz", "Is Ted Ok", "1"),
    ("Is Ted Ok 01 of 12 (2026).cbz", None, "1"),
    ("Gen 13 013B (1995).cbz", None, "13B"),
    ("Amazing Spider-Man 078.BEY (2022).cbz", None, "78.BEY"),
    ("X-Men v2 012.cbz", None, "12"),
    # --- behaviour that must not move ----------------------------------------
    ("Hidden Springs 001 (2026).cbz", "Hidden Springs", "1"),
    ("Crisis of Infinite Earths 001.cbz", "Crisis of Infinite Earths", "1"),
    ("Sicko #01 [July 2026].cbz", "Sicko", "1"),
    ("Shaolin Cowboy - Staying A.i. Live #002 [ 2026].cbr",
     "Shaolin Cowboy - Staying A.i. Live", "2"),
    ("Nightwing #117 - Absolute Power.cbz", "Nightwing", "117"),
    ("Batman 42 (2020).cbz", "Batman", "42"),
    ("Amazing Spider-Man (2018) Issue 080.BEY.cbz", None, "80.BEY"),
    ("Batman 100th Anniversary Special #5.cbz", None, "5"),
    ("Comic-001.cbz", None, "1"),
]


@pytest.mark.parametrize(
    "filename,series", [(f, s) for f, s, _ in CORPUS if s is not None]
)
def test_series_name_from_first_file(tmp_path, filename, series):
    (tmp_path / filename).write_bytes(b"PK\x03\x04")
    assert get_series_name_from_files(str(tmp_path), "DB Name") == series


@pytest.mark.parametrize(
    "filename,issue", [(f, i) for f, _, i in CORPUS if i is not None]
)
def test_issue_number(filename, issue):
    assert extract_issue_number(filename) == issue


class TestPrimitive:
    @pytest.mark.parametrize("stem,expected", [
        ("Batman 497 (1993) - Knightfall", ("Batman 497", True)),
        ("Sicko #01 [July 2026]", ("Sicko #01", True)),
        ("Batman 497 - Knightfall", ("Batman 497 - Knightfall", False)),
        ("(2016) Batman 001", ("", True)),
        ("X-Force (1991) Issue -1", ("X-Force", True)),
        # An unclosed bracket is not a group.
        ("Batman 001 (2016", ("Batman 001 (2016", False)),
    ])
    def test_prefix_before_first_group(self, stem, expected):
        assert prefix_before_first_group(stem) == expected

    @pytest.mark.parametrize("text,expected", [
        ("Batman 497", True),
        ("Sicko #01", True),
        ("Spider-Man -001", True),
        ("Sandman 001.5", True),
        ("Is Ted Ok 001 of 5", True),
        ("Black Cat - 001", True),
        ("X-Force", False),
        ("Batman - Year One", False),
        # A dash glued to a word is part of the word.
        ("Batman-001", False),
    ])
    def test_ends_with_issue_number(self, text, expected):
        assert ends_with_issue_number(text) is expected

    def test_sign_table_is_shared_with_the_filing_matcher(self):
        from helpers import collection
        assert collection._SIGN_CHARS is SIGN_CHARS
        assert SIGN_CHARS[0] == "-"


# --- the metadata-lookup path -------------------------------------------------
# routes.metadata.series_issue_year_from_filename is what a provider is asked
# for when there is no cvinfo. Single-file ComicVine search, batch and bulk all
# read the name through the same two functions; the ComicVine route and the GCD
# directory search used to carry a pattern list each.
LOOKUP = [
    ("Batman 001 (2016).cbz", ("Batman", "1", 2016)),
    ("Avengers Assemble Featuring Captain Citrus 001 (2014) - Choose Wisely.cbr",
     ("Avengers Assemble Featuring Captain Citrus", "1", 2014)),
    ("Batman 497 (1993) - Knightfall, Part 11.cbz", ("Batman", "497", 1993)),
    ("Detective Comics 156 (1937) - Batmobile of 1950.cbz", ("Detective Comics", "156", 1937)),
    # The old route read these as issue 1 of a series called "Spider-Man -1".
    ("Spider-Man -1 (1990).cbz", ("Spider-Man", "-1", 1990)),
    ("Amazing Spider-Man -001 (1997) - Flashback.cbz", ("Amazing Spider-Man", "-1", 1997)),
    ("X-Force (1991) Issue -1.cbz", ("X-Force", "-1", 1991)),
    ("Amazing Spider-Man (2018) Issue 080.BEY.cbz", ("Amazing Spider-Man", "80.BEY", 2018)),
    # Only ONE trailing number is the issue.
    ("Spider-Man 2099 001 (1992).cbz", ("Spider-Man 2099", "1", 1992)),
    ("Batman 2016 001.cbz", ("Batman 2016", "1", None)),
    ("Area 51 05 (2000).cbz", ("Area 51", "5", 2000)),
    ("Spider-Man 2099 - Dark Genesis 001 (2023).cbz",
     ("Spider-Man 2099 - Dark Genesis", "1", 2023)),
    # Counts, point issues, explicit markers.
    ("Is Ted Ok 001 of 5.cbz", ("Is Ted Ok", "1", None)),
    ("Series 05 (of 12) (2020).cbz", ("Series", "5", 2020)),
    ("Batman 700.1 (2011).cbz", ("Batman", "700.1", 2011)),
    ("Nightwing #117 - Absolute Power.cbz", ("Nightwing", "117", None)),
    ("Sicko #01 [July 2026].cbz", ("Sicko", "1", None)),
    ("Batman Issue 5.cbz", ("Batman", "5", None)),
    # Separators left by "Series - 003" naming are dropped.
    ("Black Cat - 001 (October, 2025).cbz", ("Black Cat", "1", None)),
    ("Batman - 003.cbz", ("Batman", "3", None)),
    ("Comic-001.cbz", ("Comic", "1", None)),
    # No issue number: a one-shot, looked up as issue 1.
    ("Hellboy (2019).cbz", ("Hellboy", "1", 2019)),
    ("Watchmen.cbz", ("Watchmen", "1", None)),
    ("Batman - Year One (1987).cbz", ("Batman - Year One", "1", 1987)),
    # A name that is only a number keeps it.
    ("300 (1998).cbz", ("300", "1", 1998)),
]


@pytest.mark.parametrize("filename,expected", LOOKUP)
def test_metadata_lookup_parse(filename, expected):
    from routes.metadata import series_issue_year_from_filename
    assert series_issue_year_from_filename(filename) == expected


def test_lookup_series_is_the_one_bulk_metadata_uses():
    # Bulk imports the route module's functions rather than copying them.
    import core.bulk_metadata as bulk
    import routes.metadata as routes_metadata
    assert (bulk.extract_series_name_from_filename
            is routes_metadata.extract_series_name_from_filename)
    assert (bulk.extract_series_name_from_folder
            is routes_metadata.extract_series_name_from_folder)


def test_folder_name_strip_has_one_copy():
    # Eight call sites in routes/metadata.py pasted the folder-name regexes;
    # only the function's own body may carry them now.
    import inspect
    import routes.metadata as routes_metadata
    source = inspect.getsource(routes_metadata)
    # (The Inducks lookup strips the year alone, on purpose: it is not a copy.)
    assert source.count(r"re.sub(r'\s*v\d+.*$'") == 1


@pytest.mark.parametrize("folder,expected", [
    ("Batman (2016)", "Batman"),
    ("Batman v3 (2016)", "Batman"),
    ("Naruto - Complete", "Naruto"),
    ("Saga", "Saga"),
    ("v2015", ""),
])
def test_series_name_from_folder(folder, expected):
    from routes.metadata import extract_series_name_from_folder
    assert extract_series_name_from_folder(folder) == expected
