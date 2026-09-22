# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Comic Library Utilities (CLU) is a Flask-based web application for managing comic book collections. It provides bulk operations for CBZ/CBR files, metadata editing, file renaming, format conversion, and folder monitoring. Designed to run in Docker, it integrates with comic databases (GCD, ComicVine, Metron) for metadata enrichment.

## Development Commands

```bash
# Run locally (development)
python app.py

# Run with Docker
docker build -t comic-utils .
docker run -p 5577:5577 -v /path/to/comics:/data -v /path/to/downloads:/downloads comic-utils

# Verify Python syntax
python -m py_compile <filename.py>

# Production server (used in Docker)
gunicorn -w 1 --threads 8 -b 0.0.0.0:5577 --timeout 120 app:app
```

## Architecture

### Core Application Flow
- **`api.py`**: Creates the Flask app instance and handles download queue/remote downloads
- **`app.py`**: Main application - imports Flask app from `api.py`, registers blueprints, defines all routes and API endpoints
- **`monitor.py`**: Standalone file watcher for folder monitoring (runs when `MONITOR=yes`)

### Core Modules (`core/`)
| Module | Purpose |
|--------|---------|
| `core/config.py` | ConfigParser-based settings from `/config/config.ini` |
| `core/database.py` | SQLite database (`comic_utils.db`) for caching, file index, reading history |
| `core/comicinfo.py` | ComicInfo.xml parsing and generation |
| `core/app_logging.py` | Centralized logging — `app_logger` and `monitor_logger`, log files in `CONFIG_DIR/logs` |
| `core/app_state.py` | Global state — APScheduler instance, wanted-issues refresh state, data-dir stats cache |
| `core/file_watcher.py` | DebouncedFileHandler for `/data` monitoring — detects changes, queues metadata scanning |
| `core/metadata_scanner.py` | Background worker scanning ComicInfo.xml — priority queue, updates file_index with metadata |
| `core/memory_utils.py` | Memory monitoring — tracks usage, triggers cleanup at thresholds, `memory_context()` manager |
| `core/version.py` | Single `__version__` string |
| `core/problem_replacements.py` | Files a downloaded replacement onto the damaged file it replaces — claim, verify, stage-then-swap, trash the old copy. See **Replacing a damaged file from its own page** |
| `core/problem_files.py` | The damaged-file worklist behind `/problem-files` — one row per `(path, source)` failure, its plain-English classification, and the retry dispatch. A ledger: rows are deleted once the file processes cleanly. See **Problem Files** below |
| `core/thumbnail_cache.py` | Per-comic thumbnail cache — the cache path, the permission-safe atomic write, regeneration and invalidation. Every mutating op owes it one call. See **Per-Comic Thumbnail Cache** below |
| `core/folder_thumbnails.py` | Folder cover art — cover selection, the four style composers (`STYLES`), and the background auto-generation queue. See **Folder Thumbnails** below |
| `core/reading_list_sync.py` | Re-checks an imported reading list against its source (GitHub CBL, Metron list, Metron arc, ComicVine arc). Probe/apply split, one opaque change token per list. See **Reading List Sync** below |
| `core/db_health.py` | Corruption detection, the in-memory error ledger and the storage diagnostic. See **Database Health** below |
| `core/db_maintenance.py` | Checkpoint, compact (`VACUUM INTO`), optimize, page accounting, and the shutdown checkpoint |
| `core/db_repair.py` | Salvage orchestration over `tools/repair_db.py` — candidate, row-count diff, guided swap |
| `core/db_lock.py` | Cross-process advisory lock serialising `init_db()` |
| `core/notifications.py` | Outbound push via Apprise - owner-global settings in `user_preferences`, event catalog (`EVENT_DEFS`), `notify_async()` used by every hook site. `apprise` is imported lazily and every path swallows its exceptions: a notification must never break the download it reports on |
| `core/comicvine_db_update.py` | Keeps the local ComicVine SQLite dump current from a public mirror — probe/apply split, download, verify, atomic swap. Holds the source URL, which must never reach the UI. See **Local ComicVine DB Auto-Update** below |

### Other Root Modules
| Module | Purpose |
|--------|---------|
| `rename.py` | Comic file renaming with regex patterns for volume/issue extraction |
| `edit.py` | CBZ editing - image manipulation, file reordering, cropping |
| `convert.py` | CBR to CBZ conversion using `unar` |
| `wrapped.py` | Yearly reading stats image generation (Spotify Wrapped style) |
| `helpers/` | Utility functions — `is_hidden()`, `safe_image_open()`, `create_thumbnail_streaming()`, `prune_empty_dirs()`, ZIP/RAR extraction, `move_file()` (see **A refused `utime`/`chmod` must not fail a completed move**). `helpers/library.py` owns the path-safety predicates: `get_protected_roots()` (automated sweeps) and `is_critical_path()` (interactive routes) |
| `recommendations.py` | AI-powered recommendations via OpenAI/Anthropic APIs |

### Models
| Module | Purpose |
|--------|---------|
| `models/metron.py` | Metron API via Mokkari — search, metadata fetch, rate-limit retry, scrobble |
| `models/comicvine.py` | ComicVine API via Simyan — volume/issue search, metadata mapping |
| `models/gcd.py` | Grand Comics Database — MySQL queries, fuzzy title matching |
| `models/komga.py` | Komga media server REST client — reading history, in-progress books |
| `models/getcomics.py` | GetComics.org scraper — cloudscraper-based search and download |
| `models/mega.py` | MEGA download support — URL parsing, AES-256 decryption |
| `models/stats.py` | Library statistics — file counts, disk usage, read stats (cached) |
| `models/timeline.py` | Reading timeline — groups history by date, filters by year/month |
| `models/cbl.py` | CBL (Comic Book List) XML parser — matches entries to collection files |
| `models/issue.py` | Data classes — `IssueObj` and `SeriesObj` for unified data representation |
| `models/update_xml.py` | Batch ComicInfo.xml field updater across CBZ files |
| `models/providers/` | Unified provider system — `BaseProvider` ABC, registry, adapters for Metron/ComicVine/GCD/AniList/MangaDex/Bedetheque |

### CBZ Operations
| Module | Purpose |
|--------|---------|
| `cbz_ops/add.py` | Insert blank images into CBZ files |
| `cbz_ops/delete.py` | Delete CBZ files from filesystem |
| `cbz_ops/convert.py` | CBR→CBZ conversion using `unar` |
| `cbz_ops/single_file.py` | Single RAR→CBZ conversion with progress reporting |
| `cbz_ops/edit.py` | CBZ editing — crop, reorder, extract covers |
| `cbz_ops/crop.py` | Cover image cropping — left/center/right/freeform with blur |
| `cbz_ops/remove.py` | Remove specific images from CBZ files |
| `cbz_ops/enhance_single.py` | Single image enhancement — contrast, brightness, blur |
| `cbz_ops/enhance_dir.py` | Batch directory image enhancement |
| `cbz_ops/rebuild.py` | Rebuild CBZ structure — normalize filenames, reorder images |
| `cbz_ops/pdf.py` | PDF→CBZ conversion via pdf2image |
| `cbz_ops/rename.py` | Comic file renaming with regex pattern matching |

### Routes
| Module | Purpose |
|--------|---------|
| `routes/downloads.py` | GetComics search/download (search is scored server-side via `score_getcomics_result`), auto-download schedules, weekly packs. `/api/series/<id>/check-missing` runs the scheduled sweep's own pass (`app.scheduled_getcomics_download`) against a single series — see **Scoped missing-issue checks** below |
| `routes/files.py` | File ops — rename, delete, move, crop, combine CBZ, upload, cleanup |
| `routes/collection.py` | File browsing — directory listing, search, thumbnails, metadata browse |
| `routes/metadata.py` | ComicInfo.xml management — provider search, batch processing, field updates. Also the whole `/api/providers/*` surface, including the local ComicVine DB auto-update endpoints (see **Local ComicVine DB Auto-Update**) — owner-only by path prefix via `core/auth.py` |
| `routes/series.py` | Releases/Wanted/Pull List — series sync, mapping, subscriptions |
| `routes/problem_files.py` | The owner-only Problem Files page and its API — list, retry, dismiss, remove, delete. Owner-gated by path via `core/auth.py` |
| `routes/notifications.py` | Notification settings - save, send-test, event catalog. Owner-only by path (`core/auth.py` gates all of `/api/config/`) |
| `routes/api_v1.py` | External API access for publishers, files and download support. 

!!! /api/v1/docs documents all token protected API routes. To keep the page in sync with the API, edit the ENDPOINTS list at
  routes/api_v1_docs.py:17 whenever a route changes — the test asserts the catalog stays complete.

### Test Organization
```
tests/
├── unit/          # Pure logic, no external deps
├── mocked/        # External APIs mocked
├── integration/   # Real SQLite database
├── routes/        # Flask route/endpoint tests
└── factories/     # Test data factories
```

### Blueprints
- `favorites_bp` (routes/favorites.py): Reading list/favorites functionality
- `opds_bp` (routes/opds.py): OPDS feed for comic readers
- `reading_lists_bp` (routes/reading_lists.py): Reading list management
- `downloads_bp` (routes/downloads.py): GetComics search and downloads
- `files_bp` (routes/files.py): File operations
- `collection_bp` (routes/collection.py): Collection browsing
- `metadata_bp` (routes/metadata.py): Metadata management
- `series_bp` (routes/series.py): Series and releases
- `notifications_bp` (routes/notifications.py): Apprise notification settings
- `database_bp` (routes/database.py): Database tab API — stats, backups, maintenance, salvage (owner-only by path prefix)

### Scoped Missing-Issue Checks

`app.scheduled_getcomics_download` is one body serving two callers: the nightly
sweep, and the "Check for Missing Issues" button on a series page
(`only_series_id=<id>`). The scoped run differs in three ways, all asserted in
`tests/unit/test_series_scoped_getcomics_run.py` because app.py cannot be
imported in tests:

- It narrows `mapped_series` to the one id.
- It **ignores the series' Monitor toggle.** That toggle exists to keep the
  *unattended* sweep off a series; clicking the button is an explicit request.
- It **must not call `update_last_getcomics_run()`** — that stamp belongs to the
  schedule, and moving it would make the nightly sweep look as though it had
  already run.

The scope parameter is `only_series_id`, not `series_id`, because the per-series
loop rebinds `series_id` to the series it is processing — a parameter of that
name is shadowed at the first iteration and the scoping silently lost.

Progress goes through the operations registry (`core/app_state.py`), so the run
shows up in the header indicator like any other background job. Pages that
follow an operation they started poll **`/api/operation/<op_id>`**, never
`/api/operations`: the latter *clears* the pending notification queue as a side
effect, so it can only ever have the one poller in `base.html`. The sweep
registers its own `op_id` when the caller supplies none, so the cron job and
`/api/run-getcomics-now` are visible too — a run that can last hours used to be
invisible unless it came from the per-series button.

#### One sweep per scope, and a ceiling on what one sweep may queue

**Every de-duplication the sweep does is in a local.** `queued_download_urls`
and `downloaded_ranges` die with the call, so a second concurrent sweep starts
with both empty and re-queues everything the first is still working through.
Nothing prevented that: one reported run took **9,408 s (2h37m)** — long enough
for the nightly cron to fire on top of it — and `/api/run-getcomics-now` had no
in-progress check at all.

`core.app_state` holds the claims (**not** app.py, so the routes can ask without
importing app). The key comes from `getcomics_sweep_scope(only_series_id)`:
`"all"` for a full sweep, `"series:<id>"` for a scoped run.

- **A full sweep does not block the per-series button.** Clicking it is an
  explicit request about one series, and making the user wait out a sweep with
  hours left is worse than the single duplicate it can cost. Two runs over the
  *same* scope are what must never overlap.
