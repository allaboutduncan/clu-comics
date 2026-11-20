"""Komga sync settings and controls (``/api/komga/*``).

Moved out of app.py so the routes can be tested. The sync itself
(``run_komga_sync``), its schedule and the next-run formatter stay in app.py
with the scheduler, and are imported inside the handlers the way the other
blueprints reach app.py: at import time app.py is still loading.

``core/auth.py`` gates ``/api/komga`` by path prefix, so the move changes no
permissions.
"""
import threading

from flask import Blueprint, jsonify, request

from core.app_logging import app_logger
from core.user_time import format_user_time

komga_bp = Blueprint("komga", __name__)


@komga_bp.route("/api/komga/config", methods=["GET"])
def api_get_komga_config():
    """Get current Komga sync configuration (password masked)."""
    from core.database import get_komga_config

    cfg = get_komga_config()
    if cfg:
        # Mask password for display
        cfg["password"] = "***" if cfg.get("password") else ""
    return jsonify({"success": True, "config": cfg or {}})


@komga_bp.route("/api/komga/config", methods=["POST"])
def api_save_komga_config():
    """Save Komga sync configuration."""
    from core.database import save_komga_config as db_save_komga_config

    data = request.get_json()

    success = db_save_komga_config(
        server_url=data.get("server_url", ""),
        username=data.get("username", ""),
        password=data.get("password", ""),
        enabled=data.get("enabled", False),
        frequency=data.get("frequency", "disabled"),
        time=data.get("time", "05:00"),
        weekday=data.get("weekday", 0),
        library_mappings=data.get("library_mappings", []),
    )

    if success:
        from app import configure_komga_sync_schedule

        configure_komga_sync_schedule()
        return jsonify({"success": True, "message": "Komga configuration saved"})
    return jsonify({"success": False, "error": "Failed to save configuration"}), 500


@komga_bp.route("/api/komga/test", methods=["POST"])
def api_test_komga_connection():
    """Test Komga server connectivity."""
    from core.database import get_komga_config, update_komga_connection_valid
    from models.komga import KomgaClient

    cfg = get_komga_config()
    if not cfg or not cfg.get("server_url"):
        return jsonify(
            {
                "success": False,
                "valid": False,
                "error": "Komga not configured. Save settings first.",
            }
        )

    username = cfg.get("username", "")
    password = cfg.get("password", "")

    app_logger.info(
        f"Komga test: url={cfg['server_url']}, user={'set' if username else 'empty'}, pass={'set' if password else 'empty'}"
    )

    if not username or not password:
        return jsonify(
            {
                "success": False,
                "valid": False,
                "error": "Komga credentials not set. Save credentials first.",
            }
        )

    try:
        client = KomgaClient(cfg["server_url"], username, password)
        valid, details = client.test_connection()
        update_komga_connection_valid(valid)
        if valid:
            return jsonify({"success": True, "valid": True, "message": details})
        else:
            return jsonify({"success": True, "valid": False, "error": details})
    except Exception as e:
        app_logger.error(f"Komga connection test error: {e}")
        update_komga_connection_valid(False)
        return jsonify({"success": False, "valid": False, "error": str(e)})


@komga_bp.route("/api/komga/sync", methods=["POST"])
def api_sync_komga_now():
    """Manually trigger Komga reading sync."""
    from app import run_komga_sync

    threading.Thread(target=run_komga_sync, daemon=True).start()
    return jsonify({"success": True, "message": "Komga sync started in background"})


@komga_bp.route("/api/komga/sync/status", methods=["GET"])
def api_komga_sync_status():
    """Get Komga sync status and statistics."""
    from core.database import get_komga_sync_stats, get_komga_config

    stats = get_komga_sync_stats()
    cfg = get_komga_config()

    # Get next scheduled run
    from app import get_next_run_for_job

    next_run_str = get_next_run_for_job("komga_sync")
    next_run = next_run_str if next_run_str != "Not scheduled" else None

    return jsonify(
        {
            "success": True,
            "total_synced_read": stats.get("total_synced_read", 0),
            "total_synced_progress": stats.get("total_synced_progress", 0),
            # Stored UTC, displayed beside next_run, so both go through the
            # user's offset.
            "last_sync": format_user_time(stats.get("last_sync")),
            "last_sync_read_count": cfg.get("last_sync_read_count", 0) if cfg else 0,
            "last_sync_progress_count": cfg.get("last_sync_progress_count", 0)
            if cfg
            else 0,
            "next_run": next_run,
        }
    )
