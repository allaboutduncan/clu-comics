"""
Series Blueprint

Provides routes for:
- Releases, Wanted, Pull List pages
- Series search, view, and sync
- Series mapping, subscription
- Issue manual status management
- Publishers CRUD
- Libraries CRUD
"""

import os
import re
import threading
import time
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash, current_app
from models import metron
import app_state
from app_logging import app_logger
from helpers.collection import match_issues_to_collection
from database import (
    get_series_by_id, get_issues_for_series, save_issues_bulk,
    update_series_sync_time, delete_issues_for_series,
    get_series_needing_sync, get_wanted_issues, get_libraries
)
from helpers.library import get_library_for_path

series_bp = Blueprint('series', __name__)


# =============================================================================
# Pages
# =============================================================================

@series_bp.route('/releases')
def releases():
    """
    Weekly Releases page integrated with Metron.
    Shows releases for a specific week or upcoming releases.
    """
    from database import get_tracked_series_lookup, normalize_series_name

    # Get tracked series lookup for highlighting
    tracked_lookup = get_tracked_series_lookup()

    api = None
    metron_username = current_app.config.get("METRON_USERNAME", "").strip()
    metron_password = current_app.config.get("METRON_PASSWORD", "").strip()
    if metron_username and metron_password:
        api = metron.get_api(metron_username, metron_password)

    if not api:
        return render_template('releases.html',
                             releases=[],
                             error="Metron API not configured or unavailable",
                             date_range="N/A",
                             view_mode="error",
                             tracked_lookup=tracked_lookup,
                             normalize_name=normalize_series_name)

    # Get query params
    date_str = request.args.get('date')
    mode = request.args.get('mode', 'weekly')

    today = datetime.now()
    current_date = today

    if date_str:
        try:
            current_date = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            pass

    releases_list = []

    # Calculate week boundaries
    start_date, end_date = metron.calculate_comic_week(current_date)

    # Helper for formatting
    fmt = '%Y-%m-%d'

    if mode == 'future':
        curr_start, curr_end = metron.calculate_comic_week(today)
        next_week_end = curr_end + timedelta(days=7)

        future_start = next_week_end

        releases_list = metron.get_releases(api,
                                          date_after=future_start.strftime(fmt),
                                          date_before=None)

        display_date_range = f"Future (After {future_start.strftime(fmt)})"

    else:
        # Weekly mode (default)
        start_str = start_date.strftime(fmt)
        end_str = end_date.strftime(fmt)

        releases_list = metron.get_releases(api,
                                          date_after=start_str,
                                          date_before=end_str)

        display_date_range = f"Week of {start_str} to {end_str}"

    # Navigation links
    prev_week_date = (start_date - timedelta(days=7)).strftime(fmt)
    next_week_date = (start_date + timedelta(days=7)).strftime(fmt)

    return render_template('releases.html',
                         releases=releases_list,
                         date_range=display_date_range,
                         view_mode=mode,
                         current_date=start_date.strftime(fmt),
                         prev_date=prev_week_date,
                         next_date=next_week_date,
                         tracked_lookup=tracked_lookup,
                         normalize_name=normalize_series_name)


@series_bp.route('/wanted')
def wanted():
    """
    Wanted Issues page - shows cached missing issues from mapped series.
    Fast load from database cache, refresh via API endpoint.
    """
    from database import get_cached_wanted_issues, get_wanted_cache_age
    from app import refresh_wanted_cache_background

    # Load from cache (fast - no file I/O)
    cached = get_cached_wanted_issues()
    cache_age = get_wanted_cache_age()

    # If cache is empty and not currently refreshing, trigger background refresh
    # But skip if we just refreshed recently (prevents infinite reload when no wanted issues exist)
    recently_refreshed = (time.time() - app_state.wanted_last_refresh_time) < 60
    if not cached and not app_state.wanted_refresh_in_progress and not recently_refreshed:
        threading.Thread(target=refresh_wanted_cache_background, daemon=True).start()
        return render_template('wanted.html',
                             upcoming=[],
                             missing=[],
                             series_stats=[],
                             total_wanted=0,
                             total_upcoming=0,
                             total_missing=0,
                             loading=True,
                             refreshing=True,
                             cache_age=None)

    # Convert cached data to format expected by template
    from datetime import date
    today = date.today().isoformat()

    wanted_issues = []
    series_stats_dict = {}

    for item in cached:
        issue_data = {
            'id': item['issue_id'],
            'number': item['issue_number'],
            'name': item['issue_name'],
            'store_date': item['store_date'],
            'cover_date': item['cover_date'],
            'image': item['image']
        }

        series_data = {
            'id': item['series_id'],
            'name': item['series_name'],
            'volume': item['series_volume']
        }

        wanted_issues.append({
            'issue': issue_data,
            'series': series_data,
            'series_id': item['series_id'],
            'series_name': item['series_name'],
            'series_volume': item['series_volume']
        })

        # Track series stats
        if item['series_id'] not in series_stats_dict:
            series_stats_dict[item['series_id']] = {
                'series': series_data,
                'missing': 0
            }
        series_stats_dict[item['series_id']]['missing'] += 1

    series_stats = list(series_stats_dict.values())

    # Sort by store_date
    def sort_key(item):
        store_date = item['issue'].get('store_date') or '9999-99-99'
        series_name = item['series_name'] or ''
        issue_num = item['issue'].get('number') or ''
        try:
            issue_num_int = int(issue_num)
        except (ValueError, TypeError):
            issue_num_int = 999999
        return (store_date, series_name, issue_num_int)

    wanted_issues.sort(key=sort_key)

    # Group by upcoming (future store_date) vs missing (past/no date)
    upcoming = [w for w in wanted_issues if w['issue'].get('store_date') and w['issue']['store_date'] > today]
    missing = [w for w in wanted_issues if not w['issue'].get('store_date') or w['issue']['store_date'] <= today]

    return render_template('wanted.html',
                         upcoming=upcoming,
                         missing=missing,
                         series_stats=series_stats,
                         total_wanted=len(wanted_issues),
                         total_upcoming=len(upcoming),
                         total_missing=len(missing),
                         loading=False,
                         refreshing=app_state.wanted_refresh_in_progress,
                         cache_age=cache_age)


