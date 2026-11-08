"""Folder-level art in the cache volume: covers, header banners and overlays.

Folder art used to be a real ``folder.png`` written into the comic folder. On
a library kept on spinning disks (Unraid's array, a NAS) that meant every
generated cover was a write to the data disk and every grid page a read from
it, so browsing woke disks that the per-comic thumbnail cache had been built to
keep asleep (#720). Folder art now lives beside those thumbnails:

    CACHE_DIR/folder_art/<first 2 hex of the name>/<uuid4 hex>.<ext>

with one ``folder_art`` row per ``(folder_path, kind)`` pointing at it.

Things that look arbitrary and are not:

- **The file name is random, not md5(folder path).** A comic thumbnail can be
  keyed on its path because it can be regenerated from the comic at any time.
  Folder art often cannot: an uploaded image exists nowhere else. With a random
  name a folder rename is a row update (``move_path_references`` follows
  ``folder_art.folder_path``), and nothing on disk has to move.
- **The cache wins over the disk, and the disk is never written or deleted.**
  A ``folder.png`` already in a library folder (uploaded by hand, or generated
  by an older CLU) is still found by :func:`find_art`. The first time it is
  *served* it is copied in (``source='imported'``), so after that one read the
  disk is not consulted again. It is left where it is: it may be there for
  another application, and we cannot tell a generated one from a chosen one.
- **A disk miss is remembered for a while** (``DISK_MISS_TTL_SECONDS``). A
  folder with no art at all is the common case, and without the memo every
  page view would stat five names in it on the data disk.
- **Writes go through a temp file in the shard plus ``os.replace``**, with the
  shard's permissions, for the same reasons as ``core/thumbnail_cache.py``
  (#548). The new file gets a new name, so the old one is unlinked only once
  the row points away from it.
- **The shard directories sit at depth 2 under /cache**, which the bounded
  chown walk in ``entrypoint.sh`` reaches. Do not nest deeper.
"""

import io
import os
import tempfile
import threading
import time
import uuid

from core.app_logging import app_logger
from core.config import get_cache_dir
from core.database import db_conn

#########################
#   Kinds               #
#########################

# Every kind of folder-level art, with the extensions each may have, in the
# legacy on-disk lookup's priority order. The folder list is the shared one in
# helpers -- see helpers.FOLDER_THUMBNAIL_EXTENSIONS for why there is only one.
from helpers import FOLDER_THUMBNAIL_EXTENSIONS  # noqa: E402

KIND_EXTENSIONS = {
    "folder": FOLDER_THUMBNAIL_EXTENSIONS,
    "header": (".jpg", ".png", ".gif", ".jpeg", ".webp"),
    "overlay": (".png",),
}
KINDS = frozenset(KIND_EXTENSIONS)

# The file name each kind has on disk (``folder.png``, ``header.jpg``...). The
# stem is the kind; that is also how an upload is recognised as folder art.
MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

SOURCES = frozenset({"generated", "uploaded", "imported", "custom"})

DISK_MISS_TTL_SECONDS = 30 * 60
MAX_DISK_MISSES_TRACKED = 20000

# A file in the store that no row points at is removed by prune_orphans only
# once it is this old: a put_art in progress writes the file before the row.
ORPHAN_FILE_MIN_AGE_SECONDS = 60 * 60

_miss_lock = threading.Lock()
_disk_misses = {}  # (folder_path, kind) -> monotonic time of the miss


def kind_for_filename(filename):
    """The kind an uploaded file is, by name (``header.jpg`` -> ``'header'``).

    None for anything that is not folder art -- including a known stem with an
    extension that kind does not take (``overlay.jpg``).
    """
    stem, ext = os.path.splitext(os.path.basename(filename or ""))
    stem, ext = stem.lower(), ext.lower()
    if stem in KIND_EXTENSIONS and ext in KIND_EXTENSIONS[stem]:
        return stem
    return None


#########################
#   Store Location      #
#########################

def art_root(cache_dir=None):
    """Directory holding the sharded folder art."""
    return os.path.join(cache_dir or get_cache_dir(), "folder_art")


def _abs(rel_file):
    return os.path.join(art_root(), rel_file)


#########################
#   Reading             #
#########################

def _record(row):
    return {
        "folder_path": row["folder_path"],
        "kind": row["kind"],
        "path": _abs(row["file"]),
        "file": row["file"],
        "source": row["source"],
        "updated_at": row["updated_at"],
        "location": "cache",
    }


