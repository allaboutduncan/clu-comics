import configparser
import threading
import os
import time
from core.app_logging import app_logger

# Use /config volume if it exists (Docker), otherwise use current directory
CONFIG_DIR = os.environ.get('CONFIG_DIR', '/config' if os.path.exists('/config') else os.getcwd())
# Ensure config directory exists
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.ini")

# Use RawConfigParser to allow special characters like % in values (no interpolation)
config = configparser.RawConfigParser()
config.optionxform = str  # Preserve case sensitivity

def write_config():
    """Writes the current in-memory config object to config.ini."""
    config.optionxform = str  # Preserve case sensitivity
    with open(CONFIG_FILE, "w") as configfile:
        config.write(configfile)

def load_config():
    """
    Loads or (if missing) creates the config file, ensuring
    that the [SETTINGS] section exists.
    """
    # Log the config file location
    app_logger.debug(f"📁 Config file location: {CONFIG_FILE}")

    # Define default settings with all required keys.
    # NOTE: WATCH and TARGET intentionally live in user_preferences (DB), not config.ini.
    # See migrate_watch_target_to_user_preferences() and get_watch_dir()/get_target_dir().
    default_settings = {
        "IGNORED_TERMS": "Annual",
        "IGNORED_FILES": "cover.jpg,cvinfo,.DS_Store",
        "IGNORED_EXTENSIONS": ".crdownload,.torrent,.tmp,.mega,.rar,.bak,.zip",
        "AUTOCONVERT": "False",
        "READ_SUBDIRECTORIES": "False",
        "CONVERT_SUBDIRECTORIES": "False",
        "XML_YEAR": "False",
        "XML_MARKDOWN": "False",
        "XML_LIST": "True",
        "MOVE_DIRECTORY": "False",
        "CONSOLIDATE_DIRECTORIES": "False",
        "AUTO_UNPACK": "False",
        "AUTO_RENAME_MONITOR": "True",
        "SKIPPED_FILES": ".xml",
        "DELETED_FILES": ".nfo,.sfv,.db,.DS_Store",
        "DOWNLOAD_PROVIDER_PRIORITY": "pixeldrain,download_now,mega",
        "PIXELDRAIN_API_KEY": "",
        "GCD_METADATA_LANGUAGES": "en",
        "COMICVINE_API_KEY": "",
        "ENABLE_CUSTOM_RENAME": "False",
        "CUSTOM_RENAME_PATTERN": "",
        "ENABLE_AUTO_RENAME": "False",
        "ENABLE_AUTO_MOVE": "False",
        "CUSTOM_MOVE_PATTERN": "{publisher}/{series_name}/v{start_year}",
        "ENABLE_DEBUG_LOGGING": "False",
        "CACHE_DIR": "/cache",
        "BOOTSTRAP_THEME": "default",
        "ENABLE_METADATA_SCAN": "True",
        "METADATA_SCAN_THREADS": "2",
        "TRASH_ENABLED": "True",
        "TRASH_DIR": "",
        "TRASH_MAX_SIZE_MB": "1024",
        "PUBLICATION_TYPES": "annual,quarterly",
        "VARIANT_TYPES": "annual,quarterly,tpB,oneshot,one-shot,o.s.,os,trade paperback,trade-paperback,omni,omnibus,omb,hardcover,deluxe,prestige,gallery",
        "SEQUEL_KEYWORDS": "season,volume,book,part,chapter"
    }

    if not os.path.exists(CONFIG_FILE):
        # Create a default config.ini if none exists
        config["SETTINGS"] = default_settings
        write_config()
    else:
        # Load existing config
        config.read(CONFIG_FILE)

        # Ensure the SETTINGS section exists
        if "SETTINGS" not in config:
            config["SETTINGS"] = {}

        # Migrate/add any missing keys with defaults (preserves existing values)
        settings_updated = False
        missing_keys = []
        for key, default_value in default_settings.items():
            if key not in config["SETTINGS"]:
                config["SETTINGS"][key] = default_value
                settings_updated = True
                missing_keys.append(key)

        # Save config if new keys were added
        if settings_updated:
            app_logger.info(f"🔄 Migrated {len(missing_keys)} new config keys: {', '.join(missing_keys)}")
            write_config()
        else:
            app_logger.debug("✅ Config file loaded successfully (no migration needed)")

    # One-time migration: move WATCH/TARGET from config.ini into user_preferences.
    # Runs after the load branches so both new and existing installs are handled.
    migrate_watch_target_to_user_preferences()

    # Always mirror the user_preferences values into the in-memory config object
    # (NOT to disk) so any legacy reader using ``config.get("SETTINGS", "TARGET")``
    # picks up the current value. user_preferences remains the source of truth.
    _mirror_watch_target_into_memory_config()


