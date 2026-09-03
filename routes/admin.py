"""
Admin endpoints for the Settings/config page.

These run on the same browser auth as the rest of the config page (the
optional CLU_USERNAME/CLU_PASSWORD session gate). They are *not* under
/api/v1/ — that namespace is the bearer-token API for offline clients,
and the token managed here is the very thing it authenticates against.
"""

from flask import Blueprint, Response, jsonify, request, session

from core.app_logging import app_logger
from core.auth import current_user, is_login_required, require_role
from core.database import (
    count_owners,
    create_api_token,
    create_user,
    delete_api_token,
    delete_user,
    list_api_tokens,
    get_api_browse_mode,
    get_api_token,
    get_libraries,
    get_owner_user,
    get_user_by_id,
    get_user_by_username,
    get_user_folder_paths,
    get_user_library_ids,
    list_users,
    rotate_api_token,
    set_api_browse_mode,
    set_user_folders,
    set_user_libraries,
    set_user_password,
    update_user,
)
from core.debug_package import build_debug_package
from core.version import __version__


admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")

VALID_ROLES = ("reader", "clerk", "owner")


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


# ---------------------------------------------------------------------------
# User management (Store Owner only)
# ---------------------------------------------------------------------------


def _user_payload(user):
    """Attach the user's granted library ids and folder paths to a public dict."""
    user = dict(user)
    user["library_ids"] = sorted(get_user_library_ids(user["id"]))
    user["folder_paths"] = get_user_folder_paths(user["id"])
    return user


@admin_bp.route("/setup-owner", methods=["POST"])
@require_role("owner")
def setup_owner_route():
    """First-run: set real credentials on the placeholder Store Owner.

    Reachable in implicit-owner mode (require_role short-circuits when login
    isn't required) so a fresh install can bootstrap its owner before any login
    is enforced.
    """
    owner = get_owner_user()
    if not owner:
        return jsonify({"success": False, "error": "no owner account"}), 404
    if not owner.get("needs_setup"):
        return jsonify({"success": False, "error": "owner already configured"}), 409

    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not password:
        return jsonify({"success": False, "error": "password is required"}), 400

    if username and username.lower() != owner["username"].lower():
        if not update_user(owner["id"], username=username):
            return jsonify({
                "success": False,
                "error": "username unavailable",
            }), 409
    set_user_password(owner["id"], password)  # also clears needs_setup
    return jsonify({"success": True, "user": _user_payload(get_user_by_id(owner["id"]))})


@admin_bp.route("/users", methods=["GET"])
@require_role("owner")
def list_users_route():
    """List all accounts (no password hashes) with their library grants."""
    users = [_user_payload(u) for u in list_users()]
    libraries = get_libraries()
    return jsonify({"success": True, "users": users, "libraries": libraries})


@admin_bp.route("/users", methods=["POST"])
@require_role("owner")
def create_user_route():
    """Create a Reader/Clerk/Owner account."""
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = (body.get("role") or "reader").lower()
    display_name = body.get("display_name")
    email = body.get("email")
    library_ids = body.get("library_ids") or []
    folder_paths = body.get("folder_paths") or []

    if not username:
        return jsonify({"success": False, "error": "username is required"}), 400
    if not password:
        return jsonify({"success": False, "error": "password is required"}), 400
    if role not in VALID_ROLES:
        return jsonify({"success": False, "error": "invalid role"}), 400

    # A second account ends implicit-owner mode and turns login on for
    # everyone (core.auth.is_login_required). A placeholder owner has no
    # password and cannot authenticate, so that flip would lock every user out
    # of the install permanently. Refuse until first-run setup has given the
    # owner real credentials.
    owner = get_owner_user()
    if owner and owner.get("needs_setup") and not is_login_required():
        return jsonify({
            "success": False,
            # The page keys off this to close the dialog: nothing in the form
            # can fix this, so the message has to be readable behind it.
            "code": "owner_needs_setup",
            "error": "Set the Store Owner's username and password first — "
                     "adding a second account turns on login, and the owner "
                     "has no password yet.",
        }), 409

    user_id = create_user(username, password=password, role=role,
                          display_name=display_name, email=email)
    if not user_id:
        return jsonify({
            "success": False,
            "error": "could not create user (username may already exist)",
        }), 409

    if library_ids:
        set_user_libraries(user_id, library_ids)
    if folder_paths:
        set_user_folders(user_id, folder_paths)

    # This account may have just turned login on. In implicit-owner mode the
    # owner never logged in, so they hold no session and would be anonymous on
    # their very next request — the page reports a failure and bounces them to
    # /login moments after a successful create. Persist the identity this
    # browser was already acting under so the flip is invisible to it. Only the
    # browser that made the call is stamped, and only when it was already the
    # owner, so it gains no privilege it did not have a moment ago.
    if not session.get("user_id"):
        acting = current_user()
        if acting and acting.get("role") == "owner":
            session["user_id"] = acting["id"]
            session["authenticated"] = True  # legacy flag, kept for compatibility

    return jsonify({"success": True, "user": _user_payload(get_user_by_id(user_id))}), 201


