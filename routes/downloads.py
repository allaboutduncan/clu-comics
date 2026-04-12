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
import time
from datetime import datetime, timedelta, date
from flask import Blueprint, request, jsonify, render_template
import core.app_state as app_state
from core.app_logging import app_logger
from core.database import (
    get_all_mapped_series,
    get_issues_for_series,
    get_manual_status_for_series,
    get_series_by_id,
)
from models.getcomics import (
    search_getcomics_for_issue,
    get_download_links,
    score_getcomics_result,
    accept_result,
)
from helpers.collection import match_issues_to_collection
from models.issue import IssueObj, SeriesObj
from core.config import config

downloads_bp = Blueprint('downloads', __name__)


# =============================================================================
# Pages
# =============================================================================

@downloads_bp.route('/weekly-packs')
def weekly_packs():
    """
    Weekly Packs page - configure automated weekly pack downloads from GetComics.
    """
    from core.database import get_weekly_packs_config, get_weekly_packs_history

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
    from core.config import config

    data = request.get_json() or {}
    page_url = data.get('url')
    filename = data.get('filename', 'comic.cbz')

    if not page_url:
        return jsonify({"success": False, "error": "URL required"}), 400

    try:
        links = get_download_links(page_url)

        # Get provider priority from config
        priority_str = config.get("SETTINGS", "DOWNLOAD_PROVIDER_PRIORITY",
                                   fallback="pixeldrain,download_now,mega")
        priority_order = [p.strip() for p in priority_str.split(",") if p.strip()]

        # Build ordered list of available (provider, url) pairs
        available = [(p, links[p]) for p in priority_order if links.get(p)]

        if not available:
            return jsonify({"success": False, "error": "No download link found"}), 404

        primary_provider, download_url = available[0]
        fallback_urls = available[1:]

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
            'provider': None,
        }
        task = {
            'download_id': download_id,
            'url': download_url,
            'dest_filename': filename,
            'internal': True,
            'fallback_urls': fallback_urls,
        }
        download_queue.put(task)

        return jsonify({"success": True, "download_id": download_id})
    except Exception as e:
        app_logger.error(f"Error downloading from getcomics: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def _run_wanted_simulation(limit, target_series_id, target_series_name):
    """Inline simulation logic — avoids importing app.py which has @app.template_filter."""
    simulation_results = []
    start_time = time.time()
    today = date.today().isoformat()

    mapped_series = get_all_mapped_series()
    for series in mapped_series:
        sid = series["id"]
        series_name = series.get("name", "")
        series_year = series.get("volume_year") or series.get("year_began")
        series_volume = series.get("volume")
        mapped_path = series.get("mapped_path")
        publisher_name = series.get("publisher_name")

        if not mapped_path:
            continue

        issues = get_issues_for_series(sid)
        if not issues:
            continue

        issue_objs = [IssueObj(i) for i in issues]
        series_obj = SeriesObj(series)
        issue_status = match_issues_to_collection(mapped_path, issue_objs, series_obj)
        manual_status = get_manual_status_for_series(sid)

        for issue in issues:
            issue_num = str(issue.get("number", ""))
            status = issue_status.get(issue_num, {})
            store_date = issue.get("store_date")

            if status.get("found"):
                continue
            if issue_num in manual_status:
                continue
            if not store_date or store_date > today:
                continue

            issue_year = int(store_date[:4]) if store_date else series_year

            search_variants_str = config.get("SETTINGS", "VARIANT_TYPES", fallback="")
            search_variants = [v.strip().lower() for v in search_variants_str.split(",") if v.strip()]

            ctx_parts = [f"{series_name} #{issue_num}"]
            if series_volume:
                ctx_parts.insert(1, f"Vol {series_volume}")
            if issue_year:
                ctx_parts.append(str(issue_year))
            search_context = "[" + ", ".join(ctx_parts) + "]"

            results = search_getcomics_for_issue(
                series_name=series_name,
                issue_num=issue_num,
                issue_year=issue_year,
                series_volume=series_volume,
                series_year=series_year,
                search_variants=search_variants,
            )

            if not results:
                simulation_results.append({
                    "series": series_name, "issue": issue_num, "issue_year": issue_year,
                    "series_volume": series_volume, "search_context": search_context,
                    "search_params": {
                        "series_name": series_name, "issue_num": issue_num,
                        "issue_year": issue_year, "series_volume": series_volume,
                        "series_year": series_year, "search_variants": search_variants,
                    },
                    "best_accept": None, "best_fallback": None,
                    "all_results": [], "status": "no_results",
                })
                continue

            best_accept = None
            best_fallback = None
            single_found = False
            scored_results = []

            for result in results:
                score, is_range, series_match = score_getcomics_result(
                    result["title"], series_name, issue_num, issue_year,
                    accept_variants=search_variants,
                    series_volume=series_volume,
                    volume_year=series_year,
                    publisher_name=publisher_name,
                )
                decision = accept_result(score, is_range, series_match, single_issue_found=single_found)
                scored_results.append({
                    "title": result.get("title", ""),
                    "link": result.get("link", ""),
                    "download_url": result.get("download_url", ""),
                    "score": score, "decision": decision,
                    "range_contains_target": is_range, "series_match": series_match,
                })
                if decision == "ACCEPT":
                    if best_accept is None or score > best_accept[1]:
                        best_accept = (result, score)
                    single_found = True
                elif decision == "FALLBACK" and best_fallback is None:
                    best_fallback = (result, score)

            chosen = best_accept or best_fallback
            if chosen:
                best_result, best_score = chosen
                tier = "direct match" if best_accept else "range fallback"
                # Use cached links from scrape_and_score_candidate if available,
                # otherwise fall back to re-scraping (for live search results)
                if best_result.get("links"):
                    links = best_result["links"]
                else:
                    links = get_download_links(best_result["link"])
                priority_str = config.get("SETTINGS", "DOWNLOAD_PROVIDER_PRIORITY", fallback="pixeldrain,download_now,mega")
                priority_order = [p.strip() for p in priority_str.split(",") if p.strip()]
                available = [(p, links[p]) for p in priority_order if links.get(p)]
                download_url = available[0][1] if available else None

                if best_accept:
                    best_accept_data = {
                        "result": {
                            "title": best_result.get("title", ""),
                            "link": best_result.get("link", ""),
                            "download_url": download_url,
                        },
                        "score": best_score, "tier": "direct match",
                    }
                else:
                    best_accept_data = None
                best_fallback_data = None
                if best_fallback:
                    best_fallback_data = {
                        "result": {
                            "title": best_fallback[0].get("title", ""),
                            "link": best_fallback[0].get("link", ""),
                            "download_url": None,
                        },
                        "score": best_fallback[1], "tier": "range fallback",
                    }
                simulation_results.append({
                    "series": series_name, "issue": issue_num, "issue_year": issue_year,
                    "series_volume": series_volume, "search_context": search_context,
                    "search_params": {
                        "series_name": series_name, "issue_num": issue_num,
                        "issue_year": issue_year, "series_volume": series_volume,
                        "series_year": series_year, "search_variants": search_variants,
                    },
                    "best_accept": best_accept_data, "best_fallback": best_fallback_data,
                    "all_results": scored_results, "status": "match_found",
                })
            else:
                simulation_results.append({
                    "series": series_name, "issue": issue_num, "issue_year": issue_year,
                    "series_volume": series_volume, "search_context": search_context,
                    "search_params": {
                        "series_name": series_name, "issue_num": issue_num,
                        "issue_year": issue_year, "series_volume": series_volume,
                        "series_year": series_year, "search_variants": search_variants,
                    },
                    "best_accept": None, "best_fallback": None,
                    "all_results": scored_results, "status": "no_match",
                })

    return simulation_results


# =============================================================================
# Simulation
# =============================================================================

@downloads_bp.route('/api/getcomics/simulate', methods=['POST'])
def api_getcomics_simulate():
    """
    Run a dry-run simulation of GetComics wanted-issues search.

    Executes the full search/scoring flow across all tracked series' wanted
    issues, but skips actual downloads. Returns structured JSON showing:
    - What search parameters were used for each issue
    - What GetComics returned
    - How each result was scored
    - What the best match was and why

    Optionally filter to specific series or limit the number of series simulated.
    """
    data = request.get_json() or {}
    series_id = data.get('series_id')  # optional: simulate single series
    limit = data.get('limit', 10)      # max series to simulate (safety limit)

    try:
        # If series_id specified, verify it exists and get the series name
        target_series_name = None
        if series_id:
            series = get_series_by_id(series_id)
            if series:
                target_series_name = series.get("name")
            else:
                return jsonify({"success": False, "error": "Series not found"}), 404

        # Run the simulation inline (avoids importing app.py which has @app.template_filter
        # that can't be registered after first request)
        all_results = _run_wanted_simulation(limit, series_id, target_series_name)

        if all_results is None:
            return jsonify({"success": False, "error": "Simulation failed"}), 500

        # Filter to target series if specified
        if target_series_name:
            all_results = [r for r in all_results if r['series'] == target_series_name]

        # Apply limit (only if not filtering by specific series)
        if series_id:
            # When targeting a specific series, show all its issues
            limited_results = all_results
        else:
            limited_results = all_results[:limit]

        # Compute summary stats from full results (not limited)
        total_series = len(all_results)
        total_issues = len(all_results)
        accept_count = sum(1 for r in all_results if r.get('best_accept'))
        fallback_count = sum(1 for r in all_results if r.get('best_fallback') and not r.get('best_accept'))
        no_match_count = sum(1 for r in all_results if not r.get('best_accept') and not r.get('best_fallback'))
        no_results_count = sum(1 for r in all_results if r.get('status') == 'no_results')

        return jsonify({
            "success": True,
            "simulation": limited_results,
            "summary": {
                "total_series_searched": total_series,
                "total_issues_searched": total_issues,
                "accept_count": accept_count,
                "fallback_count": fallback_count,
                "no_match_count": no_match_count,
                "no_results_count": no_results_count,
                "shown_in_response": len(limited_results),
                "target_series": target_series_name,
            }
        })
    except Exception as e:
        import traceback
        app_logger.error(f"Simulation error: {e}")
        app_logger.error(f"Simulation traceback: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500


# =============================================================================
# Sync Schedule
# =============================================================================

@downloads_bp.route('/api/get-sync-schedule', methods=['GET'])
def api_get_sync_schedule():
    """Get the current series sync schedule configuration."""
    try:
        from core.database import get_sync_schedule

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

        from app import get_next_run_for_job

        return jsonify({
            "success": True,
            "schedule": {
                "frequency": schedule['frequency'],
                "time": schedule['time'],
                "weekday": schedule['weekday']
            },
            "next_run": get_next_run_for_job('series_sync'),
            "last_sync": schedule.get('last_sync')
        })
    except Exception as e:
        app_logger.error(f"Failed to get sync schedule: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@downloads_bp.route('/api/save-sync-schedule', methods=['POST'])
def api_save_sync_schedule():
    """Save the series sync schedule configuration."""
    try:
        from core.database import save_sync_schedule as db_save_sync_schedule
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
        from core.database import get_getcomics_schedule

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

        from app import get_next_run_for_job

        return jsonify({
            "success": True,
            "schedule": {
                "frequency": schedule['frequency'],
                "time": schedule['time'],
                "weekday": schedule['weekday']
            },
            "next_run": get_next_run_for_job('getcomics_download'),
            "last_run": schedule.get('last_run')
        })
    except Exception as e:
        app_logger.error(f"Failed to get getcomics schedule: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@downloads_bp.route('/api/save-getcomics-schedule', methods=['POST'])
def api_save_getcomics_schedule():
    """Save the GetComics auto-download schedule configuration."""
    try:
        from core.database import save_getcomics_schedule
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
        from core.database import get_weekly_packs_config

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

        from app import get_next_run_for_job
        next_run = get_next_run_for_job('weekly_packs_download')

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
        from core.database import save_weekly_packs_config
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
        from core.database import get_weekly_packs_history

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
