"""
Cross-check a matched issue's date against the date claimed by its filename.

CLU parses a year from a filename or folder and uses it to *find* a series, but
nothing verifies the issue that is ultimately written. When a series match is
wrong the issue number still resolves, so the file is tagged with confidently
wrong metadata rather than left alone -- e.g. a folder named
``Diabolik - Nero Su Nero (2014)`` matching the unrelated 1999 American
``Diabolik``.

Comparing the matched issue's date against the filename catches that, and does
so without knowing anything about a particular provider: every provider
populates ``cover_date`` on ``IssueResult``, and every ``to_comicinfo``
implementation emits ``Year``.

``issue_year_from_filename`` and ``date_conflict`` are pure and never raise.
``evaluate`` wraps both for call sites and logs a conflict as a side effect, so
that a rejected match is never silent. Callers decide what to *do* about a
conflict; see ``date_check_mode()`` for whether they should act on one at all.
"""
import re
from datetime import datetime
from typing import Any, Optional

from core.app_logging import app_logger
from core.config import config


# A standalone four-digit year. The lookbehind is what keeps scan credits out:
# "Hal2008" and "bud_666" name a scanner, not a publication date. The trailing
# guard stops a longer digit run (a page count, a resolution) contributing its
# first four digits.
_YEAR = re.compile(r"(?<![0-9A-Za-z])(?:19|20)\d{2}(?![0-9])")

# Modes, in increasing order of consequence.
MODE_OFF = "off"
MODE_LOG = "log"
MODE_ENFORCE = "enforce"
_VALID_MODES = (MODE_OFF, MODE_LOG, MODE_ENFORCE)

DEFAULT_TOLERANCE_YEARS = 2


def date_check_mode() -> str:
    """How callers should treat a date conflict: 'off', 'log' or 'enforce'.

    Read at call time rather than at import, so a settings change takes effect
    without a restart.
    """
    try:
        raw = config.get("SETTINGS", "DATE_CHECK_MODE", fallback=MODE_OFF)
    except Exception:
        return MODE_OFF
    mode = str(raw or "").strip().lower()
    return mode if mode in _VALID_MODES else MODE_OFF


def date_check_tolerance() -> int:
    """Years of disagreement to allow before calling a match a conflict.

    Cover date, on-sale date and the year a scanner puts in a filename routinely
    disagree by a year, and reprints by more, so only a gross mismatch should
    count against a match.
    """
    try:
        value = config.getint("SETTINGS", "DATE_CHECK_TOLERANCE_YEARS",
                              fallback=DEFAULT_TOLERANCE_YEARS)
    except Exception:
        return DEFAULT_TOLERANCE_YEARS
    return value if value >= 0 else DEFAULT_TOLERANCE_YEARS


def issue_year_from_filename(name: Optional[str]) -> Optional[int]:
    """The publication year a filename claims, or None if it claims none.

    Distinct from ``extract_year_from_name`` in ``routes/metadata.py``, which
    answers a different question -- the year a *series* began. This one accepts
    any standalone year, including the scene-release form "(Mondadori 1957-12)"
    that carries a per-issue date.

    The "vYYYY" volume marker is deliberately *not* accepted: it names the run,
    not the issue, so reading it as an issue date would make every later issue
    of a long-running series look like a conflict. The lookbehind excludes it
    for free, since a letter precedes the digits.

    Returns None when the filename names more than one distinct year: two
    candidates cannot disambiguate anything, and guessing between them is how a
    correct match gets rejected.
    """
    if not name:
        return None

    upper_bound = datetime.now().year + 1
    years = {
        int(match.group(0)) for match in _YEAR.finditer(str(name))
    }
    plausible = {year for year in years if 1900 <= year <= upper_bound}

    if len(plausible) != 1:
        return None
    return plausible.pop()


def _year_of(issue_date: Any) -> Optional[int]:
    """Year from whatever a provider hands back: 'YYYY-MM-DD', 'YYYY-MM',
    'YYYY', an int, or None."""
    if issue_date is None:
        return None
    if isinstance(issue_date, int):
        return issue_date if 1900 <= issue_date <= 2999 else None
    text = str(issue_date).strip()
    if not text:
        return None
    match = re.match(r"^(\d{4})", text)
    if not match:
        return None
    year = int(match.group(1))
    return year if 1900 <= year <= 2999 else None


def date_conflict(filename_year: Optional[int], issue_date: Any,
                  tolerance: Optional[int] = None) -> bool:
    """True when the issue's date contradicts the filename beyond `tolerance`.

    False whenever either side is missing or unparseable. A missing date is not
    evidence of a bad match, and treating it as one would reject every provider
    that happens not to carry dates.
    """
    if filename_year is None:
        return False
    issue_year = _year_of(issue_date)
    if issue_year is None:
        return False
    if tolerance is None:
        tolerance = date_check_tolerance()
    return abs(issue_year - filename_year) > tolerance


def conflict_message(filename: str, filename_year: int, issue_date: Any) -> str:
    """One-line explanation, shared so the log and the UI agree."""
    return (
        f"Date conflict for {filename}: filename says {filename_year}, "
        f"matched issue is dated {issue_date}"
    )


def evaluate(filename: Optional[str], issue_date: Any) -> tuple:
    """Convenience for call sites: ``(mode, conflicted, filename_year)``.

    Short-circuits entirely when the mode is 'off', so the disabled path costs
    one config read and does no parsing at all.
    """
    mode = date_check_mode()
    if mode == MODE_OFF:
        return MODE_OFF, False, None

    filename_year = issue_year_from_filename(filename)
    conflicted = date_conflict(filename_year, issue_date)
    if conflicted:
        app_logger.info(conflict_message(filename, filename_year, issue_date))
    return mode, conflicted, filename_year
