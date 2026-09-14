"""The per-comic thumbnail cache: where it lives, how it is written, and when
it is stale.

A comic's thumbnail is a JPEG at
``CACHE_DIR/thumbnails/<first 2 hex of md5(path)>/<md5(path)>.jpg``. That
formula, the extract-first-page-and-resize body, and the ``thumbnail_jobs``
bookkeeping used to be copy-pasted in nine places — ``app.py`` three times,
``wrapped.py`` once, and once in every mutating op under ``cbz_ops/`` *except*
``rebuild.py``. The omission was invisible, and it is exactly why rebuilding a
whole series from the File Manager left every thumbnail stale while rebuilding
one issue at a time worked (issue #548). This module is the single owner, so a
new mutating op has one call to make and one place to find it.

Three things here look like detail and are not:

- **The path formula must not change.** It is ``md5`` of the *path*, not the
  content, so the cache has no way to notice a rewritten file — and every
  already-cached JPEG on every install is addressed by it. Changing the hash,
  the shard width or the extension orphans the lot.
- **Writes go through a temp file and ``os.replace``.** Saving straight onto
  the cache path opens the *existing* file for writing, which fails with
  ``EACCES`` when that file is owned by root and CLU is running as ``PUID`` —
  the container's documented root-fallback leaves exactly those files behind.
  ``os.replace`` only needs the shard *directory* to be writable, so it
  succeeds where the in-place save could not, and it makes the write atomic
  into the bargain: an interrupted save used to leave a truncated JPEG that
  ``os.path.exists()`` happily served forever.
- **A failed regeneration records ``error``.** The old copies only wrote the
  row inside the success branch, so a write that failed left a stale
  ``completed`` row pointing at a stale image — the cache claimed to be fresh
  and nothing ever revisited it.
"""

import hashlib
import os
import tempfile

from core.app_logging import app_logger
from core.config import config

# Every historical copy of this block resized to 300px height at quality 85.
THUMBNAIL_HEIGHT = 300
JPEG_QUALITY = 85

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


#########################
#   Cache Location      #
#########################

def thumbnails_root(cache_dir=None):
    """Directory holding the sharded per-comic thumbnails."""
    if cache_dir is None:
        cache_dir = config.get("SETTINGS", "CACHE_DIR", fallback="/cache")
    return os.path.join(cache_dir, "thumbnails")


def thumbnail_cache_path(file_path, cache_dir=None):
    """Cache path for ``file_path``.

    Byte-compatible with the nine inline copies this replaces — same md5 of the
    UTF-8 path, same two-character shard, same ``.jpg``. Do not "improve" it:
    every thumbnail already on disk is addressed this way.
    """
    path_hash = hashlib.md5(
        file_path.encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    return os.path.join(thumbnails_root(cache_dir), path_hash[:2], f"{path_hash}.jpg")


def is_thumbnail_stale(file_path, cache_path=None):
    """True when the comic has been rewritten since its thumbnail was made.

    Returns False when either side cannot be stat'd. "I cannot tell" must mean
    "serve what we have": the alternative is regenerating on every request for
    a comic that has been moved or unmounted.
    """
    if cache_path is None:
        cache_path = thumbnail_cache_path(file_path)
    try:
        return os.path.getmtime(file_path) > os.path.getmtime(cache_path)
    except OSError:
        return False


def file_changed_since(file_path, recorded_mtime):
    """True when the comic has been rewritten since ``recorded_mtime``.

    Used to decide whether a failed thumbnail deserves another attempt. A NULL
    mtime means the row predates the column, so the answer is "we cannot tell"
    — and the useful reading of that is *retry once*, after which the row
    carries a real mtime and settles.
    """
    if recorded_mtime is None:
        return True
    try:
        return os.path.getmtime(file_path) > float(recorded_mtime)
    except (OSError, TypeError, ValueError):
        return False


#########################
#   Writing             #
#########################

def write_cached_thumbnail(img, cache_path):
    """Write a PIL image to ``cache_path`` atomically and accessibly.

    Staged as a temp file *in the same shard directory* (so the replace is a
    rename, not a cross-filesystem copy that would carry mkstemp's 0600 across),
    permission-matched to that directory, then moved into place. When the
    destination exists but belongs to someone else, the replace still succeeds —
    that is the whole point, see the module docstring.
    """
    shard_dir = os.path.dirname(cache_path)
    try:
        os.makedirs(shard_dir, exist_ok=True)
    except OSError as e:
        app_logger.error(
            f"Cannot create thumbnail cache directory {shard_dir}: {e}. "
            f"Check that the cache volume is writable by PUID:PGID."
        )
        return False

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=shard_dir, suffix=".jpg.tmp")
        os.close(fd)
        img.save(tmp_path, format="JPEG", quality=JPEG_QUALITY)

        # mkstemp hands back 0600; match the shard dir so a differently-owned
        # process (a second container, a NAS account) can replace it next time.
        from helpers import match_parent_permissions

        match_parent_permissions(tmp_path)

        try:
            os.replace(tmp_path, cache_path)
        except PermissionError:
            # Destination exists and is not ours, on a filesystem where replace
            # needs write access to it as well. Unlinking needs only the
            # directory, which we demonstrably have.
            os.unlink(cache_path)
            os.replace(tmp_path, cache_path)

        tmp_path = None
        return True

    except PermissionError as e:
        app_logger.error(
            f"Permission denied writing thumbnail {cache_path}: {e}. "
            f"The cache volume is not writable by the user CLU runs as — set "
            f"PUID/PGID to match its owner, or chown it to them."
        )
        return False
    except Exception as e:
        app_logger.error(f"Failed to write thumbnail {cache_path}: {e}")
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


