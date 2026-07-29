"""
Multi-user identity & role-based access control (RBAC).

This module owns the notion of "who is the current user" and "may they do
this". It is deliberately additive: when no real user accounts have been
created the app runs in *implicit-owner* mode — every request is treated as
the Store Owner and no login is required — so existing single-user installs
behave exactly as before.

Role hierarchy (ascending privilege):

    reader  — read/view comics, personal reading data
    clerk   — reader + file ops, metadata/scrape, downloads, pull list
    owner   — clerk + app settings, user management, library grants

Identity is resolved once per request into ``flask.g.current_user`` (a plain
dict from the ``users`` table, or None). The browser path is wired from
``routes/auth.py``; the ``/api/v1`` token path sets ``g.current_user`` from its
own bearer-token ``before_request`` hook.
"""
import functools
import os

from flask import g, request, redirect, url_for, jsonify, flash

from core.database import (
    count_users,
    get_owner_user,
    get_user_by_id,
    user_has_library,
)

# Ascending privilege. Unknown roles sort to 0 (deny everything).
ROLE_LEVELS = {"reader": 1, "clerk": 2, "owner": 3}


def role_at_least(user, minimum):
    """True if ``user`` holds at least the ``minimum`` role."""
    if not user:
        return False
    return ROLE_LEVELS.get(user.get("role"), 0) >= ROLE_LEVELS.get(minimum, 99)


def get_owner():
    """Return the Store Owner account (the implicit-mode identity)."""
    return get_owner_user()


def is_login_required():
    """Whether the app should force login.

    True when more than one account exists, or when the legacy
    ``CLU_USERNAME``/``CLU_PASSWORD`` env gate is configured. Otherwise the app
    stays login-free and everyone is the owner (backward-compat guarantee).
    """
    if os.environ.get("CLU_USERNAME") and os.environ.get("CLU_PASSWORD"):
        return True
    return count_users() > 1


def resolve_current_user():
    """Resolve the effective user for this request without touching ``g``.

    Session-authenticated user if present; otherwise the implicit owner when
    login is not required; otherwise None (anonymous / must log in).
    """
    from flask import session

    uid = session.get("user_id")
    if uid:
        user = get_user_by_id(uid)
        if user and user.get("is_active"):
            return user
    if not is_login_required():
        return get_owner()
    return None


def load_current_user():
    """Populate ``g.current_user`` for the request. Idempotent."""
    if getattr(g, "current_user", None) is None:
        g.current_user = resolve_current_user()
    return g.current_user


def current_user():
    """Return the request's current user, resolving lazily if needed."""
    user = getattr(g, "current_user", None)
    if user is None:
        user = load_current_user()
    return user


def _wants_json():
    """True when the caller expects a JSON error rather than an HTML redirect."""
    return request.path.startswith("/api/") or request.path.startswith("/opds")


def _deny(status=403):
    if _wants_json():
        return jsonify({"error": "forbidden" if status == 403 else "unauthorized"}), status
    if status == 401:
        return redirect(url_for("auth.login", next=request.path))
    flash("You don't have permission to do that.")
    # index is the app-level landing route; fall back to root if unavailable.
    try:
        return redirect(url_for("index"))
    except Exception:
        return redirect("/")


