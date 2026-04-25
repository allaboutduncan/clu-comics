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
    filesystem_browse_issues,
    filesystem_browse_publishers,
    filesystem_browse_series,
    get_api_browse_mode,
    get_api_token,
    get_db_connection,
    get_favorite_publishers_paginated,
    get_reading_position,
    get_reading_positions_since_paginated,
    get_recent_files_paginated,
    get_to_read_items_paginated,
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


def _paged_response(items, total, page, page_size, **extra):
    """Common envelope for every list endpoint under /api/v1/."""
    total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0
    body = {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_more": page < total_pages,
    }
    body.update(extra)
    return jsonify(body)


def _resolve_mode():
    """Return (mode, error_response_or_None). Honors ?mode= override, falls back to saved preference."""
    raw = request.args.get("mode")
    mode = (raw or get_api_browse_mode()).lower()
    if mode not in ("metadata", "filesystem"):
        return None, (jsonify({"error": "invalid_mode"}), 400)
    return mode, None


def _data_dir():
    """Resolve DATA_DIR lazily so tests can mock the `app` module."""
    from app import DATA_DIR  # noqa: WPS433
    return DATA_DIR


def _looks_like_traversal(value):
    """Reject anything containing a `..` segment regardless of OS-specific path semantics."""
    if not value:
        return False
    norm = value.replace("\\", "/")
    return any(seg == ".." for seg in norm.split("/"))


def _path_is_known_directory(path):
    """True if `path` exists in file_index as a directory row."""
    if not path:
        return False
    conn = get_db_connection()
    if not conn:
        return False
    try:
        c = conn.cursor()
        c.execute(
            "SELECT 1 FROM file_index WHERE path = ? AND type = 'directory' LIMIT 1",
            (path,),
        )
        return c.fetchone() is not None
    finally:
        conn.close()


def _resolve_filesystem_path(*parts):
    """
    Resolve `parts` to a path that the API may safely browse in filesystem mode.

    Strategy (in order):
      1. Reject anything containing `..` outright.
      2. If the joined value is already a known directory in file_index, trust
         it — file_index is server-curated and never contains paths outside the
         user's library. This handles the common case where the client echoes
         back the absolute `path` we returned from the publishers endpoint, but
         DATA_DIR's literal string differs from the indexed paths (e.g. Docker
         vs. host, scan with one mount and serve with another).
      3. Otherwise treat the parts as relative to DATA_DIR with a strict
         `commonpath` check.

    Returns the resolved path on success, or None on rejection.
    """
    base = _data_dir()

    cleaned = [p for p in parts if p]
    app_logger.info(
        f"_resolve_filesystem_path: parts={cleaned!r} data_dir={base!r}"
    )

    if any(_looks_like_traversal(p) for p in cleaned):
        app_logger.warning(
            f"_resolve_filesystem_path: REJECTED (traversal) parts={cleaned!r}"
        )
        return None

    # Step 2: try the literal joined path against file_index first.
    if cleaned:
        candidate = None
        for p in cleaned:
            if os.path.isabs(p):
                candidate = p
            else:
                candidate = os.path.join(candidate or base or "", p)
        app_logger.info(
            f"_resolve_filesystem_path: step2 candidate={candidate!r}"
        )
        if candidate and _path_is_known_directory(candidate):
            app_logger.info(
                f"_resolve_filesystem_path: ACCEPTED via file_index match: {candidate!r}"
            )
            return candidate
        # Try slash-normalised variants too — file_index may have stored
        # the path with the opposite separator.
        if candidate:
            for variant in {
                candidate.replace("\\", "/"),
                candidate.replace("/", "\\"),
            }:
                if variant != candidate and _path_is_known_directory(variant):
                    app_logger.info(
                        f"_resolve_filesystem_path: ACCEPTED via slash-variant: {variant!r}"
                    )
                    return variant

    # Step 3: strict join-under-DATA_DIR fallback.
    if not base:
        app_logger.warning(
            "_resolve_filesystem_path: REJECTED (no DATA_DIR configured)"
        )
        return None
    base_abs = os.path.abspath(base)
    candidate = base
    for p in cleaned:
        if os.path.isabs(p):
            candidate = p
        else:
            candidate = os.path.join(candidate, p)
    candidate_abs = os.path.normpath(os.path.abspath(candidate))
    try:
        cp = os.path.commonpath([candidate_abs, base_abs])
    except ValueError as e:
        app_logger.warning(
            f"_resolve_filesystem_path: REJECTED (commonpath ValueError: {e}) "
            f"candidate_abs={candidate_abs!r} base_abs={base_abs!r}"
        )
        return None
    if cp != base_abs:
        app_logger.warning(
            f"_resolve_filesystem_path: REJECTED (outside DATA_DIR) "
            f"candidate_abs={candidate_abs!r} base_abs={base_abs!r} commonpath={cp!r}"
        )
        return None
    app_logger.info(
        f"_resolve_filesystem_path: ACCEPTED via DATA_DIR join: {candidate_abs!r}"
    )
    return candidate_abs


