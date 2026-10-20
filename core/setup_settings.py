"""The Guided Setup's partial settings save (``/api/setup/settings``).

The Settings page saves a tab at a time, and two of those saves
(``/api/config/file-processing``, ``/api/config/system-perf``) reset every
field the body leaves out. The wizard collects a handful of fields per step,
so it cannot use them. This is a whitelist of the settings the wizard owns:
an absent key is left alone, an unknown key or a wrong type is refused.

Each entry is ``wizard key -> (type, read, write[, validate])``. Most are
schema settings; ``auto_metadata_on_move``, ``timezone``,
``download_source_priority`` and ``download_packs`` predate the schema and
keep their own accessors. ``validate(value)`` returns an error message or
None, and runs after the type check. Later wizard steps extend
``WIZARD_SETTINGS`` rather than adding routes.
"""

from core.settings import SETTINGS_SCHEMA, get_setting, set_setting


def _schema(key, validate=None):
    return (SETTINGS_SCHEMA[key].type,
            lambda: get_setting(key),
            lambda value: set_setting(key, value),
            validate)


def _positive(value):
    return None if value > 0 else "must be greater than 0"


def _auto_metadata_on_move():
    from core.config import PREF_AUTO_METADATA_ON_MOVE, is_auto_metadata_on_move_enabled

    def write(value):
        from core.database import set_user_preference
        return bool(set_user_preference(PREF_AUTO_METADATA_ON_MOVE, bool(value),
                                        category="metadata"))

    return ("bool", is_auto_metadata_on_move_enabled, write)


def _timezone():
    """The ``timezone`` pref: "UTC" or a signed hour offset ("-5", "5.5").

    Not a schema setting (``NON_SCHEMA_INI_KEYS``). The Settings page saves it
    through ``/api/config/system-perf``, which resets eight other settings
    the body leaves out and stores whatever it is sent; here a value
    ``core.user_time`` would silently read as UTC is refused instead.
    """
    from core.database import get_user_preference, set_user_preference

    def read():
        return str(get_user_preference("timezone", default="UTC") or "UTC")

    def write(value):
        return bool(set_user_preference("timezone", value, category="personalization"))

    return ("str", read, write, _valid_timezone)


def _valid_timezone(value):
    from core.user_time import parse_offset_hours

    text = value.strip()
    if text == "UTC":
        return None
    try:
        hours = float(text)
    except ValueError:
        return 'must be "UTC" or an hour offset such as "-5" or "5.5"'
    if text != value or parse_offset_hours(text) != hours:
        return "must be an offset between -12 and 14 hours"
    return None


def _comma_list(value, allowed):
    """Error for a comma-separated subset of ``allowed``: non-empty, no repeats."""
    names = [n.strip() for n in value.split(",")]
    if not value.strip() or any(not n for n in names):
        return "must name at least one, separated by commas"
    unknown = [n for n in names if n not in allowed]
    if unknown:
        return f"has unknown entries: {', '.join(unknown)} (choose from {', '.join(allowed)})"
    if len(set(names)) != len(names):
        return "names an entry twice"
    if ",".join(names) != value:
        return "must have no spaces"
    return None


def _source_priority():
    """``download_source_priority``: the order Auto-download tries sources in.

    A source runs only if it is in this list, so it is also the on/off switch
    for Usenet and DC++. Stored as a JSON list (as the Settings page writes
    it); the wizard reads and writes it comma-separated. The Settings route
    for it is the generic, clerk-level ``/api/preferences/<key>`` in app.py.
    """
    from models.download_sources import KNOWN_SOURCES

    def read():
        from models.download_sources import get_source_priority
        return ",".join(get_source_priority())

    def write(value):
        import json
        from core.database import set_user_preference
        return bool(set_user_preference("download_source_priority",
                                        json.dumps(value.split(",")), category="downloads"))

    return ("str", read, write, lambda value: _comma_list(value, KNOWN_SOURCES))


def _provider_priority(value):
    # Lazy: models.getcomics is a heavy import, and it owns the provider keys.
    from models.getcomics import PROVIDER_LABELS
    return _comma_list(value, tuple(PROVIDER_LABELS))


def _single_line(value):
    if value != value.strip() or "\n" in value or "\r" in value:
        return "must be a single line with no surrounding spaces"
    return None


def _download_packs():
    from core.config import PREF_DOWNLOAD_PACKS, is_download_packs_enabled

    def write(value):
        from core.database import set_user_preference
        return bool(set_user_preference(PREF_DOWNLOAD_PACKS, bool(value), category="downloads"))

    return ("bool", is_download_packs_enabled, write)


WIZARD_SETTINGS = {
    # Metadata
    "xml_year": _schema("XML_YEAR"),
    "xml_markdown": _schema("XML_MARKDOWN"),
    "xml_list": _schema("XML_LIST"),
    "auto_metadata_on_move": _auto_metadata_on_move(),
    # Folder Monitoring. WATCH/TARGET and the monitor switch have their own
    # partial saves (/api/config/folder-paths, /api/config/folder-monitor).
    "autoconvert": _schema("AUTOCONVERT"),
    "auto_rename_monitor": _schema("AUTO_RENAME_MONITOR"),
    "enable_custom_rename": _schema("ENABLE_CUSTOM_RENAME"),
    "custom_rename_pattern": _schema("CUSTOM_RENAME_PATTERN"),
    "trash_enabled": _schema("TRASH_ENABLED"),
    "trash_max_size_mb": _schema("TRASH_MAX_SIZE_MB", _positive),
    # Downloads. Clients and indexers have their own routes
    # (/api/download-clients, /api/indexers).
    "download_source_priority": _source_priority(),
    "pixeldrain_api_key": _schema("PIXELDRAIN_API_KEY", _single_line),
    "download_provider_priority": _schema("DOWNLOAD_PROVIDER_PRIORITY", _provider_priority),
    "download_packs": _download_packs(),
    # Schedules
    "timezone": _timezone(),
}


def _type_ok(typ, value):
    # bool is an int subclass; refuse it where an int is wanted, and vice versa.
    if typ == "bool":
        return isinstance(value, bool)
    if typ == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, str)


def read_wizard_settings():
    """``{wizard key: current value}`` for every whitelisted setting."""
    return {key: spec[1]() for key, spec in WIZARD_SETTINGS.items()}


def validate_wizard_settings(data):
    """Return an error message for the first bad key or value, else None."""
    for key, value in data.items():
        spec = WIZARD_SETTINGS.get(key)
        if spec is None:
            return f"Unknown setting: {key}"
        if not _type_ok(spec[0], value):
            return f"{key} must be a {spec[0]}"
        validate = spec[3] if len(spec) > 3 else None
        error = validate(value) if validate else None
        if error:
            return f"{key} {error}"
    return None


def save_wizard_settings(data):
    """Write the given keys (already validated). True when every write landed."""
    ok = True
    for key, value in data.items():
        ok = bool(WIZARD_SETTINGS[key][2](value)) and ok
    return ok
