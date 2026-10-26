---
paths:
  - "models/**"
  - "core/comicinfo.py"
  - "core/bulk_metadata.py"
  - "core/metadata_backfill.py"
  - "core/metadata_dates.py"
  - "core/credit_backfill.py"
  - "core/series_json_builder.py"
  - "core/metadata_scanner.py"
  - "routes/metadata.py"
  - "routes/bulk_metadata.py"
  - "routes/source_wall.py"
  - "routes/series.py"
  - "cbz_ops/rename.py"
  - "cbz_ops/smart_rename.py"
  - "static/js/source_wall.js"
  - "static/js/clu-metadata*.js"
  - "templates/config.html"
  - "app.py"
---

# Metadata: Metron auth, collected editions, provenance, series.json, ComicInfo writes

### Metron Authentication

Metron is **API token only**. mokkari 5.0 removed username/password: the
constructor is `Session(api_token, *, cache=, user_agent=, ...)`, and passing
`username=` raised inside every Metron call (#673). `requirements.txt` floors
mokkari at 5.0 rather than pinning below it.

- `get_api()` keeps its `(username, password, api_token)` signature for its
  ~100 positional callers but ignores the first two. With no token it returns
  `None` and warns **once** that basic auth is no longer supported.
- `MetronProvider` has `auth_fields = ["api_token"]`, no `auth_modes`, and
  `retired_auth_fields = ["username", "password"]`. `_apply_auth_mode` strips
  retired fields from every save and probe, so a stale username from an old
  install or a cached page can never be written back. A payload left empty is
  a 400. `test_connection` tells a username-only install it needs a token.
- **The cache is the mokkari 5 `SqliteCache`** (`default_ttl=`, table `cache`).
  `get()` honours expiry now, so `METRON_CACHE_EXPIRE_DAYS` is a real TTL. The
  same file path is reused, and mokkari discards a 4.x `responses` table on open.
  The purge helpers use the public `delete(key)` (exact detail URLs, built from
  `session.api_url`) and `invalidate("issue", "list")`; never raw SQL on the table.

**A 401 or 403 latches a lockout that stops all Metron traffic.** A user mistyped
their credentials, CLU retried until Metron's fail2ban banned their IP, and a
Metron admin asked for it to be reported. Metron's published guidance is that
only 429 and 5xx are worth retrying. So:

- Detection reads the status off `exc.__cause__.response.status_code`
  (`metron.auth_failure_status`), the same place `is_connection_error` looks —
  not out of `ApiError`'s message, which mokkari assembles for humans.
- Enforcement is in **two** places, and both are needed. `_MetronPacer.before()`
  short-circuits every `_api_call`; `get_api()` refuses to hand out a client at
  all, checked *before* the session cache, because several callers fetch a
  client once and then drive mokkari in a loop.
- **There is no timed auto-retry.** A rejected credential does not heal on its
  own. The block clears only on a credential save, a successful connection test,
  or the Re-enable button (`POST /api/providers/<type>/reset-auth`).
- The flag lives in `user_preferences` (`metron_auth_blocked` and friends), not
  just in memory, so a container crash-looping with bad credentials does not
  resume hammering. The database is the source of truth; the copy in `_AuthBlock`
  is a memo that `invalidate_session_cache()` drops. Reads fail **open** but
  never cache that: `get_user_preference` swallows its own errors, so a read
  taken before the database is ready is indistinguishable from "not blocked",
  and remembering it would switch the lockout off for the life of the process.
  `_read_persisted()` therefore queries the table directly and returns `None`
  on failure, which leaves the memo unloaded so the next call retries.
- **`test_connection` is the one path allowed past the lockout**
  (`get_api(..., ignore_auth_lock=True)`, `_api_call(..., raise_auth_errors=True)`).
  Without that bypass a user who fixed their credentials could never prove it.

> **Every Metron call must go through `_api_call`.** A raw `api.<method>()` takes
> no rate-limit slot *and* ignores the lockout, so a loop holding a client keeps
> calling with credentials Metron has already rejected. `fetch_arcs_page` is the
> deliberate exception — it builds its own request to escape mokkari's
> auto-pagination — and therefore carries the auth rules by hand. That includes
> adding `Authorization: Bearer <api.api_token>` itself: mokkari 5 keeps the
> token out of `api.header`, so an arc page sent with `api.header` alone is a
> 401, and that latches the lockout for *all* Metron traffic.

Saving Metron credentials verifies them in the same request
(`BaseProvider.validate_on_save`, Metron only). The credentials are stored either
way and the response carries `valid`/`error`: refusing the save would strand a
user whose provider is merely down.

### Collected Editions in the Metadata Cascade

ComicVine catalogues a trade paperback as its own volume, usually named exactly
like the series it collects: "Giant Monster" (2005, two issues) and "Giant
Monster" (2007, one issue, #1 "TPB/HC"). `Giant Monster (2007).cbz` found
neither, and `Giant Monster 001 (2007).cbz` was tagged as the 2005 "Book One".
Three pieces fix that in `/api/search-metadata`:

- **`parse_comic_filename` understands `Series (YYYY)` and collected-edition
  markers** (`detect_collected_edition`). It used to fall through to the
  last-resort fallback for the former, giving series `'Giant Monster (2007)'`
  and no year. TPB/HC/GN/OGN are stripped from the search name. Omnibus,
  Compendium, Absolute and Deluxe Edition only flag the file, because ComicVine
  keeps those words in the volume name. A trailing "Vol. N" becomes the issue
  number only when no other number was parsed, so `Batman Vol. 3 050` is
  unchanged. A custom rename pattern is never rewritten.
- **`comicvine.rank_volume_candidates` orders the ComicVine candidates** for
  both the API and the local dump, through the shared
  `_pick_comicvine_volume_issue`. The order is: exact name, then a volume that
  started in the file's year, then `looks_collected` when the file is flagged
  or has no issue number, then year distance, then issue count. The local dump
  stores `start_year` as TEXT, so every year comparison converts it first.
  `looks_collected` reads only the *opening* of the description and ignores
  "collected in/as": the floppies' own descriptions point at their trade.
- **`core.metadata_dates.issue_year_fits` is a preference, not a rejection.**
  A match whose cover year is more than a year from the filename's (and whose
  volume did not start in that year) is *held*, and the cascade keeps asking
  lower-priority providers for one that fits. If none does, the held match is
  applied as before, because filenames do carry re-release years (#549: a 2025
  file for a 2019 issue). While a match is held, a later provider's selection
  prompt is skipped: a usable answer must not turn back into a question. This
  is always on and separate from `date_check_mode`, whose enforce tolerance
  (2 years) calls the Giant Monster case "no conflict".