# Back-compat alias — older code paths reference the previous name.
_safe_join_under_data_dir = _resolve_filesystem_path


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@api_v1_bp.route("/auth/ping", methods=["GET"])
def ping():
    return jsonify({
        "ok": True,
        "version": __version__,
        "browse_mode": get_api_browse_mode(),
    })


@api_v1_bp.route("/library/publishers", methods=["GET"])
def list_publishers():
    page, page_size, offset = _paginate_args()
    mode, err = _resolve_mode()
    if err:
        return err

    if mode == "filesystem":
        result = filesystem_browse_publishers(_data_dir(), offset=offset, limit=page_size)
    else:
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

    return _paged_response(
        result.get("items", []),
        result.get("total", 0),
        page,
        page_size,
        mode=mode,
    )


@api_v1_bp.route("/library/series", methods=["GET"])
def list_series():
    page, page_size, offset = _paginate_args()
    mode, err = _resolve_mode()
    if err:
        return err

    publisher = request.args.get("publisher")
    app_logger.info(
        f"/api/v1/library/series mode={mode} publisher={publisher!r} "
        f"args={dict(request.args)} url={request.url!r}"
    )

    if mode == "filesystem":
        if not publisher:
            app_logger.warning(
                "/api/v1/library/series filesystem-mode REJECTED: missing publisher"
            )
            return jsonify({"error": "Missing 'publisher' parameter"}), 400
        publisher_path = _safe_join_under_data_dir(publisher)
        if not publisher_path:
            app_logger.warning(
                f"/api/v1/library/series invalid_path: publisher={publisher!r} "
                f"data_dir={_data_dir()!r}"
            )
            return jsonify({
                "error": "invalid_path",
                "received": publisher,
                "data_dir": _data_dir(),
            }), 400
        result = filesystem_browse_series(publisher_path, offset=offset, limit=page_size)
    else:
        sort = request.args.get("sort", "alpha")
        if sort not in ("alpha", "count", "year", "recent"):
            sort = "alpha"
        filters = {}
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

    return _paged_response(
        result.get("items", []),
        result.get("total", 0),
        page,
        page_size,
        mode=mode,
    )


@api_v1_bp.route("/library/issues", methods=["GET"])
def list_issues():
    page, page_size, offset = _paginate_args()
    mode, err = _resolve_mode()
    if err:
        return err

    series = request.args.get("series")
    publisher = request.args.get("publisher")
    app_logger.info(
        f"/api/v1/library/issues mode={mode} publisher={publisher!r} "
        f"series={series!r} args={dict(request.args)} url={request.url!r}"
    )

    if mode == "filesystem":
        if not publisher or not series:
            app_logger.warning(
                "/api/v1/library/issues filesystem-mode REJECTED: missing publisher/series"
            )
            return jsonify({"error": "Missing 'publisher' or 'series' parameter"}), 400
        series_path = _safe_join_under_data_dir(publisher, series)
        if not series_path:
            app_logger.warning(
                f"/api/v1/library/issues invalid_path: publisher={publisher!r} "
                f"series={series!r} data_dir={_data_dir()!r}"
            )
            return jsonify({
                "error": "invalid_path",
                "received_publisher": publisher,
                "received_series": series,
                "data_dir": _data_dir(),
            }), 400
        result = filesystem_browse_issues(series_path, offset=offset, limit=page_size)

        # Enrich with progress markers (same shape as metadata mode).
        items = result.get("items", [])
        paths = [it.get("path") for it in items if it.get("path")]
        progress_map = _progress_map_for_paths(paths)
        enriched = [
            {
                **it,
                "has_progress": it.get("path") in progress_map,
                "last_page": (progress_map.get(it.get("path")) or {}).get("page_number"),
            }
            for it in items
        ]
        return _paged_response(
            enriched,
            result.get("total", 0),
            page,
            page_size,
            mode=mode,
        )

    sort = request.args.get("sort", "alpha")
    if sort not in ("alpha", "year", "recent"):
        sort = "alpha"

    if not series:
        return jsonify({"error": "Missing 'series' parameter"}), 400

    filters = {"series": [series]}
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
    progress_map = _progress_map_for_paths(paths)

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

    return _paged_response(
        enriched,
        result.get("total", 0),
        page,
        page_size,
        mode=mode,
    )


