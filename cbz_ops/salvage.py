"""Salvage rebuild: rewrite a CBZ whose pages fail their CRC but still decode.

A ``Bad CRC-32`` means the stored bytes no longer match the checksum the
archive recorded -- often a handful of flipped bits inside an image, which
still decodes with a smear nobody notices. Rebuild cannot help: it extracts
inside one ``try`` and the first bad CRC aborts it. Here each damaged entry is
re-read with ``ZipExtFile._expected_crc = None`` (CPython's own "no reference
value" path, which skips the check) and kept only if Pillow can fully decode
it.

Nothing is written unless *every* damaged entry decodes. A page that does not
is reported by number so the user can remove it in Edit; a partly-readable
original is worth more than a rewrite that silently drops a page.

Must never import from ``app`` -- see CLAUDE.md.
"""

import io
import os
import zipfile
import zlib

import defusedxml.ElementTree as SafeET

from core.app_logging import app_logger
from helpers import open_zip_for_write, archive_error_detail

PAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def is_crc_error(exc):
    return isinstance(exc, zipfile.BadZipFile) and "bad crc-32" in str(exc).lower()


def read_entry_lenient(zf, info):
    """``(data, crc_ok)`` for one entry, reading past a CRC mismatch.

    Only the checksum is waived. A damaged deflate stream (``zlib.error``) or a
    truncated entry still raises: those bytes are genuinely not there.
    """
    try:
        return zf.read(info), True
    except zipfile.BadZipFile as e:
        if not is_crc_error(e):
            raise
    with zf.open(info) as f:
        f._expected_crc = None
        return f.read(), False


def is_page(name):
    base = os.path.basename(name)
    return name.lower().endswith(PAGE_EXTENSIONS) and not base.startswith(("._", "."))


def decodes(name, data):
    """True when *data* is a usable page image (or a parseable ComicInfo.xml)."""
    if is_page(name):
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as img:
                img.load()
            return True
        except Exception:
            return False
    if os.path.basename(name).lower() == "comicinfo.xml":
        try:
            SafeET.fromstring(data)
            return True
        except Exception:
            # ParseError, or a DefusedXmlException for entity tricks: either
            # way it is not a ComicInfo.xml worth keeping.
            return False
    return False


def first_undecodable_page(path):
    """``(name, reason)`` for the first page of *path* that will not decode, or None.

    The archive's CRCs say nothing once it has been repacked: Edit's save writes
    whatever bytes it extracted with fresh checksums, so a broken page left in
    place would pass ``testzip`` and launder the damage. This is the check that
    does not.
    """
    from PIL import Image

    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir() or not is_page(info.filename):
                continue
            try:
                with Image.open(io.BytesIO(zf.read(info))) as img:
                    img.load()
            except Exception as e:
                return info.filename, archive_error_detail(e)
    return None


def page_numbers(names):
    """``{name: 1-based page}`` in the order a reader shows them."""
    pages = sorted((n for n in names if is_page(n)), key=lambda n: n.lower())
    return {n: i for i, n in enumerate(pages, 1)}


def _describe(entries):
    parts = []
    for e in entries:
        parts.append(f"page {e['page']}" if e.get("page") else e["name"])
    return ", ".join(parts)


def salvage_cbz(path):
    """Rewrite *path* in place if every CRC-damaged entry still decodes.

    Returns ``{ok, salvaged, failed, message}``; ``salvaged`` and ``failed`` are
    lists of ``{name, page, reason?}``. On failure the file is untouched. The new
    archive is assembled off-volume by :func:`helpers.open_zip_for_write` and
    moved into place last, so there is no half-written state to restore.
    """
    salvaged, failed, entries = [], [], []
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            pages = page_numbers(i.filename for i in infos)
            for info in infos:
                if info.is_dir():
                    continue
                page = pages.get(info.filename)
                try:
                    data, crc_ok = read_entry_lenient(zf, info)
                except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as e:
                    failed.append({"name": info.filename, "page": page,
                                   "reason": archive_error_detail(e)})
                    continue
                if not crc_ok:
                    if decodes(info.filename, data):
                        salvaged.append({"name": info.filename, "page": page})
                    else:
                        failed.append({"name": info.filename, "page": page,
                                       "reason": "does not decode"})
                        continue
                entries.append((info, data))
    except Exception as e:
        return {"ok": False, "salvaged": [], "failed": [],
                "message": f"Could not open the archive: {archive_error_detail(e)}"}

    if failed:
        return {"ok": False, "salvaged": salvaged, "failed": failed,
                "message": f"Could not salvage {_describe(failed)} — the damaged "
                           "data does not decode. Open the comic in Edit and remove "
                           "it, or replace the file."}
    if not salvaged:
        return {"ok": False, "salvaged": [], "failed": [],
                "message": "Every entry passed its checksum — there is nothing to salvage."}

    try:
        with open_zip_for_write(path) as out:
            for info, data in entries:
                zi = zipfile.ZipInfo(info.filename)
                zi.date_time = info.date_time if info.date_time[0] >= 1980 else (1980, 1, 1, 0, 0, 0)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = info.external_attr
                out.writestr(zi, data)
        with zipfile.ZipFile(path) as check:
            bad = check.testzip()
        if bad is not None:
            return {"ok": False, "salvaged": salvaged, "failed": [],
                    "message": f"The rewritten archive failed its own check on {bad}."}
    except Exception as e:
        app_logger.error(f"Salvage rebuild of {path} failed: {archive_error_detail(e)}")
        return {"ok": False, "salvaged": salvaged, "failed": [],
                "message": f"Could not write the rebuilt archive: {archive_error_detail(e)}"}

    what = _describe(salvaged)
    app_logger.info(f"Salvaged {path}: {what} had a CRC mismatch, decoded OK")
    return {"ok": True, "salvaged": salvaged, "failed": [],
            "message": f"Rebuilt — {what} had a CRC mismatch and decoded OK."}
