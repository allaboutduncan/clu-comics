---
paths:
  - "core/reading_list_sync.py"
  - "core/reading_list_match.py"
  - "core/wanted_reading_lists.py"
  - "routes/reading_lists.py"
  - "models/cbl.py"
  - "models/metron.py"
  - "models/comicvine.py"
  - "static/js/reading_list.js"
  - "app.py"
  - "core/database.py"
---

# Reading list sync and reading-list gaps on the Wanted list

### Reading List Sync

An imported reading list used to be a **snapshot**. Nothing went back to the
provider, and re-running an import produced a *second* list, because nothing
looks a list up by its `source`. Sync existed but covered GitHub CBLs only.

`core/reading_list_sync.py` now handles all four sources with one shape:

    probe(row)  -- one cheap request that yields a change token
    apply(row)  -- the expensive rebuild, run only when the token moved

`reading_lists.source_version` holds that token. **It is opaque — compare it,
never parse it** (`_token_date` is the single exception and tolerates anything).
One column serves every provider:

| Source | Probe (1 call) | Token |
|--------|----------------|-------|
| a GitHub CBL url | GET the raw file | sha256 of the content |
| `metron://reading-list/<id>` | `api.reading_list` | the list's ISO `modified` |
| `metron://arc/<id>` | `api.arc_issues_list` | fingerprint of the issue ids |
| `comicvine://arc/<id>` | `cv.get_story_arc` | fingerprint + `date_last_updated` |

Things that look arbitrary and are not:

- **The Metron probes purge mokkari's cache first** (`purge_reading_list_cache`,
  `purge_arc_issues_cache`). Its 1-day TTL would otherwise answer "no changes"
  for a list that moved, and for a reading list `apply` could pair fresh detail
  with stale items and stamp the token anyway.
- **An arc cannot use `modified`.** An arc's own `modified` advances when the
  arc *record* is edited; adding an issue to an arc modifies the **issue**. So
  an arc is fingerprinted by its membership, read from a call the sync has to
  make anyway. Only a Metron *reading list* can use a timestamp — its items
  belong to it.
- **That timestamp is what makes the sweep cheap.** `sync_all` pre-filters
  every Metron reading list with a *single* `modified_gt` call
  (`models.metron.list_reading_lists_modified_since`) instead of a detail
  request each. Metron's filter is **date-granular and exclusive**, so the
  lower bound steps back a day or a list edited later on the day it synced is
  invisible. mokkari follows every `next` link inside that one call and does so
  *below* the pacer, so the window is clamped to `MAX_BULK_LOOKBACK_DAYS`;
  anything older is probed individually.
- **`list_reading_lists_modified_since` returns `None` on failure and `{}` for
  "nothing changed", and the caller acts on the difference.** Collapsing the
  two would make every failed call look like proof that nothing moved, and the
  sweep would skip every list. A list is only ever skipped on positive evidence.
- **The ComicVine probe is where the real saving is.** `fetch_cv_arc_issues`
  makes one request per issue in the arc. `get_story_arc` already returns the
  issue ids, so an unchanged CV arc costs one request instead of N.
- **`stored_token()` falls back to `source_hash`** when `source_version` is
  NULL — that is exactly the set of GitHub rows that predate the column, so
  there is nothing to backfill and no first-sweep stampede.
- **A failed diff must not stamp the token.** `apply` writes `source_version`
  only after `sync_reading_list_entries` succeeds; stamping early would make
  the next sweep skip a list that was never actually updated.

`POST /api/reading-lists/<id>/sync` **probes in the request and applies in a
thread**. The probe is one call and answering "no changes" instantly is worth
more than a task id; the rebuild is not — a ComicVine arc would outlast
gunicorn's 120s timeout. So the response is either
`{changed: false}` or `{changed: true, background: true, task_id}`, and
`static/js/reading_list.js` handles both. `{"force": true}` re-syncs a list
whose token has not moved.

`app.scheduled_reading_list_sync` is a **wrapper** over `sync_all` and must stay
one — it used to carry a verbatim copy of the GitHub sync body, which is
precisely why it never covered anything else. `tests/unit/test_reading_list_scheduled_sync.py`
asserts that structurally, because app.py cannot be imported in tests. It reuses
the existing `reading_list_sync` schedule, job id and settings UI.

`_sanitize_html`, `_is_github_url`, `_convert_github_blob_to_raw`,
`_metron_year_hints` and the per-provider entry builders live in
`core/reading_list_sync.py` and are imported into `routes/reading_lists.py`
under their original names, so the import workers and the sync build entries
through the *same* function and cannot drift.

### Reading-List Matching

`CBLLoader.match_file` (`models/cbl.py`) tries identity before names:
Metron issue id → ComicVine issue id → ComicVine volume id + issue number →
ComicInfo metadata → filename. Every importer, the sync and the Re-match
button pass an entry's ids through `CBLLoader.entry_ids(entry)` and prefetch
them with `prefetch_ids(entries)`.

