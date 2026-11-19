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

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for

from core.app_logging import app_logger
from helpers.responses import error_response

library_health_bp = Blueprint("library_health", __name__)

_MAX_BATCH = 5000


@library_health_bp.route("/api/problem-files/health/summary", methods=["GET"])
def api_summary():
    from core.library_health import get_summary
    from core.series_conflicts import count_open

    # The conflict count is here, not in get_summary(): the header badge polls
    # that and has no use for it.
    return jsonify({"success": True, **get_summary(), "series_conflicts": count_open()})


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
# Series folder conflicts (core/series_conflicts.py)
# ---------------------------------------------------------------------------

def _conflict_id(data):
    try:
        value = int(data.get("id"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


@library_health_bp.route("/library-health/series")
def series_conflicts_page():
    return render_template("library_series_conflicts.html", current_page="/library-health")


@library_health_bp.route("/api/problem-files/series-conflicts", methods=["GET"])
def api_series_conflicts():
    from core.series_conflict_merge import group_by_parent, likely_same_name
    from core.series_conflicts import list_conflicts

    include_ignored = request.args.get("include_ignored") in ("1", "true", "yes")
    rows = list_conflicts(include_ignored=include_ignored)
    if rows is None:
        return error_response("Could not read the series conflicts", code=500)
    for row in rows:
        row["reachable"] = os.path.isdir(row["folder"])
        row["likely_same"] = likely_same_name(row["folder"], row.get("mapped_folder"))
    return jsonify({
        "success": True,
        "conflicts": rows,
        "open": sum(1 for r in rows if not r["ignored"]),
        "groups": group_by_parent([r for r in rows if r["reachable"]]),
    })


@library_health_bp.route("/api/problem-files/series-conflicts/ignore", methods=["POST"])
def api_series_conflict_ignore():
    from core.series_conflicts import set_ignored

    data = request.get_json(silent=True) or {}
    conflict_id = _conflict_id(data)
    if not conflict_id:
        return jsonify({"success": False, "error": "Missing id"}), 400
    if not set_ignored(conflict_id, ignored=data.get("ignored", True) is not False):
        return error_response("That conflict is no longer listed", code=404)
    return jsonify({"success": True})


@library_health_bp.route("/api/problem-files/series-conflicts/remove", methods=["POST"])
def api_series_conflict_remove():
    """Forget a row whose folder is gone. A folder still on disk is Ignored instead."""
    from core.series_conflicts import get_conflict, remove

    data = request.get_json(silent=True) or {}
    conflict_id = _conflict_id(data)
    if not conflict_id:
        return jsonify({"success": False, "error": "Missing id"}), 400
    row = get_conflict(conflict_id)
    if not row:
        return error_response("That conflict is no longer listed", code=404)
    if os.path.isdir(row["folder"]):
        return error_response("The folder is still there; choose Ignore instead", code=409)
    remove(conflict_id)
    return jsonify({"success": True})


@library_health_bp.route("/api/problem-files/series-conflicts/switch", methods=["POST"])
def api_series_conflict_switch():
    """Make this row's folder the series' folder (Scan Library's old "Apply").

    The previously mapped folder becomes the conflict, so nothing is lost from
    the list: its comics are now the ones not counted as owned.
    """
    from core.path_access import grant_refusal
    from core.series_conflicts import after_switch, get_conflict
    from helpers.library import is_valid_library_path
    from models import library_automap

    data = request.get_json(silent=True) or {}
    conflict_id = _conflict_id(data)
    if not conflict_id:
        return jsonify({"success": False, "error": "Missing id"}), 400
    row = get_conflict(conflict_id)
    if not row:
        return error_response("That conflict is no longer listed", code=404)
    folder = row["folder"]
    if not is_valid_library_path(folder):
        return jsonify({"success": False, "error": "The folder must be inside a library"}), 400
    if not os.path.isdir(folder):
        return error_response("The folder no longer exists", code=409)
    refusal = grant_refusal(folder)
    if refusal:
        return jsonify({"success": False, "error": refusal[0]}), refusal[1]

    previous = row.get("mapped_folder")
    result = library_automap.apply_and_sync([{
        "folder": folder,
        "metron_id": row["series_id"],
        "series_name": row.get("series_name"),
        "publisher_name": row.get("publisher_name"),
        "year": row.get("year"),
        "status": row.get("series_status"),
        "cv_id": row.get("cv_id"),
    }])
    if not result.get("applied"):
        # apply_automap's failure text can be a raw exception; it is logged there.
        return error_response("Could not map the folder to this series", code=500)
    after_switch(conflict_id, previous)
    return jsonify({"success": True, "folder": folder, "previous": previous})


def _merge_plan_or_refusal(conflict_id, mode, keep=None):
    """``(plan, None)`` or ``(None, response)`` -- path, grant and plan checks in one place."""
    from core.path_access import grant_refusal
    from core.series_conflict_merge import MergeRefused, build_plan
    from core.series_conflicts import get_conflict

    row = get_conflict(conflict_id)
    try:
        plan = build_plan(row, mode=mode, keep=keep)
    except MergeRefused as e:
        return None, (jsonify({"success": False, "error": str(e)}), e.status)
    refusal = grant_refusal(plan["folder"], plan["mapped_folder"])
    if refusal:
        return None, (jsonify({"success": False, "error": refusal[0]}), refusal[1])
    plan["series_id"] = row["series_id"]
    plan["series_name"] = row.get("series_name")
    return plan, None


@library_health_bp.route("/api/problem-files/series-conflicts/<int:conflict_id>/plan",
                         methods=["GET", "POST"])
def api_series_conflict_plan(conflict_id):
    """The compare view: both folders' comics, paired by issue, and what a merge would do.

    POST ``{mode, keep}`` is the confirmation step: the same plan with the
    user's keeper choices applied, so the dialog lists exactly what is trashed.
    """
    from core.series_conflict_merge import summary

    data = (request.get_json(silent=True) or {}) if request.method == "POST" else {}
    mode = data.get("mode") or request.args.get("mode") or "merge"
    keep = data.get("keep") or {}
    if not isinstance(keep, dict):
        return jsonify({"success": False, "error": "keep must be an object"}), 400
    plan, refused = _merge_plan_or_refusal(conflict_id, mode, keep)
    if refused:
        return refused
    return jsonify({"success": True, "plan": plan, "summary": summary(plan)})


@library_health_bp.route("/api/problem-files/series-conflicts/merge", methods=["POST"])
def api_series_conflict_merge():
    """Queue a merge (or a drop) on the file job queue.

    The request re-plans with the user's keeper choices and refuses when the
    result differs from the plan the user reviewed (``signature``), so nothing
    the dialog did not show can be moved or trashed.
    """
    from core import series_conflict_merge as merge
    from core.job_queue import file_jobs

    data = request.get_json(silent=True) or {}
    conflict_id = _conflict_id(data)
    if not conflict_id:
        return jsonify({"success": False, "error": "Missing id"}), 400
    mode = data.get("mode") or merge.MODE_MERGE
    keep = data.get("keep") or {}
    if not isinstance(keep, dict):
        return jsonify({"success": False, "error": "keep must be an object"}), 400
    signature = data.get("signature")
    if not signature:
        return jsonify({"success": False, "error": "Missing signature"}), 400

    plan, refused = _merge_plan_or_refusal(conflict_id, mode, keep)
    if refused:
        return refused
    if plan["signature"] != signature:
        return error_response(
            "The folders changed since you opened the comparison; review it again", code=409)
    if not plan["actions"]:
        return jsonify({"success": False, "error": "There is nothing to move or trash"}), 400

    rename = bool(data.get("rename")) and mode == merge.MODE_MERGE
    label = ("Merging " if mode == merge.MODE_MERGE else "Dropping duplicates from ") + \
        os.path.basename(plan["folder"])
    # The trash reads its folder and limits from app.config, and the queue's
    # worker thread has no app context of its own.
    app = current_app._get_current_object()

    def job(op):
        with app.app_context():
            merge.run(op, conflict_id, mode, keep, rename, signature)

    ahead = file_jobs.pending_count()
    op_id = file_jobs.submit("series_merge", label, len(plan["actions"]), job)
    return jsonify({"success": True, "op_id": op_id, "queued": ahead > 0})


# A dry run lists and pairs every folder in the request; bound it.
MAX_BULK_CONFLICTS = 100


def _bulk_ids(values):
    ids = []
    for value in values or []:
        cid = _conflict_id({"id": value})
        if cid and cid not in ids:
            ids.append(cid)
    return ids


@library_health_bp.route("/api/problem-files/series-conflicts/bulk-plan", methods=["POST"])
def api_series_conflict_bulk_plan():
    """Dry run for merging several conflicts with their default keepers.

    Every conflict is planned on its own; one that cannot be merged reports
    why and is left out of the totals rather than failing the whole dry run.
    """
    from core.series_conflict_merge import summary

    data = request.get_json(silent=True) or {}
    ids = _bulk_ids(data.get("ids"))
    if not ids:
        return jsonify({"success": False, "error": "Missing ids"}), 400
    if len(ids) > MAX_BULK_CONFLICTS:
        return jsonify({"success": False,
                        "error": f"Merge at most {MAX_BULK_CONFLICTS} series at once"}), 400

    items = []
    totals = {"series": 0, "move": 0, "trash": 0, "held": 0}
    for cid in ids:
        plan, refused = _merge_plan_or_refusal(cid, "merge")
        if refused:
            body = refused[0].get_json() or {}
            items.append({"id": cid, "ok": False, "error": body.get("error") or "Cannot be merged"})
            continue
        s = summary(plan)
        items.append({
            "id": cid, "ok": bool(plan["actions"]),
            "error": None if plan["actions"] else "Nothing to move or trash",
            "series_name": plan.get("series_name"),
            "folder": plan["folder"], "mapped_folder": plan["mapped_folder"],
            "summary": s, "folder_emptied": plan["folder_emptied"],
            "trash": [a["trash"] for a in plan["actions"] if a.get("trash")],
            "held": plan["held"], "signature": plan["signature"],
        })
        if plan["actions"]:
            totals["series"] += 1
            for key in ("move", "trash", "held"):
                totals[key] += s[key]
    return jsonify({"success": True, "items": items, "totals": totals})


@library_health_bp.route("/api/problem-files/series-conflicts/bulk-merge", methods=["POST"])
def api_series_conflict_bulk_merge():
    """Queue one job that merges every reviewed conflict with its default keepers.

    Each item carries the signature its dry run returned. Any mismatch refuses
    the whole request (409) before anything is queued: the dry run the user
    confirmed no longer describes what would happen.
    """
    from core import series_conflict_merge as merge
    from core.job_queue import file_jobs

    data = request.get_json(silent=True) or {}
    raw = data.get("items")
    if not isinstance(raw, list) or not raw:
        return jsonify({"success": False, "error": "Missing items"}), 400
    if len(raw) > MAX_BULK_CONFLICTS:
        return jsonify({"success": False,
                        "error": f"Merge at most {MAX_BULK_CONFLICTS} series at once"}), 400

    items, total = [], 0
    for entry in raw:
        cid = _conflict_id(entry if isinstance(entry, dict) else {})
        signature = entry.get("signature") if isinstance(entry, dict) else None
        if not cid or not signature:
            return jsonify({"success": False, "error": "Each item needs an id and a signature"}), 400
        plan, refused = _merge_plan_or_refusal(cid, "merge")
        if refused:
            return refused
        if plan["signature"] != signature:
            return error_response(
                "Some folders changed since the dry run; review it again", code=409)
        if plan["actions"]:
            items.append((cid, signature))
            total += len(plan["actions"])
    if not items:
        return jsonify({"success": False, "error": "There is nothing to move or trash"}), 400

    rename = bool(data.get("rename"))
    app = current_app._get_current_object()

    def job(op):
        with app.app_context():
            merge.run_group(op, items, rename)

    ahead = file_jobs.pending_count()
    op_id = file_jobs.submit("series_merge", f"Merging {len(items)} series", total, job)
    return jsonify({"success": True, "op_id": op_id, "queued": ahead > 0, "series": len(items)})


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
