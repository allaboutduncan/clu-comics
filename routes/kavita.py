"""Kavita reading sync: site settings and each user's own link.

Two surfaces, split by who may use them:

- ``/api/kavita/*`` is the owner's: the server URL, the per-library path
  prefixes, the schedule and "sync everyone". ``core/auth.py`` gates the prefix.
- ``/api/account/kavita*`` is each user's own: their Kavita auth key and "sync
  me". It MUST stay under ``/api/account/`` -- that prefix is what lets a Reader
  write their own data (``core.auth._READER_WRITE_PREFIXES``).

No response ever carries an auth key. The sync body is ``core/kavita_sync.py``.
"""
import re
import threading

from flask import Blueprint, current_app, jsonify, request

from core import kavita_store, kavita_sync
from core.app_logging import app_logger
from core.auth import current_user
from core.user_time import format_user_time

kavita_bp = Blueprint("kavita", __name__)

_FREQUENCIES = ("disabled", "daily", "weekly")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DEFAULT_TIME = "05:30"


def _public_status(link):
    """A link as the pages show it. Built field by field so a key can never
    ride along."""
    if not link:
        return {"linked": False}
    return {
        "linked": True,
        "kavita_username": link.get("kavita_username", ""),
        "connection_valid": bool(link.get("connection_valid")),
        "last_sync": format_user_time(link.get("last_sync")) if link.get("last_sync") else None,
        "last_sync_read_count": link.get("last_sync_read_count", 0),
        "last_sync_progress_count": link.get("last_sync_progress_count", 0),
        "last_error": link.get("last_error", ""),
        # Counts only; see kavita_store.DETAIL_KEYS.
        "detail": {
            k: int(v) for k, v in (link.get("detail") or {}).items()
            if k in kavita_store.DETAIL_KEYS
        },
    }


def _next_run():
    from app import get_next_run_for_job

    text = get_next_run_for_job("kavita_sync")
    return text if text != "Not scheduled" else None


def _start_sync(target, **kwargs):
    """Run a sync off-request; a library's worth of series outlasts the
    request timeout."""
    kwargs["app"] = current_app._get_current_object()
    threading.Thread(target=target, kwargs=kwargs, daemon=True).start()


def _linked_users():
    users = []
    for link in kavita_store.list_links():
        row = _public_status(link)
        row["user_id"] = link["user_id"]
        row["username"] = link["username"]
        users.append(row)
    return users


# ---------------------------------------------------------------------------
# Owner
# ---------------------------------------------------------------------------

@kavita_bp.route("/api/kavita/config", methods=["GET"])
def api_get_kavita_config():
    from core.database import get_schedule

    sched = get_schedule("kavita") or {}
    return jsonify({
        "success": True,
        "config": {
            "server_url": kavita_store.get_server_url(),
            "library_mappings": kavita_store.get_library_mappings(),
            "frequency": sched.get("frequency") or "disabled",
            "time": sched.get("time") or DEFAULT_TIME,
            "weekday": sched.get("weekday") or 0,
        },
    })


@kavita_bp.route("/api/kavita/config", methods=["POST"])
def api_save_kavita_config():
    from core.database import save_schedule

    data = request.get_json(silent=True) or {}

    server_url = str(data.get("server_url") or "").strip().rstrip("/")
    if server_url and not re.match(r"^https?://[^\s/]+", server_url, re.IGNORECASE):
        return jsonify({
            "success": False,
            "error": "The server URL must start with http:// or https://",
        }), 400

    frequency = data.get("frequency") or "disabled"
    time_str = data.get("time") or DEFAULT_TIME
    try:
        weekday = int(data.get("weekday") or 0)
    except (TypeError, ValueError):
        weekday = -1
    if frequency not in _FREQUENCIES or not _TIME.match(str(time_str)) or not 0 <= weekday <= 6:
        return jsonify({"success": False, "error": "Invalid schedule"}), 400

    mappings = data.get("library_mappings")
    if mappings is not None and not isinstance(mappings, list):
        return jsonify({"success": False, "error": "Invalid library mappings"}), 400

    previous_url = kavita_store.get_server_url()
    if not kavita_store.set_server_url(server_url):
        return jsonify({"success": False, "error": "Failed to save configuration"}), 500
    if server_url != previous_url and previous_url:
        # Chapter ids belong to the server they came from.
        kavita_store.clear_sync_log()
        app_logger.info("Kavita server URL changed; the import log was cleared")

    if mappings is not None and not kavita_store.save_library_mappings(mappings):
        return jsonify({"success": False, "error": "Failed to save library mappings"}), 500

    if not save_schedule("kavita", frequency, time_str, weekday):
        return jsonify({"success": False, "error": "Failed to save the schedule"}), 500

    from app import configure_kavita_sync_schedule

    configure_kavita_sync_schedule()
    return jsonify({"success": True, "message": "Kavita configuration saved"})