def require_role(minimum):
    """Decorator: require the current user to hold at least ``minimum`` role.

    Emits JSON 403 for API/OPDS routes, redirect/flash for HTML routes. In
    implicit-owner mode the owner satisfies every role.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            if not is_login_required():
                return fn(*args, **kwargs)  # implicit-owner mode: unrestricted
            user = current_user()
            if user is None:
                return _deny(401)
            if not role_at_least(user, minimum):
                return _deny(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def user_can_access_path(user, path):
    """True if ``user`` may access the library containing ``path``.

    Owners bypass all library scoping. Non-owners need an explicit grant for
    the library the path resolves to. A path outside every configured library
    is denied for non-owners.
    """
    if not user:
        return False
    if user.get("role") == "owner":
        return True
    if not path:
        return False
    from helpers.library import get_library_for_path

    library = get_library_for_path(path)
    if not library:
        return False
    return user_has_library(user["id"], library["id"])


# ---------------------------------------------------------------------------
# Centralized route policy (role required per request)
#
# Enforcement is applied ONLY when login is required (multi-user mode). In
# implicit-owner mode the gate is skipped entirely, so single-user installs are
# unaffected. Owners always satisfy every rule.
#
# The policy leans on a safe default: any *mutating* request (POST/PUT/DELETE/
# PATCH) that isn't otherwise classified requires Clerk, while reads default to
# Reader. That means most file/metadata/download mutations are covered without
# enumeration; we only enumerate the Owner surface, the few GET-sensitive Clerk
# areas, and the personal-data writes a Reader must be allowed to perform.
# ---------------------------------------------------------------------------

_MUTATING_METHODS = {"POST", "PUT", "DELETE", "PATCH"}

# Owner-only regardless of method (settings, system, DB, user/token admin).
_OWNER_PREFIXES = (
    "/api/admin",
    "/config",
    "/api/config/",
    "/api/database",
    "/api/komga",
    "/api/save-",              # schedule save endpoints
    "/schedules",
    "/gcd-import",
    "/restart",
    "/api/rebuild-file-index",
    "/api/providers",          # provider credentials (secrets)
    "/users",                  # user-management page
    "/setup-owner",            # first-run owner setup page
)

# GET is Reader (browsing), but creating/editing/deleting requires Owner.
_OWNER_MUTATION_PREFIXES = (
    "/api/libraries",
    "/api/publishers",
)

# Personal-data writes a Reader may perform on their own data. Trailing slashes
# keep these from swallowing unrelated paths (e.g. "/api/reading-lists").
_READER_WRITE_PREFIXES = (
    "/api/read/",              # /api/read/<path>/page/<n>
    "/api/mark-comic-read",    # records the reader's own read history
    "/api/reading-position",
    "/api/reading-stats",
    "/api/favorites",          # favorites_bp
    "/api/to-read",
    "/to-read",
    "/api/continue-reading",
    "/api/on-the-stack",
)

# Read-only endpoints that use POST purely to carry a request body (a batch of
# paths), not to mutate anything. Without an explicit entry these fall through
# to the "mutation → clerk" default and 403 for Readers, breaking the parts of
# the collection grid that load lazily via these batch endpoints — folder
# thumbnails and folder/file counts — which every role must be able to view.
# Keep this list to genuine reads.
_READER_READ_POST_PREFIXES = (
    "/api/browse-thumbnails",  # batch folder-thumbnail lookup
    "/api/browse-metadata",    # batch folder/file counts
)

# Clerk-level areas where even GET is a Clerk feature (not just Reader viewing).
_CLERK_PREFIXES = (
    "/api/getcomics",
    "/api/download-clients",
    "/api/indexers",
    "/api/usenet",
    "/api/pull-list",
    "/api/bulk-metadata",
    "/api/source-wall",
    "/api/weekly-packs",
)


def required_role_for_request():
    """Return the minimum role required for the current request's path+method."""
    path = request.path
    mutating = request.method in _MUTATING_METHODS

    if path.startswith(_OWNER_PREFIXES):
        return "owner"
    if mutating and path.startswith(_OWNER_MUTATION_PREFIXES):
        return "owner"
    if path.startswith(_READER_WRITE_PREFIXES):
        return "reader"
    if path.startswith(_READER_READ_POST_PREFIXES):
        return "reader"
    if path.startswith(_CLERK_PREFIXES):
        return "clerk"
    if mutating:
        return "clerk"
    return "reader"


def check_request_permitted(user):
    """Return a deny response if ``user`` lacks the role required for this
    request, else None. Caller is responsible for only invoking this in
    multi-user mode."""
    required = required_role_for_request()
    if not role_at_least(user, required):
        return _deny(403 if user else 401)
    return None


def accessible_library_ids(user):
    """Return the set of library ids ``user`` may access.

    Owners (and, defensively, a missing user in implicit-owner mode) get every
    enabled library; other users get exactly their granted set.
    """
    from core.database import get_libraries, get_user_library_ids

    if not user or user.get("role") == "owner":
        return {lib["id"] for lib in get_libraries(enabled_only=True)}
    return get_user_library_ids(user["id"])


def filter_paths_for_user(user, items, key=None):
    """Filter a list of paths (or dicts) to those the user may access.

    ``key`` names the dict field holding the path when ``items`` are dicts;
    when None, ``items`` are treated as plain path strings. No-op (returns the
    input) in implicit-owner mode so single-user installs are unaffected.
    """
    if not is_login_required():
        return items
    if user and user.get("role") == "owner":
        return items

    from helpers.library import get_library_for_path

    allowed = accessible_library_ids(user)

    def _ok(path):
        if not path:
            return False
        lib = get_library_for_path(path)
        return bool(lib) and lib["id"] in allowed

    if key is None:
        return [p for p in items if _ok(p)]
    return [it for it in items if _ok(it.get(key))]


def enforce_path_access(path):
    """Return a deny response if the current user may not access the library
    containing ``path``, else None. No-op in implicit-owner mode.

    Call this inline in file-serve / reader / browse routes *after* the path has
    been normalised to an absolute filesystem path.
    """
    if not is_login_required():
        return None
    user = current_user()
    if user is None:
        return _deny(401)
    if not user_can_access_path(user, path):
        return _deny(403)
    return None


def require_library_access(path_arg="path"):
    """Decorator: require the current user to have access to the library that
    contains the request's target path.

    The path is read (in order) from the view kwargs, query string, then form
    body, under the name ``path_arg``.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            if not is_login_required():
                return fn(*args, **kwargs)  # implicit-owner mode: unrestricted
            user = current_user()
            if user is None:
                return _deny(401)
            target = (
                kwargs.get(path_arg)
                or request.args.get(path_arg)
                or request.form.get(path_arg)
            )
            if not user_can_access_path(user, target):
                return _deny(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorator
