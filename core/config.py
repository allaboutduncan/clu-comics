"""Application configuration.

Settings live in the ``user_preferences`` table (see ``core.settings``).
``config.ini`` is deprecated: ``migrate_ini_to_user_preferences`` imports it
once and renames it ``config.ini.migrated``; nothing reads or writes it after
that. The import is removed in the release after it ships.

``config`` is kept as a **read-only compatibility view** over the preferences,
because ``api.py`` (which must not be edited) and a long tail of callers read
``config.get("SETTINGS", KEY)``. New code should call ``core.settings``.
"""
import configparser
import os

from core.app_logging import app_logger

# Use /config volume if it exists (Docker), otherwise use current directory
CONFIG_DIR = os.environ.get('CONFIG_DIR', '/config' if os.path.exists('/config') else os.getcwd())
# Ensure config directory exists
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.ini")  # legacy; import source only

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


def get_data_dir() -> str:
    """The library mount."""
    return DATA_DIR


_MISSING = object()


def _stringify(value):
    """Render a typed setting the way the ini did ('True'/'False', digits)."""
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def _lookup(key):
    """String value of a legacy ini key, or _MISSING if it is not a setting."""
    from core.settings import SETTINGS_SCHEMA, get_setting
    if key in SETTINGS_SCHEMA:
        return _stringify(get_setting(key))
    if key == "WATCH":
        return get_watch_dir() or "/downloads/temp"
    if key == "TARGET":
        return get_target_dir() or "/downloads/processed"
    if key == "CACHE_DIR":
        return get_cache_dir()
    if key == "DATA_DIR":
        return get_data_dir()
    if key == "COMICVINE_API_KEY":
        try:
            from core.database import get_provider_credentials
            return (get_provider_credentials("comicvine") or {}).get("api_key") or ""
        except Exception:
            return ""
    rec = {"REC_ENABLED": ("rec_enabled", True), "REC_PROVIDER": ("rec_provider", "gemini"),
           "REC_API_KEY": ("rec_api_key", ""), "REC_MODEL": ("rec_model", "gemini-2.0-flash")}
    if key in rec:
        try:
            from core.database import get_user_preference
            return _stringify(get_user_preference(rec[key][0], default=rec[key][1]))
        except Exception:
            return _stringify(rec[key][1])
    return _MISSING


class _SettingsSection:
    """``config["SETTINGS"]``: dict-like, read-only, string-valued."""

    def get(self, key, fallback=None):
        value = _lookup(key)
        return fallback if value is _MISSING else value

    def __getitem__(self, key):
        value = _lookup(key)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __contains__(self, key):
        return _lookup(key) is not _MISSING

    def __setitem__(self, key, value):
        app_logger.warning(f"Ignoring write to retired config.ini setting {key}; use core.settings.set_setting")


class _PrefsConfig:
    """Read-only stand-in for the old RawConfigParser, backed by preferences."""

    def __init__(self):
        self._section = _SettingsSection()

    def _value(self, section, option):
        return _lookup(option) if section == "SETTINGS" else _MISSING

    def get(self, section, option, fallback=_MISSING, **_):
        value = self._value(section, option)
        if value is _MISSING:
            if fallback is _MISSING:
                raise configparser.NoOptionError(option, section)
            return fallback
        return value

    def getboolean(self, section, option, fallback=_MISSING, **_):
        value = self._value(section, option)
        if value is _MISSING:
            if fallback is _MISSING:
                raise configparser.NoOptionError(option, section)
            return fallback
        return value.strip().lower() in ("true", "1", "yes", "on")

    def getint(self, section, option, fallback=_MISSING, **_):
        value = self._value(section, option)
        try:
            return int(value)
        except (TypeError, ValueError):
            if fallback is _MISSING:
                raise configparser.NoOptionError(option, section)
            return fallback

    def has_section(self, section):
        return section == "SETTINGS"

    def has_option(self, section, option):
        return self._value(section, option) is not _MISSING

    def __getitem__(self, section):
        if section != "SETTINGS":
            raise KeyError(section)
        return self._section

    def __contains__(self, section):
        return section == "SETTINGS"

    def set(self, section, option, value=None):
        app_logger.warning(f"Ignoring write to retired config.ini setting {option}; use core.settings.set_setting")

    def remove_option(self, section, option):
        return False


config = _PrefsConfig()


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


def load_config():
    """Run the one-time config.ini import if it is still owed.

    Idempotent and cheap once done. Callers that import this module before the
    database exists (``api.py``, ``missing.py``, ``cbz_ops/*``) call it again
    later; the import simply waits until the preference table is reachable.
    """
    migrate_ini_to_user_preferences()


PREF_INI_MIGRATED = "config_ini_migrated_to_prefs"

