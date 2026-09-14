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
| `core/thumbnail_cache.py` | Per-comic thumbnail cache — the cache path, the permission-safe atomic write, regeneration and invalidation. Every mutating op owes it one call. See **Per-Comic Thumbnail Cache** below |
| `core/folder_thumbnails.py` | Folder cover art — cover selection, the four style composers (`STYLES`), and the background auto-generation queue. See **Folder Thumbnails** below |
| `core/reading_list_sync.py` | Re-checks an imported reading list against its source (GitHub CBL, Metron list, Metron arc, ComicVine arc). Probe/apply split, one opaque change token per list. See **Reading List Sync** below |
| `core/notifications.py` | Outbound push via Apprise - owner-global settings in `user_preferences`, event catalog (`EVENT_DEFS`), `notify_async()` used by every hook site. `apprise` is imported lazily and every path swallows its exceptions: a notification must never break the download it reports on |

### Other Root Modules
| Module | Purpose |
|--------|---------|
| `rename.py` | Comic file renaming with regex patterns for volume/issue extraction |
| `edit.py` | CBZ editing - image manipulation, file reordering, cropping |
| `convert.py` | CBR to CBZ conversion using `unar` |
| `wrapped.py` | Yearly reading stats image generation (Spotify Wrapped style) |
| `helpers/` | Utility functions — `is_hidden()`, `safe_image_open()`, `create_thumbnail_streaming()`, `prune_empty_dirs()`, ZIP/RAR extraction. `helpers/library.py` owns the path-safety predicates: `get_protected_roots()` (automated sweeps) and `is_critical_path()` (interactive routes) |
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
| `routes/metadata.py` | ComicInfo.xml management — provider search, batch processing, field updates |
| `routes/series.py` | Releases/Wanted/Pull List — series sync, mapping, subscriptions |
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
effect, so it can only ever have the one poller in `base.html`.

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

### Frontend
- Jinja2 templates in `templates/`
- Bootswatch themes (26 themes supported)
- Bootstrap 5 with custom CSS in `static/css/`

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

Same-series ranges get FALLBACK because the issues ARE the main series issues. Arc/different-series ranges get REJECT because arcs have their own internal issue numbering separate from the main series.

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

### Database Access
```python
from core.database import get_db_connection
conn = get_db_connection()
# Always use WAL mode - concurrent reads supported
```

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
