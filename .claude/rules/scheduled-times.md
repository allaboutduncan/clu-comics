---
paths:
  - "core/user_time.py"
  - "app.py"
  - "routes/downloads.py"
  - "models/timeline.py"
  - "models/stats.py"
  - "templates/schedules.html"
  - "templates/config.html"
  - "templates/base.html"
---

# Scheduled job times (UTC everywhere)

### Scheduled Job Times

**Every schedule time in the `schedules` table is UTC, and every job triggers in
UTC.** The Schedules page has always written it that way (`CLU.localToUtc` on
save, `CLU.utcToLocal` on load), but nothing on the server honoured it, so one
sentence on that page could quote three different clocks:
`CronTrigger(hour=, minute=)` was built with no `timezone=` and therefore
inherited the scheduler's — `tzlocal`'s answer for the host, and nothing in the
Dockerfile, compose file or `entrypoint.sh` sets `TZ`; `get_next_run_for_job`
printed `job.next_run_time` raw; and `last_run` came straight out of SQLite as
UTC. On a non-UTC host the job also *fired* at the wrong instant, which is why
no display-only fix was possible.

`core/user_time.py` is the **one** parser for the `timezone` preference — a
**fixed offset** ("UTC", `-5`, `5.5`), with no zone name and therefore no DST.
It had been copied inline into `models/timeline.py` and `models/stats.py`, which
is why it is worth one module; both now delegate. Nothing in it raises: a
missing or corrupt preference reads as UTC, because it is called from GET routes
that render a page and from bare APScheduler threads, and an out-of-range value
is clamped rather than used to shift every timestamp by hundreds of hours.

Things that look arbitrary and are not:

- **`get_next_run_for_job` is the only next-run formatter** (seven GET routes
  call it inline), and it normalises with `astimezone` **before** applying the
  offset. APScheduler hands back an aware datetime in the *scheduler's* zone, so
  assuming UTC would be right only on a UTC host. It keeps its
  swallow-everything contract and its `"Not scheduled"` fallback: a clock that
  cannot be read must not 500 a page.
- **`jitter` was dead for its whole life.** It was passed to `add_job()`
  alongside a `CronTrigger` *instance*, and `BaseScheduler._create_trigger`
  returns the instance before `trigger_args` is ever read. Do not "fix" it:
  `CronTrigger._apply_jitter` runs inside `get_next_fire_time`, so a live jitter
  lands in `job.next_run_time` and makes "Next" read up to 30 minutes after the
  time in the input box — indistinguishable, to a user, from this bug being
  unfixed. If the thundering-herd concern is real it comes back as
  `CronTrigger(..., jitter=)` plus wording on the page.
- **`last_successful_pack` is not converted.** It is a pack *date*, not a
  timestamp; shifting it by an offset would name a different week.
- **Conversion happens at the route boundary, never in
  `core.database.get_schedule`.** The raw UTC string is what non-display callers
  compare against.
- **`app.py`'s daily DB backup (`CronTrigger(hour=3, minute=17)`) stays on host
  time** — it is not user-configured and nothing displays it.
- **The Schedules page shows the server's own clock, and it has to.** The
  preference is a fixed offset, so it cannot follow DST: a user in US Central
  needs `-5` from March to November and `-6` the rest of the year. Pick the
  wrong one and *every displayed time stays self-consistent* — the input box
  and "Next" agree, because both are the same offset — while every schedule
  fires an hour out, and the only symptom is a job that appears not to run.
  That is a real report. `server_now_utc` + `host_offset_label()` put the
  server's time, UTC, and the host's own offset on the page so the discrepancy
  is readable instead of deduced. The clock ticks locally from one server
  instant: there is nothing to fetch, so it is **not** a `CLU.startPoll` case,
  and it formats by shifting the instant and reading its UTC fields so the
  *viewer's* zone never enters into it — a phone in another country must show
  the same page.
- **The three JS helpers live once, in `base.html`'s `CLU` block**
  (`CLU.tzOffsetHours`, `CLU.utcToLocal`, `CLU.localToUtc`). There were two
  copies, and the `config.html` one had drifted into dead code while the Komga
  time input on that very page converted nothing. `CLU.tzOffsetHours()` reads an
  `id="timezone"` element and returns 0 when absent — which fails *silently*,
  showing raw UTC, so any template with a schedule time input must carry the
  holder. `tests/unit/test_schedules_page_time_conversion.py` asserts that
  across every template.
- **`/api/get-getcomics-schedule` and `/api/save-getcomics-schedule` belong to
  `routes/downloads.py` only.** They were declared a second time in `app.py`;
  the blueprint registers first, so those copies were unreachable and had
  already drifted. Do not re-add them.

app.py cannot be imported in tests, so the trigger timezone, the absent jitter,
the `describe_age` call and the absence of those routes are asserted
structurally in `tests/unit/test_schedule_timezone_wiring.py`.
