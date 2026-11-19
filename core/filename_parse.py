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
