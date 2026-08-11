"""
Reading Position Blueprint

Manages the comic reader's "resume where I left off" bookmark:
``/api/reading-position`` (GET / POST / DELETE).

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

from flask import Blueprint, request, jsonify

from core.auth import enforce_path_access
from core.database import (
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
