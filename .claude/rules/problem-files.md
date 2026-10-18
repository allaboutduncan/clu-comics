---
paths:
  - "core/problem_files.py"
  - "core/problem_replacements.py"
  - "core/library_health.py"
  - "core/series_json_check.py"
  - "routes/problem_files.py"
  - "routes/library_health.py"
  - "templates/problem_files.html"
  - "templates/library_duplicates.html"
  - "static/js/problem_files.js"
  - "static/js/library_duplicates.js"
  - "cbz_ops/rebuild.py"
  - "cbz_ops/single_file.py"
  - "core/thumbnail_cache.py"
  - "routes/metadata.py"
  - "monitor.py"
  - "app.py"
---

# Problem Files, damaged-file replacement and the library health scan

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

`series-json` is the one **scan-driven** source (`core/series_json_check.py`):
nothing failed, so the page's *Scan series.json* button walks the library roots
and flags a `series.json` whose `name`, `description_text` or `comic_image` is
missing, null or blank (Mylar has no `title`; "series" and "title" both mean
`name`). It is never scheduled — the page promises not to scan on its own. The
row's path is the `series.json` itself, `healthy_file` is True (the comics are
fine) and `/api/problem-files/delete` refuses it by basename; the actions are
Refresh (`/api/series-json/refresh`, then a re-check) and Dismiss/Remove. A scan
clears rows only under roots it actually walked, for the unmounted-library
reason below. `is_hidden` needs the **full** path, not a bare directory name.

**Refresh all** (`POST /api/problem-files/refresh-series-json-all`,
`core.series_json_check.refresh_all`) is the single-folder Refresh in a loop over
every un-dismissed row, off-request on a thread. It shares the series.json
`_scan_lock` with the scan (both rewrite the same rows). "Corrected" is the
**re-check's** verdict (`recheck_file`), never the provider call's — a provider can
answer and still have no description — so the folders that stay blank are
reported with the checker's own reason. `force` is never passed, so hand edits
are kept. One folder failing never stops the run. The report is kept in memory
for the last run (`last_bulk_result`, read from `…/result`) and shown on the page;
it is not a ledger — the still-listed rows are. **The thread must push an
application context** (as with `core.problem_replacements`): the provider lookups
read `current_app`, and without one every lookup raised and was swallowed, so every
folder reported "No provider returned data" while a request-thread Refresh worked. The per-folder line is DEBUG and
the run logs one INFO summary naming the first 20 failures. Tests:
`tests/integration/test_series_json_bulk_refresh.py`, `TestRefreshAllSeriesJson`
in `tests/routes/test_problem_files_routes.py`.

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

### Library Health Scan

Problem Files is report-only, so an empty page meant "nothing has failed", not
"the library is clean". `core/library_health.py` is the opt-in answer
(`library_health_scan_enabled`, default **off** — it reads every comic). Findings
land in the existing `problem_files` ledger under three new sources, `integrity`
(a CBZ whose CRCs or pages fail), `orphan` (a `.cbr` beside its `.cbz`, or a
comic left in TARGET/WATCH for over a day) and `empty-folder` (a library folder
with no comics: nothing, or only cvinfo/series.json/folder art). Duplicates are *derived* and shown on
`/problem-files/duplicates`; they are not ledger rows.

Things that look arbitrary and are not:

- **Rolling and resumable, with no cursor.** Each run checks `library_health_batch`
  files inside a wall-clock budget, stalest first; "needs checking" is derived
  from `file_health` against `file_index` every time. Results expire after
  `library_health_recheck_days`, or bit rot in an untouched file is never found.
- **Results live in `file_health`, not on `file_index`.** A rebuild does
  `DELETE FROM file_index`, and a content hash is the expensive thing to lose. A
  row is trusted only while its `(size, mtime)` equal the index's, so a rewritten
  file is re-checked with no hook on whatever rewrote it. `file_health.path` is a
  path column like the six in **Path References** but is deliberately *not* in
  `move_path_references`: a moved file is simply re-hashed, and
  `prune_health_rows` drops the old row (never while the index is empty — a
  rebuild in progress looks like "everything was deleted").
