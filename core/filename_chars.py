"""
The character map -- the one sanitizer for names CLU writes to disk.

File renames (``cbz_ops.rename.apply_filename_cleanup``), folder names
(``helpers.sanitize_path_segment``) and the reading-list search terms built
from them all go through ``apply_char_map``, so a file and the folder it lives
in can no longer disagree about what "Armageddon / X-Men" becomes (#588).

The map is ``{char: replacement}``. A filesystem-hostile character missing from
it is removed; the map can change what replaces one, never keep one. Other
characters are left alone unless the user adds a row for them.

NOTE: mirrored once in JS as ``CLU.applyCharMap`` (static/js/clu-utils.js).
"""

import re

from core.app_logging import app_logger

# Filesystem-hostile characters, always removed unless the map replaces them.
FILENAME_ILLEGAL_CHARS = '\\/:*?"<>|&$;'

PREF_KEY = "rename_char_replacements"

MAX_REPLACEMENT_LEN = 8


def invalid_replacement_chars(value):
    """The hostile characters in ``value``, sorted. Empty means it is usable."""
    return sorted(set(value or "") & set(FILENAME_ILLEGAL_CHARS))


def normalise_char_map(raw):
    """Coerce a stored or submitted map into a safe ``{char: replacement}``.

    Keys must be one non-space character: the spaces option owns spaces. A
    replacement loses any hostile character it carries and is capped at
    ``MAX_REPLACEMENT_LEN``. ``str.translate`` makes one pass, so a hostile
    character left in a replacement would reach the disk.
    """
    if not isinstance(raw, dict):
        return {}
    cmap = {}
    for key, value in raw.items():
        if not isinstance(key, str) or len(key) != 1 or key.isspace():
            continue
        value = "" if value is None else str(value)
        value = "".join(c for c in value if c not in FILENAME_ILLEGAL_CHARS)
        cmap[key] = value[:MAX_REPLACEMENT_LEN]
    return cmap


def _legacy_char_map(get_pref):
    """Build a map from the pre-map settings (one charset, one replacement)."""
    if not get_pref("rename_clean_specials_enabled", default=False):
        return {}
    charset = get_pref("rename_clean_specials_charset", default="") or ""
    if get_pref("rename_clean_specials_mode", default="remove") == "replace":
        replacement = get_pref("rename_clean_specials_replacement", default="") or ""
    else:
        replacement = ""
    return normalise_char_map({c: replacement for c in charset})


def load_char_map():
    """The user's map. Falls back to the legacy settings until the page is
    saved once, and to ``{}`` (remove every hostile character) on error."""
    try:
        from core.database import get_user_preference

        stored = get_user_preference(PREF_KEY, default=None)
        if stored is None:
            return _legacy_char_map(get_user_preference)
        return normalise_char_map(stored)
    except Exception as e:
        app_logger.warning(f"Failed to load character map from DB: {e}")
        return {}


def apply_char_map(text, cmap):
    """Replace mapped characters, remove unmapped hostile ones, collapse spaces."""
    if not text:
        return text
    table = {c: "" for c in FILENAME_ILLEGAL_CHARS}
    table.update(cmap or {})
    text = text.translate(str.maketrans(table))
    return re.sub(r"\s+", " ", text)
