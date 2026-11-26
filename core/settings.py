"""Typed access to the settings that used to live in config.ini.

``user_preferences`` is the only store. ``SETTINGS_SCHEMA`` is the one table
mapping a legacy ini key to its preference key, type and default; the save routes and
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
    # no longer read (default off).
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
    # Kavita reading sync: the server every user's own auth key is used against.
    "KAVITA_SERVER_URL": _s("kavita_server_url", _S, "", "downloads"),
    # Owner opt-out for core.url_guard's address check (LAN download hosts,
    # fake-ip / split-horizon DNS). Off by default. Never in an ini.
    "DOWNLOAD_ALLOW_PRIVATE_HOSTS": _s("download_allow_private_hosts", _B, False, "downloads"),
    # Bundled FlareSolverr (core/flaresolverr.py). On by default: it ships in
    # the image and only starts when a download meets a Cloudflare challenge.
    # Never in an ini.
    "FLARESOLVERR_ENABLED": _s("flaresolverr_enabled", _B, True, "downloads"),
    "FLARESOLVERR_IDLE_MINUTES": _s("flaresolverr_idle_minutes", _I, 10, "downloads"),
    "FLARESOLVERR_TIMEOUT": _s("flaresolverr_timeout", _I, 60, "downloads"),
    # Identify a file with no cvinfo from its cover (models.metron
    # .match_issue_by_cover). One or two extra Metron requests per such file.
    # Never in an ini.
    "METRON_COVER_MATCH": _s("metron_cover_match", _B, True, "metadata"),
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
    # How many automatic download runs (per kind) the Status page keeps a
    # report for. Never in an ini; see core/download_runs.py.
    "DOWNLOAD_RUN_HISTORY_KEEP": _s("download_run_history_keep", _I, 30, "downloads"),
    "ONESHOT_FOLDERS": _s("oneshot_folders", _S, "oneshots,one-shots,specials", "file_processing"),
    # library health scan (core/library_health.py). Opt-in: it reads every comic.
    "LIBRARY_HEALTH_SCAN_ENABLED": _s("library_health_scan_enabled", _B, False, "system"),
    "LIBRARY_HEALTH_INTERVAL_HOURS": _s("library_health_interval_hours", _I, 6, "system"),
    "LIBRARY_HEALTH_BATCH": _s("library_health_batch", _I, 200, "system"),
    "LIBRARY_HEALTH_RECHECK_DAYS": _s("library_health_recheck_days", _I, 90, "system"),
    # appearance
    "BOOTSTRAP_THEME": _s("bootstrap_theme", _S, "default", "personalization"),
    # Site-default Wrapped style (core/wrapped_styles.py); users pick their
    # own in the Wrapped viewer. Blank = each recap's own default. Never in
    # an ini.
    "WRAPPED_STYLE": _s("wrapped_style", _S, "", "personalization"),
    # Site-default page width (core/page_width.py); users override it on
    # /account. Never in an ini.
    "PAGE_WIDTH": _s("page_width", _S, "auto", "personalization"),
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

# Legacy ini keys that are *not* plain preferences, or that stay out of the
# schema on purpose. The config.ini import is gone; the list remains as the
# record of where each key went.
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

# The generic ``/api/preferences/<key>`` route (app.py) is deny-by-default: it
# serves a preference only if it is listed here, and only to a user holding at
# least the role next to it. Anything else is a 404, so a preference added
# later (to SETTINGS_SCHEMA or anywhere else) is unreadable through that route
# until someone decides it should not be. The keys are the ones the frontend
# fetches; ``tests/unit/test_preference_access.py`` keeps the two in step.
#
#   smart_rename_preview_enabled  static/js/files.js, which also loads on
#                                 series.html (a Reader page) -- a UI toggle
#   series_json_provider,
#   gcd_metadata_languages,
#   inducks_countries             templates/config.html (owner page)
#   credit_backfill_enabled       templates/schedules.html (owner page)
#
# The route's POST side has its own list, PREFERENCE_WRITE_ROLES, below.
PREFERENCE_READ_ROLES = {
    "smart_rename_preview_enabled": "reader",
    "series_json_provider": "owner",
    "gcd_metadata_languages": "owner",
    "inducks_countries": "owner",
    "credit_backfill_enabled": "owner",
}

# Preferences that hold credentials or key material. They are never served by
# the generic route whatever the role asking; a test asserts no entry above is
# one of these, and that every schema pref that looks like a credential is.
SECRET_PREFERENCES = frozenset({
    "secret_key",
    "api_token",
    "rec_api_key",
    "pixeldrain_api_key",
    "custom_headers",
})


def preference_read_status(key, user):
    """HTTP status for serving preference ``key`` to ``user`` via the generic route.

    200 when it may be returned, 403 when the key is readable but not at this
    user's role, 404 for every key that is not on ``PREFERENCE_READ_ROLES`` (or
    is a secret). ``user`` is the request's user dict, as ``core.auth`` returns it.
    """
    if key in SECRET_PREFERENCES:
        return 404
    minimum = PREFERENCE_READ_ROLES.get(key)
    if minimum is None:
        return 404
    from core.auth import role_at_least
    return 200 if role_at_least(user, minimum) else 403


# The same route's POST side, deny-by-default in the same way: it writes a
# preference only if it is listed here, and only for a user holding at least
# the role next to it. A secret is never writable through it, whatever the
# role. The keys are the ones the frontend saves this way, each at the role of
# the page that saves it; ``tests/unit/test_preference_access.py`` keeps the
# two in step. Every other setting has a dedicated save route.
#
#   series_json_provider,
#   gcd_metadata_languages,
#   inducks_countries,
#   download_source_priority      templates/config.html (owner page)
#   credit_backfill_enabled       templates/schedules.html (owner page)
PREFERENCE_WRITE_ROLES = {
    "series_json_provider": "owner",
    "gcd_metadata_languages": "owner",
    "inducks_countries": "owner",
    "download_source_priority": "owner",
    "credit_backfill_enabled": "owner",
}


def preference_write_status(key, user):
    """HTTP status for writing preference ``key`` as ``user`` via the generic route.

    200 when the write may go ahead, 403 when the key is writable but not at
    this user's role, 404 for every key that is not on ``PREFERENCE_WRITE_ROLES``
    (or is a secret).
    """
    if key in SECRET_PREFERENCES:
        return 404
    minimum = PREFERENCE_WRITE_ROLES.get(key)
    if minimum is None:
        return 404
    from core.auth import role_at_least
    return 200 if role_at_least(user, minimum) else 403


def _check_source_priority(value):
    # Stored as a JSON-encoded list of source names, as the Settings page and
    # models.download_sources.get_source_priority agree.
    import json
    if not isinstance(value, str):
        return "must be a JSON list"
    try:
        order = json.loads(value)
    except ValueError:
        return "must be a JSON list"
    if not isinstance(order, list) or not all(isinstance(s, str) for s in order):
        return "must be a JSON list of source names"
    return None


def _check_series_json_provider(value):
    from core.series_json_builder import PROVIDER_CHOICES
    if value not in PROVIDER_CHOICES:
        return f"must be one of {', '.join(PROVIDER_CHOICES)}"
    return None


# Writable keys that are not in SETTINGS_SCHEMA: (type, category, extra check).
# A schema key takes its type and category from the schema instead. The
# categories are the ones the pages have always stored them under.
_WRITE_SPECS = {
    "series_json_provider": (_S, "metadata", _check_series_json_provider),
    "inducks_countries": (_S, "metadata", None),
    "download_source_priority": (_S, "downloads", _check_source_priority),
    "credit_backfill_enabled": (_B, "metadata", None),
}


def _schema_key_for_pref(pref):
    for key, spec in SETTINGS_SCHEMA.items():
        if spec.pref == pref:
            return key
    return None


def _type_error(value, typ):
    if typ == _B:
        return None if isinstance(value, bool) else "must be true or false"
    if typ == _I:
        ok = isinstance(value, int) and not isinstance(value, bool)
        return None if ok else "must be a whole number"
    return None if isinstance(value, str) else "must be a string"


def save_preference(key, value):
    """Validate and store ``value`` for a key on ``PREFERENCE_WRITE_ROLES``.

    Returns ``(status, error)``: ``(200, None)`` once stored, 400 when the value
    is refused, 404 for an unlisted key, 500 when the store fails. Nothing is
    written unless the status is 200. A key in ``SETTINGS_SCHEMA`` must carry the
    schema's type and is stored through ``set_setting``; any other listed key
    must match its ``_WRITE_SPECS`` entry. Access is the caller's job
    (``preference_write_status``); this refuses unlisted keys regardless.
    """
    if key not in PREFERENCE_WRITE_ROLES or key in SECRET_PREFERENCES:
        return 404, "Unknown preference"
    schema_key = _schema_key_for_pref(key)
    if schema_key is not None:
        error = _type_error(value, SETTINGS_SCHEMA[schema_key].type)
        if error:
            return 400, f"{key} {error}"
        if not set_setting(schema_key, value):
            return 500, "Could not save preference"
        return 200, None
    typ, category, check = _WRITE_SPECS[key]
    error = _type_error(value, typ) or (check(value) if check else None)
    if error:
        return 400, f"{key} {error}"
    from core.database import set_user_preference
    if not set_user_preference(key, value, category=category):
        return 500, "Could not save preference"
    return 200, None


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