def get_art(folder_path, kind="folder"):
    """The cached art for a folder, as a record dict, or None.

    A row whose file has gone (someone cleared /cache by hand) is dropped, so
    the folder falls back to its disk art or to generation.
    """
    if not folder_path or kind not in KINDS:
        return None
    try:
        with db_conn() as conn:
            if not conn:
                return None
            row = conn.execute(
                "SELECT folder_path, kind, file, source, updated_at FROM folder_art "
                "WHERE folder_path = ? AND kind = ?",
                (folder_path, kind),
            ).fetchone()
    except Exception as e:
        app_logger.error(f"Failed to read folder art for {folder_path}: {e}")
        return None
    if not row:
        return None
    rec = _record(row)
    if not os.path.isfile(rec["path"]):
        _delete_rows([(folder_path, kind)])
        return None
    return rec


def get_art_batch(folder_paths, kind="folder"):
    """``{folder_path: record}`` for every folder in the list that has cached art.

    One query per 500 folders instead of one stat per folder on the data disk.
    Folders without cached art are simply absent from the result.
    """
    paths = [p for p in dict.fromkeys(folder_paths or []) if p]
    if not paths or kind not in KINDS:
        return {}
    found = {}
    try:
        with db_conn() as conn:
            if not conn:
                return {}
            for i in range(0, len(paths), 500):
                chunk = paths[i:i + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    "SELECT folder_path, kind, file, source, updated_at FROM folder_art "
                    f"WHERE kind = ? AND folder_path IN ({marks})",
                    (kind, *chunk),
                ).fetchall()
                for row in rows:
                    rec = _record(row)
                    if os.path.isfile(rec["path"]):
                        found[rec["folder_path"]] = rec
    except Exception as e:
        app_logger.error(f"Failed to read folder art batch: {e}")
        return {}
    return found


def folders_with_cached_art(kind="folder"):
    """Every folder path with a cached row of this kind.

    For whole-library walks (the index builders), which test thousands of
    folders and must not issue one query each. Rows only -- no file check.
    """
    try:
        with db_conn() as conn:
            if not conn:
                return set()
            rows = conn.execute(
                "SELECT folder_path FROM folder_art WHERE kind = ?", (kind,)
            ).fetchall()
        return {r["folder_path"] for r in rows}
    except Exception as e:
        app_logger.error(f"Failed to list folders with art: {e}")
        return set()


def _miss_is_fresh(key):
    with _miss_lock:
        at = _disk_misses.get(key)
        if at is None:
            return False
        if time.monotonic() - at < DISK_MISS_TTL_SECONDS:
            return True
        del _disk_misses[key]
        return False


def _note_miss(key):
    with _miss_lock:
        _disk_misses[key] = time.monotonic()
        if len(_disk_misses) > MAX_DISK_MISSES_TRACKED:
            cutoff = time.monotonic() - DISK_MISS_TTL_SECONDS
            for k in [k for k, t in _disk_misses.items() if t < cutoff]:
                del _disk_misses[k]
            if len(_disk_misses) > MAX_DISK_MISSES_TRACKED:
                _disk_misses.clear()


def _forget_misses(folder_path):
    with _miss_lock:
        for k in [k for k in _disk_misses if k[0] == folder_path]:
            del _disk_misses[k]


def reset_state():
    """Clear the in-memory disk-miss memo. Test-support only."""
    with _miss_lock:
        _disk_misses.clear()


def find_disk_art(folder_path, kind="folder"):
    """Legacy art sitting *in* the folder (``folder.png``, ``header.jpg``...).

    Stat-based: probes only the lowercase canonical names, never ``listdir``
    (a publisher folder can hold thousands of series). A miss is remembered
    for ``DISK_MISS_TTL_SECONDS`` so a folder without art costs the data disk
    nothing on the next page view.
    """
    if not folder_path or kind not in KINDS:
        return None
    key = (folder_path, kind)
    if _miss_is_fresh(key):
        return None
    for ext in KIND_EXTENSIONS[kind]:
        candidate = os.path.join(folder_path, f"{kind}{ext}")
        try:
            if os.path.isfile(candidate):
                return candidate
        except OSError:
            continue
    _note_miss(key)
    return None


def _disk_record(folder_path, kind):
    disk = find_disk_art(folder_path, kind)
    if not disk:
        return None
    try:
        mtime = os.path.getmtime(disk)
    except OSError:
        mtime = 0
    return {
        "folder_path": folder_path,
        "kind": kind,
        "path": disk,
        "file": None,
        "source": "disk",
        "updated_at": mtime,
        "location": "disk",
    }


