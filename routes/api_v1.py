"""
v1 JSON API for the offline mobile/desktop client.

All endpoints are mounted under /api/v1/ and require a long-lived bearer
token. The token is generated server-side and stored in user_preferences
(key='api_token'). When no token has been generated, the entire blueprint
returns 503 so it cannot be probed.

Identity contract:
- Browse / cover / download endpoints accept file_index.id (integer).
- Reading-progress endpoints accept comic_path (the absolute path the
  server has on disk), matching the existing reading_positions UNIQUE key.
"""

import hmac
import os
from urllib.parse import unquote

from flask import Blueprint, jsonify, request, Response

from core.app_logging import app_logger
from core.database import (
    get_api_token,
    get_db_connection,
    get_reading_position,
    get_reading_positions_since,
    mark_issue_read,
    metadata_browse,
    save_reading_position,
)
from core.version import __version__
from helpers import create_thumbnail_streaming, serve_comic_file


api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@api_v1_bp.before_request
def _require_api_token():
    token = get_api_token()
    if not token:
        return jsonify({
            "error": "api_disabled",
            "message": (
                "API token is not set. Generate one with: "
                "python -m flask --app app rotate-api-token"
            ),
        }), 503

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "unauthorized"}), 401

    presented = auth_header[len("Bearer "):].strip()
    if not hmac.compare_digest(presented, token):
        return jsonify({"error": "unauthorized"}), 401

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_row_by_id(file_id):
    conn = get_db_connection()
    if not conn:
        return None
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, name, path, size, modified_at, has_comicinfo,
                   ci_title, ci_series, ci_number, ci_count, ci_volume,
                   ci_year, ci_writer, ci_penciller, ci_inker, ci_colorist,
                   ci_letterer, ci_coverartist, ci_publisher, ci_genre,
                   ci_characters
            FROM file_index
            WHERE id = ?
            """,
            (file_id,),
        )
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _paginate_args():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(200, max(1, int(request.args.get("page_size", 50))))
    except (TypeError, ValueError):
        page_size = 50
    return page, page_size, (page - 1) * page_size


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@api_v1_bp.route("/auth/ping", methods=["GET"])
def ping():
    return jsonify({"ok": True, "version": __version__})


@api_v1_bp.route("/library/publishers", methods=["GET"])
def list_publishers():
    page, page_size, offset = _paginate_args()
    sort = request.args.get("sort", "alpha")
    if sort not in ("alpha", "count"):
        sort = "alpha"
    result = metadata_browse(
        axis="publisher",
        filters={},
        sort=sort,
        offset=offset,
        limit=page_size,
    )
    return jsonify({
        "items": result.get("items", []),
        "total": result.get("total", 0),
        "page": page,
        "page_size": page_size,
    })


@api_v1_bp.route("/library/series", methods=["GET"])
def list_series():
    page, page_size, offset = _paginate_args()
    sort = request.args.get("sort", "alpha")
    if sort not in ("alpha", "count", "year", "recent"):
        sort = "alpha"

    filters = {}
    publisher = request.args.get("publisher")
    if publisher:
        filters["publisher"] = [publisher]
    search = request.args.get("q") or request.args.get("search")
    if search:
        filters["search"] = search

    result = metadata_browse(
        axis="series",
        filters=filters,
        sort=sort,
        offset=offset,
        limit=page_size,
    )
    return jsonify({
        "items": result.get("items", []),
        "total": result.get("total", 0),
        "page": page,
        "page_size": page_size,
    })


@api_v1_bp.route("/library/issues", methods=["GET"])
def list_issues():
    page, page_size, offset = _paginate_args()
    sort = request.args.get("sort", "alpha")
    if sort not in ("alpha", "year", "recent"):
        sort = "alpha"

    series = request.args.get("series")
    if not series:
        return jsonify({"error": "Missing 'series' parameter"}), 400

    filters = {"series": [series]}
    publisher = request.args.get("publisher")
    if publisher:
        filters["publisher"] = [publisher]

    result = metadata_browse(
        axis="issue",
        filters=filters,
        sort=sort,
        offset=offset,
        limit=page_size,
    )

    items = result.get("items", [])
    paths = [it.get("path") for it in items if it.get("path")]
    progress_map = {}
    if paths:
        conn = get_db_connection()
        if conn:
            try:
                placeholders = ",".join(["?"] * len(paths))
                c = conn.cursor()
                c.execute(
                    f"SELECT comic_path, page_number, total_pages "
                    f"FROM reading_positions WHERE comic_path IN ({placeholders})",
                    paths,
                )
                for row in c.fetchall():
                    progress_map[row["comic_path"]] = {
                        "page_number": row["page_number"],
                        "total_pages": row["total_pages"],
                    }
            finally:
                conn.close()

    enriched = []
    for it in items:
        path = it.get("path")
        prog = progress_map.get(path) if path else None
        enriched.append({
            **it,
            "id": _id_for_path(path) if path else None,
            "has_progress": prog is not None,
            "last_page": prog["page_number"] if prog else None,
        })

    return jsonify({
        "items": enriched,
        "total": result.get("total", 0),
        "page": page,
        "page_size": page_size,
    })


def _id_for_path(path):
    conn = get_db_connection()
    if not conn:
        return None
    try:
        c = conn.cursor()
        c.execute("SELECT id FROM file_index WHERE path = ?", (path,))
        row = c.fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


@api_v1_bp.route("/issue/<int:file_id>", methods=["GET"])
def get_issue(file_id):
    row = _file_row_by_id(file_id)
    if not row:
        return jsonify({"error": "not_found"}), 404

    progress = get_reading_position(row["path"])
    return jsonify({
        "id": row["id"],
        "name": row["name"],
        "path": row["path"],
        "size": row.get("size") or 0,
        "modified_at": row.get("modified_at"),
        "has_comicinfo": row.get("has_comicinfo"),
        "metadata": {
            "title": row.get("ci_title") or "",
            "series": row.get("ci_series") or "",
            "number": row.get("ci_number") or "",
            "count": row.get("ci_count") or "",
            "volume": row.get("ci_volume") or "",
            "year": row.get("ci_year") or "",
            "writer": row.get("ci_writer") or "",
            "penciller": row.get("ci_penciller") or "",
            "inker": row.get("ci_inker") or "",
            "colorist": row.get("ci_colorist") or "",
            "letterer": row.get("ci_letterer") or "",
            "coverartist": row.get("ci_coverartist") or "",
            "publisher": row.get("ci_publisher") or "",
            "genre": row.get("ci_genre") or "",
            "characters": row.get("ci_characters") or "",
        },
        "progress": progress,
    })


@api_v1_bp.route("/issue/<int:file_id>/cover", methods=["GET"])
def get_issue_cover(file_id):
    row = _file_row_by_id(file_id)
    if not row:
        return jsonify({"error": "not_found"}), 404
    file_path = row["path"]
    if not os.path.exists(file_path):
        return jsonify({"error": "not_found"}), 404

    try:
        max_size = int(request.args.get("size", 400))
    except (TypeError, ValueError):
        max_size = 400
    max_size = max(64, min(2000, max_size))

    # Extract first image from CBZ for cover. For CBR/PDF/EPUB we fall back
    # to a 404 — those rarely live as primary library files and the helper
    # handles only direct image paths in the streaming thumbnail call.
    ext = os.path.splitext(file_path)[1].lower()
    if ext != ".cbz" and ext != ".zip":
        return jsonify({"error": "cover_unavailable"}), 404

    try:
        import zipfile
        with zipfile.ZipFile(file_path) as zf:
            image_names = [
                n for n in zf.namelist()
                if n.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                and not n.startswith("__MACOSX")
            ]
            if not image_names:
                return jsonify({"error": "cover_unavailable"}), 404
            image_names.sort()
            with zf.open(image_names[0]) as img_fp:
                from PIL import Image
                import io
                img = Image.open(img_fp)
                img.thumbnail((max_size, max_size), Image.LANCZOS)
                buf = io.BytesIO()
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(buf, format="JPEG", quality=85, optimize=True)
                buf.seek(0)
                return Response(buf.getvalue(), mimetype="image/jpeg")
    except Exception as e:
        app_logger.error(f"cover extraction failed for {file_path}: {e}")
        return jsonify({"error": "cover_failed"}), 500


@api_v1_bp.route("/issue/<int:file_id>/download", methods=["GET"])
def download_issue(file_id):
    row = _file_row_by_id(file_id)
    if not row:
        return jsonify({"error": "not_found"}), 404
    return serve_comic_file(
        row["path"],
        range_header=request.headers.get("Range"),
        as_attachment=True,
    )


# ---------------------------------------------------------------------------
# Reading progress
# ---------------------------------------------------------------------------


@api_v1_bp.route("/progress", methods=["GET"])
def get_progress():
    path = request.args.get("path")
    if not path:
        return jsonify({"error": "Missing 'path' parameter"}), 400
    progress = get_reading_position(unquote(path))
    return jsonify(progress if progress is not None else None)


@api_v1_bp.route("/progress", methods=["PUT"])
def put_progress():
    body = request.get_json(silent=True) or {}
    path = body.get("path")
    page_number = body.get("page_number")
    if not path or page_number is None:
        return jsonify({"error": "Missing 'path' or 'page_number'"}), 400
    try:
        page_number = int(page_number)
    except (TypeError, ValueError):
        return jsonify({"error": "page_number must be an integer"}), 400

    total_pages = body.get("total_pages")
    if total_pages is not None:
        try:
            total_pages = int(total_pages)
        except (TypeError, ValueError):
            return jsonify({"error": "total_pages must be an integer"}), 400

    try:
        time_spent = int(body.get("time_spent", 0))
    except (TypeError, ValueError):
        time_spent = 0

    ok = save_reading_position(
        comic_path=path,
        page_number=page_number,
        total_pages=total_pages,
        time_spent=time_spent,
    )
    if not ok:
        return jsonify({"error": "save_failed"}), 500

    return jsonify(get_reading_position(path))


@api_v1_bp.route("/progress/since", methods=["GET"])
def progress_since():
    try:
        ts = int(request.args.get("ts", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "ts must be an integer unix timestamp"}), 400
    rows = get_reading_positions_since(ts)
    return jsonify({"items": rows, "count": len(rows)})


@api_v1_bp.route("/issues/read", methods=["POST"])
def post_issue_read():
    body = request.get_json(silent=True) or {}
    path = body.get("path")
    if not path:
        return jsonify({"error": "Missing 'path'"}), 400
    try:
        page_count = int(body.get("page_count", 0))
    except (TypeError, ValueError):
        page_count = 0
    try:
        time_spent = int(body.get("time_spent", 0))
    except (TypeError, ValueError):
        time_spent = 0

    ok = mark_issue_read(
        issue_path=path,
        page_count=page_count,
        time_spent=time_spent,
    )
    if not ok:
        return jsonify({"error": "save_failed"}), 500
    return jsonify({"ok": True})
