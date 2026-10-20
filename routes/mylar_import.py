"""Mylar3 importer: the ``/import/mylar`` page and its API.

Owner-only by path prefix (``/import`` and ``/api/import`` are in
``core.auth._OWNER_PREFIXES``): the import writes download-client passwords,
indexer keys and the ComicVine key, and reads them out of the uploaded files.
No response ever carries one of those secrets back; the preview reports only
whether each is set.

Heavy imports are done inside the views so the symbols stay patchable in tests.
"""

import os

from flask import Blueprint, current_app, jsonify, render_template, request

from core.app_logging import app_logger

mylar_import_bp = Blueprint("mylar_import", __name__)

MAX_DB_BYTES = 512 * 1024 * 1024
MAX_INI_BYTES = 1024 * 1024
_CHUNK = 1024 * 1024


class _TooLarge(Exception):
    pass


def _save_capped(storage, dest, limit):
    """Stream an upload to ``dest``, refusing it past ``limit`` bytes."""
    written = 0
    with open(dest, "wb") as out:
        while True:
            chunk = storage.stream.read(_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise _TooLarge()
            out.write(chunk)
    return written


@mylar_import_bp.route("/import/mylar")
def mylar_import_page():
    from models import comicvine_source, metron
    from models import mylar_import

    latest = mylar_import.latest_job()
    return render_template(
        "import_mylar.html",
        latest_job_id=latest["id"] if latest else "",
        metron_configured=metron.is_metron_configured(),
        comicvine_available=comicvine_source.is_available(),
        drop_folder=mylar_import.drop_folder_status(),
        # Opened from the Guided Setup's Mylar3 step: offer the way back.
        from_setup=request.args.get("from") == "setup",
    )


@mylar_import_bp.route("/api/import/mylar/drop-folder", methods=["GET"])
def mylar_drop_folder():
    from models import mylar_import

    return jsonify({"success": True, **mylar_import.drop_folder_status()})


@mylar_import_bp.route("/api/import/mylar/load", methods=["POST"])
def mylar_load():
    """Stage the files (upload or drop folder), parse them, return the preview."""
    from core.mylar_reader import MylarReadError
    from models import mylar_import

    from_drop = False
    if request.is_json:
        from_drop = (request.get_json(silent=True) or {}).get("source") == "drop-folder"
    else:
        from_drop = request.form.get("source") == "drop-folder"

    uploads = {
        mylar_import.DB_NAME: (request.files.get("mylar_db"), MAX_DB_BYTES),
        mylar_import.INI_NAME: (request.files.get("config_ini"), MAX_INI_BYTES),
    }
    if not from_drop and not any(f and f.filename for f, _ in uploads.values()):
        return jsonify({"success": False,
                        "error": "Choose mylar.db and/or config.ini to import"}), 400

    import_id, folder = mylar_import.new_import()
    try:
        if from_drop:
            if not mylar_import.stage_from_drop_folder(folder):
                mylar_import.discard_import(import_id)
                return jsonify({
                    "success": False,
                    "error": f"No mylar.db or config.ini in {mylar_import.drop_folder()}",
                }), 400
        else:
            for name, (storage, limit) in uploads.items():
                if storage and storage.filename:
                    _save_capped(storage, os.path.join(folder, name), limit)
        mylar_import.load_import(import_id)
        return jsonify({"success": True, **mylar_import.build_preview(import_id)})
    except _TooLarge:
        mylar_import.discard_import(import_id)
        return jsonify({"success": False, "error": "File is too large"}), 413
    except MylarReadError as e:
        mylar_import.discard_import(import_id)
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        mylar_import.discard_import(import_id)
        app_logger.error(f"Mylar3 import: loading files failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@mylar_import_bp.route("/api/import/mylar/preview", methods=["POST"])
def mylar_preview():
    from core.mylar_reader import MylarReadError
    from models import mylar_import

    data = request.get_json(silent=True) or {}
    import_id = data.get("import_id")
    mapping = data.get("mapping")
    if not import_id:
        return jsonify({"success": False, "error": "import_id is required"}), 400
    if mapping is not None and not isinstance(mapping, list):
        return jsonify({"success": False, "error": "mapping must be a list"}), 400
    try:
        return jsonify({"success": True, **mylar_import.build_preview(import_id, mapping)})
    except MylarReadError as e:
        return jsonify({"success": False, "error": str(e)}), 404


@mylar_import_bp.route("/api/import/mylar/apply", methods=["POST"])
def mylar_apply():
    from core.mylar_reader import MylarReadError
    from models import mylar_import

    data = request.get_json(silent=True) or {}
    import_id = data.get("import_id")
    if not import_id:
        return jsonify({"success": False, "error": "import_id is required"}), 400
    settings = data.get("settings") or {}
    selections = {
        "series": data.get("series") or [],
        "comicvine": bool(settings.get("comicvine")),
        "client": bool(settings.get("client")),
        "indexers": settings.get("indexers") or [],
    }
    try:
        op_id = mylar_import.start_apply_job(
            current_app._get_current_object(), import_id, selections
        )
    except MylarReadError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    except RuntimeError as e:
        return jsonify({"success": False, "error": str(e)}), 409
    return jsonify({"success": True, "op_id": op_id})


@mylar_import_bp.route("/api/import/mylar/status", methods=["GET"])
def mylar_status():
    from models import mylar_import

    op_id = request.args.get("op_id", "")
    if not op_id:
        # The page asks this on load to pick up an import already under way.
        return jsonify({"success": True, "job": mylar_import.latest_job()})
    job = mylar_import.get_job(op_id)
    if not job:
        return jsonify({"success": False, "error": "Unknown or expired import"}), 404
    return jsonify({"success": True, "job": job})