@series_bp.route('/pull-list')
def pull_list():
    """
    Pull List page - shows all tracked series in the database.
    """
    from database import get_all_mapped_series, get_all_publishers

    series_list = get_all_mapped_series()
    publishers = get_all_publishers()

    return render_template('pull_list.html',
                         series_list=series_list,
                         total_series=len(series_list),
                         publishers=publishers)


@series_bp.route('/series-search')
def series_search():
    """
    Series Search page - search Metron database for series.
    """
    metron_username = current_app.config.get("METRON_USERNAME", "").strip()
    metron_password = current_app.config.get("METRON_PASSWORD", "").strip()
    metron_configured = bool(metron_username and metron_password)

    return render_template('series_search.html',
                          metron_configured=metron_configured)


@series_bp.route('/publishers')
def publishers_page():
    """
    Publishers admin page - manage publishers from Metron or manually.
    """
    from database import get_all_publishers

    publishers = get_all_publishers()

    return render_template('publishers.html',
                         publishers=publishers,
                         total_publishers=len(publishers))


@series_bp.route('/issue/<slug>')
def issue_view(slug):
    """
    Resolve an issue ID to its parent series and redirect to the series view.
    """
    from app import generate_series_slug

    match = re.search(r'-(\d+)$', slug)
    if not match:
        flash("Invalid issue URL format", "error")
        return redirect(url_for('.releases'))

    issue_id = int(match.group(1))

    # 1. Try DB cache first (no API call needed)
    from database import get_issue_by_id
    cached_issue = get_issue_by_id(issue_id)
    if cached_issue:
        series_id = cached_issue['series_id']
        cached_series = get_series_by_id(series_id)
        if cached_series:
            series_slug = generate_series_slug(
                cached_series['name'], series_id, cached_series.get('volume'))
            return redirect(url_for('.series_view', slug=series_slug))

    # 2. Not cached — call api.issue() to resolve series_id
    api = None
    metron_username = current_app.config.get("METRON_USERNAME", "").strip()
    metron_password = current_app.config.get("METRON_PASSWORD", "").strip()
    if metron_username and metron_password:
        api = metron.get_api(metron_username, metron_password)

    if not api:
        flash("Metron API not configured", "error")
        return redirect(url_for('.releases'))

    try:
        full_issue = api.issue(issue_id)
        series_id = full_issue.series.id
        series_name = full_issue.series.name
        series_volume = getattr(full_issue.series, 'volume', None)

        series_slug = generate_series_slug(series_name, series_id, series_volume)
        return redirect(url_for('.series_view', slug=series_slug))
    except Exception as e:
        if metron.is_connection_error(e):
            app_logger.warning(f"Metron unavailable while resolving issue {issue_id}: {e}")
            flash("Metron is currently unavailable. Please try again later.", "error")
        else:
            app_logger.error(f"Could not resolve issue {issue_id}: {e}")
            flash("Issue not found on Metron", "error")
        return redirect(url_for('.releases'))


