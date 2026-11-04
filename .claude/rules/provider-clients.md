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

`core/provider_client.py` is the call policy every HTTP metadata provider is
meant to share (#704). Metron is its first user (#703); GCD API, MangaDex,
Bedetheque and ComicVine are moving onto it one PR at a time. **Build a
provider's pacing, retry or auth handling on this module. Do not add another
provider-local limiter or retry loop.**

### Pace where each HTTP request is sent, never once per call

One logical call can be many requests. mokkari and simyan follow every `next`
link inside one list call, so CLU taking one limiter slot per `_api_call` let a
long Metron series send dozens of unpaced pages and draw a stream of 429s
(#703). The rule follows from that:

- A library that paginates internally is paced **through its own per-send
  hook**. For Metron that is the `rate_limiter` each `MokkariSession` is built
  with (`_CluRateLimiter`, one per session and so one per account).
- A plain-`requests` provider paces inside the one method that sends each
  request, once per page (the `HttpClient._send()` #704 adds), using
  `helpers.rate_limit.get_limiter()`.
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
4. Add a row to `PROVIDER_RULES` in `tests/unit/test_provider_calls_guarded.py`.
   That ratchet asserts every client method call sits in a lambda (or a named
   nested `def`) passed to the guard, and that only the owner module builds the
   client. On `main` it caught the eight raw Metron calls that #703 fixed.

Tests: `tests/unit/test_provider_client.py` (the policy, against a fake
client), `tests/unit/test_provider_calls_guarded.py` (the ratchet), and
`TestPerSendPacing` / `TestRateLimitClassification` / `TestFetchArcsPagePacing`
in `tests/mocked/test_metron_model.py`. `TestPerSendPacing` drives a real
mokkari Session through a paginated list and counts one limiter slot per page.
