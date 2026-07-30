"""
Browser login gate + per-request identity resolution.

Authentication is *opt-in*: while only the implicit Store Owner exists (and no
CLU_USERNAME/CLU_PASSWORD env gate is configured) the app requires no login and
runs as the owner, exactly as the single-user app did. Login/RBAC activate once
a second account exists or the env gate is set (see core.auth.is_login_required).

Credentials are validated against the ``users`` table (werkzeug-hashed
passwords); the legacy CLU_USERNAME/CLU_PASSWORD are migrated into a hashed
owner account at startup, so they continue to work through this same path.

OPDS, static, and /api/v1 routes are exempt from the browser gate (OPDS and the
token API carry their own identity).
"""
from flask import (
    Blueprint,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from core.auth import (
    check_request_permitted,
    current_user,
    is_login_required,
    load_current_user,
)
from core.database import (
    create_api_token,
    delete_api_token,
    list_api_tokens,
    update_last_login,
    verify_password,
)

auth_bp = Blueprint("auth", __name__)


# Download endpoints (api.py) are auth-free by design so the browser extension
# — which sends no Flask session cookie — can queue downloads. "/download"
# prefix-matches /download, /download_status, /download_status_all and
# /download_summary; the management endpoints are listed explicitly.
_EXEMPT_PREFIXES = (
    "/login", "/logout", "/static/", "/opds", "/api/insights", "/api/v1/",
    "/download",
    "/cancel_download", "/retry_download", "/dismiss_download",
    "/clear_downloads", "/clear_failed_downloads",
)


@auth_bp.before_app_request
def require_login():
    """Resolve the current user for every request and gate browser access.

    Always populates ``g.current_user`` (to the owner in implicit-owner mode).
    Redirects to /login only when login is required and no user is
    authenticated.
    """
    if any(request.path.startswith(p) for p in _EXEMPT_PREFIXES):
        return None

    user = load_current_user()

    if not is_login_required():
        # Implicit-owner mode: no login, no RBAC — single-user behaviour.
        return None

    if user is None:
        # API namespaces get a JSON 401; browser routes redirect to login.
        if request.path.startswith(("/api/", "/opds")):
            return jsonify({"error": "unauthorized"}), 401
        return redirect(url_for("auth.login", next=request.path))

    # Multi-user mode: enforce role-based access for the resolved user.
    return check_request_permitted(user)


@auth_bp.teardown_app_request
def _reset_request_identity(exc=None):
    """Clear the per-request identity cache at request teardown.

    ``load_current_user`` memoizes the resolved user on ``g.current_user`` for
    the duration of a request. In production each request gets its own
    application context, so this is discarded automatically. When an application
    context spans multiple requests, though — e.g. pytest-flask pushes one shared
    context per test — the cached user would otherwise leak into the next request
    (a logged-out session still reads as authenticated; a second user's write is
    attributed to the first). Popping it here guarantees every request resolves
    its own identity from its own session.
    """
    g.pop("current_user", None)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if not is_login_required():
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        user = verify_password(username, password)
        if user:
            session["user_id"] = user["id"]
            session["authenticated"] = True  # legacy flag, kept for compatibility
            update_last_login(user["id"])
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)

        error = "Invalid username or password."

    return render_template("login.html", error=error)


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Self-service account page: any logged-in user manages their OWN API tokens.
#
# Token minting used to be owner-only (via /api/admin). These routes let every
# role (reader included) create/list/revoke tokens scoped to themselves. All
# operations pass g.current_user["id"] to the user-scoped DB helpers, so a user
# can never see or delete another user's tokens. The reader-write allowance for
# the POST/DELETE lives in core.auth._READER_WRITE_PREFIXES ("/api/account/").
# A token only ever authenticates as its owner, so it grants no privilege the
# user doesn't already have.
# ---------------------------------------------------------------------------


@auth_bp.route("/account")
def account():
    """Self-service account page (currently: personal API tokens)."""
    return render_template("account.html")


@auth_bp.route("/api/account/tokens", methods=["GET"])
def account_list_tokens():
    """List the current user's API tokens (metadata only)."""
    user = current_user()
    if user is None:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"success": True, "tokens": list_api_tokens(user["id"])})


@auth_bp.route("/api/account/tokens", methods=["POST"])
def account_create_token():
    """Issue a new API token for the current user. Plaintext is returned once."""
    user = current_user()
    if user is None:
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip() or None
    token = create_api_token(user["id"], name=name)
    if not token:
        return jsonify({"success": False, "error": "could not create token"}), 500
    return jsonify({"success": True, "token": token, "name": name}), 201


@auth_bp.route("/api/account/tokens/<int:token_id>", methods=["DELETE"])
def account_delete_token(token_id):
    """Revoke one of the current user's tokens (scoped to the owner)."""
    user = current_user()
    if user is None:
        return jsonify({"error": "unauthorized"}), 401
    if not delete_api_token(token_id, user_id=user["id"]):
        return jsonify({"success": False, "error": "token not found"}), 404
    return jsonify({"success": True})
