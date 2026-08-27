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
| `routes/downloads.py` | GetComics search/download (search is scored server-side via `score_getcomics_result`), auto-download schedules, weekly packs |
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

- Base: `python:3.11-slim-bookworm`
- Uses `tini` as PID 1, `gosu` for user switching
- Playwright/Chromium for web scraping features
- `entrypoint.sh` handles PUID/PGID permissions

## Key Patterns

### Logging
Use `app_logger` from `core/app_logging.py` for application logs, `monitor_logger` for folder monitoring.

### Database Access
```python
from core.database import get_db_connection
conn = get_db_connection()
# Always use WAL mode - concurrent reads supported
```

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
