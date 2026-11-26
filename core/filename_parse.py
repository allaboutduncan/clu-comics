"""Shared primitives for reading a series name or issue number out of a filename.

Pure functions: no logging, no settings, no imports from the app. Parsers all
over the repo strip "(2014)" and then guess the issue number by its position,
which throws away the one reliable marker of where the number sits:

    Detective Comics 156 (1937) - Batmobile of 1950.cbz

Once the group is gone, "1950" is the last number (#778) and "156 - Batmobile
of 1950" is glued to the series name (#790). Read the other way round, the
first group is an anchor: the issue number is the last token before it and
everything after it is title text.

What must NOT be folded in here: the renamer ladders in cbz_ops/rename.py
(their output is filenames on disk) and the stored GetComics keys. See
.claude/rules/series-name-matching.md.
"""
import re

# A leading minus is part of the issue NUMBER, not a sign to be discarded:
# Marvel's 1997 "Flashback" month shipped as issue -1, and Metron/ComicVine
# carry it as "-1" (some rows as "-01"). helpers/collection.py aliases these.
SIGN_CHARS = "-‐‑‒–—―−"
SIGN_CLASS = "[" + SIGN_CHARS + "]"

_FIRST_GROUP = re.compile(r"\([^)]*\)|\[[^\]]*\]")

# "... 001", "... #001", "... -001", "... 001.5", "... 001 of 5" at the end.
# The sign must be attached to the digits and the token must follow whitespace,
# so the dash of "Black Cat - 001" is a separator, not a sign.
TRAILING_ISSUE = re.compile(
    rf"\s+#?{SIGN_CLASS}?\d+(?:\.\w+)?(?:\s+of\s+\d+)?\s*$", re.IGNORECASE
)


def prefix_before_first_group(stem):
    """Text before the first "(...)" or "[...]" group, and whether one exists.

    ``stem`` is a filename without its extension. Returns ``(stem, False)``
    when there is no group. The prefix can be empty ("(2016) Batman 001"), and
    it need not hold a number ("X-Force (1991) Issue -1"); a caller that needs
    one falls back to reading the whole name.
    """
    match = _FIRST_GROUP.search(stem)
    if not match:
        return stem, False
    return stem[: match.start()].rstrip(), True


def ends_with_issue_number(text):
    """True when ``text`` ends in an issue-number token (see TRAILING_ISSUE)."""
    return bool(TRAILING_ISSUE.search(text))


# --- Which issue(s) a filename names -----------------------------------------
#
# The library scan's last tier used to search a filename for the wanted number
# anywhere it was delimited, so "Batman 400 - Knightfall Part 1" was also #1 and
# "Spider-Man 2099 005 (1993)" was also #2099 (#792), while "Series V02 - Title"
# was never volume 2 (#795). ``issue_numbers`` reads the name once and says
# which number(s) it carries; the caller compares.

# An issue-number token: optional glued sign, digits, an optional point suffix
# (".1", ".BEY") and an optional short letter suffix ("13B"). Ordinals ("100th")
# are not numbers.
_ISSUE_TOKEN = (
    rf"{SIGN_CLASS}?\d+(?:\.\w+)?"
    r"(?:(?!st\b|nd\b|rd\b|th\b)[A-Za-z]{1,3})?"
)
# A bare token starts where no word is running: "v2", "X-23" and "001.5"'s own
# tail are not tokens. The lookbehind sits before the optional sign, so the
# dash of "Batman-001" (hanging off a word) is a separator, not a sign.
_BARE_TOKEN = re.compile(rf"(?<![\w.])({_ISSUE_TOKEN})(?!\w)")
_EXPLICIT_TOKEN = re.compile(
    rf"(?:#\s*|\bissue\s+)({_ISSUE_TOKEN})(?!\w)", re.IGNORECASE
)
# "001-006": digits, dash, digits, nothing between.
_RANGE_TOKEN = re.compile(r"(?<![\w.])#?(\d+)-(\d+)(?!\w)")
_OF_COUNT = re.compile(r"\bof\s+\d+\b", re.IGNORECASE)
# "v07", "V02", "Vol. 2", "Volume 003": a volume ordinal.
_VOLUME_TOKEN = re.compile(
    r"(?<![\w.])(v|vol\.?|volume)\s*(\d+)(?!\w)", re.IGNORECASE
)
# "Book 2" names a collected volume too, but only a collected series is asked.
_BOOK_TOKEN = re.compile(r"(?<![\w.])(book)\s*(\d+)(?!\w)", re.IGNORECASE)
_YEAR_SHAPED = re.compile(r"(?:19|20)\d{2}")
# A range wider than this is a typo or a date, not a pack of issues.
_MAX_RANGE = 500


