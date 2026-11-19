"""A rename that refuses an existing destination instead of replacing it.

``os.rename`` and ``os.replace`` silently replace an existing destination on
POSIX. Checking ``os.path.exists`` first only narrows the window: anything that
creates the name between the check and the rename -- a download being filed,
another rename, a second tab, a tool outside CLU -- is destroyed, with nothing
in the trash (#723, #731). So the rename itself refuses wherever the platform
lets it:

  1. Windows: ``os.rename`` already refuses (``FileExistsError``).
  2. Linux: ``renameat2(RENAME_NOREPLACE)`` -- atomic, no hard link needed.
  3. Elsewhere, or where the filesystem rejects that flag: ``os.link`` +
     ``os.unlink``. ``link`` fails with EEXIST rather than replacing.
  4. A filesystem with neither (CIFS, some FUSE, mergerfs across branches):
     check, then ``os.rename``. Still a race, but a window of microseconds.

Import-light on purpose (standard library only): ``helpers/__init__`` and
``cbz_ops.rename`` both use it, and the latter is loaded by ``monitor.py`` in a
process where app.py must never be imported.
"""

import errno
import os

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_UNSUPPORTED_ERRNOS = {
    getattr(errno, name)
    for name in ("EINVAL", "ENOSYS", "EOPNOTSUPP", "ENOTSUP")
    if hasattr(errno, name)
}
_renameat2_fn = None  # None = not looked up yet, False = unavailable


def _same_file(a: str, b: str) -> bool:
    """True if both paths resolve to the same file. On a case-insensitive
    filesystem this is a case-only rename ("Avx" -> "AVX"), not a collision."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _load_renameat2():
    """glibc's ``renameat2`` via ctypes, or None where there is none."""
    global _renameat2_fn
    if _renameat2_fn is None:
        _renameat2_fn = False
        if os.name == "posix":
            try:
                import ctypes

                libc = ctypes.CDLL(None, use_errno=True)
                fn = libc.renameat2
                fn.argtypes = [
                    ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint
                ]
                fn.restype = ctypes.c_int
                _renameat2_fn = fn
            except (OSError, AttributeError):
                pass
    return _renameat2_fn or None


def _renameat2_noreplace(src: str, dst: str) -> bool:
    """Rename with RENAME_NOREPLACE. True if renamed, False if this platform or
    filesystem does not offer it. Raises ``FileExistsError`` if *dst* exists."""
    fn = _load_renameat2()
    if fn is None:
        return False
    import ctypes

    ctypes.set_errno(0)
    if fn(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE) == 0:
        return True
    err = ctypes.get_errno()
    if err in _UNSUPPORTED_ERRNOS:
        return False
    # OSError() picks the subclass from the errno: EEXIST -> FileExistsError.
    raise OSError(err, os.strerror(err), src, None, dst)


def _destination_exists(dst: str) -> FileExistsError:
    return FileExistsError(errno.EEXIST, "A file already exists at the destination", dst)


def _link_then_unlink(src: str, dst: str) -> bool:
    """Rename via a hard link. True if renamed, False if the filesystem cannot
    hard-link. Raises ``FileExistsError`` if *dst* exists."""
    try:
        os.link(src, dst, follow_symlinks=False)
    except FileExistsError:
        raise
    except (OSError, NotImplementedError):
        return False
    try:
        os.unlink(src)
    except OSError:
        # Do not leave the comic under two names; *dst* is the link we just made.
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise
    return True


def rename_no_clobber(src: str, dst: str) -> None:
    """Rename *src* to *dst*, raising ``FileExistsError`` instead of replacing
    a different file already at *dst*.

    A case-only rename (``Foo.cbz`` -> ``foo.cbz``) on a case-insensitive mount
    finds *dst* "existing" because it is *src* itself; that is not a collision.

    A cross-device rename raises ``OSError`` (EXDEV) like ``os.rename``; use
    ``helpers.move_file(src, dst, replace=False)`` when the two may be on
    different mounts.
    """
    if os.path.lexists(dst):
        if _same_file(src, dst):
            os.rename(src, dst)
            return
        raise _destination_exists(dst)
    if os.name == "nt":
        os.rename(src, dst)  # refuses an existing destination on Windows
        return
    if _renameat2_noreplace(src, dst):
        return
    if _link_then_unlink(src, dst):
        return
    if os.path.lexists(dst):
        raise _destination_exists(dst)
    os.rename(src, dst)
