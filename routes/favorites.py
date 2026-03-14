"""
Favorites API Blueprint

Provides REST API endpoints for managing:
- Favorite Publishers (root-level folders)
- Favorite Series (folders within publishers)
- Issues Read (comic files marked as read)
- To Read (files and folders marked as 'want to read')
"""

from flask import Blueprint, request, jsonify
from core.database import (
    add_favorite_publisher, remove_favorite_publisher,
    get_favorite_publishers, is_favorite_publisher,
    mark_issue_read, unmark_issue_read,
    get_issues_read, is_issue_read, get_issue_read_date,
    hide_issue_from_history,
    add_to_read, remove_to_read, get_to_read_items, is_to_read,
    clear_stats_cache_keys
)
from core.app_logging import app_logger

favorites_bp = Blueprint('favorites', __name__, url_prefix='/api/favorites')


# =============================================================================
# Publisher Endpoints
# =============================================================================

@favorites_bp.route('/publishers', methods=['GET'])
def get_publishers():
    """Get all favorite publishers."""
    try:
        publishers = get_favorite_publishers()
        return jsonify({
            "success": True,
            "publishers": publishers
        })
    except Exception as e:
        app_logger.error(f"Error getting favorite publishers: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@favorites_bp.route('/publishers/check', methods=['GET'])
def check_publisher():
    """Check if a publisher is favorited."""
    path = request.args.get('path')
    if not path:
        return jsonify({"success": False, "error": "Missing path parameter"}), 400

    try:
        is_fav = is_favorite_publisher(path)
        return jsonify({
            "success": True,
            "is_favorite": is_fav
        })
    except Exception as e:
        app_logger.error(f"Error checking favorite publisher: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@favorites_bp.route('/publishers', methods=['POST'])
def add_publisher():
    """Add a publisher to favorites."""
    data = request.get_json() or {}
    path = data.get('path')

    if not path:
        return jsonify({"success": False, "error": "Missing path in request body"}), 400

    try:
        success = add_favorite_publisher(path)
        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to add favorite publisher"}), 500
    except Exception as e:
        app_logger.error(f"Error adding favorite publisher: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@favorites_bp.route('/publishers', methods=['DELETE'])
def remove_publisher():
    """Remove a publisher from favorites."""
    data = request.get_json() or {}
    path = data.get('path')

    if not path:
        return jsonify({"success": False, "error": "Missing path in request body"}), 400

    try:
        success = remove_favorite_publisher(path)
        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to remove favorite publisher"}), 500
    except Exception as e:
        app_logger.error(f"Error removing favorite publisher: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# =============================================================================
# Issues Read Endpoints
# =============================================================================

@favorites_bp.route('/issues', methods=['GET'])
def get_issues():
    """Get all read issues."""
    try:
        issues = get_issues_read()
        return jsonify({
            "success": True,
            "issues": issues
        })
    except Exception as e:
        app_logger.error(f"Error getting read issues: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@favorites_bp.route('/issues/check', methods=['GET'])
def check_issue():
    """Check if an issue has been read."""
    path = request.args.get('path')
    if not path:
        return jsonify({"success": False, "error": "Missing path parameter"}), 400

    try:
        is_read = is_issue_read(path)
        read_at = get_issue_read_date(path) if is_read else None
        return jsonify({
            "success": True,
            "is_read": is_read,
            "read_at": read_at
        })
    except Exception as e:
        app_logger.error(f"Error checking issue read status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@favorites_bp.route('/issues', methods=['POST'])
def mark_read():
    """Mark an issue as read."""
    data = request.get_json() or {}
    path = data.get('path')

    if not path:
        return jsonify({"success": False, "error": "Missing path in request body"}), 400

    try:
        success = mark_issue_read(path)
        if success:
            clear_stats_cache_keys(['library_stats', 'reading_history'])  # Only invalidate reading-related cache
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to mark issue as read"}), 500
    except Exception as e:
        app_logger.error(f"Error marking issue as read: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@favorites_bp.route('/issues', methods=['DELETE'])
def unmark_read():
    """Remove read status from an issue."""
    data = request.get_json() or {}
    path = data.get('path')

    if not path:
        return jsonify({"success": False, "error": "Missing path in request body"}), 400

    try:
        success = unmark_issue_read(path)
        if success:
            clear_stats_cache_keys(['library_stats', 'reading_history'])  # Only invalidate reading-related cache
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to unmark issue as read"}), 500
    except Exception as e:
        app_logger.error(f"Error unmarking issue as read: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@favorites_bp.route('/issues/hide', methods=['POST'])
def hide_from_history():
    """Hide a read issue from timeline and wrapped views."""
    data = request.get_json() or {}
    path = data.get('path')

    if not path:
        return jsonify({"success": False, "error": "Missing path in request body"}), 400

    try:
        success = hide_issue_from_history(path)
        if success:
            clear_stats_cache_keys(['library_stats', 'reading_history'])
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to hide issue from history"}), 500
    except Exception as e:
        app_logger.error(f"Error hiding issue from history: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# =============================================================================
# To Read Endpoints
# =============================================================================

@favorites_bp.route('/to-read', methods=['GET'])
def get_to_read():
    """Get all 'to read' items."""
    try:
        items = get_to_read_items()
        return jsonify({
            "success": True,
            "items": items
        })
    except Exception as e:
        app_logger.error(f"Error getting 'to read' items: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@favorites_bp.route('/to-read/check', methods=['GET'])
def check_to_read():
    """Check if an item is in the 'to read' list."""
    path = request.args.get('path')
    if not path:
        return jsonify({"success": False, "error": "Missing path parameter"}), 400

    try:
        is_marked = is_to_read(path)
        return jsonify({
            "success": True,
            "is_to_read": is_marked
        })
    except Exception as e:
        app_logger.error(f"Error checking 'to read' status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@favorites_bp.route('/to-read', methods=['POST'])
def add_to_read_item():
    """Add an item to 'to read' list."""
    data = request.get_json() or {}
    path = data.get('path')
    item_type = data.get('type', 'file')

    if not path:
        return jsonify({"success": False, "error": "Missing path in request body"}), 400

    try:
        success = add_to_read(path, item_type)
        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to add to 'to read'"}), 500
    except Exception as e:
        app_logger.error(f"Error adding to 'to read': {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@favorites_bp.route('/to-read', methods=['DELETE'])
def remove_to_read_item():
    """Remove an item from 'to read' list."""
    data = request.get_json() or {}
    path = data.get('path')

    if not path:
        return jsonify({"success": False, "error": "Missing path in request body"}), 400

    try:
        success = remove_to_read(path)
        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to remove from 'to read'"}), 500
    except Exception as e:
        app_logger.error(f"Error removing from 'to read': {e}")
        return jsonify({"success": False, "error": str(e)}), 500
