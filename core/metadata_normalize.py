"""
Canonicalise ComicInfo credit/character values.

External taggers (ComicTagger, Metron/ComicVine plugins) append their own
provider ID to credit values:

    <Penciller>Ron Lim [3258]</Penciller>
    <Characters>Aquaman [2357]</Characters>

CLU stores what it reads, so "Ron Lim" and "Ron Lim [3258]" fragment into two
distinct people across browse pages and Insights counts. These helpers strip
*numeric-only* bracketed segments and leave every other bracket alone —
"[uncredited]", "[Bruce Wayne]", "[1st printing]" are meaningful and are kept
verbatim.

This module deliberately has no project imports: it is a leaf used by
core/database.py, app.py and routes/.
"""

import re

# A bracketed run of ASCII digits, optionally padded: "[3258]", "[ 41502 ]".
# Deliberately [0-9] and not \d — \d matches Arabic-Indic and other Unicode
# digits, which are far more likely to be part of a real name than a
# provider ID.
_PROVIDER_ID_RE = re.compile(r"\[\s*[0-9]+\s*\]")

_WHITESPACE_RE = re.compile(r"\s+")


def strip_provider_ids(text):
    """
    Remove numeric bracketed provider IDs from a single value.

    "Ron Lim [3258]"       -> "Ron Lim"
    "Ron [3258] Lim"       -> "Ron Lim"
    "[3258]"               -> ""
    "Ron Lim [uncredited]" -> "Ron Lim [uncredited]"   (non-numeric: kept)
    None / "" / 0          -> ""

    Non-str input is coerced with str(). Whitespace is only collapsed when a
    substitution actually happened, so clean values come back unchanged apart
    from a .strip() — this keeps the one-time backfill from churning rows it
    has no reason to touch.
    """
    if not text:
        return ""
    s = str(text)
    if "[" not in s:
        return s.strip()
    cleaned = _PROVIDER_ID_RE.sub(" ", s)
    if cleaned == s:
        return s.strip()
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def split_credit_list(text, sep=","):
    """
    Tokenise a comma-separated credit/character field into cleaned, ordered,
    case-insensitively de-duplicated tokens.

    "Ron Lim [3258], Ron Lim"  -> ("Ron Lim",)
    "A [1],,B"                 -> ("A", "B")
    "[3258]"                   -> ()
    None / ""                  -> ()

    De-dup keeps the first spelling seen. This is the single tokenizer for the
    whole app — core.database._split_tag_values delegates here.
    """
    if not text:
        return ()
    out = []
    seen = set()
    for part in str(text).split(sep):
        value = strip_provider_ids(part)
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return tuple(out)


def normalize_credit_list(text, sep=","):
    """
    Canonical string form of a credit list: cleaned tokens joined with ", ".

    "Ron Lim [3258], Ron Lim" -> "Ron Lim"
    "A [1],B [2]"             -> "A, B"
    "[3258]"                  -> ""
    """
    return ", ".join(split_credit_list(text, sep))
