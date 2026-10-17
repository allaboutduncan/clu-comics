---
paths:
  - "core/job_queue.py"
  - "routes/files.py"
  - "routes/metadata.py"
  - "core/bulk_metadata.py"
  - "routes/bulk_metadata.py"
  - "core/app_state.py"
  - "static/js/files.js"
---

# File job queue

### File Job Queue

A drag of 70 files onto a folder with a `cvinfo` used to send 70 `/move`
requests, each starting its own unbounded thread that moved the file, fetched
metadata, rewrote the CBZ and reconciled the whole series. About 70 threads in
one gunicorn worker contended for the GIL and the single SQLite writer, so the
whole app slowed down. Threads parked on the Metron limiter went quiet past
`STALE_TIMEOUT`, so the page reported "N moves failed" while they were still
working, and they raced each other writing the same `cvinfo`.

Now `POST /move-batch` (and `/move`, as a batch of one) runs in two phases:

- **Move phase**: one op, one thread, run at once. Every item is moved with
  `update_index_on_move(..., reconcile=False)`, then each touched series is
  reconciled **once**, the same pattern as `_do_rename_batch`. This phase is
  deliberately *not* queued: it is fast, and a drop must not wait behind a
  70-file tagging job.
- **Tag phase**: if auto-metadata-on-move is on, one `"metadata"` job is
  submitted to `core.job_queue.file_jobs` for the whole batch.
  `_do_tag_batch` runs the provider chain one item at a time and updates its
  op **before every item**; that update is the heartbeat that keeps it clear of
  `STALE_TIMEOUT`.

Things that look arbitrary and are not:

- **One worker, on purpose.** The work is paced by process-wide provider rate
  limits, so a second worker would only wait on the same limiter. A single
  worker also serialises `cvinfo` writes.
- **`queued` is a registry status.** `register_operation(status="queued")` and
  `start_operation()` exist for it. The stale sweep only touches `running`, so
  a job waiting behind a long batch is never marked stalled.
- **The tag op carries the mover's `user_id`.** It is registered on the worker
  thread, where identity would otherwise resolve to the owner.
- **One refusal rejects the whole batch** before anything moves, as in
  `custom_rename_batch`.
- **Remove XML (`/cbz-bulk-clear-comicinfo`) is a `file_jobs` job too.** Every
  file is a full CBZ rewrite. Its response carries `queued: true` when the job
  is waiting behind another, so the toast can say so instead of claiming it has
  started. A file with no ComicInfo.xml counts as *skipped*, not failed.
- **Bulk metadata (`core.bulk_metadata.start_bulk_job`) is a `file_jobs` job.**
  Scope expansion and the `bulk_metadata_job` row still happen in the request;
  only the runner is queued. `/api/bulk-metadata/progress` passes the live op's
  status through, so a waiting job reports `queued` and the progress modal
  shows "Queued: N ahead".
- **Batch rename (`/custom-rename-batch`) is a `file_jobs` job, even though it
  is fast.** That is for ordering, not load: a rename running beside a queued
  tagging or Remove XML job would move a path out from under it. The cost is
  that a rename submitted behind a long tagging batch waits for it, and the
  response's `queued` flag lets the modal say so. `/rename-directory` is still
  synchronous in the request and is not queued.

Tests: `tests/unit/test_job_queue.py`; `TestMoveBatch`, `TestDoMoveBatch`,
`TestDoTagBatch` and `TestCustomRenameBatch` in `tests/routes/test_files_routes.py`;
`TestBulkClearComicInfo` and `TestRemoveComicInfoBatchJob` in
`tests/routes/test_metadata_routes.py`; `TestRunsOnFileJobQueue` in
`tests/routes/test_bulk_metadata.py`.
