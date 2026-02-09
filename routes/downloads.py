"""
Downloads Blueprint

Provides routes for:
- GetComics search and download
- GetComics auto-download schedule
- Series sync schedule
- Weekly packs configuration, history, and status
"""

import uuid
import threading
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, render_template
import app_state
from app_logging import app_logger

downloads_bp = Blueprint('downloads', __name__)


# =============================================================================
# Pages
# =============================================================================

@downloads_bp.route('/weekly-packs')
def weekly_packs():
    """
    Weekly Packs page - configure automated weekly pack downloads from GetComics.
    """
    from database import get_weekly_packs_config, get_weekly_packs_history

    config = get_weekly_packs_config()
    history = get_weekly_packs_history(limit=20)

    return render_template('weekly_packs.html',
                         config=config,
                         history=history)


# =============================================================================
# GetComics Search & Download
# =============================================================================

@downloads_bp.route('/api/getcomics/search')
def api_getcomics_search():
    """Search getcomics.org for comics."""
    from models.getcomics import search_getcomics

    query = request.args.get('q', '')
    if not query:
        return jsonify({"success": False, "error": "Query required"}), 400

    try:
        results = search_getcomics(query)
        return jsonify({"success": True, "results": results})
    except Exception as e:
        app_logger.error(f"Error searching getcomics: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@downloads_bp.route('/api/getcomics/download', methods=['POST'])
def api_getcomics_download():
    """Get download link from getcomics page and queue download."""
    from models.getcomics import get_download_links
    from api import download_queue, download_progress


    data = request.get_json() or {}
    page_url = data.get('url')
    filename = data.get('filename', 'comic.cbz')

    if not page_url:
        return jsonify({"success": False, "error": "URL required"}), 400

    try:
        links = get_download_links(page_url)

        # Priority: PIXELDRAIN > DOWNLOAD NOW
        download_url = links.get("pixeldrain") or links.get("download_now")

        if not download_url:
            return jsonify({"success": False, "error": "No download link found"}), 404

        # Queue download using existing system
        download_id = str(uuid.uuid4())
        download_progress[download_id] = {
            'url': download_url,
            'progress': 0,
            'bytes_total': 0,
            'bytes_downloaded': 0,
            'status': 'queued',
            'filename': filename,
            'error': None,
        }
        task = {
            'download_id': download_id,
            'url': download_url,
            'dest_filename': filename,
            'internal': True  # Use basic headers (no custom_headers_str required)
        }
        download_queue.put(task)

        return jsonify({"success": True, "download_id": download_id})
    except Exception as e:
        app_logger.error(f"Error downloading from getcomics: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# =============================================================================
# Sync Schedule
# =============================================================================

@downloads_bp.route('/api/get-sync-schedule', methods=['GET'])
def api_get_sync_schedule():
    """Get the current series sync schedule configuration."""
    try:
        from database import get_sync_schedule

        schedule = get_sync_schedule()
        if not schedule:
            return jsonify({
                "success": True,
                "schedule": {
                    "frequency": "disabled",
                    "time": "03:00",
                    "weekday": 0
                },
                "next_run": "Not scheduled",
                "last_sync": None
            })

        # Calculate next run time
        next_run = "Not scheduled"
        if schedule['frequency'] != 'disabled':
            try:
                jobs = app_state.sync_scheduler.get_jobs()
                if jobs:
                    next_run_time = jobs[0].next_run_time
                    if next_run_time:
                        next_run = next_run_time.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                pass

        return jsonify({
            "success": True,
            "schedule": {
                "frequency": schedule['frequency'],
                "time": schedule['time'],
                "weekday": schedule['weekday']
            },
            "next_run": next_run,
            "last_sync": schedule.get('last_sync')
        })
    except Exception as e:
        app_logger.error(f"Failed to get sync schedule: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@downloads_bp.route('/api/save-sync-schedule', methods=['POST'])
def api_save_sync_schedule():
    """Save the series sync schedule configuration."""
    try:
        from database import save_sync_schedule as db_save_sync_schedule
        from app import configure_sync_schedule

        data = request.get_json()
        frequency = data.get('frequency', 'disabled')
        time_str = data.get('time', '03:00')
        weekday = int(data.get('weekday', 0))

        # Validate inputs
        if frequency not in ['disabled', 'daily', 'weekly']:
            return jsonify({"success": False, "error": "Invalid frequency"}), 400

        # Save to database
        if not db_save_sync_schedule(frequency, time_str, weekday):
            return jsonify({"success": False, "error": "Failed to save schedule to database"}), 500

        # Reconfigure the scheduler
        configure_sync_schedule()

        app_logger.info(f"Sync schedule saved: {frequency} at {time_str}")

        return jsonify({
            "success": True,
            "message": f"Sync schedule saved successfully: {frequency} at {time_str}"
        })
    except Exception as e:
        app_logger.error(f"Failed to save sync schedule: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# =============================================================================
# GetComics Schedule
# =============================================================================

@downloads_bp.route('/api/get-getcomics-schedule', methods=['GET'])
def api_get_getcomics_schedule():
    """Get the current GetComics auto-download schedule configuration."""
    try:
        from database import get_getcomics_schedule

        schedule = get_getcomics_schedule()
        if not schedule:
            return jsonify({
                "success": True,
                "schedule": {
                    "frequency": "disabled",
                    "time": "03:00",
                    "weekday": 0
                },
                "next_run": "Not scheduled",
                "last_run": None
            })

        # Calculate next run time
        next_run = "Not scheduled"
        if schedule['frequency'] != 'disabled':
            try:
                jobs = app_state.getcomics_scheduler.get_jobs()
                if jobs:
                    next_run_time = jobs[0].next_run_time
                    if next_run_time:
                        next_run = next_run_time.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                pass

        return jsonify({
            "success": True,
            "schedule": {
                "frequency": schedule['frequency'],
                "time": schedule['time'],
                "weekday": schedule['weekday']
            },
            "next_run": next_run,
            "last_run": schedule.get('last_run')
        })
    except Exception as e:
        app_logger.error(f"Failed to get getcomics schedule: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@downloads_bp.route('/api/save-getcomics-schedule', methods=['POST'])
def api_save_getcomics_schedule():
    """Save the GetComics auto-download schedule configuration."""
    try:
        from database import save_getcomics_schedule
        from app import configure_getcomics_schedule

        data = request.get_json()
        frequency = data.get('frequency', 'disabled')
        time_str = data.get('time', '03:00')
        weekday = int(data.get('weekday', 0))

        # Validate frequency
        if frequency not in ['disabled', 'daily', 'weekly']:
            return jsonify({"success": False, "error": "Invalid frequency"}), 400

        # Validate time format
        try:
            parts = time_str.split(':')
            if len(parts) != 2 or not (0 <= int(parts[0]) <= 23) or not (0 <= int(parts[1]) <= 59):
                raise ValueError("Invalid time format")
        except Exception:
            return jsonify({"success": False, "error": "Invalid time format. Use HH:MM"}), 400

        # Save to database
        if not save_getcomics_schedule(frequency, time_str, weekday):
            return jsonify({"success": False, "error": "Failed to save schedule to database"}), 500

        # Reconfigure the scheduler
        configure_getcomics_schedule()

        app_logger.info(f"GetComics schedule saved: {frequency} at {time_str}")

        return jsonify({
            "success": True,
            "message": f"Schedule saved: {frequency} at {time_str}"
        })
    except Exception as e:
        app_logger.error(f"Failed to save getcomics schedule: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@downloads_bp.route('/api/run-getcomics-now', methods=['POST'])
def api_run_getcomics_now():
    """Manually trigger GetComics auto-download immediately."""
    try:
        from app import scheduled_getcomics_download

        # Run in a background thread to not block the request
        threading.Thread(target=scheduled_getcomics_download, daemon=True).start()
        return jsonify({
            "success": True,
            "message": "GetComics auto-download started in background"
        })
    except Exception as e:
        app_logger.error(f"Failed to start getcomics download: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# =============================================================================
# Weekly Packs
# =============================================================================

@downloads_bp.route('/api/get-weekly-packs-config', methods=['GET'])
def api_get_weekly_packs_config():
    """Get the current Weekly Packs configuration."""
    try:
        from database import get_weekly_packs_config

        config = get_weekly_packs_config()
        if not config:
            return jsonify({
                "success": True,
                "config": {
                    "enabled": False,
                    "format": "JPG",
                    "publishers": [],
                    "weekday": 2,
                    "time": "10:00",
                    "retry_enabled": True,
                    "start_date": None
                },
                "next_run": "Not scheduled",
                "last_run": None,
                "last_successful_pack": None,
                "start_date": None
            })

        # Calculate next run time
        next_run = "Not scheduled"
        if config['enabled']:
            try:
                jobs = app_state.weekly_packs_scheduler.get_jobs()
                if jobs:
                    next_run_time = jobs[0].next_run_time
                    if next_run_time:
                        next_run = next_run_time.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                pass

        return jsonify({
            "success": True,
            "config": {
                "enabled": config['enabled'],
                "format": config['format'],
                "publishers": config['publishers'],
                "weekday": config['weekday'],
                "time": config['time'],
                "retry_enabled": config['retry_enabled'],
                "start_date": config.get('start_date')
            },
            "next_run": next_run,
            "last_run": config.get('last_run'),
            "last_successful_pack": config.get('last_successful_pack'),
            "start_date": config.get('start_date')
        })
    except Exception as e:
        app_logger.error(f"Failed to get weekly packs config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@downloads_bp.route('/api/save-weekly-packs-config', methods=['POST'])
def api_save_weekly_packs_config():
    """Save the Weekly Packs configuration."""
    try:
        from database import save_weekly_packs_config
        from app import configure_weekly_packs_schedule

        data = request.get_json()
        enabled = bool(data.get('enabled', False))
        format_pref = data.get('format', 'JPG')
        publishers = data.get('publishers', [])
        weekday = int(data.get('weekday', 2))
        time_str = data.get('time', '10:00')
        retry_enabled = bool(data.get('retry_enabled', True))
        start_date = data.get('start_date')  # Optional YYYY-MM-DD format

        # Validate start_date if provided
        if start_date:
            try:
                parsed_date = datetime.strptime(start_date, '%Y-%m-%d')
                # Validate it's within 6 months back to current
                now = datetime.now()
                six_months_ago = now - timedelta(days=180)
                if parsed_date < six_months_ago or parsed_date > now:
                    return jsonify({"success": False, "error": "Start date must be within the last 6 months"}), 400
            except ValueError:
                return jsonify({"success": False, "error": "Invalid start_date format. Use YYYY-MM-DD"}), 400

        # Validate format
        if format_pref not in ['JPG', 'WEBP']:
            return jsonify({"success": False, "error": "Invalid format. Use JPG or WEBP"}), 400

        # Validate publishers
        valid_publishers = ['DC', 'Marvel', 'Image', 'INDIE']
        if not all(p in valid_publishers for p in publishers):
            return jsonify({"success": False, "error": f"Invalid publisher. Use: {valid_publishers}"}), 400

        # Validate time format
        try:
            parts = time_str.split(':')
            if len(parts) != 2 or not (0 <= int(parts[0]) <= 23) or not (0 <= int(parts[1]) <= 59):
                raise ValueError("Invalid time format")
        except Exception:
            return jsonify({"success": False, "error": "Invalid time format. Use HH:MM"}), 400

        # Validate weekday
        if not (0 <= weekday <= 6):
            return jsonify({"success": False, "error": "Invalid weekday. Use 0-6 (Mon-Sun)"}), 400

        # Save to database
        if not save_weekly_packs_config(enabled, format_pref, publishers, weekday, time_str, retry_enabled, start_date):
            return jsonify({"success": False, "error": "Failed to save config to database"}), 500

        # Reconfigure the scheduler
        configure_weekly_packs_schedule()

        days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        app_logger.info(f"Weekly packs config saved: enabled={enabled}, {format_pref}, {publishers}, {days[weekday]} at {time_str}")

        return jsonify({
            "success": True,
            "message": f"Weekly packs config saved"
        })
    except Exception as e:
        app_logger.error(f"Failed to save weekly packs config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@downloads_bp.route('/api/run-weekly-packs-now', methods=['POST'])
def api_run_weekly_packs_now():
    """Manually trigger Weekly Packs download immediately."""
    try:
        from app import scheduled_weekly_packs_download

        # Run in a background thread to not block the request
        threading.Thread(target=scheduled_weekly_packs_download, daemon=True).start()
        return jsonify({
            "success": True,
            "message": "Weekly packs download check started in background"
        })
    except Exception as e:
        app_logger.error(f"Failed to start weekly packs download: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@downloads_bp.route('/api/weekly-packs-history', methods=['GET'])
def api_weekly_packs_history():
    """Get recent weekly pack download history."""
    try:
        from database import get_weekly_packs_history

        limit = request.args.get('limit', 20, type=int)
        history = get_weekly_packs_history(limit)

        return jsonify({
            "success": True,
            "history": history
        })
    except Exception as e:
        app_logger.error(f"Failed to get weekly packs history: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@downloads_bp.route('/api/check-weekly-pack-status', methods=['GET'])
def api_check_weekly_pack_status():
    """Check if the latest weekly pack has links available."""
    try:
        from models.getcomics import find_latest_weekly_pack_url, check_weekly_pack_availability

        pack_url, pack_date = find_latest_weekly_pack_url()
        if not pack_url:
            return jsonify({
                "success": True,
                "found": False,
                "message": "Could not find weekly pack on homepage"
            })

        available = check_weekly_pack_availability(pack_url)

        return jsonify({
            "success": True,
            "found": True,
            "pack_date": pack_date,
            "pack_url": pack_url,
            "links_available": available,
            "message": "Links available" if available else "Links not ready yet"
        })
    except Exception as e:
        app_logger.error(f"Failed to check weekly pack status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