- **A CBL's `<Database Name="cv" Series= Issue=>` is kept**, in
  `reading_list_entries.cv_volume_id` / `cv_issue_id`. Lists imported before
  those columns existed get them on their next sync: `sync_reading_list_entries`
  fills ids on *kept* entries with `COALESCE`, never overwriting one.
- **`file_index.ci_cvid`** is the ComicVine issue id, read from ComicInfo
  `Web`/`Notes` by `core.comicinfo.comicvine_issue_id`. Same contract as
  `ci_metronid` (NULL unread, 0 none) and the same idle backfill query. A note
  saying "Issue ID: N" counts only when `provider_from_notes` says ComicVine
  wrote the Notes -- a ComicVine *mention* anywhere in the text is not
  provenance -- and the id is read with `extract_issue_id`, the one Notes→id
  parser (the metadata backfill re-exports it). ComicTagger's own Notes are
  not trusted; its `Web` URL is.
- **The volume tier reads `cvinfo`** (`cv_volume_folders`, cached 5 min)
  through `models.comicvine.find_cvinfo_in_folder` / `parse_cvinfo_volume_id`
  (case-insensitive name, one parser). It is not in `file_index`, so the map
  costs one small read per indexed folder; it is built only when an entry has
  a volume id and the issue-id tier missed. **Readers never wait on a
  rebuild** while a previous map exists -- only one thread builds, the rest
  keep the old map -- and a failed folder read (`list_comic_dirs()` → None) is
  never cached. The tier is name-blind, so it **skips a file whose name or
  `ci_series` names a publication type the entry's series does not**
  (`names_other_publication_type`): "Batman (2016)/" holds "Batman Annual
  001", whose `<Number>` is 1 too.
- **Issue numbers compare through `normalize_issue_number`, never SQL `CAST`.**
  `CAST('50.LR' AS REAL)` is 50, which is how every `#50.LR` entry matched
  `#050`. The sign is part of the number (`-1` is never `1`). The filename tier
  uses `filename_issue_matches`: the series must start the name
  (punctuation-blind) and **the first issue token after it** must equal the
  wanted one -- the issue-badge rule: an explicit `#` token wins, else the
  first bare number. "(of 9)", "Part 2" and a subtitle number come after the
  issue; a "Vol. 2"/"v2" marker is skipped; a `(...)` number is a year or a
  duplicate counter. A leading `-` is a sign only when the hyphen is not inside
  a word ("X-23").
- **Series names are matched punctuation-blind.** `search_by_comic_metadata`
  turns every run of non-word characters into a `LIKE` wildcard: normalising
  the column in SQL left `": "` as a double space no single-spaced needle could
  match, so every series with a colon failed. **Both sides go through SQLite's
  `LOWER()`**, which folds ASCII only -- lowercasing the needle in Python would
  stop "Élektra" matching. The numeric arm keeps its `GLOB '[0-9]*'` guard (a
  blank `<Number>` casts to 0), rows come back exact name first, then shortest,
  and **`limit` applies after the exact number check**: "%X%Men%" #1 also
  returns every Uncanny/All-New X-Men #1.
- **The filename tier stops at the first search round with a passing
  candidate.** Each search is a full `file_index` LIKE scan, run per entry and
  inline on `/api/scan-downloads` via `fill_gaps_for_series`; it widens (word
  searches, the series-only search, the article-dropped retry) only while
  nothing has passed. It also skips publication types the entry is not.
  The Map Issue search adds a word search (`search_file_index_all_words` on
  `\w+` words) because a file can keep a `:` that the search term spells ` -`.
- **The Map Issue modal sends the entry's hints** (`series`, `number`,
  `volume`, `year`, `issue_year`) to `/api/reading-lists/search-file`, which
  reads the years with `CBLLoader.entry_year_hints` -- `year` is the issue's
  year in a CBL but the volume's start year in a Metron/ComicVine import --
  ranks with the matcher's own checks, and tags each row
  `fit: match | year_mismatch | null`. Every path and name it renders goes
  through `CLU.escapeHtml`, so every page loading `reading_list.js` loads
  `clu-utils.js` first.

Tests: `tests/unit/test_cbl_matching_ids_and_names.py`,
`tests/integration/test_reading_list_id_matching.py`.

### Reading-List Gaps on the Wanted List

An unmatched reading-list entry is a wanted issue: it shows on the Wanted page
and the nightly GetComics sweep searches for it. Opt-in per list
(`reading_lists.track_wanted`, default OFF — a 300-issue arc import must not
silently start 300 searches).

**Nothing is stored.** `core/wanted_reading_lists.py` derives the set from
`matched_file_path IS NULL AND manual_override_path IS NULL`, which is already
exactly what "unmatched" means. That is why there is no hook in
`add_reading_list_entry`, `sync_reading_list_entries`,
`update_reading_list_entry_match`, `delete_reading_list_entry` or
`delete_reading_list` — mapping an issue by hand removes it and clearing the
mapping brings it back, for free. **Do not add a table here.** `wanted_issues`
in particular cannot hold these rows: it is keyed `series_id`/`issue_id` (Metron
ids, NOT NULL) and `refresh_wanted_cache_background` opens by wiping it.

