"""Where an archive member may be written on disk.

``zipfile.extract`` strips drive letters, leading separators and ``..`` from a
member name before it writes. Code that writes a member *by hand* gets none of
that: the CRC fallbacks in ``cbz_ops/edit.py`` and ``routes/metadata.py`` built
the destination as ``os.path.join(folder, <raw member name>)``, so an entry
called ``../../../config/x`` with a deliberately wrong CRC-32 was written
outside the extraction folder (GHSA-6p2x-pmgg-9qjj).

:func:`safe_member_path` is the one answer to "where does this member go".
**Never join a raw member name onto a directory** -- call this instead.
``tests/unit/test_archive_member_paths.py`` is the ratchet.

Import-light on purpose (stdlib only): ``cbz_ops`` modules import it, and they
must never pull in ``app``.
"""

import os

_WINDOWS_ILLEGAL = str.maketrans(':<>|"?*', "_" * 7)


class UnsafeArchiveMember(ValueError):
    """The member name has no usable destination inside the folder."""


def _sanitized_arcname(member_name):
    """``zipfile.ZipFile._extract_member``'s name handling, step for step.

    It has to be the same answer, not merely a safe one: ``extract()`` has
    usually started writing the entry at its own sanitised path before the CRC
    error is raised, and the fallback must land on that file -- the editor
    looks a damaged page up by the path it finds on disk.
    """
    arcname = member_name.replace("/", os.path.sep)
    if os.path.altsep:
        arcname = arcname.replace(os.path.altsep, os.path.sep)
    arcname = os.path.splitdrive(arcname)[1]
    invalid = ("", os.path.curdir, os.path.pardir)
    arcname = os.path.sep.join(p for p in arcname.split(os.path.sep) if p not in invalid)
    if os.path.sep == "\\":
        arcname = arcname.translate(_WINDOWS_ILLEGAL)
        parts = (p.rstrip(" .") for p in arcname.split(os.path.sep))
        arcname = os.path.sep.join(p for p in parts if p)
    return arcname


def safe_member_path(dest_dir, member_name):
    """The path ``zipfile.extract(member_name, dest_dir)`` writes to.

    Raises :class:`UnsafeArchiveMember` when the name sanitises to nothing, or
    when the result does not resolve strictly inside ``dest_dir`` (a symlink
    in the folder pointing out of it, say). Containment is realpath-compared
    on a separator boundary, so ``/x/a_folder2`` is not inside ``/x/a_folder``.
    """
    if not isinstance(member_name, str) or "\0" in member_name:
        raise UnsafeArchiveMember(f"Invalid archive member name: {member_name!r}")
    arcname = _sanitized_arcname(member_name)
    if not arcname:
        raise UnsafeArchiveMember(f"Archive member has no usable name: {member_name!r}")

    target = os.path.normpath(os.path.join(dest_dir, arcname))
    real_root = os.path.normcase(os.path.realpath(dest_dir))
    real_target = os.path.normcase(os.path.realpath(target))
    root = real_root.rstrip(os.sep) or real_root
    if not real_target.startswith(root + os.sep):
        raise UnsafeArchiveMember(
            f"Archive member {member_name!r} resolves outside the extraction folder"
        )
    return target
