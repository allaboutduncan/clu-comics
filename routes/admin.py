"""
Admin endpoints for the Settings/config page.

These run on the same browser auth as the rest of the config page (the
optional CLU_USERNAME/CLU_PASSWORD session gate). They are *not* under
/api/v1/ — that namespace is the bearer-token API for offline clients,
and the token managed here is the very thing it authenticates against.
"""

from flask import Blueprint, Response, jsonify, request

from core.app_logging import app_logger
from core.database import (
    get_api_browse_mode,
    get_api_token,
    rotate_api_token,
    set_api_browse_mode,
)
from core.debug_package import build_debug_package
from core.version import __version__


admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")


@admin_bp.route("/api-token", methods=["GET"])
def get_token():
    """Return the long-lived API token used by the offline mobile/desktop client."""
    token = get_api_token()
    return jsonify({
        "success": True,
        "configured": bool(token),
        "token": token or "",
    })


@admin_bp.route("/api-token/rotate", methods=["POST"])
def rotate_token():
    """Generate a fresh API token, replacing any existing one."""
    try:
        token = rotate_api_token()
        return jsonify({"success": True, "token": token})
    except Exception as e:
        app_logger.error(f"Failed to rotate API token: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route("/api-browse-mode", methods=["GET"])
def get_browse_mode():
    """Return the saved /api/v1/library/* browse mode."""
    return jsonify({"success": True, "mode": get_api_browse_mode()})


@admin_bp.route("/api-browse-mode", methods=["PUT"])
def put_browse_mode():
    """Persist the /api/v1/library/* browse mode (metadata|filesystem)."""
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    if not set_api_browse_mode(mode):
        return jsonify({
            "success": False,
            "error": "mode must be 'metadata' or 'filesystem'",
        }), 400
    return jsonify({"success": True, "mode": get_api_browse_mode()})


@admin_bp.route("/debug-package", methods=["GET"])
def download_debug_package():
    """Build and return a redacted debug package (config, settings, logs) as a ZIP."""
    try:
        data = build_debug_package()
        return Response(
            data,
            mimetype="application/zip",
            headers={
                "Content-Disposition":
                    f"attachment; filename=clu-debug-{__version__}.zip",
            },
        )
    except Exception as e:
        app_logger.error(f"Failed to build debug package: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
