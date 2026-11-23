"""Library health: the scan, its summary card/badge, and the duplicate review page.

Every URL here sits under ``/library-health``, ``/problem-files`` (the old page
URL, which redirects) or ``/api/problem-files``, all in
``core.auth._OWNER_PREFIXES``, so the surface is owner-only without decorators --
which is also why the settings endpoint lives here rather than behind the generic
``/api/preferences/<key>`` route: that one writes only the keys on
``core.settings.PREFERENCE_WRITE_ROLES``, and this setting (which decides whether
CLU reads every comic in the library) is deliberately not one of them.

Heavy imports are done inside the views so the symbols stay patchable in tests.
"""

import os
import threading

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from core.app_logging import app_logger
from helpers.responses import error_response

library_health_bp = Blueprint("library_health", __name__)

_MAX_BATCH = 5000


@library_health_bp.route("/api/problem-files/health/summary", methods=["GET"])
def api_summary():
    from core.library_health import get_summary

    # live: the page's coverage bar moves while a scan runs. The header badge
    # (below) uses the figure stored by the last scan and counts nothing.
    return jsonify({"success": True, **get_summary(live=True)})


@library_health_bp.route("/api/problem-files/health/badge", methods=["GET"])
def api_badge():
    """The header badge's payload. Hidden until a scan has ever completed."""
    from core.library_health import get_summary

    s = get_summary()
    return jsonify({
        "success": True,
        "visible": bool(s["last_run_at"]),
        "level": s["level"],
        "score": s["score"],
        "coverage_pct": s["coverage"]["pct"],
        "running": s["running"],
        "duplicate_groups": s["duplicates"]["exact"] + s["duplicates"]["likely"],
    })


@library_health_bp.route("/api/problem-files/health/scan", methods=["POST"])
def api_scan():
    """Run a scan now (batch plus the cheap checks), off-request.

    Walking and hashing a library far outlasts gunicorn's 120s timeout. The page
    follows ``/api/operation/<op_id>`` -- never ``/api/operations``, which clears
    the notification queue as a side effect.
    """
    from core.app_state import complete_operation, register_operation
    from core.library_health import claim_scan, release_scan, run_scan

    if not claim_scan():
        return jsonify({"success": False, "error": "A scan is already running"}), 409
    try:
        op_id = register_operation("library_health", "Library health scan")
    except Exception:
        release_scan()
        raise

    def _run():
        failed = False
        try:
            failed = "error" in run_scan(op_id=op_id, run_cheap=True)
        except Exception as e:  # run_scan swallows; this is belt and braces
            app_logger.error(f"Library health scan crashed: {e}")
            failed = True
        finally:
            complete_operation(op_id, error=failed)
            release_scan()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"success": True, "op_id": op_id})


@library_health_bp.route("/api/problem-files/health/scan/stop", methods=["POST"])
def api_scan_stop():
    """Ask the running scan to stop after the file it is on.

    What it has checked is kept (every result is stamped as it is made), so
    the next run continues from there. 409 when no scan is running.
    """
    from core.library_health import request_stop

    if not request_stop():
        return jsonify({"success": False, "error": "No scan is running"}), 409
    return jsonify({"success": True})


@library_health_bp.route("/api/problem-files/health/settings", methods=["GET", "POST"])
def api_settings():
    from core.settings import get_bool, get_int, set_setting

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if "enabled" in data:
            set_setting("LIBRARY_HEALTH_SCAN_ENABLED", bool(data["enabled"]))
        for field, key, low, high in (
            ("interval_hours", "LIBRARY_HEALTH_INTERVAL_HOURS", 1, 168),
            ("batch", "LIBRARY_HEALTH_BATCH", 10, _MAX_BATCH),
            ("recheck_days", "LIBRARY_HEALTH_RECHECK_DAYS", 7, 3650),
        ):
            if field in data:
                try:
                    value = int(data[field])
                except (TypeError, ValueError):
                    return jsonify({"success": False, "error": f"Invalid {field}"}), 400
                if not low <= value <= high:
                    return jsonify({
                        "success": False,
                        "error": f"{field} must be between {low} and {high}",
                    }), 400
                set_setting(key, value)
    return jsonify({
        "success": True,
        "enabled": get_bool("LIBRARY_HEALTH_SCAN_ENABLED"),
        "interval_hours": get_int("LIBRARY_HEALTH_INTERVAL_HOURS"),
        "batch": get_int("LIBRARY_HEALTH_BATCH"),
        "recheck_days": get_int("LIBRARY_HEALTH_RECHECK_DAYS"),
    })


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

@library_health_bp.route("/problem-files/duplicates")
def legacy_duplicates_page():
    """The page's old URL; kept so bookmarks keep working."""
    return redirect(url_for("library_health.duplicates_page"), code=301)


@library_health_bp.route("/library-health/duplicates")
def duplicates_page():
    from flask import current_app

    # With the trash on and room for the copies, "Keep selected" is undoable,
    # so the page skips its confirmation (static/js/library_duplicates.js).
    try:
        trash_max_mb = int(current_app.config.get("TRASH_MAX_SIZE_MB", 1024))
    except (TypeError, ValueError):
        trash_max_mb = 0
    return render_template(
        "library_duplicates.html",
        current_page="/library-health",
        trash_enabled=bool(current_app.config.get("TRASH_ENABLED", True)),
        trash_max_bytes=max(0, trash_max_mb) * 1024 * 1024,
    )


