"""The Database tab's API (``/api/database/*``).

A blueprint rather than routes in ``app.py``, because ``app.py`` cannot be
imported in tests: ``tests/routes/conftest.py`` used to re-declare every one of
these by hand as a stub, with nothing keeping the two copies honest. Adding the
endpoints below would have meant writing each of them twice.

**Owner-only, with no decorators here.** ``core/auth.py`` gates the
``/api/database`` prefix in ``_OWNER_PREFIXES``, so every route in this file --
including any added later -- inherits that.

Heavy imports are done *inside* the view functions, matching
``routes/problem_files.py``. That is not style: it keeps ``core.database``
symbols patchable, and the existing suite (``TestBackupReusesAKnownIntegrityResult``
and friends) patches them by name.

Anything that can outlive gunicorn's ``--timeout 120`` -- a full
``integrity_check``, a compact, a salvage -- runs on a thread and reports
through the operations registry. Pages following one of those poll
``/api/operation/<op_id>``, never ``/api/operations``: the latter *clears* the
pending notification queue as a side effect, so it can only have the one poller
in ``base.html``.
"""
import threading

from flask import Blueprint, jsonify, request, send_file

from core.app_logging import app_logger
import core.app_state as app_state

database_bp = Blueprint("database", __name__)


def _run_in_background(op_type, label, worker):
    """Register an operation and run ``worker(progress)`` on a daemon thread.

    ``worker`` returns a result dict, which is stashed on the operation so the
    poller can render it.
    """
    op_id = app_state.register_operation(op_type, label, total=0)

    def _progress(detail):
        app_state.update_operation(op_id, detail=detail)

    def _runner():
        try:
            result = worker(_progress)
            _stash_result(op_id, result)
            app_state.complete_operation(
                op_id, error=not (result or {}).get("success", True)
            )
        except Exception as e:
            app_logger.error(f"{op_type} failed: {e}", exc_info=True)
            _stash_result(op_id, {"success": False, "error": str(e)})
            app_state.complete_operation(op_id, error=True)

    threading.Thread(target=_runner, name=op_type, daemon=True).start()
    return op_id


# Results outlive the operations registry, which prunes completed entries on a
# TTL -- the user would otherwise lose a compact's before/after numbers while
# still reading them.
_op_results = {}
_op_results_lock = threading.Lock()


def _stash_result(op_id, result):
    with _op_results_lock:
        _op_results[op_id] = result
        if len(_op_results) > 20:
            for stale in list(_op_results)[:-20]:
                del _op_results[stale]


def get_op_result(op_id):
    with _op_results_lock:
        return _op_results.get(op_id)


# ---------------------------------------------------------------------------
# Stats, health and backups
# ---------------------------------------------------------------------------