Things that look arbitrary and are not:

- **`series_volume` is always `None` on a reading-list work item.**
  `reading_list_entries.volume` is heterogeneous — CBL writes its `<Volume>`
  element, which is a *year*; the Metron importer writes a volume *number*;
  ComicVine writes NULL. Passing it to `score_getcomics_result(series_volume=)`
  would fail the volume check against every result. `series_year` carries the
  year; `search_year` (computed once, in `get_reading_list_wanted_items`) is
  what both the page and the sweep narrow on, so they cannot disagree.
- **A NULL year means "released", not "skip"** — the opposite of the
  mapped-series rule. That rule is right for a release calendar, where an
  undated row is an unscheduled solicitation. A reading list is back catalogue,
  and `issue_year` is NULL on every row imported before that column existed, so
  skipping NULL would make the feature do nothing for the lists people import
  most.
- **Blank series or issue number is excluded.** `CBLLoader.match_file` returns
  None immediately for either, so such an entry can never match and would sit
  on the list being searched every night.
- **De-duplication does not use `issue_number_to_int`.** That returns None for
  `1.MU`, `Annual` and fractions, which would collapse every non-numeric issue
  of a series into one bucket and drop all but the first.

`app.scheduled_getcomics_download` collects **both** sources into one flat
`work_items` list and runs its ~390-line search/score/queue body once over it.
Two loops would mean two copies of that body. The reading-list source is
guarded by `only_series_id is None` — a scoped run is an explicit request for
one series.

> **The download loop must stay closed.** A reading-list entry has no
> `mapped_path`, so `process_incoming_wanted_issues` cannot file a finished
> download back onto it — the entry stays unmatched and would be re-queued
> every night forever. Two things prevent that, and both are needed:
> `core.reading_list_match.rematch_tracked_lists()` runs **before** the wanted
> set is decided (picking up whatever the WATCH/TARGET pipeline has since filed
> into the library), and `_mark_queued()` stamps `last_queued_at` at all
> **three** queue sites — pre-source submit, the GetComics `download_queue.put`,
> and the post-source fallback — holding the entry off for
> `QUEUE_COOLDOWN_DAYS` when a re-match never closes it. The cooldown is
> sweep-only: a queued issue is still missing, so it keeps showing on the page.
> All of this is asserted structurally in
> `tests/unit/test_series_scoped_getcomics_run.py`, because app.py cannot be
> imported in tests.

The matching loop lives in `core/reading_list_match.py` precisely so the Re-match
button and the sweep share it. `rematch_tracked_lists` takes the rename pattern
as an argument: the sweep is an APScheduler job with no application context, so
reading it through `current_app` would raise, be swallowed, and silently match
against a pattern the user does not use.

Deleting a comic clears the entry's match, which is how the issue comes back
onto this list — still nothing stored, because "unmatched" is already derived
from the two path columns being NULL. See **Path References** below.

#### Closing a gap the moment the file arrives

The release gate above is right for the sweep and useless for the entry that
needs help most. A future-dated entry is *never* part of the sweep's work, so
when the issue finally ships — arriving as an ordinary **mapped-series** wanted
issue, filed by `app.process_incoming_wanted_issues` — nothing tells the entry
about it. `core.reading_list_match.fill_gaps_for_series` is the pass hooked onto
that arrival, in the `if moved_count > 0` branch, beside the reconcile and the
digest.

- **It is gap-only, and that is the whole safety argument.** It hands
  `rematch_entries` only entries that are already unmatched, so that loop can
  write a path but can never clear one — clearing a match is a decision for the
  nightly sweep and the explicit Re-match button, not for a file arriving.
  `unmatched_tracked_entries()` is what enforces it; do not widen it to a full
  re-match.
- **It is scoped to the series that just moved** (`moved_series_names`, compared
  through `core.wanted_reading_lists._norm_series`). `process_incoming_wanted_issues`
  runs **inline on the request thread** for `POST /api/scan-downloads`, and each
  gap costs up to ~8 LIKE scans over `file_index`. A name that does not
  normalise onto an entry is simply not covered here — the nightly
  `rematch_tracked_lists` still catches it. `MAX_GAP_ENTRIES_PER_PASS` and the
  non-blocking `_gap_lock` (two callers drive a TARGET sweep) bound the rest.
- **No other mover is hooked.** `core.problem_replacements._swap` replaces a
  damaged file *at its existing path*, so an entry matched to it stays matched;
  `update_index_on_move` scenario 1 is WATCH/TEMP → `/data`, not TARGET.
- `_resolve_rename_pattern` is shared with `rematch_tracked_lists` for the
  reason given above — neither caller has an application context.

Asserted structurally in `tests/unit/test_target_move_gap_hook.py`, because
app.py cannot be imported in tests.