- **A dry run neither claims nor blocks.** The simulation queues nothing.
- **The claim is released in a `finally`**, or one failed sweep locks its scope
  for the life of the process.
- Both routes answer **409** rather than starting a run the sweep would
  immediately stand down from.

`MAX_DOWNLOADS_PER_SWEEP` is a blast-radius limit, not a throughput limit: a run
that goes wrong goes wrong for its whole length, and the reported sweep queued
203 downloads of which a handful of files accounted for most. Hitting it stops
the run; the rest is picked up next time.

> **A mapped-series issue now carries a queue stamp, like a reading-list entry.**
> `wanted_queue_log(series_id, issue_number)` and `QUEUE_COOLDOWN_DAYS` (shared
> with `core.wanted_reading_lists` — one cooldown, not two knobs meaning the
> same thing). Normally an issue *is* filed back by
> `process_incoming_wanted_issues` and stops being wanted, so the stamp only
> bites when that did not happen: a dead mirror, a provider rate-limiting every
> request, an archive that will not unpack. Those are exactly the runs that
> re-queued the same issue nightly.
>
> It is **its own table**, not a column on `wanted_issues`:
> `refresh_wanted_cache_background` opens by wiping that table, which would
> throw the stamp away precisely when it is needed. It is keyed on
> `issue_number`, not `issue_id` — the id is a provider's, the number is what
> the search and the filing match on. It is **sweep-only**: a queued issue is
> still missing, so it keeps showing on the Wanted page, and the scoped run
> ignores the cooldown the same way it ignores the Monitor toggle. A stamp that
> cannot be read or parsed lets the issue through — pinning an issue off the
> list forever is far worse than one extra search.

### Split GetComics Posts

A GetComics post can hold several downloads (a `<li>` per range, sometimes next
to the post's own buttons). Queue through `get_download_parts()` /
`get_result_parts()`, never `get_download_links()` — it returns only the first
part, which is how #542 fetched Supergirl #1–15 for every issue in #1–80.
Automated downloads take one part via `select_parts_for_issue()` and record that
part's range, not the post title's; a range part is a pack, so it is taken only
with Download Packs on (see **Range Pack Handling**). A manual grab does the same when the search
modal passes the issue (only for a scored result list); every part is queued
only when there is no issue to go by.

#### A `#fragment` link is one comic on a listing page, not a post

A GetComics *listing* page (a weekly update, a Top-10 collection) holds many
unrelated comics. `getcomics_urls.url` is UNIQUE, so the scrape index stores one
row per comic keyed **`<page url>#<entry slug>`** (`_slugify_entry_title`). HTTP
drops the fragment, so fetching such a link returns the whole page — and
returning its first part hands back **a different comic**. That is how 20 wanted
Star Wars issues each downloaded the page's first entry, "Star Wars - Jedi
Knights 010": 20 copies, 1.19 GB, in 106 seconds.

- **`get_download_parts()` is fragment-aware and must stay so.** Given an entry
  slug it returns *that* entry's links (via `_enumerate_page_entries`, which
  mirrors the three layouts the indexer reads, so entry→links cannot drift from
  entry→slug). If the entry is gone it falls back to the row's stored
  `download_url` and otherwise returns **no links**. It must **never** fall
  through to the page-wide read — a download of the wrong comic under the right
  name is far worse than no download.
- **`_slugify_entry_title` is written into `getcomics_urls.url`.** Every slug
  already on every install came out of it, so the truncation lengths and the
  issue-number prefix are load-bearing. Do not tidy it.
- **A slug matching two entries counts as no match.** Both collapsed into one row
  under `INSERT OR REPLACE`, so the page cannot say which one the index kept;
  picking the first is a coin toss between two comics.
- `#canonical` is the migration marker for a single-comic page indexed before
  entry keys existed. It names the page, not an entry.