#########################
#   Generation          #
#########################

def _sorted_page_names(names):
    """Page entries of an archive, in the order a reader would see them.

    Case-insensitive, and without the junk entries that would otherwise become
    a comic's cover: ``__MACOSX`` resource forks and dotfiles.
    """
    return sorted(
        (
            n
            for n in names
            if os.path.splitext(n.lower())[1] in IMAGE_EXTENSIONS
            and not n.startswith("__MACOSX")
            and not os.path.basename(n).startswith(".")
        ),
        key=str.lower,
    )


def _render(image_file):
    """Resize an open page stream to the cache's standard size."""
    from PIL import Image

    img = Image.open(image_file)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")
    aspect_ratio = img.width / img.height
    img.thumbnail(
        (int(THUMBNAIL_HEIGHT * aspect_ratio), THUMBNAIL_HEIGHT),
        Image.Resampling.LANCZOS,
    )
    # Decode before the caller closes the archive. PIL is lazy, and thumbnail()
    # returns without loading anything when the page is already smaller than the
    # target -- the copies this replaces got away with saving inside the `with`
    # block, and doing it outside fails with "seek on closed file".
    img.load()
    return img


def _first_page(file_path):
    """Open the first page of a comic and return it as a resized PIL image.

    Returns None when the archive holds no pages or the type is unsupported.
    Raises whatever the archive libraries raise — the caller records the failure.
    """
    lowered = file_path.lower()

    if lowered.endswith((".cbz", ".zip")):
        import zipfile

        with zipfile.ZipFile(file_path, "r") as zf:
            pages = _sorted_page_names(zf.namelist())
            if not pages:
                return None
            with zf.open(pages[0]) as page:
                return _render(page)

    if lowered.endswith((".cbr", ".rar")):
        import rarfile

        with rarfile.RarFile(file_path, "r") as rf:
            pages = _sorted_page_names(rf.namelist())
            if not pages:
                return None
            with rf.open(pages[0]) as page:
                return _render(page)

    app_logger.warning(f"Unsupported file type for thumbnail: {file_path}")
    return None


def _record_job(file_path, status):
    """Best-effort ``thumbnail_jobs`` upsert. Never raises — the thumbnail on
    disk is the real artifact; this row only decides what the serving route
    does while one is missing."""
    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        if not conn:
            return
        try:
            try:
                file_mtime = os.path.getmtime(file_path)
            except OSError:
                file_mtime = None
            conn.execute(
                "INSERT OR REPLACE INTO thumbnail_jobs "
                "(path, status, file_mtime, updated_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (file_path, status, file_mtime),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        app_logger.error(f"Could not record thumbnail job for {file_path}: {e}")


def regenerate_thumbnail(file_path, cache_path=None, record_job=True):
    """(Re)build the cached thumbnail for one comic.

    This is the call every mutating op owes the cache after rewriting an
    archive. Returns True on success; never raises, so a caller can append it
    to a pipeline without wrapping it.
    """
    if cache_path is None:
        cache_path = thumbnail_cache_path(file_path)

    try:
        img = _first_page(file_path)
        if img is None:
            app_logger.warning(f"No images found in {file_path}")
            if record_job:
                _record_job(file_path, "error")
            return False

        if not write_cached_thumbnail(img, cache_path):
            if record_job:
                _record_job(file_path, "error")
            return False

        if record_job:
            _record_job(file_path, "completed")
        app_logger.info(f"Thumbnail regenerated successfully for {file_path}")
        return True

    except Exception as e:
        app_logger.error(f"Error regenerating thumbnail for {file_path}: {e}")
        if record_job:
            _record_job(file_path, "error")
        return False


def invalidate_thumbnail(file_path):
    """Drop a comic's cached thumbnail and its job row.

    For paths that stop existing — a delete, or the old name after a rename.
    Regeneration is `regenerate_thumbnail`; this is for when there is nothing
    left to regenerate from.
    """
    cache_path = thumbnail_cache_path(file_path)
    try:
        if os.path.exists(cache_path):
            os.remove(cache_path)
    except OSError as e:
        app_logger.error(f"Could not remove thumbnail {cache_path}: {e}")

    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        if not conn:
            return
        try:
            conn.execute("DELETE FROM thumbnail_jobs WHERE path = ?", (file_path,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        app_logger.error(f"Could not clear thumbnail job for {file_path}: {e}")
