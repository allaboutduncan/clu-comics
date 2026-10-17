# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Comic Library Utilities (CLU) is a Flask-based web application for managing comic book collections. It provides bulk operations for CBZ/CBR files, metadata editing, file renaming, format conversion, and folder monitoring. Designed to run in Docker, it integrates with comic databases (GCD, ComicVine, Metron) for metadata enrichment.

## Development Commands

```bash
python app.py                       # run locally
python -m py_compile <filename.py>  # syntax check
```

Production runs `gunicorn -w 1 --threads 8 --timeout 120 app:app` (see Dockerfile). **The single worker is load-bearing**: the thumbnail in-flight guard and several in-memory claims/cooldowns assume one process.

## Architecture

### Core Application Flow
- **`api.py`**: Creates the Flask app instance and handles download queue/remote downloads
- **`app.py`**: Main application - imports Flask app from `api.py`, registers blueprints, defines all routes and API endpoints
- **`monitor.py`**: Standalone file watcher for folder monitoring (runs when `MONITOR=yes`)

### API v1 docs

/api/v1/docs documents all token protected API routes. To keep the page in sync with the API, edit the ENDPOINTS list at
  routes/api_v1_docs.py:17 whenever a route changes — the test asserts the catalog stays complete.

### `cbz_ops/rename.py` must never import from `app`

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

### Data Flow
1. Comics stored in `/data` (mounted volume)
2. Downloads go to `/downloads/temp` then processed to `/downloads/processed`
   - After files are moved *out* of TARGET, a debounced sweep (`schedule_target_cleanup`
     → `helpers.prune_empty_dirs`) removes the empty wrapper folders left behind.
     It never deletes — or descends into — WATCH, TARGET, TRASH or a library root,
     and refuses to run at all if TARGET resolves inside a library. WATCH nested
     inside TARGET is a supported layout.
3. SQLite database in `CACHE_DIR` (default `/cache`)
4. Settings persisted in the `user_preferences` table (`config.ini` is imported once, then retired)

### Function-local imports

> **A function-local import binds the name for the whole function.**
> `from datetime import datetime, timedelta` inside a `try` in
> `scheduled_getcomics_download`'s per-issue loop made `datetime` a local, so a
> `datetime.now()` 236 lines earlier raised `UnboundLocalError` and **every**
> scheduled auto-download died before it searched for anything. The same shape
> in `config_page` made saving Metron credentials a silent no-op, swallowed by
> the surrounding `except Exception: pass`. It is invisible to `py_compile` and
> to an unused-import check, so
> `tests/unit/test_no_shadowed_local_imports.py` asserts it over the AST of
> every module: a function must not read a name it later imports locally. The
> rule is deliberately "reads it *first*" rather than "shadows a module-level
> import" — the latter fires on 81 harmless re-imports and would need an
> allow-list that goes green on the next real one.

### Frontend

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

**Every setting lives in the `user_preferences` table. `config.ini` is deprecated.**
`core/settings.py` is the one table (`SETTINGS_SCHEMA`: legacy key → pref name,
type, default, category) and the one accessor. Key settings:
- `WATCH`/`TARGET`: Folder monitoring paths (`get_watch_dir()` / `get_target_dir()`)
- `AUTOCONVERT`: Auto CBR-to-CBZ conversion
- `BOOTSTRAP_THEME`: UI theme name
- API keys: `PIXELDRAIN_API_KEY` is a setting; `COMICVINE_API_KEY` and Metron
  credentials live in `provider_credentials`

Things that look arbitrary and are not:

- **New settings go in `SETTINGS_SCHEMA`.** Read with `get_setting`/`get_bool`/
  `get_int`/`get_list`, write with `set_setting`. Prefs that predate the schema
  keep their names (`enable_custom_rename`, `bootstrap_theme`, `custom_headers`...).
- **Reads are cached for 5s, as one snapshot of every schema pref.** `monitor.py`
  and `api.py` are separate processes; the old 5s mtime poll of `config.ini` is
  gone, and this TTL gives them the same latency with nothing to watch. A failed
  read is *not* cached (`get_user_preference` swallows its errors, so a failure
  looks like "unset"; caching it would pin the defaults for the TTL, and the
  first reads happen during startup). `load_flask_config` invalidates first.
