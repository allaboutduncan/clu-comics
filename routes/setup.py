"""Guided Setup API (``/api/setup/*``).

**Owner-only, with no decorators here.** ``core/auth.py`` gates the
``/api/setup`` prefix in ``_OWNER_PREFIXES``, so every route in this file --
including the ones the wizard adds later -- inherits that.

A blueprint rather than routes in ``app.py``, because ``app.py`` cannot be
imported in tests. Heavy imports are done inside the views so the symbols stay
patchable.
"""
from flask import Blueprint, jsonify, request

setup_bp = Blueprint("setup", __name__)


@setup_bp.route("/api/setup/preflight", methods=["GET"])
def api_setup_preflight():
    """Read-only health checks for the install (volumes, DB, owner, user)."""
    from core.setup_preflight import run_preflight

    return jsonify(run_preflight())


@setup_bp.route("/api/setup/browse", methods=["GET"])
def api_setup_browse():
    """Folder picker limited to /data and /downloads (no ``path``: the roots).

    ``/list-directories`` refuses anything outside the library roots and
    TARGET, so it cannot pick a path that is not configured yet.
    """
    from core.setup_browse import BrowseError, browse

    try:
        result = browse(request.args.get("path", "").strip())
    except BrowseError as e:
        return jsonify({"success": False, "error": e.message}), e.status
    return jsonify({"success": True, **result})
