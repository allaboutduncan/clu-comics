"""Tests for the collection grid's issue badge and its read icon.

Three symptoms were reported against one series folder: read status not showing,
issue 008 badged ``#1`` while 001 badged ``#1`` too, and issue 007 getting no
badge at all. The badge number is produced in JavaScript
(``static/js/collection.js``) and there is no JS test runner in this repo, so:

* the *behaviour* is tested by pulling the regex sources out of the .js and
  running them through Python's ``re`` -- they are deliberately written in the
  syntax both engines share, and the table below was verified to give identical
  results under node;
* the *wiring* (which cannot be expressed as a pure function) is asserted
  structurally against the file, the same way ``tests/unit/test_filename_chars.py``
  asserts its JS mirrors.
"""
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COLLECTION_JS = os.path.join(REPO, "static", "js", "collection.js")


@pytest.fixture(scope="module")
def js_src():
    with open(COLLECTION_JS, encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------
# Pull the parser out of the .js so a pattern change is caught here.
# --------------------------------------------------------------------------

def _raw_strings(block):
    """Every String.raw`...` literal in a slice of JS source, in order."""
    return re.findall(r"String\.raw`([^`]*)`", block)


def _code_only(block):
    """Drop // comments so an assertion cannot match prose about the code."""
    return "\n".join(
        line.split("//", 1)[0] for line in block.splitlines()
    )


def _extract_parser(src):
    """Return (marker_patterns, bare_token, yearish, of_strip) from the source."""
    m = re.search(r"const BADGE_YEARISH = /(.+?)/;", src)
    assert m, "BADGE_YEARISH not found -- was the constant renamed?"
    yearish = re.compile(m.group(1))

    m = re.search(r"const BADGE_ISSUE_PATTERNS = \[(.*?)\];", src, re.S)
    assert m, "BADGE_ISSUE_PATTERNS not found -- was the constant renamed?"
    markers = _raw_strings(m.group(1))
    assert markers, "BADGE_ISSUE_PATTERNS held no String.raw patterns"

    m = re.search(r"const BADGE_BARE_TOKEN =\s*(String\.raw`[^`]*`);", src)
    assert m, "BADGE_BARE_TOKEN not found -- was the constant renamed?"
    bare = _raw_strings(m.group(1))[0]

    m = re.search(r"stem\.replace\(/(\\s\*\\\(\?.*?)/ig, ''\)", src)
    assert m, "the 'of NN' strip was not found in extractIssueNumber"
    of_strip = re.compile(m.group(1), re.I)

    return markers, bare, yearish, of_strip


@pytest.fixture(scope="module")
def parser(js_src):
    markers, bare, yearish, of_strip = _extract_parser(js_src)

    def extract(filename):
        if not filename:
            return None
        stem = re.sub(r"\.(cbz|cbr|cb7|cbt|zip|pdf)$", "", filename, flags=re.I)
        stem = of_strip.sub("", stem)
        for source in markers:
            m = re.search(source, stem)
            if m:
                return m.group(1)
        tokens = [m.group(1) for m in re.finditer(bare, stem)]
        if not tokens:
            return None
        for t in tokens:
            if not yearish.match(t):
                return t
        return tokens[0]

    return extract


@pytest.fixture(scope="module")
def strip_zeros(js_src):
    m = re.search(r"String\(num\)\.match\(/(.+?)/\);", js_src)
    assert m, "stripIssueZeros' regex was not found"
    pattern = re.compile(m.group(1))

    def strip(num):
        hit = pattern.match(str(num))
        if not hit:
            return str(num)
        return hit.group(1) + hit.group(2) + (hit.group(3) or "")

    return strip


# Every row was run under node against the real collection.js and returned the
# same value, which is what makes running them here legitimate.
BADGE_CASES = [
    # (filename, expected badge number after zero-stripping)
    ("Captain America 008 (2005).cbz", "8"),
    ("Captain America 001 (2005).cbz", "1"),
    ("Captain America 007 (2005).cbz", "7"),
    # The reported symptom: a trailing subtitle number used to win.
    ("Captain America 008 - Book 1 (2005).cbz", "8"),
    ("Captain America 008 - The Winter Soldier Part 1.cbz", "8"),
    ("Captain America 001 - The Winter Soldier Part 1.cbz", "1"),
    # A mini-series count sits where the issue's trailing context is expected.
    ("Captain America 008 of 12 (2005).cbz", "8"),
    ("Captain America 008 (of 12) (2005).cbz", "8"),
    ("Adventure Time - Quadruple Feature 01 (of 04) (2026).cbz", "1"),
    # Used to produce no badge at all, which also hid the read icon.
    ("Captain America 008 [2005].cbz", "8"),
    ("Captain America 007 - The Winter Soldier.cbz", "7"),
    ("Captain America_008_(2005).cbz", "8"),
    ("Captain America 008 (Sep 2005).cbz", "8"),
    ("Captain America 008 (2005-09).cbz", "8"),
    ("Captain America - 008 - The Winter Soldier (2005).cbz", "8"),
    ("008 - Captain America (2005).cbz", "8"),
    # Decimal/suffix and negative issue numbers.
    ("Captain America 001.MU (2005).cbz", "1.MU"),
    ("Captain America -1 (2005).cbz", "-1"),
    ("The Amazing Spider-Man (2018) Issue 080.BEY.cbz", "80.BEY"),
    # A marker beats position.
    ("Captain America #8 (2005) (4 covers).cbz", "8"),
    ("Top 10 (1999) Volume 01 Issue 010.cbz", "10"),
    # A series name that is or contains a year-like number.
    ("2000 AD 2350 (2023).cbz", "2350"),
    ("1984 001 (1978).cbz", "1"),
    ("Batman '66 001 (2016).cbz", "1"),
    # Other real-world shapes.
    ("Captain America Annual 001 (2005).cbz", "1"),
    ("Captain America v6 008 (2005).cbz", "8"),
    ("Batman_-_Superman_045_2025_Webrip.cbr", "45"),
    ("Captain America (2005) 008.cbz", "8"),
    ("Captain America 008 - 1.cbz", "8"),
    ("Captain America 008 2005.cbz", "8"),
    # Nothing to report.
    ("Captain America.cbz", None),
    ("folder.jpg", None),
]


class TestFilenameFallback:

    @pytest.mark.parametrize("filename,expected", BADGE_CASES)
    def test_table(self, parser, strip_zeros, filename, expected):
        raw = parser(filename)
        got = None if raw is None else strip_zeros(raw)
        assert got == expected

    def test_the_subtitle_number_never_wins(self, parser, strip_zeros):
        # The reported bug: 008 badged '#1', and 001 badged '#1' too, so two
        # cards in one folder claimed the same issue.
        a = strip_zeros(parser("Captain America 008 - Book 1 (2005).cbz"))
        b = strip_zeros(parser("Captain America 001 (2005).cbz"))
        assert a == "8"
        assert b == "1"
        assert a != b

    def test_the_old_unanchored_pattern_is_gone(self, js_src):
        # /\s(\d{1,4})\s*\(\d{4}\)/ only required the digits to sit next to a
        # parenthesised year, so any trailing number won.
        assert r"/\s(\d{1,4})\s*\(\d{4}\)/" not in js_src
        # ...and the two unanchored fallbacks that grabbed a stray '#1' or
        # whatever number happened to precede the extension.
        assert r"/#(\d{1,4})/" not in js_src
        assert r"/\s(\d{1,4})\.[^.]+$/" not in js_src

    def test_the_one_million_issues_are_not_truncated(self, parser):
        # Every other capture is bounded to \d{1,4}; DC's One Million one-shots
        # really are numbered 1,000,000.
        assert parser("Captain America 1000000 (1998).cbz") == "1000000"

    def test_a_series_name_ending_in_a_number_is_a_known_limit(self, parser):
        # Documented in the code: no positional rule can tell a series name
        # ending in a bare number from an issue number. ci_number is the fix,
        # which is why the badge prefers it.
        assert parser("Batman 66 001 (2016).cbz") == "66"


class TestStripIssueZeros:

    @pytest.mark.parametrize("value,expected", [
        ("008", "8"),
        ("001", "1"),
        ("0", "0"),
        ("012.1", "12.1"),
        ("001.MU", "1.MU"),
        ("-01", "-1"),
        ("-1", "-1"),
        ("1000000", "1000000"),
        ("8", "8"),
        # A non-numeric head passes through: ComicInfo <Number> is free text.
        ("Annual 1", "Annual 1"),
        ("", ""),
    ])
    def test_values(self, strip_zeros, value, expected):
        assert strip_zeros(value) == expected

    def test_padding_is_never_used(self, js_src):
        # padStart would turn '-1' into '0-1', and the badge must agree with the
        # series page and the reading-list badge, which both strip.
        start = js_src.index("function badgeIssueNumber")
        body = js_src[start:start + 1200]
        assert "stripIssueZeros(" in body
        assert "padStart(" not in body
        assert "padIssueNumber" not in body


class TestBadgeIsNotGatedOnTheNumber:
    """The read icon is a child of .issue-badge, so hiding the badge when the
    number cannot be parsed hid the read status too -- one bug reported as
    "no badge" and "read status not showing"."""

    @pytest.fixture(scope="class")
    def badge_block(self, js_src):
        start = js_src.index("gridItem.classList.add('has-comic')")
        end = js_src.index("Show missing XML badge", start)
        return js_src[start:end]

    def test_display_is_not_inside_a_number_branch(self, badge_block):
        # `if (issueNumberSpan)` is fine -- it guards the span, not the badge.
        # What must not exist is a branch on the number itself.
        assert re.search(r"if \(issueNum\s*[)&|]", badge_block) is None, (
            "the badge's display must not be gated on having an issue number")

    def test_the_number_is_optional(self, badge_block):
        assert "issueNum ? '#' + issueNum : ''" in badge_block

    def test_it_asks_badgeIssueNumber_not_the_filename_parser(self, badge_block):
        assert "badgeIssueNumber(item)" in badge_block
        assert "extractIssueNumber(item.name)" not in badge_block

    def test_an_unnumbered_badge_is_marked_for_css(self, badge_block):
        assert "issue-badge-unnumbered" in badge_block
        css = open(os.path.join(REPO, "static", "css", "collection.css"),
                   encoding="utf-8").read()
        assert ".issue-badge-unnumbered .read-icon" in css

    def test_read_state_is_applied_for_every_file(self, js_src):
        # Outside the hasThumbnail branch: one unconditional site per file, so
        # the icon and the menu labels cannot disagree.
        start = js_src.index("gridItem.classList.add('file')")
        end = js_src.index("Show missing XML badge", start)
        body = js_src[start:end]
        i_has_comic = body.index("gridItem.classList.add('has-comic')")
        i_apply = body.index("applyReadStateToGridItem(gridItem")
        assert i_apply > i_has_comic, "read state must come after the badge block"
        # And the old duplicate inside the dropdown branch is gone.
        assert "const isRead = readIssuesSet.has(item.path);" not in js_src


class TestReadStateIsOneFunction:

    def test_the_primitive_covers_icon_and_labels(self, js_src):
        start = js_src.index("function applyReadStateToGridItem")
        body = js_src[start:js_src.index("function applyReadIconsToGrid")]
        assert "bi-book-fill" in body
        assert ".set-read-date-text" in body
        assert ".action-mark-unread" in body
        assert ".action-hide-history" in body

    def test_update_read_icon_delegates(self, js_src):
        start = js_src.index("function updateReadIcon")
        body = js_src[start:js_src.index("function applyReadStateToGridItem")]
        assert "applyReadStateToGridItem(item, isRead)" in body
        # No second copy of the class flip.
        assert "bi-book-fill" not in body

    def test_the_backfill_iterates_the_grid_not_the_set(self, js_src):
        # readIssuesSet can hold tens of thousands of paths; one updateReadIcon
        # call per read path would be one querySelectorAll per path.
        start = js_src.index("function applyReadIconsToGrid")
        body = js_src[start:start + 500]
        assert "querySelectorAll('.grid-item.file[data-path]')" in body
        assert "readIssuesSet.has(el.dataset.path)" in body
        assert "updateReadIcon(" not in body


class TestReaderBridge:
    """reader.js's host contract is _readerAllItems / _readerReadIssuesSet /
    _readerOnMarkedRead. collection.js got the first right and the other two
    wrong, and neither defect is visible to a test that does not read the file."""

    def test_the_set_is_published_after_it_is_rebuilt(self, js_src):
        # readIssuesSet = new Set(...) REBINDS the variable, so publishing at
        # parse time handed reader.js the initial empty Set for the life of the
        # page -- and _markPathAsRead added to an orphan.
        publishes = [m.start() for m in
                     re.finditer(r"window\._readerReadIssuesSet\s*=", js_src)]
        assert len(publishes) == 1, "publish _readerReadIssuesSet exactly once"
        rebind = js_src.index("readIssuesSet = new Set(data.paths")
        assert publishes[0] > rebind, (
            "_readerReadIssuesSet must be published after readIssuesSet is rebound")

    def test_the_false_comment_is_gone(self, js_src):
        assert "never reassigned" not in js_src

    def test_the_marked_read_hook_exists(self, js_src):
        # Without it, finishing a comic in the reader never flipped the icon.
        assert "window._readerOnMarkedRead = function" in js_src
        start = js_src.index("window._readerOnMarkedRead = function")
        body = js_src[start:start + 300]
        assert "readIssuesSet.add(path)" in body
        assert "updateReadIcon(path, true)" in body

    def test_the_race_with_the_first_render_is_closed(self, js_src):
        # loadDirectory() and this fetch are both started from DOMContentLoaded;
        # nothing used to reconcile the grid when the fetch lost.
        start = js_src.index("function loadReadIssues")
        body = _code_only(js_src[start:start + 1400])
        assert "applyReadIconsToGrid()" in body
        # Not a re-render: that would drop and re-request every thumbnail.
        assert "renderPage(" not in body

    def test_paths_are_matched_byte_exact(self, js_src):
        # issues_read.issue_path is stored byte-exact so it joins against
        # file_index.path (CLAUDE.md, Path References).
        start = js_src.index("function loadReadIssues")
        body = _code_only(js_src[start:start + 1400])
        assert "toLowerCase()" not in body
        assert ".trim()" not in body
        assert "normalize(" not in body


class TestBadgePrefersTheIndexedNumber:

    @pytest.fixture(scope="class")
    def body(self, js_src):
        start = js_src.index("function badgeIssueNumber")
        return js_src[start:start + 1200]

    def test_ci_number_is_preferred_over_the_filename(self, body):
        i_index = body.index("item.ciNumber")
        i_filename = body.index("extractIssueNumber(item.name)")
        assert i_index < i_filename

    def test_blank_ci_number_falls_back(self, body):
        # '' is a real stored value -- a file tagged with an empty <Number>.
        assert "fromIndex || extractIssueNumber(item.name)" in body
        assert ".trim()" in body

    def test_both_feeders_map_ci_number(self, js_src):
        # /api/browse and /api/browse-recursive supply it; the other modes do
        # not and keep the filename answer.
        assert js_src.count("ciNumber: file.ci_number") == 2
