"""Which spellings of a series name are the same series, for FILING.

One table, two consumers, both in ``helpers/collection.py``:

* ``series_name_regex`` -- the filename tier (``generate_filename_pattern``),
  which compiles a wanted series name into a regex and tests filenames.
* ``norm_series_text`` -- the ComicInfo tier (``_comicinfo_series_matches``),
  which compares two names as text.

They used to carry a character table each, written separately, and so they
disagreed: the ComicInfo tier dropped a leading article from either side while
the filename tier made "The" optional only when the *series name* had it. A
download named "The Totally Awesome Hulk 017 (2017).cbz" could therefore never
be filed under "Totally Awesome Hulk" (#780) -- it stayed in TARGET, the issue
stayed wanted, and it was downloaded again.

A new relaxation goes HERE, once, never at a call site.

This is deliberately not the home of every name normaliser in the repo. The
GetComics scorer and the reading-list matcher ask different questions, and
several GetComics keys are stored in the database. See
``.claude/rules/series-name-matching.md``.

Import-light on purpose: ``helpers/collection.py`` is imported by ``monitor.py``
in a process where app.py is not loaded.
"""

import re

from core.filename_chars import FILENAME_ILLEGAL_CHARS

# Apostrophe variants: ASCII, curly single quotes, modifier letter apostrophe.
APOS_CLASS = "['‘’ʼ]"
_APOS_RE = re.compile(APOS_CLASS)

# Unicode dashes: hyphen .. horizontal bar, and the minus sign.
_DASHES = "‐-―−"

# Separators that differ freely between a database name and a filename.
# "Nemesis: Forever", "Nemesis - Forever" and "Nemesis Forever" are one series,
# and a "/" can never appear in a filename at all. Parentheses and brackets are
# deliberately absent: _BENIGN_SERIES_EXTRA reads them to recognise "(2025)".
# "+" is here because the GetComics scorer reads "/", "+" and "&" as one
# crossover separator: it accepts "Batman + Superman #5" for "Batman/Superman",
# and filing has to accept at least what the scorer downloads.
_SEPARATOR_BODY = r"\s\-_:;,\./\\\+" + _DASHES
_SEPARATOR_RE = re.compile("[" + _SEPARATOR_BODY + "]+")

# Characters that must never be REQUIRED of a filename.
#
# "!" is dropped by download hosts and by most people naming a file
# ("Vampyrates!" arrives as "Vampyrates 003"). The rest are the members of
# FILENAME_ILLEGAL_CHARS that are not separators already: CLU's own sanitizer
# removes them from every name it writes, so demanding one means CLU can rename
# a file into a name its own matcher then refuses. Derived, not listed, so a
# character added to the sanitizer is covered here without a second edit.
NEVER_REQUIRED = "!" + "".join(
    ch for ch in FILENAME_ILLEGAL_CHARS if not _SEPARATOR_RE.fullmatch(ch)
)
_NEVER_REQUIRED_BODY = re.escape(NEVER_REQUIRED)

# The class a series NAME is flattened with before it is split into words.
SERIES_SEP_RE = re.compile("[" + _SEPARATOR_BODY + _NEVER_REQUIRED_BODY + "]+")

# What may sit between two words of the name inside a FILENAME.
_WORD_GAP = r"[\s\-_:'\.&\+" + _DASHES + _NEVER_REQUIRED_BODY + r"]*"

# A dropped leading article is the one whole-word relaxation the name
# comparison allows: "Ultimates" really is how a library spells "The
# Ultimates". models/cbl.py imports this through helpers.collection.
LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)

# The filename regex is narrower than LEADING_ARTICLE on purpose. It is the
# tier that moves files, and only "The" has a report behind it.
_OPTIONAL_THE = r"(?:The[\s\-_]+)?"

# Connecting words a filename often omits ("Magik Colossus" for "Magik and
# Colossus"). Safe only inside the anchored filename regex, with an issue
# number after it -- do NOT reuse it for the name comparison, where the
# leftover is the whole question.
OPTIONAL_WORDS = frozenset({"and", "the", "of", "or", "vs", "versus"})