- **The sweep also keys a per-run `queued_download_urls` on the resolved
  download URL**, as a backstop — *not* on the page URL, because a legitimately
  split post serves several different parts from one page (#542) and taking a
  different part per issue is correct. `downloaded_ranges` cannot cover this
  case: it records only a labelled split part or a `range fallback` tier, and
  the incident was an unlabelled ACCEPT part. A suppressed duplicate still
  counts as a download and still stamps `_mark_queued`, or the issue would be
  handed on to the lower-priority sources while its file is already in flight.

#### The sitemap index: one row per page, one visit per page

Two bugs lived here, and both reported themselves as a network problem.

- **`build_sitemap_index` built its rows without `url`**, which is `TEXT NOT
  NULL`. Every `executemany` raised `NOT NULL constraint failed`, the per-page
  `except` swallowed it, and the weekly job logged `⚠️ Sitemap index rebuild
  returned 0 URLs — check network access`. On a fresh database it failed even
  earlier: `_migrate_from_old_tables` drops `getcomics_sitemap_pages` and
  nothing recreated it, so the first query raised `no such table`.
  `_ensure_urls_table` now recreates it — it holds nothing but HTTP cache
  hints, so losing it costs one unconditional refetch.
- **A sitemap row is keyed by the page URL**, and a scraped *entry* by
  `<page>#<slug>`, so the two never collide. The write is an **upsert, not
  `INSERT OR REPLACE`**: a single-comic page carries its title, issue number
  and `download_url` on that very row, and REPLACE deletes the row before
  re-inserting it, so a weekly refresh would throw away everything the scraper
  learned. `COALESCE` keeps what is there and fills only what is blank.
- **`lookup_series_urls` de-duplicates `full_url`.** It returns one row per
  *entry*, and a listing page holds many entries sharing one page — while the
  caller scrapes `full_url`. In the reported log all 20 candidate slots went to
  three distinct pages (8×, 7×, 5×), *per issue searched*, each behind a
  2-permit/1s rate limiter. That is most of the 9,408 s sweep, and the page
  actually holding the issue never got a slot.
  `scrape_and_score_candidate` already scores every entry on a page and returns
  the right one's links, so one visit is enough.

`_norm_series_key()` is the single copy of the expression stored in, and matched
against, `series_norm_norm`. Every writer has to agree with
`lookup_series_urls`'s `WHERE series_norm_norm = ?`; a row that disagrees is
indexed and then never found, which is what the sitemap insert also did.

#### A rate-limited provider is stood down, not asked again

`core.download_utils` holds a per-provider cooldown
(`note_provider_rate_limited` / `provider_rate_limited` /
`clear_provider_cooldown`). The auto-retry policy backs off **one download**,
which is the wrong unit when a provider throttles the whole client: every queued
item hits the same wall and each schedules its own 60/300/900 s retries. A
reported log has 35 consecutive `MEGA download failed: Too many requests` against
just **4 distinct files**, 35 stack traces, and not one success.

- `models.mega.RATE_LIMIT_CODES` is the allowlist (-3, -4, -6, -14, -16) and
  raises `MegaRateLimited`. "File not found" and "Expired link" are about the
  file, not the client, and must stay out of it.
- `MegaRateLimited` subclasses `Exception` so every existing handler still
  works — which is why **its handler must come before the generic one**, or the
  cooldown never gets set.
- `select_download_url` moves a cooling provider to the **back**, not out: when
  it is the only link the post offers it is still better than nothing, and the
  downloader refuses it on arrival anyway.
- In memory and per-process, like the queue it guards. A cooldown lost on
  restart costs one wasted request; one that outlives the outage would strand a
  provider that has long since recovered.

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

### Path References

Six columns hold an absolute comic path as a bare string, with no foreign key to
`file_index`, so every one of them is orphaned independently when a file moves:

| Table | Column(s) |
|-------|-----------|
| `file_metadata_tags` | `file_path` |
| `reading_positions` | `comic_path` |
| `issues_read` | `issue_path` |
| `reading_list_entries` | `matched_file_path`, `manual_override_path` |
| `reading_lists` | `thumbnail_path` |

`core.database.move_path_references(old, new, is_dir=, conn=)` is the **one**
body for "the file moved, follow it", and `clear_path_references(path)` for "the
file is gone, let go of it". There used to be two drifting copies of the first —
`move_reading_data` and an inline block inside `update_file_index_entry` — which
is exactly why the directory branch never rewrote `file_metadata_tags`. Do not
add a third; `update_file_index_entry` and `update_index_on_move`'s directory
branch both delegate.

Reading lists are the reason `manual_override_path` is on that list as well as
the auto column. `rematch_entries` deliberately **skips** an entry that has a
manual override — a hand-picked mapping is the user's answer, not the matcher's
— so a manual mapping broken by a rename can never heal itself. Following the
path is the only thing that fixes it.

Things that look arbitrary and are not:

- **`move_path_references` takes an injected `conn`, and `update_file_index_entry`
  passes its own.** Opening a second connection while the first still holds the
  WAL writer lock blocks for the full 30s `busy_timeout` and then fails — on
  every single rename.
- **`OR REPLACE` is only for the three tables with a uniqueness constraint on
  the path** (`reading_positions UNIQUE(user_id, comic_path)`,
  `issues_read.issue_path` UNIQUE, `file_metadata_tags PRIMARY KEY(file_path,
  kind, value)`). The two reading-list tables have none, and writing
  `OR REPLACE` there would read as though they did.
- **`file_metadata_tags` needed `OR REPLACE` all along.** Its composite primary
  key meant renaming onto a path that already had tag rows raised
  `IntegrityError` inside the one `try` that also performs the `file_index`
  rename — so the rename was silently lost and logged as a generic error.
- **The trailing slash in `'{old}/%'` is load-bearing** — `'{old}%'` also
  rewrites `/data/Batman Beyond` when you move `/data/Batman`.
- **`clear_path_references` has no `is_dir` flag.** At delete time the path is
  already gone, so an `os.path.isdir` test is permanently False; the exact match
  and the prefix sweep both run unconditionally, and the `LIKE` arm is a free
  no-op for a file. `delete_file_index_entry` uses the same shape for the same
  reason.
- **A delete does NOT clear `reading_positions` or `issues_read`.** "I read
  this" is a fact about the user, not about the file, and a trashed comic can be
  restored.
- **`idx_rle_matched_path` / `idx_rle_override_path` serve the `= ?` arms only.**
  SQLite's `LIKE` is case-insensitive by default and cannot use a BINARY-collated
  index. Do **not** "fix" that with `COLLATE NOCASE`: these paths are stored
  byte-exact so they join against `file_index.path`.

#### Deletion hooks — the three callers that are not deletions

`forget_deleted_path` / `forget_deleted_paths` pair the index delete with the
mapping clear, and they are wired at the **deliberate deletion sites**
(`app.update_index_on_delete`, `routes/files.delete_multiple`,
`core/file_watcher.py`) — *not* behind a flag on `delete_file_index_entry`.
Three of that function's callers delete a row as part of something that is not a
deletion at all, and must keep calling it directly:

- `cbz_ops/single_file.py` — CBR→CBZ is a rename wearing a delete's clothes:
  delete the old row, add the new one, then `move_path_references`.
- `app.py`'s post-download tidy-up — dropping a stale row after a ComicVine
  rename.
- `routes/collection.py` — a folder **re-scan**, which deletes the subtree and
  immediately re-adds it. Clearing here would wipe every reading-list match
  under that folder on every rescan.

#### `cbz_ops/rename.py` must never import from `app`

`monitor.py` imports that module at the top level **in a separate process where
app.py is not loaded**, so even a try/except-guarded `from app import
update_index_on_move` would *succeed* there and execute all of `app.py` —
starting a second APScheduler and spawning another monitor. So `rename_files`
returns `(old, new)` pairs and `routes/files.rename_directory` follows them,
`reconcile=False` per file with one coalesced `reconcile_wanted_for_series` per
series at the end (a folder of 300 issues must not fire 300 whole-series
recomputes). `rename_file` gets no hook at all: its callers work on WATCH/TARGET
staging files, which sit outside `/data` and which `update_index_on_move` would
decline anyway.

All of this is asserted structurally in
`tests/unit/test_path_reference_hook_sites.py`, because app.py cannot be
imported in tests.

### Notification Hook Sites

Downloads settle in **three independent places** — there is no single choke
point. A new download path needs its own hook:

| Path | Terminal status set at |
|------|------------------------|
| In-process HTTP (GetComics/Pixeldrain/MEGA/ComicBookPlus) | `api.py` success in `process_download`; failure after the `is_cancel_requested` guard *and* the `_schedule_auto_retry` gate that follows `set_error_status` |
| Usenet (SABnzbd/NZBGet) | `models/usenet.py` `_set_status` |
| DC++ / AirDC++ | `models/dcpp.py` `_set_status` |

Both pollers delegate to the shared `core.notifications.notify_download_terminal()`
and must call it **outside** their `_jobs_lock`.

Two rules that are easy to break:

- **Cancellations must never notify.** Aborting a transfer is how a cancel
  surfaces from most providers, so the failure path runs for cancels too.
  `set_error_status` downgrades those to `cancelled`, and the notification sits
  *after* the early return that follows it. Hooking inside `set_error_status`
  would be wrong twice over — `download_getcomics` calls it a second time before
  re-raising, so one failure would notify twice.
- **A retryable failure must not notify either.** An in-process download that
  fails on every mirror is parked in a backoff window and re-queued up to
  `MAX_AUTO_RETRIES` times (`core/download_utils.py`), so the first failure is
  usually transient. `_schedule_auto_retry` returns True in that case and
  `process_download` returns early — no push, and no weekly-pack 'failed' write
  either, or the scheduler would queue a second copy alongside the retry. The
  ordering (cancel guard → auto-retry → notify) is asserted structurally in
  `tests/unit/test_download_notify_hooks.py`. Auto-retry is deliberately skipped
  when `manual_url` is set: that is the Cloudflare-challenge marker, and no
  automated client passes those.

- **Wanted issues send one digest per sweep.** The hook is in
  `process_incoming_wanted_issues` inside the `if moved_count > 0` branch and
  outside the per-match loop; a catch-up sweep can import dozens of issues, and
  one push each is unusable. `tests/unit/test_wanted_digest_hook.py` asserts
  this structurally, because app.py cannot be imported in tests.

### Archives in WATCH

Unpacking is **unconditional** — the old `AUTO_UNPACK` setting is gone, and
`core/config.py`'s `REMOVED_SETTINGS` strips it from an upgraded `config.ini`.
Two consequences that are easy to undo by accident:

- **`.zip` and `.rar` bypass `IGNORED_EXTENSIONS`.** They ship *on* that list
  because they are not comics to move, so honouring it in
  `_handle_file_if_complete` would mean the monitor never opens one. The same
  decision is mirrored in `core.download_utils.monitor_claims`, which is why
  `ARCHIVE_EXTS` is defined there (import-light) and re-exported by
  `helpers/unwrap.py` — the two must never disagree, or api.py hands a file to a
  monitor that will not take it.
- **Unpacking is content-aware** (`helpers.unwrap.classify_archive`). A pack of
  ready comics is extracted; an archive of *page images* IS the comic and becomes
  a `.cbz` (a `.zip` is renamed, never repacked; a `.rar` goes through
  `convert_to_cbz`). Blindly extracting the second kind explodes a comic into
  loose pages that the pipeline then moves to TARGET one page at a time — the bug
  this replaced. An archive that cannot be listed falls back to a blind extract,
  so `UNKNOWN_ARCHIVE` must never be conflated with "contains pages".

Multipart/hybrid release **folders** still go to `unwrap_release` first, and
`_process_archive` re-checks that before touching a part.

#### A damaged archive must be opened once, not once per sweep

`zipfile.extractall` writes members in order and raises on the first damaged
one, so the members before it are already on disk. Extracting straight into
WATCH therefore left a partial payload **and** the archive: the 5-minute
reconcile sweep re-extracted the same comics on every pass, and `_move_file`
gave each copy a fresh ` (N)` name in TARGET. One report had 23 corrupt zips
re-processed 22 times each in two hours — 2,416 log lines, 48% of the file, and
`Moon Knight V3 (v1998) #002 (19).cbz` in TARGET.

Three things fix it, and all three are needed:

- **`unzip_file` stages into a hidden `.clu_unzip_<name>` dir** beside the
  archive and promotes the contents only on success. The sweep prunes dot-named
  directories, so a half-extracted archive contributes nothing at all.
- **Every step reports its own outcome.** `unzip_file`, `_unrar_file`,
  `_process_archive` and `_process_file` return a bool, and
  `_handle_file_if_complete` logs `"File Download Complete"` only on True. It
  used to log it unconditionally, on the line *after* a failure the blanket
  `except` had swallowed at INFO — the same "a filesystem test is a different
  question" mistake as `convert_to_cbz`. `_move_file` returns a third value,
  `None`, for "nothing to do" (gone, hidden, still downloading, in cooldown):
  only False earns a backoff, only True earns the completion line.
- **`_failed_files` covers *every* processing failure, not just moves.** It was
  `_failed_moves`, written only by `_move_file`'s except, and the unpack path
  was the gap — `_in_flight` is cleared in a `finally` and `_failed_unwraps`
  covers only multipart release folders. The backoff is checked at the top of
  `_handle_file_if_complete`, before the stability wait and before the archive
  is opened. Still keyed on `(mtime, size)`: the file that failed is often the
  one about to become valid, and a re-download lands at the same path.

> **The orphan sweep can never clear these.** `is_reapable_temp_file` requires
> a `TEMP_DOWNLOAD_PATTERNS` match, and a complete-but-corrupt `.zip` matches
> none — it is not a partial download. An archive falls outside "ignored
> extension" (deliberately bypassed so it can be unpacked) *and* outside
> "reapable temp file", so once corrupt it is immortal unless something records
> the failure. That something is `_failed_files` plus a `problem_files` row
> under `SOURCE_UNPACK` — the one problem source whose path is a download
> rather than a library comic, which is why nothing else reports it.
> `retry_problem` declines it: monitor.py owns WATCH and retries on its own
> schedule, and a second unpacker would race it.

### Data Flow
1. Comics stored in `/data` (mounted volume)
2. Downloads go to `/downloads/temp` then processed to `/downloads/processed`
   - After files are moved *out* of TARGET, a debounced sweep (`schedule_target_cleanup`
     → `helpers.prune_empty_dirs`) removes the empty wrapper folders left behind.
     It never deletes — or descends into — WATCH, TARGET, TRASH or a library root,
     and refuses to run at all if TARGET resolves inside a library. WATCH nested
     inside TARGET is a supported layout.
3. SQLite database in `CACHE_DIR` (default `/cache`)
4. Config persisted in `/config/config.ini` - deprecated - all future settings should be stored in `user_preferences` table in the database

#### Two workers must never pick the same destination name

`_download_dir()` is WATCH, and api.py runs **three** download workers over it.
Names are reserved through `core.download_utils.claim_download_path()`, which
takes an `O_CREAT|O_EXCL` marker on `<final>.claim.crdownload`; the matching
`release_download_claim()` runs on both the success and the all-retries-failed
path. The old `while os.path.exists(final)` scan tested only the *finished*
`.cbz`, which does not exist while a download is running, so two workers picked
`_4.cbz` one second apart, wrote into one `_4.cbz.0.crdownload`, and the loser
died with "Temp file not found".

- **The marker's name must keep `.crdownload` in it.** `monitor.py`'s
  `_is_temporary_download_file` matches that substring anywhere, so the marker is
  invisible to the monitor and reaped by the orphan sweep if one ever leaks. A
  plain `.claim` would be moved to TARGET as though it were a comic — which is
  also why the claim is **not** an empty `.cbz` placeholder.
- **The claim is taken once and held across all retry attempts.** The temp file
  carries the attempt number, so claiming *that* would release and re-take the
  reservation on every retry and reopen the same window.
- The final move is `os.replace`, not `os.rename`: rename refuses an existing
  destination on Windows.
- **All four downloaders use it.** `download_getcomics` was moved onto the
  claim first and `download_pixeldrain`, `download_comicbookplus` and
  `download_mega` were left on the old scan — which is how a reported log shows
  PixelDrain writing `..._1.cbr.part`, the orphan sweep unlinking it
  mid-transfer, and the final `os.replace` failing ENOENT 22 minutes later.
  Their `.part` names derive from the claimed destination, so a temp file is as
  reserved as the file it becomes.
- **PixelDrain holds its claim while a resumable partial remains.** It resumes
  from `tmp_path` and a retry recomputes the destination from scratch, so
  releasing with the partial still there would let another worker take the name
  and resume *our* bytes into its own file. `download_comicbookplus` is the
  opposite case — it always opens `'wb'` — so it deletes the partial and frees
  the name.

#### The orphan sweep may only delete what stopped growing

`monitor.cleanup_orphan_files` had no age check and ran hourly, so it deleted
downloads that were still running (64 MB and 32 MB mid-transfer in one report).
The writer keeps filling its unlinked handle and then fails at the final rename
with ENOENT — a *retryable* failure — so the download is re-queued under a fresh
`_N` name and killed again an hour later; one pack was fetched sixteen times.
`ORPHAN_MIN_AGE_SECONDS` is a grace period on mtime. The monitor is a separate
process from api.py and cannot see `download_progress`, so mtime is the only
evidence available to it.

#### A partial download is recognised by name, and there are two questions to ask

`core.download_utils.is_temporary_download_file()` is the **one** copy of "is
this an in-progress download rather than a comic". It used to exist twice,
byte-identically — in `monitor.py` and inline in `routes/files.py`'s
`/cleanup-orphan-files` — and the two drifted: neither ever learned about
`.dctmp`, AirDC++'s partial-download extension.

An AirDC++ transfer writing into WATCH was therefore processed as a finished
comic every 30 seconds for 20 minutes, and left 28 copies of itself in TARGET.

- **The match is substring-anywhere, not a suffix test, and must stay so.**
  Chrome writes `X.zip.0.crdownload`, and `CLAIM_SUFFIX` (`.claim.crdownload`)
  rides on the behaviour deliberately so a name reservation is invisible to the
  monitor. `tests/unit/test_download_name_claim.py` pins it.
- **`.dctmp` needs its own entry; `.tmp` does not cover it.** There is no dot
  immediately before `tmp` in `.dctmp`, so `'.tmp' in 'x.cbr.dctmp'` is False.
  That one character is the whole bug, which is why a test asserts it.
- **No stability heuristic can catch this.** Both completion checks
  (`_is_download_complete`, `_wait_for_download_completion`) are size-only, and
  DC++ **preallocates** the file at its full final size — so a live transfer
  reads as complete within seconds. The name is the only available evidence.
- **"Must not process" and "safe to reap" are different questions.**
  `is_reapable_temp_file()` is the subset CLU owns; everything in
  `EXTERNALLY_OWNED_TEMP_PATTERNS` is another client's live queue state. A DC++
  queue item legitimately sits idle for hours waiting for a source slot, so the
  mtime rule reads it as abandoned — and deleting it makes AirDC++ start over.
  **Both** deletion sites use the reapable predicate: `monitor.cleanup_orphan_files`
  and `/cleanup-orphan-files`. The latter also had *no age check at all* until
  this change, so clicking the button killed running downloads exactly as the
  sweep once did.
- `monitor_claims` consults the same predicate, or api.py hands the monitor a
  file the monitor now refuses.

#### A failed move must not leave a copy at the destination

`shutil.move` falls back to `copy2()` + `os.unlink()` on **any** `OSError` from
`os.rename`, not just `EXDEV`. When the source is readable but not removable — a
download client holding it open, a CIFS/NFS mount, a read-only source — the copy
**succeeds**, the unlink fails, and a full copy is left in TARGET with nothing
but an ERROR naming the *source* in the log. `get_unique_filepath` is a pure
`os.path.exists` scan with no memory, so the next sweep picks ` (1)`, the one
after ` (2)`, without bound: 28 copies of one still-downloading comic.

`monitor.move_download()` is the fix and every move out of WATCH goes through
it. Its cleanup is safe because of an invariant the caller supplies: `dst` is
`get_unique_filepath`'s output, so it did not exist a moment ago — and if the
move raised while `src` is *still there*, the move did not complete, so anything
now at `dst` was created by that call. **Both halves of that test are load-bearing**;
dropping the `src` check would let a later error undo a move that succeeded.

This is not DC++-specific, so do not gate it on an extension.

#### A refused `utime`/`chmod` must not fail a completed move

`shutil.move`'s cross-device fallback is `copyfile` **then `copystat`**, and
`copystat` calls `os.utime(dst)` unguarded and guards `os.chmod(dst)` only
against `NotImplementedError`. A mount that refuses either — CIFS/SMB without
`noperm`, a Windows-backed WSL2 bind mount — therefore raises `EPERM` *after*
the destination has been written in full. The file is complete and valid; only
its timestamps and mode are missing. `shutil.move` reports that as a failed
move, and every caller believed it.

That is how a `.cbr` came to sit beside its own `.cbz` in TARGET on **every**
download in one library:

    app.log     12:11:43  ERROR  Failed to convert ...001.cbr:
                                 [Errno 1] Operation not permitted: '...001.cbz'
    monitor.log 12:11:44  INFO   Converted to: /downloads/processed/...001.cbz

`helpers.move_file()` is the fix — `os.replace`, else `copyfile` + a tolerated
`copystat` + `os.remove` — and both mounts-facing movers use it:
`helpers.open_zip_for_write` (every CBZ CLU writes) and
`app.process_incoming_wanted_issues` (TARGET → library, where the same EPERM
left a complete copy in `/data` *and* the source in TARGET to be re-copied on
every sweep).

- **Only the metadata failure is demoted.** A failed `copyfile` and a source
  that cannot be removed both still raise: the second is the #582 failure mode
  above, and in TARGET a leftover source is re-matched forever.
- **It is not a replacement for `monitor.move_download()`.** That one *removes*
  a partial copy at `dst`; this one *keeps* a complete one. Different questions.
- **Timestamps are still copied when the mount allows it** — `copystat` is
  attempted every time and only its failure is swallowed, to a DEBUG line.

#### `convert_to_cbz` reports its own outcome; `os.path.exists` does not

A conversion can write a complete CBZ and still fail, and when it does
`convert_to_cbz` **deliberately keeps the source archive**. Every caller used to
answer "did it work?" with `os.path.exists(<base>.cbz)`, which is a different
question and answered yes — so monitor.py logged "Converted to" one second after
app.log logged "Failed to convert", nothing retried, and 23 CBR/CBZ pairs
accumulated in one TARGET folder unnoticed.

`convert_to_cbz` returns a bool. The three external callers — `monitor._move_file`,
`monitor._archive_to_comic` and `api._finish_download_in_watch` — branch on it.
**Do not reintroduce a filesystem test as the success signal**; pinned by
`tests/unit/test_monitor.py` and `tests/mocked/test_convert_to_cbz_contract.py`.

A failure is handed to the Problem Files worklist under `SOURCE_CONVERT`
(`convert_single_rar_file(..., problem_source=)`), and a success clears the row.
The rebuild fallback passes no source on purpose: it records its own row against
the `.cbz` it started from, and two rows for one failure is how they drift apart.
Retry for that source points at **Rebuild**, which is
`CLU.executeStreamingOp('single_file', path)` — literally `convert_to_cbz`, and
already streamed; re-running it inside the request would outlive gunicorn's
timeout on a 500MB pack.

`process_incoming_wanted_issues`' TARGET scan **skips an archive that has a
`.cbz` sibling**. Without it a stuck pair matches the same wanted issue twice
and both are filed, reproducing the pair inside `/data`. Same rule, same reason,
as `core.problem_replacements.is_acceptable_replacement`.

#### A move that failed once backs off

`self._failed_moves` keys `(fingerprint, failures, retry_at)` on the abspath and
is checked at the top of `_move_file`, before the stability wait and before any
copy is attempted. Nothing else remembered a failure: `_in_flight` is cleared in
a `finally` and `_failed_unwraps` covers only multipart release folders, so the
30s poll and the 5-minute reconcile sweep retried forever. The key is
`(mtime, size)` rather than the path alone because the file that failed is often
the file that is about to become valid — the `.dctmp` case ends with AirDC++
rewriting it — and a path-only key would hold the finished comic back for the
whole cooldown.

`_rename_file` returns `RENAME_FAILED`, not `None`, when the rename raised.
Both used to be `None`, so a permission error logged the reassuring
`No rename needed for: ...` — the log claimed the filename was fine while the
rename failed on every pass.

### Frontend
- Jinja2 templates in `templates/`
- Bootswatch themes (26 themes supported)
- Bootstrap 5 with custom CSS in `static/css/`

#### Control characters in static assets — always escapes, never raw bytes

Write a control character in JS/CSS/HTML as an escape (`'\0'`), never as a
literal byte. A raw one is invisible in an editor, review and diff, makes `grep`
report the whole file as "Binary file … matches" so it stops being searchable,
and survives every syntax check. One stray raw NUL — in a string used as a DOM
attribute value, where the HTML parser rewrites it to U+FFFD — made every button
on the Problem Files page do nothing, with no error anywhere.
`tests/unit/test_static_assets_are_text.py` pins this across
`static/js`, `static/css` and `templates/`.

#### User Feedback — never use native JS dialogs
**Never** use `alert()`, `confirm()`, or `prompt()`. Always use a Bootstrap **Modal**
(confirmations, anything needing a decision or input) or a **Toast** (success,
error, and status messages).

- Confirmations: `CLU.showDeleteConfirmation()` / the `window._cluDelete` contract
  (`static/js/clu-delete.js` + `partials/modal_delete_confirm.html`), or a
  purpose-built modal in the page template.
- Messages: `CLU.showToast(title, message, type)`, `CLU.showSuccess()`,
  `CLU.showError()` from `static/js/clu-utils.js`.
- A page using toasts must include `partials/toast_container.html`, otherwise
  `CLU.showToast` falls back to `alert()`.

## Configuration

Settings in `core/config.py` define defaults merged with `/config/config.ini`. Key settings:
- `WATCH`/`TARGET`: Folder monitoring paths
- `AUTOCONVERT`: Auto CBR-to-CBZ conversion
- `BOOTSTRAP_THEME`: UI theme name
- API keys: `COMICVINE_API_KEY`, `PIXELDRAIN_API_KEY`, `METRON_USERNAME/PASSWORD`
- `config.ini`  is being deprecated - all future settings should be stored in `user_preferences` table in the database

## File Processing Pipeline

CBZ processing in `edit.py` (`process_cbz_file`):
1. Delete `_MACOSX` folders
2. Remove prefix characters (`.`, `_`, `._`) from filenames
3. Skip/delete files based on configured extensions
4. Normalize image filenames with zero-padded numbering

## GetComics Search Scoring System

The GetComics download detection uses a scoring system in `models/getcomics.py` (`score_getcomics_result`) to match search results against wanted issues.

### Scoring Components

| Component | Points | Description |
|-----------|--------|-------------|
| Series match | +30 | Series name matches |
| Issue match | +30 | Issue number found explicitly (e.g., `#1`) |
| Standalone issue | +20 | Issue number found without `#` prefix |
| Year match | +20 | Year matches exactly |
| Title tightness | +15/-10 | Bonus for title closely matching series |
| Different series | -30 | Remaining text indicates different series |
| Arc sub-series | -30 | Story arc sub-series (not variant) |
| Variant sub-series | -30 | Publication variant without acceptance |
| Issue mismatch | -40 | Explicit issue number found but wrong |
| Wrong year | -20 | Year present but doesn't match |

### Range Pack Handling

Ranges are handled differently based on whether they're same-series or different-series:

| Scenario | Result | Score |
|----------|--------|-------|
| Same-series range ending on target (e.g., "Batman #1-12" searching for #12) | FALLBACK | 39 |
| Same-series range containing target (e.g., "Batman #1-12" searching for #5) | FALLBACK | 39 |
| Different-series range ending on target (e.g., "Court of Owls #1-5" searching for #5) | REJECT | -100 |
| Different-series range containing target (e.g., "Court of Owls #1-5" searching for #3) | REJECT | -100 |
| Same-series range with annuals added on (e.g., "Batman #1-50 + Annuals" searching for #50) | FALLBACK | 20 |
| Annual-series range containing target (e.g., "Batman Annual #1-5" searching regular #2) | REJECT | -100 |

Same-series ranges get FALLBACK because the issues ARE the main series issues. Arc/different-series ranges get REJECT because arcs have their own internal issue numbering separate from the main series — and so do annuals and quarterlies. A publication type joined to an issue run by `+`/`&` is an add-on to that run, not a sub-series (`_is_publication_addon`).

> **FALLBACK is a score, not a download.** Automated downloads take a pack only
> when **Download Packs** is on (`download_packs` in `user_preferences`, off by
> default, read through `core.config.is_download_packs_enabled()`). A pack is
> anything covering more than one issue, decided by
> `models.getcomics.is_pack_download()` on the part that would be downloaded:
> a range post, or a range part of a split post. A split post's single-issue
> part is *not* a pack even when the post title is a range (Ginseng Roots #11 in
> "#1-12"). The gate is repeated in four places with no shared choke point:
> the sweep (`app.scheduled_getcomics_download`), the simulation
> (`routes/downloads._run_wanted_simulation`) and both
> `try_download_for_issue` (`models/usenet.py`, `models/dcpp.py`). A skipped
> pack reports `status: "pack_skipped"` and records no range. Manual grabs are
> never gated: the search window marks packs, so picking one is a choice.

### Variant Keywords

Variants are publication types that can be optionally accepted via `SEARCH_VARIANTS` config:

```
annual, quarterly, tpB, oneshot, one-shot, o.s., os, OS,
trade paperback, trade-paperback, omni, omnibus, omb,
hardcover, deluxe, prestige, gallery, absolute
```

Every keyword also matches its plural ("Annuals", "TPBs", "Quarterlies",
"Omnibuses"): build patterns with `_keyword_pattern()`, never `re.escape(kw)`
alone and **never a hand-rolled `s?`**. That spelling silently covers the
regular plurals and misses "omnibuses" and "galleries", which is worse than
missing all of them — the keyword still matches in the places that *do* know
the plural, so the pack is scored as a matched variant while
`parse_result_title` fails to flag it as a format, and the range-pack rejection
in `score_comic` never fires. "Batman Omnibuses #1-3" then becomes a fallback
for regular Batman #2.

### Sub-series Detection

1. **Variants** (Annual, TPB, Quarterly, etc.): Publication variants, penalized unless the variant keyword is in `SEARCH_VARIANTS` config
2. **Arcs** (Batman - Court of Owls): Story arcs with dash notation ("-"), always penalized - arc issue numbering is different from main series
3. **Sequels** (Season Two, Volume 3, Book 4, Part X, Chapter X): Sequel keywords from `SEQUEL_KEYWORDS` config, detected as arc-type sub-series
4. **Different Series** (Batman Inc, Flash Gordon): Series with remaining text that isn't variant, arc, or sequel, penalized

### Sequel Keywords

Sequel keywords (`SEQUEL_KEYWORDS` config) detect space-separated volume/sequel patterns:

```
season, volume, book, part, chapter
```

Examples: "Top 10 Season Two #1", "Rogue Vol 2 #1". These are treated as arc-type sub-series with their own issue numbering.

### "The" Prefix Handling

The swap logic allows matching "The Flash" with "Flash" for series flexibility. However, if a search uses "The " prefix and the result doesn't (or vice versa), it's treated as a different series to prevent false matches.

### Crossover Detection

Crossover keywords (`CROSSOVER_KEYWORDS` config) identify mashup/crossover series names where a year-like number is followed by a crossover separator:

```
meets, vs, versus, x-over, crossover
```

Examples: "Batman '66 Meets Steed and Mrs Peel", "Batman 1984 Meets Spider-Man". When the remaining text after the series name starts with a year-like number followed by a crossover keyword, the result is marked as a different series (not a variant of the base series).

### Decision Thresholds

- `ACCEPT`: Score >= 40, strong match
- `FALLBACK`: Score positive but < 40, same-series range containing target issue
- `REJECT`: Score <= 0 or different-series arc/range

### Config Settings

Key configurable lists (in `config.ini` under `[SETTINGS]`):

| Setting | Purpose | Default |
|---------|---------|---------|
| `VARIANT_TYPES` | Publication format keywords | annual,quarterly,tpB,oneshot,... |
| `PUBLICATION_TYPES` | Series type keywords — also keeps annuals/specials from matching as regular issues in `helpers/collection.py` | annual,quarterly |

> **Never reuse `VARIANT_TYPES` for filename matching.** It carries adjectives
> ("absolute", "deluxe", "prestige", "gallery") that are ordinary words in real
> issue titles (`Nightwing 117 - Absolute Power.cbz`), so filtering on it would
> report owned issues as missing and re-download them. `helpers/collection.py`
> keeps its own narrower `_COLLECTED_EDITION_TYPES` for that reason.
>
> Spin-offs are blocked structurally instead, by `_STRICT_GAP`: with
> `strict_gap=True`, `generate_filename_pattern()` forbids letters between the
> series name and the issue number, so `TMNT - Nightwatcher 003` cannot satisfy
> TMNT #3 while `Nightwing 117 - Absolute Power` still matches (its subtitle
> comes *after* the number). Only `match_wanted_issues_to_files` opts in — it
> is the one matcher that moves and renames files. `match_issues_to_collection`
> stays loose deliberately.
| `SEQUEL_KEYWORDS` | Volume/sequel keywords | season,volume,book,part,chapter |
| `CROSSOVER_KEYWORDS` | Crossover detection keywords | meets,vs,versus,x-over,crossover |

## Docker Environment

- Base: `python:3.14-slim-bookworm`
- Uses `tini` as PID 1, `gosu` for user switching
- Web scraping uses `cloudscraper` (pure Python) - there is no headless browser in the image. Cloudflare challenges are surfaced to the user as "download manually" rather than solved; see `core/download_utils.py` `is_cloudflare_challenge()`. Do not reintroduce Playwright/Chromium.
- `entrypoint.sh` handles PUID/PGID permissions
- **`entrypoint.sh` must print before it does anything slow, and must time
  anything that can be slow.** Its first `echo` was once a hundred lines in,
  after every expensive thing it does, so a slow ownership pass was
  indistinguishable from a container that never started — users read a working
  deploy as a broken one. The opening banner is deliberately variable-free: an
  unset variable there aborts under `set -u` and puts us straight back to
  silence.
- **Every `chown`, `chmod` and `find | xargs` in `entrypoint.sh` must be
  guarded** (`|| echo "  note: …"`, not `|| true` — a real mount problem should
  leave a trace). The script runs under `set -euo pipefail`, so an unguarded one
  ends the container with a bare exit code and no message at all: `find` exits 1
  on a traversal error, `xargs` exits 123 on a failed `chown`, and `chown`
  itself returns `EPERM` on a CIFS/NFS bind mount **even for root**.
- Startup cost belongs off the import thread. Gunicorn binds the socket and then
  imports `app:app` in the worker, inside its own `--timeout 120` budget, so
  module-level work delays every request and can get the worker killed and
  respawned. The startup database backup (`quick_check` + MD5 + full deflate)
  runs on a daemon thread for that reason and reuses the integrity result
  computed just above it — see `backup_database(known_integrity=…)`. Asserted in
  `tests/unit/test_startup_blocking_work.py`.

## Key Patterns

### Logging
Use `app_logger` from `core/app_logging.py` for application logs, `monitor_logger` for folder monitoring.

> **INFO is a per-event budget, not a free channel.** The debug package ships
> the last 5,000 lines of each log, and one support report's `app.log` covered
> **eight minutes** because four sites each logged per item rather than per
> operation: the TARGET dump in `process_incoming_wanted_issues` (930 lines, and
> it runs once per *completed download*, not on a schedule), the sitemap
> candidate line (851), the monitor's config banner (770, saying the same thing
> 385 times) and a corrupt archive retried every five minutes (2,416 lines —
> 48% of `monitor.log`). All are DEBUG now, with one summary line at INFO.
>
> Three rules came out of that: a **per-item** line inside a loop is DEBUG and
> the loop reports a count; a banner repeats only when its content **changes**;
> and a condition that cannot change on its own (memory above threshold, a
> provider rate-limiting) is announced on the **transition**, then occasionally
> — never once per poll. A real failure logged at INFO is the opposite mistake,
> and hid the corrupt-archive loop for the whole of that report.

### Database Access
```python
from core.database import get_db_connection, db_conn
conn = get_db_connection()
# Always use WAL mode - concurrent reads supported

# Prefer db_conn() in new code -- it closes on every path:
with db_conn() as conn:
    if conn:
        ...
```

The dominant pattern in `core/database.py` puts `conn.close()` *inside* the
`try`, so every swallowed exception leaks a connection to the garbage
collector. That is not cosmetic: a leaked read connection pins a WAL read mark,
which blocks auto-checkpoint, which lets the `-wal` grow without bound. During a
corruption episode *every* call throws, so every call leaks — a feedback loop.
`db_conn()` exists so new code cannot add to it. The ~290 existing sites are
being converted where they are hot, not all at once.

### Database Health

Three "database disk image is malformed" failures in 24 hours produced one ERROR
line per failing query and nothing else. The app kept serving for 78 minutes
with a malformed database, and logged `Added file to index` on the line straight
after that insert failed. Two halves came out of that: make corruption
impossible to miss, and stop causing it.

| Module | Purpose |
|--------|---------|
| `core/db_health.py` | Corruption classification, the in-memory error ledger, `describe_storage()`, and the scheduled `run_scheduled_health_check()` |
| `core/db_maintenance.py` | `checkpoint_wal`, `compact_database`, `optimize_database`, `get_page_stats`, `shutdown_checkpoint` |
| `core/db_repair.py` | Salvage orchestration over `tools/repair_db.py`: candidate, row-count diff, guided swap |
| `core/db_lock.py` | The cross-process lock that serialises `init_db()` |
| `routes/database.py` | The whole `/api/database/*` surface (owner-only by path prefix) |

Things that look arbitrary and are not:

- **Corruption is detected by a connection factory, not by edits at call
  sites.** `get_db_connection()` passes `factory=_GuardedConnection`, whose
  cursors wrap `execute*` **and every fetch**. The fetch half is the important
  half: on a large scan SQLite does not touch the damaged page until rows are
  pulled, so most real corruption surfaces at `fetchall()`. One choke point
  covers ~350 call sites that all still swallow their own exceptions.
- **`sqlite3.OperationalError` is a subclass of `sqlite3.DatabaseError`.**
  Latching on the base class would fire the corruption alert on every "database
  is locked", and this app has 8 gunicorn threads, ~10 APScheduler threads and a
  separate `monitor.py` process contending for one file. `CORRUPTION_MARKERS` is
  a deliberate allowlist; widening it produces an alert nobody believes.
- **The error ledger is in memory and must stay there.** Writing it to the
  database is how the alarm gets lost exactly when it matters. It does *not*
  follow `metron_auth_blocked`'s persist-to-`user_preferences` pattern.
- **The badge goes red if the live `quick_check` OR the latched state is bad.**
  `quick_check` does not read every page, so a database that threw "malformed"
  an hour ago can still pass it. A passing check never silently clears a latched
  failure; only a successful swap or a *scheduled* pass does.
- **`get_db_connection()` closes the connection when a PRAGMA raises.**
  `sqlite3.connect()` succeeds lazily, so a PRAGMA is usually the first
  statement to touch the file and is where a damaged database fails — with a
  real connection object already created. Returning `None` without closing it
  leaks a handle on the one database least able to afford one, and on Windows it
  blocks the `os.replace` that installs a repaired copy.

> **`swap_in_database()` is the only way to replace the database file.**
> Restore, Compact and the salvage apply all call it. Its order is load-bearing:
> quiesce (`wait_for_background_writers`) → verify → pre-swap snapshot → delete
> the live sidecars → `os.replace` → re-assert `journal_mode=WAL`. A second copy
> of this sequence is exactly how the old restore came to validate one database
> and then move a *different* WAL over it.

- **A backup never installs a `-wal`.** Backups now hold exactly one member,
  `comic_utils.db`, produced by `sqlite3.Connection.backup()` (`pages=-1`, one
  step — the incremental form restarts whenever a writer commits, so on a busy
  library it can loop forever). The old code `zipfile.write()`'d the live `.db`,
  `-wal` and `-shm` at three different instants under peak write load; a torn
  pair reads back as *malformed*. `restore_database` still reads old archives
  but ignores their sidecars — SQLite rebuilds them.
- **Backup filenames step forward a second on collision.** They carry
  second-resolution timestamps, and a manual backup, a pre-swap snapshot and a
  salvage apply can all land inside one second; two of them silently overwrote
  each other. Stepping the timestamp keeps the name matching
  `_BACKUP_FILENAME_RE` — the path-traversal guard — and lexically sortable.
- **Quarantine snapshots have their own regex and their own accessor.** Do
  **not** widen `_BACKUP_FILENAME_RE` to cover `comic_utils_corrupt_*.zip`: that
  pattern also decides what `_cleanup_old_backups` may rotate away and what
  `restore_database` may install, so widening it lets a user restore a
  known-corrupt file over a healthy database.
- **Compact uses `VACUUM INTO`, never an in-place `VACUUM`.** In-place needs an
  exclusive lock on the whole database, which with this many threads is never
  granted — and while waiting it makes every other connection burn its own 30s
  busy timeout. `VACUUM INTO` runs in an ordinary read transaction and hands its
  output to `swap_in_database`. It is also refused outright on a database that
  fails its integrity check: VACUUM rewrites every page.
- **`tools/repair_db.py` stays standard-library only.** Its entire value is that
  it runs by `docker exec` when the app will not start. The dependency points
  `core/db_repair.py` → `tools.repair_db`, never back;
  `tests/integration/test_db_repair.py::TestRescueScriptStaysStandalone` asserts
  it. `sqlite3` is in the Dockerfile's apt list so the CLI's `.recover` (the
  better salvage) is not dead code in production.
- **The salvage diff reports what it cannot know.** In a real salvage the
  corrupt table is exactly the one whose `SELECT COUNT(*)` fails, so it
  contributes nothing to `total_before` and thousands to `total_after` —
  making a naive total-vs-total show a loss of **zero** on the only table that
  lost rows. `rows_lost` is summed only over tables where both counts are known,
  and the rest are named in `unknown_before` for the UI to report as unknown.
- **`init_db()` is serialised across processes.** It moves the database file
  between volumes (`_migrate_db_to_config_dir`) and does four
  CREATE-copy-DROP-RENAME table rebuilds, none of it in a transaction;
  `busy_timeout` serialises statements, not a four-step rebuild. Two full runs
  genuinely overlap when gunicorn kills a worker on `--timeout 120` and respawns
  it: that re-imports `app.py` and launches a second `monitor.py` while the
  orphaned first — never reaped, since `run_monitor` blocks in `communicate()` —
  is still running its own. The lock **fails open**: refusing to start is worse
  than the race.
- **Shutdown checkpoints the WAL.** `shutdown_server` overrides gunicorn's own
  handler and calls `os._exit(0)`, so every `docker stop`/`restart` killed the
  process with connections open, mid-transaction, and the WAL never
  checkpointed — and nothing else ever checkpointed either. The checkpoint is
  bounded and `os._exit` still runs unconditionally: overrunning Docker's grace
  period earns a SIGKILL, which is the unclean shutdown being avoided.
  `restart_app`'s `os.execv` gets the same treatment.
- **`describe_storage()` is the highest-value field on the page.** A database on
  CIFS/NFS/sshfs is the commonest cause of this corruption and nothing in the
  code can fix it; `overlay` means `/config` was never mounted and the database
  dies with the container. Returns `risk: "unknown"` off Linux rather than
  raising.

`app.py` cannot be imported in tests, so `scheduled_db_health_check` and
`scheduled_db_backup` are **wrappers** over `core/` bodies and
`tests/unit/test_db_shutdown_and_schedule.py` asserts that structurally, along
with the shutdown ordering.

### Problem Files

`core/problem_files.py` owns the `problem_files` table and the owner-only
`/problem-files` page (`routes/problem_files.py`). A damaged comic used to
produce an ERROR log line and nothing else — `thumbnail_jobs` records a bare
`status='error'` with no message — so the only user-facing surface was a generic
`error.svg` tile. Writers today: `core/thumbnail_cache.py` (`thumbnail`), both
`cbz_ops/rebuild.py` and `cbz_ops/single_file.py` (`rebuild`),
`cbz_ops/single_file.py` again for a failed CBR/RAR conversion (`convert`),
`routes/metadata.py` (`metadata-write`), and `monitor.py` for an archive in
WATCH that will not unpack (`unpack` — see **A damaged archive must be opened
once, not once per sweep**). The metadata scanner and `core/bulk_metadata.py`
are deliberate follow-ups.

`unpack` is the one source whose path is a **download** rather than a library
comic: the file never reached `/data`, which is why nothing else reports it.
`is_critical_path` protects the WATCH *folder*, not files inside it, so Delete
still works from the page.

**Detection is report-only.** Nothing scans the library; a file appears because
an operation tried to read it and could not. The page says so in as many words,
because an empty page otherwise reads as "your library is clean".

Things that look arbitrary and are not:

- **It is a ledger, not a history.** `clear_problem` DELETEs. There is no
  `resolved_at`, and no `forget_problem` either — "the user fixed it elsewhere"
  and "the operation succeeded" are the same DELETE, and two names for one body
  is how they drift apart.
- **The key is `(path, source)`.** One file can be broken two ways at once. With
  `path` alone the last writer wins, `occurrences` counts unrelated event
  streams, and a successful thumbnail would clear a *rebuild* failure nobody
  fixed.
- **Dismissal is keyed on `file_mtime`, not on the error text.** A damaged
  archive raises `zlib.error` or `BadZipFile` depending on which page is read
  first, so matching the message would un-dismiss rows at random. A dismissal
  lifts only when the file is rewritten and still fails.
- **Pruning needs positive evidence.** `_is_definitely_gone()` requires the
  *parent directory* to exist. An unmounted library makes every path look
  deleted, and a bare `os.path.exists` would empty the table the first time a
  NAS went to sleep. Unreachable rows come back with `reachable: False` and the
  page offers only "Remove". This is also why there is no delete hook: there is
  no choke point for a file leaving the library, and the file watcher is known
  to miss events on the Docker volume.
- **`list_problems` returns `None` on a read failure, never `[]`.** An empty
  list has to mean "nothing is wrong". Collapsing the two let the page assert
  "Nothing has failed" over a database it had just failed to query, while its
  own summary still counted the rows. Same contract, for the same reason, as
  `models.metron.list_reading_lists_modified_since`; the route turns `None` into
  a 500 and the page shows an error.
- **Pruning is a write, and the listing does not depend on it.** `_prune`
  returns the keys it actually deleted, and a row that could not be removed
  stays in the listing (unreachable, with a Remove button). Dropping a row
  because we *meant* to delete it is what made 56 entries vanish from the table
  while the summary still counted them.
- **The empty state is derived, not asserted.** The page only says "Nothing has
  failed" when the counts agree with the rows; a non-zero count over an empty
  table reports that the entries could not be listed.
- **`skipped` records no problem.** A `.pdf` having no thumbnail reader is a
  fact about the format. Recording it would put every PDF in the library on the
  page — the same mistake that made every PDF re-queue at every boot (#548).
- **The problem hand-offs are NOT gated on `record_job`.** That flag exists so
  folder-art generation does not write `thumbnail_jobs` rows, because those
  drive re-queue storms. It says nothing about diagnostics.
- **`CLASS_CACHE_WRITE` is not archive damage.** An unwritable `/cache` is the
  one thumbnail failure where the comic is fine, so `classify()` returns
  `healthy_file: True` and the page hides Delete entirely. Folding it into the
  generic bucket would offer to delete a healthy library.
- **`classify()` is honest about Rebuild.** Rebuild extracts every entry inside
  one `try`, so a bad CRC aborts the lot; its only real repair is an archive
  that is a RAR wearing a `.cbz` name (`repairable: True`). Everything else gets
  "replace the file", and the page demotes the Rebuild button accordingly.
- **Retry is thumbnail-only, and synchronous.** It is one archive's first page,
  so the operations registry would add a job row, a poller and a race for no
  benefit. It calls `invalidate_thumbnail` *before* `regenerate_thumbnail`:
  `/api/thumbnail` refuses to re-attempt an errored row whose mtime has not
  changed, so without it the retry succeeds and the grid still serves
  `error.svg`. A "retry all" would need the registry.
- **`MAX_OPEN_PROBLEMS` caps new inserts, never updates.** A mount whose
  permissions get revoked mid-scan turns every file into an `OSError`; the cap
  turns a 40,000-row page into 2,000 rows and one log line.
- Delete, Rebuild and Search reuse `move_to_trash`/`is_critical_path`,
  `CLU.executeStreamingOp('single_file', path)` and `CLU.createSourceSearch`
  rather than growing their own implementations. The Search action's
  series/issue/year comes from `cbz_ops.rename.parse_comic_filename` in
  `routes/problem_files._attach_search_context`, so "find a replacement" cannot
  disagree with the parser the rest of the app renames and matches with. It is
  the *primary* button whenever `classify()` says "replace the file", which is
  most real damage.
- **Rows are addressed by a `data-path` + `data-source` pair, never one joined
  key.** A joined key needs a delimiter and no character is illegal in a
  filesystem path; worse, the HTML parser rewrites some bytes inside attribute
  values (a NUL becomes U+FFFD), so what `getAttribute` returns is not always
  what was written. That mismatch made `findRow` miss and every button on every
  row silently do nothing.

#### Replacing a damaged file from its own page

`core/problem_replacements.py` files a downloaded replacement straight onto the
damaged file it replaces. The user already told us the destination when they hit
Search from a Problem Files row: the damaged file's own path.

**Nothing else would ever claim that download.** `process_incoming_wanted_issues`
only files issues that are *missing*, and a corrupt file is still a file, so the
replacement would sit in TARGET forever. That is the whole reason this exists.

The flow is `claim_replacement` (when the download is queued) →
`apply_pending` (whenever something lands in TARGET) → `acknowledge`.
`apply_pending_for_app` is the one entry point both callers use — the sweep in
`app.process_incoming_wanted_issues` (which covers the user closing the page)
and `POST /api/problem-files/replacements/apply` (which the page polls while a
swap is outstanding).

> **The swap order is load-bearing, and it is not the obvious one.**
> Trash first, move second is wrong: `move_to_trash` calls
> `_cleanup_empty_parent`, which rmtree's the folder as soon as it empties, so
> on a single-issue folder the destination stopped existing *between the two
> steps*. The move then failed with "cannot find the path specified", leaving
> the library slot empty and the download still in TARGET. So the replacement is
> staged into the destination folder first under a hidden `.clu_incoming` name —
> the folder never empties, nothing prunes it — and the last step is an
> `os.replace` within one directory, which is atomic. Pinned by
> `TestSwapOrdering` in `tests/integration/test_problem_replacements.py`, whose
> `move_to_trash` stand-in deliberately moves the file *out* of the folder and
> prunes it; stashing it in place instead leaves the folder non-empty and the
> regression goes unnoticed.

> **The pass must run inside an application context.** `api.py` calls
> `process_incoming_wanted_issues` from a bare daemon thread
> (`check_wanted_after_watch_empty`), and **every** function in `helpers/trash.py`
> reads `current_app` — `get_trash_dir`, `get_trash_max_size_bytes`,
> `is_trash_path`, `_cleanup_empty_parent`. Without a pushed context
> `move_to_trash` raises "Working outside of application context" and every
> automatic replacement fails. Nothing else in that function needed a context,
> because it reads `app.config` directly — which is exactly why this was easy to
> miss. Pinned in `tests/unit/test_replacement_pass_hook.py`.

Other things that look arbitrary and are not:

- **One pass at a time** (`_pass_lock`). Two callers drive it — the sweep thread
  and the page, which polls every 10s while a swap is outstanding — and both
  walk the same TARGET. Whoever is second stands down; the work is idempotent.
- **A `.cbr` never replaces a `.cbz`** (`is_acceptable_replacement`). TARGET
  holds a `.cbr` only when the WATCH pipeline has not converted it yet, and
  swapping one in downgrades the library. That is a *hold*, not a failure: the
  entry stays pending and the next pass takes the file once it is a `.cbz`. The
  reverse (a `.cbz` for a damaged `.cbr`) is an upgrade and is allowed.
- **Unstaging recreates TARGET's folder.** The wanted sweep calls
  `schedule_target_cleanup`, which prunes TARGET's empty folders — and staging
  the file is what empties one. If the download still cannot go back it is
  *kept* where it is and the location logged: a stranded file is recoverable,
  a deleted one is not.
- **The destructive routes gate on `core.problem_files.has_problem`.**
  `is_critical_path` protects WATCH, TARGET and the trash root but **not**
  `/config` or `/cache`, where the database lives. Requiring a listed row costs
  nothing and keeps arbitrary-path deletion out of this blueprint.
- **Matching reuses `match_wanted_issues_to_files`.** It is the one matcher that
  moves and renames files, it already opts into `strict_gap=True`, and it
  already handles aliases and the ComicInfo fallback. A second matcher here
  would be a third system to keep in step.
- **The replacement is verified before anything moves.** `verify_replacement`
  CRC-checks the whole archive and requires page images. A partly-readable
  original is worth more than a broken replacement, and without this the swap
  would destroy the original for nothing. A `.cbr` passes structurally — CLU has
  no CRC check for RAR and the WATCH pipeline converts them anyway.
- **The damaged file goes to the trash, never straight to deletion**, and a
  failed swap restores it. A failed replacement has to be a no-op: the damaged
  comic is still the comic the user had, and it is what the entry describes.
- **A failed apply is terminal until the user re-claims.** Otherwise every sweep
  retries the same broken download and the page never says why.
- **A claim with no series or issue is never matched** — it would take the first
  file in TARGET.
- **`target_path` is byte-exact**, for the same reason `reading_positions`
  keeps `comic_path` byte-exact: it is the key the `problem_files` rows and the
  page's own requests are joined on. The page echoes `row.path` verbatim; do not
  normalise it at either end.
- **The applied record outlives the problem row.** A successful swap clears the
  problem entry — the file is fixed — so the row cannot report the result. The
  banner is the only place left, which is why the record persists until the user
  dismisses it.

> **A failed rebuild must leave the comic where it found it.**
> `rebuild_single_cbz_file` renames the `.cbz` to `.zip` *before* extracting and
> to `.bak` before recompressing. A per-entry CRC error — the commonest failure
> on a damaged archive, and exactly what this page points Rebuild at — used to
> abort mid-way and strand the comic under a name nothing in the library
> recognises, beside a scratch folder of loose pages. `_restore_after_failed_rebuild`
> in **both** `cbz_ops/single_file.py` and `cbz_ops/rebuild.py` puts it back;
> `tests/unit/test_rebuild_restores_on_failure.py` pins it. Do not offer Rebuild
> from anywhere without that guard in place.

`helpers.archive_error_detail()` is the message half of
`describe_archive_error()` — same sanitising, no class prefix — so the store can
hold class and message in their own columns. **Do not split the formatted string
by hand**: it returns a bare class name with no colon when the message is empty,
and real messages carry their own colons. Routing the thumbnail handler through
it also fixed the log line that dumped several KB of raw header bytes per bad
comic.

### Per-Comic Thumbnail Cache

A comic's thumbnail is a JPEG at
`CACHE_DIR/thumbnails/<first 2 hex of md5(path)>/<md5(path)>.jpg`, and
`core/thumbnail_cache.py` is its only owner. That formula, the
extract-first-page-and-resize body, and the `thumbnail_jobs` upsert used to be
copy-pasted in **nine** places — `app.py` three times, `wrapped.py` once, and
once in every mutating op under `cbz_ops/` *except* `rebuild.py`. Nobody could
see the omission, and it is exactly why rebuilding a whole series from the File
Manager left every cover stale while rebuilding one issue at a time worked
(#548). **Do not inline a tenth copy** — `regenerate_thumbnail(path)` is one
call.

Things that look arbitrary and are not:

- **The cache is keyed on the path, not the content.** A rewritten comic keeps
  its key, so nothing downstream can notice it changed. Two mechanisms cover
  that, and both are needed: every mutating op calls `regenerate_thumbnail`
  explicitly, and `/api/thumbnail` refuses a cache hit that `is_thumbnail_stale`
  (comic mtime > thumbnail mtime) — the latter is what catches a path that
  forgets the former. Changing the hash, the shard width or the extension
  orphans every JPEG already on every install.
- **Writes go through a temp file in the shard dir plus `os.replace`, never a
  save onto the cache path.** `Image.save()` opens the *existing* file, which
  fails with `EACCES` when it is root-owned and CLU is running as `PUID` — the
  exact error #548 reported. `os.replace` needs permission on the *directory*,
  which CLU has, so it succeeds where the in-place save could not; it also makes
  the write atomic, closing a hole where an interrupted save left a truncated
  JPEG that `os.path.exists()` served forever.
- **`/cache` must stay in every ownership pass in `entrypoint.sh`** — the chown
  loop, the `chmod g+s` list, and the `can_write` probe — and in the
  `Dockerfile` `mkdir`. It was in none of them, which is how root-owned
  thumbnails got created in the first place. The application-side fix makes CLU
  survive that state; this is what stops it happening.
- **That `/cache` chown walk is bounded to `-maxdepth 2`, and the bound is not
  an optimisation to be tidied away.** There is one JPEG per comic at
  `thumbnails/<shard>/<md5>.jpg`, and `find` lstats every one of them on every
  start — correct ownership skips the *chown*, never the *walk*, whatever a
  comment claims. Unbounded, that was minutes of complete silence before the
  container's first log line, on every boot. Depth 2 reaches the 256 shard
  directories, which is the level that matters: `os.replace` needs the
  directory and never the file it replaces, so a root-owned JPEG at depth 3 is
  inert, while a root-owned shard *directory* is #548 all over again — **do not
  lower it to 1.** Everything else under `/cache` that is opened in place for
  writing already sits at depth ≤ 2 (`github_tree_cache.json`,
  `publisher_logos/<id>.png`, the legacy `comic_utils.db`, `tmp/` staging).
  `/cache/trash` gets its own uncapped pass because `move_to_trash` moves whole
  directories in and it is size-capped anyway. **Do not apply the bound to
  `/config`**: the SQLite DB with its WAL/SHM sidecars, the log files and
  `/config/.cache/<provider>/cache.sqlite` (depth 3) really are opened in place.
  `tests/unit/test_entrypoint_startup.py` pins all of it.
- **Nothing may serve a cached JPEG blind.** A cached file can exist, be
  current, and still be unreadable — a root-fallback start writes it as root and
  a UMASK clearing other-read locks the gosu'd process out. `send_from_directory`
  then 500s forever, because `is_thumbnail_stale` is False so nothing
  regenerates. `/api/thumbnail` goes through `_serve_cached_thumbnail`, which
  falls through on `OSError` and lets the regeneration path replace the file.
- **An `error` job is never permanent.** The startup scan retries errored rows
  (bounded to one attempt per restart, since it runs once), and the serving
  route only returns `error.svg` while `file_changed_since` says the comic has
  not been rewritten. A `.cbz` that is really a RAR fails, gets rebuilt into a
  real CBZ, and must recover without a restart.
- **A `skipped` job *is* permanent — do not generalise the bullet above.**
  `error` means this attempt failed; `skipped` means there is no reader for
  this *type* at all (`can_thumbnail` says so for a `.pdf`, and
  `generate_thumbnail_task` declines CBR/RAR), and a type cannot change while
  the path does not. So `get_thumbnail` answers it **without** a
  `file_changed_since` gate and without re-queuing. It had no branch at all
  once, so such a request fell through to the `processing` upsert and submitted
  another doomed job on every poll — and the grid re-polls every 2s.
- **Job rows are written only through `set_job_status`**, because that is what
  stamps `file_mtime`. A raw `INSERT OR REPLACE (path, status)` leaves it NULL
  *and* wipes any value already there, and `scan_library_task` reads NULL as
  "migrated, mtime unknown" and re-queues. A `.cbz` self-healed (its
  `completed` row goes through the cache); every CBR ever viewed did not.
- **`scan_library_task` must enumerate the same set as `build_file_index`** —
  the `is_hidden` prune on directories and the `.`/`_` guard on files, not an
  extension test alone. It is the only library walker whose output never
  reaches `file_index`, so nothing downstream catches its mistakes: it queued
  every macOS AppleDouble sidecar (`._Foo.cbz`, a resource fork, not an
  archive) in the library, each of which failed and, once errored rows became
  retryable, was re-queued at every boot. `prune_hidden_jobs` clears what the
  unfiltered walk recorded; it matches on the **basename only**, because a
  library configured at `/mnt/_comics` is indexed normally and a component-wise
  match would delete all of its rows.
- **The in-flight guard is not conditioned on staleness.** A stale cache file is
  precisely the state *during* regeneration, so gating the `processing` branch
  on it would re-queue a duplicate job on every poll — a grid of covers against
  a two-worker executor.
- **In `cbz_ops/rebuild.py` the refresh goes through `_refresh_thumbnail`**,
  which swallows. The call sites sit inside the big `try/except` that decides
  the rebuild's success, and by then the CBZ is already written: an unwritable
  cache must not be reported to the user as "Failed to rebuild".

`generate_thumbnail_task` (async, skips CBR) and `generate_thumbnail_sync`
(folder art, records no job row) are now thin wrappers. `wrapped.py`'s
`ImageUtils.get_thumbnail_path` delegates too.

### Folder Thumbnails

Folder cover art is **a real `folder.png` written into the comic folder** — there
is no separate cache. That single fact drives most of the design.

The work is split in two:

| Where | What |
|-------|------|
| `core/folder_thumbnails.py` | Pure PIL. `select_cover_files()` picks which comics contribute covers; the `STYLES` registry maps the site-wide `folder_thumbnail_style` preference to one of four composers (`fanned`, `single`, `cascade`, `mosaic`) |
| `app.generate_folder_thumbnail_internal` | The I/O around it — reads preferences and the pin, resolves covers through the per-comic thumbnail cache, clears old art, writes `folder.png` |

The split exists so the composers are testable: `app.py` cannot be imported in
tests, so anything left there is only reachable through AST assertions
(`tests/unit/test_folder_thumbnail_orchestrator.py`). **Put new logic in
`core/`, not in the orchestrator.**

Rules that are easy to break:

- **Every composer must return exactly `CANVAS_SIZE` (200x300) RGBA.** The grid
  sizes cards from the container (`aspect-ratio: 2/3` in `collection.css`), so a
  composer returning a different size renders as a differently-sized card next
  to its neighbours.
- **Element 0 of the cover list is the *primary* cover in every style** — the
  whole image in Single, the front card in Fanned/Cascade, the top-left tile in
  the Mosaic. That is what makes a pinned issue work across all four styles.
  The orchestrator prepends the pin **before** truncating to `max_covers`;
  truncating first would silently drop the pin in Single Image mode.
- **Both branches of `select_cover_files` pick candidates through
  `_is_cover_candidate`.** The flat branch always filtered leading `.`/`-`/`_`
  and the nested, borrowed-cover branch never did, so a publisher folder could
  take an AppleDouble sidecar out of a child series folder — and `._` sorts
  before letters, so it won.
- **Clearing old art must sweep `helpers.FOLDER_THUMBNAIL_EXTENSIONS`**, never a
  local list. A missed extension survives the write and keeps winning
  `find_folder_thumbnail`, so the new image is generated and then never shown.
  This is exactly how a `.webp` gap existed in two places before.
- **`may_write()` is what protects uploaded art**, and only for auto-generation
  (`overwrite=False`). The explicit menu actions and the "Regenerate All
  Thumbnails" sweep pass `overwrite=True` and *will* destroy an uploaded image —
  deliberately, and behind a confirm modal.
- **Adding a style is one entry in `STYLES` plus a preview image.** The /config
  picker renders from `style_choices()`, so no template change is needed, but
  `tools/make_thumb_style_samples.py` must be re-run to produce
  `static/images/thumb-style-<id>.png` (a test asserts every style has one).
  Re-run it after changing any composer, or the previews drift from reality.

Both recursive sweeps (`/api/generate-all-missing-thumbnails` and
`/api/regenerate-all-thumbnails`, in `routes/collection.py`) run on a background
thread and report through the `core/app_state.py` operations registry. They must
stay off-request: walking a library and rebuilding every folder far outlasts
gunicorn's 120s timeout.

> **A sweep never touches the folder it was invoked on.** `_walk_folders()`
> excludes its own roots, so "Regenerate All Thumbnails" on `/data/DC Comics`
> restyles every series inside it and leaves `/data/DC Comics/folder.png` alone
> — a publisher image is usually hand-picked, and it is not what the user is
> replacing. The all-libraries sweep from /config excludes the library roots the
> same way. This is a promise the confirmation modal makes in so many words, so
> it is behaviour, not an implementation detail.

A per-folder pin lives in `folder_thumbnail_pins` (`folder_path` PK →
`comic_path`). Store both paths **byte-exact**, for the same reason
`reading_positions` does — they are joined against `file_index.path`.

### Local ComicVine DB Auto-Update

The `comicvine_sqlite` provider reads a SQLite dump from a path the user types
into Settings. A public mirror republishes that database, so
`core/comicvine_db_update.py` can keep the user's copy current.

**Opt-in** (`comicvine_sqlite_auto_update` in `user_preferences`, default OFF).
A ~541 MB download and a multi-GB disk write must not start unannounced on
upgrade. The button is not gated: clicking it is a choice.

The mirror publishes a 69-byte `checksum` file in `md5sum` format next to the
zip, holding the MD5 of the **extracted** `.db`. That gives the same probe/apply
split as `core/reading_list_sync.py` — and the same value is the integrity
check on the unpacked file, so nothing else had to be invented to verify the
download.

> **The source URL must never reach the UI.** It is a module constant in
> `core/comicvine_db_update.py`. The settings page states the cadence and the
> size, and nothing else; `tests/unit/test_comicvine_db_update_ui.py` and
> `tests/routes/test_comicvine_db_routes.py` both assert the hostname appears
> in neither the template nor any response body.

Things that look arbitrary and are not:

- **The 2-week cadence is in the body, not the trigger.** The job is registered
  as `IntervalTrigger(hours=6)` with an explicit `next_run_time`, and
  `run_scheduled_update()` gates on the persisted `comicvine_sqlite_last_check`.
  An `IntervalTrigger`'s clock restarts at process start and this scheduler has
  no jobstore, so `IntervalTrigger(weeks=2)` would **never fire** on a
  `restart: always` container restarted more often than that — the feature
  would look enabled and do nothing forever. Asserted structurally in
  `tests/unit/test_comicvine_db_update_schedule.py`, because app.py cannot be
  imported in tests.
- **`probe()` returns `(None, message)` on failure and never an empty token.**
  Same contract, for the same reason, as
  `models.metron.list_reading_lists_modified_since` and
  `core.problem_files.list_problems`: collapsing "I could not ask" into
  "nothing changed" would make every outage prove the database is current, and
  the sweep would skip it forever.
- **The swap order is load-bearing.** Download → unpack (hashing as it writes,
  one pass over several GB) → **delete the zip** → verify → fix permissions →
  `os.replace`. The zip goes *before* the swap so peak disk is `old + new`
  rather than `old + new + 541 MB` on the one volume that must hold all three.
  Everything is staged in the **destination directory** under the hidden
  `.clu_incoming` name `core/problem_replacements.py` uses, so the last step is
  an atomic rename within one directory.
- **`match_parent_permissions` on the staged file, before the replace.** A file
  CLU writes lands with the process umask, and on a root-fallback start it lands
  `root:0600` — unreadable to the gosu'd process that then opens it read-only on
  every single lookup. Same failure as #548's root-owned thumbnails.
- **Nothing is stamped on failure**, and the token is written only after the
  swap succeeds. Stamping earlier would make the next sweep skip a database
  that was never installed — the same rule as `core.reading_list_sync.apply`.
  A failed run is a no-op: the destination is untouched until the replace and
  every staged file is removed in a `finally`.
- **Verification is the MD5 plus a schema probe, not `check_integrity()`.**
  `quick_check` reads every page of a multi-GB file, and a byte-exact match
  against the publisher's own hash is stronger evidence than a structural scan
  of bytes already proven. What the hash cannot prove is that the file is the
  database *this provider* expects, which is what `verify_database()` checks —
  the same `cv_volume`/`cv_issue` tables `ComicVineSqliteProvider.test_connection`
  looks for.
- **Stale `-wal`/`-shm` beside the destination are removed after the swap.** CLU
  opens this file `mode=ro` and never creates sidecars, so anything there came
  with the user's own copy; a `-wal` from the previous database beside a fresh
  one is corruption.
- **`needs_update` is also true when the file is missing.** A matching token
  over a deleted database would never dislodge itself, and this is what makes
  the button work as a first-run bootstrap.
- **One run at a time** (`_run_lock`, non-blocking). The sweep and the button
  both drive it, the button can be clicked twice, and they walk the same
  destination directory. Whoever is second stands down; the work is idempotent.
- **The routes are under `/api/providers/comicvine_sqlite/`, not
  `/api/preferences/<key>`.** `core/auth.py` gates `/api/providers` as
  owner-only; `/api/preferences/` is *not* on that list, so as a POST it falls
  through to the `clerk` default — and a Clerk must not be able to switch on a
  site-wide 541 MB download. `gcd_metadata_languages` uses the generic route
  because it sets a string; this one spends bandwidth and disk.

The run is off-request on a daemon thread reporting through the operations
registry — it far outlasts gunicorn's 120s timeout — and the page polls
**`/api/operation/<op_id>`**, never `/api/operations`.

### Metron Authentication

Metron accepts either an **API token** (mokkari >= 4.4.0, `api_token=`, sent as
`Authorization: Bearer`) or a username and password. Both are supported;
`models/providers/metron_provider.py` declares them as two `auth_modes`, and the
settings page renders the picker from that list alone — adding a mode is a data
change, not a template change. Every field a mode names must also be in
`auth_fields`, or it is rendered and then dropped on save.

**A 401 or 403 latches a lockout that stops all Metron traffic.** A user mistyped
their credentials, CLU retried until Metron's fail2ban banned their IP, and a
Metron admin asked for it to be reported. Metron's published guidance is that
only 429 and 5xx are worth retrying. So:

- Detection reads the status off `exc.__cause__.response.status_code`
  (`metron.auth_failure_status`), the same place `is_connection_error` looks —
  not out of `ApiError`'s message, which mokkari assembles for humans.
- Enforcement is in **two** places, and both are needed. `_MetronPacer.before()`
  short-circuits every `_api_call`; `get_api()` refuses to hand out a client at
  all, checked *before* the session cache, because several callers fetch a
  client once and then drive mokkari in a loop.
- **There is no timed auto-retry.** A rejected credential does not heal on its
  own. The block clears only on a credential save, a successful connection test,
  or the Re-enable button (`POST /api/providers/<type>/reset-auth`).
- The flag lives in `user_preferences` (`metron_auth_blocked` and friends), not
  just in memory, so a container crash-looping with bad credentials does not
  resume hammering. The database is the source of truth; the copy in `_AuthBlock`
  is a memo that `invalidate_session_cache()` drops. Reads fail **open** but
  never cache that: `get_user_preference` swallows its own errors, so a read
  taken before the database is ready is indistinguishable from "not blocked",
  and remembering it would switch the lockout off for the life of the process.
  `_read_persisted()` therefore queries the table directly and returns `None`
  on failure, which leaves the memo unloaded so the next call retries.
- **`test_connection` is the one path allowed past the lockout**
  (`get_api(..., ignore_auth_lock=True)`, `_api_call(..., raise_auth_errors=True)`).
  Without that bypass a user who fixed their credentials could never prove it.

> **Every Metron call must go through `_api_call`.** A raw `api.<method>()` takes
> no rate-limit slot *and* ignores the lockout, so a loop holding a client keeps
> calling with credentials Metron has already rejected. `fetch_arcs_page` is the
> deliberate exception — it builds its own request to escape mokkari's
> auto-pagination — and therefore carries the auth rules by hand, including
> sending no basic-auth tuple when only a token is configured.

Saving Metron credentials verifies them in the same request
(`BaseProvider.validate_on_save`, Metron only). The credentials are stored either
way and the response carries `valid`/`error`: refusing the save would strand a
user whose provider is merely down.

### ComicInfo.xml Writes

`core/comicinfo.py` owns the **single** `generate_comicinfo_xml`.
`routes/metadata.py` and `models/comicvine.py` only re-export it (callers and
tests import the name from both). There used to be two near-duplicate copies
that had drifted; a field added to one was silently dropped on the paths using
the other. Do not add a third — extend the one in `core/`.

It uses an explicit `add(tag, ...)` allowlist, so a field a provider maps but
that has no line there is computed and then discarded. Adding a ComicInfo field
means adding one `add()` call.

> **The writer never invents a value it can't know.** It defaults only
> `LanguageISO` (`en`) and `Manga` (`No`) — both safe, since every manga-aware
> provider sets `Manga` itself. It deliberately has **no `Notes` fallback**: a
> serializer cannot know a file's provenance, and `Notes` doubles as the
> "already tagged, skip this file" sentinel read by `routes/metadata.py`,
> `models/comicvine.py` and `app.py`, so a fabricated one would both mislabel
> the source and make the file permanently un-retaggable. Every provider mapper
> sets `Notes`; the two inline GCD-SQLite builders in `routes/metadata.py` set
> theirs where the dict is assembled.

That sentinel is read by **six** independent auto-tag entry points — two in
`app.py` (`auto_fetch_metron_metadata`, `auto_fetch_comicvine_sqlite_metadata`),
one in `models/comicvine.py` (`auto_fetch_metadata_for_folder`) and three in
`routes/metadata.py` (batch, provider search, GCD) — with no shared choke point,
so the *check* is necessarily repeated. The *policy* is not: all six call
`core.comicinfo.has_trusted_notes()`, and Notes written by scrapers we don't
trust (Amazon, Comixology) are listed once in `UNTRUSTED_NOTES_MARKERS` so those
files stay eligible for re-tagging. Add a new exclusion to that tuple only —
never re-inline the string at a call site.

> **The three folder-walking entry points must ask `is_zip_container()` first.**
> ComicInfo.xml lives inside a zip, and every reader and writer here raises
> `ValueError("Only .zip or .cbz files are supported by this function.")` on
> anything else — but they all collect `(".cbz", ".cbr")`. Two of them run the
> whole folder inside one `try`, so a single `.cbr` abandoned every remaining
> file in that folder: eleven of those errors in one reported log, each one a
> folder tagged part-way. CLU converts CBRs in the WATCH pipeline, so one in the
> library is a file the user chose to keep — it is a **skip** (`continue`), not
> a failure, and not an error counted on every run.

Writing is a full rebuild of the archive, so `add_comicinfo_to_cbz`
(`routes/metadata.py`) and `add_comicinfo_to_archive` (`models/comicvine.py`)
**merge by default**: tags the archive already had that the new metadata does
not supply are carried forward via `core.comicinfo.merge_comicinfo_bytes`.
No provider covers every field — ComicVine has no genre data at all — so without
this, re-tagging a GCD-sourced file with ComicVine wipes its `Genre`.

> **A restore must pass `merge_existing=False`.** The bulk-metadata undo
> (`routes/bulk_metadata.py`) re-applies snapshotted prior bytes; merging there
> would carry tags forward from the very metadata being undone, leaving the file
> in neither the old nor the new state.

Credit roles from ComicVine arrive as ONE comma-joined string per creator
("penciler, inker"). `models/comicvine.parse_creator_roles` splits it and
buckets each token independently, so one person can hold several credits; the
local-DB path (`models/comicvine_sqlite.py`) calls the same function so the two
cannot drift.

### Image Processing
Use `helpers.py` functions: `safe_image_open()`, `create_thumbnail_streaming()` for memory-safe PIL operations.

## Project Rules

- **Deleting directories:** any code that removes a directory it did not create must
  first consult `helpers.library.get_protected_roots()` (automated/background work) or
  `is_critical_path()` (user-initiated routes). A sweep that only checks its own root
  is not enough — `is_hidden()` treats any name starting with `.` or `_` as junk, so a
  configured folder can be destroyed via its *parent* without ever being visited.

- Every new route in `routes/` must have a corresponding test in `tests/routes/`.
- Any modification to `cbz_ops/` or file operations must include a pytest fixture check.
- **Verification:** Before finishing any task, run `pytest` and ensure 100% pass rate.
- **Maintenance:** If a feature is updated, the corresponding test file MUST be updated in the same PR.
