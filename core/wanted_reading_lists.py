"""Unmatched reading-list entries as wanted issues.

A reading list used to be a dead end: when an import or a Re-match could not
find a file, the entry showed as a red tile on the list page and nothing else
happened. The issue never reached the Wanted page and the nightly GetComics
sweep never looked for it.

Nothing here is stored. "Unmatched" is already exactly
``matched_file_path IS NULL AND manual_override_path IS NULL``, so the wanted
set is *derived* from that and both halves of the feature come for free:
``process_rematch`` writing ``None`` puts an issue on the list, and
``map_entry`` writing a path takes it off. There is no second copy of the truth
to drift.

Two things this deliberately does NOT reuse:

- **The ``wanted_issues`` table.** It is keyed ``series_id``/``issue_id``
  (Metron ids, both NOT NULL) and ``refresh_wanted_cache_background`` opens by
  wiping it, so a row inserted here would be both unrepresentable and
  short-lived. Reading-list entries carry at most a Metron *issue* id; CBL and
  GitHub imports carry no provider ids at all.
- **``reading_list_entries.volume`` as a volume number.** That column is
  heterogeneous -- CBL writes its ``<Volume>`` element, which is a *year*; the
  Metron importer writes a volume *number*; ComicVine writes NULL. Feeding a
  year to ``score_getcomics_result(series_volume=...)`` would fail the volume
  check against every result, so work items carry ``series_volume=None`` and
  let ``series_year`` carry the year.
"""

from datetime import date, datetime, timedelta

from core.database import get_db_connection
from core.app_logging import app_logger

# How long a queued entry is left alone before the sweep may search for it
# again. A download that succeeded and was filed will have re-matched long
# before this; one that silently failed gets another attempt within the week.
QUEUE_COOLDOWN_DAYS = 7


def _norm_series(name):
    """The equivalence class two series names share for de-duplication.

    Uses the same normaliser the GetComics scorer does, so "Batman: Year One"
    from a CBL and "Batman - Year One" from a mapped series collapse together.
    Imported lazily: ``models/getcomics.py`` pulls in ``cloudscraper`` at module
    level, which has no business being a dependency of this module.
    """
    try:
        from models.getcomics import normalize_series_for_compare

        return normalize_series_for_compare(name or "")
    except Exception:
        return str(name or "").strip().lower()


def _norm_issue(number):
    """Normalise an issue number for de-duplication.

    Deliberately *not* ``issue_number_to_int``: that returns None for "1.MU",
    "Annual" and fractional numbers, which would collapse every non-numeric
    issue of a series into a single bucket and silently drop all but the first.
    """
    s = str(number or "").strip().lstrip("#").strip()
    if not s:
        return ""
    return str(int(s)) if s.isdigit() else s.lower()


def dedupe_key(series, issue_number):
    """The key an item is de-duplicated on, across both wanted sources."""
    return (_norm_series(series), _norm_issue(issue_number))


