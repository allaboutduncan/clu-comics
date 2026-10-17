---
paths:
  - "app.py"
  - "routes/downloads.py"
  - "models/getcomics.py"
  - "core/download_utils.py"
  - "core/app_state.py"
  - "models/mega.py"
  - "models/usenet.py"
  - "models/dcpp.py"
  - "models/download_sources.py"
  - "core/wanted_reading_lists.py"
---

# GetComics sweep, split posts and scoring

### Scoped Missing-Issue Checks

`app.scheduled_getcomics_download` is one body serving two callers: the nightly
sweep, and the "Check for Missing Issues" button on a series page
(`only_series_id=<id>`). The scoped run differs in three ways, all asserted in
`tests/unit/test_series_scoped_getcomics_run.py` because app.py cannot be
imported in tests:

- It narrows `mapped_series` to the one id.
- It **ignores the series' Monitor toggle.** That toggle exists to keep the
  *unattended* sweep off a series; clicking the button is an explicit request.
- It **must not call `update_last_getcomics_run()`** — that stamp belongs to the
  schedule, and moving it would make the nightly sweep look as though it had
  already run.

The scope parameter is `only_series_id`, not `series_id`, because the per-series
loop rebinds `series_id` to the series it is processing — a parameter of that
name is shadowed at the first iteration and the scoping silently lost.

Progress goes through the operations registry (`core/app_state.py`), so the run
shows up in the header indicator like any other background job. Pages that
follow an operation they started poll **`/api/operation/<op_id>`**, never
`/api/operations`: the latter *clears* the pending notification queue as a side
effect, so it can only ever have the one poller in `base.html`. The sweep
registers its own `op_id` when the caller supplies none, so the cron job and
`/api/run-getcomics-now` are visible too — a run that can last hours used to be
invisible unless it came from the per-series button.

#### One sweep per scope, and a ceiling on what one sweep may queue

**Every de-duplication the sweep does is in a local.** `queued_download_urls`
and `downloaded_ranges` die with the call, so a second concurrent sweep starts
with both empty and re-queues everything the first is still working through.
Nothing prevented that: one reported run took **9,408 s (2h37m)** — long enough
for the nightly cron to fire on top of it — and `/api/run-getcomics-now` had no
in-progress check at all.

`core.app_state` holds the claims (**not** app.py, so the routes can ask without
importing app). The key comes from `getcomics_sweep_scope(only_series_id)`:
`"all"` for a full sweep, `"series:<id>"` for a scoped run.

- **A full sweep does not block the per-series button.** Clicking it is an
  explicit request about one series, and making the user wait out a sweep with
  hours left is worse than the single duplicate it can cost. Two runs over the
  *same* scope are what must never overlap.
- **A dry run neither claims nor blocks.** The simulation queues nothing.
- **The claim is released in a `finally`**, or one failed sweep locks its scope
  for the life of the process.
- Both routes answer **409** rather than starting a run the sweep would
  immediately stand down from.

`MAX_DOWNLOADS_PER_SWEEP` is a blast-radius limit, not a throughput limit: a run
that goes wrong goes wrong for its whole length, and the reported sweep queued
203 downloads of which a handful of files accounted for most. Hitting it stops
the run; the rest is picked up next time.

> **A mapped-series issue now carries a queue stamp, like a reading-list entry.**
> `wanted_queue_log(series_id, issue_number)` and `QUEUE_COOLDOWN_DAYS` (shared
> with `core.wanted_reading_lists` — one cooldown, not two knobs meaning the
> same thing). Normally an issue *is* filed back by
> `process_incoming_wanted_issues` and stops being wanted, so the stamp only
> bites when that did not happen: a dead mirror, a provider rate-limiting every
> request, an archive that will not unpack. Those are exactly the runs that
> re-queued the same issue nightly.
>
> It is **its own table**, not a column on `wanted_issues`:
> `refresh_wanted_cache_background` opens by wiping that table, which would
> throw the stamp away precisely when it is needed. It is keyed on
> `issue_number`, not `issue_id` — the id is a provider's, the number is what
> the search and the filing match on. It is **sweep-only**: a queued issue is
> still missing, so it keeps showing on the Wanted page, and the scoped run
> ignores the cooldown the same way it ignores the Monitor toggle. A stamp that
> cannot be read or parsed lets the issue through — pinning an issue off the
> list forever is far worse than one extra search.

### Split GetComics Posts

