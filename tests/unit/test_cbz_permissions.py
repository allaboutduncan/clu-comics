"""Regression tests: CBZ files CLU rewrites must stay as accessible as their
containing folder.

Before this fix, ``add_comicinfo_to_archive`` created its rewrite temp with
``tempfile.mkstemp`` and no ``dir=``, so the temp landed in the system temp dir
at the hardcoded ``0600`` mode; ``shutil.move`` into the library then copied
``0600`` onto the file. Combined with the container's root fallback this yielded
``root:-rw-------`` archives the app could no longer read, breaking thumbnails
and metadata for new issues. The rewrite paths now (a) keep the temp on the same
filesystem and (b) call ``match_parent_permissions`` on the result.
"""
import io
import os
import stat
import zipfile

import pytest

from PIL import Image

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='POSIX chmod/group semantics')


def _make_cbz(path):
    """Write a minimal but real CBZ (one JPEG page) to ``path``."""
    buf = io.BytesIO()
    Image.new('RGB', (4, 4), (200, 10, 10)).save(buf, format='JPEG')
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('001.jpg', buf.getvalue())


def test_add_comicinfo_leaves_world_readable_file(tmp_path):
    from models.comicvine import add_comicinfo_to_archive

    folder = tmp_path
    os.chmod(folder, 0o777)
    cbz = folder / 'Book - 001.cbz'
    _make_cbz(str(cbz))
    os.chmod(cbz, 0o600)  # simulate a prior 0600-leaked archive

    ok = add_comicinfo_to_archive(str(cbz), '<?xml version="1.0"?><ComicInfo/>')
    assert ok is True

    # ComicInfo.xml was embedded ...
    with zipfile.ZipFile(str(cbz)) as zf:
        names = {n.lower() for n in zf.namelist()}
    assert 'comicinfo.xml' in names

    # ... and the archive is now as accessible as its 0777 folder (0666),
    # not the 0600 it started with.
    mode = stat.S_IMODE(os.stat(cbz).st_mode)
    assert mode == 0o666
