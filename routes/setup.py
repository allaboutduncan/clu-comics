"""Guided Setup page and API (``/setup``, ``/api/setup/*``).

**Owner-only, with no decorators here.** ``core/auth.py`` gates the
``/setup`` and ``/api/setup`` prefixes in ``_OWNER_PREFIXES``, so every route
in this file -- including the ones the wizard adds later -- inherits that.

A blueprint rather than routes in ``app.py``, because ``app.py`` cannot be
imported in tests. Heavy imports are done inside the views so the symbols stay
patchable.
"""
from flask import Blueprint, jsonify, redirect, render_template, request, url_for

setup_bp = Blueprint("setup", __name__)


@setup_bp.before_app_request
def first_run_redirect():
    """Send the owner of a fresh install from ``GET /`` to ``/setup``, once.

    Independent of hook order: ``load_current_user`` is memoized on ``g``, so
    whether ``auth.require_login`` ran first or not we see the same user, and
    an anonymous request (``None``) is left for it to send to /login.
    """
    if request.method != "GET" or request.path != "/":
        return None
    from core.auth import load_current_user
    from core.setup_state import claim_first_run

    user = load_current_user()
    if not user or user.get("role") != "owner":
        return None
    if claim_first_run():
        return redirect(url_for("setup.setup_page"))
    return None


@setup_bp.route("/setup", methods=["GET"])
def setup_page():
    """Placeholder until the wizard lands: preflight, skip, link to Settings."""
    from core.setup_state import STATE_IN_PROGRESS, STATE_NONE, get_setup_state, set_setup_state

    state = get_setup_state()
    if state["state"] == STATE_NONE:  # reached directly, not via the redirect
        set_setup_state(state=STATE_IN_PROGRESS)
        state["state"] = STATE_IN_PROGRESS
    return render_template("setup.html", setup_state=state)


@setup_bp.route("/api/setup/state", methods=["GET"])
def api_setup_state():
    from core.setup_state import get_setup_state

    return jsonify({"success": True, **get_setup_state()})


@setup_bp.route("/api/setup/state", methods=["POST"])
def api_setup_state_save():
    """Partial save of the lifecycle: an absent key is left alone.

    ``state`` is one of ``in_progress|skipped|completed`` (``none`` and
    ``upgraded`` belong to init_db); ``last_step`` is a step id, ``""``
    clears it; ``path`` is a list of use-case ids.
    """
    from core.setup_state import WRITABLE_STATES, get_setup_state, is_valid_token, set_setup_state

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Expected a JSON object"}), 400

    state = data.get("state")
    if state is not None and state not in WRITABLE_STATES:
        return jsonify({"success": False,
                        "error": f"state must be one of {', '.join(WRITABLE_STATES)}"}), 400

    last_step = data.get("last_step")
    if last_step is not None and last_step != "" and not is_valid_token(last_step):
        return jsonify({"success": False, "error": "Invalid last_step"}), 400

    path = data.get("path")
    if path is not None:
        if not isinstance(path, list) or not all(is_valid_token(p) for p in path):
            return jsonify({"success": False,
                            "error": "path must be a list of use-case ids"}), 400
        path = list(dict.fromkeys(path))

    if not set_setup_state(state=state, last_step=last_step, path=path):
        return jsonify({"success": False, "error": "Could not save setup state"}), 500
    return jsonify({"success": True, **get_setup_state()})


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