A GetComics post can hold several downloads (a `<li>` per range, sometimes next
to the post's own buttons). Queue through `get_download_parts()` /
`get_result_parts()`, never `get_download_links()` — it returns only the first
part, which is how #542 fetched Supergirl #1–15 for every issue in #1–80.
Automated downloads take one part via `select_parts_for_issue()` and record that
part's range, not the post title's; a range part is a pack, so it is taken only
with Download Packs on (see **Range Pack Handling**). A manual grab does the same when the search
modal passes the issue (only for a scored result list); every part is queued
only when there is no issue to go by.

#### A `#fragment` link is one comic on a listing page, not a post

A GetComics *listing* page (a weekly update, a Top-10 collection) holds many
unrelated comics. `getcomics_urls.url` is UNIQUE, so the scrape index stores one
row per comic keyed **`<page url>#<entry slug>`** (`_slugify_entry_title`). HTTP
drops the fragment, so fetching such a link returns the whole page — and
returning its first part hands back **a different comic**. That is how 20 wanted
Star Wars issues each downloaded the page's first entry, "Star Wars - Jedi
Knights 010": 20 copies, 1.19 GB, in 106 seconds.

- **`get_download_parts()` is fragment-aware and must stay so.** Given an entry
  slug it returns *that* entry's links (via `_enumerate_page_entries`, which
  mirrors the three layouts the indexer reads, so entry→links cannot drift from
  entry→slug). If the entry is gone it falls back to the row's stored
  `download_url` and otherwise returns **no links**. It must **never** fall
  through to the page-wide read — a download of the wrong comic under the right
  name is far worse than no download.
- **`_slugify_entry_title` is written into `getcomics_urls.url`.** Every slug
  already on every install came out of it, so the truncation lengths and the
  issue-number prefix are load-bearing. Do not tidy it.
- **A slug matching two entries counts as no match.** Both collapsed into one row
  under `INSERT OR REPLACE`, so the page cannot say which one the index kept;
  picking the first is a coin toss between two comics.
- `#canonical` is the migration marker for a single-comic page indexed before
  entry keys existed. It names the page, not an entry.
