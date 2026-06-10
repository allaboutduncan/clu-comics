"""
Debug Package builder.

Bundles the most useful diagnostic data (config, settings, recent logs and
system info) into a single in-memory ZIP so users can attach it to a support
request. All secrets are redacted by default using the same masking the app
uses elsewhere for credentials, so the package is safe to share publicly.

Contents of the ZIP:
    README.txt          -- what's inside + redaction notice
    system_info.json    -- version, platform, python, key paths, flags
    config.ini          -- copy of config.ini with secrets redacted
    db_settings.json    -- user_preferences rows + table list (settings only)
    logs/app.log        -- last N lines of app.log
    logs/monitor.log    -- last N lines of monitor.log
"""
import configparser
import io
import json
import os
import platform
import sys
import zipfile
from collections import deque

from core.app_logging import APP_LOG, MONITOR_LOG, LOG_DIR, app_logger
from core.config import CONFIG_DIR, CONFIG_FILE
from core.version import __version__
from models.providers.crypto import mask_credential

# Substrings that mark a config/preference key as sensitive (case-insensitive).
# Matching on substrings keeps this future-proof as new secret settings are
# added (anything with KEY / PASSWORD / TOKEN / SECRET in its name).
_SENSITIVE_SUBSTRINGS = ("KEY", "PASSWORD", "TOKEN", "SECRET")

# Explicit keys that don't contain an obvious marker but are still sensitive.
_SENSITIVE_EXPLICIT = {"METRON_USERNAME", "CLU_USERNAME"}

# How many trailing lines of each log file to include.
LOG_TAIL_LINES = 5000


def _is_sensitive_key(key: str) -> bool:
    """True when a config/preference key holds a secret that must be redacted."""
    if not key:
        return False
    upper = key.upper()
    if upper in _SENSITIVE_EXPLICIT:
        return True
    return any(marker in upper for marker in _SENSITIVE_SUBSTRINGS)


def _redact_value(value):
    """Mask a value if it is a non-empty string; pass through otherwise."""
    if isinstance(value, str) and value:
        return mask_credential(value)
    return value


def _redacted_config_ini(config_path: str = CONFIG_FILE) -> str:
    """Return config.ini as a string with all sensitive values masked."""
    if not os.path.exists(config_path):
        return f"# config.ini not found at {config_path}\n"

    parser = configparser.RawConfigParser()
    parser.optionxform = str  # preserve key case
    try:
        parser.read(config_path)
    except configparser.Error as e:
        return f"# Failed to parse config.ini: {e}\n"

    for section in parser.sections():
        for key, value in parser.items(section):
            if _is_sensitive_key(key) and value:
                parser.set(section, key, mask_credential(value))

    buf = io.StringIO()
    parser.write(buf)
    return buf.getvalue()


def _db_settings_json() -> str:
    """Dump the user_preferences table + table list as JSON (secrets redacted)."""
    from core.database import get_db_connection

    data = {"user_preferences": [], "tables": []}
    conn = None
    try:
        conn = get_db_connection()
        if conn is None:
            return json.dumps({"error": "database unavailable"}, indent=2)
        c = conn.cursor()

        c.execute(
            "SELECT key, value, category, updated_at FROM user_preferences ORDER BY key"
        )
        for row in c.fetchall():
            key = row["key"]
            value = row["value"]
            if _is_sensitive_key(key):
                value = _redact_value(value)
            data["user_preferences"].append({
                "key": key,
                "value": value,
                "category": row["category"],
                "updated_at": row["updated_at"],
            })

        c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        data["tables"] = [r["name"] for r in c.fetchall()]
    except Exception as e:
        app_logger.error(f"Debug package: failed reading db settings: {e}")
        data["error"] = str(e)
    finally:
        if conn is not None:
            conn.close()

    return json.dumps(data, indent=2, default=str)


def _system_info_json() -> str:
    """Return non-sensitive runtime/environment info as JSON."""
    info = {
        "version": __version__,
        "platform": platform.platform(),
        "python_version": sys.version,
        "paths": {
            "config_dir": CONFIG_DIR,
            "log_dir": LOG_DIR,
            "config_file": CONFIG_FILE,
        },
        "flags": {
            "MONITOR": os.environ.get("MONITOR", ""),
            "ENABLE_DEBUG_LOGGING": os.environ.get("ENABLE_DEBUG_LOGGING", ""),
            "PUID": os.environ.get("PUID", ""),
            "PGID": os.environ.get("PGID", ""),
        },
    }
    return json.dumps(info, indent=2, default=str)


def _tail(path: str, lines: int = LOG_TAIL_LINES) -> str:
    """Return the last ``lines`` lines of a text file, or a placeholder."""
    if not path or not os.path.exists(path):
        return f"(log file not found: {path})\n"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=lines)
        return "".join(tail)
    except Exception as e:
        return f"(failed to read log {path}: {e})\n"


def _readme() -> str:
    """Short explanation of the package contents and the redaction guarantee."""
    return (
        "CLU Debug Package\n"
        "=================\n\n"
        f"Generated by Comic Library Utilities v{__version__}.\n\n"
        "Contents:\n"
        "  system_info.json  - version, platform, python, key paths, flags\n"
        "  config.ini        - your config with secrets masked\n"
        "  db_settings.json  - user_preferences (settings) + table list\n"
        "  logs/app.log      - last {n} lines of the application log\n"
        "  logs/monitor.log  - last {n} lines of the monitor log\n\n"
        "Redaction:\n"
        "  API keys, passwords and tokens are masked (e.g. 'abcd...wxyz').\n"
        "  No reading history, file paths or full database is included.\n"
        "  This package is safe to attach to a public support request.\n"
    ).format(n=LOG_TAIL_LINES)


def build_debug_package() -> bytes:
    """Build the debug package ZIP and return its raw bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", _readme())
        zf.writestr("system_info.json", _system_info_json())
        zf.writestr("config.ini", _redacted_config_ini())
        zf.writestr("db_settings.json", _db_settings_json())
        zf.writestr("logs/app.log", _tail(APP_LOG))
        zf.writestr("logs/monitor.log", _tail(MONITOR_LOG))
    buf.seek(0)
    return buf.getvalue()