- **Never read a setting into a module constant.** The cbz_ops modules,
  `core/comicinfo.py` and `missing.py` did (`skippedFiles`, `LARGE_FILE_THRESHOLD`,
  `xml_year`...), freezing whatever was stored at import. Ask per call.
- **`core.config.config` is a deprecated read-only view** over the preferences,
  kept for one release so nothing outside the repo breaks. No code in the repo
  reads through it any more: `TestNoNewIniStyleReads` in
  `tests/unit/test_settings.py` is a ratchet whose table is now empty, and it
  fails if a `config.get("SETTINGS", ...)` appears. Writes through the view are
  refused with a warning. Use `core.settings`; `/cache` and `/data` come from
  `core.config.get_cache_dir()` / `get_data_dir()` (functions, so tests patch one
  name). The next release deletes the view, `CONFIG_FILE` and the import.
- **`app.py` re-runs `load_flask_config(app)` right after `init_db()`.**
  `api.py` fills `app.config` at import, before the database exists, so on the
  first start after the ini import it would otherwise hold defaults (trash,
  rename pattern...) until the next settings save. Asserted structurally.
- **The import runs once** (`migrate_ini_to_user_preferences`, flag
  `config_ini_migrated_to_prefs`) and renames the file to `config.ini.migrated`.
  It never overwrites a pref that already has a value — the DB is authoritative
  for the keys migrated piecemeal earlier — and the flag is set only after every
  write succeeded. It is removed in the release after it ships.
- **`/config` and `/cache` are fixed paths, not settings and not env vars.**
  The database and backups live on the `/config` volume and thumbnails/trash on
  `/cache`; users mount volumes onto them (`docker-compose.yaml`).
  `core.config.CACHE_DIR` is a constant. An ini that named a different
  `CACHE_DIR` gets a warning at import time and is otherwise ignored.
  (`CONFIG_DIR` still honours an env override, which tests and non-Docker dev
  runs rely on.)
- Asserted in `tests/unit/test_settings.py` (schema covers every legacy key, no
  module writes the ini) and `tests/unit/test_config.py`.

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

- **`swap_in_database()` (`core/database.py`) is the only way to replace the database file** - restore, compact and salvage all go through it. See `.claude/rules/database-health.md` before touching that area.

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

## Subsystem notes (`.claude/rules/`)

Detailed design notes for each subsystem live in path-scoped rule files that load automatically when you work on matching files. If a change touches a subsystem below without opening one of its listed files, read its rule file first.

- `.claude/rules/getcomics-sweep.md` - GetComics sweep, split posts and scoring
- `.claude/rules/file-job-queue.md` - File job queue
- `.claude/rules/reading-lists.md` - Reading list sync and reading-list gaps on the Wanted list
- `.claude/rules/target-scan.md` - TARGET scan, series identity and filename matching
- `.claude/rules/filename-chars.md` - Filesystem-hostile characters (the one sanitizer)
- `.claude/rules/path-references.md` - Path references and deletion hooks
- `.claude/rules/notifications.md` - Notification hook sites and the status page stores
- `.claude/rules/watch-pipeline.md` - WATCH pipeline: archives, name claims, orphan sweep, moves, conversion
- `.claude/rules/scheduled-times.md` - Scheduled job times (UTC everywhere)
- `.claude/rules/issue-badge.md` - Collection grid issue badge and read-status wiring
- `.claude/rules/database-health.md` - Database health, maintenance and repair
- `.claude/rules/problem-files.md` - Problem Files, damaged-file replacement and the library health scan
- `.claude/rules/thumbnails.md` - Per-comic thumbnail cache and folder thumbnails
- `.claude/rules/comicvine-db-update.md` - Local ComicVine DB auto-update
- `.claude/rules/metadata.md` - Metadata: Metron auth, collected editions, provenance, series.json, ComicInfo writes

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