@admin_bp.route("/users/<int:user_id>", methods=["PUT"])
@require_role("owner")
def update_user_route(user_id):
    """Update role/status/profile/password/library grants for an account.

    Guards the last active Store Owner from being demoted or deactivated.
    """
    target = get_user_by_id(user_id)
    if not target:
        return jsonify({"success": False, "error": "user not found"}), 404

    body = request.get_json(silent=True) or {}
    role = body.get("role")
    is_active = body.get("is_active")
    display_name = body.get("display_name")
    email = body.get("email")
    password = body.get("password")
    library_ids = body.get("library_ids")
    folder_paths = body.get("folder_paths")

    if role is not None and role.lower() not in VALID_ROLES:
        return jsonify({"success": False, "error": "invalid role"}), 400

    # Resolve a username change (only when it actually differs), guarding
    # against collisions with another account.
    new_username = None
    raw_username = (body.get("username") or "").strip()
    if raw_username and raw_username.lower() != target["username"].lower():
        clash = get_user_by_username(raw_username)
        if clash and clash["id"] != user_id:
            return jsonify({
                "success": False,
                "error": "username already exists",
            }), 409
        new_username = raw_username

    # Protect the last active owner from losing owner access.
    demoting = target["role"] == "owner" and role is not None and role != "owner"
    deactivating = target["role"] == "owner" and is_active is False
    if (demoting or deactivating) and count_owners() <= 1:
        return jsonify({
            "success": False,
            "error": "cannot remove the last Store Owner",
        }), 400

    update_user(
        user_id,
        username=new_username,
        role=role.lower() if role is not None else None,
        display_name=display_name,
        email=email,
        is_active=is_active,
    )
    if password:
        set_user_password(user_id, password)
    if library_ids is not None:
        set_user_libraries(user_id, library_ids)
    if folder_paths is not None:
        set_user_folders(user_id, folder_paths)

    return jsonify({"success": True, "user": _user_payload(get_user_by_id(user_id))})


@admin_bp.route("/users/<int:user_id>", methods=["DELETE"])
@require_role("owner")
def delete_user_route(user_id):
    """Delete an account. The last active Store Owner cannot be deleted."""
    target = get_user_by_id(user_id)
    if not target:
        return jsonify({"success": False, "error": "user not found"}), 404
    if target["role"] == "owner" and count_owners() <= 1:
        return jsonify({
            "success": False,
            "error": "cannot delete the last Store Owner",
        }), 400

    delete_user(user_id)
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Per-user API tokens (Store Owner only)
# ---------------------------------------------------------------------------


@admin_bp.route("/users/<int:user_id>/tokens", methods=["GET"])
@require_role("owner")
def list_user_tokens_route(user_id):
    """List a user's API tokens (metadata only — never the plaintext/hash)."""
    if not get_user_by_id(user_id):
        return jsonify({"success": False, "error": "user not found"}), 404
    return jsonify({"success": True, "tokens": list_api_tokens(user_id)})


@admin_bp.route("/users/<int:user_id>/tokens", methods=["POST"])
@require_role("owner")
def create_user_token_route(user_id):
    """Issue a new API token for a user. The plaintext token is returned exactly
    once; only its hash is stored."""
    if not get_user_by_id(user_id):
        return jsonify({"success": False, "error": "user not found"}), 404
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip() or None
    token = create_api_token(user_id, name=name)
    if not token:
        return jsonify({"success": False, "error": "could not create token"}), 500
    return jsonify({"success": True, "token": token, "name": name}), 201


@admin_bp.route("/tokens/<int:token_id>", methods=["DELETE"])
@require_role("owner")
def delete_token_route(token_id):
    """Revoke (delete) an API token by id."""
    if not delete_api_token(token_id):
        return jsonify({"success": False, "error": "token not found"}), 404
    return jsonify({"success": True})


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
