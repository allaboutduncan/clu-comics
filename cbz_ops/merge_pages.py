"""
Merge pages from two copies of the same comic into one CBZ.

One copy (the *base*) usually has the better scan; the other (the *donor*)
has pages the base lacks — original ads, a letters page, a back cover. The
user picks pages from both, orders them, and the result replaces one of the
two files in place.

Nothing is extracted: pages are listed and thumbnailed straight from the
archives, and the output is streamed entry-by-entry from the source zips.
(Split and Edit extract to ``.tmp_*`` folders, which leak when their modal is
cancelled.)

The output is always flat — every page at the archive root — and renumbered
``"<stem> 001.jpg"``…, because two archives' original names cannot be relied on
to sort into the order the user chose.

This module must never import ``app`` (see CLAUDE.md, ``cbz_ops/rename.py``).
"""

import io
import os
import uuid
import zipfile

from core.app_logging import app_logger
from helpers import open_zip_for_write, match_parent_permissions
from helpers.trash import move_to_trash
from cbz_ops.split import IMAGE_EXTS, _natural_key

THUMB_SIZE = (300, 450)
LARGE_SIZE = (1200, 1800)


def _is_page(name):
    """True for an image entry that is a page of the comic."""
    if name.endswith('/'):
        return False
    if any(part == '__MACOSX' for part in name.split('/')):
        return False
    base = os.path.basename(name)
    if not base or base.startswith(('.', '_')):
        return False
    return base.lower().endswith(IMAGE_EXTS)


def _comicinfo_entry(names):
    """The archive's ComicInfo.xml entry name, or None (root copy preferred)."""
    found = [n for n in names if os.path.basename(n).lower() == 'comicinfo.xml']
    if not found:
        return None
    found.sort(key=lambda n: (n.count('/'), n))
    return found[0]


def _image_size(zf, name):
    """(width, height) from the image header only; None if unreadable."""
    from PIL import Image
    try:
        with zf.open(name) as fp, Image.open(fp) as img:
            return img.size
    except Exception:
        return None


def list_pages(path):
    """Describe one archive's pages, in reader order.

    Returns ``{path, name, file_size, page_count, has_comicinfo,
    median: [w, h] | None, pages: [{entry, basename, size, width, height}]}``.
    """
    with zipfile.ZipFile(path) as zf:
        infos = {i.filename: i for i in zf.infolist()}
        names = sorted((n for n in infos if _is_page(n)), key=_natural_key)
        pages = []
        for n in names:
            dims = _image_size(zf, n)
            pages.append({
                'entry': n,
                'basename': os.path.basename(n),
                'size': infos[n].file_size,
                'width': dims[0] if dims else None,
                'height': dims[1] if dims else None,
            })
        has_ci = _comicinfo_entry(infos.keys()) is not None

    sized = sorted((p['width'] * p['height'], p['width'], p['height'])
                   for p in pages if p['width'] and p['height'])
    median = list(sized[len(sized) // 2][1:]) if sized else None
    return {
        'path': path,
        'name': os.path.basename(path),
        'file_size': os.path.getsize(path),
        'page_count': len(pages),
        'has_comicinfo': has_ci,
        'median': median,
        'pages': pages,
    }


def render_page_thumb(path, entry, large=False):
    """JPEG bytes for one page, scaled down. Raises KeyError for an unknown entry."""
    from PIL import Image
    with zipfile.ZipFile(path) as zf:
        if entry not in zf.namelist() or not _is_page(entry):
            raise KeyError(entry)
        with zf.open(entry) as fp, Image.open(fp) as img:
            img.thumbnail(LARGE_SIZE if large else THUMB_SIZE, Image.LANCZOS)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            out = io.BytesIO()
            img.save(out, 'JPEG', quality=85 if large else 80)
            return out.getvalue()


def page_names(stem, exts):
    """Sequential flat names for ``exts`` (one lower-cased extension per page)."""
    width = max(3, len(str(len(exts))))
    return [f"{stem} {i:0{width}d}{ext}" for i, ext in enumerate(exts, start=1)]


def commit_merge(base_path, donor_path, pages, target_path):
    """Write the chosen pages, in order, over ``target_path``.

    ``pages`` is a list of ``{'src': 'base' | 'donor', 'entry': name}``.
    ``target_path`` must be one of the two sources. The original target goes
    to the trash; on any failure before that point the target is untouched.

    ComicInfo.xml comes from the base, else the donor, else none.

    Returns ``{'path', 'size', 'page_count', 'comicinfo': bool, 'trashed': bool}``.
    Raises ValueError for a bad request.
    """
    if os.path.normcase(os.path.abspath(base_path)) == os.path.normcase(os.path.abspath(donor_path)):
        raise ValueError("Base and donor must be different files")
    if target_path not in (base_path, donor_path):
        raise ValueError("Target must be the base or the donor")
    if not pages:
        raise ValueError("No pages selected")

    stem = os.path.splitext(os.path.basename(target_path))[0]
    target_dir = os.path.dirname(target_path)
    # Hidden, so the watcher and the indexer both ignore it while it exists.
    staging = os.path.join(target_dir, f".clu_merge_{uuid.uuid4().hex[:8]}.cbz")

    with zipfile.ZipFile(base_path) as zb, zipfile.ZipFile(donor_path) as zd:
        sources = {'base': zb, 'donor': zd}
        valid = {'base': set(zb.namelist()), 'donor': set(zd.namelist())}

        exts = []
        for p in pages:
            src, entry = p.get('src'), p.get('entry')
            if src not in sources or entry not in valid[src] or not _is_page(entry):
                raise ValueError(f"Unknown page: {entry}")
            exts.append(os.path.splitext(entry)[1].lower())
        names = page_names(stem, exts)

        ci = None
        for src in ('base', 'donor'):
            ci_entry = _comicinfo_entry(valid[src])
            if ci_entry:
                ci = sources[src].read(ci_entry)
                break

        try:
            with open_zip_for_write(staging, set_permissions=False) as out:
                for p, name in zip(pages, names):
                    with sources[p['src']].open(p['entry']) as fin, out.open(name, 'w') as fout:
                        while True:
                            chunk = fin.read(1024 * 1024)
                            if not chunk:
                                break
                            fout.write(chunk)
                if ci is not None:
                    out.writestr('ComicInfo.xml', ci)
        except Exception:
            if os.path.exists(staging):
                os.remove(staging)
            raise

    try:
        trash = move_to_trash(target_path)
    except Exception:
        os.remove(staging)
        raise
    os.replace(staging, target_path)
    match_parent_permissions(target_path)

    app_logger.info(
        f"Merged {len(pages)} page(s) from {os.path.basename(base_path)} + "
        f"{os.path.basename(donor_path)} into {target_path}"
    )
    return {
        'path': target_path,
        'size': os.path.getsize(target_path),
        'page_count': len(pages),
        'comicinfo': ci is not None,
        'trashed': bool(trash.get('trashed')),
    }
