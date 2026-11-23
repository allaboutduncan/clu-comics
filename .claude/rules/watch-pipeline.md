---
paths:
  - "monitor.py"
  - "api.py"
  - "helpers/**"
  - "core/download_utils.py"
  - "cbz_ops/convert.py"
  - "cbz_ops/single_file.py"
  - "convert.py"
  - "routes/files.py"
  - "app.py"
  - "core/monitor_supervisor.py"
  - "routes/folder_monitor.py"
---

### Folder Monitor lifecycle

Whether `monitor.py` runs is the `monitor_enabled` setting (`MONITOR_ENABLED`
in `SETTINGS_SCHEMA`, default **off**), not the `MONITOR` env var. The env var
is no longer read: releases 7.0 to 7.3 imported `MONITOR=yes` into the setting
once, and that import is gone. The debug package still reports the raw value,
for the "I set MONITOR=yes and nothing happens" report.
`core.download_utils.monitor_enabled()` reads the same
setting, so api.py's DEFER/FULL decision and the running process cannot
disagree for longer than the 5s settings cache.

`core/monitor_supervisor.py` is a daemon thread in the gunicorn worker that
starts the child while the setting is on and terminates it when it goes off.
`POST /api/config/folder-monitor` and the File Processing save `poke()` it.
`POST /api/config/folder-paths` saves WATCH/TARGET on their own (absent key =
unchanged) and does not poke: the monitor notices a WATCH change by itself.
Things that look arbitrary and are not:

- **The monitor governs itself; the supervisor's `terminate()` is a fallback.**
  `monitor.exit_reason` stops the process when the setting goes off *and* when
  `os.getppid()` changes. A gunicorn worker killed on `--timeout` leaves an
  orphan with no supervisor to kill it, and two monitors on one WATCH race each
  other's moves, which happened on every respawn before this.
- **`monitor.lock` in CONFIG_DIR makes it single-instance.** A second monitor
  exits with `EXIT_ALREADY_RUNNING` (75), which the supervisor retries on a
  short fixed delay and never counts as a crash. That is the normal answer
  while an orphan winds down. `restart_app` stops the supervisor before
  `os.execv`, because execv keeps the pid and the old monitor would never notice.
- **A failed settings read is "unknown", not "off".** Both sides use
  `core.settings.peek_setting`, which returns `None` when the store cannot be
  read. `get_bool` returns the default (False) instead, and acting on that
  would stop monitoring on every "database is locked".
- **WATCH changes are applied in place.** `monitor.reschedule_if_changed`
  re-points the observer when WATCH or READ_SUBDIRECTORIES changes, then runs a
  reconcile sweep over the new folder. The observer used to be scheduled once
  at startup, so a WATCH change needed a container restart. Onboarding sets
  WATCH and enables monitoring in one pass.
- **Exit 0 restarts at once if the setting is still on.** The child exits 0
  when it reads the setting as off. The supervisor's cached read may lag, so
  this is not treated as a crash; the next read settles it.

Saving Metron credentials returns `suggest_enable_monitor` when monitoring is
off, and the settings page offers to turn it on with a modal. Tests:
`tests/unit/test_monitor_supervisor.py`, `test_monitor_lifecycle.py`,
`test_monitor_supervisor_wiring.py` (AST over
app.py) and `tests/routes/test_folder_monitor_routes.py`.

# WATCH pipeline: archives, name claims, orphan sweep, moves, conversion

### Archives in WATCH

Unpacking is **unconditional** — the old `AUTO_UNPACK` setting is gone (it is
named only in `core.settings.NON_SCHEMA_INI_KEYS`, the record of legacy keys).
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

#### A release folder is offered to the unwrapper by every file in it (#844)

`.r00`, `.nfo` and `file_id.diz` are not in `ARCHIVE_EXTS`, so their own file
events never reached `_process_archive`'s folder check and each part was
renamed and moved to TARGET by itself. The reconcile sweep offers folders
first, which is why this only showed where file events arrive (not on NFS).
`_process_file` therefore offers the parent of **any** file in a WATCH
subfolder to `_maybe_unwrap_release_folder` before the plain path.

