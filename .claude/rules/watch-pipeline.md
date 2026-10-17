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
---

# WATCH pipeline: archives, name claims, orphan sweep, moves, conversion

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
