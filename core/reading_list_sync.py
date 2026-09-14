"""Re-check an imported reading list against the source it was imported from.

An imported list is a snapshot. Nothing in CLU ever went back to the provider
that produced it, so a curator adding three issues to "Crisis on Infinite
Earths" was invisible -- re-running the import just produced a second list,
because nothing looks a reading list up by its ``source``.

This module closes that for all four sources CLU imports from. The shape is the
same for each one:

    probe(row)  -- one cheap request that yields a *change token*
    apply(row)  -- the expensive rebuild, run only when the token moved

``reading_lists.source_version`` stores that token. It is **opaque**: compare
it, never parse it (``_token_date`` is the one exception, and it tolerates
anything). One column serves every provider, which is the point -- the
alternative was a per-provider column each.

| Source                          | Probe (1 call)          | Token                        |
|---------------------------------|-------------------------|------------------------------|
| a GitHub CBL url                | GET the raw file        | sha256 of the content        |
| ``metron://reading-list/<id>``  | ``api.reading_list``    | the list's ISO ``modified``  |
| ``metron://arc/<id>``           | ``api.arc_issues_list`` | fingerprint of the issue ids |
| ``comicvine://arc/<id>``        | ``cv.get_story_arc``    | fingerprint + last-updated   |

Two of those are not obvious:

- **An arc cannot use ``modified``.** An arc's own ``modified`` advances when
  the arc *record* is edited; adding an issue to an arc modifies the **issue**.
  So an arc is fingerprinted by its membership, read from a call the sync has
  to make anyway.
- **The ComicVine probe is where the saving is.** ``fetch_cv_arc_issues`` makes
  one request per issue in the arc and says so in its own comment about
  exhausting the hourly budget. ``get_story_arc`` already returns the issue
  ids, so an unchanged CV arc now costs one request instead of N.

Everything lives here rather than in ``app.py`` because ``app.py`` cannot be
imported in tests; the scheduled sweep there is a thin wrapper over
``sync_all``.
"""

import hashlib
import re as re_module
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import requests

from core.app_logging import app_logger
from core.metadata_dates import year_of

# Source kinds. ``parse_source`` returns one of these, or None.
GITHUB = "github"
METRON_LIST = "metron-list"
METRON_ARC = "metron-arc"
COMICVINE_ARC = "comicvine-arc"

_GITHUB_HOSTS = {"github.com", "raw.githubusercontent.com"}

# Allowed HTML tags for imported descriptions (ComicVine, Metron, etc.)
_SAFE_TAGS = {'p', 'br', 'b', 'strong', 'em', 'i', 'u', 'ul', 'ol', 'li', 'a', 'h2', 'h3', 'h4'}

# How far back ``sync_all`` is willing to push its single ``modified_gt`` call.
# mokkari follows every ``next`` link inside one call and does so *below* the
# shared pacer (see ``models.metron.list_issues_modified_since``), so a window
# of "everything since 2019" would pull the whole corpus in one request. Lists
# older than this are probed individually instead -- a handful of calls, and
# only for lists nobody has synced in a year.
MAX_BULK_LOOKBACK_DAYS = 365


# ---------------------------------------------------------------------------
# Source identification
# ---------------------------------------------------------------------------


def _is_github_url(url):
    """Check if a URL is from github.com or raw.githubusercontent.com using proper URL parsing."""
    try:
        parsed = urlparse(url)
        return parsed.hostname in _GITHUB_HOSTS
    except Exception:
        return False


def _convert_github_blob_to_raw(url):
    """Convert a github.com blob URL to a raw.githubusercontent.com URL.

    Only transforms URLs whose hostname is exactly github.com and whose path
    contains /blob/.  Returns the URL unchanged otherwise.
    """
    try:
        parsed = urlparse(url)
        if parsed.hostname == "github.com" and "/blob/" in parsed.path:
            new_path = parsed.path.replace("/blob/", "/", 1)
            return parsed._replace(
                netloc="raw.githubusercontent.com", path=new_path
            ).geturl()
    except Exception:
        pass
    return url


_PROVIDER_SOURCE_RE = re_module.compile(
    r"^(metron|comicvine)://(reading-list|arc)/(\d+)/?$", re_module.IGNORECASE
)


