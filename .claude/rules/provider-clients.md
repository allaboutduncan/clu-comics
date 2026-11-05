---
paths:
  - "core/provider_client.py"
  - "helpers/rate_limit.py"
  - "models/metron.py"
  - "models/comicvine.py"
  - "models/gcd_api.py"
  - "models/providers/**"
---

# Provider call layer: pacing, lockouts, quota latches

`core/provider_client.py` is the call policy every HTTP metadata provider
shares (#704). Metron (#703), the plain-`requests` providers (through
`HttpClient`) and ComicVine are all on it. **Build a
provider's pacing, retry or auth handling on this module. Do not add another
provider-local limiter or retry loop.**

### Pace where each HTTP request is sent, never once per call

One logical call can be many requests. mokkari and simyan follow every `next`
link inside one list call, so CLU taking one limiter slot per call let a
long Metron series send dozens of unpaced pages and draw a stream of 429s
(#703). The rule follows from that:

- A library that paginates internally is paced **through its own per-send
  hook**. For Metron that is the `rate_limiter` each `MokkariSession` is built
  with (`_CluRateLimiter`, one per session and so one per account).
- A plain-`requests` provider paces inside the one method that sends each
  request, once per page: `HttpClient._send()` (`core/http_client.py`, below).
- `GuardedClient.call()` **never paces**, and `_gate()` is not the place for a
  paginating library's limiter. A test pins that `call()` does not sleep.
- The limit you enforce is the limit Settings shows (`BaseProvider.rate_limit`).
  A test asserts Metron's seed equals `MetronProvider.rate_limit`.

### `GuardedClient`

`call(fn, context, default, *, ignore_auth_lock, raise_auth_errors,
raise_api_errors)` checks the lockout and quota, runs `fn`, and acts on the
`Verdict` from the subclass's `_classify(exc)`. That method is the only
provider-specific part:

| Verdict | Meaning | What `call` does |
|---|---|---|
| `AUTH(status, detail)` | credentials rejected | latch the lockout, return default (re-raise under `raise_auth_errors`) |
| `QUOTA(wait)` | long window spent | `wait > 0`: latch the quota that long. `0`: the classifier already latched. Never retried |
| `RETRY(wait)` | burst rejection | retry. `wait > 0` sleeps first; `0` means the per-send pacer already backed off |
| `CONNECTION` / `ERROR` | anything else | log, return default (re-raise under `raise_api_errors`) |
| `None` | not a provider error | propagate |

Things that look arbitrary and are not:

- **`RETRY(0)` exists because two retry layers stacked.** mokkari's limiter
  already blocks every thread for `Retry-After` after a 429, so a CLU sleep on
  top doubled the wait. Return `RETRY(0)` whenever the pacer has seen the
  rejection.
- **A paused call returns `default` without sleeping.** A sweep over 800 series
  must not sit through 800 waits that cannot succeed. Routes get a reason from
  `paused_reason()` and should say it (503) rather than show "no results".
- **`raise_api_errors` keeps a route's "provider unavailable" response.** An
  auth rejection still latches and returns `default`, even with the flag set.

`lockout` may be `None` for a provider with no credentials to reject
(MangaDex, Bedetheque...). It gets no Re-enable button, and an `AUTH` verdict
from it is handled as `ERROR`.

### `HttpClient` (`core/http_client.py`)

This is the `GuardedClient` for plain-`requests` providers. GCD API, MangaDex,
MangaUpdates, Bedetheque and AniList all use it. `get`/`post` return the
`Response`, or `default`. An anonymous provider gets one client through
`shared_http_client(name, build)`, one session for every thread. GCD API keeps
one client per credential pair. `json_or_none` turns a response into a body,
or `None` if the response is missing or not JSON.

Every provider sets `rate_limit` to the rate it actually enforces, which is
also what Settings shows:

| Provider | `rate_limit` | Why |
|---|---|---|
| GCD API | 30 | |
| MangaDex | 180 | ~5/s per IP is the documented floor |
| MangaUpdates | 30 | |
| Bedetheque | 30 | The 2s gap it always used |
| AniList | 30 | Its docs say "degraded state … limited to 30 requests per minute". Raise this when that lifts. |

**User-Agent.** Providers that ask clients not to pose as a browser get
`core.version.CLU_USER_AGENT`: Metron, MangaDex (which refuses spoofed agents),
MangaUpdates, Bedetheque and AniList. The two behind Cloudflare's bot check,
ComicVine and comics.org, keep `DEFAULT_PROVIDER_USER_AGENT`. A per-user token,
such as AniList's, is passed as a per-request header, never set on the shared
session.

- **Even spacing, not a burst window.** The pacer is `get_limiter("http:<name>", 1,
  60 / per_minute)`, a one-slot window. A 180/min sliding window would allow
  180 requests at once and trip MangaDex's 5/s. `per_minute` is the provider
  class's `rate_limit`. One pacer per provider name is shared by every client
  instance (`_pacer_for`).
- **The hold.** A 429/503 sets one provider-wide hold from `Retry-After`
  (seconds or an HTTP date) or `X-RateLimit-Retry-After` (an epoch). If no
  wait is given the hold is 5s. Every `_send` waits it out, so the verdict is
  `RETRY(0)`. A wait above `quota_threshold` (300s) is never held. It latches
  `QUOTA` instead.
- **Inside a Flask request** the hold plus the slot wait is capped at
  `interactive_wait`. Past the cap the client raises `ProviderBusy`, which
  returns `ERROR`, so the call returns `default`.
- **A 403 latches only if the body is not HTML** (`auth_rejection`). An HTML
  403 is Cloudflare's bot block, and comics.org sits behind Cloudflare.
  Latching on it would pause the provider for something no credential change
  can fix. A 401 always latches.
- `tests/conftest.py` stubs `_Pacer.wait_turn` for every test. Tests that need
  real pacing are marked `@pytest.mark.real_pacing` (`tests/unit/test_http_client.py`).

The GCD API's lockout is `AuthLockout("gcd_api", ...)` in `models/gcd_api.py`.
`GCDApiProvider._get_client()` returns `None` while it is latched.
`test_connection` is the one bypass (`check_credentials()`). The credential
save route clears **any** registered lockout (`routes/metadata._provider_lockout`),
not only Metron's. It also drops the shared GCD client.

### `LibraryClient` (`core/provider_client.py`) — method-named clients

A provider whose calls go through a client library (simyan, mokkari) gives
callers a `LibraryClient` subclass, not the library object. Every method has
the library's own name and runs under the provider's `GuardedClient`:
`api.series(5)`, `cv.get_volume(4050)`. ComicVine (#704 PR 4a) and Metron
(4b) both use it; the lambda shims `_cv_call_with_retry` and `_api_call` are
gone.

- **The methods are generated from `READ_METHODS`** when the subclass is
  defined, and there is **no `__getattr__`**. A name not on the list is an
  `AttributeError`, so a read a library adds can never be reached around the
  policy. The ratchet asserts that every public read the *installed* library
  has is on the list.
- **`using(**options)` returns a view** on the same library object and policy,
  with different `CallOptions`: `context` (the log name, by default
  `"<method>(<args>)"` capped at 60 chars), `default`, `raise_api_errors`,
  `ignore_auth_lock`, `raise_auth_errors`, and ComicVine's `blocking`. Sites
  converted from a shim pass their old context string, so log lines don't
  change.
- **Each provider keeps its failure contract**, via `_run`. The base returns
  `options.default`, which is Metron's contract (`MetronClient`).
  `ComicVineClient` overrides `_run` to **raise**, as its callers always
  expected.
- **Guard one request per method call.** A helper that makes several requests
  calls the client once per request and post-processes outside it, so a
  failed request retries on its own. It must then tell a failed request
  (`None`) from an empty answer (`[]`): "not found on Metron" is logged only
  for the second.
- **`bool(client)` is always True**, so `if not api:` still means "no client
  was issued".
- **Only the owner module touches `client._library`** (the ratchet's
  `test_only_owners_reach_the_library_object`).

### ComicVine (`models/comicvine.py`)

`_ComicVinePolicy(GuardedClient)` has `AuthLockout("comicvine")` and
`QuotaLatch("ComicVine", "hourly")`. `get_cv_client(key)` returns a cached
`ComicVineClient`. Its methods return the result or **raise**.
`search_resource(resource, query, max_results=None)` is the guarded
cross-version search ("volume", "story_arc"...). `_raw_search` underneath it
picks `search_<x>s` or the legacy `search`.

- **Pacing is Simyan's alone.** Each client is a `CachedLimiterSession`.
  Its bucket allows 1 request per second and 200 per hour, and it is
  consulted on every send, pagination included. CLU's per-call 180/h limiter
  (`_comicvine_rate_limiter`) was the #703 anti-pattern and is gone.
  `COMICVINE_RATE_LIMIT_PER_HOUR` now only logs a warning.
- **Simyan's limiter refusing past `max_delay` → `QUOTA`.** Its per-second
  bucket never needs that long, so a refusal means the 200/h bucket is spent.
  Retrying it would cost one `max_delay` (60s) per series for an hour.
  `_limiter_refused` matches "Rate limit not cleared" anywhere in the
  `__cause__` chain. A genuine read timeout is `CONNECTION`.
- **"Slow down cowboy" (a 420) → `RETRY`, with a real sleep.** Unlike
  mokkari, Simyan's bucket doesn't learn from it: only 429 is in
  `limit_statuses`. `_retry_wait` scales the sleep linearly
  (`COMICVINE_RATE_LIMIT_BACKOFF` × attempt). After the last attempt the
  rejection itself is raised.
- **Interactive cap.** `_gate()` reads the bucket through `_bucket_wait_seconds`.
  Inside a request it raises `ComicVineBusy` once the wait exceeds 10s.
  `Bucket.waiting()` cannot be used for this: it answers only after a failed
  put. So the code peeks at the item `limit - slots` places back from the
  newest. `hourly_wait_seconds(slots)` is the public form, used by the Mylar
  import to wait visibly.
- **While the lockout or the quota is latched**, every method raises
  `ComicVineUnavailable` and sends nothing. `get_cv_client` still hands out
  clients, because no caller can reach Simyan except through a guarded
  method.
- **`check_api_key(api_key, record=)`** is the connection test's deliberate
  bypass: one `_raw_search` on `client._library`, no retry, and it ignores
  the lockout. With `record` (the saved key, not a Guided Setup probe) a pass
  clears the lockout and a 401 latches it.
- **Quiet while paused.** A latched quota fails every call at once, so the
  per-item handlers log through `log_cv_failure`, which uses DEBUG for a
  skipped call.

`GuardedClient` changed for this: `_gate()` now runs inside the `try`, so a
refusal is classified, and there is a `_retry_wait(wait, attempt)` hook.

### Metron (`models/metron.py`)

`_MetronPolicy(GuardedClient)` has `AuthLockout("metron")` and
`QuotaLatch("Metron", "daily")`. `get_api()` / `get_flask_api()` return a
`MetronClient`, cached per token in `_SESSION_CACHE`, and refuse to hand one
out while the lockout is latched. Its methods return the result or the
default. See `.claude/rules/metadata.md` for the lockout and pacing details.

- **`READ_METHODS` is mokkari's reads plus `collection_scrobble`**, the one
  write CLU sends. The other writes (`*_post`, `*_patch`, `collection_add`...)
  are deliberately absent; the ratchet lists them in `not_reads`, so a new
  mokkari method fails until someone classifies it.
- **`api.arcs_page(params, page)`** is the one request mokkari does not send.
  It builds its own request to escape auto-pagination, so it takes the
  session's limiter, adds the Bearer token, and latches the lockout and the
  daily quota itself.
- **The purge helpers take a client** (`purge_series_cache(id, api=api)`) and
  purge its session's cache; with none they purge every cached session.

### `AuthLockout(provider, display_name)`

This is the 401/403 brake moved out of Metron unchanged. It is persisted to
`user_preferences` as `<provider>_auth_blocked` / `_auth_error` /
`_auth_blocked_at` (Metron's original keys, so no migration was needed). It
fails open without caching an unreadable read and has no timer. Building one
registers it under `lockout_for(provider)`, and that registry is what the
generic `POST /api/providers/<type>/reset-auth` clears. A new provider gets the
Re-enable route by building its lockout and nothing more.

### `QuotaLatch(display_name, window_label)`

This holds "the daily/hourly window is spent until X". It lives in memory only:
the first response after a restart re-teaches it. It logs on the transition in
each direction, never per call.

### Adding a provider (#704)

1. Subclass `GuardedClient` and write `_classify` for that library's errors.
2. Build an `AuthLockout` and a `QuotaLatch` for it.
3. Pace per send, as described above.
4. Give callers a `LibraryClient` subclass, not the library object.
5. Add a row to `PROVIDER_RULES` in `tests/unit/test_provider_calls_guarded.py`
   with `client_class` and `library_class`. The ratchet then asserts:
   - only the owner module builds the library object or reaches `_library`;
   - the names in `retired` appear nowhere;
   - `READ_METHODS` covers every public method the installed library has,
     except the writes listed in the row's `not_reads`.

   It replaced a lambda-shape check that caught the eight raw Metron calls
   #703 fixed; with no shim left there is no lambda to check. A
   plain-`requests` provider goes in `HTTP_PROVIDER_MODULES` instead. Those
   modules may not import `requests` or build a `Session`.

Tests: `tests/unit/test_provider_client.py` (the policy, against a fake
client), `tests/unit/test_library_client.py` (generated methods, `using()`,
automatic context), `tests/unit/test_provider_calls_guarded.py` (the ratchet), and
`TestPerSendPacing` / `TestRateLimitClassification` / `TestArcsPagePacing` /
`TestAFailedRequestIsNotANotFound` in `tests/mocked/test_metron_model.py`. `TestPerSendPacing` drives a real
mokkari Session through a paginated list and counts one limiter slot per page.
