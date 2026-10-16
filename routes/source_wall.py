"""
Source Wall Blueprint

Provides a table-based metadata editor for browsing and editing
ComicInfo.xml fields across the library.
"""

import threading
from flask import Blueprint, request, jsonify, render_template
from core.app_logging import app_logger
import os
from core.auth import (
    enforce_path_access, filter_paths_for_user, current_user,
    accessible_folder_prefixes,
)
from helpers.library import is_valid_library_path
from core.database import (
    get_source_wall_files,
    update_file_index_ci_field,
    get_user_preference,
    set_user_preference,
    get_distinct_ci_values,
    get_file_index_ci_for_paths,
    get_provider_mix,
    get_provider_state_for_paths,
    get_source_wall_files_by_provider,
    set_ci_provider,
)

source_wall_bp = Blueprint('source_wall', __name__)

CI_FIELD_TO_XML = {
    'ci_title': 'Title',
    'ci_series': 'Series',
    'ci_number': 'Number',
    'ci_count': 'Count',
    'ci_volume': 'Volume',
    'ci_year': 'Year',
    'ci_writer': 'Writer',
    'ci_penciller': 'Penciller',
    'ci_inker': 'Inker',
    'ci_colorist': 'Colorist',
    'ci_letterer': 'Letterer',
    'ci_coverartist': 'CoverArtist',
    'ci_publisher': 'Publisher',
    'ci_genre': 'Genre',
    'ci_characters': 'Characters',
}

XML_TO_CI_FIELD = {v: k for k, v in CI_FIELD_TO_XML.items()}

# Shown until the user saves their own selection. ci_provider is read-only
# (it is derived from Notes, never typed), so it is a display column only and
# stays out of CI_FIELD_TO_XML, which gates every edit.
DEFAULT_COLUMNS = ['name', 'ci_provider', 'ci_volume']
PAGE_SIZE = 500


def _is_within(path, root):
    """True when ``path`` is ``root`` or lies under it (normalised, case kept)."""
    path, root = os.path.normpath(path), os.path.normpath(root)
    return path == root or path.startswith(root + os.sep)


def _provider_scopes(library_path, folder=None):
    """The folders the current user may list for the provider views.

    Unrestricted users get the library root; a folder-scoped user gets only
    their granted subtrees inside it, so neither the files nor the per-provider
    counts reveal anything outside a grant.

    ``folder`` narrows that to one subtree (the folder being browsed). It is
    intersected with the grants rather than trusted: a grant inside the folder
    stays as it is, a folder inside a grant becomes the scope, and anything else
    drops out.
    """
    prefixes = accessible_folder_prefixes(current_user())
    if prefixes is None:
        allowed = [library_path]
    else:
        allowed = [p for p in prefixes if _is_within(p, library_path)]
    if not folder:
        return allowed
    scopes = []
    for a in allowed:
        if _is_within(folder, a):
            scopes.append(folder)
        elif _is_within(a, folder):
            scopes.append(a)
    return list(dict.fromkeys(scopes))


def _checked_folder(library_path):
    """The optional ``folder`` query arg, validated, as ``(folder, error)``.

    It must be a library path inside ``library_path`` that the user may browse;
    otherwise the error is a ready-to-return response.
    """
    folder = request.args.get('folder', '').strip()
    if not folder or os.path.normpath(folder) == os.path.normpath(library_path):
        return None, None
    if not is_valid_library_path(folder) or not _is_within(folder, library_path):
        return None, (jsonify({"success": False, "error": "Invalid folder"}), 403)
    denied = enforce_path_access(folder, mode='browse')
    if denied:
        return None, denied
    return folder, None


@source_wall_bp.route('/source-wall')
def source_wall_page():
    """Render the Source Wall metadata table page."""
    # metron_available comes from the app-wide context processor, which reads
    # the credentials out of the database. Passing a locally computed value
    # here shadows it, and the config.ini fallback it used is empty on every
    # install that stores its Metron credentials (or an API token) in the DB —
    # which hid the Pull List menu on this page alone.
    return render_template('source_wall.html')