@database_bp.route("/api/database/stats", methods=["GET"])
def api_database_stats():
    """Database file sizes, per-table row counts, and last-backup info."""
    try:
        from core.database import (
            get_database_stats, list_backups, list_quarantine_snapshots,
        )

        stats = get_database_stats()
        backups = list_backups()
        last_backup = backups[0] if backups else None
        return jsonify({
            "success": True,
            "stats": stats,
            "last_backup": last_backup,
            "backup_count": len(backups),
            "quarantine": list_quarantine_snapshots(),
        })
    except Exception as e:
        app_logger.error(f"api_database_stats failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@database_bp.route("/api/database/health", methods=["GET"])
def api_database_health():
    """The cheap poll: latched integrity, recent errors, WAL size.

    Deliberately does no ``quick_check`` and no per-table ``COUNT(*)`` -- both
    are full scans, and this is what a page polls while an operation runs.
    """
    try:
        import os

        from core.database import get_db_path
        from core.db_health import error_summary, recent_db_errors

        db_path = get_db_path()

        def _size(path):
            try:
                return os.path.getsize(path)
            except OSError:
                return 0

        return jsonify({
            "success": True,
            "last_known_integrity": app_state.get_db_integrity(),
            "errors": recent_db_errors(limit=20),
            "error_summary": error_summary(),
            "wal_size": _size(db_path + "-wal"),
            "db_size": _size(db_path),
        })
    except Exception as e:
        app_logger.error(f"api_database_health failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@database_bp.route("/api/database/backups", methods=["GET"])
def api_database_backups():
    """List of available DB backups (newest first)."""
    try:
        from core.database import list_backups

        return jsonify({"success": True, "backups": list_backups()})
    except Exception as e:
        app_logger.error(f"api_database_backups failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@database_bp.route("/api/database/backup", methods=["POST"])
def api_database_backup():
    """Force a manual backup, bypassing the unchanged-since-last-backup check."""
    try:
        from core.database import backup_database

        result = backup_database(max_backups=3, force=True)
        if result is False:
            return jsonify({"success": False, "error": "Backup failed (see logs)"}), 500
        if result is None:
            return jsonify({"success": False, "error": "Database does not exist"}), 404
        return jsonify({"success": True, "filename": result})
    except Exception as e:
        app_logger.error(f"api_database_backup failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@database_bp.route("/api/database/backups/<filename>", methods=["DELETE"])
def api_database_backup_delete(filename):
    """Delete a single backup ZIP."""
    try:
        from core.database import delete_backup

        delete_backup(filename)
        return jsonify({"success": True, "filename": filename})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except FileNotFoundError as e:
        return jsonify({"success": False, "error": f"Backup not found: {e}"}), 404
    except Exception as e:
        app_logger.error(f"api_database_backup_delete failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@database_bp.route("/api/database/backups/<filename>/download", methods=["GET"])
def api_database_backup_download(filename):
    """Stream a backup ZIP to the user as a download."""
    try:
        from core.database import get_backup_path

        full_path = get_backup_path(filename)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except FileNotFoundError as e:
        return jsonify({"success": False, "error": f"Backup not found: {e}"}), 404
    return send_file(
        full_path,
        as_attachment=True,
        download_name=filename,
        mimetype="application/zip",
    )


@database_bp.route("/api/database/quarantine/<filename>/download", methods=["GET"])
def api_database_quarantine_download(filename):
    """Download a corrupt-database snapshot for forensics.

    These are never offered for restore -- they contain the broken file -- but
    they are the only surviving copy of what was lost, so they are downloadable.
    Guarded by their own pattern; see ``_QUARANTINE_FILENAME_RE``.
    """
    try:
        from core.database import get_quarantine_path

        full_path = get_quarantine_path(filename)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except FileNotFoundError as e:
        return jsonify({"success": False, "error": f"Snapshot not found: {e}"}), 404
    return send_file(
        full_path,
        as_attachment=True,
        download_name=filename,
        mimetype="application/zip",
    )


@database_bp.route("/api/database/restore", methods=["POST"])
def api_database_restore():
    """Restore the DB from a previously created backup ZIP. The current DB is
    snapshotted to a pre-restore safety backup before being replaced."""
    try:
        from core.database import restore_database

        body = request.get_json(silent=True) or {}
        filename = body.get("filename")
        if not filename:
            return jsonify({"success": False, "error": "filename is required"}), 400
        result = restore_database(filename)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except FileNotFoundError as e:
        return jsonify({"success": False, "error": f"Backup not found: {e}"}), 404
    except RuntimeError as e:
        return jsonify({"success": False, "error": str(e)}), 409
    except Exception as e:
        app_logger.error(f"api_database_restore failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


@database_bp.route("/api/database/integrity", methods=["POST"])
def api_database_integrity():
    """Run an integrity check. ``{"full": true}`` backgrounds the thorough one."""
    try:
        from core.database import check_integrity

        body = request.get_json(silent=True) or {}
        full = bool(body.get("full"))

        if not full:
            ok, message = check_integrity(quick=True)
            app_state.set_db_integrity(ok, None if ok else message)
            if ok:
                from core.db_health import clear_db_errors

                clear_db_errors()
            return jsonify({"success": True, "ok": ok, "message": message,
                            "full": False})

        def _worker(progress):
            progress("running a full integrity check")
            ok, message = check_integrity(quick=False)
            app_state.set_db_integrity(ok, None if ok else message)
            if ok:
                from core.db_health import clear_db_errors

                clear_db_errors()
            # integrity_check returns one line per problem; keep them all.
            return {"success": True, "ok": ok, "message": message,
                    "lines": [ln for ln in str(message).split("; ") if ln]}

        op_id = _run_in_background(
            "db_integrity", "Full database integrity check", _worker
        )
        return jsonify({"success": True, "full": True, "op_id": op_id})
    except Exception as e:
        app_logger.error(f"api_database_integrity failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@database_bp.route("/api/database/checkpoint", methods=["POST"])
def api_database_checkpoint():
    """Checkpoint the WAL. Fast, and honest when a reader blocks it."""
    try:
        from core.db_maintenance import checkpoint_wal

        result = checkpoint_wal(truncate=True)
        if result.get("error"):
            return jsonify({"success": False, **result}), 500
        return jsonify({"success": True, **result})
    except Exception as e:
        app_logger.error(f"api_database_checkpoint failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@database_bp.route("/api/database/optimize", methods=["POST"])
def api_database_optimize():
    """ANALYZE + PRAGMA optimize. Cheap enough to run inline."""
    try:
        from core.db_maintenance import optimize_database

        result = optimize_database()
        if result.get("error"):
            return jsonify({"success": False, **result}), 500
        return jsonify({"success": True, **result})
    except Exception as e:
        app_logger.error(f"api_database_optimize failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@database_bp.route("/api/database/compact", methods=["POST"])
def api_database_compact():
    """Compact via ``VACUUM INTO`` and swap. Always backgrounded."""
    try:
        from core.db_maintenance import compact_database

        op_id = _run_in_background(
            "db_compact", "Compacting database",
            lambda progress: compact_database(progress=progress),
        )
        return jsonify({"success": True, "op_id": op_id})
    except Exception as e:
        app_logger.error(f"api_database_compact failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@database_bp.route("/api/database/operation/<op_id>", methods=["GET"])
def api_database_operation_result(op_id):
    """The stashed result of a finished maintenance operation.

    Separate from ``/api/operation/<op_id>`` (progress) because these results
    have to outlive the registry's TTL -- see ``_op_results``.
    """
    result = get_op_result(op_id)
    if result is None:
        return jsonify({"success": True, "pending": True})
    return jsonify({"success": True, "pending": False, "result": result})


# ---------------------------------------------------------------------------
# Salvage
# ---------------------------------------------------------------------------


@database_bp.route("/api/database/salvage", methods=["POST"])
def api_database_salvage_start():
    """Build a salvaged copy of the database. Never swaps anything."""
    try:
        from core.db_repair import is_running, start_salvage

        if is_running():
            return jsonify({"success": False,
                            "error": "A salvage is already running."}), 409

        op_id = _run_in_background(
            "db_salvage", "Salvaging database",
            lambda progress: start_salvage(progress=progress),
        )
        return jsonify({"success": True, "op_id": op_id})
    except Exception as e:
        app_logger.error(f"api_database_salvage_start failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@database_bp.route("/api/database/salvage", methods=["GET"])
def api_database_salvage_get():
    """The pending salvage candidate and its row-count diff, if any."""
    try:
        from core.db_repair import get_candidate, is_running

        return jsonify({
            "success": True,
            "running": is_running(),
            "candidate": get_candidate(),
        })
    except Exception as e:
        app_logger.error(f"api_database_salvage_get failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@database_bp.route("/api/database/salvage", methods=["DELETE"])
def api_database_salvage_discard():
    """Throw the candidate away."""
    try:
        from core.db_repair import discard_candidate

        discard_candidate()
        return jsonify({"success": True})
    except Exception as e:
        app_logger.error(f"api_database_salvage_discard failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@database_bp.route("/api/database/salvage/apply", methods=["POST"])
def api_database_salvage_apply():
    """Install the salvaged database. Requires the token from the diff."""
    try:
        from core.db_repair import apply_candidate

        body = request.get_json(silent=True) or {}
        token = body.get("token")
        if not token:
            return jsonify({"success": False, "error": "token is required"}), 400
        result = apply_candidate(token)
        return jsonify({"success": True, **result})
    except FileNotFoundError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    except RuntimeError as e:
        return jsonify({"success": False, "error": str(e)}), 409
    except Exception as e:
        app_logger.error(f"api_database_salvage_apply failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500
