---
paths:
  - "core/comicvine_db_update.py"
  - "routes/metadata.py"
  - "models/comicvine_sqlite.py"
  - "models/comicvine_source.py"
  - "templates/config.html"
  - "app.py"
  - "core/auth.py"
---

# Local ComicVine DB auto-update

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