- **The sweep also keys a per-run `queued_download_urls` on the resolved
  download URL**, as a backstop — *not* on the page URL, because a legitimately
  split post serves several different parts from one page (#542) and taking a
  different part per issue is correct. `downloaded_ranges` cannot cover this
  case: it records only a labelled split part or a `range fallback` tier, and
  the incident was an unlabelled ACCEPT part. A suppressed duplicate still
  counts as a download and still stamps `_mark_queued`, or the issue would be
  handed on to the lower-priority sources while its file is already in flight.

#### The sitemap index: one row per page, one visit per page

Two bugs lived here, and both reported themselves as a network problem.

- **`build_sitemap_index` built its rows without `url`**, which is `TEXT NOT
  NULL`. Every `executemany` raised `NOT NULL constraint failed`, the per-page
  `except` swallowed it, and the weekly job logged `⚠️ Sitemap index rebuild
  returned 0 URLs — check network access`. On a fresh database it failed even
  earlier: `_migrate_from_old_tables` drops `getcomics_sitemap_pages` and
  nothing recreated it, so the first query raised `no such table`.
  `_ensure_urls_table` now recreates it — it holds nothing but HTTP cache
  hints, so losing it costs one unconditional refetch.
- **A sitemap row is keyed by the page URL**, and a scraped *entry* by
  `<page>#<slug>`, so the two never collide. The write is an **upsert, not
  `INSERT OR REPLACE`**: a single-comic page carries its title, issue number
  and `download_url` on that very row, and REPLACE deletes the row before
  re-inserting it, so a weekly refresh would throw away everything the scraper
  learned. `COALESCE` keeps what is there and fills only what is blank.
- **`lookup_series_urls` de-duplicates `full_url`.** It returns one row per
  *entry*, and a listing page holds many entries sharing one page — while the
  caller scrapes `full_url`. In the reported log all 20 candidate slots went to
  three distinct pages (8×, 7×, 5×), *per issue searched*, each behind a
  2-permit/1s rate limiter. That is most of the 9,408 s sweep, and the page
  actually holding the issue never got a slot.
  `scrape_and_score_candidate` already scores every entry on a page and returns
  the right one's links, so one visit is enough.

`_norm_series_key()` is the single copy of the expression stored in, and matched
against, `series_norm_norm`. Every writer has to agree with
`lookup_series_urls`'s `WHERE series_norm_norm = ?`; a row that disagrees is
indexed and then never found, which is what the sitemap insert also did.

#### A rate-limited provider is stood down, not asked again

`core.download_utils` holds a per-provider cooldown
(`note_provider_rate_limited` / `provider_rate_limited` /
`clear_provider_cooldown`). The auto-retry policy backs off **one download**,
which is the wrong unit when a provider throttles the whole client: every queued
item hits the same wall and each schedules its own 60/300/900 s retries. A
reported log has 35 consecutive `MEGA download failed: Too many requests` against
just **4 distinct files**, 35 stack traces, and not one success.

- `models.mega.RATE_LIMIT_CODES` is the allowlist (-3, -4, -6, -14, -16) and
  raises `MegaRateLimited`. "File not found" and "Expired link" are about the
  file, not the client, and must stay out of it.
- `MegaRateLimited` subclasses `Exception` so every existing handler still
  works — which is why **its handler must come before the generic one**, or the
  cooldown never gets set.
- `select_download_url` moves a cooling provider to the **back**, not out: when
  it is the only link the post offers it is still better than nothing, and the
  downloader refuses it on arrival anyway.
- In memory and per-process, like the queue it guards. A cooldown lost on
  restart costs one wasted request; one that outlives the outage would strand a
  provider that has long since recovered.

## GetComics Search Scoring System

The GetComics download detection uses a scoring system in `models/getcomics.py` (`score_getcomics_result`) to match search results against wanted issues.

### Range Pack Handling

Ranges are handled differently based on whether they're same-series or different-series:

| Scenario | Result | Score |
|----------|--------|-------|
| Same-series range ending on target (e.g., "Batman #1-12" searching for #12) | FALLBACK | 39 |
| Same-series range containing target (e.g., "Batman #1-12" searching for #5) | FALLBACK | 39 |
| Different-series range ending on target (e.g., "Court of Owls #1-5" searching for #5) | REJECT | -100 |
| Different-series range containing target (e.g., "Court of Owls #1-5" searching for #3) | REJECT | -100 |
| Same-series range with annuals added on (e.g., "Batman #1-50 + Annuals" searching for #50) | FALLBACK | 20 |
| Annual-series range containing target (e.g., "Batman Annual #1-5" searching regular #2) | REJECT | -100 |

Same-series ranges get FALLBACK because the issues ARE the main series issues. Arc/different-series ranges get REJECT because arcs have their own internal issue numbering separate from the main series — and so do annuals and quarterlies. A publication type joined to an issue run by `+`/`&` is an add-on to that run, not a sub-series (`_is_publication_addon`).

> **FALLBACK is a score, not a download.** Automated downloads take a pack only
> when **Download Packs** is on (`download_packs` in `user_preferences`, off by
> default, read through `core.config.is_download_packs_enabled()`). A pack is
> anything covering more than one issue, decided by
> `models.getcomics.is_pack_download()` on the part that would be downloaded:
> a range post, or a range part of a split post. A split post's single-issue
> part is *not* a pack even when the post title is a range (Ginseng Roots #11 in
> "#1-12"). The gate is repeated in four places with no shared choke point:
> the sweep (`app.scheduled_getcomics_download`), the simulation
> (`routes/downloads._run_wanted_simulation`) and both
> `try_download_for_issue` (`models/usenet.py`, `models/dcpp.py`). A skipped
> pack reports `status: "pack_skipped"` and records no range. Manual grabs are
> never gated: the search window marks packs, so picking one is a choice.

### Variant Keywords

Every keyword also matches its plural ("Annuals", "TPBs", "Quarterlies",
"Omnibuses"): build patterns with `_keyword_pattern()`, never `re.escape(kw)`
alone and **never a hand-rolled `s?`**. That spelling silently covers the
regular plurals and misses "omnibuses" and "galleries", which is worse than
missing all of them — the keyword still matches in the places that *do* know
the plural, so the pack is scored as a matched variant while
`parse_result_title` fails to flag it as a format, and the range-pack rejection
in `score_comic` never fires. "Batman Omnibuses #1-3" then becomes a fallback
for regular Batman #2.

### Sub-series Detection

1. **Variants** (Annual, TPB, Quarterly, etc.): Publication variants, penalized unless the variant keyword is in `SEARCH_VARIANTS` config
2. **Arcs** (Batman - Court of Owls): Story arcs with dash notation ("-"), always penalized - arc issue numbering is different from main series
3. **Sequels** (Season Two, Volume 3, Book 4, Part X, Chapter X): Sequel keywords from `SEQUEL_KEYWORDS` config, detected as arc-type sub-series
4. **Different Series** (Batman Inc, Flash Gordon): Series with remaining text that isn't variant, arc, or sequel, penalized

### "The" Prefix Handling

The swap logic allows matching "The Flash" with "Flash" for series flexibility. However, if a search uses "The " prefix and the result doesn't (or vice versa), it's treated as a different series to prevent false matches.

### Crossover Detection

Crossover keywords (`CROSSOVER_KEYWORDS` config) identify mashup/crossover series names where a year-like number is followed by a crossover separator:

```
meets, vs, versus, x-over, crossover
```

Examples: "Batman '66 Meets Steed and Mrs Peel", "Batman 1984 Meets Spider-Man". When the remaining text after the series name starts with a year-like number followed by a crossover keyword, the result is marked as a different series (not a variant of the base series).