def _mirror_watch_target_into_memory_config():
    """Copy WATCH/TARGET from user_preferences into the in-memory ``config`` object.

    The values are NOT written to ``config.ini`` (write_config is not called).
    """
    try:
        from core.database import get_user_preference
    except Exception:
        return
    try:
        if "SETTINGS" not in config:
            config["SETTINGS"] = {}
        watch_val = (get_user_preference("watch", default="") or "").strip()
        target_val = (get_user_preference("target", default="") or "").strip()
        if watch_val:
            config["SETTINGS"]["WATCH"] = watch_val
        if target_val:
            config["SETTINGS"]["TARGET"] = target_val
    except Exception as e:
        app_logger.debug(f"In-memory WATCH/TARGET mirror skipped: {e}")


def migrate_watch_target_to_user_preferences():
    """Move legacy WATCH/TARGET from config.ini into the user_preferences table.

    Runs at most once per install (guarded by the `watch_target_migrated_to_prefs`
    flag in user_preferences). After migration the keys are stripped from
    config.ini so user_preferences is the single source of truth.
    """
    try:
        from core.database import get_user_preference, set_user_preference
    except Exception as e:
        app_logger.debug(f"Skipping WATCH/TARGET migration (DB not ready): {e}")
        return

    try:
        if get_user_preference("watch_target_migrated_to_prefs", default=False):
            return

        legacy_watch = config["SETTINGS"].get("WATCH") if "SETTINGS" in config else None
        legacy_target = config["SETTINGS"].get("TARGET") if "SETTINGS" in config else None

        if legacy_watch and not get_user_preference("watch"):
            set_user_preference("watch", legacy_watch.strip(), category="file_processing")
            app_logger.info(f"Migrated WATCH ({legacy_watch}) from config.ini to user_preferences")

        if legacy_target and not get_user_preference("target"):
            set_user_preference("target", legacy_target.strip(), category="file_processing")
            app_logger.info(f"Migrated TARGET ({legacy_target}) from config.ini to user_preferences")

        # Strip keys from config.ini so future reads can't desync.
        rewrite = False
        if "SETTINGS" in config:
            if "WATCH" in config["SETTINGS"]:
                config.remove_option("SETTINGS", "WATCH")
                rewrite = True
            if "TARGET" in config["SETTINGS"]:
                config.remove_option("SETTINGS", "TARGET")
                rewrite = True
        if rewrite:
            write_config()

        set_user_preference("watch_target_migrated_to_prefs", True, category="file_processing")
    except Exception as e:
        app_logger.error(f"WATCH/TARGET migration failed: {e}")


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


