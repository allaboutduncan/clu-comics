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

## How a folder is matched to a publication

Two things make this harder than it is for the other providers, and they pull in opposite
directions.

A Disney title names several runs at once: eleven Italian publications reduce to the name
`Topolino` — the libretto, the giornale, and nine reprint runs — so the title alone can never
identify one. At the same time these folders are very often named after a slice of a run rather
than after the publication: `Topolino anno 1975`, `Anno 1986 vol 1571-1622`, `Albo d'oro v2`. So
the name has to be read loosely, and then the result has to be narrowed strictly.

**Finding candidates.** Three names are tried in turn, and the first that matches anything at all
wins: the folder name; the folder name with a trailing run marker removed (`anno 1975`,
`vol 1571-1622`, `v2`, `Repack`); and the series name CLU parses out of the filename itself.
Within one name, the title is looked up as spelled, then with a trailing parenthetical qualifier
removed, then with a series ordinal normalised (`II Serie` and `2a serie` both mean
`Seconda Serie`), and finally — only if nothing else matched — on its significant words with
Italian articles and prepositions ignored, so `Le grandi storie di Walt Disney - L'opera omnia di
Romano Scarpa` reaches what INDUCKS calls `Le grandi storie Disney - L'opera omnia di Romano
Scarpa`.

A name that spells out a qualifier always wins over the bare one: a folder called
`Topolino (giornale)` is never answered by the publication merely called `Topolino`.

**Narrowing them.** The issue number then decides between whatever the name produced. Of the
eleven publications called `Topolino`, only `it/TL` has an issue 1500 at all — so the loose name
is safe, because the narrowing is evidence the caller already has rather than a preference. If
more than one publication still holds that number, the year in the filename is compared with that
issue's own date, and after that an exactly-matching title beats a qualifier-stripped one.

This is also what lets a run continue into its second series: `Super Almanacco Paperino` names two
publications, and issues 1–17 come from one while 18 onwards come from the other. Both are
offered, and the issue number picks correctly for each file.

**Ambiguity is refused, not guessed.** If more than one publication survives all of that, nothing
is written and the folder goes to the review queue, where you choose. Picking one arbitrarily is
how a library ends up confidently mislabelled. Equally, if no publication holds the issue number,
the provider returns nothing and the next provider gets its turn — better than answering from the
wrong run.

## The date check

INDUCKS carries a real per-issue publication date, so the [metadata date
check](metadata-date-check.md) applies to it.

One caveat specific to this material: a comic whose issue number is a bare four-digit number in
the 1900–2099 range — `Topolino 1904.cbz` — has that number read as a publication year, so the
check sees a disagreement that is not there. Under `enforce` those files are rejected. Either
leave the check on `log` for a folder of high-numbered issues, or name the files with the year as
well, which resolves it.
 With `date_check_mode` set to `enforce`, a match
whose issue date contradicts the filename year is dropped and the next provider gets its turn.
This is worth turning on for a Disney library specifically: short, common, heavily reused titles
are exactly where the other providers produce confident wrong answers.

## Ordering

INDUCKS is more specialised than the other providers and is registered after them, so it changes
nothing for a library with no Disney material in it. For a library that is mostly Disney, order it
ahead of GCD in the per-library provider settings.
