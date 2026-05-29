"""
Source Wall Blueprint

Provides a table-based metadata editor for browsing and editing
ComicInfo.xml fields across the library.
"""

import threading
from flask import Blueprint, request, jsonify, render_template
from core.app_logging import app_logger
from core.config import config
from helpers.library import is_valid_library_path
from core.database import (
    get_source_wall_files,
    update_file_index_ci_field,
    get_user_preference,
    set_user_preference,
    get_distinct_ci_values,
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


@source_wall_bp.route('/source-wall')
def source_wall_page():
    """Render the Source Wall metadata table page."""
    metron_available = bool(
        config.get("METRON", "USERNAME", fallback="")
        and config.get("METRON", "PASSWORD", fallback="")
    )
    return render_template(
        'source_wall.html',
        metron_available=metron_available,
    )


@source_wall_bp.route('/api/source-wall/files')
def get_files():
    """Return directories and files with ci_ metadata for a path."""
    path = request.args.get('path', '')
    if not path or not is_valid_library_path(path):
        return jsonify({"success": False, "error": "Invalid path"}), 403

    try:
        directories, files = get_source_wall_files(path)
        return jsonify({
            "success": True,
            "directories": directories,
            "files": files,
        })
    except Exception as e:
        app_logger.error(f"Source wall files error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


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


@source_wall_bp.route('/api/source-wall/columns')
def get_columns():
    """Load saved column preferences."""
    columns = get_user_preference('source_wall_columns', ['name', 'ci_volume'])
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

    values = get_distinct_ci_values(field, query, parent_path=path or None, limit=20)
    return jsonify({"success": True, "values": values})


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
        """Re-read the archive and flip file_index.has_comicinfo to match.

        update_comicinfo_in_zip only mutates an existing ComicInfo.xml; if the
        file had none to begin with, the rewrite is a no-op and has_comicinfo
        should stay at 0. Checking disk after the fact is the only reliable
        signal.
        """
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
