"""Typed access to the settings that used to live in config.ini.

``user_preferences`` is the only store. ``SETTINGS_SCHEMA`` is the one table
mapping a legacy ini key to its preference key, type and default; the one-time
import (``core.config.migrate_ini_to_user_preferences``), the save routes and
every reader all go through it, so they cannot disagree about a name or a
default.

Reads are cached for ``CACHE_TTL`` seconds. ``monitor.py`` and ``api.py`` are
separate processes from the Flask app and used to learn about a change from a
5-second mtime poll of config.ini; a short TTL gives them the same latency
without a file to watch. A write in this process drops the cache at once.

Reads never raise on an unreadable store (an unknown key is a programming
error and does raise KeyError): it returns the schema default, because
these are called from scheduler threads, the file monitor and GET routes, and
(like ``is_download_packs_enabled``) a settings read must not take them down.
"""

import threading
import time
from collections import namedtuple

from core.app_logging import app_logger

CACHE_TTL = 5.0

Setting = namedtuple("Setting", "pref type default category")

_B, _I, _S = "bool", "int", "str"


def _s(pref, typ, default, category):
    return Setting(pref, typ, default, category)


# legacy ini key -> Setting.  Prefs that already existed keep their names.
SETTINGS_SCHEMA = {
    # file processing
    # Whether monitor.py runs and owns WATCH. Was the MONITOR env var, which
    # core.config.seed_monitor_from_env imports once (default off).
    "MONITOR_ENABLED": _s("monitor_enabled", _B, False, "file_processing"),
    "AUTOCONVERT": _s("autoconvert", _B, False, "file_processing"),
    "READ_SUBDIRECTORIES": _s("read_subdirectories", _B, False, "file_processing"),
    "CONVERT_SUBDIRECTORIES": _s("convert_subdirectories", _B, False, "file_processing"),
    "MOVE_DIRECTORY": _s("move_directory", _B, False, "file_processing"),
    "CONSOLIDATE_DIRECTORIES": _s("consolidate_directories", _B, False, "file_processing"),
    "AUTO_RENAME_MONITOR": _s("auto_rename_monitor", _B, True, "file_processing"),
    "RECONCILE_INTERVAL_MINUTES": _s("reconcile_interval_minutes", _I, 5, "file_processing"),
    "IGNORED_EXTENSIONS": _s("ignored_extensions", _S, ".crdownload,.torrent,.tmp,.mega,.rar,.bak,.zip", "file_processing"),
    "IGNORED_FILES": _s("ignored_files", _S, "cover.jpg,cvinfo,.DS_Store", "file_processing"),
    "IGNORED_TERMS": _s("ignored_terms", _S, "Annual", "file_processing"),
    "SKIPPED_FILES": _s("skipped_files", _S, ".xml", "file_processing"),
    "DELETED_FILES": _s("deleted_files", _S, ".nfo,.sfv,.db,.DS_Store", "file_processing"),
    "AUTO_CLEANUP_ORPHAN_FILES": _s("auto_cleanup_orphan_files", _B, True, "file_processing"),
    "CLEANUP_INTERVAL_HOURS": _s("cleanup_interval_hours", _I, 1, "file_processing"),
    "OPERATION_TIMEOUT": _s("operation_timeout", _I, 3600, "system"),
    "LARGE_FILE_THRESHOLD": _s("large_file_threshold", _I, 500, "system"),
    "ENABLE_DEBUG_LOGGING": _s("enable_debug_logging", _B, False, "system"),
    "ENABLE_METADATA_SCAN": _s("enable_metadata_scan", _B, True, "system"),
    "METADATA_SCAN_THREADS": _s("metadata_scan_threads", _I, 2, "system"),
    # comicinfo
    "XML_YEAR": _s("xml_year", _B, False, "metadata"),
    "XML_MARKDOWN": _s("xml_markdown", _B, False, "metadata"),
    "XML_LIST": _s("xml_list", _B, True, "metadata"),
    # rename / move (the first two predate this module)
    "ENABLE_CUSTOM_RENAME": _s("enable_custom_rename", _B, False, "file_processing"),
    "CUSTOM_RENAME_PATTERN": _s("custom_rename_pattern", _S, "", "file_processing"),
    "ENABLE_AUTO_RENAME": _s("enable_auto_rename", _B, False, "file_processing"),
    "ENABLE_AUTO_MOVE": _s("enable_auto_move", _B, False, "file_processing"),
    "CUSTOM_MOVE_PATTERN": _s("custom_move_pattern", _S, "{publisher}/{series_name}/v{start_year}", "file_processing"),
    # trash
    "TRASH_ENABLED": _s("trash_enabled", _B, True, "file_processing"),
    "TRASH_DIR": _s("trash_dir", _S, "", "file_processing"),
    "TRASH_MAX_SIZE_MB": _s("trash_max_size_mb", _I, 1024, "file_processing"),
    # downloads
    "DOWNLOAD_PROVIDER_PRIORITY": _s("download_provider_priority", _S, "pixeldrain,download_now,mega", "downloads"),
    "PIXELDRAIN_API_KEY": _s("pixeldrain_api_key", _S, "", "downloads"),
    "HEADERS": _s("custom_headers", _S, "", "downloads"),
    "GCD_METADATA_LANGUAGES": _s("gcd_metadata_languages", _S, "en", "metadata"),
    # getcomics scoring vocabularies
    "PUBLICATION_TYPES": _s("publication_types", _S, "annual,quarterly", "downloads"),
    "VARIANT_TYPES": _s(
        "variant_types", _S,
        "annual,quarterly,tpB,oneshot,one-shot,o.s.,os,trade paperback,trade-paperback,"
        "omni,omnibus,omb,hardcover,deluxe,prestige,gallery",
        "downloads",
    ),
    "SEQUEL_KEYWORDS": _s("sequel_keywords", _S, "season,volume,book,part,chapter", "downloads"),
    "CROSSOVER_KEYWORDS": _s("crossover_keywords", _S, "meets,vs,versus,x-over,crossover", "downloads"),
    "ONESHOT_FOLDERS": _s("oneshot_folders", _S, "oneshots,one-shots,specials", "file_processing"),
    # library health scan (core/library_health.py). Opt-in: it reads every comic.
    "LIBRARY_HEALTH_SCAN_ENABLED": _s("library_health_scan_enabled", _B, False, "system"),
    "LIBRARY_HEALTH_INTERVAL_HOURS": _s("library_health_interval_hours", _I, 6, "system"),
    "LIBRARY_HEALTH_BATCH": _s("library_health_batch", _I, 200, "system"),
    "LIBRARY_HEALTH_RECHECK_DAYS": _s("library_health_recheck_days", _I, 90, "system"),
    # appearance
    "BOOTSTRAP_THEME": _s("bootstrap_theme", _S, "default", "personalization"),
    # Guided Setup (core/setup_state.py). Never in an ini; init_db stamps the
    # state once, and "" (undecided) deliberately does not redirect.
    "GUIDED_SETUP_STATE": _s("guided_setup_state", _S, "", "onboarding"),
    "GUIDED_SETUP_LAST_STEP": _s("guided_setup_last_step", _S, "", "onboarding"),
    "GUIDED_SETUP_PATH": _s("guided_setup_path", _S, "", "onboarding"),
    # The one-time "Guided Setup can check your configuration" banner shown to
    # the owner of an upgraded install.
    "GUIDED_SETUP_BANNER_DISMISSED": _s("guided_setup_banner_dismissed", _B, False, "onboarding"),
    # JSON counts of the last Mylar3 import (models/mylar_import.py). The job
    # itself is forgotten after 15 minutes; the wizard's status reads this.
    "MYLAR_IMPORT_LAST_RUN": _s("mylar_import_last_run", _S, "", "onboarding"),
}

