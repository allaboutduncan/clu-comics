# Metadata date check

When CLU looks up metadata automatically it parses a series name and an issue number from the
file, finds the series with a provider, then fetches that issue number. If the *series* match is
wrong the issue number still resolves, so the file is tagged with confidently wrong metadata
instead of being left alone.

The date check compares the date of the matched issue against the year in the filename and treats
a large disagreement as a failed match.

It is off by default.

## Settings

Both live under **Config → Metadata Match Validation**.

| Setting | Default | Meaning |
| --- | --- | --- |
| `DATE_CHECK_MODE` | `off` | `off`, `log` or `enforce` |
| `DATE_CHECK_TOLERANCE_YEARS` | `2` | How far the dates may disagree before the match counts as wrong |

**`off`** — no change in behaviour. Nothing is parsed and nothing is compared.

**`log`** — conflicts are written to the log; what gets tagged is exactly what `off` would tag.
Start here. Run a normal metadata job over your library and read the conflicts it reports before
letting it change anything.

**`enforce`** — a conflicting match is not written. In a bulk job the file goes to the review
queue with the reason `date_conflict`. In the batch metadata screen it is reported as unmatched
with the reason "date conflict"; run a manual search on it to pick the right series yourself,
which is exempt from the check.

## What counts as a date

The check only acts when it can read a year from the filename **and** the provider supplied a date
for the issue. If either is missing it does nothing — a missing date is not evidence of a bad
match.

A year is read from any standalone four-digit number, including the release form many scans use:

```
Diabolik - Nero Su Nero #001 (2014).cbz          -> 2014
001 - Il re del terrore (Mondadori 1962-11).cbz  -> 1962
```

Two things are deliberately ignored:

- **Scanner credits and tags.** `Hal2008` names a person, and `1920px` a pixel height, not a
  publication date. A run of digits touching letters on either side is never read as a year.
- **Volume markers.** `v1998` names the run, not this issue. Reading it as an issue date would
  make every later issue of a long-running series look like a conflict.

If a filename names two different years, the check does nothing rather than guess between them.

## Where it applies

Automatic lookups only — bulk metadata jobs, the batch metadata screen, and automatic single-file
searches.

When a match is rejected in the batch screen, the next provider in your priority order still gets
its turn. Only if every provider is exhausted is the file reported unmatched.

**Some providers are exempt.** MangaDex, MangaUpdates, AniList and Bedetheque record the year the
*series* began rather than a date per issue, so comparing it against a filename would reject every
volume of a long-running title — `Berserk v22 (2003)` against a series that began in 1989 is
fourteen years apart and perfectly correct. The check applies to Comic Vine, Metron and the Grand
Comics Database, and to nothing it does not recognise.

A series you pick yourself from the selection dialog is never second-guessed. You have already
decided, and the check does not overrule you.

## Choosing a tolerance

Cover dates, on-sale dates and the year a scanner types into a filename routinely disagree by a
year. Reprints, facsimile editions and omnibuses disagree by more, and legitimately so — an
omnibus published in 2014 collecting 1960s material carries the 2014 edition date.

The default of 2 years is meant to catch gross mismatches like a 1999 series matched to a 2014
file, not to police off-by-one differences. If `log` mode reports conflicts on matches you know
are right, raise the tolerance rather than turning the check off.