def find_art(folder_path, kind="folder"):
    """The art a folder shows: cached first, then legacy art on disk.

    Returns a record dict (``location`` is ``'cache'`` or ``'disk'``) or None.
    Never imports -- that happens when the image is actually served
    (:func:`import_disk_art`), so listing a folder does not read image bytes.
    """
    return get_art(folder_path, kind) or _disk_record(folder_path, kind)


def find_art_batch(folder_paths, kind="folder"):
    """``{folder_path: record or None}``: :func:`find_art` for many folders.

    The cache is asked once for all of them; only the misses touch the disk.
    """
    paths = [p for p in dict.fromkeys(folder_paths or []) if p]
    cached = get_art_batch(paths, kind)
    return {
        p: cached[p] if p in cached else _disk_record(p, kind)
        for p in paths
    }


def has_art(folder_path, kind="folder"):
    """True when the folder has art of this kind, cached or on disk."""
    return find_art(folder_path, kind) is not None


def art_url(folder_path, kind="folder", record=None, external=False):
    """URL that serves a folder's art, or None when it has none.

    Always names the *folder* and the kind, never a file: the route resolves
    the image itself, so a cache path never reaches the client and cannot be
    asked for. ``v`` changes whenever the art does, which is what lets the
    browser cache the image without showing a stale one after a regenerate.
    Needs an application/request context (``url_for``).
    """
    from flask import url_for

    if record is None:
        record = find_art(folder_path, kind)
    if not record:
        return None
    return url_for(
        "collection.serve_folder_thumbnail",
        folder=folder_path,
        kind=kind,
        v=int(record.get("updated_at") or 0),
        _external=external,
    )


#########################
#   Writing             #
#########################

def _encode(image, ext):
    """Bytes for ``image`` -- either bytes already, or a PIL image to encode."""
    if isinstance(image, (bytes, bytearray)):
        return bytes(image)
    fmt = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF",
           ".webp": "WEBP"}[ext]
    buf = io.BytesIO()
    img = image
    if fmt == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.save(buf, format=fmt)
    return buf.getvalue()


