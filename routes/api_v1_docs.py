"""
Public reference docs for /api/v1/*.

This blueprint is intentionally separate from `api_v1_bp` so the bearer-token
gate does not apply -- the docs themselves are reference material; the
endpoints they describe still require a token.
"""

from flask import Blueprint, render_template


api_docs_bp = Blueprint("api_v1_docs", __name__, url_prefix="/api/v1")


# Single source of truth for the endpoint catalog. Edit here when routes
# change and the rendered page updates with no template changes.
ENDPOINTS = [
    {
        "section": "Auth",
        "method": "GET",
        "path": "/api/v1/auth/ping",
        "summary": "Verify bearer token + report server version and saved browse mode.",
        "auth": True,
        "params": [],
        "body": None,
        "response": {
            "ok": True,
            "version": "x.y.z",
            "browse_mode": "metadata",
        },
    },
    {
        "section": "Library",
        "method": "GET",
        "path": "/api/v1/library/publishers",
        "summary": "List publishers. Either ComicInfo metadata facet or top-level filesystem dirs.",
        "auth": True,
        "params": [
            {"name": "page", "type": "int", "default": "1", "desc": "1-indexed page number."},
            {"name": "page_size", "type": "int", "default": "50", "desc": "Items per page (max 200)."},
            {"name": "mode", "type": "str", "default": "(saved)", "desc": "metadata | filesystem. Falls back to the saved preference."},
            {"name": "sort", "type": "str", "default": "alpha", "desc": "alpha | count (metadata mode only)."},
        ],
        "body": None,
        "response": {
            "items": [{"value": "DC Comics", "name": "DC Comics", "path": "/data/DC Comics", "count": 42, "has_thumbnail": True}],
            "total": 12,
            "page": 1,
            "page_size": 50,
            "total_pages": 1,
            "has_more": False,
            "mode": "filesystem",
        },
    },
    {
        "section": "Library",
        "method": "GET",
        "path": "/api/v1/library/series",
        "summary": "List series under a publisher.",
        "auth": True,
        "params": [
            {"name": "publisher", "type": "str", "default": "—", "desc": "Required in filesystem mode (name or absolute path)."},
            {"name": "q", "type": "str", "default": "—", "desc": "Search filter (metadata mode)."},
            {"name": "sort", "type": "str", "default": "alpha", "desc": "alpha | count | year | recent."},
            {"name": "mode", "type": "str", "default": "(saved)", "desc": "metadata | filesystem."},
            {"name": "page", "type": "int", "default": "1", "desc": "1-indexed."},
            {"name": "page_size", "type": "int", "default": "50", "desc": "Max 200."},
        ],
        "body": None,
        "response": {
            "items": [{"value": "Batman", "name": "Batman", "path": "/data/DC Comics/Batman", "count": 3}],
            "total": 1,
            "page": 1,
            "page_size": 50,
            "total_pages": 1,
            "has_more": False,
            "mode": "filesystem",
        },
    },
    {
        "section": "Library",
        "method": "GET",
        "path": "/api/v1/library/issues",
        "summary": "List issues under a series. Items are progress-enriched.",
        "auth": True,
        "params": [
            {"name": "series", "type": "str", "default": "—", "desc": "Required."},
            {"name": "publisher", "type": "str", "default": "—", "desc": "Required in filesystem mode."},
            {"name": "sort", "type": "str", "default": "alpha", "desc": "alpha | year | recent."},
            {"name": "mode", "type": "str", "default": "(saved)", "desc": "metadata | filesystem."},
            {"name": "page", "type": "int", "default": "1", "desc": "1-indexed."},
            {"name": "page_size", "type": "int", "default": "50", "desc": "Max 200."},
        ],
        "body": None,
        "response": {
            "items": [{
                "id": 42,
                "name": "Batman 001 (2020).cbz",
                "path": "/data/DC Comics/Batman/Batman 001 (2020).cbz",
                "size": 18234567,
                "has_progress": True,
                "last_page": 5,
            }],
            "total": 1,
            "page": 1,
            "page_size": 50,
            "total_pages": 1,
            "has_more": False,
            "mode": "filesystem",
        },
    },
    {
        "section": "Library — Dashboard",
        "method": "GET",
        "path": "/api/v1/library/favorites",
        "summary": "Favorited publisher folders. `value` echoes back as `?publisher=` for /library/series.",
        "auth": True,
        "params": [
            {"name": "page", "type": "int", "default": "1", "desc": "1-indexed."},
            {"name": "page_size", "type": "int", "default": "50", "desc": "Max 200."},
        ],
        "body": None,
        "response": {
            "items": [{"value": "DC Comics", "name": "DC Comics", "path": "/data/DC Comics", "type": "publisher", "created_at": "2026-04-25 10:00:00"}],
            "total": 1, "page": 1, "page_size": 50, "total_pages": 1, "has_more": False,
            "scope": "favorites",
        },
    },
    {
        "section": "Library — Dashboard",
        "method": "GET",
        "path": "/api/v1/library/to-read",
        "summary": "User's 'want to read' list. Mixed file/folder rows; files carry id + progress.",
        "auth": True,
        "params": [
            {"name": "page", "type": "int", "default": "1", "desc": "1-indexed."},
            {"name": "page_size", "type": "int", "default": "50", "desc": "Max 200."},
        ],
        "body": None,
        "response": {
            "items": [
                {"value": "Batman 001", "name": "Batman 001", "path": "/data/.../Batman 001.cbz", "type": "file", "id": 42, "has_progress": True, "last_page": 5, "created_at": "..."},
                {"value": "Marvel", "name": "Marvel", "path": "/data/Marvel", "type": "folder", "created_at": "..."},
            ],
            "total": 2, "page": 1, "page_size": 50, "total_pages": 1, "has_more": False,
            "scope": "to_read",
        },
    },
    {
        "section": "Library — Dashboard",
        "method": "GET",
        "path": "/api/v1/library/recent",
        "summary": "Most-recently-indexed CBZ/CBR files inside enabled libraries.",
        "auth": True,
        "params": [
            {"name": "page", "type": "int", "default": "1", "desc": "1-indexed."},
            {"name": "page_size", "type": "int", "default": "50", "desc": "Max 200."},
        ],
        "body": None,
        "response": {
            "items": [{
                "id": 42, "value": "Batman 001 (2020).cbz", "name": "Batman 001 (2020).cbz",
                "path": "/data/DC Comics/Batman/Batman 001 (2020).cbz",
                "size": 18234567, "added_at": "2026-04-20 09:33:11", "type": "file",
                "has_progress": False, "last_page": None,
            }],
            "total": 1, "page": 1, "page_size": 50, "total_pages": 1, "has_more": False,
            "scope": "recent",
        },
    },
    {
        "section": "Issue",
        "method": "GET",
        "path": "/api/v1/issue/<file_id>",
        "summary": "Full ComicInfo metadata + saved reading position for a file_index row.",
        "auth": True,
        "params": [
            {"name": "file_id", "type": "int", "default": "(path)", "desc": "file_index.id integer."},
        ],
        "body": None,
        "response": {
            "id": 42, "name": "Batman 001 (2020).cbz", "path": "/data/.../Batman 001 (2020).cbz",
            "size": 18234567, "modified_at": 1700000000, "has_comicinfo": True,
            "metadata": {"title": "Origin", "series": "Batman", "number": "1", "year": "2020", "publisher": "DC Comics"},
            "progress": {"page_number": 5, "total_pages": 32},
        },
    },
    {
        "section": "Issue",
        "method": "GET",
        "path": "/api/v1/issue/<file_id>/cover",
        "summary": "JPEG of the first image inside the CBZ. Use ?size=N to clamp the long edge.",
        "auth": True,
        "params": [
            {"name": "file_id", "type": "int", "default": "(path)", "desc": "file_index.id."},
            {"name": "size", "type": "int", "default": "400", "desc": "Max long-edge px (64–2000)."},
        ],
        "body": None,
        "response": "binary/JPEG (image/jpeg) on success; 404 if cover unavailable.",
    },
    {
        "section": "Issue",
        "method": "GET",
        "path": "/api/v1/issue/<file_id>/download",
        "summary": "Stream the comic file. Supports HTTP Range for partial content.",
        "auth": True,
        "params": [
            {"name": "file_id", "type": "int", "default": "(path)", "desc": "file_index.id."},
            {"name": "Range", "type": "header", "default": "—", "desc": "Optional bytes=START-END for resumable downloads."},
        ],
        "body": None,
        "response": "binary; 200 (full) or 206 (partial, when Range supplied).",
    },
    {
        "section": "Reading progress",
        "method": "GET",
        "path": "/api/v1/progress",
        "summary": "Saved reading position for a single comic, by absolute path.",
        "auth": True,
        "params": [
            {"name": "path", "type": "str", "default": "—", "desc": "URL-encoded absolute comic_path. Required."},
        ],
        "body": None,
        "response": {"page_number": 5, "total_pages": 32, "updated_at": "..."},
    },
    {
        "section": "Reading progress",
        "method": "PUT",
        "path": "/api/v1/progress",
        "summary": "Save / update the reading position for a comic.",
        "auth": True,
        "params": [],
        "body": {"path": "/data/.../Batman 001.cbz", "page_number": 5, "total_pages": 32, "time_spent": 120},
        "response": {"page_number": 5, "total_pages": 32, "updated_at": "..."},
    },
    {
        "section": "Reading progress",
        "method": "GET",
        "path": "/api/v1/progress/since",
        "summary": "Reading positions changed at or after a unix timestamp. Paginated for resumable sync.",
        "auth": True,
        "params": [
            {"name": "ts", "type": "int", "default": "0", "desc": "Unix timestamp; rows with updated_at >= ts."},
            {"name": "page", "type": "int", "default": "1", "desc": "1-indexed."},
            {"name": "page_size", "type": "int", "default": "50", "desc": "Max 200."},
        ],
        "body": None,
        "response": {
            "items": [{"comic_path": "...", "page_number": 5, "total_pages": 32, "updated_at": "..."}],
            "total": 1, "page": 1, "page_size": 50, "total_pages": 1, "has_more": False,
            "count": 1,
        },
    },
    {
        "section": "Reading progress",
        "method": "POST",
        "path": "/api/v1/issues/read",
        "summary": "Mark an issue as read; records page_count and time_spent.",
        "auth": True,
        "params": [],
        "body": {"path": "/data/.../Batman 001.cbz", "page_count": 32, "time_spent": 1800},
        "response": {"ok": True},
    },
]


@api_docs_bp.route("/docs", methods=["GET"])
def api_v1_docs():
    """Render reference docs for /api/v1/* — public, no token required."""
    return render_template("api_v1_docs.html", endpoints=ENDPOINTS)
