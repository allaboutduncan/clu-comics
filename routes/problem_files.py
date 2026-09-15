"""The Problem Files page and its API.

A blueprint rather than routes in ``app.py`` so the logic is directly testable:
``app.py`` cannot be imported in tests, and anything left there is only
reachable through AST assertions.

Owner-only, enforced centrally — ``/problem-files`` and ``/api/problem-files``
are both in ``core.auth._OWNER_PREFIXES``, so there are no decorators here.

Heavy imports are done inside the views, matching ``routes/download_clients.py``,
so the module stays cheap to import and the symbols stay patchable in tests.
"""

import os

from flask import Blueprint, current_app, jsonify, render_template, request

from core.app_logging import app_logger

problem_files_bp = Blueprint("problem_files", __name__)


@problem_files_bp.route("/problem-files")
def page():
    """The Problem Files page."""
    return render_template("problem_files.html", current_page="/problem-files")


@problem_files_bp.route("/api/problem-files", methods=["GET"])
def api_list():
    """List recorded problems, newest failure first."""
    from core.problem_files import KNOWN_SOURCES, count_problems, list_problems

    source = request.args.get("source") or None
    if source and source not in KNOWN_SOURCES:
        return jsonify({"success": False, "error": f"Unknown source: {source}"}), 400

    include_dismissed = request.args.get("include_dismissed") in ("1", "true", "yes")
    query = request.args.get("q") or None
    try:
        limit = min(int(request.args.get("limit", 500)), 1000)
        offset = max(int(request.args.get("offset", 0)), 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid limit or offset"}), 400

    problems = list_problems(
        source=source,
        include_dismissed=include_dismissed,
        query=query,
        limit=limit,
        offset=offset,
    )
    if problems is None:
        # Not the same as "nothing is wrong". Saying so lets the page show an
        # error instead of asserting "Nothing has failed" over a database it
        # could not read.
        return jsonify({
            "success": False,
            "error": "Could not read the problem-files list",
        }), 500

    from core.problem_replacements import list_replacements

    _attach_search_context(problems)
    replacements = list_replacements()
    by_path = {r["target_path"]: r for r in replacements}
    for row in problems:
        row["replacement"] = by_path.get(row["path"])

    return jsonify(
        {
            "success": True,
            "problems": problems,
            "counts": count_problems(),
            # Carried separately as well: an applied swap deletes the problem
            # row (the file is fixed), so the only place left to report it is
            # the page's own banner.
            "replacements": replacements,
        }
    )


def _attach_search_context(problems):
    """Give each row the series/issue/year a source search needs.

    Parsed here rather than in the browser, and through
    `cbz_ops.rename.parse_comic_filename` rather than a regex of this page's
    own, so "search for a replacement" cannot disagree with the parser the rest
    of the app renames and matches files with. Best-effort: an unparseable name
    still gets a query (the bare filename), because a search the user can edit
    beats no button at all.
    """
    try:
        from cbz_ops.rename import parse_comic_filename

        custom_pattern = current_app.config.get("CUSTOM_RENAME_PATTERN", "")
    except Exception as e:
        app_logger.error(f"Could not load the filename parser: {e}")
        return

    for row in problems:
        filename = row.get("filename") or ""
        series, issue, year = "", "", None
        try:
            parsed = parse_comic_filename(filename, custom_pattern=custom_pattern or None)
            series = (parsed.get("series_name") or "").strip()
            issue = (parsed.get("issue_number") or "").strip()
            year = parsed.get("year")
        except Exception as e:
            app_logger.warning(f"Could not parse {filename} for search: {e}")

        if series:
            query = f"{series} {issue}".strip()
        else:
            # No parse: fall back to the stem, which is still a better starting
            # point than an empty box.
            query = os.path.splitext(filename)[0]

        row["search"] = {
            "series": series,
            "issue": issue,
            "year": year,
            "query": query,
        }


def _path_and_source(require_source=True):
    """Pull and validate ``path``/``source`` from a JSON body.

    Returns ``(path, source, error_response)``; the caller returns the error if
    it is not None.
    """
    from core.problem_files import KNOWN_SOURCES

    data = request.get_json(silent=True) or {}
    path = data.get("path")
    source = data.get("source")

    if not path:
        return None, None, (jsonify({"success": False, "error": "Missing path"}), 400)
    if require_source and not source:
        return None, None, (jsonify({"success": False, "error": "Missing source"}), 400)
    if source and source not in KNOWN_SOURCES:
        return None, None, (
            jsonify({"success": False, "error": f"Unknown source: {source}"}),
            400,
        )
    return path, source, None


@problem_files_bp.route("/api/problem-files/retry", methods=["POST"])
def api_retry():
    """Re-run the operation that recorded this problem.

    Synchronous: one file, one fast read. See ``core.problem_files.retry_problem``
    for why this does not go through the operations registry.
    """
    from core.problem_files import get_problem, retry_problem

    path, source, error = _path_and_source()
    if error:
        return error

    ok, message = retry_problem(path, source)
    # The retried operation records or clears its own row, so the row's current
    # state is the answer -- never a guess from the return value.
    remaining = get_problem(path, source)
    return jsonify(
        {
            "success": True,
            "fixed": ok and remaining is None,
            "message": message,
            "problem": remaining,
        }
    )


@problem_files_bp.route("/api/problem-files/dismiss", methods=["POST"])
def api_dismiss():
    """Hide or un-hide an entry without touching the file."""
    from core.problem_files import set_dismissed

    path, source, error = _path_and_source()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    dismissed = data.get("dismissed", True)
    updated = set_dismissed(path, source, dismissed=bool(dismissed))
    if not updated:
        return jsonify({"success": False, "error": "No such entry"}), 404
    return jsonify({"success": True, "dismissed": bool(dismissed)})


@problem_files_bp.route("/api/problem-files/remove", methods=["POST"])
def api_remove():
    """Drop an entry from the list, leaving the file alone.

    For a problem the user has fixed outside CLU. Omitting ``source`` removes
    every entry for the path.
    """
    from core.problem_files import clear_problem

    path, source, error = _path_and_source(require_source=False)
    if error:
        return error

    removed = clear_problem(path, source)
    return jsonify({"success": True, "removed": removed})


@problem_files_bp.route("/api/problem-files/delete", methods=["POST"])
def api_delete():
    """Send the file to trash and drop all of its entries.

    Reuses the same primitives as the File Manager rather than re-implementing
    them: ``is_critical_path`` guards the target, ``move_to_trash`` handles the
    size-capped trash and its restore manifest.
    """
    from helpers.library import is_critical_path
    from helpers.trash import move_to_trash
    from core.problem_files import clear_problem, has_problem

    path, _source, error = _path_and_source(require_source=False)
    if error:
        return error

    # Only ever delete something this page is actually listing. is_critical_path
    # guards WATCH/TARGET/trash but not /config or /cache, and this endpoint
    # otherwise takes an arbitrary path.
    if not has_problem(path):
        return jsonify({"success": False, "error": "Not a listed problem file"}), 404

    if not os.path.exists(path):
        # Nothing to delete, but the stale entry is still worth clearing.
        clear_problem(path)
        return jsonify({"success": True, "trashed": False, "missing": True})

    if is_critical_path(path):
        app_logger.error(f"Refused to delete critical path from Problem Files: {path}")
        return jsonify({"success": False, "error": "That path is protected"}), 403

    try:
        result = move_to_trash(path)
    except Exception as e:
        app_logger.error(f"Error deleting problem file {path}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

    try:
        from app import update_index_on_delete

        update_index_on_delete(path)
    except Exception as e:
        app_logger.error(f"Could not update file index after deleting {path}: {e}")

    clear_problem(path)
    return jsonify({"success": True, "trashed": result.get("trashed", False)})


# ---------------------------------------------------------------------------
# Replacements
# ---------------------------------------------------------------------------

@problem_files_bp.route("/api/problem-files/replace", methods=["POST"])
def api_claim_replacement():
    """Record that a queued download is meant to replace this damaged file.

    Called the moment the user queues something from the search modal. The
    damaged file's own path is the destination, which is the whole reason this
    works: the wanted sweep only files issues that are *missing*, and a corrupt
    file is still a file, so nothing else would ever claim the download.
    """
    from core.problem_replacements import claim_replacement

    path, _source, error = _path_and_source(require_source=False)
    if error:
        return error

    data = request.get_json(silent=True) or {}
    ok = claim_replacement(
        path,
        series=data.get("series", ""),
        issue=data.get("issue", ""),
        query=data.get("query", ""),
        source=data.get("download_source", ""),
    )
    if not ok:
        return jsonify({"success": False, "error": "Could not record the replacement"}), 500
    return jsonify({"success": True})


@problem_files_bp.route("/api/problem-files/replacements/apply", methods=["POST"])
def api_apply_replacements():
    """Run the replacement pass now.

    The page polls this while a swap is outstanding. The pass also runs from
    the download pipeline, so closing the page does not strand a download --
    both callers go through core.problem_replacements so they cannot drift.
    """
    from core.problem_replacements import apply_pending_for_app, list_replacements

    changed = apply_pending_for_app(current_app.config)
    return jsonify(
        {"success": True, "changed": changed, "replacements": list_replacements()}
    )


@problem_files_bp.route("/api/problem-files/replacements/ack", methods=["POST"])
def api_ack_replacement():
    """Dismiss a finished replacement from the banner, or cancel a pending one."""
    from core.problem_replacements import acknowledge, cancel

    path, _source, error = _path_and_source(require_source=False)
    if error:
        return error

    data = request.get_json(silent=True) or {}
    if data.get("cancel"):
        return jsonify({"success": True, "cancelled": cancel(path)})
    return jsonify({"success": True, "acknowledged": acknowledge(path)})
