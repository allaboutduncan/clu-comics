# INDUCKS provider

[INDUCKS](https://inducks.org/) is the reference index of Disney comics worldwide. It is the one
source that covers this material properly, because a Disney issue is an anthology of a dozen
unrelated stories and the general-purpose databases model it as a single book.

Like the GCD and local ComicVine providers, it reads a SQLite database file you build and place on
disk. CLU never downloads or builds it, and opens it read-only.

## What it covers

The current dump holds 84 countries, 7,310 publications, 259,785 issues and just over two million
story entries. Disney publishing is overwhelmingly European and South American: by issues indexed
the largest countries are Sweden, Italy, the Netherlands, Norway, France, Germany, Finland and
Brazil, with the US ninth.

Every publication records its country and language, so `it/TL` unambiguously identifies the Italian
*Topolino* and `se/KA` the Swedish *Kalle Anka* — the disambiguation no other provider can offer
for a title that exists in twenty countries at once.

## Setting it up

1. Build the database. INDUCKS publishes its whole database as a tarball of `.isv` files at a
   stable URL; importing it produces roughly 700 MB of SQLite.
2. Put the file on a path CLU can see, for example `/config/inducks.db`.
3. In **Config → Metadata Providers → INDUCKS (Disney)**, enter that path and save.

### Publication countries

The same title is published in dozens of countries, so an unfiltered search is ambiguous for
nearly every Disney name there is. The **Publication Countries** field on the provider card is a
comma-separated list of INDUCKS country codes, stored as the `inducks_countries` preference.

| Preference | Default | Meaning |
| --- | --- | --- |
| `inducks_countries` | `us` | Which countries' publications a lookup may match |

Set it to the countries your library actually holds — `it` for an Italian collection, `it,fr,se`
for several. The default of `us` keeps a freshly configured provider harmless rather than
confidently foreign.

## What it writes

The album is the unit. `Series` and `Number` name the album, `Summary` holds its table of contents
one line per story, and credits and characters are merged across the stories in first-appearance
order — covers and illustrations excluded, since a cover artist is not the penciller of the book.
`LanguageISO` comes from the publication, and character names are the localised ones where INDUCKS
has them, so an Italian issue says Paperino rather than Donald Duck.

`Notes` records the INDUCKS issue code, which is also what marks the file as already tagged.

## Ambiguity is refused, not guessed

Where a folder name resolves to more than one publication, the provider returns all of them and
tags nothing. A folder called `Topolino` matches eleven Italian publications — the libretto, the
giornale, and nine reprint runs — and picking one arbitrarily is how a library ends up confidently
mislabelled. Such a folder goes to the review queue, where you choose.

A year in the folder name settles the case where two runs share an identical title and started in
different years, because that is evidence rather than a tiebreak. It does not help where the
competing runs are reprints of each other; those need a manual choice.

## The date check

INDUCKS carries a real per-issue publication date, so the [metadata date
check](metadata-date-check.md) applies to it. With `date_check_mode` set to `enforce`, a match
whose issue date contradicts the filename year is dropped and the next provider gets its turn.
This is worth turning on for a Disney library specifically: short, common, heavily reused titles
are exactly where the other providers produce confident wrong answers.

## Ordering

INDUCKS is more specialised than the other providers and is registered after them, so it changes
nothing for a library with no Disney material in it. For a library that is mostly Disney, order it
ahead of GCD in the per-library provider settings.