def _write_file(data, ext):
    """Write ``data`` into a fresh store file and return its path relative to the root."""
    name = uuid.uuid4().hex
    rel = os.path.join(name[:2], f"{name}{ext}")
    dest = _abs(rel)
    shard = os.path.dirname(dest)
    os.makedirs(shard, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=shard, suffix=f"{ext}.tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        # mkstemp hands back 0600; match the shard so the file can be served
        # by whichever user CLU ends up running as.
        from helpers import match_parent_permissions

        match_parent_permissions(tmp_path)
        os.replace(tmp_path, dest)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return rel


def _unlink_quietly(rel_file):
    if not rel_file:
        return
    try:
        os.unlink(_abs(rel_file))
    except FileNotFoundError:
        pass
    except OSError as e:
        app_logger.warning(f"Could not remove old folder art {rel_file}: {e}")


def put_art(folder_path, kind, image, ext, source):
    """Store art for a folder, replacing any it had of that kind.

    ``image`` is the encoded bytes or a PIL image. Returns the new record, or
    None when it could not be stored (logged). Never raises.
    """
    ext = (ext or "").lower()
    if not ext.startswith("."):
        ext = f".{ext}"
    if not folder_path or kind not in KINDS or source not in SOURCES:
        app_logger.error(f"Refusing folder art write: {folder_path!r} {kind!r} {source!r}")
        return None
    if ext not in KIND_EXTENSIONS[kind]:
        app_logger.error(f"Refusing folder art write: {kind} cannot be {ext}")
        return None

    try:
        data = _encode(image, ext)
        rel = _write_file(data, ext)
    except PermissionError as e:
        app_logger.error(
            f"Permission denied writing folder art for {folder_path}: {e}. "
            f"The cache volume is not writable by the user CLU runs as — set "
            f"PUID/PGID to match its owner, or chown it to them."
        )
        return None
    except Exception as e:
        app_logger.error(f"Failed to write folder art for {folder_path}: {e}")
        return None

    now = time.time()
    old_file = None
    try:
        with db_conn() as conn:
            if not conn:
                raise RuntimeError("no database connection")
            row = conn.execute(
                "SELECT file FROM folder_art WHERE folder_path = ? AND kind = ?",
                (folder_path, kind),
            ).fetchone()
            old_file = row["file"] if row else None
            conn.execute(
                "INSERT INTO folder_art (folder_path, kind, file, source, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(folder_path, kind) DO UPDATE SET "
                "file = excluded.file, source = excluded.source, "
                "updated_at = excluded.updated_at",
                (folder_path, kind, rel, source, now),
            )
            conn.commit()
    except Exception as e:
        app_logger.error(f"Failed to record folder art for {folder_path}: {e}")
        _unlink_quietly(rel)
        return None

    if old_file and old_file != rel:
        _unlink_quietly(old_file)
    _forget_misses(folder_path)
    return {
        "folder_path": folder_path,
        "kind": kind,
        "path": _abs(rel),
        "file": rel,
        "source": source,
        "updated_at": now,
        "location": "cache",
    }


def import_disk_art(folder_path, kind, disk_path):
    """Copy legacy on-disk art into the store. The disk file is left alone.

    Returns the cache record, or None when the copy failed (the caller then
    serves the disk file as before).
    """
    ext = os.path.splitext(disk_path)[1].lower()
    try:
        with open(disk_path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        app_logger.warning(f"Could not read folder art {disk_path} to import it: {e}")
        return None
    rec = put_art(folder_path, kind, data, ext, "imported")
    if rec:
        app_logger.debug(f"Imported folder art into the cache: {disk_path}")
    return rec


def _delete_rows(keys):
    """Delete ``(folder_path, kind)`` rows and their files. Returns how many went."""
    removed = 0
    try:
        with db_conn() as conn:
            if not conn:
                return 0
            files = []
            for folder_path, kind in keys:
                row = conn.execute(
                    "SELECT file FROM folder_art WHERE folder_path = ? AND kind = ?",
                    (folder_path, kind),
                ).fetchone()
                if row:
                    files.append(row["file"])
                cur = conn.execute(
                    "DELETE FROM folder_art WHERE folder_path = ? AND kind = ?",
                    (folder_path, kind),
                )
                removed += cur.rowcount
            conn.commit()
    except Exception as e:
        app_logger.error(f"Failed to delete folder art rows: {e}")
        return 0
    for rel in files:
        _unlink_quietly(rel)
    return removed


def delete_art(folder_path, kind=None):
    """Remove a folder's cached art (one kind, or every kind). Never raises.

    Legacy art on disk is not touched.
    """
    kinds = [kind] if kind else sorted(KINDS)
    removed = _delete_rows([(folder_path, k) for k in kinds if k in KINDS])
    _forget_misses(folder_path)
    return removed


def prune_orphans():
    """Drop art for folders that are provably gone, and files nothing points at.

    Not hooked to deletion: a folder moved to the trash can be restored, and
    the watcher misses deletes anyway. Pruning needs positive evidence
    (``is_path_provably_gone``), so a sleeping NAS does not lose its art.
    Returns ``(rows_removed, files_removed)``. Never raises.
    """
    from helpers.library import get_library_roots, is_path_provably_gone

    try:
        with db_conn() as conn:
            if not conn:
                return (0, 0)
            rows = conn.execute(
                "SELECT folder_path, kind, file FROM folder_art"
            ).fetchall()
    except Exception as e:
        app_logger.error(f"Failed to read folder art for pruning: {e}")
        return (0, 0)

    roots = get_library_roots()
    mounted = {}
    gone = [
        (r["folder_path"], r["kind"]) for r in rows
        if is_path_provably_gone(r["folder_path"], roots=roots, mounted=mounted)
    ]
    rows_removed = _delete_rows(gone) if gone else 0

    referenced = {
        os.path.normcase(os.path.normpath(r["file"])) for r in rows
        if (r["folder_path"], r["kind"]) not in set(gone)
    }
    files_removed = 0
    root = art_root()
    cutoff = time.time() - ORPHAN_FILE_MIN_AGE_SECONDS
    try:
        shards = os.listdir(root)
    except OSError:
        shards = []
    for shard in shards:
        shard_dir = os.path.join(root, shard)
        if not os.path.isdir(shard_dir):
            continue
        try:
            names = os.listdir(shard_dir)
        except OSError:
            continue
        for name in names:
            rel = os.path.normcase(os.path.normpath(os.path.join(shard, name)))
            if rel in referenced:
                continue
            full = os.path.join(shard_dir, name)
            try:
                if os.path.getmtime(full) > cutoff:
                    continue
                os.unlink(full)
                files_removed += 1
            except OSError:
                continue

    if rows_removed or files_removed:
        app_logger.info(
            f"Pruned folder art: {rows_removed} row(s) for deleted folders, "
            f"{files_removed} unreferenced file(s)"
        )
    return (rows_removed, files_removed)
