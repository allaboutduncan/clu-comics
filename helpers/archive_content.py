"""Is this archive a comic, or only its covers? (#794)

A variant-cover pack, or a 1-3 page "covers" release, is named like the issue
it belongs to, so the filing matcher took it for that issue: the issue read as
owned and the real comic was never downloaded. ``cover_only_reason`` is the one
question the matcher asks before a file may claim a wanted issue.

It is decided from **member names alone**. For a zip that is the central
directory -- nothing is decompressed -- and the answer is memoised on
``(path, mtime, size)``, because a held-back file stays in TARGET and is
matched again on every pass.

"Cannot tell" is ``None``, the same answer as "this is a comic". A download
must never be held back because its archive could not be listed.
"""
import os
import re
import threading
import zipfile

from core.app_logging import app_logger

# A cover word standing on its own. Letters on either side rule it out
# ("discovery", "Undiscovered", "covered"); digits and underscores do not
# ("Nova_001_cover_B", "001cvr").
_COVER_WORD = re.compile(r"(?<![a-z])(?:covers?|cvr|variants?)(?![a-z])", re.IGNORECASE)

# At or under this many pages, cover wording anywhere is enough. Deliberately
# not a page-count rule by itself: a short one-shot is a real comic.
SMALL_PAGE_COUNT = 3

_RAR_MAGIC = b"Rar!"

MAX_VERDICTS = 2000
_verdicts = {}
_verdicts_lock = threading.Lock()


def _has_cover_word(text):
    return bool(text and _COVER_WORD.search(text))


def _list_members(path):
    """Member names, or None when the archive cannot be listed.

    Decided on content, not the extension: a RAR named ``.cbz`` is still a RAR.
    Anything that is neither is None without a tool being spawned.
    """
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path, "r") as zf:
                return zf.namelist()
        with open(path, "rb") as fh:
            if fh.read(4) != _RAR_MAGIC:
                return None
        from helpers import list_rar_entries

        return list_rar_entries(path)
    except Exception as e:
        app_logger.debug(f"Could not list {path} for the cover-only check: {e}")
        return None


def judge_members(names, filename="", folder_name=""):
    """The reason an archive with these member names is not a comic, or None.

    ``names`` is a raw member listing; ``None`` means it could not be read.
    """
    if names is None:
        return None

    from helpers.unwrap import PAGE_IMAGE_EXTS, _meaningful_members

    pages = [
        n for n in _meaningful_members(names)
        if os.path.splitext(n)[1].lower() in PAGE_IMAGE_EXTS
    ]
    if not pages:
        return "it contains no page images"

    page_stems = [os.path.splitext(p)[0] for p in pages]
    if all(_has_cover_word(s) for s in page_stems):
        return f"every one of its {len(pages)} page(s) is a cover"

    if len(pages) <= SMALL_PAGE_COUNT:
        stem = os.path.splitext(filename or "")[0]
        if (
            _has_cover_word(stem)
            or _has_cover_word(folder_name)
            or any(_has_cover_word(s) for s in page_stems)
        ):
            return f"it has {len(pages)} page(s) and is labelled as covers"

    return None


def cover_only_reason(path, filename=None):
    """Why ``path`` must not be filed as an issue, or None to file it.

    None also covers every "cannot tell": a missing file, something that is
    not an archive, a listing that failed.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime, st.st_size)

    with _verdicts_lock:
        if key in _verdicts:
            return _verdicts[key]

    names = _list_members(path)
    reason = judge_members(
        names,
        filename=filename or os.path.basename(path),
        folder_name=os.path.basename(os.path.dirname(path)),
    )

    # An unlistable archive is not remembered: the listing tools can fail for
    # reasons that pass (a timeout on a sleeping NAS), and the answer was the
    # permissive one.
    if names is not None:
        with _verdicts_lock:
            if len(_verdicts) >= MAX_VERDICTS:
                _verdicts.clear()
            _verdicts[key] = reason
    return reason
