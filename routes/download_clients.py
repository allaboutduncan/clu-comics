"""
Download Clients Blueprint.

Provides routes for configuring Usenet download clients (SABnzbd, NZBGet)
and Newznab indexers, plus connection testing. Mirrors the metadata
provider routes in routes/metadata.py.

Model and database imports are performed lazily inside each function so the
blueprint imports cheaply and tests can patch dependencies.
"""
from flask import Blueprint, jsonify, request

from core.app_logging import app_logger

download_clients_bp = Blueprint('download_clients', __name__)


# =============================================================================
# Download Clients
# =============================================================================

@download_clients_bp.route('/api/download-clients', methods=['GET'])
def list_download_clients():
    """List available download clients merged with their configured status."""
    try:
        from models.download_clients import get_available_download_clients
        from core.database import (
            get_all_download_clients_status,
            get_download_client_config_masked,
        )

        clients = get_available_download_clients()
        status_by_type = {s['client_type']: s for s in get_all_download_clients_status()}

        for c in clients:
            status = status_by_type.get(c['type'], {})
            c['has_config'] = c['type'] in status_by_type
            c['is_active'] = status.get('is_active', 0) == 1
            c['is_valid'] = status.get('is_valid', 0) == 1
            c['last_tested'] = status.get('last_tested')
            c['config_masked'] = (
                get_download_client_config_masked(c['type']) if c['has_config'] else None
            )

        return jsonify({"success": True, "clients": clients})
    except Exception as e:
        app_logger.error(f"Error listing download clients: {e}")
        return jsonify({"error": str(e)}), 500


def _validate_client_type(client_type):
    """Return True if client_type is a known ClientType, else False."""
    from models.download_clients import ClientType
    try:
        ClientType(client_type)
        return True
    except ValueError:
        return False


@download_clients_bp.route('/api/download-clients/<client_type>/config', methods=['GET'])
def get_download_client_config_route(client_type):
    """Get masked config for a download client (safe for display)."""
    try:
        from core.database import get_download_client_config_masked

        if not _validate_client_type(client_type):
            return jsonify({"error": f"Unknown client type: {client_type}"}), 400

        masked = get_download_client_config_masked(client_type)
        if not masked:
            return jsonify({"success": True, "has_config": False, "config": {}})
        return jsonify({"success": True, "has_config": True, "config": masked})
    except Exception as e:
        app_logger.error(f"Error getting download client config: {e}")
        return jsonify({"error": str(e)}), 500


@download_clients_bp.route('/api/download-clients/<client_type>/config', methods=['POST'])
def save_download_client_config_route(client_type):
    """Save config for a download client."""
    try:
        from core.database import save_download_client_config

        if not _validate_client_type(client_type):
            return jsonify({"error": f"Unknown client type: {client_type}"}), 400

        data = request.get_json()
        if not data:
            return jsonify({"error": "No config provided"}), 400

        success = save_download_client_config(client_type, data)
        if success:
            return jsonify({"success": True, "message": f"Config saved for {client_type}"})
        return jsonify({"error": "Failed to save config"}), 500
    except Exception as e:
        app_logger.error(f"Error saving download client config: {e}")
        return jsonify({"error": str(e)}), 500


@download_clients_bp.route('/api/download-clients/<client_type>/config', methods=['DELETE'])
def delete_download_client_config_route(client_type):
    """Delete a download client's config."""
    try:
        from core.database import delete_download_client_config

        if not _validate_client_type(client_type):
            return jsonify({"error": f"Unknown client type: {client_type}"}), 400

        success = delete_download_client_config(client_type)
        if success:
            return jsonify({"success": True, "message": f"Config deleted for {client_type}"})
        return jsonify({"error": "Failed to delete config"}), 500
    except Exception as e:
        app_logger.error(f"Error deleting download client config: {e}")
        return jsonify({"error": str(e)}), 500


@download_clients_bp.route('/api/download-clients/<client_type>/test', methods=['POST'])
def test_download_client(client_type):
    """Test connection to a download client using saved config."""
    try:
        from core.database import (
            get_download_client_config,
            update_download_client_validity,
        )
        from models.download_clients import (
            get_download_client_by_name,
            DownloadClientConfig,
        )

        if not _validate_client_type(client_type):
            return jsonify({"error": f"Unknown client type: {client_type}"}), 400

        config_dict = get_download_client_config(client_type)
        if not config_dict:
            return jsonify({"success": False, "error": "No config configured"}), 400

        client = get_download_client_by_name(
            client_type, DownloadClientConfig.from_dict(config_dict)
        )
        is_valid = client.test_connection()
        update_download_client_validity(client_type, is_valid)

        if is_valid:
            return jsonify({"success": True, "valid": True,
                            "message": f"Connection to {client_type} successful"})
        reason = getattr(client, "last_error", None) or f"Connection to {client_type} failed"
        return jsonify({"success": True, "valid": False, "error": reason})
    except Exception as e:
        app_logger.error(f"Error testing download client: {e}")
        return jsonify({"error": str(e)}), 500


