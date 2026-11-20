"""The Logs page and its incremental tail (``/logs``, ``/logs/tail``).

Moved out of app.py so the routes can be tested. ``/app-logs`` and
``/mon-logs`` are the two pages this one replaced; they redirect.
"""
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for

from core.app_logging import APP_LOG, MONITOR_LOG, read_log_tail

logs_bp = Blueprint("logs", __name__)


@logs_bp.route("/logs")
def logs_page():
    """Combined logs page with tabs for app and monitor logs."""
    return render_template("logs.html", config=current_app.config)


@logs_bp.route("/app-logs")
def app_logs_page():
    return redirect(url_for("logs.logs_page"))


# Route for monitor logs page
@logs_bp.route("/mon-logs")
def mon_logs_page():
    return redirect(url_for("logs.logs_page"))


# Incremental log tail endpoint (polled by the logs page; replaces SSE
# streaming, which held a gunicorn thread per open tab and grew the browser
# DOM without bound). Returns only the bytes added since the client's last
# byte offset.
_LOG_FILES = {"app": APP_LOG, "mon": MONITOR_LOG}


@logs_bp.route("/logs/tail")
def logs_tail():
    log_type = request.args.get("type", "app")
    log_file = _LOG_FILES.get(log_type)
    if log_file is None:
        return jsonify({"error": "Invalid log type"}), 400

    pos = request.args.get("pos", type=int)
    lines, new_pos, reset = read_log_tail(log_file, pos=pos)
    return jsonify({"pos": new_pos, "lines": lines, "reset": reset})
