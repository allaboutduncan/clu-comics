---
paths:
  - "core/database.py"
  - "core/db_health.py"
  - "core/db_maintenance.py"
  - "core/db_repair.py"
  - "core/db_lock.py"
  - "routes/database.py"
  - "tools/repair_db.py"
  - "static/js/database_panel.js"
  - "app.py"
---

# Database health, maintenance and repair

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
