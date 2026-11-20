"""
Reading Position Blueprint

Manages the comic reader's "resume where I left off" bookmark:
``/api/reading-position`` (GET / POST / DELETE), the reader's "I finished
this" call (``/api/mark-comic-read``, moved here from app.py so it can be
tested), and the Insights page's reading-history metadata backfill
(``/api/backfill-reading-metadata``).

Contract notes for anyone touching this file:

- ``page_number`` is **1-indexed** — page 1 is the first page, matching what the
  reader displays. ``core.database.save_reading_position`` stores it verbatim.
- A ``page_number`` of 0 or 1 means "the user is at the start", which is not a
  bookmark worth keeping, so POST *deletes* the row instead of storing it. The
  reader relies on this: ``navigator.sendBeacon`` can only issue a POST, so a
  page-unload flush must be able to express "clear my bookmark" without a
  DELETE. This rule lives here and NOT in ``save_reading_position`` so that
  ``PUT /api/v1/progress`` keeps its literal store-what-you-are-given semantics.
- POST tolerates a body sent with any Content-Type. ``sendBeacon`` is given a
  Blob typed ``application/json``, but some user agents drop the type and the
  request arrives as ``text/plain``; rejecting those would silently lose exactly
  the saves this endpoint exists to capture.
"""

import json
import os

from flask import Blueprint, current_app, request, jsonify

from core.app_logging import app_logger
from core.auth import enforce_path_access
from core.database import (
    clear_stats_cache_prefix,
    current_user_id,
    mark_issue_read,
    save_reading_position,
    get_reading_position,
    delete_reading_position,
)

reading_bp = Blueprint("reading", __name__)


def _request_payload():
    """Return the POST body as a dict, regardless of Content-Type.

    Falls back to a manual ``json.loads`` so a beacon that arrives as
    ``text/plain`` is still honoured. Returns ``None`` when the body is not
    usable JSON.
    """
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    try:
        raw = request.get_data() or b"{}"
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _coerce_int(value, field):
    """Coerce ``value`` to int, raising ValueError with a field-named message.

    Without this a client can store a string page number, which then poisons the
    ``page_number < total_pages - 1`` comparison in ``get_continue_reading_items``.
    """
    if isinstance(value, bool):
        raise ValueError(f"Invalid {field}")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {field}")


@reading_bp.route("/api/reading-position", methods=["GET", "POST", "DELETE"])
def api_reading_position():
    """
    Manage reading position bookmarks.

    GET    ?path=<abs path>  -> {"page_number", "total_pages", "updated_at", "time_spent"}
                                or {"page_number": None} when there is no bookmark.
    POST   {comic_path, page_number, total_pages?, time_spent?}
                             -> {"success": bool} or {"success": True, "cleared": True}
                                when page_number <= 1 cleared the bookmark.
    DELETE ?path=<abs path>  -> {"success": bool}
    """
    if request.method == "GET":
        comic_path = request.args.get("path")
        if not comic_path:
            return jsonify({"error": "Missing path parameter"}), 400

        denied = enforce_path_access(comic_path)
        if denied:
            return denied

        position = get_reading_position(comic_path)
        if position:
            return jsonify(
                {
                    "page_number": position["page_number"],
                    "total_pages": position["total_pages"],
                    "updated_at": position["updated_at"],
                    "time_spent": position.get("time_spent", 0),
                }
            )
        return jsonify({"page_number": None})

    elif request.method == "POST":
        data = _request_payload()
        if data is None:
            return jsonify({"error": "Invalid or missing JSON body"}), 400

        comic_path = data.get("comic_path")
        page_number = data.get("page_number")

        if not comic_path or page_number is None:
            return jsonify({"error": "Missing comic_path or page_number"}), 400

        denied = enforce_path_access(comic_path)
        if denied:
            return denied

        try:
            page_number = _coerce_int(page_number, "page_number")
            total_pages = data.get("total_pages")
            if total_pages is not None:
                total_pages = _coerce_int(total_pages, "total_pages")
            time_spent = data.get("time_spent", 0)
            time_spent = _coerce_int(time_spent, "time_spent") if time_spent else 0
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        # Page 0/1 is the start of the book, not a bookmark. Clearing here is
        # what lets a beacon-only POST express "start over" (see module docstring).
        if page_number <= 1:
            delete_reading_position(comic_path)
            return jsonify({"success": True, "cleared": True})

        if total_pages is not None and total_pages > 0:
            page_number = max(1, min(page_number, total_pages))

        success = save_reading_position(
            comic_path, page_number, total_pages, time_spent
        )
        return jsonify({"success": success})

    elif request.method == "DELETE":
        comic_path = request.args.get("path")
        if not comic_path:
            return jsonify({"error": "Missing path parameter"}), 400

        denied = enforce_path_access(comic_path)
        if denied:
            return denied

        success = delete_reading_position(comic_path)
        return jsonify({"success": success})


