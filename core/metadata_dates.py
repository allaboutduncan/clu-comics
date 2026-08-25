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

``evaluate_series`` is the folder-level counterpart: a folder year names the
year the series *began*, not the year an issue came out, so it is compared with
the matched series' start year. Keeping the two comparisons apart is what stops
issue #200 of a 1962 series reading as a conflict against its folder.
"""
import re
from datetime import datetime
from typing import Any, Optional

from core.app_logging import app_logger


# A standalone four-digit year -- alphanumerics on neither side.
#
# The lookbehind keeps scan credits out: "Hal2008" and "bud_666" name a scanner,
# not a publication date. The lookahead is symmetric for the same reason, and it
# matters more than it looks: digital releases are routinely tagged with a pixel
# height, and "1920px" is both extremely common and a plausible-looking year.
# Guarding only against a following digit let "Batman 001 (1920px).cbz" claim
# 1920, and made "Batman 001 (2016) (Digital) (1920px).cbz" ambiguous between
# two years so the check silently skipped the file.
_YEAR = re.compile(r"(?<![0-9A-Za-z])(?:19|20)\d{2}(?![0-9A-Za-z])")

# Modes, in increasing order of consequence.
MODE_OFF = "off"
MODE_LOG = "log"
MODE_ENFORCE = "enforce"
_VALID_MODES = (MODE_OFF, MODE_LOG, MODE_ENFORCE)

DEFAULT_TOLERANCE_YEARS = 2

# Preference keys. config.ini is deprecated for new settings (CLAUDE.md), so
# these live in the user_preferences table.
PREF_MODE = "date_check_mode"
PREF_TOLERANCE = "date_check_tolerance_years"

# Providers whose ComicInfo ``Year`` is the year the *series* began rather than
# the year this issue was published, and which leave ``IssueResult.cover_date``
# as None so there is nothing issue-level to fall back on.
#
# The three manga providers assign ``series_year`` to ``Year``
# (mangadex_provider.py:376, anilist_provider.py:381, mangaupdates_provider.py),
# and Bedetheque does the same with ``series.year``
# (bedetheque_provider.py:537 and :571).
#
# Comparing that against a filename would reject every volume of a long-running
# series: "Berserk v22 (2003)" against a series that began in 1989 is fourteen
# years apart and perfectly correct. The check simply does not apply to them.
#
# Keep this in step with the providers in ``models/providers/``: a new provider
# that reports a series year here will produce false rejections until listed.
_SERIES_YEAR_PROVIDERS = frozenset({
    "anilist", "mangadex", "mangaupdates", "bedetheque",
})

# Providers whose ComicInfo ``Year`` is the issue's own publication year, and
# which the check therefore applies to. Listing these explicitly rather than
# treating "not in the deny list" as safe means a provider added later is
# exempt until someone classifies it -- the wrong default here rejects real
# matches, so an unknown provider gets the benefit of the doubt.
_ISSUE_YEAR_PROVIDERS = frozenset({
    "comicvine", "comicvine_sqlite", "gcd", "gcd_api", "metron",
})


def year_is_issue_level(provider: Optional[str]) -> bool:
    """Whether this provider's ComicInfo ``Year`` describes the issue.

    Accepts either a provider slug ("comicvine_sqlite") or the display string
    the batch path carries in ``source`` ("ComicVine (Local DB)", "GCD API"),
    so matching is on substring.

    False for anything unrecognised, including None: rejecting a user's match
    on a year we cannot interpret is worse than not checking it.
    """
    if not provider:
        return False
    key = str(provider).strip().lower()
    if any(name in key for name in _SERIES_YEAR_PROVIDERS):
        return False
    return any(name in key for name in _ISSUE_YEAR_PROVIDERS)


def date_check_mode() -> str:
    """How callers should treat a date conflict: 'off', 'log' or 'enforce'.

    Stored in ``user_preferences`` rather than config.ini, which CLAUDE.md
    deprecates for new settings. Read at call time rather than at import, so a
    change takes effect without a restart.
    """
    try:
        from core.database import get_user_preference
        raw = get_user_preference(PREF_MODE, default=MODE_OFF)
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
        from core.database import get_user_preference
        value = int(get_user_preference(PREF_TOLERANCE,
                                        default=DEFAULT_TOLERANCE_YEARS))
    except (TypeError, ValueError):
        return DEFAULT_TOLERANCE_YEARS
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


def series_conflict_message(folder_name: str, folder_year: int,
                            series_year: Any) -> str:
    """One-line explanation of a folder-year/series-start-year disagreement."""
    return (
        f"Date conflict for folder {folder_name}: folder says {folder_year}, "
        f"matched series began {series_year}"
    )


def evaluate_series(folder_name: Optional[str], folder_year: Optional[int],
                    series_year: Any) -> tuple:
    """``(mode, conflicted)`` for a folder's year against a series' start year.

    Deliberately separate from ``evaluate``, because the two years do not mean
    the same thing. A filename year is the year *that issue* was published and
    belongs against the issue's cover date; a folder year (``Captain Marvel
    (2002)``, ``v2002``) is the year the *series* began and belongs against the
    series' start year. Comparing a folder year with a cover date would flag
    issue #200 of a 1962 series as a twenty-year disagreement when it is simply
    a long run.

    Callers must pass a year taken from the folder name itself. The year from
    ``_series_year_for_folder`` will not do: it falls back to the first
    filename, which carries an issue year.
    """
    mode = date_check_mode()
    if mode == MODE_OFF:
        return MODE_OFF, False

    conflicted = date_conflict(folder_year, series_year)
    if conflicted:
        app_logger.info(
            series_conflict_message(folder_name, folder_year, series_year)
        )
    return mode, conflicted
