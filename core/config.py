"""Application configuration.

Settings live in the ``user_preferences`` table; read and write them through
``core.settings``. ``config.ini`` is gone: nothing reads or writes it, and
``load_config`` only warns about one that was left behind.
"""
import os

from core.app_logging import app_logger

# Use /config volume if it exists (Docker), otherwise use current directory
CONFIG_DIR = os.environ.get('CONFIG_DIR', '/config' if os.path.exists('/config') else os.getcwd())
# Ensure config directory exists
os.makedirs(CONFIG_DIR, exist_ok=True)

# The cache volume (thumbnails, trash). Fixed, like /config: users mount a
# volume onto it (see docker-compose.yaml), so a second way to point elsewhere
# only adds a way to misconfigure. It stays a constant rather than a stored
# setting because core/database.py needs it before the database exists.
CACHE_DIR = "/cache"

# The library mount. docker-compose.yaml requires the library at /data, so like
# /cache it is a fixed path, not a setting (the old `DATA_DIR` ini key was never
# written by anything and always read back as this default).
DATA_DIR = "/data"


def get_cache_dir() -> str:
    """The cache volume (thumbnails, trash). A function so tests patch one name."""
    return CACHE_DIR


def get_config_dir() -> str:
    """The config volume (database, backups, logs). A function so tests patch one name."""
    return CONFIG_DIR


def get_data_dir() -> str:
    """The library mount."""
    return DATA_DIR


# The downloads mount (WATCH/TARGET default to /downloads/temp and
# /downloads/processed under it). Fixed like /data; only the Guided Setup
# folder picker asks for the mount itself.
DOWNLOADS_DIR = "/downloads"


def get_downloads_dir() -> str:
    """The downloads mount. A function so tests patch one name."""
    return DOWNLOADS_DIR


def is_oneshot_folder(folder_path) -> bool:
    """True when the folder's base name is in the ONESHOT_FOLDERS setting.

    One-shot folders (default: oneshots, one-shots, specials) hold unrelated
    single issues, so metadata writes must not drop folder-level sidecars
    (cvinfo / series.json) that would wrongly bind every file to one series.
    Matching is case-insensitive on the base name, ignoring stray slashes.
    """
    if not folder_path:
        return False
    try:
        from core.settings import get_setting
        names = get_setting("ONESHOT_FOLDERS")
    except Exception:
        names = "oneshots,one-shots,specials"
    wanted = {n.strip().strip('/\\').lower() for n in names.split(',') if n.strip()}
    base = os.path.basename(str(folder_path).rstrip('/\\')).lower()
    return bool(base) and base in wanted


_leftover_ini_reported = False


def load_config():
    """Warn, once per process, about a ``config.ini`` that was never imported.

    Settings live in ``user_preferences``. Releases 7.0 to 7.3 imported a
    legacy ``config.ini`` on first start and renamed it ``config.ini.migrated``;
    that import is gone. A file still sitting there means the install skipped
    those releases, so its settings are not in the database and the app is
    running on defaults: say so, loudly, instead of looking like a fresh
    install. Nothing reads or writes the file.

    The name is kept because ``api.py`` and ``monitor.py`` call it at import.
    """
    global _leftover_ini_reported
    if _leftover_ini_reported:
        return
    path = os.path.join(CONFIG_DIR, "config.ini")
    try:
        leftover = os.path.isfile(path)
    except OSError:
        leftover = False
    if leftover:
        _leftover_ini_reported = True
        app_logger.warning(
            f"{path} was never imported and is being ignored: this version no longer "
            "reads config.ini. To carry its settings over, start CLU 7.3 once (it "
            "imports the file and renames it config.ini.migrated), then upgrade again. "
            "Otherwise re-enter them in Settings and delete the file."
        )


def get_watch_dir() -> str:
    """Return the configured WATCH path from user_preferences ('' if unset)."""
    try:
        from core.database import get_user_preference
        return (get_user_preference("watch", default="") or "").strip()
    except Exception:
        return ""


def get_target_dir() -> str:
    """Return the configured TARGET path from user_preferences ('' if unset)."""
    try:
        from core.database import get_user_preference
        return (get_user_preference("target", default="") or "").strip()
    except Exception:
        return ""