@series_bp.route('/series/<slug>')
def series_view(slug):
    """
    View all issues in a series.
    URL format: /series/series-name-vVOLUME-ID
    """
    from app import generate_series_slug
    from database import (save_series_mapping, get_publisher,
                          get_series_mapping, get_manual_status_for_series)

    # Extract ID from the end of the slug
    match = re.search(r'-(\d+)$', slug)
    if not match:
        app_logger.error(f"Invalid series URL format - no ID found in slug: {slug}")
        flash("Invalid series URL format", "error")
        return redirect(url_for('.releases'))

    series_id = int(match.group(1))

    # Check for force refresh parameter
    force_refresh = request.args.get('refresh', '').lower() in ('1', 'true', 'yes')

    cached_series = None
    if not force_refresh:
        cached_series = get_series_by_id(series_id)
    else:
        app_logger.info(f"Force refresh requested for series {series_id}, skipping cache")

    # Check for cached series with recent sync (within 24 hours)
    use_cache = False

    if cached_series and cached_series.get('last_synced_at'):
        try:
            last_sync = datetime.fromisoformat(cached_series['last_synced_at'].replace('Z', '+00:00'))
            if datetime.now(last_sync.tzinfo if last_sync.tzinfo else None) - last_sync < timedelta(hours=24):
                use_cache = True
                app_logger.info(f"Using cached data for series {series_id} (synced {cached_series['last_synced_at']})")
        except Exception as e:
            app_logger.warning(f"Could not parse last_synced_at: {e}")

    api = None
    if not use_cache:
        metron_username = current_app.config.get("METRON_USERNAME", "").strip()
        metron_password = current_app.config.get("METRON_PASSWORD", "").strip()
        if metron_username and metron_password:
            api = metron.get_api(metron_username, metron_password)

        if not api:
            if cached_series:
                app_logger.warning(f"API not available, using stale cache for series {series_id}")
                use_cache = True
            else:
                flash("Metron API not configured", "error")
                return redirect(url_for('.releases'))

    try:
        if use_cache and cached_series:
            app_logger.info(f"Loading series {series_id} from cache")
            series_info = cached_series

            if cached_series.get('publisher_id'):
                publisher = get_publisher(cached_series['publisher_id'])
                if publisher:
                    series_info['publisher'] = {'id': publisher['id'], 'name': publisher['name']}

            all_issues = get_issues_for_series(series_id)
            app_logger.info(f"Loaded {len(all_issues)} cached issues")

        else:
            app_logger.info(f"Fetching series details for series_id: {series_id}")
            series_info = api.series(series_id)
            app_logger.info(f"Got series_info: {series_info.name if series_info else 'None'}")

            app_logger.info(f"Fetching all issues for series_id: {series_id}")
            all_issues_result = metron.get_all_issues_for_series(api, series_id)
            all_issues = list(all_issues_result) if all_issues_result else []
            app_logger.info(f"Got {len(all_issues)} issues")

            # Cache the data
            from database import save_publisher
            if hasattr(series_info, 'publisher') and series_info.publisher:
                save_publisher(series_info.publisher.id, series_info.publisher.name)

            if hasattr(series_info, 'model_dump'):
                series_dict_for_save = series_info.model_dump(mode='json')
            elif hasattr(series_info, 'dict'):
                series_dict_for_save = series_info.dict()
            else:
                series_dict_for_save = {'id': series_id, 'name': getattr(series_info, 'name', '')}

            # Compute cover_image from first issue
            cover_image = None
            if all_issues:
                def get_issue_attr(obj, key, default=None):
                    if isinstance(obj, dict):
                        return obj.get(key, default)
                    return getattr(obj, key, default)
                def issue_sort_key(x):
                    num = get_issue_attr(x, 'number')
                    if num and str(num).replace('.', '').isdigit():
                        return float(num)
                    return 999
                sorted_for_cover = sorted(all_issues, key=issue_sort_key)
                if sorted_for_cover:
                    img = get_issue_attr(sorted_for_cover[0], 'image')
                    cover_image = str(img) if img else None

            # Save series to database FIRST (required for foreign key constraint)
            existing_mapping = get_series_mapping(series_id)
            save_series_mapping(series_dict_for_save, existing_mapping or None, cover_image)

            # Now save issues to cache
            save_issues_bulk(all_issues, series_id)
            update_series_sync_time(series_id, len(all_issues))

        # Helper to get attribute from dict or object
        def get_attr(obj, key, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        # Get cover image
        first_issue_image = None
        if use_cache and cached_series:
            first_issue_image = cached_series.get('cover_image')

        if not first_issue_image and all_issues:
            def sort_key(x):
                num = get_attr(x, 'number')
                if num and str(num).replace('.', '').isdigit():
                    return float(num)
                return 999
            sorted_issues = sorted(all_issues, key=sort_key)
            if sorted_issues:
                first_issue_image = get_attr(sorted_issues[0], 'image')

        # Check for existing mapping
        mapped_path = get_series_mapping(series_id)

        # If mapped, check which issues are present
        issue_status = {}
        if mapped_path and os.path.isdir(mapped_path):
            issue_status = match_issues_to_collection(mapped_path, all_issues, series_info)

        # Get manual status
        manual_status = get_manual_status_for_series(series_id)
        if manual_status:
            app_logger.info(f"Series {series_id} manual_status: {manual_status}")

        # Convert series_info to dict for JSON serialization
        series_dict = None
        if series_info:
            if isinstance(series_info, dict):
                series_dict = series_info
            else:
                try:
                    if hasattr(series_info, 'model_dump'):
                        series_dict = series_info.model_dump(mode='json')
                    elif hasattr(series_info, 'dict'):
                        import json
                        series_dict = json.loads(json.dumps(series_info.dict(), default=str))
                    elif hasattr(series_info, '__dict__'):
                        import json
                        series_dict = json.loads(json.dumps(vars(series_info), default=str))
                except Exception as e:
                    app_logger.warning(f"Could not serialize series_info: {e}")
                    series_dict = {
                        'id': getattr(series_info, 'id', None),
                        'name': getattr(series_info, 'name', None),
                        'sort_name': getattr(series_info, 'sort_name', None),
                        'volume': getattr(series_info, 'volume', None),
                        'status': str(getattr(series_info, 'status', '')),
                        'year_began': getattr(series_info, 'year_began', None),
                        'year_end': getattr(series_info, 'year_end', None),
                        'desc': getattr(series_info, 'desc', None),
                        'cv_id': getattr(series_info, 'cv_id', None),
                        'gcd_id': getattr(series_info, 'gcd_id', None),
                        'issue_count': getattr(series_info, 'issue_count', None),
                        'resource_url': str(getattr(series_info, 'resource_url', '')),
                    }
                    publisher = getattr(series_info, 'publisher', None)
                    if publisher:
                        series_dict['publisher'] = {
                            'id': getattr(publisher, 'id', None),
                            'name': getattr(publisher, 'name', None),
                        }

        # Get last_synced_at for UI display
        last_synced_at = None
        if cached_series and cached_series.get('last_synced_at'):
            try:
                sync_dt = datetime.fromisoformat(cached_series['last_synced_at'])
                last_synced_at = sync_dt.strftime('%Y-%m-%d %H:%M')
            except (ValueError, TypeError):
                last_synced_at = cached_series.get('last_synced_at')

        # Get libraries for library selector (Subscribe/Map modals)
        libraries = get_libraries(enabled_only=True)
        default_library = None
        if mapped_path:
            default_library = get_library_for_path(mapped_path)
        if not default_library and libraries:
            default_library = libraries[0]

        from database import get_series_subscription
        series_subscription = get_series_subscription(series_id)

        return render_template('series.html',
                             series=series_info,
                             series_dict=series_dict,
                             issues=all_issues,
                             first_issue_image=first_issue_image,
                             mapped_path=mapped_path,
                             issue_status=issue_status,
                             manual_status=manual_status,
                             last_synced_at=last_synced_at,
                             today=datetime.now().strftime('%Y-%m-%d'),
                             libraries=libraries,
                             default_library=default_library,
                             series_subscription=series_subscription)
    except Exception as e:
        if metron.is_connection_error(e):
            app_logger.warning(f"Metron unavailable while loading series {series_id}: {e}")
            flash("Metron is currently unavailable. Please try again later.", "error")
        else:
            import traceback
            app_logger.error(f"Error fetching series data for series {series_id}: {e}")
            app_logger.error(traceback.format_exc())
            flash(f"Error loading series: {str(e)}", "error")
        return redirect(url_for('.releases'))


# =============================================================================
# Series API
# =============================================================================

@series_bp.route('/api/series/search', methods=['GET'])
def api_search_series():
    """Search Metron API for series by name."""
    from app import generate_series_slug

    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"success": False, "error": "Search query required"}), 400

    metron_username = current_app.config.get("METRON_USERNAME", "").strip()
    metron_password = current_app.config.get("METRON_PASSWORD", "").strip()

    if not metron_username or not metron_password:
        return jsonify({"success": False, "error": "Metron credentials not configured"}), 400

    try:
        api = metron.get_api(metron_username, metron_password)
        if not api:
            return jsonify({"success": False, "error": "Failed to connect to Metron API"}), 500

        results = api.series_list({'name': query})

        series_list = []
        for series in results:
            series_id = getattr(series, 'id', None)
            series_name = getattr(series, 'display_name', '') or getattr(series, 'name', '')
            volume = getattr(series, 'volume', None)
            year_began = getattr(series, 'year_began', None)
            issue_count = getattr(series, 'issue_count', None)
            status = getattr(series, 'status', None)

            publisher = getattr(series, 'publisher', None)
            publisher_name = getattr(publisher, 'name', '') if publisher else ''

            slug = generate_series_slug(series_name, series_id, volume)

            series_list.append({
                "id": series_id,
                "name": series_name,
                "volume": volume,
                "year_began": year_began,
                "publisher": publisher_name,
                "issue_count": issue_count,
                "status": status,
                "slug": slug
            })

        return jsonify({
            "success": True,
            "series": series_list,
            "count": len(series_list)
        })
    except Exception as e:
        if metron.is_connection_error(e):
            app_logger.warning(f"Metron unavailable during series search: {e}")
            return jsonify({"success": False, "error": "Metron is currently unavailable. Please try again later."}), 503
        app_logger.error(f"Error searching Metron series: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@series_bp.route('/api/series/<int:series_id>/map', methods=['POST'])
def map_series(series_id):
    """Map a series to a local directory and save to database."""
    from database import save_publisher, save_series_mapping

    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    mapped_path = data.get('mapped_path')
    series_data = data.get('series')

    if not mapped_path or not series_data:
        return jsonify({'error': 'Missing mapped_path or series data'}), 400

    try:
        publisher = series_data.get('publisher')
        if publisher and isinstance(publisher, dict):
            save_publisher(publisher.get('id'), publisher.get('name'))

        success = save_series_mapping(series_data, mapped_path)

        if success:
            return jsonify({'success': True, 'mapped_path': mapped_path})
        else:
            return jsonify({'error': 'Failed to save mapping'}), 500

    except Exception as e:
        app_logger.error(f"Error mapping series {series_id}: {e}")
        return jsonify({'error': str(e)}), 500


@series_bp.route('/api/series/<int:series_id>/mapping', methods=['GET'])
def get_series_mapping_route(series_id):
    """Get the mapped path for a series."""
    from database import get_series_mapping
    mapped_path = get_series_mapping(series_id)
    return jsonify({'mapped_path': mapped_path})


@series_bp.route('/api/series/<int:series_id>/mapping', methods=['DELETE'])
def delete_series_mapping_route(series_id):
    """Remove the mapping for a series."""
    from database import remove_series_mapping
    success = remove_series_mapping(series_id)
    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'error': 'Failed to remove mapping'}), 500