Metron is usually the culprit, because it runs first and may know only the
mini-series. `_try_metron_single` is untouched; the hold in the cascade covers
it.

Batch, bulk and the `app.py` auto-tag loops do not use this yet. The helpers
are shared precisely so they can.

### Provider Provenance, Re-tag and Backfill

Nothing recorded which provider tagged a file except the free text in
ComicInfo `Notes`, so a series could silently hold a mix (#626) and nothing could
re-ask "the provider that tagged *this* file" (#579).

- **`core.comicinfo.provider_from_notes` is the only Notes→provider table**
  (`PROVIDER_NOTES_MARKERS`). Order matters: `ComicVine (Local DB)` before
  `ComicVine`, `GCD REST API` before `GCD`. It reads only text after
  `Metadata from `, so a provider name inside a free-text note is not
  provenance. Untrusted-scraper and unrecognised Notes return None.
- **`file_index.ci_provider`**: NULL = never read, `''` = read and none, else a
  `ProviderType` value or `manual`. The `''` keeps untagged files out of
  `queue_provider_backfill()` (drained by the scanner's idle monitor, like
  `ci_metronid`). `update_file_index_from_comicinfo` uses COALESCE for it: no
  Notes in the dict means "don't know", not "none".
- **Re-tag** is `start_bulk_job(provider=, replace=, only_from=)`, not a new
  job type. A forced provider **ignores a cvinfo that names another provider**
  (`_try_cvinfo(only_provider=)`), never falls back to a second source (a miss
  goes to review), and skips files already tagged by it. `replace` writes with
  `merge_existing=False` and tells the index `authoritative=True`, otherwise
  `ci_metronid` would keep the id the archive no longer carries.
- **Backfill** (`core/metadata_backfill.py`, `POST /api/bulk-metadata/backfill`)
  asks each file's *own* provider for the issue by the id the file carries and
  adds only tags that are blank (`fill_blank_comicinfo_bytes`, the inverse of
  `merge_comicinfo_bytes`). `Notes` is never refilled. Manual-tagged and
  unrecognised files are skipped, not guessed. It is separate from the
  scheduled `core/credit_backfill.py`, which stays Metron/credits/recent only.
- Both write audit rows, so Metadata History can revert them.
- **Source Wall shows it.** `ci_provider` is a default, configurable, display-only column
  (never in `CI_FIELD_TO_XML`, which gates every edit). The *Browse folders / Tagged by X*
  picker is a flat, paged, library-wide view (`/api/source-wall/providers` and `/by-provider`).
  Both scope to `accessible_folder_prefixes(user)`, so a folder-scoped user's rows **and
  counts** stop at their grants. A row's dropdown and the bulk bar re-tag through
  `CLU.openRetagFilesModal` (`scope: 'files'`). Every reload goes through
  `reloadCurrentView()`, or a delete in the provider view drops back to the tree.
  A *This folder / All* toggle narrows the counts and the view to the folder being
  browsed (`folder=` on both endpoints). The folder is **intersected with the user's
  grants** (`_provider_scopes`), never trusted, and must lie inside the library.
  An **Unknown** provider (has ComicInfo.xml, `ci_provider == ''`) is the one editable state:
  `POST /api/source-wall/set-provider` rewrites `Notes` via `notes_for_provider` and only then
  sets the index, because the scanner re-derives `ci_provider` from Notes and would undo an
  index-only edit. A known provider is changed by re-tagging, never by relabelling, and the route
  skips those. Side effect to remember: the stamp is a *trusted* Notes, so the auto-taggers will
  leave that file alone afterwards. `manual` is labelled "Manual" in the UI.
- The Problem Files page deliberately has no provider-mismatch source: it is a
  ledger of failures, and a mixed series is not one.

Tests: `test_provider_from_notes.py`, `test_provider_tracking.py`,
`test_bulk_metadata_retag.py`, `test_metadata_backfill.py`,
`test_metadata_tools_wiring.py`.

### Which Provider Fills series.json

`core/series_json_builder.py` decides it (#618). Four writers used to pick a
provider their own way, so a Metron record with a null description beat a
ComicVine one that had text, and the user could not ask for the other.

- **`resolve_series_for_folder`** asks the preferred provider first, then the
  library's enabled providers in order (`provider_order`), using ids already in
  the folder (cvinfo, then an existing series.json) — it never asks the user.
  Only Metron and ComicVine can fill series.json; GCD and the manga providers
  are filtered out.
- **Description fallback**: the first provider that answers supplies every
  field; if its description is empty the next provider supplies *only* the
  description. `with_description_fallback` does the same for the writers that
  already hold a series (subscribe, map, series-page refresh, bulk sidecars),
  topping Metron up from ComicVine and vice versa.
- **`series_json_provider`** (`auto`/`metron`/`comicvine`) is a global
  `user_preferences` key. `auto` follows the library order, not a second list.
- **`POST /api/series-json/refresh`** is synchronous (one to three provider
  calls for a single folder) and gated by `file_op_refusal`,
  `is_valid_library_path` and the one-shot-folder check. `force` rebuilds and
  discards hand edits; the default keeps `PRESERVED_FIELDS`.
- The automatic sidecar path still never overwrites an existing series.json.
- `_fetch_comicvine` reuses `cbz_ops.smart_rename._build_comicvine_series_dict`
  rather than copying the shared-client and rate-limit handling.

Asserted in `tests/unit/test_series_json_builder.py`,
`tests/routes/test_series_json_routes.py` and `tests/unit/test_series_json_wiring.py`.

### ComicInfo.xml Writes

`core/comicinfo.py` owns the **single** `generate_comicinfo_xml`.
`routes/metadata.py` and `models/comicvine.py` only re-export it (callers and
tests import the name from both). There used to be two near-duplicate copies
that had drifted; a field added to one was silently dropped on the paths using
the other. Do not add a third — extend the one in `core/`.

It uses an explicit `add(tag, ...)` allowlist, so a field a provider maps but
that has no line there is computed and then discarded. Adding a ComicInfo field
means adding one `add()` call.

> **The writer never invents a value it can't know.** It defaults only
> `LanguageISO` (`en`) and `Manga` (`No`) — both safe, since every manga-aware
> provider sets `Manga` itself. It deliberately has **no `Notes` fallback**: a
> serializer cannot know a file's provenance, and `Notes` doubles as the
> "already tagged, skip this file" sentinel read by `routes/metadata.py`,
> `models/comicvine.py` and `app.py`, so a fabricated one would both mislabel
> the source and make the file permanently un-retaggable. Every provider mapper
> sets `Notes`; the two inline GCD-SQLite builders in `routes/metadata.py` set
> theirs where the dict is assembled.

That sentinel is read by **six** independent auto-tag entry points — two in
`app.py` (`auto_fetch_metron_metadata`, `auto_fetch_comicvine_sqlite_metadata`),
one in `models/comicvine.py` (`auto_fetch_metadata_for_folder`) and three in
`routes/metadata.py` (batch, provider search, GCD) — with no shared choke point,
so the *check* is necessarily repeated. The *policy* is not: all six call
`core.comicinfo.has_trusted_notes()`, and Notes written by scrapers we don't
trust (Amazon, Comixology) are listed once in `UNTRUSTED_NOTES_MARKERS` so those
files stay eligible for re-tagging. Add a new exclusion to that tuple only —
never re-inline the string at a call site.

> **The three folder-walking entry points must ask `is_zip_container()` first.**
> ComicInfo.xml lives inside a zip, and every reader and writer here raises
> `ValueError("Only .zip or .cbz files are supported by this function.")` on
> anything else — but they all collect `(".cbz", ".cbr")`. Two of them run the
> whole folder inside one `try`, so a single `.cbr` abandoned every remaining
> file in that folder: eleven of those errors in one reported log, each one a
> folder tagged part-way. CLU converts CBRs in the WATCH pipeline, so one in the
> library is a file the user chose to keep — it is a **skip** (`continue`), not
> a failure, and not an error counted on every run.

Writing is a full rebuild of the archive, so `add_comicinfo_to_cbz`
(`routes/metadata.py`) and `add_comicinfo_to_archive` (`models/comicvine.py`)
**merge by default**: tags the archive already had that the new metadata does
not supply are carried forward via `core.comicinfo.merge_comicinfo_bytes`.
No provider covers every field — ComicVine has no genre data at all — so without
this, re-tagging a GCD-sourced file with ComicVine wipes its `Genre`.

> **A restore must pass `merge_existing=False`.** The bulk-metadata undo
> (`routes/bulk_metadata.py`) re-applies snapshotted prior bytes; merging there
> would carry tags forward from the very metadata being undone, leaving the file
> in neither the old nor the new state.

Credit roles from ComicVine arrive as ONE comma-joined string per creator
("penciler, inker"). `models/comicvine.parse_creator_roles` splits it and
buckets each token independently, so one person can hold several credits; the
local-DB path (`models/comicvine_sqlite.py`) calls the same function so the two
cannot drift.
