"""Insights, Timeline and the reading-stats API.

These routes lived in app.py, which cannot be imported in tests, so none of
them had a route test. They moved here unchanged apart from the blueprint.

Routes:
    GET /insights                      -- the Insights page
    GET /api/insights                  -- flat JSON for the Homepage widget
    GET /api/reading-stats             -- read / pages / time, by year
    GET /api/reading-trends/<field>    -- top writers, artists, characters, publishers
    GET /api/recently-read             -- recent reads with thumbnails
    GET /timeline                      -- the reading Timeline page
    GET /api/timeline                  -- lazy-loaded Timeline pages
    GET /api/reading-insights/summary  -- goal, streaks, weeks, shelf
    GET /api/reading-insights/clock    -- reading time by hour and weekday
    GET /api/reading-insights/day/<d>  -- one day's sittings and reads
    POST /api/account/reading-goal     -- the user's yearly goal

``/api/insights`` is a login-exempt prefix (routes/auth.py), matched on a
segment boundary. Do not add a route under ``/api/insights/``: it would be
served without a login.
"""
import os
from datetime import date, datetime

from flask import (
    Blueprint, flash, jsonify, redirect, render_template, request, url_for,
)

from core.database import get_issues_read, get_user_preference
from models.stats import (
    get_file_type_distribution,
    get_largest_comics,
    get_library_stats,
    get_reading_heatmap_data,
    get_reading_history_stats,
    get_top_publishers,
    get_top_series_by_count,
)
from models.reading_insights import (
    MAX_GOAL,
    clock_for_period,
    day_detail,
    insights_summary,
    set_reading_goal,
)
from models.timeline import get_reading_timeline

insights_bp = Blueprint("insights", __name__)


@insights_bp.route("/insights")
def insights_page():
    from core.database import get_reading_totals

    library_stats = get_library_stats()
    file_types = get_file_type_distribution()
    top_publishers = get_top_publishers()
    reading_history = get_reading_history_stats()
    largest_comics = get_largest_comics()
    top_series = get_top_series_by_count()
    reading_heatmap = get_reading_heatmap_data()
    reading_totals = get_reading_totals()

    return render_template(
        "insights.html",
        library_stats=library_stats,
        file_types=file_types,
        top_publishers=top_publishers,
        reading_history=reading_history,
        largest_comics=largest_comics,
        top_series=top_series,
        reading_heatmap=reading_heatmap,
        reading_totals=reading_totals,
        rec_enabled=get_user_preference("rec_enabled", default=True),
    )


@insights_bp.route("/api/insights")
def api_insights():
    """Return library + per-user reading stats as JSON for the Homepage custom
    API widget (gethomepage.dev ``customapi`` type — a flat JSON object).

    The library-wide counts (files, size, root folders) are global. The reading
    counters (``issues_read``, ``pages_read``, ``time_reading*``) are per user:
    present a per-user API token as ``Authorization: Bearer <token>`` (the same
    tokens minted in Settings → Users for ``/api/v1``) and the numbers reflect
    that account. Homepage's ``customapi`` widget supports this via its
    per-widget ``headers`` option.

    With no token the counters reflect the Store Owner, matching the previous
    behaviour; a legacy global token also maps to the owner. A malformed or
    unknown token is rejected with 401 so a misconfigured widget doesn't
    silently surface the owner's stats.
    """
    from core.auth import resolve_optional_bearer_user
    from core.auth_throttle import too_many_attempts_json
    from models.stats import get_insights_stats

    status, user = resolve_optional_bearer_user(
        request.headers.get("Authorization", "")
    )
    if status == "throttled":
        return too_many_attempts_json(user)
    if status == "invalid":
        return jsonify({"error": "unauthorized"}), 401

    payload = get_insights_stats(user_id=user["id"] if user else None)
    if payload is None:
        return jsonify({"error": "Failed to get stats"}), 500

    return jsonify(payload)


@insights_bp.route("/api/reading-stats")
def api_reading_stats():
    """Get reading statistics, optionally filtered by year."""
    from core.database import get_reading_stats_by_year
    from wrapped import get_years_with_reading_data

    year = request.args.get("year")

    # If year is 'all' or empty, get all-time stats
    if year and year != "all":
        try:
            year = int(year)
        except ValueError:
            return jsonify({"success": False, "error": "Invalid year"}), 400
    else:
        year = None

    stats = get_reading_stats_by_year(year)

    # Get available years for the dropdown
    years = get_years_with_reading_data()
    current_year = datetime.now().year
    if current_year not in years:
        years.insert(0, current_year)

    # Calculate hours and minutes for display
    total_seconds = stats.get("total_time", 0)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60

    return jsonify(
        {
            "success": True,
            "stats": {
                "total_read": stats.get("total_read", 0),
                "total_pages": stats.get("total_pages", 0),
                "total_time": total_seconds,
                "hours": hours,
                "minutes": minutes,
            },
            "years": years,
            "selected_year": year if year else "all",
        }
    )


@insights_bp.route("/api/reading-trends/<field>")
def api_reading_trends(field):
    """Get top values for a metadata field (writer, penciller, characters, publisher)."""
    from core.database import get_reading_trends

    valid_fields = ["writer", "penciller", "characters", "publisher"]
    if field not in valid_fields:
        return jsonify(
            {"error": f"Invalid field. Must be one of: {', '.join(valid_fields)}"}
        ), 400

    year = request.args.get("year", type=int)
    limit = request.args.get("limit", 10, type=int)

    trends = get_reading_trends(field, year=year, limit=limit)
    return jsonify(trends)