PREF_AUTO_METADATA_ON_MOVE = "auto_metadata_on_move"


def is_auto_metadata_on_move_enabled() -> bool:
    """True when files moved into a folder with a cvinfo should be auto-tagged.

    Gates the post-move ``auto_fetch_*_metadata`` fetchers in app.py. Some
    users curate their own ComicInfo.xml and do not want a move to trigger a
    provider lookup, so this is a user-facing switch -- but it has always been
    on, so the default stays True and an unreadable preference store fails
    open rather than silently disabling the feature.
    """
    try:
        from core.database import get_user_preference
        return bool(get_user_preference(PREF_AUTO_METADATA_ON_MOVE, default=True))
    except Exception:
        return True


PREF_DOWNLOAD_PACKS = "download_packs"


def is_download_packs_enabled() -> bool:
    """True when automated downloads may take a pack for one missing issue.

    A pack is a download holding more than one issue: a range post
    ("Batman #1-50") or a range part of a split post ("#1-15"). The nightly
    sweep, "Check for Missing Issues" and the Usenet/DC++ auto-download used to
    fall back to one whenever no single issue was found, so missing #15 could
    fetch 80 issues. Default OFF, and an unreadable preference store falls back
    to that default. Manual grabs are never gated: picking a pack by hand is a
    decision the user made.
    """
    try:
        from core.database import get_user_preference
        return bool(get_user_preference(PREF_DOWNLOAD_PACKS, default=False))
    except Exception:
        return False


def apply_session_cookie_policy(app):
    """Set the session cookie's attributes on *app*.

    ``SameSite=Lax`` keeps the cookie off cross-site POSTs (the same-origin
    check in ``core/request_origin.py`` is the other half), and ``HttpOnly``
    keeps it out of page scripts (Flask's default, stated so it cannot drift).
    ``Secure`` is deliberately left unset: most installs are plain HTTP on a
    LAN, and a Secure cookie would never be sent back, locking them out.

    Called from ``load_flask_config`` (the real app) and when ``auth_bp`` is
    registered (``routes/auth.py``), so the test app gets it too.
    """
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True


