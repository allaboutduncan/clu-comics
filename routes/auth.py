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
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from core.auth import (
    check_request_permitted,
    is_login_required,
    load_current_user,
)
from core.database import update_last_login, verify_password

auth_bp = Blueprint("auth", __name__)


_EXEMPT_PREFIXES = ("/login", "/logout", "/static/", "/opds", "/api/insights", "/api/v1/")


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