@library_health_bp.route("/api/problem-files/duplicates", methods=["GET"])
def api_duplicates():
    """One page of duplicate groups: exact first, then likely.

    ``offset``/``limit`` page through the whole list, so no group is out of
    reach however many there are. A failed read is a 500, never an empty
    list: an empty list has to mean "there are no duplicates".
    """
    from core.library_health import (
        DEFAULT_DUPLICATE_PAGE,
        MAX_DUPLICATE_PAGE,
        find_duplicates,
    )

    truthy = ("1", "true", "yes")
    include_dismissed = request.args.get("include_dismissed") in truthy
    try:
        offset = max(0, int(request.args.get("offset", 0)))
        limit = int(request.args.get("limit", DEFAULT_DUPLICATE_PAGE))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "offset and limit must be numbers"}), 400
    limit = max(1, min(limit, MAX_DUPLICATE_PAGE))
    try:
        found = find_duplicates(include_dismissed=include_dismissed, strict=True)
    except Exception as e:
        return error_response("Could not read the duplicate groups", e)
    ordered = found["exact"] + found["likely"]
    total = len(ordered)
    page = ordered[offset:offset + limit]
    next_offset = offset + len(page)
    return jsonify({
        "success": True,
        "exact": [g for g in page if g["kind"] == "exact"],
        "likely": [g for g in page if g["kind"] == "likely"],
        "total_groups": total,
        "total_exact": len(found["exact"]),
        "total_likely": len(found["likely"]),
        "offset": offset,
        "next_offset": next_offset if next_offset < total else None,
        "truncated": next_offset < total,
        "wasted_bytes": sum(g["wasted_bytes"] for g in found["exact"]),
    })


@library_health_bp.route("/api/problem-files/duplicates/resolve", methods=["POST"])
def api_resolve():
    from core.library_health import resolve_group

    data = request.get_json(silent=True) or {}
    group_id = data.get("id")
    keep = data.get("keep")
    trash = data.get("trash")
    if not group_id or not keep or not isinstance(trash, list):
        return jsonify({"success": False, "error": "Missing id, keep or trash"}), 400
    result = resolve_group(group_id, keep, trash)
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result), (200 if result["success"] else 207)


@library_health_bp.route("/api/problem-files/duplicates/rename", methods=["POST"])
def api_rename_member():
    """Rename one copy in a likely group, from the compare view (#691).

    Two different comics whose names collide -- ``Wonder Woman 005 (1996)`` that
    is really the annual -- are resolved by giving one its right name; the group
    is re-derived from names on every load, so it simply stops being found.
    The rename itself is ``/rename``'s body (``routes.files.rename_path``).
    """
    from core.library_health import follow_member_rename, group_exists, validate_member_rename
    from routes.files import rename_path

    data = request.get_json(silent=True) or {}
    group_id, path, name = data.get("id"), data.get("path"), data.get("name")
    if not all(isinstance(v, str) and v for v in (group_id, path, name)):
        return jsonify({"success": False, "error": "Missing id, path or name"}), 400
    try:
        checked = validate_member_rename(group_id, path, name)
    except Exception as e:
        return error_response("Could not read the duplicate groups", e)
    if checked.get("error"):
        return jsonify({"success": False, "error": checked["error"]}), 400

    new_path = checked["new_path"]
    payload, status = rename_path(path, new_path)
    if status != 200:
        if status == 400:  # the one 400 rename_path returns: the name is taken
            status = 409
        if status >= 500:
            return error_response("Could not rename the file", RuntimeError(payload.get("error")))
        return jsonify({"success": False, "error": payload.get("error") or "Could not rename"}), status

    follow_member_rename(path, new_path)
    gone = group_exists(group_id) is False
    return jsonify({
        "success": True,
        "new_path": new_path,
        "new_name": os.path.basename(new_path),
        "group_gone": gone,
    })


@library_health_bp.route("/api/problem-files/duplicates/dismiss", methods=["POST"])
def api_dismiss():
    from core.library_health import dismiss_group

    data = request.get_json(silent=True) or {}
    group_id = data.get("id")
    if not group_id:
        return jsonify({"success": False, "error": "Missing id"}), 400
    seen = data.get("members")
    if seen is not None and not (isinstance(seen, list) and all(isinstance(p, str) for p in seen)):
        return jsonify({"success": False, "error": "members must be a list of paths"}), 400
    try:
        result = dismiss_group(
            group_id,
            dismissed=data.get("dismissed", True) is not False,
            until_scan=data.get("until_scan") is True,
            seen_paths=seen,
        )
    except Exception as e:
        return error_response("Could not update this group", e)
    if result.get("changed"):
        # A copy turning up is a conflict with what the page shows, not a bad request.
        return error_response("Another copy has turned up since the page loaded",
                              code=409, changed=True)
    if not result.get("success"):
        return error_response("Could not update this group", code=400)
    return jsonify({"success": True, "gone": bool(result.get("gone"))})