- **A folder in `_in_flight_dirs` is skipped there.** The unwrapper's own
  staged output comes back through `_process_file` while the claim is held;
  without the skip it would be answered "claimed" and never moved.
- **A failed unwrap is `_report_unwrap_failure`**: a `SOURCE_UNPACK` row on the
  *folder* (`CLASS_RELEASE_UNWRAP_FAILED` / `_PARTIAL`, the only directory that
  source lists), cleared by a later full success, and an exponential backoff
  capped at `ORPHAN_MIN_AGE_SECONDS`. It was a fixed 5-minute cooldown with a
  WARNING per attempt and nothing on any page. The WARNING is first-failure
  only now.
- **`_failed_unwraps` is `(fingerprint, failures, retry_at)`**, the fingerprint
  being the archive *parts* only. A missing volume arriving retries at once; a
  comic staged by a partial unwrap leaving the folder must not, or the 24h
  partial hold would re-hand-off the same comics on the next sweep.

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
every sweep). So do the other movers onto a mount: `/move` and `/move-batch`
(`routes.files._move_item`), `models.comicvine.auto_move_file`, the
replacement staging and rollback in `core.problem_replacements`, trash restore,
and the Usenet import into WATCH. Each comic moved onto the library then gets
`match_parent_permissions`, or a download that arrived `root:0600` stays
unreadable to thumbnails. `helpers.move_tree` is the folder counterpart (same
hidden-sibling staging, same tolerated `copystat`); it refuses an existing
destination instead of nesting into it as `shutil.move` did. Tests:
`tests/unit/test_library_moves_cross_device.py`,
`tests/routes/test_move_batch_cross_device.py`.

- **Filing a download never overwrites, and never invents a name (#731).**
  `process_incoming_wanted_issues` calls `move_file(src, dst, replace=False)`;
  the refusal comes from the rename itself (`helpers.no_clobber.rename_no_clobber`:
  Windows `os.rename`, `renameat2(RENAME_NOREPLACE)`, `link`+`unlink`, and only
  then check-then-rename on a mount with neither), not from an `os.path.exists`
  check made earlier. A download whose name is taken **stays in TARGET**: it
  used to be filed as `Name (1)`, a second copy of an issue the library already
  had. The handler `continue`s before `moved_count`, the queue-stamp clear and
  the index insert, and announces through `app_state.first_filing_refusal`
  (WARNING once per file, DEBUG after), because the file is matched again on
  every pass. The rename *after* filing is different: the comic is already in
  the library, so on a collision it keeps the name it arrived with.
  `rename_comic_from_metadata` uses the same primitive. `replace=True` stays
  the default: every in-place CBZ rewrite relies on the overwrite.
  `rename_files`/`rename_file` still use `free_destination` and ` (1)`.
  **A manual `/move` or `/move-batch` overwrites on purpose**
  (`routes.files._move_item`): dragging a better copy onto a comic in the File
  Manager is how users replace one. Do not pass `replace=False` there.
- **Only the metadata failure is demoted.** A failed `copyfile` and a source
  that cannot be removed both still raise: the second is the #582 failure mode
  above, and in TARGET a leftover source is re-matched forever.
- **It is not a replacement for `monitor.move_download()`.** That one *removes*
  a partial copy at `dst`; this one *keeps* a complete one. Different questions.
- **Timestamps are still copied when the mount allows it** — `copystat` is
  attempted every time and only its failure is swallowed, to a DEBUG line.
- **The copy never writes onto `dst`.** It goes to a hidden
  `.<32 hex>.clu_incoming` sibling and is `os.replace`'d over `dst`. The name
  must not embed `dst`'s: a component is capped at 255 bytes, and a long or
  non-ASCII comic name plus a suffix hits ENAMETOOLONG. Every
  in-place CBZ rewrite is staged in `CACHE_DIR/tmp` and lands here
  cross-device; copying straight onto the comic truncated it first, so a
  disk-full or NAS drop mid-copy lost it. A failed copy removes the sibling; a
  crash leftover is hidden (never indexed or moved) and reaped by the next
  cross-device move into that folder once a day old (`STALE_INCOMING_SECONDS`).
  **The reaper takes only `move_file`'s own shape** (`_MOVE_FILE_STAGED`: `.` +
  32 lowercase hex + `.clu_incoming`, fullmatch) and only regular files. The
  fixed-name siblings of `core.problem_replacements` (`.<name>.clu_incoming`,
  held across the trash step) and `core.comicvine_db_update` share the suffix
  and must never be reaped; those modules clear their own. A `move_tree` stage
  is a directory, so it is skipped too. **Age is `max(st_mtime, st_ctime)`**,
  never mtime alone: `copystat` stamps the source's old mtime on a sibling
  created a moment ago, so by mtime a live copy reads as a day stale. ctime is
  recent for a just-staged file on both POSIX (inode change, which copystat
  bumps) and Windows (creation time).
  The fallback refuses when `src` and `dst` are the same file (a case-only
  rename): the copy would land on it and `os.remove(src)` delete it.
  Tests: `tests/unit/test_move_file_atomic_overwrite.py`.

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

