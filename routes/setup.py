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


@setup_bp.app_context_processor
def inject_setup_banner():
    """``show_setup_banner``: the upgraded-install banner in ``base.html``.

    Owner only, and never on the setup pages themselves. A failure says "no
    banner": it is an invitation, not something a page should fail over.
    """
    if request.path.startswith(("/setup", "/api/")):
        return {"show_setup_banner": False}
    try:
        from core.auth import load_current_user
        from core.setup_state import show_upgrade_banner

        user = load_current_user()
        show = bool(user and user.get("role") == "owner" and show_upgrade_banner())
    except Exception:
        show = False
    return {"show_setup_banner": show}


@setup_bp.route("/setup", methods=["GET"])
def setup_page():
    """The Guided Setup wizard. A visit moves ``none``/``upgraded`` on.

    Reached directly rather than via the redirect, a fresh install still
    counts as started; an upgraded install that opens it has taken up the
    banner's invitation, so the banner goes too.
    """
    from core.setup_preflight import (
        DOCS_BASE, DOCS_MONITORING, DOCS_SETTINGS, DOCS_SETUP, DOCS_USERS,
    )
    from core.setup_state import (
        STATE_IN_PROGRESS, STATE_NONE, STATE_UPGRADED, get_setup_state, set_setup_state,
    )

    state = get_setup_state()
    if state["state"] in (STATE_NONE, STATE_UPGRADED):
        set_setup_state(state=STATE_IN_PROGRESS)
        state["state"] = STATE_IN_PROGRESS
    docs = {
        "base": DOCS_BASE,
        "setup": DOCS_SETUP,
        "settings": DOCS_SETTINGS,
        "monitoring": DOCS_MONITORING,
        "users": DOCS_USERS,
        "metadata": f"{DOCS_BASE}/features/app-settings/metadata/",
        "comicvine_db": f"{DOCS_BASE}/features/local-databases/comicvine/",
        "gcd_db": f"{DOCS_BASE}/features/local-databases/gcd/",
    }
    return render_template("setup.html", setup_state=state, docs=docs)


@setup_bp.route("/setup-owner", methods=["GET"])
def setup_owner_page():
    """First-run screen to set the Store Owner's credentials.

    Only meaningful while the owner is a needs_setup placeholder; otherwise it
    redirects home. The form posts to ``/api/admin/setup-owner``.
    """
    from core.database import get_owner_user

    owner = get_owner_user()
    if not owner or not owner.get("needs_setup"):
        return redirect(url_for("index"))
    return render_template("setup_owner.html", current_page="/setup-owner")


@setup_bp.route("/api/setup/state", methods=["GET"])
def api_setup_state():
    from core.setup_state import get_setup_state

    return jsonify({"success": True, **get_setup_state()})


@setup_bp.route("/api/setup/state", methods=["POST"])
def api_setup_state_save():
    """Partial save of the lifecycle: an absent key is left alone.

    ``state`` is one of ``in_progress|skipped|completed`` (``none`` and
    ``upgraded`` belong to init_db); ``last_step`` is a step id, ``""``
    clears it; ``path`` is a list of use-case ids; ``banner_dismissed`` is a
    bool (the upgraded-install banner).
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

    banner_dismissed = data.get("banner_dismissed")
    if banner_dismissed is not None and not isinstance(banner_dismissed, bool):
        return jsonify({"success": False, "error": "banner_dismissed must be a bool"}), 400

    if not set_setup_state(state=state, last_step=last_step, path=path,
                           banner_dismissed=banner_dismissed):
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


@setup_bp.route("/api/setup/status", methods=["GET"])
def api_setup_status():
    """Per-section status for the progress rail and the Summary step."""
    from core.setup_status import get_setup_status

    return jsonify(get_setup_status())


@setup_bp.route("/api/setup/settings", methods=["GET"])
def api_setup_settings():
    """Current values of the settings the wizard edits, for prefill."""
    from core.setup_settings import read_wizard_settings

    return jsonify({"success": True, "settings": read_wizard_settings()})


@setup_bp.route("/api/setup/settings", methods=["POST"])
def api_setup_settings_save():
    """Partial save: only the keys sent are written, from a whitelist.

    The Settings page's own saves reset whatever a body leaves out, so the
    wizard, which shows a few fields per step, cannot use them.
    """
    from core.setup_settings import (
        read_wizard_settings, save_wizard_settings, validate_wizard_settings,
    )

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        return jsonify({"success": False, "error": "Expected a non-empty JSON object"}), 400
    error = validate_wizard_settings(data)
    if error:
        return jsonify({"success": False, "error": error}), 400
    if not save_wizard_settings(data):
        return jsonify({"success": False, "error": "Could not save the settings"}), 500
    return jsonify({"success": True, "settings": read_wizard_settings()})
