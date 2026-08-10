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


def resolve_optional_bearer_user(auth_header):
    """Resolve an optional ``Authorization: Bearer <token>`` header to a user.

    For endpoints that are public by default but can be *personalised* with an
    API token (e.g. ``/api/insights`` for a Homepage widget). Returns a
    ``(status, user)`` tuple:

        ("anonymous", None)  no Bearer credentials — the caller should fall
                             back to its default identity (the Store Owner).
        ("ok", user_dict)    a valid per-user token, or the legacy global token
                             (which maps to the Store Owner).
        ("invalid", None)    a Bearer token was presented but matched nothing —
                             the caller should reject with 401 so a
                             misconfigured token isn't silently served as the
                             owner's data.

    Unlike the ``/api/v1`` gate this never 503s: a missing token is a valid
    anonymous state, not an error. The per-user tokens are the same ones minted
    in Settings → Users and used by ``/api/v1``.
    """
    import hmac
    from core.database import (
        _hash_token,
        get_api_token,
        get_user_by_token_hash,
    )

    if not auth_header or not auth_header.startswith("Bearer "):
        return "anonymous", None

    presented = auth_header[len("Bearer "):].strip()
    if not presented:
        return "anonymous", None

    user = get_user_by_token_hash(_hash_token(presented))
    if user:
        return "ok", user

    legacy_token = get_api_token()
    if legacy_token and hmac.compare_digest(presented, legacy_token):
        return "ok", get_owner()

    return "invalid", None


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


# ---------------------------------------------------------------------------
# Folder-level access
#
# Library grants (library_permissions) are the coarse gate; folder grants
# (user_folder_permissions) refine access WITHIN a granted library. A grant on a
# folder covers that folder and every descendant (nested inheritance). A granted
# library with no folder grants shows nothing. A grant equal to the library root
# = the whole library. Owners and single-user (implicit-owner) installs bypass
# all of this and get full access.
#
# folder_access_level() returns one of:
#   'full'     — view + read files here (path at/under a grant).
#   'traverse' — path is an ancestor of a grant: list it so the user can navigate
#                down, but its direct contents must be filtered (grant siblings
#                stay hidden). NOT sufficient to serve a file.
#   'none'     — no access.
# ---------------------------------------------------------------------------


def _load_scope(user):
    """Fetch a non-owner's scope once so a whole listing can be filtered without
    per-item DB hits. Returns (libraries, granted_library_ids, grant_paths)."""
    from core.database import get_libraries, get_user_folder_paths

    libraries = get_libraries(enabled_only=True)
    granted = accessible_library_ids(user)
    grants = (
        [os.path.normpath(g) for g in get_user_folder_paths(user["id"])]
        if user else []
    )
    return libraries, granted, grants


def _level_for(path, libraries, granted, grants):
    """Pure access-level computation against pre-loaded scope (see _load_scope)."""
    if not path:
        return "none"
    norm = os.path.normpath(path)

    lib_root = None
    lib_id = None
    for lib in libraries:
        root = os.path.normpath(lib["path"])
        if norm == root or norm.startswith(root + os.sep):
            lib_root, lib_id = root, lib["id"]
            break
    if lib_root is None or lib_id not in granted:
        return "none"

    in_lib = [g for g in grants if g == lib_root or g.startswith(lib_root + os.sep)]
    if not in_lib:
        return "none"  # granted library, no folder picks -> nothing

    for g in in_lib:
        if norm == g or norm.startswith(g + os.sep):
            return "full"
    for g in in_lib:
        if g.startswith(norm + os.sep):
            return "traverse"
    return "none"


def folder_access_level(user, path):
    """Return 'full' | 'traverse' | 'none' for ``user`` accessing ``path``."""
    if not is_login_required():
        return "full"
    if not user:
        return "none"
    if user.get("role") == "owner":
        return "full"
    if not path:
        return "none"
    libraries, granted, grants = _load_scope(user)
    return _level_for(path, libraries, granted, grants)


