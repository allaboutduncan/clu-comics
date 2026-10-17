"""Library health: the scan, its summary card/badge, and the duplicate review page.

Every URL here sits under ``/problem-files`` or ``/api/problem-files``, both in
``core.auth._OWNER_PREFIXES``, so the surface is owner-only without decorators --
which is also why the settings endpoint lives here rather than behind the generic
``/api/preferences/<key>`` route: that one is not owner-gated, and this setting
decides whether CLU reads every comic in the library.

Heavy imports are done inside the views so the symbols stay patchable in tests.
"""

import threading

from flask import Blueprint, jsonify, render_template, request

from core.app_logging import app_logger

library_health_bp = Blueprint("library_health", __name__)

_MAX_BATCH = 5000


@library_health_bp.route("/api/problem-files/health/summary", methods=["GET"])
def api_summary():
    from core.library_health import get_summary

    return jsonify({"success": True, **get_summary()})


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
def duplicates_page():
    return render_template("library_duplicates.html", current_page="/problem-files")


@library_health_bp.route("/api/problem-files/duplicates", methods=["GET"])
def api_duplicates():
    from core.library_health import MAX_DUPLICATE_GROUPS, find_duplicates

    include_dismissed = request.args.get("include_dismissed") in ("1", "true", "yes")
    found = find_duplicates(include_dismissed=include_dismissed)
    total = len(found["exact"]) + len(found["likely"])
    # Exact groups first and in full priority; the cap bounds the payload, and
    # the totals say when it clipped.
    exact = found["exact"][:MAX_DUPLICATE_GROUPS]
    likely = found["likely"][: max(0, MAX_DUPLICATE_GROUPS - len(exact))]
    return jsonify({
        "success": True,
        "exact": exact,
        "likely": likely,
        "total_groups": total,
        "truncated": total > len(exact) + len(likely),
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


@library_health_bp.route("/api/problem-files/duplicates/dismiss", methods=["POST"])
def api_dismiss():
    from core.library_health import dismiss_group

    data = request.get_json(silent=True) or {}
    group_id = data.get("id")
    if not group_id:
        return jsonify({"success": False, "error": "Missing id"}), 400
    ok, error = dismiss_group(group_id, dismissed=data.get("dismissed", True) is not False)
    if not ok:
        return jsonify({"success": False, "error": error}), 400
    return jsonify({"success": True})
