"""
Optional environment-variable login gate.

When CLU_USERNAME *and* CLU_PASSWORD are both set the app requires
browser-based authentication.  When either is absent the app behaves
exactly as before (no login required).

OPDS and static routes are always exempt so comic-reader apps and
assets continue to work without a browser session.
"""
import hmac

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

auth_bp = Blueprint("auth", __name__)


def _auth_enabled():
    """Return True when both CLU_USERNAME and CLU_PASSWORD are configured."""
    return bool(
        current_app.config.get("CLU_USERNAME")
        and current_app.config.get("CLU_PASSWORD")
    )


_EXEMPT_PREFIXES = ("/login", "/logout", "/static/", "/opds", "/api/insights", "/api/v1/")


@auth_bp.before_app_request
def require_login():
    """Redirect unauthenticated users to /login when auth is enabled."""
    if not _auth_enabled():
        return None

    if any(request.path.startswith(p) for p in _EXEMPT_PREFIXES):
        return None

    if session.get("authenticated"):
        return None

    return redirect(url_for("auth.login", next=request.path))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if not _auth_enabled():
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        expected_user = current_app.config["CLU_USERNAME"]
        expected_pass = current_app.config["CLU_PASSWORD"]

        if hmac.compare_digest(username, expected_user) and hmac.compare_digest(
            password, expected_pass
        ):
            session["authenticated"] = True
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)

        error = "Invalid username or password."

    return render_template("login.html", error=error)


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