# ini keys that are *not* plain preferences, or that stay out of the schema on
# purpose. The import handles each of these itself (or deliberately skips it).
#   CACHE_DIR      a fixed path (core.config.CACHE_DIR), not a setting
#   WATCH/TARGET   already migrated, see core.config.get_watch_dir
#   TIMEZONE       migrated by init_db
#   COMICVINE_API_KEY  lives in provider_credentials
#   REC_*          migrated to rec_* (and the ini copies are import-only)
NON_SCHEMA_INI_KEYS = frozenset({
    "CACHE_DIR", "WATCH", "TARGET", "TIMEZONE", "COMICVINE_API_KEY",
    "REC_ENABLED", "REC_PROVIDER", "REC_API_KEY", "REC_MODEL", "AUTO_UNPACK",
    "METRON_USERNAME", "METRON_PASSWORD",
})

_snapshot = None  # (monotonic time, {pref: raw stored value})
_cache_lock = threading.Lock()


def _coerce(value, typ, default):
    """Coerce a stored or ini value to ``typ``; fall back to ``default``."""
    if value is None:
        return default
    try:
        if typ == _B:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            return str(value).strip().lower() in ("true", "1", "yes", "on")
        if typ == _I:
            if isinstance(value, bool):
                return int(value)
            return int(float(str(value).strip()))
        return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        return default