def load_flask_config(app, logger=None):
    """
    Helper function to populate a Flask app's config with
    the latest [SETTINGS] from config.ini.
    """
    # Ensure we have the most up-to-date config in memory
    load_config()

    if logger:
        logger.info("Loading config file values...")

    # Load FLASK_ENV from environment variable (for production/development mode)
    app.config["FLASK_ENV"] = os.environ.get("FLASK_ENV", "development")
    app.config["ENV"] = app.config["FLASK_ENV"]  # Also set ENV for compatibility

    # **Ensure SETTINGS is a dictionary before accessing**
    settings = config["SETTINGS"] if "SETTINGS" in config else {}

    # WATCH and TARGET live in user_preferences (single source of truth).
    app.config["WATCH"] = get_watch_dir() or "/downloads/temp"
    app.config["TARGET"] = get_target_dir() or "/downloads/processed"
    app.config["IGNORED_TERMS"] = settings.get("IGNORED_TERMS", "")
    app.config["IGNORED_FILES"] = settings.get("IGNORED_FILES", "")
    app.config["IGNORED_EXTENSIONS"] = settings.get("IGNORED_EXTENSIONS", "")
    app.config["AUTOCONVERT"] = config.getboolean("SETTINGS", "AUTOCONVERT", fallback=False)
    app.config["READ_SUBDIRECTORIES"] = config.getboolean("SETTINGS", "READ_SUBDIRECTORIES", fallback=False)
    app.config["CONVERT_SUBDIRECTORIES"] = config.getboolean("SETTINGS", "CONVERT_SUBDIRECTORIES", fallback=False)
    app.config["XML_YEAR"] = config.getboolean("SETTINGS", "XML_YEAR", fallback=False)
    app.config["XML_MARKDOWN"] = config.getboolean("SETTINGS", "XML_MARKDOWN", fallback=False)
    app.config["XML_LIST"] = config.getboolean("SETTINGS", "XML_LIST", fallback=False)
    app.config["MOVE_DIRECTORY"] = config.getboolean("SETTINGS", "MOVE_DIRECTORY", fallback=False)
    app.config["AUTO_UNPACK"] = config.getboolean("SETTINGS", "AUTO_UNPACK", fallback=False)
    app.config["SKIPPED_FILES"] = settings.get("SKIPPED_FILES", "")
    app.config["DELETED_FILES"] = settings.get("DELETED_FILES", "")
    app.config["HEADERS"] = settings.get("HEADERS", "")
    app.config["DOWNLOAD_PROVIDER_PRIORITY"] = settings.get("DOWNLOAD_PROVIDER_PRIORITY", "pixeldrain,download_now,mega")
    app.config["PIXELDRAIN_API_KEY"] = settings.get("PIXELDRAIN_API_KEY", "")
    app.config["GCD_METADATA_LANGUAGES"] = settings.get("GCD_METADATA_LANGUAGES", "en")
    app.config["COMICVINE_API_KEY"] = settings.get("COMICVINE_API_KEY", "")
    app.config["METRON_USERNAME"] = ""
    app.config["METRON_PASSWORD"] = ""

    # Load API credentials from DB (provider_credentials table)
    try:
        from core.database import get_provider_credentials
        metron_creds = get_provider_credentials('metron')
        if metron_creds:
            if metron_creds.get('username'):
                app.config["METRON_USERNAME"] = metron_creds['username']
            if metron_creds.get('password'):
                app.config["METRON_PASSWORD"] = metron_creds['password']

        comicvine_creds = get_provider_credentials('comicvine')
        if comicvine_creds:
            if comicvine_creds.get('api_key'):
                app.config["COMICVINE_API_KEY"] = comicvine_creds['api_key']
    except Exception:
        pass  # DB not initialized yet, config.ini values remain

    from core.database import get_user_preference
    app.config["ENABLE_CUSTOM_RENAME"] = bool(get_user_preference('enable_custom_rename', default=False))
    app.config["CUSTOM_RENAME_PATTERN"] = get_user_preference('custom_rename_pattern', default='') or ''
    app.config["SMART_RENAME_PREVIEW_ENABLED"] = bool(get_user_preference('smart_rename_preview_enabled', default=True))
    app.config["SMART_RENAME_RECURSIVE"] = bool(get_user_preference('smart_rename_recursive', default=True))
    app.config["ENABLE_AUTO_RENAME"] = config.getboolean("SETTINGS", "ENABLE_AUTO_RENAME", fallback=False)
    app.config["ENABLE_AUTO_MOVE"] = config.getboolean("SETTINGS", "ENABLE_AUTO_MOVE", fallback=False)
    app.config["CUSTOM_MOVE_PATTERN"] = settings.get("CUSTOM_MOVE_PATTERN", "{publisher}/{series_name}/v{start_year}")
    app.config["ENABLE_DEBUG_LOGGING"] = config.getboolean("SETTINGS", "ENABLE_DEBUG_LOGGING", fallback=False)
    app.config["CACHE_DIR"] = settings.get("CACHE_DIR", "/cache")
    app.config["TRASH_ENABLED"] = config.getboolean("SETTINGS", "TRASH_ENABLED", fallback=True)
    app.config["TRASH_DIR"] = settings.get("TRASH_DIR", "")
    app.config["TRASH_MAX_SIZE_MB"] = config.getint("SETTINGS", "TRASH_MAX_SIZE_MB", fallback=1024)
    app.config["BOOTSTRAP_THEME"] = get_user_preference('bootstrap_theme', default='default') or 'default'

    # Session / auth gate (env-var based, optional)
    import secrets
    app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
    app.config["CLU_USERNAME"] = os.environ.get("CLU_USERNAME", "")
    app.config["CLU_PASSWORD"] = os.environ.get("CLU_PASSWORD", "")

    if logger:
        logger.info(f"Watching: {app.config['WATCH']}")

def monitor_config(interval=5):
    """
    Background thread to watch config.ini for changes.
    If modified, automatically reloads the in-memory 'config' object.
    """
    last_mtime = os.path.getmtime(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else None

    while True:
        time.sleep(interval)
        try:
            current_mtime = os.path.getmtime(CONFIG_FILE)
            if last_mtime is None or current_mtime != last_mtime:  # File is new or changed
                load_config()
                last_mtime = current_mtime
                app_logger.debug("Config file reloaded at: ".format(time.ctime(last_mtime)))
        except FileNotFoundError:
            app_logger.info(f"Warning: {CONFIG_FILE} not found.")
            last_mtime = None  # Reset because file may appear later

# Start monitoring config.ini in the background
thread = threading.Thread(target=monitor_config, args=(5,), daemon=True)
thread.start()

# Initial config load
load_config()