A failure is handed to the Library Health page under `SOURCE_CONVERT`
(`convert_single_rar_file(..., problem_source=)`), and a success clears the row.
The rebuild fallback passes no source on purpose: it records its own row against
the `.cbz` it started from, and two rows for one failure is how they drift apart.
Retry for that source points at **Rebuild**, which is
`CLU.executeStreamingOp('single_file', path)` — literally `convert_to_cbz`, and
already streamed; re-running it inside the request would outlive gunicorn's
timeout on a 500MB pack.

#### A PDF is converted in WATCH, by every route, or it stays there

PDF→CBZ used to live only in `_handoff_unwrapped`, so only a PDF that came out
of a multipart release *folder* was converted. One extracted from a pack by
`_unrar_file`, or dropped loose, was renamed and moved to TARGET as a `.pdf`.
And when that one conversion failed it logged "produced no CBZ" and returned:
the parts were already cleaned up, the folder classified as `NORMAL`, and the
next sweep moved the raw PDF (#834).

- **The step is in `_process_file`** (`_pdf_to_comic`, gated on AUTOCONVERT),
  ahead of the rename, so the route does not matter. `_handoff_unwrapped` only
  claims the staged file and calls `_process_file`, and backs a False off
  itself, because it does not come through `_handle_file_if_complete`.
- **It converts before the move, where a `.cbr` converts after it.** A PDF that
  will not convert must not reach TARGET, where the wanted sweep would file it
  into the library. Failure is `SOURCE_CONVERT` + `CLASS_PDF_FAILED` on the
  WATCH path, a False from `_process_file` and therefore a `_failed_files`
  backoff; success clears the row.
- **`process_pdf_file` returns a bool and is all or nothing**, the same
  contract as `convert_to_cbz`: True only when every page rendered and saved,
  the CBZ was written and the PDF was removed. It returned `None` and swallowed
  everything, so a failed CBZ write was followed by deleting the PDF, and a page
  that failed to save was just absent from the CBZ. Do not test
  `os.path.exists(<base>.cbz)` instead.
- **Its scratch folder is `.temp_<name>`** (`cbz_ops.pdf.scratch_dir_for`), which
  `is_scratch_dir` matches. It was a bare `<name>/` beside the PDF, which in
  WATCH is an ordinary sub-folder of loose page images.
- **A `.cbz` of the same name is never overwritten.** `process_pdf_file` refuses,
  and `_pdf_to_comic` returns before opening the PDF and records no problem: the
  backoff retries once that comic has moved on.

Tests: the `pdf_handler` tests in `tests/unit/test_monitor.py`,
`TestProcessPdfFileOutcome` in `tests/mocked/test_pdf.py`.

#### A RAR or 7z named `.cbz` is recognised by its bytes

Every conversion decision used to be made on the extension, so a RAR or 7-Zip
archive shipped as `.cbz` (the report was a pack of six 7z files) reached the
library as a "CBZ" nothing could preview, tag or edit ("File is not a zip
file"), while a reader that sniffs content opened it.
`helpers.mislabelled_cbz_format` (a six-byte read, `'rar'`/`'7z'`/None) is
checked in three places. Both formats convert through the RAR ladder, whose
tools read content:

- `monitor._move_file` renames it to `.cbr` in TARGET, so the existing CBR
  branch converts it. With AUTOCONVERT off a RAR is still renamed (that is its
  right label); a 7z keeps its name and is only logged.
- `routes.files._do_move_batch` lists it on Library Health (`SOURCE_CONVERT`,
  advice Rebuild). The move phase is the step that always runs.
- `routes.files._do_tag_batch` converts it first when AUTOCONVERT is on, and
  otherwise skips the providers for it.

`api._finish_download_in_watch` still decides on the name; the two `routes`
checks are the net for files that arrive that way.

#### A RAR extraction is all or nothing

`helpers.extract_rar_with_unar` used to return `(True, 0)` for **any** non-zero
exit that left one file behind (`failed_count` was only ever counted for
`unar`). Every caller read that as a clean extraction, and every caller deletes
the source on success: a CBR that gave up 39 of its 104 pages became a 39-page
CBZ, was logged "Successfully converted", and the original was removed. The same
file gave 80 pages on the next run, so this is not only about damaged archives.

- **An attempt counts only when** the tool exited 0, reported no failed member,
  and left at least as many page images as `list_rar_entries` says the archive
  holds. The page count is checked because an exit status is one tool's opinion;
  an archive nobody can list is "unknown" and judged on the exit status alone.
- **A partial attempt is discarded and the next tool is tried** (unrar, 7z,
  unar). When none extracts everything the result is `(False, n)`: `n` is the
  best attempt's shortfall (at least 1), and 0 still means nothing came out.
  The output directory gains nothing either way. Each discarded attempt logs
  one WARNING with the tool, its exit code, the page counts and the tail of the
  tool's own output, which was never logged before.
- **Discarding means emptying a directory, so it must be one the call owns.** An
  empty directory handed in is used as-is; a directory that already holds
  something gets a hidden `.clu_rar_*` stage inside it and the result is moved
  out on success. `monitor._unrar_file` extracted straight into WATCH and
  `unwrap_release` extracts several parts into one layer dir.
  `_unrar_file` now stages into `.clu_unrar_<name>` and promotes, like
  `unzip_file`.
- **Always unpack the tuple.** `cbz_ops/rebuild.py` tested the bare tuple, which
  is always truthy, so even a total failure wrote an empty CBZ and removed the
  source.
- **No caller is offered the partial payload.** `convert_single_rar_file` (all
  three copies) returns False before anything is zipped, so there is never an
  incomplete `.cbz` for the sibling rule below to mistake for the real one.
  The failure is a `SOURCE_CONVERT` row with `CLASS_RAR_PARTIAL`
  (`cbz_ops/convert.py`, the folder-level run, records it too), and
  `cbz_ops.single_file`'s `__main__` exits non-zero so `/stream` does not end in
  a success toast.

- **A tool that hangs is a failed attempt.** Every archive tool runs through
  `helpers.run_archive_tool` (timeout required, `stdin=DEVNULL`, own session,
  so a password prompt reads EOF; unrar also gets `-p-`). They had no timeout,
  so one damaged archive or stalled NAS read blocked the single-threaded
  monitor for good. Listing gets `ARCHIVE_LIST_TIMEOUT` (60s); an extraction
  gets `archive_extract_timeout()`: the whole volume set's size at 4 MB/s,
  floor 180s, ceiling 3600s. A timeout is discarded like a partial attempt
  (one WARNING naming tool, archive and timeout; next tool; `(False, n)`), so
  the backoff and the Library Health row follow from the existing contract.
  `unwrap._extract_zip`'s 7z/unar fallback stages each attempt in a hidden
  `.clu_zip_*` dir, because its `out_dir` is shared by a whole layer.
  `tests/unit/test_archive_tool_timeouts.py` is the AST ratchet.

Tests: `tests/unit/test_rar_extract_strict.py`,
`tests/mocked/test_convert_to_cbz_contract.py`.

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
