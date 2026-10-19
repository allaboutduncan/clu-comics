"""Folder Monitoring: whether monitor.py runs, and whether it is running now.

Both URLs sit under ``/api/config/``, which is in ``core.auth._OWNER_PREFIXES``,
so the surface is owner-only without decorators. The generic
``/api/preferences/<key>`` route is not owner-gated, and this setting decides
whether a background process moves files out of WATCH.

The settings page saves the switch with the rest of the File Processing form;
these exist for the callers that change *only* this -- the prompt after saving
Metron credentials, and the onboarding flow.

Heavy imports are done inside the views so the symbols stay patchable in tests.
"""

from flask import Blueprint, jsonify, request

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