def load_flask_config(app, logger=None):
    """Populate a Flask app's config from the stored settings."""
    from core.settings import get_setting, get_bool, get_int, invalidate

    apply_session_cookie_policy(app)

    # A save just happened, or the DB was not ready last time: read fresh.
    invalidate()
    load_config()

    if logger:
        logger.info("Loading settings...")

    # Load FLASK_ENV from environment variable (for production/development mode)
    app.config["FLASK_ENV"] = os.environ.get("FLASK_ENV", "development")
    app.config["ENV"] = app.config["FLASK_ENV"]  # Also set ENV for compatibility

    # WATCH and TARGET live in user_preferences (single source of truth).
    app.config["WATCH"] = get_watch_dir() or "/downloads/temp"
    app.config["TARGET"] = get_target_dir() or "/downloads/processed"
    app.config["IGNORED_TERMS"] = get_setting("IGNORED_TERMS")
    app.config["IGNORED_FILES"] = get_setting("IGNORED_FILES")
    app.config["IGNORED_EXTENSIONS"] = get_setting("IGNORED_EXTENSIONS")
    app.config["AUTOCONVERT"] = get_bool("AUTOCONVERT")
    app.config["READ_SUBDIRECTORIES"] = get_bool("READ_SUBDIRECTORIES")
    app.config["CONVERT_SUBDIRECTORIES"] = get_bool("CONVERT_SUBDIRECTORIES")
    app.config["XML_YEAR"] = get_bool("XML_YEAR")
    app.config["XML_MARKDOWN"] = get_bool("XML_MARKDOWN")
    app.config["XML_LIST"] = get_bool("XML_LIST")
    app.config["MOVE_DIRECTORY"] = get_bool("MOVE_DIRECTORY")
    app.config["SKIPPED_FILES"] = get_setting("SKIPPED_FILES")
    app.config["DELETED_FILES"] = get_setting("DELETED_FILES")
    app.config["HEADERS"] = get_setting("HEADERS")
    app.config["DOWNLOAD_PROVIDER_PRIORITY"] = get_setting("DOWNLOAD_PROVIDER_PRIORITY")
    app.config["PIXELDRAIN_API_KEY"] = get_setting("PIXELDRAIN_API_KEY")
    app.config["GCD_METADATA_LANGUAGES"] = get_setting("GCD_METADATA_LANGUAGES")
    app.config["COMICVINE_API_KEY"] = ""
    app.config["METRON_USERNAME"] = ""
    app.config["METRON_PASSWORD"] = ""
    app.config["METRON_API_TOKEN"] = ""

    # Load API credentials from DB (provider_credentials table)
    try:
        from core.database import get_provider_credentials
        metron_creds = get_provider_credentials('metron')
        if metron_creds:
            if metron_creds.get('username'):
                app.config["METRON_USERNAME"] = metron_creds['username']
            if metron_creds.get('password'):
                app.config["METRON_PASSWORD"] = metron_creds['password']
            if metron_creds.get('api_token'):
                app.config["METRON_API_TOKEN"] = metron_creds['api_token']

        comicvine_creds = get_provider_credentials('comicvine')
        if comicvine_creds:
            if comicvine_creds.get('api_key'):
                app.config["COMICVINE_API_KEY"] = comicvine_creds['api_key']
    except Exception:
        pass  # DB not initialized yet

    from core.database import get_user_preference
    app.config["ENABLE_CUSTOM_RENAME"] = get_bool("ENABLE_CUSTOM_RENAME")
    # {year} -> {volume_year}: this copy is taken before app.py's one-time
    # migration runs, and nothing re-normalises it on save. Same literal
    # rewrite as cbz_ops.rename.normalise_rename_pattern (not imported here:
    # core.config sits below cbz_ops in the import graph).
    app.config["CUSTOM_RENAME_PATTERN"] = (
        get_setting("CUSTOM_RENAME_PATTERN") or ''
    ).replace("{year}", "{volume_year}")
    app.config["SMART_RENAME_PREVIEW_ENABLED"] = bool(get_user_preference('smart_rename_preview_enabled', default=True))
    app.config["SMART_RENAME_RECURSIVE"] = bool(get_user_preference('smart_rename_recursive', default=True))
    app.config["SMART_RENAME_EXCLUDE_TERMS"] = get_user_preference('smart_rename_exclude_terms', default='Annual,Special') or ''
    app.config["ENABLE_AUTO_RENAME"] = get_bool("ENABLE_AUTO_RENAME")
    app.config["ENABLE_AUTO_MOVE"] = get_bool("ENABLE_AUTO_MOVE")
    app.config["CUSTOM_MOVE_PATTERN"] = get_setting("CUSTOM_MOVE_PATTERN")
    app.config["ENABLE_DEBUG_LOGGING"] = get_bool("ENABLE_DEBUG_LOGGING")
    app.config["CACHE_DIR"] = CACHE_DIR
    app.config["TRASH_ENABLED"] = get_bool("TRASH_ENABLED")
    app.config["TRASH_DIR"] = get_setting("TRASH_DIR")
    app.config["TRASH_MAX_SIZE_MB"] = get_int("TRASH_MAX_SIZE_MB")
    app.config["BOOTSTRAP_THEME"] = get_setting("BOOTSTRAP_THEME") or 'default'

    # Session / auth gate (env-var based, optional)
    #
    # This runs again on every settings save, so it must never rotate a key
    # that is already in use: cookies signed with the old one stop verifying
    # and everyone is logged out mid-save, with no replacement cookie sent
    # (the session isn't modified during those requests). Only the very first
    # call -- before the DB exists -- mints a provisional key, which app.py
    # then upgrades to the persisted one. An explicit SECRET_KEY always wins.
    import secrets
    env_secret = os.environ.get("SECRET_KEY")
    if env_secret:
        app.secret_key = env_secret
    elif not getattr(app, "secret_key", None):
        app.secret_key = secrets.token_hex(32)
    app.config["CLU_USERNAME"] = os.environ.get("CLU_USERNAME", "")
    app.config["CLU_PASSWORD"] = os.environ.get("CLU_PASSWORD", "")

    if logger:
        logger.info(f"Watching: {app.config['WATCH']}")


# Initial import attempt. It no-ops until the database exists; app.py,
# monitor.py and api.py call load_config() again after init_db().
load_config()