def accessible_folder_prefixes(user):
    """Return the absolute folder prefixes a user may see, or None when
    unrestricted (owner / implicit-owner mode).

    Used by DB-aggregate readers (the metadata browser) to constrain rows to the
    user's granted subtrees. A non-owner with granted libraries but no folder
    picks — and an anonymous user in multi-user mode — returns [] (sees nothing).
    """
    if not is_login_required():
        return None
    if user and user.get("role") == "owner":
        return None
    if not user:
        return []
    libraries, granted, grants = _load_scope(user)
    lib_roots = [(os.path.normpath(l["path"]), l["id"]) for l in libraries]
    out = []
    for g in grants:
        for root, lid in lib_roots:
            if (g == root or g.startswith(root + os.sep)) and lid in granted:
                out.append(g)
                break
    return out


def user_can_access_path(user, path):
    """True if ``user`` may fully view and read files under ``path``.

    Owners bypass all scoping. Non-owners need the containing library granted AND
    a folder grant covering the path. This is the strict check used by
    file-serve, the reader, OPDS and the token API — a 'traverse'-only ancestor
    directory does NOT satisfy it.
    """
    return folder_access_level(user, path) == "full"


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
    "/api/account/",           # a user managing their OWN API tokens (self-service)
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
    "/api/dcpp",
    "/api/pull-list",
    "/api/bulk-metadata",
    "/api/source-wall",
    "/api/weekly-packs",
    "/logs/tail",              # raw app/monitor log content (can expose system info)
)

# Browser *pages* that are Clerk features (file management, pull list,
# releases/wanted, downloads, weekly packs, series search, publishers, metadata
# history). These are GET routes that would otherwise default to Reader; listing
# them here keeps Readers off both the page (direct-URL access → 403/redirect)
# and its nav entry (the template gates on the same role). The API endpoints
# backing them are already covered by _CLERK_PREFIXES / _OWNER_* above. Matched
# by exact path so a prefix can't swallow an unrelated reader route.
_CLERK_PAGE_PATHS = frozenset({
    "/files",            # File Manager
    "/pull-list",        # My Pull List
    "/releases",         # Weekly Releases
    "/wanted",           # Wanted Issues
    "/status",           # Download Status
    "/weekly-packs",     # Weekly Packs
    "/series-search",    # Series Search
    "/publishers",       # Publishers
    "/metadata/history",  # Metadata History
    "/source-wall",      # Source Wall
    "/logs",             # Logs viewer (can expose system info)
    "/app-logs",         # Logs alias (redirects to /logs)
    "/mon-logs",         # Logs alias (redirects to /logs)
})


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
    if path in _CLERK_PAGE_PATHS:
        return "clerk"
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


def filter_paths_for_user(user, items, key=None, allow_traverse=False):
    """Filter a list of paths (or dicts) to those the user may access.

    ``key`` names the dict field holding the path when ``items`` are dicts;
    when None, ``items`` are treated as plain path strings. No-op (returns the
    input) in implicit-owner mode so single-user installs are unaffected.

    ``allow_traverse`` keeps navigable ancestor directories (those on the path to
    a grant): pass True when filtering a directory listing so the user can walk
    down to a granted subfolder; keep it False for recent-files / search results,
    which must be fully accessible.
    """
    if not is_login_required():
        return items
    if user and user.get("role") == "owner":
        return items
    if not user:
        return []

    libraries, granted, grants = _load_scope(user)
    allowed = ("full", "traverse") if allow_traverse else ("full",)

    def _ok(path):
        return _level_for(path, libraries, granted, grants) in allowed

    if key is None:
        return [p for p in items if _ok(p)]
    return [it for it in items if _ok(it.get(key))]


def enforce_path_access(path, mode="full"):
    """Return a deny response if the current user may not access ``path``, else
    None. No-op in implicit-owner mode.

    ``mode='full'`` (default) is the strict check for file-serve / reader routes.
    ``mode='browse'`` also permits a 'traverse'-only ancestor directory, so a
    browse/list route can render the directory (its contents must then be run
    through ``filter_paths_for_user(..., allow_traverse=True)``).

    Call this inline *after* the path has been normalised to an absolute
    filesystem path.
    """
    if not is_login_required():
        return None
    user = current_user()
    if user is None:
        return _deny(401)
    level = folder_access_level(user, path)
    allowed = ("full", "traverse") if mode == "browse" else ("full",)
    if level not in allowed:
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
