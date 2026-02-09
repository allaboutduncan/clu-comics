import configparser
import threading
import os
import time
from app_logging import app_logger

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

    # Define default settings with all required keys
    default_settings = {
        "WATCH": "/downloads/temp",
        "TARGET": "/downloads/processed",
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
        "AUTO_UNPACK": "False",
        "SKIPPED_FILES": ".xml",
        "DELETED_FILES": ".nfo,.sfv,.db,.DS_Store",
        "HEADERS": "",
        "DOWNLOAD_PROVIDER_PRIORITY": "pixeldrain,download_now,mega",
        "PIXELDRAIN_API_KEY": "",
        "GCD_METADATA_LANGUAGES": "en",
        "COMICVINE_API_KEY": "",
        "METRON_USERNAME": "",
        "METRON_PASSWORD": "",
        "ENABLE_CUSTOM_RENAME": "False",
        "CUSTOM_RENAME_PATTERN": "",
        "ENABLE_AUTO_RENAME": "False",
        "ENABLE_AUTO_MOVE": "False",
        "CUSTOM_MOVE_PATTERN": "{publisher}/{series_name}/v{start_year}",
        "ENABLE_DEBUG_LOGGING": "False",
        "CACHE_DIR": "/cache",
        "BOOTSTRAP_THEME": "default",
        "TIMEZONE": "UTC",
        "ENABLE_METADATA_SCAN": "True",
        "METADATA_SCAN_THREADS": "2"
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

    # Populate Flask app.config safely
    app.config["WATCH"] = settings.get("WATCH", "/downloads/temp")
    app.config["TARGET"] = settings.get("TARGET", "/downloads/processed")
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
    app.config["METRON_USERNAME"] = settings.get("METRON_USERNAME", "")
    app.config["METRON_PASSWORD"] = settings.get("METRON_PASSWORD", "")
    app.config["ENABLE_CUSTOM_RENAME"] = config.getboolean("SETTINGS", "ENABLE_CUSTOM_RENAME", fallback=False)
    app.config["CUSTOM_RENAME_PATTERN"] = settings.get("CUSTOM_RENAME_PATTERN", "")
    app.config["ENABLE_AUTO_RENAME"] = config.getboolean("SETTINGS", "ENABLE_AUTO_RENAME", fallback=False)
    app.config["ENABLE_AUTO_MOVE"] = config.getboolean("SETTINGS", "ENABLE_AUTO_MOVE", fallback=False)
    app.config["CUSTOM_MOVE_PATTERN"] = settings.get("CUSTOM_MOVE_PATTERN", "{publisher}/{series_name}/v{start_year}")
    app.config["ENABLE_DEBUG_LOGGING"] = config.getboolean("SETTINGS", "ENABLE_DEBUG_LOGGING", fallback=False)
    app.config["CACHE_DIR"] = settings.get("CACHE_DIR", "/cache")
    app.config["BOOTSTRAP_THEME"] = settings.get("BOOTSTRAP_THEME", "default")

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