def _spec(key):
    try:
        return SETTINGS_SCHEMA[key]
    except KeyError:
        raise KeyError(f"Unknown setting {key!r}; add it to SETTINGS_SCHEMA") from None


def _load_snapshot():
    """Every schema preference in one query, or None if the store is unreadable.

    One read per TTL window serves all settings, so a per-file loop that asks
    for three of them costs nothing. ``get_user_preference`` swallows its own
    errors, which makes a failed read look like "unset"; reading directly lets
    us tell the two apart and refuse to cache a failure.
    """
    import json
    from core.database import db_conn
    prefs = [s.pref for s in SETTINGS_SCHEMA.values()]
    try:
        with db_conn() as conn:
            if not conn:
                return None
            rows = conn.execute(
                "SELECT key, value FROM user_preferences WHERE key IN (%s)"
                % ",".join("?" * len(prefs)), prefs).fetchall()
        out = {}
        for r in rows:
            try:
                out[r[0]] = json.loads(r[1])
            except (TypeError, ValueError):
                continue
        return out
    except Exception as e:
        app_logger.debug(f"settings snapshot unavailable, using defaults: {e}")
        return None


def _snapshot_data():
    """The cached {pref: value} snapshot, refreshed past the TTL; None if unreadable."""
    global _snapshot
    now = time.monotonic()
    with _cache_lock:
        snap = _snapshot
    if snap is None or now - snap[0] >= CACHE_TTL:
        data = _load_snapshot()
        if data is None:
            return None  # not cached: the next call retries
        snap = (now, data)
        with _cache_lock:
            _snapshot = snap
    return snap[1]


def get_setting(key):
    """Return the typed value of a schema setting (default if unset/unreadable)."""
    spec = _spec(key)
    data = _snapshot_data()
    if data is None:
        return spec.default
    return _coerce(data.get(spec.pref), spec.type, spec.default)


def peek_setting(key):
    """Like ``get_setting``, but ``None`` when the store cannot be read.

    For callers that *act* on a change and must not mistake a failed read for
    the default -- the folder monitor would otherwise shut itself down on every
    "database is locked".
    """
    spec = _spec(key)
    data = _snapshot_data()
    if data is None:
        return None
    return _coerce(data.get(spec.pref), spec.type, spec.default)


def get_bool(key):
    return bool(get_setting(key))


def get_int(key):
    return int(get_setting(key))


def get_list(key):
    """Comma-separated setting as a list of stripped, non-empty items."""
    return [p.strip() for p in str(get_setting(key) or "").split(",") if p.strip()]


def set_setting(key, value):
    """Store a schema setting, coercing to its type. Returns True on success."""
    spec = _spec(key)
    coerced = _coerce(value, spec.type, spec.default)
    from core.database import set_user_preference
    ok = bool(set_user_preference(spec.pref, coerced, category=spec.category))
    invalidate(key)
    return ok


def invalidate(key=None):
    """Drop the cached snapshot (after a write, in tests, after a restore)."""
    global _snapshot
    with _cache_lock:
        _snapshot = None


def all_settings():
    """{ini key: typed value} for every schema setting, for page rendering."""
    return {k: get_setting(k) for k in SETTINGS_SCHEMA}


def get_ext_list(key):
    """Comma-separated setting as lower-case items (file extensions)."""
    return [p.lower() for p in get_list(key)]
