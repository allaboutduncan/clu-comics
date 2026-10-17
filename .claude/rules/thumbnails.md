---
paths:
  - "core/thumbnail_cache.py"
  - "core/folder_thumbnails.py"
  - "cbz_ops/**"
  - "app.py"
  - "routes/collection.py"
  - "wrapped.py"
  - "entrypoint.sh"
  - "Dockerfile"
  - "tools/make_thumb_style_samples.py"
  - "static/js/collection.js"
  - "static/css/collection.css"
  - "templates/config.html"
---

# Per-comic thumbnail cache and folder thumbnails

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
- **…but it is conditioned on the job really existing.** The executor queue is
  in memory, so a restart or worker kill orphans every queued row, which then
  said `processing` forever: the route answered `loading.svg` and the startup
  scan skipped it (20,000 stuck rows on one install). Jobs are submitted through
  `submit_thumbnail_job`, and both the route and the scan trust a `processing`
  row only while `is_in_flight(path)` says so. This relies on gunicorn's single
  worker (`-w 1`).
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
- **`/api/folder-thumbnail` confines before it opens anything, and the order is
  load-bearing.** It takes the path from the query string, so `os.path.normpath`
  is not a guard — it normalises a path, it does not confine one, and that alone
  is what the route once had: any logged-in user could read any file the process
  could read, `/config` and the provider credentials included
  (GHSA-vhvw-93fg-whm8). Three checks, none interchangeable:
  `is_allowed_path` (realpath, so a symlink out of a library is refused too);
  the per-user grant, asked of the **containing folder** in `mode='browse'`
  rather than of the image in `mode='full'` — the grid draws art for a
  *traverse*-only ancestor on the way down to a grant, and an ancestor's
  `folder.png` is never itself under one; and the name, which must be exactly
  what `find_folder_thumbnail` produces. They run **before** the existence
  checks, so a denied path is indistinguishable from a missing one. There is no
  default mimetype: `mime_types.get(ext, 'image/jpeg')` served a file of any
  type at all, relabelled.
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