@series_bp.route('/api/series/<int:series_id>/subscribe', methods=['POST'])
def subscribe_series(series_id):
    """Create folder, map series, and create cvinfo file."""
    from database import save_series_mapping

    data = request.get_json() or {}
    path = data.get('path', '').strip()

    if not path:
        return jsonify({'success': False, 'error': 'Path required'}), 400

    try:
        series = get_series_by_id(series_id)
        if not series:
            return jsonify({'success': False, 'error': 'Series not found'}), 404

        os.makedirs(path, exist_ok=True)
        app_logger.info(f"Created folder for subscription: {path}")

        save_series_mapping(series, path)
        app_logger.info(f"Subscribed series {series_id} to {path}")

        # Create cvinfo file with available metadata
        cv_id = series.get('cv_id')
        metron_id = series.get('id') or series_id
        cvinfo_path = os.path.join(path, 'cvinfo')
        
        # Use metron.create_cvinfo_file to properly handle missing cv_id
        success = metron.create_cvinfo_file(
            cvinfo_path,
            cv_id=cv_id,  # Pass None if cv_id is missing
            series_id=metron_id,
            publisher_name=series.get('publisher_name'),
            start_year=series.get('year_began')
        )
        
        if success:
            if not cv_id:
                app_logger.warning(f"Created cvinfo without ComicVine ID for series {series_id} at {cvinfo_path}")
            else:
                app_logger.info(f"Created cvinfo at {cvinfo_path} with CV ID {cv_id}")
        else:
            app_logger.error(f"Failed to create cvinfo at {cvinfo_path}")

        return jsonify({'success': True, 'path': path})
    except Exception as e:
        app_logger.error(f"Error subscribing series {series_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@series_bp.route('/api/series/<int:series_id>/subscription', methods=['POST'])
def toggle_series_subscription(series_id):
    """Toggle On the Stack subscription for a series."""
    from database import set_series_subscription
    try:
        data = request.get_json()
        enabled = data.get('enabled', True)
        set_series_subscription(series_id, enabled)
        return jsonify({"success": True})
    except Exception as e:
        app_logger.error(f"Error toggling subscription for series {series_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@series_bp.route('/api/series/<int:series_id>/check-collection', methods=['GET'])
def check_series_collection(series_id):
    """
    Check which issues exist in the mapped directory.

    Query params:
        refresh: If 'true', bypass cache and re-scan the directory
    """
    from database import (
        get_series_mapping, invalidate_collection_status_for_series,
        get_manual_status_for_series
    )

    refresh = request.args.get('refresh', 'false').lower() == 'true'

    mapped_path = get_series_mapping(series_id)
    if not mapped_path:
        return jsonify({'error': 'Series not mapped'}), 404

    if not os.path.isdir(mapped_path):
        return jsonify({'error': 'Mapped directory not found'}), 404

    if refresh:
        invalidate_collection_status_for_series(series_id)
        app_logger.info(f"Refreshing collection status for series {series_id}")

    try:
        cached_series = get_series_by_id(series_id)
        cached_issues = get_issues_for_series(series_id)

        if cached_series and cached_issues:
            series_info = cached_series
            all_issues = cached_issues
        else:
            api = None
            metron_username = current_app.config.get("METRON_USERNAME", "").strip()
            metron_password = current_app.config.get("METRON_PASSWORD", "").strip()
            if metron_username and metron_password:
                api = metron.get_api(metron_username, metron_password)

            if not api:
                return jsonify({'error': 'Metron API not configured and no cached data'}), 500

            series_info = api.series(series_id)
            all_issues_result = metron.get_all_issues_for_series(api, series_id)
            all_issues = list(all_issues_result) if all_issues_result else []

        issue_status = match_issues_to_collection(mapped_path, all_issues, series_info, use_cache=not refresh)

        manual_status = get_manual_status_for_series(series_id)

        found_count = sum(1 for s in issue_status.values() if s.get('found'))
        manual_count = len(manual_status)
        manual_not_found = sum(1 for num in manual_status.keys()
                               if not issue_status.get(num, {}).get('found'))
        missing_count = len(all_issues) - found_count - manual_not_found

        return jsonify({
            'success': True,
            'issue_status': issue_status,
            'manual_status': manual_status,
            'found_count': found_count,
            'manual_count': manual_count,
            'missing_count': missing_count,
            'total_count': len(all_issues)
        })

    except Exception as e:
        if metron.is_connection_error(e):
            app_logger.warning(f"Metron unavailable while checking collection for series {series_id}: {e}")
            return jsonify({'error': 'Metron is currently unavailable. Please try again later.'}), 503
        app_logger.error(f"Error checking collection for series {series_id}: {e}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# Manual Status
# =============================================================================

@series_bp.route('/api/series/<int:series_id>/manual-status', methods=['GET'])
def get_series_manual_status(series_id):
    """Get all manually-marked issue statuses for a series."""
    from database import get_manual_status_for_series
    manual_status = get_manual_status_for_series(series_id)
    return jsonify({'success': True, 'manual_status': manual_status})


@series_bp.route('/api/series/<int:series_id>/issue/<issue_number>/manual-status', methods=['POST'])
def set_issue_manual_status(series_id, issue_number):
    """Set or update manual status for an issue."""
    from database import set_manual_status

    data = request.get_json() or {}
    status = data.get('status')
    notes = (data.get('notes') or '').strip() or None

    if status not in ('owned', 'skipped'):
        return jsonify({'error': 'Invalid status. Must be "owned" or "skipped"'}), 400

    success = set_manual_status(series_id, issue_number, status, notes)
    if success:
        return jsonify({'success': True, 'status': status, 'notes': notes})
    else:
        return jsonify({'error': 'Failed to set manual status'}), 500


@series_bp.route('/api/series/<int:series_id>/issue/<issue_number>/manual-status', methods=['DELETE'])
def delete_issue_manual_status(series_id, issue_number):
    """Clear manual status for an issue."""
    from database import clear_manual_status

    success = clear_manual_status(series_id, issue_number)
    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'error': 'Failed to clear manual status'}), 500


@series_bp.route('/api/series/<int:series_id>/bulk-manual-status', methods=['POST'])
def set_bulk_manual_status(series_id):
    """Set manual status for multiple issues at once."""
    from database import bulk_set_manual_status

    data = request.get_json() or {}
    issue_numbers = data.get('issue_numbers', [])
    status = data.get('status')
    notes = data.get('notes', '').strip() or None

    if not issue_numbers:
        return jsonify({'error': 'No issue numbers provided'}), 400

    if status not in ('owned', 'skipped'):
        return jsonify({'error': 'Invalid status. Must be "owned" or "skipped"'}), 400

    count = bulk_set_manual_status(series_id, issue_numbers, status, notes)
    if count >= 0:
        return jsonify({'success': True, 'count': count, 'status': status})
    else:
        return jsonify({'error': 'Failed to set bulk manual status'}), 500


@series_bp.route('/api/series/<int:series_id>/bulk-manual-status', methods=['DELETE'])
def delete_bulk_manual_status(series_id):
    """Clear manual status for multiple issues at once."""
    from database import bulk_clear_manual_status

    data = request.get_json() or {}
    issue_numbers = data.get('issue_numbers', [])

    if not issue_numbers:
        return jsonify({'error': 'No issue numbers provided'}), 400

    count = bulk_clear_manual_status(series_id, issue_numbers)
    if count >= 0:
        return jsonify({'success': True, 'count': count})
    else:
        return jsonify({'error': 'Failed to clear bulk manual status'}), 500


# =============================================================================
# Sync
# =============================================================================

@series_bp.route('/api/sync/series/<int:series_id>', methods=['POST'])
def sync_series(series_id):
    """Force sync a specific series from Metron API"""
    from database import clear_wanted_cache_for_series, get_series_mapping, update_series_desc

    metron_username = current_app.config.get("METRON_USERNAME", "").strip()
    metron_password = current_app.config.get("METRON_PASSWORD", "").strip()
    if not metron_username or not metron_password:
        return jsonify({'error': 'Metron credentials not configured'}), 500

    api = metron.get_api(metron_username, metron_password)
    if not api:
        return jsonify({'error': 'Failed to initialize Metron API'}), 500

    try:
        series_mapping = get_series_by_id(series_id)
        mapped_path = series_mapping.get('mapped_path') if series_mapping else None

        series_info = api.series(series_id)
        if not series_info:
            return jsonify({'error': 'Series not found'}), 404

        # Check if API has desc and database desc is blank - update if so
        api_desc = getattr(series_info, 'desc', None) or (series_info.get('desc') if isinstance(series_info, dict) else None)
        db_desc = series_mapping.get('desc') if series_mapping else None
        if api_desc and not db_desc:
            update_series_desc(series_id, api_desc)
            app_logger.info(f"Updated description for series {series_id}")

        all_issues_result = metron.get_all_issues_for_series(api, series_id)
        all_issues = list(all_issues_result) if all_issues_result else []

        delete_issues_for_series(series_id)
        save_issues_bulk(all_issues, series_id)
        update_series_sync_time(series_id, len(all_issues))

        clear_wanted_cache_for_series(series_id)

        issue_status = {}
        found_count = 0
        missing_count = len(all_issues)

        if mapped_path and os.path.exists(mapped_path):
            issue_status = match_issues_to_collection(mapped_path, all_issues, series_info)
            found_count = sum(1 for s in issue_status.values() if s.get('found'))
            missing_count = len(all_issues) - found_count

        return jsonify({
            'success': True,
            'series_id': series_id,
            'issue_count': len(all_issues),
            'issue_status': issue_status,
            'found_count': found_count,
            'missing_count': missing_count,
            'synced_at': datetime.now().isoformat()
        })

    except Exception as e:
        if metron.is_connection_error(e):
            app_logger.warning(f"Metron unavailable while syncing series {series_id}: {e}")
            return jsonify({'error': 'Metron is currently unavailable. Please try again later.'}), 503
        app_logger.error(f"Error syncing series {series_id}: {e}")
        return jsonify({'error': str(e)}), 500


@series_bp.route('/api/sync/all', methods=['POST'])
def sync_all_series():
    """Sync all mapped series that need updating"""
    metron_username = current_app.config.get("METRON_USERNAME", "").strip()
    metron_password = current_app.config.get("METRON_PASSWORD", "").strip()
    if not metron_username or not metron_password:
        return jsonify({'error': 'Metron credentials not configured'}), 500

    api = metron.get_api(metron_username, metron_password)
    if not api:
        return jsonify({'error': 'Failed to initialize Metron API'}), 500

    try:
        hours = request.json.get('hours', 24) if request.is_json else 24
        series_to_sync = get_series_needing_sync(hours)

        results = []
        for series in series_to_sync:
            series_id = series['id']
            try:
                series_info = api.series(series_id)
                if not series_info:
                    results.append({'series_id': series_id, 'success': False, 'error': 'Not found'})
                    continue

                all_issues_result = metron.get_all_issues_for_series(api, series_id)
                all_issues = list(all_issues_result) if all_issues_result else []

                delete_issues_for_series(series_id)
                save_issues_bulk(all_issues, series_id)
                update_series_sync_time(series_id, len(all_issues))

                results.append({
                    'series_id': series_id,
                    'success': True,
                    'issue_count': len(all_issues)
                })

            except Exception as e:
                error_msg = 'Metron is currently unavailable' if metron.is_connection_error(e) else str(e)
                log = app_logger.warning if metron.is_connection_error(e) else app_logger.error
                log(f"Error syncing series {series_id}: {e}")
                results.append({'series_id': series_id, 'success': False, 'error': error_msg})

        return jsonify({
            'success': True,
            'synced': len([r for r in results if r['success']]),
            'failed': len([r for r in results if not r['success']]),
            'results': results
        })

    except Exception as e:
        app_logger.error(f"Error in sync_all_series: {e}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# Wanted API
# =============================================================================

@series_bp.route('/api/wanted', methods=['GET'])
def get_wanted_issues_api():
    """Get all wanted issues across all tracked series"""
    try:
        wanted = get_wanted_issues()
        return jsonify({
            'success': True,
            'issues': wanted,
            'count': len(wanted)
        })
    except Exception as e:
        app_logger.error(f"Error getting wanted issues: {e}")
        return jsonify({'error': str(e)}), 500


@series_bp.route('/api/scan-downloads', methods=['POST'])
def api_scan_downloads():
    """Scan TARGET folder for wanted issues."""
    from app import process_incoming_wanted_issues

    try:
        process_incoming_wanted_issues()
        return jsonify({
            "success": True,
            "message": "Download directory scan complete"
        })
    except Exception as e:
        app_logger.error(f"Failed to scan downloads: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@series_bp.route('/api/refresh-wanted', methods=['POST'])
def api_refresh_wanted():
    """Trigger wanted issues cache refresh in background."""
    from app import refresh_wanted_cache_background

    try:
        if app_state.wanted_refresh_in_progress:
            return jsonify({
                "success": True,
                "message": "Refresh already in progress"
            })

        threading.Thread(target=refresh_wanted_cache_background, daemon=True).start()
        return jsonify({
            "success": True,
            "message": "Wanted issues refresh started"
        })
    except Exception as e:
        app_logger.error(f"Failed to start wanted refresh: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@series_bp.route('/api/wanted-status', methods=['GET'])
def api_wanted_status():
    """Get wanted issues cache refresh status."""
    from database import get_wanted_cache_age, get_cached_wanted_issues

    try:
        cache_age = get_wanted_cache_age()
        cached = get_cached_wanted_issues()
        count = len(cached) if cached else 0

        return jsonify({
            "refreshing": app_state.wanted_refresh_in_progress,
            "cache_age": cache_age,
            "count": count
        })
    except Exception as e:
        app_logger.error(f"Failed to get wanted status: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Libraries API
# =============================================================================

@series_bp.route('/api/libraries', methods=['GET'])
def api_get_libraries():
    """Get all configured libraries."""
    from database import get_libraries

    try:
        include_disabled = request.args.get('all', '').lower() == 'true'
        libraries = get_libraries(enabled_only=not include_disabled)
        return jsonify({
            "success": True,
            "libraries": libraries
        })
    except Exception as e:
        app_logger.error(f"Error getting libraries: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@series_bp.route('/api/libraries', methods=['POST'])
def api_add_library():
    """Add a new library."""
    from database import add_library
    from app import scan_filesystem_for_sync
    from database import sync_file_index_incremental, invalidate_browse_cache

    data = request.get_json() or {}
    name = data.get('name', '').strip()
    path = data.get('path', '').strip()

    if not name:
        return jsonify({"success": False, "error": "Library name is required"}), 400
    if not path:
        return jsonify({"success": False, "error": "Library path is required"}), 400

    if not os.path.exists(path):
        return jsonify({"success": False, "error": f"Path does not exist: {path}"}), 400
    if not os.path.isdir(path):
        return jsonify({"success": False, "error": f"Path is not a directory: {path}"}), 400

    try:
        library_id = add_library(name, path)
        if library_id:
            def rebuild_index_for_new_library():
                try:
                    app_logger.info(f"Rebuilding file index after adding library: {name}")
                    filesystem_entries = scan_filesystem_for_sync()
                    sync_file_index_incremental(filesystem_entries)
                    app_logger.info(f"File index rebuilt successfully for new library: {name}")
                except Exception as e:
                    app_logger.error(f"Error rebuilding index for new library: {e}")

            thread = threading.Thread(target=rebuild_index_for_new_library, daemon=True)
            thread.start()

            return jsonify({
                "success": True,
                "id": library_id,
                "message": f"Library '{name}' added successfully. File index is being rebuilt."
            })
        else:
            return jsonify({"success": False, "error": "Failed to add library (path may already exist)"}), 400
    except Exception as e:
        app_logger.error(f"Error adding library: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@series_bp.route('/api/libraries/<int:library_id>', methods=['PUT', 'PATCH'])
def api_update_library(library_id):
    """Update an existing library."""
    from database import update_library, get_library_by_id

    existing = get_library_by_id(library_id)
    if not existing:
        return jsonify({"success": False, "error": "Library not found"}), 404

    data = request.get_json() or {}
    name = data.get('name')
    path = data.get('path')
    enabled = data.get('enabled')

    if path:
        path = path.strip()
        if not os.path.exists(path):
            return jsonify({"success": False, "error": f"Path does not exist: {path}"}), 400
        if not os.path.isdir(path):
            return jsonify({"success": False, "error": f"Path is not a directory: {path}"}), 400

    try:
        if update_library(library_id, name=name, path=path, enabled=enabled):
            return jsonify({
                "success": True,
                "message": "Library updated successfully"
            })
        else:
            return jsonify({"success": False, "error": "Failed to update library"}), 400
    except Exception as e:
        app_logger.error(f"Error updating library: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@series_bp.route('/api/libraries/<int:library_id>', methods=['DELETE'])
def api_delete_library(library_id):
    """Delete a library."""
    from database import delete_library, get_library_by_id

    existing = get_library_by_id(library_id)
    if not existing:
        return jsonify({"success": False, "error": "Library not found"}), 404

    try:
        if delete_library(library_id):
            return jsonify({
                "success": True,
                "message": "Library deleted successfully"
            })
        else:
            return jsonify({"success": False, "error": "Failed to delete library"}), 400
    except Exception as e:
        app_logger.error(f"Error deleting library: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# =============================================================================
# Publishers API
# =============================================================================

@series_bp.route('/api/publishers', methods=['GET'])
def api_get_publishers():
    """Get all publishers from the database."""
    from database import get_all_publishers

    try:
        publishers = get_all_publishers()
        return jsonify({
            "success": True,
            "publishers": publishers
        })
    except Exception as e:
        app_logger.error(f"Error getting publishers: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@series_bp.route('/api/publishers', methods=['POST'])
def api_add_publisher():
    """Add a new publisher."""
    from database import save_publisher, get_db_connection

    data = request.get_json() or {}
    publisher_id = data.get('id')
    name = data.get('name')
    path = data.get('path')
    logo = data.get('logo')

    if not name:
        return jsonify({"success": False, "error": "Name is required"}), 400

    if publisher_id is None:
        conn = get_db_connection()
        if conn:
            c = conn.cursor()
            c.execute('SELECT MIN(id) FROM publishers')
            min_id = c.fetchone()[0]
            publisher_id = (min_id - 1) if min_id and min_id < 0 else -1
            conn.close()

    try:
        success = save_publisher(publisher_id, name, path, logo)
        if success:
            return jsonify({"success": True, "id": publisher_id})
        else:
            return jsonify({"success": False, "error": "Failed to save publisher"}), 500
    except Exception as e:
        app_logger.error(f"Error adding publisher: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@series_bp.route('/api/publishers/<signed:publisher_id>', methods=['DELETE'])
def api_delete_publisher(publisher_id):
    """Delete a publisher."""
    from database import delete_publisher

    try:
        success = delete_publisher(publisher_id)
        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Publisher not found"}), 404
    except Exception as e:
        app_logger.error(f"Error deleting publisher: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@series_bp.route('/api/publishers/<signed:publisher_id>', methods=['PUT', 'PATCH'])
def api_update_publisher(publisher_id):
    """Update a publisher."""
    from database import get_db_connection

    data = request.get_json() or {}
    name = data.get('name')
    path = data.get('path')
    logo = data.get('logo')

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"success": False, "error": "Database connection failed"}), 500

        c = conn.cursor()

        ALLOWED_COLUMNS = {'name', 'path', 'logo'}
        updates = []
        params = []

        if name is not None:
            updates.append('name')
            params.append(name)
        if path is not None:
            updates.append('path')
            params.append(path if path else None)
        if logo is not None:
            updates.append('logo')
            params.append(logo if logo else None)

        if not updates:
            conn.close()
            return jsonify({"success": False, "error": "No fields to update"}), 400

        if not all(col in ALLOWED_COLUMNS for col in updates):
            conn.close()
            return jsonify({"success": False, "error": "Invalid field"}), 400

        set_clause = ', '.join(col + ' = ?' for col in updates)
        params.append(publisher_id)
        c.execute('UPDATE publishers SET ' + set_clause + ' WHERE id = ?', params)  # nosec B608 - columns validated against ALLOWED_COLUMNS
        conn.commit()

        if c.rowcount > 0:
            conn.close()
            app_logger.info(f"Updated publisher ID: {publisher_id}")
            return jsonify({"success": True})
        else:
            conn.close()
            return jsonify({"success": False, "error": "Publisher not found"}), 404

    except Exception as e:
        app_logger.error(f"Error updating publisher: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@series_bp.route('/api/publishers/search', methods=['GET'])
def api_search_publishers():
    """Search Metron API for publishers."""
    from database import get_all_publishers

    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"success": False, "error": "Search query required"}), 400

    metron_username = current_app.config.get("METRON_USERNAME", "").strip()
    metron_password = current_app.config.get("METRON_PASSWORD", "").strip()

    if not metron_username or not metron_password:
        return jsonify({"success": False, "error": "Metron credentials not configured"}), 400

    try:
        api = metron.get_api(metron_username, metron_password)
        if not api:
            return jsonify({"success": False, "error": "Failed to connect to Metron API"}), 500

        results = api.publishers_list({'name': query})

        existing_publishers = get_all_publishers()
        existing_ids = {p['id'] for p in existing_publishers}

        publishers = []
        for pub in results:
            pub_id = pub.id if hasattr(pub, 'id') else pub.get('id')
            pub_name = pub.name if hasattr(pub, 'name') else pub.get('name')
            pub_image = str(pub.image) if hasattr(pub, 'image') and pub.image else None

            publishers.append({
                "id": pub_id,
                "name": pub_name,
                "image": pub_image,
                "exists": pub_id in existing_ids
            })

        return jsonify({
            "success": True,
            "publishers": publishers
        })
    except Exception as e:
        if metron.is_connection_error(e):
            app_logger.warning(f"Metron unavailable during publisher search: {e}")
            return jsonify({"success": False, "error": "Metron is currently unavailable. Please try again later."}), 503
        app_logger.error(f"Error searching Metron publishers: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@series_bp.route('/api/publishers/<signed:publisher_id>/logo', methods=['POST'])
def api_download_publisher_logo(publisher_id):
    """Download and save a publisher logo from URL."""
    from database import update_publisher_logo
    import urllib.request
    import urllib.error

    data = request.get_json() or {}
    logo_url = data.get('url')

    if not logo_url:
        return jsonify({"success": False, "error": "Logo URL required"}), 400

    from urllib.parse import urlparse
    parsed = urlparse(logo_url)
    if parsed.scheme not in ('http', 'https'):
        return jsonify({"success": False, "error": "Only HTTP/HTTPS URLs are allowed"}), 400

    try:
        cache_dir = current_app.config.get("CACHE_DIR", "/cache")
        logos_dir = os.path.join(cache_dir, "publisher_logos")
        os.makedirs(logos_dir, exist_ok=True)

        ext = os.path.splitext(logo_url.split('?')[0])[1] or '.png'
        logo_filename = f"{publisher_id}{ext}"
        logo_path = os.path.join(logos_dir, logo_filename)

        req = urllib.request.Request(logo_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as response:  # nosec B310 - scheme validated above
            with open(logo_path, 'wb') as f:
                f.write(response.read())

        relative_path = f"publisher_logos/{logo_filename}"
        success = update_publisher_logo(publisher_id, relative_path)

        if success:
            return jsonify({"success": True, "logo_path": relative_path})
        else:
            return jsonify({"success": False, "error": "Failed to update database"}), 500

    except urllib.error.URLError as e:
        app_logger.error(f"Error downloading logo: {e}")
        return jsonify({"success": False, "error": f"Download failed: {e}"}), 500
    except Exception as e:
        app_logger.error(f"Error saving publisher logo: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