def _blank(text, pattern):
    """Replace every match with spaces, keeping every offset where it was."""
    return pattern.sub(lambda m: " " * len(m.group()), text)


def issue_numbers(stem, start=0, prefer_volume=False):
    """The issue number(s) a filename names, and whether it names a range.

    ``stem`` is a filename without its extension. ``start`` is where the series
    name ends when the caller recognised it at the front of the name; nothing
    before it is read, which is what keeps the "2099" of "Spider-Man 2099" and
    the "52" of "The New 52" from being issue numbers. ``prefer_volume`` is for
    a collected-edition series, whose "issues" are volumes.

    Returns ``(numbers, is_range)``. ``numbers`` are raw tokens ("005",
    "-001", "78.BEY"); compare them through an issue-number normaliser. A name
    that carries no number returns ``([], False)``.

    In order:

    1. A range, "001-006": every number in it.
    2. An explicit "#N" / "Issue N", wherever it sits.
    3. With ``prefer_volume``, a volume marker ("v07", "Vol. 2", "Book 2").
    4. The last number before the first "(...)"/"[...]" group.
    5. No number there: the first number after the series name ("400 -
       Knightfall Part 1" is #400). When the series name was not recognised
       the last 3+ digit number is taken instead, so "Spider-Man 2099 005"
       is still #5.
    6. Nothing but a spelled-out "Vol. N" / "Volume N": that number. A bare
       "v2" is not taken here, since on a single issue it is the series volume.
    """
    text = (stem or "").replace("_", " ")
    after_series = start > 0
    first_group = _FIRST_GROUP.search(text, start)
    group_at = first_group.start() if first_group else len(text)
    # Years, release tags and duplicate counters live in groups; never numbers.
    text = _blank(text, _FIRST_GROUP)

    for match in _RANGE_TOKEN.finditer(text, start, group_at):
        low, high = int(match.group(1)), int(match.group(2))
        both_years = all(_YEAR_SHAPED.fullmatch(g) for g in match.groups())
        if low < high <= low + _MAX_RANGE and not both_years:
            return [str(n) for n in range(low, high + 1)], True

    explicit = _EXPLICIT_TOKEN.search(text, start)
    if explicit:
        return [explicit.group(1)], False

    volumes = list(_VOLUME_TOKEN.finditer(text, start))
    if prefer_volume:
        markers = sorted(
            volumes + list(_BOOK_TOKEN.finditer(text, start)),
            key=lambda m: m.start(),
        )
        if markers:
            return [markers[0].group(2)], False

    # "01 of 12": the count is not the issue. A volume ordinal is not either.
    text = _blank(_blank(text, _OF_COUNT), _VOLUME_TOKEN)
    tokens = list(_BARE_TOKEN.finditer(text, start))
    before_group = [t for t in tokens if t.start() < group_at]
    if first_group and before_group:
        return [before_group[-1].group(1)], False
    if tokens:
        if after_series:
            return [tokens[0].group(1)], False
        wide = [t for t in tokens if len(re.sub(r"\D.*$", "", t.group(1).lstrip(SIGN_CHARS))) >= 3]
        return [(wide[-1] if wide else tokens[0]).group(1)], False

    spelled = [v for v in volumes if len(v.group(1)) > 1]
    if spelled:
        return [spelled[0].group(2)], False
    return [], False