def reading_list_wanted_rows():
    """Unmatched entries of every reading list opted into the wanted list.

    The series/issue-number guards are not defensive noise:
    ``CBLLoader.match_file`` returns None immediately when either is blank, so
    an entry missing one can never match and would otherwise sit on the wanted
    list forever, searched every night.
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []
        c = conn.cursor()
        c.execute(
            """
            SELECT e.id             AS entry_id,
                   e.series         AS series,
                   e.issue_number   AS issue_number,
                   e.year           AS year,
                   e.issue_year     AS issue_year,
                   e.metron_id      AS metron_id,
                   e.last_queued_at AS last_queued_at,
                   rl.id            AS reading_list_id,
                   rl.name          AS reading_list_name
            FROM reading_list_entries e
            JOIN reading_lists rl ON rl.id = e.reading_list_id
            WHERE COALESCE(rl.track_wanted, 0) = 1
              AND e.matched_file_path IS NULL
              AND e.manual_override_path IS NULL
              AND TRIM(COALESCE(e.series, '')) <> ''
              AND TRIM(COALESCE(e.issue_number, '')) <> ''
            ORDER BY rl.name, e.sort_order, e.id
            """
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        app_logger.error(f"Failed to read reading-list wanted entries: {e}")
        return []


def _released(row, current_year):
    """Whether this entry's issue is out yet.

    There is no ``store_date`` on a reading-list entry, so the year is the only
    signal. A NULL year means "released", not "skip" -- the opposite of the
    mapped-series rule in ``app.scheduled_getcomics_download``. That rule is
    right for a release calendar, where an undated row is an unscheduled
    solicitation; a reading list is overwhelmingly back catalogue, and
    ``issue_year`` is NULL on every row imported before that column existed.
    Skipping NULL would make the feature quietly do nothing for the lists
    people import most.
    """
    year = row.get("issue_year") or row.get("year")
    if not year:
        return True
    try:
        return int(year) <= current_year
    except (TypeError, ValueError):
        return True


def _off_cooldown(row, now, cooldown_days):
    """Whether enough time has passed to search for this entry again."""
    stamp = row.get("last_queued_at")
    if not stamp:
        return True
    text = str(stamp).replace("Z", "").strip()
    for parse in (
        lambda: datetime.fromisoformat(text),
        lambda: datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            return now - parse() >= timedelta(days=cooldown_days)
        except ValueError:
            continue
    # An unparseable stamp must not pin an entry off the list forever.
    return True


def get_reading_list_wanted_items(released_only=False, today=None):
    """Wanted items derived from opted-in reading lists, de-duplicated.

    De-duplication happens here rather than in SQL so one normalisation covers
    both the collision *within* the results (an arc list and a "best of" list
    both containing Crisis #1 is common) and the collision against the
    mapped-series sweep, which ``build_reading_list_work_items`` layers on top.

    Args:
        released_only: drop entries whose year is still in the future.
        today: ISO date string, for tests.

    Returns:
        List of row dicts, one per distinct series/issue.
    """
    current_year = int((today or date.today().isoformat())[:4])
    out, seen = [], set()
    for row in reading_list_wanted_rows():
        if released_only and not _released(row, current_year):
            continue
        key = dedupe_key(row.get("series"), row.get("issue_number"))
        if key in seen:
            continue
        seen.add(key)
        # One year for every consumer to read, so the Wanted page and the
        # sweep cannot disagree about which year narrows the search.
        #
        # The fallback is not sloppiness: `year` holds the issue's own year for
        # a CBL import (the <Year> element) and the series start year for a
        # Metron one, which is exactly why entry_year_hints feeds both into
        # issue_years rather than choosing between them.
        row["search_year"] = row.get("issue_year") or row.get("year") or None
        out.append(row)
    return out


def build_reading_list_work_items(existing=(), today=None,
                                  cooldown_days=QUEUE_COOLDOWN_DAYS, now=None):
    """Work items for the GetComics sweep, in the shape its search body reads.

    Args:
        existing: the work items already collected from mapped series. Anything
            matching one of those is dropped -- the mapped-series item carries a
            publisher and a real volume number, so it searches better.
        today: ISO date string, for tests.
        cooldown_days: skip entries queued more recently than this.
        now: datetime, for tests.

    Returns:
        List of work-item dicts.
    """
    now = now or datetime.now()
    taken = {
        dedupe_key(i.get("series_name"), i.get("issue_num"))
        for i in (existing or ())
    }

    alias_cache = {}

    def _aliases(series_name):
        # Aliases are keyed by series NAME, not id, so reading-list items
        # inherit them for free. Memoised: rows are ordered by list, so the
        # same series recurs in runs.
        if series_name not in alias_cache:
            try:
                from models.getcomics import get_series_alias_list

                alias_cache[series_name] = get_series_alias_list(series_name)
            except Exception:
                alias_cache[series_name] = []
        return alias_cache[series_name]

    items = []
    for row in get_reading_list_wanted_items(released_only=True, today=today):
        key = dedupe_key(row.get("series"), row.get("issue_number"))
        if key in taken:
            continue
        if not _off_cooldown(row, now, cooldown_days):
            continue
        taken.add(key)

        series_name = row["series"]
        items.append({
            "source": "reading_list",
            "entry_id": row["entry_id"],
            "reading_list_id": row["reading_list_id"],
            "reading_list_name": row["reading_list_name"],
            "series_name": series_name,
            "issue_num": str(row["issue_number"]).strip(),
            "issue_year": row["search_year"],
            "series_year": row.get("year"),
            # Not row["volume"] -- see the module docstring.
            "series_volume": None,
            "publisher_name": None,
            "series_aliases": _aliases(series_name),
            # No cover_date, so the proactive scrape-index refresh no-ops.
            "cover_date": None,
        })
    return items
