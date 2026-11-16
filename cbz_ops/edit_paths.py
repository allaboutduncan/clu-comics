"""Containment for the CBZ editor's paths.

The editor works in two requests. ``/edit`` renames ``X.cbz`` to ``X.zip`` and
extracts it into ``X_folder`` (or a single nested directory inside it);
``/save`` then rezips that folder back to ``X.cbz``, deletes ``X.zip`` and
removes the extraction folder. All three paths come back to ``/save`` from the
browser, so none of them can be trusted: this module decides whether they are
the paths the editor itself would have produced, and whether the current user
may act on them, *before* anything is renamed, written or removed.

Kept free of any ``app`` import (``cbz_ops`` modules must be importable on
their own, see CLAUDE.md), so it is tested directly.
"""

import os

from core.auth import file_op_refusal
from core.config import get_target_dir, get_watch_dir
from helpers.library import get_library_roots, get_protected_roots, is_critical_path

EDITABLE_EXTENSIONS = (".cbz", ".zip")
EXTRACT_SUFFIX = "_folder"


def _real(path):
    return os.path.normcase(os.path.realpath(path))


def _strictly_inside(real_path, real_root):
    """Separator-bounded containment: ``/data/X`` is not inside ``/data/X-Men``."""
    root = real_root.rstrip(os.sep) or real_root
    return real_path != root and real_path.startswith(root + os.sep)


def _inside_or_equal(real_path, real_root):
    return real_path == real_root or _strictly_inside(real_path, real_root)


def editor_roots():
    """The configured locations the editor may work in, as ``(configured, real)``.

    Every enabled library root, plus WATCH and TARGET: the File Manager browses
    the staging folders as well as the libraries, and its Edit action opens
    comics in both. Nothing else -- not the system temp dir that
    ``is_allowed_path`` also admits, and never ``/config`` or ``/cache``.
    """
    roots = []
    candidates = []
    try:
        candidates.extend(get_library_roots())
    except Exception:
        pass
    candidates.extend(d for d in (get_watch_dir(), get_target_dir()) if d)
    for root in candidates:
        try:
            roots.append((os.path.normpath(root), _real(root)))
        except Exception:
            continue
    return roots


def _canonical_for_grants(path, roots):
    """``path`` re-expressed under the configured root that really contains it.

    The folder-grant check compares against the configured root strings, so a
    symlink inside a granted folder that points into an ungranted one would
    pass it by its apparent name. Rebuilding the path from its realpath under
    the configured root makes the grant check judge where the bytes really are.
    Longest root first, so a nested root (WATCH inside TARGET) wins.
    """
    raw_real = os.path.realpath(path)
    real = os.path.normcase(raw_real)
    for configured, real_root in sorted(roots, key=lambda r: len(r[1]), reverse=True):
        if _strictly_inside(real, real_root):
            # relpath on the un-normcased realpaths, so a case-insensitive
            # filesystem keeps the name's real spelling for the grant lookup.
            return os.path.join(
                configured, os.path.relpath(raw_real, os.path.realpath(configured))
            )
    return None


def editor_archive_refusal(path):
    """``(message, status)`` if the editor must not open or write ``path``, else None.

    ``path`` is a comic archive the editor would act on (the ``file_path`` of
    ``/edit``, the ``original_file_path``/``zip_file_path`` of ``/save``). It
    must be an absolute ``.cbz``/``.zip`` path whose realpath lies strictly
    inside a library root, WATCH or TARGET, and the current user must hold a
    folder grant for it (``file_op_refusal``, which is a no-op for owners and
    single-user installs). Checked on both the path as given and the path it
    really resolves to.
    """
    if not path or not isinstance(path, str) or "\0" in path:
        return "Invalid file path", 400
    if not os.path.isabs(path):
        return "File path must be absolute", 400
    if not path.lower().endswith(EDITABLE_EXTENSIONS):
        return "Only .cbz or .zip files can be edited", 400

    roots = editor_roots()
    real = _real(path)
    if not any(_strictly_inside(real, real_root) for _c, real_root in roots):
        return "Access denied - path is outside the library", 403

    canonical = _canonical_for_grants(path, roots)
    refusal = file_op_refusal(path, canonical) if canonical else file_op_refusal(path)
    if refusal:
        return refusal
    return None


def editor_extract_root(archive_path):
    """The folder ``process_cbz_file`` extracts ``archive_path`` into."""
    return os.path.splitext(archive_path)[0] + EXTRACT_SUFFIX


def editor_zip_path(archive_path):
    """The ``.zip`` name ``process_cbz_file`` renames ``archive_path`` to."""
    return os.path.splitext(archive_path)[0] + ".zip"


def _extract_root_refusal(extract_root):
    """Refuse an extraction folder that is, holds, or sits on a protected root."""
    if is_critical_path(extract_root):
        return "Access denied - refusing to remove a protected folder", 403
    real_root = _real(extract_root)
    try:
        protected = {os.path.normcase(p) for p in get_protected_roots()}
    except Exception:
        return "Could not resolve protected folders", 503
    for real_protected in protected:
        # A protected root at or under the folder would be deleted with it.
        if _inside_or_equal(real_protected, real_root):
            return "Access denied - refusing to remove a protected folder", 403
    return None


def validate_save_paths(folder_name, zip_file_path, original_file_path):
    """``(message, status)`` if ``/save`` must refuse these paths, else None.

    Nothing is trusted from the form beyond the archive's own name:

    - ``original_file_path`` and ``zip_file_path`` must each pass
      :func:`editor_archive_refusal`;
    - ``zip_file_path`` must be exactly the ``.zip`` the ``/edit`` step renames
      that archive to, and must exist;
    - the extraction folder is derived from the archive
      (``<stem>_folder``, a sibling of it), must exist, and must not be or
      contain a protected root;
    - ``folder_name`` must be that folder or a directory inside it (``/edit``
      descends into a single nested directory) -- realpath-compared on a
      separator boundary.
    """
    if not folder_name or not zip_file_path or not original_file_path:
        return "Missing required data", 400
    if not isinstance(folder_name, str) or "\0" in folder_name or not os.path.isabs(folder_name):
        return "Invalid extraction folder", 400

    for path in (original_file_path, zip_file_path):
        refusal = editor_archive_refusal(path)
        if refusal:
            return refusal

    expected_zip = editor_zip_path(original_file_path)
    if os.path.normcase(os.path.normpath(zip_file_path)) != os.path.normcase(os.path.normpath(expected_zip)):
        return "Archive paths do not belong to the same edit", 400
    if not os.path.isfile(zip_file_path):
        return "The archive being edited no longer exists", 404

    extract_root = editor_extract_root(original_file_path)
    if not os.path.isdir(extract_root) or os.path.islink(extract_root):
        return "The extraction folder for this edit does not exist", 404
    refusal = _extract_root_refusal(extract_root)
    if refusal:
        return refusal

    real_folder = _real(folder_name)
    if not _inside_or_equal(real_folder, _real(extract_root)):
        return "Extraction folder does not belong to this archive", 403
    if not os.path.isdir(folder_name):
        return "The extraction folder for this edit does not exist", 404

    refusal = file_op_refusal(folder_name, extract_root)
    if refusal:
        return refusal
    return None
