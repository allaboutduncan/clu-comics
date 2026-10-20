"""Folder Monitoring: whether monitor.py runs, and whether it is running now.

Both URLs sit under ``/api/config/``, which is in ``core.auth._OWNER_PREFIXES``,
so the surface is owner-only without decorators. The generic
``/api/preferences/<key>`` route is not owner-gated, and this setting decides
whether a background process moves files out of WATCH.

The settings page saves the switch with the rest of the File Processing form;
these exist for the callers that change *only* this -- the prompt after saving
Metron credentials, and the onboarding flow.

``/api/config/folder-paths`` is the same idea for WATCH and TARGET: the File
Processing save takes the whole form, and a body that leaves a field out resets
it, so the Guided Setup cannot send just the two paths through it. Here a key
that is absent is left alone.

Heavy imports are done inside the views so the symbols stay patchable in tests.
"""

from flask import Blueprint, current_app, jsonify, request

folder_monitor_bp = Blueprint("folder_monitor", __name__)


@folder_monitor_bp.route("/api/config/folder-monitor", methods=["GET"])
def api_folder_monitor_status():
    from core import monitor_supervisor

    return jsonify({"success": True, **monitor_supervisor.status()})


@folder_monitor_bp.route("/api/config/folder-monitor", methods=["POST"])
def api_folder_monitor_set():
    from core import monitor_supervisor
    from core.settings import set_setting

    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"success": False, "error": "'enabled' must be true or false"}), 400
    if not set_setting("MONITOR_ENABLED", enabled):
        return jsonify({"success": False, "error": "Could not save the setting"}), 500
    monitor_supervisor.poke()
    return jsonify({"success": True, **monitor_supervisor.status()})


# Pref key, app.config key, default when unset -- as load_flask_config does.
_FOLDER_PATHS = {
    "watch": ("WATCH", "/downloads/temp"),
    "target": ("TARGET", "/downloads/processed"),
}


def _folder_paths():
    from core.config import get_target_dir, get_watch_dir

    return {"watch": get_watch_dir(), "target": get_target_dir(),
            "defaults": {key: default for key, (_, default) in _FOLDER_PATHS.items()}}


@folder_monitor_bp.route("/api/config/folder-paths", methods=["GET"])
def api_folder_paths_get():
    """The stored WATCH/TARGET ('' means unset, so the default applies)."""
    return jsonify({"success": True, **_folder_paths()})


@folder_monitor_bp.route("/api/config/folder-paths", methods=["POST"])
def api_folder_paths_set():
    """Save WATCH and/or TARGET. An empty string clears one back to its default.

    Validated with the same predicate as the File Processing save, and both
    values are checked before either is written, so a refused TARGET cannot
    leave a half-applied WATCH behind. monitor.py re-points itself when WATCH
    changes (``monitor.reschedule_if_changed``), so nothing is poked here.
    """
    from core.database import set_user_preference
    from helpers.library import watch_target_verdict

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Expected a JSON object"}), 400
    updates = {}
    for key in _FOLDER_PATHS:
        if key not in data:
            continue
        value = data[key]
        if value is None:
            value = ""
        if not isinstance(value, str):
            return jsonify({"success": False, "error": f"'{key}' must be a string"}), 400
        # Same cleanup as app.sanitize_config_value, which this cannot import.
        updates[key] = value.replace("\n", "").replace("\r", "").strip()
    if not updates:
        return jsonify({"success": False,
                        "error": "Provide 'watch' and/or 'target'"}), 400

    warnings = []
    for key, value in updates.items():
        blocked, message = watch_target_verdict(_FOLDER_PATHS[key][0], value)
        if blocked:
            return jsonify({"success": False, "error": message}), 400
        if message:
            warnings.append(message)

    for key, value in updates.items():
        if not set_user_preference(key, value, category="file_processing"):
            return jsonify({"success": False, "error": "Could not save the setting"}), 500
        config_key, default = _FOLDER_PATHS[key]
        current_app.config[config_key] = value or default

    return jsonify({"success": True, "warnings": warnings, **_folder_paths()})
