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
    bulk_update_file_index_ci_field,
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
    bootstrap_theme = config.get("SETTINGS", "BOOTSTRAP_THEME", fallback="default")
    metron_available = bool(
        config.get("METRON", "USERNAME", fallback="")
        and config.get("METRON", "PASSWORD", fallback="")
    )
    return render_template(
        'source_wall.html',
        bootstrap_theme=bootstrap_theme,
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


@source_wall_bp.route('/api/source-wall/update-field', methods=['POST'])
def update_field():
    """Update a single ci_ field for one file, then sync to CBZ in background."""
    data = request.get_json(silent=True) or {}
    path = data.get('path', '')
    field = data.get('field', '')
    value = data.get('value', '')

    if not path or not is_valid_library_path(path):
        return jsonify({"success": False, "error": "Invalid path"}), 403

    if field not in CI_FIELD_TO_XML:
        return jsonify({"success": False, "error": f"Invalid field: {field}"}), 400

    ok = update_file_index_ci_field(path, field, value)
    if not ok:
        return jsonify({"success": False, "error": "Database update failed"}), 500

    # Sync to CBZ in background
    xml_tag = CI_FIELD_TO_XML[field]
    t = threading.Thread(
        target=_sync_field_to_cbz,
        args=(path, {xml_tag: value}),
        daemon=True,
    )
    t.start()

    return jsonify({"success": True})


@source_wall_bp.route('/api/source-wall/bulk-update', methods=['POST'])
def bulk_update():
    """Bulk update a ci_ field across multiple files with background CBZ sync."""
    data = request.get_json(silent=True) or {}
    paths = data.get('paths', [])
    field = data.get('field', '')
    value = data.get('value', '')

    if not paths:
        return jsonify({"success": False, "error": "No files specified"}), 400

    if field not in CI_FIELD_TO_XML:
        return jsonify({"success": False, "error": f"Invalid field: {field}"}), 400

    for p in paths:
        if not is_valid_library_path(p):
            return jsonify({"success": False, "error": f"Invalid path: {p}"}), 403

    affected = bulk_update_file_index_ci_field(paths, field, value)
    if affected < 0:
        return jsonify({"success": False, "error": "Database update failed"}), 500

    import core.app_state as app_state
    xml_tag = CI_FIELD_TO_XML[field]
    label = f"Updating {xml_tag} for {len(paths)} files"
    op_id = app_state.register_operation("source_wall", label, total=len(paths))

    t = threading.Thread(
        target=_bulk_sync_to_cbz,
        args=(paths, xml_tag, value, op_id),
        daemon=True,
    )
    t.start()

    return jsonify({"success": True, "op_id": op_id, "affected": affected})


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


def _sync_field_to_cbz(path, updates):
    """Sync field changes to the actual CBZ file."""
    try:
        import core.comicinfo as comicinfo
        comicinfo.update_comicinfo_in_zip(path, updates)
    except Exception as e:
        app_logger.error(f"Failed to sync field to CBZ {path}: {e}")


def _bulk_sync_to_cbz(paths, xml_tag, value, op_id):
    """Background worker: sync field change to multiple CBZ files."""
    import core.app_state as app_state
    import core.comicinfo as comicinfo

    for i, path in enumerate(paths):
        try:
            comicinfo.update_comicinfo_in_zip(path, {xml_tag: value})
        except Exception as e:
            app_logger.error(f"Failed to sync {xml_tag} to {path}: {e}")
        app_state.update_operation(
            op_id,
            current=i + 1,
            detail=f"Updated {i + 1}/{len(paths)}",
        )

    app_state.complete_operation(op_id)