# ini keys that map to a differently-shaped preference than SETTINGS_SCHEMA.
_INI_TO_PREF = (
    ("WATCH", "watch", "file_processing"),
    ("TARGET", "target", "file_processing"),
    ("TIMEZONE", "timezone", "personalization"),
)


def _db_ready():
    """True when the preference table can be read (init_db has run)."""
    try:
        from core.database import db_conn
        with db_conn() as conn:
            if not conn:
                return False
            conn.execute("SELECT 1 FROM user_preferences LIMIT 1")
            return True
    except Exception:
        return False


def migrate_ini_to_user_preferences(ini_path=None):
    """One-time import of every config.ini setting into user_preferences.

    Returns the number of preferences written, or None when nothing was done
    (already migrated, database not ready, or a failure). The flag is set --
    and the file renamed to ``config.ini.migrated`` -- only after every write
    succeeded, so a failed run is retried on the next start.

    A preference that already holds a value is never overwritten: the database
    is authoritative for the keys that were migrated piecemeal earlier
    (custom rename, theme, headers, watch/target...), and an ini copy of those
    is stale by construction. Nothing reads or writes the ini after this.
    """
    from core.settings import SETTINGS_SCHEMA, _coerce

    path = ini_path or CONFIG_FILE
    if not _db_ready():
        app_logger.debug("Skipping config.ini import (database not ready)")
        return None
    from core.database import get_user_preference, set_user_preference

    try:
        if get_user_preference(PREF_INI_MIGRATED, default=False):
            return None
        if not os.path.isfile(path):
            set_user_preference(PREF_INI_MIGRATED, True, category="migration")
            return None

        parser = configparser.RawConfigParser()
        parser.optionxform = str
        parser.read(path)
        section = parser["SETTINGS"] if "SETTINGS" in parser else {}

        written = 0
        for key, spec in SETTINGS_SCHEMA.items():
            if key not in section:
                continue
            if get_user_preference(spec.pref, default=None) is not None:
                continue
            value = _coerce(section[key], spec.type, spec.default)
            if key == "CUSTOM_RENAME_PATTERN":
                # Same literal rewrite as cbz_ops.rename.normalise_rename_pattern.
                value = value.replace("{year}", "{volume_year}")
            if not set_user_preference(spec.pref, value, category=spec.category):
                raise RuntimeError(f"could not store {spec.pref}")
            written += 1

        for key, pref, category in _INI_TO_PREF:
            raw = (section.get(key, "") or "").strip() if section else ""
            if raw and get_user_preference(pref, default=None) in (None, ""):
                if not set_user_preference(pref, raw, category=category):
                    raise RuntimeError(f"could not store {pref}")
                written += 1

        # ComicVine's key lives with the other provider credentials.
        cv_key = (section.get("COMICVINE_API_KEY", "") or "").strip() if section else ""
        if cv_key:
            from core.database import get_provider_credentials, save_provider_credentials
            existing = get_provider_credentials("comicvine") or {}
            if not existing.get("api_key"):
                save_provider_credentials("comicvine", {**existing, "api_key": cv_key})
                written += 1

        # Recommendation settings (older installs only; rec_* are the live prefs).
        if section and "REC_ENABLED" in section and get_user_preference("rec_enabled", default=None) is None:
            for pref, ini_key, default in (
                ("rec_enabled", "REC_ENABLED", "True"),
                ("rec_provider", "REC_PROVIDER", "gemini"),
                ("rec_api_key", "REC_API_KEY", ""),
                ("rec_model", "REC_MODEL", "gemini-2.0-flash"),
            ):
                raw = section.get(ini_key, default)
                val = (raw == "True") if pref == "rec_enabled" else raw
                set_user_preference(pref, val, category="personalization")
                written += 1

        ini_cache = (section.get("CACHE_DIR", "") or "").strip().rstrip("/") if section else ""
        if ini_cache and ini_cache != CACHE_DIR:
            app_logger.warning(
                f"config.ini set CACHE_DIR={ini_cache}, which is no longer configurable: the cache "
                f"is always {CACHE_DIR}. Mount your cache folder there (see docker-compose.yaml); "
                "thumbnails under the old path will be rebuilt."
            )

        set_user_preference(PREF_INI_MIGRATED, True, category="migration")
        try:
            os.replace(path, path + ".migrated")
        except OSError as e:
            app_logger.warning(f"Imported config.ini but could not rename it: {e}")
        app_logger.info(f"Imported {written} setting(s) from config.ini into user_preferences")
        return written
    except Exception as e:
        app_logger.error(f"config.ini import failed (will retry next start): {e}")
        return None


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


def load_flask_config(app, logger=None):
    """Populate a Flask app's config from the stored settings."""
    from core.settings import get_setting, get_bool, get_int, invalidate

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