@source_wall_bp.route('/api/source-wall/files')
def get_files():
    """Return directories and files with ci_ metadata for a path."""
    path = request.args.get('path', '')
    if not path or not is_valid_library_path(path):
        return jsonify({"success": False, "error": "Invalid path"}), 403

    denied = enforce_path_access(path, mode='browse')
    if denied:
        return denied

    try:
        directories, files = get_source_wall_files(path)
        # Folder-scope: keep navigable ancestor dirs, hide out-of-grant entries.
        user = current_user()
        directories = filter_paths_for_user(
            user, directories, key='path', allow_traverse=True)
        files = filter_paths_for_user(user, files, key='path')
        return jsonify({
            "success": True,
            "directories": directories,
            "files": files,
        })
    except Exception as e:
        app_logger.error(f"Source wall files error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@source_wall_bp.route('/api/source-wall/providers')
def provider_counts():
    """Which providers tagged the comics in a library, with counts.

    Query: path (a library root), optional folder (narrow to that subtree).
    Feeds the "By provider" view's picker.
    """
    path = request.args.get('path', '')
    if not path or not is_valid_library_path(path):
        return jsonify({"success": False, "error": "Invalid path"}), 403
    denied = enforce_path_access(path, mode='browse')
    if denied:
        return denied

    folder, err = _checked_folder(path)
    if err:
        return err

    total = {"providers": {}, "untagged": 0, "no_xml": 0, "unscanned": 0, "total": 0}
    for scope in _provider_scopes(path, folder):
        mix = get_provider_mix(scope, recursive=True)
        if mix is None:
            return jsonify({"success": False, "error": "Could not read the file index"}), 500
        for name, n in mix["providers"].items():
            total["providers"][name] = total["providers"].get(name, 0) + n
        for key in ("untagged", "no_xml", "unscanned", "total"):
            total[key] += mix[key]
    return jsonify({"success": True, "mix": total})


@source_wall_bp.route('/api/source-wall/by-provider')
def files_by_provider():
    """Every comic in a library tagged by one provider, as flat paged rows.

    Query: path (library root), provider (id, ``manual`` or ``untagged``),
    offset, and optionally folder (a subfolder to narrow to; omitted = the whole
    library). Rows carry ``parent`` so the page can show where each file lives.
    """
    path = request.args.get('path', '')
    provider = request.args.get('provider', '').strip()
    offset = max(request.args.get('offset', 0, type=int) or 0, 0)
    if not path or not is_valid_library_path(path):
        return jsonify({"success": False, "error": "Invalid path"}), 403
    denied = enforce_path_access(path, mode='browse')
    if denied:
        return denied
    if not provider:
        return jsonify({"success": False, "error": "provider is required"}), 400
    folder, err = _checked_folder(path)
    if err:
        return err

    files, total = get_source_wall_files_by_provider(
        _provider_scopes(path, folder), provider, limit=PAGE_SIZE, offset=offset)
    if files is None:
        return jsonify({"success": False, "error": "Could not read the file index"}), 500
    # Belt and braces: the SQL is already scoped, but a grant is the contract.
    files = filter_paths_for_user(current_user(), files, key='path')
    return jsonify({
        "success": True,
        "files": files,
        "total": total,
        "offset": offset,
        "page_size": PAGE_SIZE,
    })


@source_wall_bp.route('/api/source-wall/set-provider', methods=['POST'])
def set_provider():
    """Say which provider tagged files whose provider is Unknown.

    Body: {"paths": ["<path>", ...], "provider": "<id>" | "manual"}

    Only files that have a ComicInfo.xml but no recognisable provider are
    touched; the rest are reported as ``skipped``. A provider that IS known is
    not this route's to overwrite -- that is what a re-tag is for. The value is
    recorded by rewriting ``Notes`` (see ``core.comicinfo.notes_for_provider``),
    since the scanner re-derives ``ci_provider`` from it and would undo anything
    held only in the index.
    """
    from core.comicinfo import SETTABLE_PROVIDERS, notes_for_provider, read_comicinfo_from_zip

    data = request.get_json(silent=True) or {}
    paths = data.get('paths') or []
    provider = data.get('provider')

    if provider not in SETTABLE_PROVIDERS:
        return jsonify({"success": False, "error": f"Invalid provider: {provider}"}), 400
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        return jsonify({"success": False, "error": "No paths provided"}), 400
    for path in paths:
        if not is_valid_library_path(path):
            return jsonify({"success": False, "error": f"Invalid path: {path}"}), 403
        denied = enforce_path_access(path)
        if denied:
            return denied

    unique = list(dict.fromkeys(paths))
    state = get_provider_state_for_paths(unique)
    if state is None:
        return jsonify({"success": False, "error": "Could not read the file index"}), 500

    items = []
    for path in unique:
        has_ci, current = state.get(path, (0, None))
        if not has_ci or current != '':
            continue
        try:
            existing = read_comicinfo_from_zip(path).get('Notes')
        except Exception as e:
            app_logger.warning(f"set-provider: cannot read {path}: {e}")
            continue
        items.append((path, {'Notes': notes_for_provider(provider, existing)}))

    skipped = len(unique) - len(items)
    if not items:
        return jsonify({
            "success": False,
            "error": "None of the selected files has an unknown provider",
            "skipped": skipped,
        }), 400

    import core.app_state as app_state
    op_id = app_state.register_operation(
        "source_wall", f"Setting provider on {len(items)} files", total=len(items))
    threading.Thread(
        target=_bulk_set_provider, args=(items, provider, op_id), daemon=True).start()

    return jsonify({
        "success": True,
        "op_id": op_id,
        "affected": len(items),
        "skipped": skipped,
    })


@source_wall_bp.route('/api/source-wall/save-pending', methods=['POST'])
def save_pending():
    """Commit a stash of staged edits.

    Body: {"updates": {"<path>": {"<ci_field>": "<value>", ...}, ...}}

    Each path appears exactly once with all its accumulated field changes,
    so the parallel CBZ-write contract (distinct paths) holds, and each
    file receives a single rebuild rather than one per field.
    """
    data = request.get_json(silent=True) or {}
    updates = data.get('updates') or {}

    if not isinstance(updates, dict) or not updates:
        return jsonify({"success": False, "error": "No updates provided"}), 400

    for path, fields in updates.items():
        if not is_valid_library_path(path):
            return jsonify({"success": False, "error": f"Invalid path: {path}"}), 403
        denied = enforce_path_access(path)
        if denied:
            return denied
        if not isinstance(fields, dict) or not fields:
            return jsonify({"success": False, "error": f"No fields for path: {path}"}), 400
        for field in fields.keys():
            if field not in CI_FIELD_TO_XML:
                return jsonify({"success": False, "error": f"Invalid field: {field}"}), 400

    edit_count = 0
    for path, fields in updates.items():
        for field, value in fields.items():
            ok = update_file_index_ci_field(path, field, value)
            if not ok:
                return jsonify({"success": False, "error": "Database update failed"}), 500
            edit_count += 1

    items = [
        (path, {CI_FIELD_TO_XML[field]: value for field, value in fields.items()})
        for path, fields in updates.items()
    ]

    import core.app_state as app_state
    label = f"Saving updates to {len(items)} files"
    op_id = app_state.register_operation("source_wall", label, total=len(items))

    t = threading.Thread(
        target=_bulk_sync_pending_to_cbz,
        args=(items, op_id),
        daemon=True,
    )
    t.start()

    return jsonify({
        "success": True,
        "op_id": op_id,
        "affected": len(items),
        "edits": edit_count,
    })


@source_wall_bp.route('/api/source-wall/reconcile-from-db', methods=['POST'])
def reconcile_from_db():
    """Rewrite ComicInfo.xml on disk from current file_index ci_ values.

    Body: {"paths": ["<path>", ...]}

    For each path, reads the committed ci_ fields from file_index and rebuilds
    the CBZ's ComicInfo.xml to match. Used to fix drift where the DB has
    metadata but the on-disk XML is missing/stale (e.g., a previous save's
    background write failed, or the archive was edited externally).

    Treats the DB as the source of truth — any non-empty ci_ field is written;
    files with all-empty ci_ values are skipped (nothing to write).

    Note: credit/character values in file_index are provider-ID-normalized
    (see core/metadata_normalize.py), so reconciling writes "Ron Lim" where
    the on-disk XML may have said "Ron Lim [3258]", and re-spaces lists as
    "A, B". That is intended — the provider ID is not part of the creator's
    name — but it does mean this route is the one path that propagates the
    normalization back to disk.
    """
    data = request.get_json(silent=True) or {}
    paths = data.get('paths') or []

    if not isinstance(paths, list) or not paths:
        return jsonify({"success": False, "error": "No paths provided"}), 400

    for path in paths:
        if not isinstance(path, str) or not is_valid_library_path(path):
            return jsonify({"success": False, "error": f"Invalid path: {path}"}), 403
        denied = enforce_path_access(path)
        if denied:
            return denied

    # Deduplicate to preserve the distinct-path contract for bulk writes.
    unique_paths = list(dict.fromkeys(paths))

    rows = get_file_index_ci_for_paths(unique_paths)

    items = []
    for path in unique_paths:
        ci_fields = rows.get(path)
        if not ci_fields:
            continue
        xml_fields = {
            CI_FIELD_TO_XML[field]: value
            for field, value in ci_fields.items()
            if field in CI_FIELD_TO_XML and value
        }
        if xml_fields:
            items.append((path, xml_fields))

    skipped = len(unique_paths) - len(items)

    if not items:
        return jsonify({
            "success": False,
            "error": "No files have ci_ data to write",
            "skipped": skipped,
        }), 400

    import core.app_state as app_state
    label = f"Writing XML for {len(items)} files"
    op_id = app_state.register_operation("source_wall", label, total=len(items))

    t = threading.Thread(
        target=_bulk_sync_pending_to_cbz,
        args=(items, op_id),
        daemon=True,
    )
    t.start()

    return jsonify({
        "success": True,
        "op_id": op_id,
        "affected": len(items),
        "skipped": skipped,
    })


@source_wall_bp.route('/api/source-wall/columns')
def get_columns():
    """Load saved column preferences."""
    columns = get_user_preference('source_wall_columns', DEFAULT_COLUMNS)
    return jsonify({"success": True, "columns": columns})


@source_wall_bp.route('/api/source-wall/columns', methods=['POST'])
def save_columns():
    """Save column preferences."""
    data = request.get_json(silent=True) or {}
    columns = data.get('columns', [])
    if not isinstance(columns, list):
        return jsonify({"success": False, "error": "columns must be a list"}), 400

    ok = set_user_preference('source_wall_columns', columns, category='source_wall')
    if ok:
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Failed to save"}), 500


@source_wall_bp.route('/api/source-wall/suggest')
def suggest_values():
    """Return distinct values for a ci_ field matching a query prefix."""
    field = request.args.get('field', '')
    query = request.args.get('q', '')
    path = request.args.get('path', '')

    if field not in CI_FIELD_TO_XML:
        return jsonify({"success": False, "error": f"Invalid field: {field}"}), 400

    if len(query) < 3:
        return jsonify({"success": True, "values": []})

    if path:
        denied = enforce_path_access(path, mode='browse')
        if denied:
            return denied

    values = get_distinct_ci_values(field, query, parent_path=path or None, limit=20)
    return jsonify({"success": True, "values": values})


def _bulk_set_provider(items, provider, op_id):
    """Background worker: rewrite Notes, then record the provider in the index.

    The index is only touched for files whose write succeeded, so a failure
    leaves the row Unknown, which is what the file still is.
    """
    import core.app_state as app_state
    import core.comicinfo as comicinfo

    failures = []

    def on_progress(completed, total, path, error):
        if error is None:
            set_ci_provider(path, provider)
        else:
            failures.append(path)
        app_state.update_operation(
            op_id, current=completed, detail=f"Updated {completed}/{total}")

    try:
        comicinfo.bulk_update_comicinfo_in_zips(items, progress_callback=on_progress)
    finally:
        app_state.complete_operation(op_id, error=bool(failures))


def _bulk_sync_pending_to_cbz(items, op_id):
    """Background worker: rebuild each CBZ once with all its staged field changes.

    `items` is a list of (path, {xml_tag: value, ...}) tuples where each path
    appears exactly once, satisfying the distinct-path contract of
    bulk_update_comicinfo_in_zips.
    """
    import zipfile
    import core.app_state as app_state
    import core.comicinfo as comicinfo
    from core.database import set_has_comicinfo

    def _resync_has_comicinfo(path):
        """Re-read the archive and flip file_index.has_comicinfo to match on-disk state."""
        try:
            with zipfile.ZipFile(path, 'r') as z:
                present = 1 if comicinfo.find_comicinfo_in_zip(z) else 0
            set_has_comicinfo(path, present)
        except Exception as e:
            app_logger.warning(f"has_comicinfo resync failed for {path}: {e}")

    def on_progress(completed, total, path, error):
        if error is None:
            _resync_has_comicinfo(path)
        app_state.update_operation(
            op_id,
            current=completed,
            detail=f"Updated {completed}/{total}",
        )

    comicinfo.bulk_update_comicinfo_in_zips(items, progress_callback=on_progress)
    app_state.complete_operation(op_id)