def parse_source(source):
    """Classify a ``reading_lists.source`` string.

    Returns ``(kind, ident)`` where kind is one of the module constants and
    ident is the provider's id (an int) or, for GitHub, the raw url to fetch.
    ``(None, None)`` for an uploaded file or anything unrecognised -- an
    uploaded ``.cbl`` has no source to go back to and is not syncable.
    """
    if not source:
        return None, None

    if _is_github_url(source):
        return GITHUB, _convert_github_blob_to_raw(source)

    match = _PROVIDER_SOURCE_RE.match(source.strip())
    if not match:
        return None, None

    provider = match.group(1).lower()
    kind = match.group(2).lower()
    ident = int(match.group(3))
    if provider == "metron":
        return (METRON_LIST if kind == "reading-list" else METRON_ARC), ident
    if provider == "comicvine" and kind == "arc":
        return COMICVINE_ARC, ident
    return None, None


def is_syncable(source):
    """True when ``source`` names something this module can re-check."""
    kind, _ = parse_source(source)
    return kind is not None


# ---------------------------------------------------------------------------
# Change tokens
# ---------------------------------------------------------------------------


def _fingerprint(ids):
    """A stable digest of a set of provider ids.

    Sorted, so the provider reordering its response is not a change; a set, so
    a duplicated id is not either. Only membership counts.
    """
    clean = sorted({str(i) for i in (ids or []) if i not in (None, "")})
    return hashlib.sha256(",".join(clean).encode()).hexdigest()


def github_token(content):
    """Token for a GitHub CBL: the content hash, the same value ``source_hash`` held."""
    return hashlib.sha256((content or "").encode()).hexdigest()


def metron_list_token(detail):
    """Token for a Metron reading list: its own ``modified`` timestamp.

    Items belong to the list, so Metron bumps the list when its contents
    change -- unlike an arc, whose membership lives on the issues.
    """
    if not detail:
        return None
    modified = detail.get("modified")
    return str(modified) if modified else None


def metron_arc_token(issues):
    """Token for a Metron arc: a fingerprint of the issue ids it contains."""
    return _fingerprint([(i or {}).get("id") for i in (issues or [])])


def comicvine_arc_token(detail):
    """Token for a ComicVine arc: membership fingerprint plus ``date_last_updated``.

    The date alone is not enough -- CV does not reliably move it when an issue
    is associated -- and membership alone would miss an issue being renumbered,
    so both go in.
    """
    if not detail:
        return None
    ids = [(i or {}).get("id") for i in (detail.get("issues") or [])]
    return f"{_fingerprint(ids)}|{detail.get('date_last_updated') or ''}"