def _progress_map_for_paths(paths):
    if not paths:
        return {}
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        placeholders = ",".join(["?"] * len(paths))
        c = conn.cursor()
        c.execute(
            f"SELECT comic_path, page_number, total_pages "
            f"FROM reading_positions WHERE comic_path IN ({placeholders})",
            paths,
        )
        return {
            row["comic_path"]: {
                "page_number": row["page_number"],
                "total_pages": row["total_pages"],
            }
            for row in c.fetchall()
        }
    finally:
        conn.close()


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


# ---------------------------------------------------------------------------
# Dashboard lists — Favorites / Want-to-Read / Recently-Added
# ---------------------------------------------------------------------------


@api_v1_bp.route("/library/favorites", methods=["GET"])
def list_favorites():
    """Favorite publisher folders. Items echo `value` for drill-through into /library/series."""
    page, page_size, offset = _paginate_args()
    rows, total = get_favorite_publishers_paginated(offset=offset, limit=page_size)
    items = [
        {
            "value": r.get("name"),
            "name": r.get("name"),
            "path": r.get("publisher_path"),
            "type": "publisher",
            "created_at": r.get("created_at"),
        }
        for r in rows
    ]
    return _paged_response(items, total, page, page_size, scope="favorites")


@api_v1_bp.route("/library/to-read", methods=["GET"])
def list_to_read():
    """User's 'want to read' list — mixed file/folder rows."""
    page, page_size, offset = _paginate_args()
    rows, total = get_to_read_items_paginated(offset=offset, limit=page_size)
    file_paths = [r["path"] for r in rows if r.get("type") == "file"]
    progress_map = _progress_map_for_paths(file_paths)
    items = []
    for r in rows:
        item = {
            "value": r.get("name"),
            "name": r.get("name"),
            "path": r.get("path"),
            "type": r.get("type"),
            "created_at": r.get("created_at"),
        }
        if r.get("type") == "file":
            item["id"] = _id_for_path(r["path"])
            prog = progress_map.get(r["path"])
            item["has_progress"] = prog is not None
            item["last_page"] = prog["page_number"] if prog else None
        items.append(item)
    return _paged_response(items, total, page, page_size, scope="to_read")


@api_v1_bp.route("/library/recent", methods=["GET"])
def list_recent():
    """Most recently indexed CBZ/CBR files inside enabled library paths."""
    page, page_size, offset = _paginate_args()
    rows, total = get_recent_files_paginated(offset=offset, limit=page_size)
    paths = [r["file_path"] for r in rows if r.get("file_path")]
    progress_map = _progress_map_for_paths(paths)
    items = []
    for r in rows:
        prog = progress_map.get(r.get("file_path"))
        items.append({
            "id": r.get("id"),
            "value": r.get("file_name"),
            "name": r.get("file_name"),
            "path": r.get("file_path"),
            "size": r.get("file_size"),
            "added_at": r.get("added_at"),
            "type": "file",
            "has_progress": prog is not None,
            "last_page": prog["page_number"] if prog else None,
        })
    return _paged_response(items, total, page, page_size, scope="recent")


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
    page, page_size, offset = _paginate_args()
    rows, total = get_reading_positions_since_paginated(
        ts, offset=offset, limit=page_size
    )
    # `count` retained for back-compat with earlier clients that expected it.
    return _paged_response(rows, total, page, page_size, count=len(rows))


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