@download_clients_bp.route('/api/download-clients/<client_type>/activate', methods=['POST'])
def activate_download_client(client_type):
    """Mark a download client as the single active client."""
    try:
        from core.database import (
            get_download_client_config,
            set_active_download_client,
        )

        if not _validate_client_type(client_type):
            return jsonify({"error": f"Unknown client type: {client_type}"}), 400

        if not get_download_client_config(client_type):
            return jsonify({"error": "Client is not configured"}), 400

        success = set_active_download_client(client_type)
        if success:
            return jsonify({"success": True, "active": client_type})
        return jsonify({"error": "Failed to activate client"}), 500
    except Exception as e:
        app_logger.error(f"Error activating download client: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Indexers
# =============================================================================

@download_clients_bp.route('/api/indexers', methods=['GET'])
def list_indexers():
    """List all configured indexers (masked) and available indexer types."""
    try:
        from core.database import get_all_indexers
        from models.indexers import get_available_indexer_types

        return jsonify({
            "success": True,
            "indexers": get_all_indexers(),
            "types": get_available_indexer_types(),
        })
    except Exception as e:
        app_logger.error(f"Error listing indexers: {e}")
        return jsonify({"error": str(e)}), 500


@download_clients_bp.route('/api/indexers', methods=['POST'])
def create_indexer():
    """Add a new indexer."""
    try:
        from core.database import add_indexer

        data = request.get_json() or {}
        name = (data.get('name') or '').strip()
        url = (data.get('url') or '').strip()
        if not name or not url:
            return jsonify({"error": "name and url are required"}), 400

        # Default to the Newznab comics category (7030) since CLU only wants
        # books/comics — narrows results and avoids indexers that require a cat.
        config = {
            "api_key": data.get('api_key'),
            "categories": (data.get('categories') or '').strip() or '7030',
        }
        new_id = add_indexer(
            name=name,
            url=url,
            config=config,
            priority=data.get('priority', 0),
            enabled=data.get('enabled', True),
            indexer_type=data.get('indexer_type', 'newznab'),
        )
        if new_id is None:
            return jsonify({"error": "Failed to add indexer"}), 500
        return jsonify({"success": True, "id": new_id})
    except Exception as e:
        app_logger.error(f"Error creating indexer: {e}")
        return jsonify({"error": str(e)}), 500


@download_clients_bp.route('/api/indexers/<int:indexer_id>', methods=['GET'])
def get_indexer_route(indexer_id):
    """Get a single indexer (masked)."""
    try:
        from core.database import get_indexer_masked

        indexer = get_indexer_masked(indexer_id)
        if not indexer:
            return jsonify({"error": "Indexer not found"}), 404
        return jsonify({"success": True, "indexer": indexer})
    except Exception as e:
        app_logger.error(f"Error getting indexer: {e}")
        return jsonify({"error": str(e)}), 500


@download_clients_bp.route('/api/indexers/<int:indexer_id>', methods=['PUT'])
def update_indexer_route(indexer_id):
    """Update an indexer (partial). Only re-encrypts secrets if provided."""
    try:
        from core.database import get_indexer, update_indexer

        if not get_indexer(indexer_id):
            return jsonify({"error": "Indexer not found"}), 404

        data = request.get_json() or {}
        # Build the encrypted config only if a secret field is present so that
        # a metadata-only edit doesn't wipe the stored api_key.
        config = None
        if 'api_key' in data or 'categories' in data:
            existing = get_indexer(indexer_id) or {}
            config = {
                "api_key": data.get('api_key', existing.get('api_key')),
                "categories": data.get('categories', existing.get('categories')),
            }

        success = update_indexer(
            indexer_id,
            name=data.get('name'),
            url=data.get('url'),
            config=config,
            priority=data.get('priority'),
            enabled=data.get('enabled'),
        )
        if success:
            return jsonify({"success": True})
        return jsonify({"error": "Failed to update indexer"}), 500
    except Exception as e:
        app_logger.error(f"Error updating indexer: {e}")
        return jsonify({"error": str(e)}), 500


@download_clients_bp.route('/api/indexers/<int:indexer_id>', methods=['DELETE'])
def delete_indexer_route(indexer_id):
    """Delete an indexer."""
    try:
        from core.database import delete_indexer, get_indexer

        if not get_indexer(indexer_id):
            return jsonify({"error": "Indexer not found"}), 404

        success = delete_indexer(indexer_id)
        if success:
            return jsonify({"success": True})
        return jsonify({"error": "Failed to delete indexer"}), 500
    except Exception as e:
        app_logger.error(f"Error deleting indexer: {e}")
        return jsonify({"error": str(e)}), 500


@download_clients_bp.route('/api/indexers/reorder', methods=['POST'])
def reorder_indexers():
    """Reorder indexers by a list of ids (index 0 = highest priority)."""
    try:
        from core.database import set_indexer_order

        data = request.get_json() or {}
        order = data.get('order')
        if not isinstance(order, list):
            return jsonify({"error": "Missing 'order' list"}), 400

        success = set_indexer_order(order)
        if success:
            return jsonify({"success": True})
        return jsonify({"error": "Failed to reorder indexers"}), 500
    except Exception as e:
        app_logger.error(f"Error reordering indexers: {e}")
        return jsonify({"error": str(e)}), 500


@download_clients_bp.route('/api/indexers/<int:indexer_id>/test', methods=['POST'])
def test_indexer(indexer_id):
    """Test connection to an indexer using saved config."""
    try:
        from core.database import get_indexer, update_indexer_validity
        from models.indexers import get_indexer_impl, IndexerConfig, IndexerType

        indexer = get_indexer(indexer_id)
        if not indexer:
            return jsonify({"error": "Indexer not found"}), 404

        config = IndexerConfig(
            name=indexer.get('name', ''),
            url=indexer.get('url', ''),
            api_key=indexer.get('api_key'),
            categories=indexer.get('categories'),
            enabled=indexer.get('enabled', True),
        )
        impl = get_indexer_impl(
            IndexerType(indexer.get('indexer_type', 'newznab')), config
        )
        is_valid = impl.test_connection()
        update_indexer_validity(indexer_id, is_valid)

        if is_valid:
            return jsonify({"success": True, "valid": True,
                            "message": f"Connection to {config.name} successful"})
        reason = getattr(impl, "last_error", None) or f"Connection to {config.name} failed"
        return jsonify({"success": True, "valid": False, "error": reason})
    except Exception as e:
        app_logger.error(f"Error testing indexer: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Usenet download status
# =============================================================================

@download_clients_bp.route('/api/usenet/downloads', methods=['GET'])
def list_usenet_downloads():
    """List in-memory Usenet downloads and their current status."""
    try:
        from models.usenet import get_usenet_downloads

        return jsonify({"success": True, "downloads": get_usenet_downloads()})
    except Exception as e:
        app_logger.error(f"Error listing usenet downloads: {e}")
        return jsonify({"error": str(e)}), 500


@download_clients_bp.route('/api/usenet/search', methods=['POST'])
def usenet_search():
    """Manual Usenet search for a series/issue across enabled indexers.

    Returns every scored result (not just accepted ones) so the user can pick,
    sorted best-first, plus whether Usenet is configured/prioritized.
    """
    try:
        from core.database import get_active_download_client, get_enabled_indexers
        from models.usenet import (
            search_usenet_for_issue,
            usenet_precedes_getcomics,
        )

        data = request.get_json() or {}
        series = (data.get('series') or '').strip()
        issue = str(data.get('issue') or '').strip()
        if not series:
            return jsonify({"error": "series is required"}), 400

        # Searching only needs indexers; an active client is needed only to grab.
        has_indexers = bool(get_enabled_indexers())
        has_client = bool(get_active_download_client())

        results = []
        errors = []
        if has_indexers:
            res = search_usenet_for_issue(
                series, issue,
                issue_year=data.get('issue_year'),
                series_volume=data.get('series_volume'),
            )
            results = sorted(res.get("all_results", []),
                             key=lambda r: r.get("score", 0), reverse=True)
            errors = res.get("errors", [])
        return jsonify({
            "success": True,
            "results": results,
            "errors": errors,
            "has_indexers": has_indexers,
            "has_client": has_client,
            "usenet_first": usenet_precedes_getcomics(),
        })
    except Exception as e:
        app_logger.error(f"Error searching usenet: {e}")
        return jsonify({"error": str(e)}), 500


@download_clients_bp.route('/api/usenet/grab', methods=['POST'])
def usenet_grab():
    """Submit a chosen NZB URL to the active download client."""
    try:
        from models.usenet import grab_nzb

        data = request.get_json() or {}
        nzb_url = data.get('nzb_url')
        filename = data.get('filename')
        if not nzb_url or not filename:
            return jsonify({"error": "nzb_url and filename are required"}), 400

        download_id = grab_nzb(
            nzb_url, filename,
            series=data.get('series'), issue=data.get('issue'),
        )
        if download_id:
            return jsonify({"success": True, "download_id": download_id})
        return jsonify({"success": False,
                        "error": "No active download client, or the client rejected the NZB"}), 502
    except Exception as e:
        app_logger.error(f"Error grabbing NZB: {e}")
        return jsonify({"error": str(e)}), 500