def _token_date(token):
    """The ``date`` a Metron token starts with, or None.

    Tokens are opaque everywhere except here, where ``sync_all`` needs a lower
    bound for its one ``modified_gt`` call. Anything unparseable -- a digest, a
    None, a format Metron changes to -- simply means "probe this one".
    """
    if not token:
        return None
    try:
        return datetime.strptime(str(token)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Description sanitising (shared with the import workers)
# ---------------------------------------------------------------------------


def _sanitize_html(html_str):
    """Strip HTML to only safe tags, removing attributes except href on <a>."""
    if not html_str:
        return html_str
    # Remove script/style blocks entirely
    cleaned = re_module.sub(r'<(script|style|iframe)[^>]*>.*?</\1>', '', html_str, flags=re_module.DOTALL | re_module.IGNORECASE)

    # Process remaining tags: keep allowed, strip others
    def _replace_tag(m):
        full = m.group(0)
        # Closing tag
        close_match = re_module.match(r'</(\w+)', full)
        if close_match:
            tag = close_match.group(1).lower()
            return f'</{tag}>' if tag in _SAFE_TAGS else ''
        # Opening/self-closing tag
        open_match = re_module.match(r'<(\w+)', full)
        if not open_match:
            return ''
        tag = open_match.group(1).lower()
        if tag not in _SAFE_TAGS:
            return ''
        # Keep href for <a>, strip all other attributes
        if tag == 'a':
            href = re_module.search(r'href=["\']([^"\']*)["\']', full, re_module.IGNORECASE)
            if href:
                url = href.group(1)
                # Convert relative ComicVine paths to absolute URLs
                if url.startswith('/') and not url.startswith('//'):
                    url = 'https://comicvine.gamespot.com' + url
                return f'<a href="{url}" target="_blank" rel="noopener">'
            return '<a>'
        return f'<{tag}>' if not full.endswith('/>') else f'<{tag}/>'

    return re_module.sub(r'<[^>]+>', _replace_tag, cleaned)


# ---------------------------------------------------------------------------
# Entry building -- shared with the import workers so the two cannot drift
# ---------------------------------------------------------------------------


def _metron_year_hints(issue, series_info):
    """Split a Metron issue's dates into the two years matching needs.

    Metron gives the series' ``year_began`` -- the year the RUN started, which
    is what the site displays as "Batman (2025)" -- and, per issue, both a
    ``cover_date`` and a ``store_date``. Those are different years far more
    often than they look: an issue shipping in November under a January cover
    date is honestly labelled either way, so both are kept and a file matching
    either one is accepted.

    Conflating the two is the bug this exists to prevent. ``year_began`` alone
    was being compared against a file's year, which for issue #14 of a 2025
    series is 2026 and never matched -- leaving nothing to reject
    "Batman 014 (1942)" with.
    """
    volume_year = year_of(series_info.get('year_began'))
    cover_year = year_of(issue.get('cover_date'))
    store_year = year_of(issue.get('store_date'))
    issue_years = {y for y in (cover_year, store_year) if y}
    return {
        'volume_year': volume_year,
        'issue_years': issue_years,
        # One representative year for display and for the source-search box.
        'issue_year': cover_year or store_year,
    }


def metron_issue_to_entry(issue, loader):
    """Build one reading-list entry from a Metron issue dict.

    Used by the Metron importers *and* by this module's sync, so a field added
    to one is added to both. ``issue`` is a ``BaseIssue`` dict as the arc
    endpoint returns it; ``process_metron_import`` unwraps ``item['issue']``
    before calling.
    """
    series_info = issue.get('series', {}) or {}
    series_name = series_info.get('name', '')
    issue_number = str(issue.get('number', '') or '')
    volume = series_info.get('volume')
    hints = _metron_year_hints(issue, series_info)

    matched_path = loader.match_file(
        series_name, issue_number,
        volume_year=hints['volume_year'],
        issue_years=hints['issue_years'],
        metron_id=issue.get('id'),
    )

    return {
        'series': series_name,
        'issue_number': str(issue_number) if issue_number else '',
        'volume': str(volume) if volume else None,
        'year': hints['volume_year'],
        'issue_year': hints['issue_year'],
        'metron_id': issue.get('id'),
        'matched_file_path': matched_path,
    }


def comicvine_issue_to_entry(issue, loader):
    """Build one reading-list entry from a resolved ComicVine arc issue."""
    series_name = issue.get('series_name', '')
    issue_number = issue.get('issue_number', '')
    volume_year = year_of(issue.get('volume_year'))
    issue_year = year_of(issue.get('cover_date'))

    matched_path = loader.match_file(
        series_name, issue_number,
        volume_year=volume_year,
        issue_years={issue_year} if issue_year else None,
    )

    return {
        'series': series_name,
        'issue_number': str(issue_number) if issue_number else '',
        # ComicVine's volume id is an identifier, not a volume year;
        # it has no place in a column the CBL export writes as Volume.
        'volume': None,
        'year': volume_year,
        'issue_year': issue_year,
        'matched_file_path': matched_path,
    }


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


class ProbeResult:
    """What one cheap look at the source found.

    ``payload`` carries whatever the probe already fetched, so ``apply`` never
    pays for the same call twice.
    """

    __slots__ = ("kind", "ident", "changed", "token", "stored", "payload", "error")

    def __init__(self, kind=None, ident=None, changed=False, token=None,
                 stored=None, payload=None, error=None):
        self.kind = kind
        self.ident = ident
        self.changed = changed
        self.token = token
        self.stored = stored
        self.payload = payload or {}
        self.error = error

    @property
    def ok(self):
        return self.error is None

    def __repr__(self):  # pragma: no cover - debugging aid
        return (f"ProbeResult(kind={self.kind!r}, changed={self.changed}, "
                f"error={self.error!r})")


def stored_token(row):
    """The token last written for a list.

    Falls back to ``source_hash`` so the GitHub lists that existed before
    ``source_version`` do not all report "changed" on the first sweep. Nothing
    writes ``source_hash`` for comparison any more; one column wins from here.
    """
    return row.get("source_version") or row.get("source_hash")


def probe(row, force=False, app=None):
    """Ask the source whether this list has moved, in one request.

    Returns a :class:`ProbeResult`. ``force`` still performs the probe -- the
    token it yields is what gets stored -- but reports ``changed`` regardless.
    """
    kind, ident = parse_source(row.get("source"))
    if not kind:
        return ProbeResult(error="This list has no source that can be synced")

    stored = stored_token(row)
    try:
        if kind == GITHUB:
            resp = requests.get(ident, timeout=30)
            resp.raise_for_status()
            content = resp.text
            token = github_token(content)
            payload = {"content": content, "filename": ident.split("/")[-1]}

        elif kind == METRON_LIST:
            api = _metron_api(app)
            if not api:
                return ProbeResult(kind=kind, ident=ident,
                                   error="Metron is not configured")
            from models.metron import fetch_reading_list_detail
            detail = fetch_reading_list_detail(api, ident)
            if not detail:
                return ProbeResult(kind=kind, ident=ident,
                                   error=f"Failed to fetch Metron reading list {ident}")
            token = metron_list_token(detail)
            payload = {"api": api, "detail": detail}

        elif kind == METRON_ARC:
            api = _metron_api(app)
            if not api:
                return ProbeResult(kind=kind, ident=ident,
                                   error="Metron is not configured")
            from models.metron import fetch_arc_issues
            issues = fetch_arc_issues(api, ident)
            token = metron_arc_token(issues)
            payload = {"api": api, "issues": issues}

        elif kind == COMICVINE_ARC:
            from models.comicvine import fetch_cv_arc_detail, get_cv_api_key
            api_key = get_cv_api_key(app)
            if not api_key:
                return ProbeResult(kind=kind, ident=ident,
                                   error="ComicVine is not configured")
            detail = fetch_cv_arc_detail(api_key, ident)
            if not detail:
                return ProbeResult(kind=kind, ident=ident,
                                   error=f"Failed to fetch ComicVine arc {ident}")
            token = comicvine_arc_token(detail)
            payload = {"api_key": api_key, "detail": detail}

        else:  # pragma: no cover - parse_source cannot produce this
            return ProbeResult(error="This list has no source that can be synced")

    except Exception as e:
        app_logger.error(f"Reading list probe failed for '{row.get('name')}': {e}")
        return ProbeResult(kind=kind, ident=ident, error=str(e))

    return ProbeResult(
        kind=kind, ident=ident, token=token, stored=stored, payload=payload,
        changed=bool(force or (token and token != stored)),
    )


def _metron_api(app=None):
    """A Metron client, or None when Metron is unconfigured or locked out.

    ``is_metron_configured`` already returns False while the auth lockout is
    latched, so a sweep on a rejected credential costs nothing.
    """
    from models.metron import get_flask_api, is_metron_configured
    if not is_metron_configured(app):
        return None
    return get_flask_api(app)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _loader(rename_pattern):
    """A throwaway CBLLoader, borrowed purely for ``match_file``."""
    from models.cbl import CBLLoader
    return CBLLoader(
        "<ReadingList><Name>x</Name><Books/></ReadingList>",
        rename_pattern=rename_pattern or '{series_name} {issue_number}',
    )


def build_entries(result, rename_pattern, progress_cb=None):
    """Turn a probe's payload into the list of entries the source now holds.

    This is the expensive half: every entry is matched against the library, and
    for ComicVine the issues have to be resolved one request at a time first.
    """
    kind = result.kind

    if kind == GITHUB:
        from models.cbl import CBLLoader
        cbl = CBLLoader(result.payload["content"],
                        filename=result.payload.get("filename"),
                        rename_pattern=rename_pattern or '{series_name} {issue_number}')
        entries = cbl.parse_entries()
        total = len(entries)
        for i, entry in enumerate(entries):
            entry['matched_file_path'] = cbl.match_file(
                entry['series'], entry['issue_number'], entry['volume'], entry['year']
            )
            _report(progress_cb, i + 1, total,
                    f"{entry.get('series', '')} #{entry.get('issue_number', '')}")
        return entries

    if kind == METRON_LIST:
        loader = _loader(rename_pattern)
        from models.metron import fetch_reading_list_items
        items = fetch_reading_list_items(result.payload["api"], result.ident)
        items.sort(key=lambda x: x.get('order', 0))
        issues = [(item.get('issue') or {}) for item in items]
        return _metron_entries(issues, loader, progress_cb)

    if kind == METRON_ARC:
        # The probe already listed them; arc issues arrive as BaseIssue dicts.
        return _metron_entries(result.payload.get("issues") or [],
                               _loader(rename_pattern), progress_cb)

    if kind == COMICVINE_ARC:
        loader = _loader(rename_pattern)
        from models.comicvine import fetch_cv_arc_issues
        issues = fetch_cv_arc_issues(result.payload["api_key"], result.ident)
        total = len(issues)
        entries = []
        for i, issue in enumerate(issues):
            entry = comicvine_issue_to_entry(issue, loader)
            entries.append(entry)
            _report(progress_cb, i + 1, total,
                    f"{entry['series']} #{entry['issue_number']}")
        return entries

    return []  # pragma: no cover - guarded by probe()


def _metron_entries(issues, loader, progress_cb):
    """Match a Metron issue list to the library, ids first."""
    loader.prefetch_metron_ids([(i or {}).get('id') for i in issues])
    total = len(issues)
    entries = []
    for i, issue in enumerate(issues):
        entry = metron_issue_to_entry(issue, loader)
        entries.append(entry)
        _report(progress_cb, i + 1, total,
                f"{entry['series']} #{entry['issue_number']}")
    return entries


def _report(progress_cb, current, total, detail):
    if not progress_cb:
        return
    try:
        progress_cb(current, total, detail)
    except Exception as e:  # a broken progress sink must not fail a sync
        app_logger.debug(f"Reading list sync progress callback failed: {e}")


def apply(row, result, rename_pattern, progress_cb=None):
    """Diff the source's current contents into the stored list.

    Entries are diffed, not rebuilt: ``sync_reading_list_entries`` adds what is
    new, drops what is gone, re-applies the source's order, and leaves any
    entry the user hand-mapped alone. The list's **name** is deliberately not
    touched -- the user may have renamed it -- but the description is refreshed.
    """
    from core.database import (
        sync_reading_list_entries,
        update_reading_list_description,
        update_reading_list_source_version,
    )

    entries = build_entries(result, rename_pattern, progress_cb)
    outcome = sync_reading_list_entries(row["id"], entries)
    if outcome is None:
        return None

    detail = result.payload.get("detail") or {}
    description = detail.get('desc') or detail.get('description')
    if description:
        update_reading_list_description(row["id"], _sanitize_html(description))

    update_reading_list_source_version(row["id"], result.token)
    return outcome


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def sync_one(list_id, rename_pattern=None, force=False, app=None,
             progress_cb=None, row=None, result=None):
    """Probe one list and, if it moved, re-diff it.

    ``row``/``result`` let a caller that already probed (the route does, so it
    can answer "no changes" without spawning a thread) hand the work straight
    to the apply half.
    """
    from core.database import get_reading_list

    if row is None:
        row = get_reading_list(list_id)
    if not row:
        return {"success": False, "message": "Reading list not found"}

    if result is None:
        result = probe(row, force=force, app=app)
    if not result.ok:
        return {"success": False, "message": result.error}

    if not result.changed:
        # Still stamp the token: a list imported before this column existed has
        # none, and one probe is enough to learn it.
        if result.token and result.token != result.stored:
            from core.database import update_reading_list_source_version
            update_reading_list_source_version(row["id"], result.token)
        return {"success": True, "changed": False, "message": "No changes detected"}

    outcome = apply(row, result, rename_pattern, progress_cb)
    if outcome is None:
        return {"success": False, "message": "Failed to sync entries"}

    return {
        "success": True,
        "changed": True,
        "added": outcome["added"],
        "removed": outcome["removed"],
        "message": f"Synced: {outcome['added']} added, {outcome['removed']} removed",
    }


def _metron_bulk_prefilter(rows, app=None):
    """One ``modified_gt`` call that says which Metron lists have *not* moved.

    This is the whole reason a Metron reading list stores a timestamp rather
    than a digest: the sweep asks Metron once for everything changed since the
    oldest list it holds, instead of making a detail request per list.

    Returns the set of Metron ids that can be skipped outright. A list is only
    ever added to it on positive evidence, so any failure here -- an empty
    answer that might have been an error, a token in a format this cannot read,
    a list older than the clamped window -- costs requests, never accuracy.
    """
    dated = {}
    stored = {}
    for row in rows:
        _, ident = parse_source(row.get("source"))
        token = stored_token(row)
        stored[ident] = token
        when = _token_date(token)
        if when:
            dated[ident] = when

    # A list with no readable timestamp (never synced, or a digest from some
    # earlier scheme) gives the call no lower bound to stand on.
    if not dated:
        return set()

    floor = date.today() - timedelta(days=MAX_BULK_LOOKBACK_DAYS)
    # Metron's modified_gt is date-granular and *exclusive*, so a list modified
    # later on the day it was synced would be invisible without the extra day.
    since = max(min(dated.values()) - timedelta(days=1), floor)

    # Anything stamped before the clamped window is outside what this call can
    # speak for, so it has to be asked about directly.
    answerable = {ident: when for ident, when in dated.items() if when >= since}
    if not answerable:
        return set()

    api = _metron_api(app)
    if not api:
        return set()

    from models.metron import list_reading_lists_modified_since
    changed = list_reading_lists_modified_since(api, since.strftime("%Y-%m-%d"))
    if changed is None:
        # The call failed. An empty dict is different -- that is Metron saying
        # nothing changed, which is exactly the answer worth acting on.
        return set()

    skip = set()
    for ident in answerable:
        reported = changed.get(int(ident))
        if reported is None or reported == stored.get(ident):
            skip.add(ident)
    return skip


def sync_all(rename_pattern=None, app=None, progress_cb=None):
    """Re-check every syncable list. The nightly job and nothing else.

    Metron reading lists are pre-filtered with a single ``modified_gt`` call;
    every other source falls through to its own cheap probe.
    """
    from core.database import get_syncable_reading_lists

    rows = [r for r in (get_syncable_reading_lists() or [])
            if is_syncable(r.get("source"))]
    if not rows:
        return {"checked": 0, "synced": 0, "unchanged": 0, "failed": 0}

    metron_rows = [r for r in rows if parse_source(r.get("source"))[0] == METRON_LIST]
    skip = set()
    if metron_rows:
        try:
            skip = _metron_bulk_prefilter(metron_rows, app=app)
            if skip:
                app_logger.info(
                    f"Metron reports {len(skip)} of {len(metron_rows)} imported "
                    f"reading list(s) unchanged; skipping their probes"
                )
        except Exception as e:
            app_logger.warning(f"Metron bulk change check failed, probing each list: {e}")
            skip = set()

    checked = synced = unchanged = failed = 0
    for row in rows:
        kind, ident = parse_source(row.get("source"))
        if kind == METRON_LIST and ident in skip:
            unchanged += 1
            continue
        checked += 1
        try:
            outcome = sync_one(row["id"], rename_pattern=rename_pattern, app=app, row=row)
            if not outcome.get("success"):
                failed += 1
                app_logger.error(
                    f"Error syncing reading list '{row.get('name')}': {outcome.get('message')}"
                )
            elif outcome.get("changed"):
                synced += 1
                app_logger.info(
                    f"Synced reading list '{row.get('name')}': "
                    f"{outcome['added']} added, {outcome['removed']} removed"
                )
            else:
                unchanged += 1
        except Exception as e:
            failed += 1
            app_logger.error(f"Error syncing reading list '{row.get('name')}': {e}")
        _report(progress_cb, checked, len(rows), row.get("name"))

    app_logger.info(
        f"Reading list sync complete: {synced} updated, {unchanged} unchanged, "
        f"{failed} failed"
    )
    return {"checked": checked, "synced": synced, "unchanged": unchanged, "failed": failed}