- **A mount going away must not flood the ledger.** Unreachable library roots are
  excluded from the query up front; ten consecutive `OSError`s abort the batch
  and the held-back results are dropped, not recorded. A file missing from a
  folder that exists is stamped `missing` with **no** ledger row, because
  `list_problems` would prune it on sight (`_is_definitely_gone`).
- **Stamp after recording.** A file is stamped in `file_health` only after its
  outcome is written, the same rule as `core.reading_list_sync.apply`.
- **Heavy walks are daily, the batch is not.** `run_scheduled_scan` is a 30-minute
  heartbeat gated on the persisted `last_run_at` (same reasoning as
  `core.comicvine_db_update`: an interval trigger restarts with the process and
  there is no jobstore); series.json and staging walks run at most every
  `CHEAP_CHECK_INTERVAL`. "Run scan now" does both. One scan at a time
  (`claim_scan`), released in a `finally`.
- **`.zip` is CRC-checked but never "no pages".** Only a `.cbz` is certainly a
  comic. CBR/RAR/PDF are hashed and passed — CLU has no RAR CRC check.
- **Likely-duplicate names strip only copy markers.** A trailing `(n)` and the
  `_n` a download name-claim adds. Stripping every parenthetical would fold
  `(Variant A)` into `(Variant B)`, and the sign of `-001` is kept so it never
  folds into `001`.
- **A tagged group needs more than matching tags.** Series + issue (zero
  stripped, sign kept) + a *known* year + publisher, **and** the filenames must
  agree on the series (`issue_stem`: the name up to the issue number). An event
  whose issues were all matched to the parent series (Future Quest and its
  "Aquaman / Jabberjaw Special" one-shots) carries one series/number/year across
  different comics; a tag that contradicts the filename is a tagging problem, not
  a duplicate. A blank year is not a wildcard.
- **A dismissed group is keyed on its member set**, so a new copy re-opens it.
- **`resolve_group` re-derives the group and validates every path** against it,
  refuses the keeper in the trash list and protected paths, and goes through
  `move_to_trash` + `forget_deleted_paths` (never a raw index delete).
- **Staging inside a library is skipped**, as in `collect_target_candidates`.
- **`empty-folder` is the one source whose path is a directory.** Found by
  `scan_empty_folders` (a cheap check, and the page's *Scan empty folders*
  button on its own `_empty_scan_lock`). One top-down walk prunes hidden and
  protected folders — both count as *content*, since "did not look" must not
  become "nothing there" — then a reverse pass marks a folder empty when its
  files are only leftovers and every child is empty. Only the **topmost** empty
  folder is listed; a library root never is. Only names CLU writes are leftovers
  (`SIDECAR_NAMES`, folder art); any other file keeps the folder off the list.
  New rows are capped per scan (`MAX_EMPTY_FOLDER_ROWS`) so cosmetic rows cannot
  fill `MAX_OPEN_PROBLEMS` ahead of damage. `/api/problem-files/delete` takes a
  directory only when it is a listed `empty-folder` row **and** `inspect_folder`
  still says empty (409 and the row is dropped otherwise), then
  `clear_problems_under` drops rows inside it — a series.json row there would
  otherwise be listed as unreachable forever (missing parent ≠ proof of deletion).
- **The badge poll is cheap**: duplicate counts are stored when a scan finishes
  (`library_health_state`, a `user_preferences` JSON), never recomputed per poll.
  The score is `100 − capped penalties` (`compute_score`); red means open
  integrity rows.

Tests: `tests/integration/test_library_health.py`,
`tests/routes/test_library_health_routes.py`, `TestScanDrivenClasses` in
`tests/unit/test_problem_files_classify.py`, and the wrapper/registration
assertions in `tests/unit/test_db_shutdown_and_schedule.py`.
