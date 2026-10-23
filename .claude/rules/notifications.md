---
paths:
  - "core/notifications.py"
  - "routes/notifications.py"
  - "api.py"
  - "models/usenet.py"
  - "models/dcpp.py"
  - "routes/download_clients.py"
  - "core/download_utils.py"
  - "templates/status.html"
  - "app.py"
---

# Notification hook sites and the status page stores

### Notification Hook Sites

`core/notifications.py` imports `apprise` lazily and every path swallows its exceptions: a notification must never break the download it reports on.

Downloads settle in **three independent places** — there is no single choke
point. A new download path needs its own hook:

| Path | Terminal status set at |
|------|------------------------|
| In-process HTTP (GetComics/Pixeldrain/MEGA/ComicBookPlus) | `api.py` success in `process_download`; failure after the `is_cancel_requested` guard *and* the `_schedule_auto_retry` gate that follows `set_error_status` |
| Usenet (SABnzbd/NZBGet) | `models/usenet.py` `_set_status` |
| DC++ / AirDC++ | `models/dcpp.py` `_set_status` |

Both pollers delegate to the shared `core.notifications.notify_download_terminal()`
and must call it **outside** their `_jobs_lock`.

The same three sites also write the outcome onto the **download run report**
(`core.download_runs.set_download_outcome`, shown on `/status` → Scheduled
Runs), under the same rules: 'failed' only after the cancel guard and the
auto-retry gate (a parked retry records 'retrying'), cancels through api.py's
`mark_cancelled` wrapper, and the pollers via `_record_run_outcome` outside the
lock. The sweep and `_enqueue_weekly_pack` write the item and `run.flush()`
**before** `download_queue.put`, or a fast worker settles a download whose item
does not exist yet. Asserted in `tests/unit/test_download_run_hooks.py`.

Run reports are bounded on purpose: skips are counters, never rows; at most
`RUN_ITEM_CAP` items a run; `download_run_history_keep` runs per kind and
nothing older than 90 days, pruned when a run starts.

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

#### The same three stores back the status page

Those three tables are also the three *stores* `/status` renders from, and a
control that acts on the whole table has to reach all three:
`download_progress` in api.py, `models.usenet.usenet_downloads` and
`models.dcpp.dcpp_downloads`. `/clear_downloads` and `/clear_failed_downloads`
(api.py) only ever knew about the first, so **Clear Completed** left every
managed-client row where it was — and each one still read "Complete", which is
what made the button look broken. `POST /api/download-clients/downloads/clear`
covers the other two in one call, and the page fires both.

- **`complete_no_move` clears as *completed*, not as failed.** The download
  finished; what failed is CLU finding the file afterwards, and it cannot
  resolve that on the user's behalf — the client may be on another host or
  writing somewhere CLU cannot see. The buckets live once, in
  `core.download_utils.CLIENT_CLEAR_BUCKETS`, because Usenet and DC++ share one
  status vocabulary and the page already renders both through one row builder.
- **Clearing a DC++ job must delete its ledger row**, exactly as
  `dismiss_dcpp_job` does: an unresolved job keeps that row *so that* a restart
  brings it back, so forgetting it in memory alone would put every cleared row
  back on the page after the next restart. A clean completion deleted its own
  row when it finished, so the delete is a no-op there.
- **Usenet has no ledger and no per-row dismiss.** Its jobs are in memory only
  and `_set_status` merely relabels, so before this nothing removed a Usenet row
  at all — not even a clean `complete` — and the page passed no `dismissUrl` for
  those rows. The bulk clear is currently the only way to remove one.
- **Untracked DC++ bundles are never cleared.** They mirror AirDC++'s own queue
  out of `_bundle_snapshot`, not `dcpp_downloads`, and they are not CLU's to
  forget.