@kavita_bp.route("/api/kavita/status", methods=["GET"])
def api_kavita_status():
    return jsonify({
        "success": True,
        "running": kavita_sync.is_running(),
        "next_run": _next_run(),
        "users": _linked_users(),
    })


@kavita_bp.route("/api/kavita/sync", methods=["POST"])
def api_kavita_sync_all():
    if not kavita_store.get_server_url():
        return jsonify({"success": False, "error": "Set the Kavita server URL first."}), 400
    if kavita_sync.is_running():
        return jsonify({"success": False, "error": "A Kavita sync is already running."}), 409
    data = request.get_json(silent=True) or {}
    _start_sync(kavita_sync.sync_all, full=bool(data.get("full")))
    return jsonify({"success": True, "message": "Kavita sync started in background"})


# ---------------------------------------------------------------------------
# Each user's own link
# ---------------------------------------------------------------------------

def _account_status(user):
    status = _public_status(kavita_store.get_link_status(user["id"]))
    status["success"] = True
    status["configured"] = bool(kavita_store.get_server_url())
    status["running"] = kavita_sync.is_running()
    return status


@kavita_bp.route("/api/account/kavita", methods=["GET"])
def account_kavita_status():
    user = current_user()
    if user is None:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_account_status(user))


@kavita_bp.route("/api/account/kavita", methods=["POST"])
def account_kavita_link():
    """Store the current user's Kavita auth key, after Kavita accepts it."""
    from models.kavita import KavitaClient

    user = current_user()
    if user is None:
        return jsonify({"error": "unauthorized"}), 401

    server_url = kavita_store.get_server_url()
    if not server_url:
        return jsonify({
            "success": False,
            "error": "Kavita sync has not been set up on this server yet.",
        }), 400

    body = request.get_json(silent=True) or {}
    auth_key = str(body.get("auth_key") or "").strip()
    if not auth_key:
        return jsonify({"success": False, "error": "Enter your Kavita auth key."}), 400

    ok, details, kavita_username = KavitaClient(server_url, auth_key).test_connection()
    if not ok:
        return jsonify({"success": False, "error": details}), 400

    if not kavita_store.save_link(user["id"], auth_key, kavita_username):
        return jsonify({"success": False, "error": "Could not save the auth key."}), 500
    return jsonify(_account_status(user))


@kavita_bp.route("/api/account/kavita", methods=["DELETE"])
def account_kavita_unlink():
    user = current_user()
    if user is None:
        return jsonify({"error": "unauthorized"}), 401
    if not kavita_store.delete_link(user["id"]):
        return jsonify({"success": False, "error": "Could not remove the link."}), 500
    return jsonify(_account_status(user))


@kavita_bp.route("/api/account/kavita/sync", methods=["POST"])
def account_kavita_sync():
    user = current_user()
    if user is None:
        return jsonify({"error": "unauthorized"}), 401

    link = kavita_store.get_link_status(user["id"])
    if not link:
        return jsonify({"success": False, "error": "Link your Kavita account first."}), 400
    if not link["connection_valid"]:
        return jsonify({
            "success": False,
            "error": "Kavita rejected your auth key. Save a new one first.",
        }), 400
    if not kavita_store.get_server_url():
        return jsonify({
            "success": False,
            "error": "Kavita sync has not been set up on this server yet.",
        }), 400
    if kavita_sync.is_running():
        return jsonify({"success": False, "error": "A Kavita sync is already running."}), 409

    data = request.get_json(silent=True) or {}
    _start_sync(kavita_sync.sync_user, user_id=user["id"], full=bool(data.get("full")))
    return jsonify({"success": True, "message": "Kavita sync started in background"})
