---
paths:
  - "helpers/collection.py"
  - "helpers/library.py"
  - "helpers/__init__.py"
  - "core/app_state.py"
  - "core/problem_replacements.py"
  - "models/cbl.py"
  - "routes/series.py"
  - "app.py"
  - "api.py"
---

# TARGET scan, series identity and filename matching

### The TARGET Scan Only Files What Is Actually Incoming

A user with `TARGET = /library/media` — which was also their enabled library
root — had 26 comics named `Batman 0NN (YYYY).cbz` moved **out** of
`(2016) Batman v3` and **into** `(2003) Superman - Batman v1`. Three
independent defects stacked, and any one of them would have prevented it.

**1. Series identity was asymmetric.** `helpers/collection._comicinfo_series_matches`
accepted the shorter name by raw substring, on the grounds that "dropped words
are how real libraries differ". `Batman 053 (2018).cbz` carries
`<Series>Batman</Series><Number>53</Number>`, and `"batman"` is a substring of
`"superman-batman ''…'' special edition"` — so it satisfied that volume's
wanted #53. The filename tier did its job (it is `.match()`-anchored on
"Superman"); this is the ComicInfo fallback, which never sees the filename.

Both directions now earn it the same way: whichever name is longer must contain
the other **as a whole word**, and the leftover after subtracting it must pass
`_BENIGN_SERIES_EXTRA`. Things that look arbitrary and are not:

- **A leading article is the only relaxation.** Do *not* widen this to
  `OPTIONAL_WORDS` (`generate_filename_pattern`'s within-name connective set).
  That set is safe there because an anchored regex and an issue number surround
  it; here the leftover **is** the whole question, so every extra droppable word
  is another chance to merge two real series — and it cannot express
  "leading only", which is the one relaxation the tests actually justify.
- **`_LEADING_ARTICLE` lives in `helpers/collection.py` and `models/cbl.py`
  imports it.** The filename tier and the ComicInfo tier ask the same question;
  two copies let them disagree about which files a list may claim.
- **Both sides are separator-normalised first** (the same class
  `generate_filename_pattern` uses), so `Batman - The Dark Knight` and
  `Batman: The Dark Knight` are one series. Parentheses and brackets are
  deliberately *kept*: `_BENIGN_SERIES_EXTRA` reads them to recognise `(2025)`.
- **The verdict is memoized on the normalised pair.** This is asked per wanted
  issue × per remaining file; without the memo a large library pays the regex
  millions of times.
- **The tier logs its refusals at DEBUG.** A tightened filter that silently
  drops a candidate is invisible in a debug bundle.

`series_names_compatible` is the public spelling, and `models/cbl.py:_match_by_metadata`
uses it as a *filter ahead of scoring* — so tightening can only remove
candidates, never mis-score one, and an unmatched entry shows "Click to map".

**2. The scan treated the whole library as incoming.**
`helpers.collection.collect_target_candidates` now owns the walk — out of
app.py so it can be tested. Two exclusions, and both are needed:

- **A directory that is, or is under, a series `mapped_path` is pruned.** A
  comic sitting in a series folder has been filed. Unconditional, so it holds
  in every layout.
- **When TARGET is inside an enabled library, the walk does not descend.**
  There, every subdirectory *is* library structure and a download wrapper
  folder is indistinguishable from a series folder. Mapped-path pruning alone
  is not enough: a library of 500 folders with 30 mapped leaves 470 exposed.
  The cost is real and deliberate — a wrapper-folder download in that layout is
  not auto-filed and stays visible at the top of TARGET.

The old `/data`-literal guard is gone from all three places it was pasted.
`helpers.library.watch_target_verdict` is the one predicate: **`/data` is a hard
400**, a configured library root **saves with a warning**. Blocking the latter
would make an existing install's config unsaveable on upgrade, and the layout is
now safe. `library_root_for` is realpath-based on purpose — a symlinked TARGET
is how a library sneaks back into a path that looks unrelated.

> **`helpers.prune_empty_dirs` deliberately does NOT mirror this.** It still
> *refuses* outright where the scan *restricts*, because deleting a library
> folder is unrecoverable and declining to move a file is not. Its comment used
> to claim it mirrored app.py; it never did. Its refusal is announced on the
> transition and then hourly, not on every five-minute sweep.

There is also a **last guard at the point of no return**: the match loop
`continue`s with a WARNING when the source directory is a mapped series folder
and is not the destination. The collector should make it unreachable; it guards
any future caller of the matcher.

**3. Nothing serialised the pass.** `api.py` runs it on a bare daemon thread per
completed download and `routes/series.py` runs it inline for
`/api/scan-downloads`; one reported log has ~9 running at once, every line
duplicated two and three times, and an ENOENT storm as the losers moved files a
winner already had.

`core.app_state.single_flight_target_sweep` is the claim, and it is a
**decorator** — api.py imports the name `process_incoming_wanted_issues` and
must not be edited, so the claiming callable has to *be* that name. It also
leaves the body untouched, which is what keeps the five AST tests asserting
against that body working. Unlike `claim_getcomics_sweep` it **coalesces**: a
refused claim asks the holder for one more pass (bounded by
`MAX_TARGET_SWEEP_PASSES`), because nothing re-triggers this pass on a timer and
a caller that stands down silently can strand a file in TARGET until the next
download finishes. `/api/scan-downloads` answers **409** rather than reporting a
scan it did not run.

**`core/problem_replacements.py` is the destructive twin** and uses the same
collector (`skip_converted_siblings=False` — it decides `.cbr` vs `.cbz` for
itself in `is_acceptable_replacement`). A match there *trashes* the file it
replaces, so the unguarded walk could take a filed comic and destroy another
with it.

> **Year is a filter and a tie-breaker in the matcher, never a requirement (#592).**
> Two mapped series can share a name (Vampirella 1969 / 2019), and the filename
> tier is year-blind, so the first one in the wanted list used to claim every
> download. `match_wanted_issues_to_files` now collects every (issue, file)
> candidate and assigns best-first via `wanted_year_fit`: a file whose year lies
> outside the series' own run (`volume_year` - 1 .. `year_end` + 1) is ruled out
> on positive evidence only, a year inside a known run outranks no information,
> and a yearless file or a series with no run data behaves exactly as before
> (re-release years legitimately differ, #549). The wanted dicts carry
> `volume_year`, `year_end` and `cover_date` for this; all are optional.

> **The per-series Monitor toggle does not gate this pass, by design.**
> `get_all_mapped_series` filters on `mapped_path` only. The toggle governs
> unattended *downloading*, not filing, and an unmonitored series must still
> receive a manual download. In the reported incident Batman v3 was the
> *source* folder, so no toggle would have helped either way.

> **`get_series_name_from_files` is a known weak spot, untouched here.** It
> derives the series name from `os.listdir()[0]` — an arbitrary file — so one
> odd comic in a folder renames the whole series for matching purposes. That is
> how the incident came to compare against the one-shot's title. With the
> identity rule tightened this now causes *under*-matching rather than a wrong
> move, which is the safe direction, but picking the most common derived name
> rather than the first would be the real fix.

`tests/unit/test_wanted_scan_target_guard.py` asserts the app.py half
structurally; the behaviour itself is in
`tests/unit/test_target_candidate_collection.py`,
`tests/unit/test_target_sweep_claim.py` and
`tests/unit/test_watch_target_verdict.py`.

### Never reuse `VARIANT_TYPES` for filename matching

> **Never reuse `VARIANT_TYPES` for filename matching.** It carries adjectives
> ("absolute", "deluxe", "prestige", "gallery") that are ordinary words in real
> issue titles (`Nightwing 117 - Absolute Power.cbz`), so filtering on it would
> report owned issues as missing and re-download them. `helpers/collection.py`
> keeps its own narrower `_COLLECTED_EDITION_TYPES` for that reason.
>
> Spin-offs are blocked structurally instead, by `_STRICT_GAP`: with
> `strict_gap=True`, `generate_filename_pattern()` forbids letters between the
> series name and the issue number, so `TMNT - Nightwatcher 003` cannot satisfy
> TMNT #3 while `Nightwing 117 - Absolute Power` still matches (its subtitle
> comes *after* the number). Only `match_wanted_issues_to_files` opts in — it
> is the one matcher that moves and renames files. `match_issues_to_collection`
> stays loose deliberately.