@insights_bp.route("/api/recently-read")
def api_recently_read():
    """Get recently read issues with metadata for display."""
    limit = request.args.get("limit", 20, type=int)
    issues = get_issues_read()[:limit]

    result = []
    for issue in issues:
        path = issue["issue_path"]
        if os.path.exists(path):
            result.append(
                {
                    "name": os.path.basename(path),
                    "path": path,
                    "read_at": issue["read_at"],
                    "thumbnail_url": url_for("get_thumbnail", path=path),
                }
            )
    return jsonify(result)


@insights_bp.route("/timeline")
def timeline():
    limit = 50
    year = request.args.get("year", None, type=int)
    month = request.args.get("month", None, type=int)

    data = get_reading_timeline(limit=limit, offset=0, year=year, month=month)

    if not data:
        flash("Error loading timeline data", "error")
        return redirect(url_for("index"))

    # Years for the filter dropdown: this user's visible history only (the
    # unscoped query here listed years from every user's reads).
    from wrapped import get_years_with_reading_data

    available_years = get_years_with_reading_data()

    return render_template(
        "timeline.html",
        stats=data["stats"],
        timeline=data["timeline"],
        filter_year=year,
        filter_month=month,
        available_years=available_years,
    )


@insights_bp.route("/api/timeline")
def api_timeline():
    """API endpoint for lazy loading timeline data."""
    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", 50, type=int)
    year = request.args.get("year", None, type=int)
    month = request.args.get("month", None, type=int)

    data = get_reading_timeline(limit=limit, offset=offset, year=year, month=month)

    if not data:
        return jsonify({"error": "Failed to load timeline data"}), 500

    # Add thumbnail URLs to each item
    for group in data["timeline"]:
        for item in group["entries"]:
            if item.get("issue_path"):
                item["thumbnail_url"] = url_for(
                    "get_thumbnail", path=item["issue_path"]
                )
            else:
                item["thumbnail_url"] = None

    return jsonify(
        {
            "timeline": data["timeline"],
            "has_more": len(data["timeline"]) > 0,
            "stats": data["stats"],
        }
    )


# ---------------------------------------------------------------------------
# Reading insights: goal, streaks, weeks, shelf
# ---------------------------------------------------------------------------
# Under /api/reading-insights/, never /api/insights/: that prefix is exempt
# from login (see the module docstring).


@insights_bp.route("/api/reading-insights/summary")
def api_reading_insights_summary():
    """Goal progress, streaks, week comparison, five-week stack and shelf.

    ``?year=`` selects the year the goal dial describes (default: this year,
    in the user's timezone). Everything else is about now.
    """
    year = request.args.get("year", type=int)
    if year is not None and not 1900 <= year <= 2200:
        return jsonify({"success": False, "error": "Invalid year"}), 400

    summary = insights_summary(year=year)
    for item in summary["shelf"]["finished"] + summary["shelf"]["open"]:
        item["thumbnail_url"] = url_for("get_thumbnail", path=item["path"])
    return jsonify({"success": True, **summary})


# Under /api/account/ so a Reader may save their own goal: that prefix is what
# core.auth._READER_WRITE_PREFIXES allows for personal data.
@insights_bp.route("/api/account/reading-goal", methods=["POST"])
def account_save_reading_goal():
    """Set, change or clear (``goal`` 0 / null) the user's goal for a year."""
    body = request.get_json(silent=True) or {}
    try:
        year = int(body.get("year"))
        goal = int(body.get("goal") or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Year and goal must be numbers"}), 400
    if not 1900 <= year <= 2200:
        return jsonify({"success": False, "error": "Invalid year"}), 400
    if not 0 <= goal <= MAX_GOAL:
        return jsonify(
            {"success": False, "error": f"Goal must be between 0 and {MAX_GOAL:,}"}
        ), 400

    if not set_reading_goal(year, goal):
        return jsonify({"success": False, "error": "Could not save the goal"}), 500
    return jsonify({"success": True, "year": year, "goal": goal or None})


@insights_bp.route("/api/reading-insights/clock")
def api_reading_insights_clock():
    """Seconds of reading per hour of day and per weekday.

    ``?year=`` limits it to one local year; omitted or ``all`` is all time.
    """
    raw = request.args.get("year")
    year = None
    if raw and raw != "all":
        try:
            year = int(raw)
        except ValueError:
            return jsonify({"success": False, "error": "Invalid year"}), 400
        if not 1900 <= year <= 2200:
            return jsonify({"success": False, "error": "Invalid year"}), 400
    return jsonify({"success": True, "year": year, **clock_for_period(year=year)})


@insights_bp.route("/api/reading-insights/day/<day>")
def api_reading_insights_day(day):
    """One local day's sittings and finished issues, for the day view."""
    try:
        local_day = date.fromisoformat(day)
    except ValueError:
        return jsonify({"success": False, "error": "Invalid date"}), 400

    detail = day_detail(local_day)
    for item in detail["sessions"] + detail["reads"]:
        item["thumbnail_url"] = url_for("get_thumbnail", path=item["path"])
    return jsonify({"success": True, **detail})
