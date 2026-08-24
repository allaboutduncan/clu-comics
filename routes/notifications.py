"""Notification settings endpoints (Apprise).

Owner-only by path: ``core.auth`` puts every ``/api/config/`` route behind the
owner role (see ``_OWNER_PREFIXES``), so these need no decorator of their own.

Lives in a blueprint rather than app.py so ``tests/routes`` can import it
directly — routes defined in app.py have to be hand-mirrored in the test
fixture, which duplicates the logic under test.
"""

from flask import Blueprint, jsonify, request

from core.app_logging import app_logger
from core.notifications import (
    EVENT_DEFS,
    PREF_ENABLED,
    PREF_EVENTS,
    PREF_URLS,
    parse_urls,
    sanitize_events,
    send_test,
)

notifications_bp = Blueprint('notifications', __name__)


@notifications_bp.route('/api/config/notifications', methods=['POST'])
def save_notification_config():
    """Persist the owner-global notification settings."""
    from core.database import set_user_preference

    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "No data provided"}), 400

        enabled = bool(data.get("enabled"))
        urls = parse_urls(data.get("urls"))
        events = sanitize_events(data.get("events"))

        # Turning it on with nowhere to send is a silent no-op the user would
        # only discover by not receiving anything — reject it instead.
        if enabled and not urls:
            return jsonify({
                "success": False,
                "error": "Add at least one notification URL, or turn "
                         "notifications off.",
            }), 400

        set_user_preference(PREF_ENABLED, enabled, category="notifications")
        set_user_preference(PREF_URLS, urls, category="notifications")
        set_user_preference(PREF_EVENTS, events, category="notifications")

        return jsonify({
            "success": True,
            "message": "Notification settings saved",
            "urlCount": len(urls),
        })
    except Exception as e:
        app_logger.error(f"Error saving notification config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@notifications_bp.route('/api/config/notifications/test', methods=['POST'])
def test_notification_config():
    """Send a test notification to the URLs in the request body.

    Deliberately uses the posted URLs rather than the saved ones, so an owner
    can verify a target before committing it.
    """
    try:
        data = request.get_json(silent=True) or {}
        urls = parse_urls(data.get("urls"))
        if not urls:
            return jsonify({
                "success": False,
                "error": "Add at least one notification URL first.",
            }), 400

        ok, error = send_test(urls)
        if ok:
            return jsonify({
                "success": True,
                "message": f"Test notification sent to {len(urls)} target(s)",
            })
        return jsonify({"success": False, "error": error}), 502
    except Exception as e:
        app_logger.error(f"Error sending test notification: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@notifications_bp.route('/api/config/notifications/events', methods=['GET'])
def list_notification_events():
    """The catalog of notifiable events, for the settings UI."""
    return jsonify({
        "success": True,
        "events": [
            {
                "id": event_id,
                "label": defn["label"],
                "description": defn["description"],
                "default": defn["default"],
            }
            for event_id, defn in EVENT_DEFS.items()
        ],
    })
