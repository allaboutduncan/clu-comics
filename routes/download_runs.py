"""Reports of the automatic download runs, for the Status page.

``core/download_runs.py`` records what each GetComics sweep and weekly-pack run
did; this blueprint lists them and exports one as JSON or CSV.

Clerk-level, enforced centrally -- ``/api/download-runs`` is in
``core.auth._CLERK_PREFIXES``, the same level as ``/status`` itself -- so there
are no decorators here.
"""

import csv
import io
import json

from flask import Blueprint, Response, jsonify, request

download_runs_bp = Blueprint("download_runs", __name__)

EXPORT_FORMATS = ("json", "csv")

CSV_COLUMNS = (
    "series_name", "issue_number", "source", "provider", "outcome",
    "download_status", "detail", "series_id", "download_id", "updated_at",
)


def _with_display_times(run):
    """Add the user's-offset rendering of each timestamp beside the UTC one."""
    from core.user_time import format_user_time

    for key in ("started_at", "finished_at"):
        run[f"{key}_display"] = format_user_time(run.get(key))
    return run


@download_runs_bp.route("/api/download-runs", methods=["GET"])
def api_list_runs():
    """Recent runs, newest first, with their counters and download tallies."""
    from core.download_runs import KINDS, list_runs

    kind = request.args.get("kind") or None
    if kind and kind not in KINDS:
        return jsonify({"success": False, "error": f"Unknown kind: {kind}"}), 400
    try:
        limit = int(request.args.get("limit", 30))
    except ValueError:
        return jsonify({"success": False, "error": "limit must be a number"}), 400

    runs = list_runs(kind=kind, limit=limit)
    if runs is None:
        # Never [] -- an empty list has to mean "no runs yet".
        return jsonify({"success": False, "error": "Could not read run history"}), 500
    return jsonify({"success": True, "runs": [_with_display_times(r) for r in runs]})


def _load(run_id, item_filter):
    """(run, None) or (None, error response)."""
    from core.download_runs import get_run

    try:
        run = get_run(run_id, item_filter)
    except ValueError as e:
        return None, (jsonify({"success": False, "error": str(e)}), 400)
    if run is None:
        return None, (jsonify({"success": False, "error": "Run not found"}), 404)
    return _with_display_times(run), None


@download_runs_bp.route("/api/download-runs/<int:run_id>", methods=["GET"])
def api_get_run(run_id):
    """One run and its items, optionally filtered (all/queued/downloaded/...)."""
    run, err = _load(run_id, request.args.get("filter") or "all")
    if err:
        return err
    return jsonify({"success": True, "run": run})


@download_runs_bp.route("/api/download-runs/<int:run_id>/export", methods=["GET"])
def api_export_run(run_id):
    """Download one run's report as a JSON or CSV attachment."""
    fmt = (request.args.get("format") or "json").lower()
    if fmt not in EXPORT_FORMATS:
        return jsonify({"success": False, "error": f"Unknown format: {fmt}"}), 400
    run, err = _load(run_id, request.args.get("filter") or "all")
    if err:
        return err

    stem = f"clu-{run['kind']}-run-{run_id}"
    if fmt == "json":
        body = json.dumps(run, indent=2, sort_keys=True)
        mimetype = "application/json"
    else:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(run["items"])
        body = buf.getvalue()
        mimetype = "text/csv"
    return Response(
        body,
        mimetype=mimetype,
        headers={"Content-Disposition": f'attachment; filename="{stem}.{fmt}"'},
    )