# An optional apostrophe, which may have a space in front of it: GetComics
# titles "Marvel's" as "Marvel 's". The space is allowed only WITH the
# apostrophe, so "Marvel s Avengers" is still not "Marvels Avengers".
_APOS_OPTIONAL = r"(?:\s?" + APOS_CLASS + r")?"


def word_regex(word):
    """Escape one series-name word, making apostrophes optional both ways.

    A possessive is written inconsistently across sources and filenames
    ("World's Finest" vs "Worlds Finest"), so the apostrophe must never be a
    hard requirement -- nor may its absence be. An apostrophe in the name
    becomes optional; a word ending in "s" without one gains an optional
    apostrophe before that "s".
    """
    if _APOS_RE.search(word):
        parts = [re.escape(p) for p in _APOS_RE.split(word)]
        return _APOS_OPTIONAL.join(parts)
    if len(word) > 1 and word[-1].lower() == "s":
        return re.escape(word[:-1]) + _APOS_OPTIONAL + re.escape(word[-1])
    return re.escape(word)


def series_name_regex(series_name):
    """Regex source matching ``series_name`` at the start of a filename.

    Uncompiled and unanchored: ``generate_filename_pattern`` embeds it in the
    rename pattern and adds the issue number after it.

    * A leading "The" is optional in BOTH directions. The GetComics scorer
      already searches with and without it, so it accepts a post the filing
      matcher must then be able to file.
    * An ampersand is the word "and", which ``OPTIONAL_WORDS`` then makes
      optional: "Black & White" files as "Black White", "Black and White" or
      "Black & White" (the word gap absorbs the character). It used to be
      deleted, so "Batman & Robin" could never claim "Batman and Robin 005" --
      a download the scorer had accepted, left in TARGET to be fetched again.
    * Separators and ``NEVER_REQUIRED`` characters become word breaks.
    * ``OPTIONAL_WORDS`` may be missing from the filename.
    """
    working = (series_name or "").strip()
    if working.lower().startswith("the "):
        working = working[4:]

    words = SERIES_SEP_RE.sub(" ", working.replace("&", " and ")).split()
    if all(word.lower() in OPTIONAL_WORDS for word in words):
        # A name made only of punctuation ("!!!") or only of optional words
        # ("&"). Either would leave a pattern that can match nothing at all,
        # and so EVERY filename carrying the right issue number. It stays a
        # literal -- and gets no optional article.
        return re.escape((series_name or "").strip())

    parts = []
    for i, word in enumerate(words):
        escaped = word_regex(word)
        if word.lower() in OPTIONAL_WORDS:
            parts.append(f"(?:{escaped}{_WORD_GAP})?")
        else:
            parts.append(escaped)
            if i < len(words) - 1:
                parts.append(_WORD_GAP)
    return _OPTIONAL_THE + "".join(parts)


_NON_ASCII_WORD = re.compile(r"[^a-z0-9]+")


def ascii_words(value):
    """Lower-cased, with every run of anything but ``a-z0-9`` as one space.

    The punctuation-blind form: "Spider-Man: Blue!" -> "spider man blue". Not
    a filing rule. It keeps no brackets and drops accents with the rest
    ("Élektra" -> "lektra"), which is why the filing comparison is
    ``norm_series_text`` below and why Inducks folds accents itself. Four
    modules each carried this expression; they call it so "what counts as
    punctuation" cannot drift between a duplicate finder, a provider search
    and a keyword test.
    """
    return _NON_ASCII_WORD.sub(" ", (value or "").lower()).strip()


def norm_series_text(value):
    """Lower-cased, punctuation-flattened form for comparing two series names.

    Agrees with ``series_name_regex`` about what is noise: separators and
    ``NEVER_REQUIRED`` characters become one space, and an apostrophe is
    removed outright so "It's Jeff!" and "Its Jeff" are equal. Brackets are
    kept (see ``_SEPARATOR_BODY``). "&" is the word "and", as in the regex --
    the one place the two differ is that the regex may also drop the word.
    """
    text = _APOS_RE.sub("", (value or "").lower()).replace("&", " and ")
    return SERIES_SEP_RE.sub(" ", text).strip()
