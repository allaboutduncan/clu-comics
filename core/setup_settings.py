"""The Guided Setup's partial settings save (``/api/setup/settings``).

The Settings page saves a tab at a time, and two of those saves
(``/api/config/file-processing``, ``/api/config/system-perf``) reset every
field the body leaves out. The wizard collects a handful of fields per step,
so it cannot use them. This is a whitelist of the settings the wizard owns:
an absent key is left alone, an unknown key or a wrong type is refused.

Each entry is ``wizard key -> (type, read, write)``. Most are schema settings;
``auto_metadata_on_move`` predates the schema and keeps its own accessor.
Later wizard steps extend ``WIZARD_SETTINGS`` rather than adding routes.
"""

from core.settings import SETTINGS_SCHEMA, get_setting, set_setting


def _schema(key):
    return (SETTINGS_SCHEMA[key].type,
            lambda: get_setting(key),
            lambda value: set_setting(key, value))


def _auto_metadata_on_move():
    from core.config import PREF_AUTO_METADATA_ON_MOVE, is_auto_metadata_on_move_enabled

    def write(value):
        from core.database import set_user_preference
        return bool(set_user_preference(PREF_AUTO_METADATA_ON_MOVE, bool(value),
                                        category="metadata"))

    return ("bool", is_auto_metadata_on_move_enabled, write)


WIZARD_SETTINGS = {
    "xml_year": _schema("XML_YEAR"),
    "xml_markdown": _schema("XML_MARKDOWN"),
    "xml_list": _schema("XML_LIST"),
    "auto_metadata_on_move": _auto_metadata_on_move(),
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
    return {key: read() for key, (_typ, read, _write) in WIZARD_SETTINGS.items()}


def validate_wizard_settings(data):
    """Return an error message for the first bad key or value, else None."""
    for key, value in data.items():
        spec = WIZARD_SETTINGS.get(key)
        if spec is None:
            return f"Unknown setting: {key}"
        if not _type_ok(spec[0], value):
            return f"{key} must be a {spec[0]}"
    return None


def save_wizard_settings(data):
    """Write the given keys (already validated). True when every write landed."""
    ok = True
    for key, value in data.items():
        ok = bool(WIZARD_SETTINGS[key][2](value)) and ok
    return ok
