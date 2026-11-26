"""
Reading Position Blueprint

Manages the comic reader's "resume where I left off" bookmark:
``/api/reading-position`` (GET / POST / DELETE), the reader's "I finished
this" call (``/api/mark-comic-read``, moved here from app.py so it can be
tested), the Insights page's reading-history metadata backfill
(``/api/backfill-reading-metadata``), and the reader's page images and page
counts (``/api/read/<path>/...``, also moved from app.py).

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
import re
import traceback
import zipfile
import zlib

import rarfile
from flask import Blueprint, Response, current_app, request, jsonify, send_file

from core.app_logging import app_logger
from core.auth import enforce_path_access
from core.comic_pages import _extract_single_rar_entry, _is_valid_image_data
from helpers import describe_archive_error
from core.reading_sessions import note_reading_activity
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
        # A save past page 1 is evidence the comic is being read right now.
        # Below the page-1 early return on purpose: opening a comic to look at
        # the cover is not a sitting. Never fails the save.
        note_reading_activity(
            comic_path, page_number, total_pages,
            session_id=data.get("session_id"),
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
        # Close the reader's session. Only with a session id: this route is
        # also the grid's "Mark as read", which is not a sitting.
        if data.get("session_id"):
            note_reading_activity(
                comic_path, page_count or None, page_count or None,
                session_id=data.get("session_id"), finished=True,
            )
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


# ---------------------------------------------------------------------------
# Page images and page counts for the reader (/api/read/<path>/...)
# ---------------------------------------------------------------------------
# A page that cannot be served answers with error.svg and a 200, not a 4xx: it
# is an <img> src, and a broken-image icon tells the reader nothing.


def _error_image():
    return send_file(
        os.path.join(current_app.static_folder, "images", "error.svg"),
        mimetype="image/svg+xml",
    )


@reading_bp.route("/api/read/<path:comic_path>/page/<int:page_num>")
def read_comic_page(comic_path, page_num):
    """Serve a specific page from a comic file."""

    # Add leading slash if missing (for absolute paths on Unix systems)
    if not comic_path.startswith("/"):
        comic_path = "/" + comic_path

    denied = enforce_path_access(comic_path)
    if denied:
        return denied

    if not os.path.exists(comic_path):
        app_logger.error(f"Comic file not found: {comic_path}")
        return _error_image()

    try:
        # Determine archive type
        ext = os.path.splitext(comic_path)[1].lower()

        # Get list of image files from archive
        image_files = []
        archive = None

        if ext in [".cbz", ".zip"]:
            archive = zipfile.ZipFile(comic_path, "r")
            all_files = archive.namelist()
        elif ext == ".cbr":
            archive = rarfile.RarFile(comic_path, "r")
            all_files = archive.namelist()
        else:
            return jsonify({"error": "Unsupported file format"}), 400

        # Filter for image files
        image_extensions = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
        for filename in all_files:
            if filename.lower().endswith(image_extensions):
                # Skip macOS metadata files
                if not filename.startswith("__MACOSX") and not os.path.basename(
                    filename
                ).startswith("."):
                    image_files.append(filename)

        # Sort naturally
        def natural_sort_key(s):
            return [
                int(text) if text.isdigit() else text.lower()
                for text in re.split("([0-9]+)", s)
            ]

        image_files.sort(key=natural_sort_key)

        # Check if page number is valid
        if page_num < 0 or page_num >= len(image_files):
            return jsonify({"error": "Invalid page number"}), 400

        # Read the requested page
        target_file = image_files[page_num]
        image_data = None
        rar_read_failed = False

        if ext == ".cbr":
            try:
                image_data = archive.read(target_file)
                if not _is_valid_image_data(image_data):
                    app_logger.warning(
                        f"rarfile returned truncated data for {target_file} from {comic_path}"
                    )
                    image_data = None
                    rar_read_failed = True
            except Exception as exc:
                app_logger.warning(
                    f"rarfile failed to read {target_file} from {comic_path} "
                    f"({type(exc).__name__}), falling back to subprocess extraction"
                )
                rar_read_failed = True

            if rar_read_failed:
                if archive:
                    archive.close()
                    archive = None
                image_data = _extract_single_rar_entry(comic_path, target_file)
                if image_data is None:
                    return _error_image()
        else:
            image_data = archive.read(target_file)

        # Close archive
        if archive:
            archive.close()

        # Determine mime type
        file_ext = os.path.splitext(target_file)[1].lower()
        mime_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }
        mime_type = mime_types.get(file_ext, "image/jpeg")

        # Return image
        return Response(image_data, mimetype=mime_type)

    except (zipfile.BadZipFile, zlib.error) as e:
        app_logger.warning(
            f"Damaged archive, cannot read page {page_num} from {comic_path}: "
            f"{describe_archive_error(e)}"
        )
        if archive:
            archive.close()
        return _error_image()

    except Exception as e:
        app_logger.error(f"Error reading comic page {page_num} from {comic_path}: {e}")
        app_logger.error(traceback.format_exc())
        if archive:
            archive.close()
        return _error_image()


@reading_bp.route("/api/read/<path:comic_path>/page/<int:page_num>/info")
def read_comic_page_info(comic_path, page_num):
    """Get information about a specific page in a comic file."""

    # Add leading slash if missing (for absolute paths on Unix systems)
    if not comic_path.startswith("/"):
        comic_path = "/" + comic_path

    denied = enforce_path_access(comic_path)
    if denied:
        return denied

    if not os.path.exists(comic_path):
        return jsonify({"success": False, "error": "File not found"}), 404

    try:
        ext = os.path.splitext(comic_path)[1].lower()
        archive = None

        if ext in [".cbz", ".zip"]:
            archive = zipfile.ZipFile(comic_path, "r")
        elif ext == ".cbr":
            archive = rarfile.RarFile(comic_path, "r")
        else:
            return jsonify({"success": False, "error": "Unsupported format"}), 400

        # Get list of image files
        image_extensions = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
        image_files = []
        for filename in archive.namelist():
            if filename.lower().endswith(image_extensions):
                if not filename.startswith("__MACOSX") and not os.path.basename(
                    filename
                ).startswith("."):
                    image_files.append(filename)

        # Sort naturally
        def natural_sort_key(s):
            return [
                int(text) if text.isdigit() else text.lower()
                for text in re.split("([0-9]+)", s)
            ]

        image_files.sort(key=natural_sort_key)

        if page_num < 0 or page_num >= len(image_files):
            archive.close()
            return jsonify({"success": False, "error": "Invalid page number"}), 400

        target_file = image_files[page_num]

        # Get file info from archive
        info = archive.getinfo(target_file)
        file_size = info.file_size if hasattr(info, "file_size") else info.compress_size
        file_name = os.path.basename(target_file)

        archive.close()

        return jsonify(
            {
                "success": True,
                "page_num": page_num,
                "file_name": file_name,
                "file_size": file_size,
                "archive_path": target_file,
            }
        )

    except Exception as e:
        detail = describe_archive_error(e)
        app_logger.error(
            f"Error getting page info for {comic_path} page {page_num}: {detail}"
        )
        if archive:
            archive.close()
        return jsonify({"success": False, "error": detail}), 500


@reading_bp.route("/api/read/<path:comic_path>/info")
def read_comic_info(comic_path):
    """Get information about a comic file (page count, etc.)."""

    # Add leading slash if missing (for absolute paths on Unix systems)
    if not comic_path.startswith("/"):
        comic_path = "/" + comic_path

    denied = enforce_path_access(comic_path)
    if denied:
        return denied

    if not os.path.exists(comic_path):
        return jsonify({"error": "Comic file not found"}), 404

    try:
        # Determine archive type
        ext = os.path.splitext(comic_path)[1].lower()

        # Get list of image files from archive
        image_files = []

        if ext in [".cbz", ".zip"]:
            with zipfile.ZipFile(comic_path, "r") as archive:
                all_files = archive.namelist()
        elif ext == ".cbr":
            with rarfile.RarFile(comic_path, "r") as archive:
                all_files = archive.namelist()
        else:
            return jsonify({"error": "Unsupported file format"}), 400

        # Filter for image files
        image_extensions = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
        for filename in all_files:
            if filename.lower().endswith(image_extensions):
                # Skip macOS metadata files
                if not filename.startswith("__MACOSX") and not os.path.basename(
                    filename
                ).startswith("."):
                    image_files.append(filename)

        return jsonify(
            {
                "success": True,
                "page_count": len(image_files),
                "filename": os.path.basename(comic_path),
            }
        )

    except Exception as e:
        app_logger.error(f"Error getting comic info for {comic_path}: {e}")
        app_logger.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500
