"""Guided Setup API (``/api/setup/*``).

**Owner-only, with no decorators here.** ``core/auth.py`` gates the
``/api/setup`` prefix in ``_OWNER_PREFIXES``, so every route in this file --
including the ones the wizard adds later -- inherits that.

A blueprint rather than routes in ``app.py``, because ``app.py`` cannot be
imported in tests. Heavy imports are done inside the views so the symbols stay
patchable.
"""
from flask import Blueprint, jsonify

setup_bp = Blueprint("setup", __name__)


@setup_bp.route("/api/setup/preflight", methods=["GET"])
def api_setup_preflight():
    """Read-only health checks for the install (volumes, DB, owner, user)."""
    from core.setup_preflight import run_preflight

    return jsonify(run_preflight())
