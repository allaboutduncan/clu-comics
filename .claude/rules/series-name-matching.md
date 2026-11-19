---
paths:
  - "core/series_names.py"
  - "core/filename_parse.py"
  - "helpers/collection.py"
  - "models/cbl.py"
  - "models/getcomics.py"
---

# Series-name matching: who decides what

"Series matching is wrong" names one of **three systems that share almost no
code**. Find out which before changing anything.

| System | Question | Entry point |
|--------|----------|-------------|
| Pre-download scorer | Is this *post title* the wanted issue? | `models/getcomics.score_getcomics_result` |
| Filing matcher | Is this *file in TARGET* the wanted issue? | `helpers/collection.match_wanted_issues_to_files` |
| Reading-list matcher | Which *indexed file* is this list entry? | `models/cbl.CBLLoader.match_file` |

### The filing rules live in `core/series_names.py`

The filing matcher has two tiers, a filename regex
(`generate_filename_pattern`) and a ComicInfo name comparison
(`_comicinfo_series_matches`). They used to carry a character table each and
disagreed: a leading "The" was dropped from either side in the ComicInfo tier
but was optional in the regex only when the *series name* had it. A download
named `The Totally Awesome Hulk 017 (2017).cbz` could never be filed under
`Totally Awesome Hulk`; it stayed in TARGET, the issue stayed wanted, and it
was downloaded again (#780).

Both tiers now read one table. `helpers/collection.py` keeps `_word_regex`,
`_norm_series_text`, `_LEADING_ARTICLE`, `_SERIES_SEP_RE` and `_APOS_CLASS` as
**aliases** (`models/cbl.py` imports one of them), and
`tests/unit/test_series_names.py` asserts they are the same objects. **A new
filing relaxation is one edit in `core/series_names.py`, never at a call site.**

| Difference | Filing rule |
|------------|-------------|
| Leading "The" | Optional in both directions in the regex. The name comparison also drops `a`/`an`; the regex does not, because it is the tier that moves files and only "The" has a report behind it. |
| `:` vs ` - ` vs space, `/`, `_`, `,`, `;`, Unicode dashes | All one separator. |
| `!` and anything in `FILENAME_ILLEGAL_CHARS` | Never required (`NEVER_REQUIRED`). Derived from the sanitizer's list, so a character CLU strips from names can never be demanded of one. |
| Apostrophes | Optional both ways, and may have a space in front (`Marvel 's`, a GetComics quirk). The space is allowed only *with* the apostrophe. |
| `&` | Removed from the name; absorbed if the file has it. |
| `and`, `the`, `of`, `or`, `vs`, `versus` mid-name | Optional in the regex only (`OPTIONAL_WORDS`). Do not use it for the name comparison: there the leftover is the whole question. |
| `(...)` and `[...]` | Kept. `_BENIGN_SERIES_EXTRA` reads them. |

Things that look arbitrary and are not:

- **A name made only of punctuation stays a literal** (`!!!`). Flattened, it
  compiles to an empty series pattern that claims every file with the right
  issue number.
- **Making "The" optional both ways lets `Flash` and `The Flash` claim one
  file.** That was already true in the other direction. The year ranking in
  `match_wanted_issues_to_files` (`wanted_year_fit`) decides; do not add a
  second tie-break.
- **The spin-off guard is `_STRICT_GAP`, not the name.** Nothing here may allow
  letters between the series name and the issue number.
- **No `collection_status` purge shipped with #780.** The change only adds
  matches and the affected files were in TARGET, not in series folders. A
  matcher change that *removes* matches still needs the purge marker and forced
  re-match.

### Reading a name or number out of a filename: the first group is the anchor

`core/filename_parse.py` holds the one rule the filename parsers share. In
`Series NNN (YYYY) - Title` the issue number is the last token before the
**first** `(...)`/`[...]` group and everything after it is title text. Parsers
used to strip the groups and then guess by position, which read `1950` as the
issue of `Detective Comics 156 (1937) - Batmobile of 1950` (#778) and kept
`497 - Knightfall, Part` in a series name (#790).

- **Never cut at "the first number followed by a dash".** That was #790's
  suggested regex. It turns `Spider-Man 2099 - Dark Genesis 001` into
  `Spider-Man`, and a too-short derived name claims more files.
- **A sign counts only when glued to the digits and preceded by whitespace,
  `#` or `Issue `** (`SIGN_CLASS`, aliased by `helpers/collection.py`).
  `Black Cat - 001` is issue 1.
- On it so far: `models.providers.base.extract_issue_number`,
  `helpers.collection.get_series_name_from_files` and
  `routes.metadata.extract_series_name_from_filename`.
  `tests/unit/test_filename_corpus.py` is one table of filenames with a column
  per parser; a parser moved onto the module gets a column.
- **A metadata lookup with no cvinfo reads the name through
  `routes.metadata.series_issue_year_from_filename`** (series from
  `extract_series_name_from_filename`, issue from `extract_issue_number`).
  The ComicVine search route and the GCD directory search carried a pattern
  list each, and eight sites pasted `extract_series_name_from_folder`'s
  regexes. Single-file, batch and bulk now share the functions; do not add a
  pattern list to a route.
- **Strip exactly one trailing number.** A second strip takes the `2099` out
  of `Spider-Man 2099 001`.
- **Not candidates:** `extract_comic_values` and `get_renamed_filename`
  (`cbz_ops/rename.py`) decide filenames on disk, so a change re-renames
  libraries. The module is import-free so `monitor.py` can reach it.

### What is deliberately not shared

- **The scorer's keys are stored.** `_norm_series_key` is `series_norm_norm` in
  `getcomics_urls`, `_slugify_entry_title` is part of `getcomics_urls.url`, and
  `_normalize_alias` keys `getcomics_series_aliases`. Changing one orphans rows
  on every install. They are not candidates for this module.
- **The scorer accepts "The" both ways when searching**
  (`_score_series_match`), then treats a swapped "The" with leftover text as a
  different series. The filing matcher has to accept at least what the scorer
  downloads, or the file is stranded; that is the invariant to check when
  either side changes.
- **The reading-list matcher has no folder to anchor it** and searches the
  whole `file_index`, so it needs a year and its own first-issue-token rule. It
  shares `_LEADING_ARTICLE` and `series_names_compatible` and nothing else.

### Why a download is matched by name at all

The sweep knows which issue it queued, but the filename is chosen inside
`api.py` (`Content-Disposition`, else the URL basename; `dest_filename` is
ignored on the GetComics-direct and PixelDrain paths), `api.py` is not to be
edited, and `process_incoming_wanted_issues` is called with no arguments.
`download_run_items` records series, issue and `download_id` but no path.
Usenet, DC++ and manual grabs arrive under the release's own name with no run
record. So every source converges on the filename, and the matcher is the one
place a fix covers all of them.
