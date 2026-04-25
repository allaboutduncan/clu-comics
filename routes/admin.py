"""
Admin endpoints for the Settings/config page.

These run on the same browser auth as the rest of the config page (the
optional CLU_USERNAME/CLU_PASSWORD session gate). They are *not* under
/api/v1/ — that namespace is the bearer-token API for offline clients,
and the token managed here is the very thing it authenticates against.
"""

from flask import Blueprint, jsonify

from core.app_logging import app_logger
from core.database import get_api_token, rotate_api_token


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