@reading_bp.route("/api/mark-comic-read", methods=["POST"])
def api_mark_comic_read():
    """Mark a comic as read in the database."""
    data = request.get_json()
    comic_path = data.get("path")
    read_at = data.get("read_at")  # Optional ISO timestamp for backfilling
    page_count = data.get("page_count", 0)
    time_spent = data.get("time_spent", 0)

    if not comic_path:
        return jsonify({"error": "Missing path parameter"}), 400

    # Extract metadata from ComicInfo.xml if available
    comic_info = None
    writer = ""
    penciller = ""
    characters = ""
    publisher = ""
    try:
        from core.comicinfo import read_comicinfo_from_zip

        if os.path.exists(comic_path) and comic_path.lower().endswith((".cbz", ".zip")):
            comic_info = read_comicinfo_from_zip(comic_path)
            if comic_info:
                writer = comic_info.get("Writer", "")
                penciller = comic_info.get("Penciller", "")
                characters = comic_info.get("Characters", "")
                publisher = comic_info.get("Publisher", "")
    except Exception as e:
        app_logger.warning(f"Could not extract ComicInfo.xml metadata: {e}")

    try:
        mark_issue_read(
            comic_path,
            read_at,
            page_count,
            time_spent,
            writer=writer,
            penciller=penciller,
            characters=characters,
            publisher=publisher,
        )
        clear_stats_cache_prefix(f"u{current_user_id()}:")  # per-user reading caches
        app_logger.info(
            f"Marked comic as read: {comic_path}"
            + (f" at {read_at}" if read_at else "")
        )
    except Exception as e:
        app_logger.error(f"Error marking comic as read: {e}")
        return jsonify({"error": str(e)}), 500

    # Scrobble to Metron if configured (non-blocking — local read already saved)
    try:
        from models import metron as metron_module

        api = metron_module.get_flask_api(current_app)
        if api:
            metron_issue_id = metron_module.resolve_metron_issue_id(
                api, comic_path, comic_info.get("Number") if comic_info else None
            )
            if metron_issue_id:
                metron_module.scrobble_issue(api, metron_issue_id, read_at)
                app_logger.info(f"Scrobbled to Metron: issue {metron_issue_id}")
    except Exception as e:
        app_logger.warning(f"Metron scrobble failed (non-blocking): {e}")

    return jsonify({"success": True})


@reading_bp.route("/api/backfill-reading-metadata", methods=["POST"])
def api_backfill_reading_metadata():
    """Start re-reading ComicInfo.xml for every comic in the reading history.

    Runs off-request (``core.reading_metadata_backfill``): opening every archive
    in the history can far outlast gunicorn's timeout, and it used to do so
    while holding the database write lock. Answers ``{success, op_id}``; the
    page follows ``/api/operation/<op_id>`` (never ``/api/operations``) and
    then reads the report from ``/api/backfill-reading-metadata/result``.
    409 while a run is already going.
    """
    from core.database import current_user_id
    from core.reading_metadata_backfill import start_backfill

    op_id = start_backfill(user_id=current_user_id())
    if op_id is None:
        return jsonify({"success": False,
                        "error": "A metadata backfill is already running"}), 409
    return jsonify({"success": True, "op_id": op_id})


@reading_bp.route("/api/backfill-reading-metadata/result", methods=["GET"])
def api_backfill_reading_metadata_result():
    """The caller's last backfill report, or ``result: null`` if none."""
    from core.database import current_user_id
    from core.reading_metadata_backfill import is_running, last_result

    return jsonify({
        "success": True,
        "running": is_running(),
        "result": last_result(user_id=current_user_id()),
    })
