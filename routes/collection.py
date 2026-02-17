"""
Collection Blueprint

Provides routes for:
- File browsing and collection pages
- Directory listing, search, recursive browse
- Folder thumbnails and CBZ previews
- Browse by metadata (writer, artist, character, publisher)
- To-read page
"""

import os
import re
import time
import zipfile
import base64
import traceback
from io import BytesIO
from datetime import datetime, timedelta
from flask import (Blueprint, request, jsonify, render_template, redirect,
                   url_for, flash, send_file, current_app)
from PIL import Image
from app_logging import app_logger
from config import config
from helpers.library import get_library_roots, get_default_library, is_valid_library_path
from database import (
    get_directory_children, get_path_counts_batch, get_recent_files,
    invalidate_browse_cache, add_file_index_entry, delete_file_index_entry,
    search_file_index, get_user_preference
)

collection_bp = Blueprint('collection', __name__)

# Dashboard section definitions
DASHBOARD_SECTION_DEFS = {
    'favorites': {
        'id': 'favorites',
        'title': 'Favorite Collections',
        'icon': 'bi-bookmark-heart-fill text-danger',
        'swiper_id': 'favoritesSwiper',
        'view_all_type': 'button',
        'view_all_text': 'View All',
    },
    'want_to_read': {
        'id': 'want_to_read',
        'title': 'Want to Read',
        'icon': 'bi-bookmark-star-fill text-warning',
        'swiper_id': 'wantToReadSwiper',
        'view_all_type': 'link',
        'view_all_href': '/to-read',
        'view_all_text': 'View All',
    },
    'continue_reading': {
        'id': 'continue_reading',
        'title': 'Continue Reading',
        'icon': 'bi-book-half text-info',
        'swiper_id': 'continueReadingSwiper',
        'section_html_id': 'continueReadingSection',
        'view_all_type': 'button',
        'view_all_onclick': 'loadContinueReading()',
        'view_all_text': 'View All',
    },
    'discover': {
        'id': 'discover',
        'title': 'Discover',
        'icon': 'bi-stars text-warning',
    },
    'recently_added': {
        'id': 'recently_added',
        'title': 'Recently Added',
        'icon': 'bi-clock-history text-primary',
        'swiper_id': 'recentAddedSwiper',
        'view_all_type': 'button',
        'view_all_onclick': 'loadRecentlyAdded()',
        'view_all_text': 'View All',
    },
    'library': {
        'id': 'library',
        'title': 'Library',
        'icon': 'bi-collection-fill text-primary',
    },
}

DEFAULT_DASHBOARD_ORDER = ['favorites', 'want_to_read', 'continue_reading', 'discover', 'recently_added', 'library']


def get_dashboard_order():
    """Return the stored dashboard order with any missing sections appended."""
    order = get_user_preference('dashboard_order', default=DEFAULT_DASHBOARD_ORDER)
    # Backfill any sections added after the user last saved
    for section_id in DEFAULT_DASHBOARD_ORDER:
        if section_id not in order:
            order.append(section_id)
    return order


def get_dashboard_sections():
    """Build ordered list of visible dashboard sections from user preferences."""
    order = get_dashboard_order()
    hidden = set(get_user_preference('dashboard_hidden', default=[]))
    rec_enabled = config.get("SETTINGS", "REC_ENABLED", fallback="True") == "True"

    sections = []
    for section_id in order:
        if section_id in hidden:
            continue
        if section_id == 'discover' and not rec_enabled:
            continue
        if section_id in DASHBOARD_SECTION_DEFS:
            sections.append(DASHBOARD_SECTION_DEFS[section_id])
    return sections


# =============================================================================
# Pages
# =============================================================================

@collection_bp.route('/files')
def files_page():
    watch = config.get("SETTINGS", "WATCH", fallback="/temp")
    target_dir = config.get("SETTINGS", "TARGET", fallback="/processed")
    return render_template('files.html', watch=watch, target_dir=target_dir)


@collection_bp.route('/collection')
@collection_bp.route('/collection/<path:subpath>')
def collection(subpath=''):
    """Render the visual browse page with optional path."""
    initial_path = f'/data/{subpath}' if subpath else ''
    return render_template('collection.html',
                           initial_path=initial_path,
                           rec_enabled=config.get("SETTINGS", "REC_ENABLED", fallback="True") == "True",
                           dashboard_sections=get_dashboard_sections())


@collection_bp.route('/to-read')
def to_read_page():
    """Render the 'To Read' page showing all items marked as 'want to read'."""
    return render_template('to_read.html')


@collection_bp.route('/browse/<category>/<path:name>')
def browse_by_metadata(category, name):
    """
    Browse comics by metadata category (writer, penciller, character, publisher).
    """
    from database import get_files_by_metadata_grouped
    from urllib.parse import unquote

    category_mapping = {
        'writer': 'writer',
        'penciller': 'penciller',
        'artist': 'penciller',
        'character': 'characters',
        'characters': 'characters',
        'publisher': 'publisher'
    }

    normalized_category = category_mapping.get(category.lower())
    if not normalized_category:
        flash(f"Invalid browse category: {category}", "error")
        return redirect(url_for('insights_page'))

    decoded_name = unquote(name)

    result = get_files_by_metadata_grouped(normalized_category, decoded_name)

    category_labels = {
        'writer': 'Writer',
        'penciller': 'Artist',
        'characters': 'Character',
        'publisher': 'Publisher'
    }

    group_labels = {
        'characters': 'Series',
        'writer': 'Publisher',
        'penciller': 'Publisher',
        'publisher': 'Series'
    }

    return render_template('browse_metadata.html',
                          category=normalized_category,
                          category_label=category_labels.get(normalized_category, 'Unknown'),
                          group_label=group_labels.get(normalized_category, 'Group'),
                          name=decoded_name,
                          groups=result['groups'],
                          total=result['total'],
                          nested=result.get('nested', False))


# =============================================================================
# Browse API
# =============================================================================

@collection_bp.route('/api/browse')
def api_browse():
    """
    Get directory listing for the browse page.
    Reads directly from file_index database for instant results.
    """
    from app import DATA_DIR

    request_start = time.time()

    path = request.args.get('path')
    if not path:
        path = DATA_DIR

    try:
        app_logger.info(f"/api/browse request for path: {path}")

        directories, files = get_directory_children(path)

        processed_directories = []
        for d in directories:
            dir_info = {
                'name': d['name'],
                'has_thumbnail': d.get('has_thumbnail', False),
                'has_files': None,
                'folder_count': None,
                'file_count': None
            }

            if d.get('has_thumbnail'):
                for ext in ['.png', '.jpg', '.jpeg', '.webp']:
                    thumb_path = os.path.join(d['path'], f'folder{ext}')
                    if os.path.exists(thumb_path):
                        dir_info['thumbnail_url'] = url_for('.serve_folder_thumbnail', path=thumb_path)
                        break

            processed_directories.append(dir_info)

        processed_files = []
        for f in files:
            filename = f['name']
            file_path = f['path']

            file_info = {
                'name': filename,
                'size': f.get('size', 0)
            }

            if filename.lower().endswith(('.cbz', '.cbr', '.zip')):
                file_info['has_thumbnail'] = True
                file_info['thumbnail_url'] = url_for('get_thumbnail', path=file_path)
            else:
                file_info['has_thumbnail'] = False

            file_info['has_comicinfo'] = f.get('has_comicinfo')

            processed_files.append(file_info)

        result = {
            "current_path": path,
            "directories": processed_directories,
            "files": processed_files,
            "parent": os.path.dirname(path) if path != DATA_DIR else None
        }

        # Check for header image
        for ext in ['.jpg', '.png', '.gif', '.jpeg']:
            header_name = f'header{ext}'
            header_path = os.path.join(path, header_name)
            if os.path.exists(header_path):
                result['header_image_url'] = url_for('.serve_folder_thumbnail', path=header_path)
                break

        # Check for overlay image
        overlay_path = os.path.join(path, 'overlay.png')
        if os.path.exists(overlay_path):
            result['overlay_image_url'] = url_for('.serve_folder_thumbnail', path=overlay_path)

        elapsed = time.time() - request_start
        app_logger.info(f"/api/browse returned {len(directories)} dirs, {len(files)} files for {path} in {elapsed:.3f}s")

        return jsonify(result)
    except Exception as e:
        app_logger.error(f"Error browsing {path}: {e}")
        return jsonify({"error": str(e)}), 500


@collection_bp.route('/api/missing-xml')
def api_missing_xml():
    """Get all comic files missing ComicInfo.xml."""
    from database import get_files_missing_comicinfo

    path = request.args.get('path')

    files = get_files_missing_comicinfo(path)

    processed = []
    for f in files:
        file_info = {
            'name': f['name'],
            'path': f['path'],
            'size': f['size'],
            'has_comicinfo': f['has_comicinfo'],
            'has_thumbnail': f['has_thumbnail'],
            'type': 'file'
        }
        if f['has_thumbnail'] or f['name'].lower().endswith(('.cbz', '.cbr', '.zip')):
            file_info['has_thumbnail'] = True
            file_info['thumbnail_url'] = url_for('get_thumbnail', path=f['path'])

        processed.append(file_info)

    return jsonify({"files": processed, "total": len(processed)})


@collection_bp.route('/api/issues-read-paths')
def api_issues_read_paths():
    """Return list of all read issue paths for client-side caching."""
    from database import get_issues_read
    issues = get_issues_read()
    paths = [issue['issue_path'] for issue in issues]
    return jsonify({"paths": paths})


@collection_bp.route('/api/scan-directory', methods=['POST'])
def api_scan_directory():
    """
    Recursively scan a directory and update the file_index.
    """
    from app import DATA_DIR

    data = request.get_json()
    path = data.get('path')

    if not path:
        return jsonify({"error": "Missing path parameter"}), 400

    normalized_path = os.path.normpath(path)
    normalized_data_dir = os.path.normpath(DATA_DIR)
    if not normalized_path.startswith(normalized_data_dir):
        return jsonify({"error": "Access denied"}), 403

    if not os.path.exists(path):
        return jsonify({"error": "Directory not found"}), 404

    if not os.path.isdir(path):
        return jsonify({"error": "Path is not a directory"}), 400

    try:
        app_logger.info(f"Starting recursive scan of: {path}")
        scan_start = time.time()

        excluded_extensions = {".png", ".jpg", ".jpeg", ".gif", ".html", ".css", ".ds_store", ".json", ".db", ".xml", ".webp"}
        allowed_files = {"missing.txt"}

        delete_file_index_entry(path)

        dir_count = 0
        file_count = 0

        def check_has_thumbnail(folder_path):
            for ext in ['.png', '.jpg', '.jpeg']:
                if os.path.exists(os.path.join(folder_path, f'folder{ext}')):
                    return 1
            return 0

        parent_dir = os.path.dirname(path)
        add_file_index_entry(
            name=os.path.basename(path),
            path=path,
            entry_type='directory',
            parent=parent_dir,
            has_thumbnail=check_has_thumbnail(path)
        )
        dir_count += 1

        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not d.startswith(('.', '_'))]

            for d in dirs:
                full_path = os.path.join(root, d)
                add_file_index_entry(
                    name=d,
                    path=full_path,
                    entry_type='directory',
                    parent=root,
                    has_thumbnail=check_has_thumbnail(full_path)
                )
                dir_count += 1

            for f in files:
                if f.startswith(('.', '_')):
                    continue

                _, ext = os.path.splitext(f.lower())
                if f.lower() not in allowed_files and ext in excluded_extensions:
                    continue

                full_path = os.path.join(root, f)
                try:
                    size = os.path.getsize(full_path)
                except (OSError, IOError):
                    size = 0

                add_file_index_entry(
                    name=f,
                    path=full_path,
                    entry_type='file',
                    parent=root,
                    size=size
                )
                file_count += 1

        elapsed = time.time() - scan_start
        app_logger.info(f"Scan complete: {path} - {dir_count} directories, {file_count} files in {elapsed:.2f}s")

        return jsonify({
            "success": True,
            "message": f"Scanned {path}",
            "directories": dir_count,
            "files": file_count,
            "elapsed": round(elapsed, 2)
        })

    except Exception as e:
        app_logger.error(f"Error scanning directory {path}: {e}")
        return jsonify({"error": str(e)}), 500


@collection_bp.route('/api/browse-metadata', methods=['POST'])
def api_browse_metadata():
    """
    Batch fetch metadata (counts) for multiple paths.
    """
    data = request.get_json()
    paths = data.get('paths', [])

    if not paths:
        return jsonify({"error": "No paths provided"}), 400

    if len(paths) > 100:
        return jsonify({"error": "Too many paths (max 100)"}), 400

    try:
        counts = get_path_counts_batch(paths)

        results = {}
        for path, (folder_count, file_count) in counts.items():
            results[path] = {
                'folder_count': folder_count,
                'file_count': file_count,
                'has_files': file_count > 0
            }

        return jsonify({"metadata": results})

    except Exception as e:
        app_logger.error(f"Error fetching browse metadata: {e}")
        return jsonify({"error": str(e)}), 500


@collection_bp.route('/api/browse-thumbnails', methods=['POST'])
def api_browse_thumbnails():
    """
    Batch fetch folder thumbnails for multiple paths.
    """
    from app import find_folder_thumbnails_batch

    data = request.get_json()
    paths = data.get('paths', [])

    if not paths:
        return jsonify({"error": "No paths provided"}), 400

    if len(paths) > 50:
        return jsonify({"error": "Too many paths (max 50)"}), 400

    try:
        folder_thumbs = find_folder_thumbnails_batch(paths)

        results = {}
        for path, thumb in folder_thumbs.items():
            if thumb:
                results[path] = {
                    'has_thumbnail': True,
                    'thumbnail_url': url_for('.serve_folder_thumbnail', path=thumb)
                }
            else:
                results[path] = {
                    'has_thumbnail': False,
                    'thumbnail_url': None
                }

        return jsonify({"thumbnails": results})

    except Exception as e:
        app_logger.error(f"Error fetching browse thumbnails: {e}")
        return jsonify({"error": str(e)}), 500


@collection_bp.route('/api/clear-browse-cache', methods=['POST'])
def api_clear_browse_cache():
    """Clear the browse cache to force refresh on next load."""
    from app import clear_browse_cache

    try:
        data = request.get_json() or {}
        path = data.get('path')

        if path:
            invalidate_browse_cache(path)
            app_logger.info(f"Cleared browse cache for: {path}")
            return jsonify({
                "success": True,
                "message": f"Browse cache cleared for {path}"
            })
        else:
            clear_browse_cache()
            app_logger.info("Cleared all browse cache")
            return jsonify({
                "success": True,
                "message": "All browse cache cleared"
            })
    except Exception as e:
        app_logger.error(f"Error clearing browse cache: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@collection_bp.route('/api/browse-recursive')
def api_browse_recursive():
    """Get all files recursively from a directory and subdirectories."""
    from app import DATA_DIR

    path = request.args.get('path', '')

    if not path:
        full_path = DATA_DIR
    else:
        full_path = path

    if not os.path.exists(full_path) or not os.path.isdir(full_path):
        return jsonify({"error": "Invalid path"}), 400

    excluded_extensions = {".png", ".jpg", ".jpeg", ".gif", ".html", ".css", ".ds_store", ".json", ".db", ".xml"}
    allowed_files = {"missing.txt", "cvinfo"}

    files = []

    for root, dirs, filenames in os.walk(full_path):
        for filename in filenames:
            _, ext = os.path.splitext(filename.lower())

            if filename.lower() not in allowed_files and ext in excluded_extensions:
                continue
            if filename.startswith(('.', '-', '_')):
                continue

            file_path = os.path.join(root, filename)

            rel_path = os.path.relpath(file_path, DATA_DIR)

            try:
                stat_info = os.stat(file_path)
                file_info = {
                    "name": filename,
                    "path": rel_path,
                    "size": stat_info.st_size,
                    "modified": stat_info.st_mtime,
                    "type": "file"
                }

                if filename.lower().endswith(('.cbz', '.cbr', '.zip')):
                    file_info['has_thumbnail'] = True
                    file_info['thumbnail_url'] = url_for('get_thumbnail', path=file_path)
                else:
                    file_info['has_thumbnail'] = False

                files.append(file_info)
            except Exception as e:
                app_logger.warning(f"Error processing file {file_path}: {e}")
                continue

    # Sort files by series name, year, then issue number
    def natural_sort_key(item):
        filename = item['name']

        match = re.match(r'^(.+?)\s+#?(\d+)\s*\((\d{4})\)', filename, re.IGNORECASE)
        if match:
            series_name = match.group(1).strip().lower()
            issue_number = int(match.group(2))
            year = int(match.group(3))
            return (series_name, year, issue_number, filename.lower())

        match_no_year = re.match(r'^(.+?)\s+#?(\d+)', filename, re.IGNORECASE)
        if match_no_year:
            series_name = match_no_year.group(1).strip().lower()
            issue_number = int(match_no_year.group(2))
            return (series_name, 0, issue_number, filename.lower())

        return (filename.lower(), 0, 0, filename.lower())

    files.sort(key=natural_sort_key)

    return jsonify({
        "current_path": path,
        "files": files,
        "total": len(files)
    })


@collection_bp.route('/api/browse/<category>/<path:name>')
def api_browse_by_metadata(category, name):
    """
    API endpoint for paginated browse results.
    """
    from database import get_files_by_metadata
    from urllib.parse import unquote

    category_mapping = {
        'writer': 'writer',
        'penciller': 'penciller',
        'artist': 'penciller',
        'character': 'characters',
        'characters': 'characters',
        'publisher': 'publisher'
    }

    normalized_category = category_mapping.get(category.lower())
    if not normalized_category:
        return jsonify({"error": "Invalid category"}), 400

    decoded_name = unquote(name)
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)

    result = get_files_by_metadata(normalized_category, decoded_name, limit=limit, offset=offset)

    for file_info in result['files']:
        file_info['thumbnail_url'] = url_for('get_thumbnail', path=file_info['path'])

    return jsonify(result)


@collection_bp.route('/api/folder-thumbnail')
def serve_folder_thumbnail():
    """Serve a folder thumbnail image."""
    image_path = request.args.get('path')

    if not image_path:
        app_logger.error("No path provided for folder thumbnail")
        return send_file('static/images/error.svg', mimetype='image/svg+xml')

    image_path = os.path.normpath(image_path)

    if not os.path.exists(image_path):
        app_logger.error(f"Folder thumbnail path does not exist: {image_path}")
        return send_file('static/images/error.svg', mimetype='image/svg+xml')

    if not os.path.isfile(image_path):
        app_logger.error(f"Folder thumbnail path is not a file: {image_path}")
        return send_file('static/images/error.svg', mimetype='image/svg+xml')

    try:
        ext = os.path.splitext(image_path)[1].lower()
        mime_types = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.webp': 'image/webp'
        }
        mime_type = mime_types.get(ext, 'image/jpeg')

        return send_file(image_path, mimetype=mime_type)
    except Exception as e:
        app_logger.error(f"Error serving folder thumbnail {image_path}: {e}")
        app_logger.error(traceback.format_exc())
        return send_file('static/images/error.svg', mimetype='image/svg+xml')


# =============================================================================
# Directory Listing
# =============================================================================

@collection_bp.route('/list-directories', methods=['GET'])
def list_directories():
    """List directories and files in the given path."""
    from app import (DATA_DIR, directory_cache, cache_lock, cache_timestamps,
                     cache_stats, MAX_CACHE_SIZE, cleanup_cache, is_cache_valid,
                     get_directory_listing)

    current_path = request.args.get('path', '')

    if not current_path:
        default_lib = get_default_library()
        current_path = default_lib['path'] if default_lib else DATA_DIR

    target_dir = current_app.config.get('TARGET', '/downloads/processed')
    normalized_path = os.path.normpath(current_path)
    normalized_target = os.path.normpath(target_dir)
    is_in_target = normalized_path == normalized_target or normalized_path.startswith(normalized_target + os.sep)

    if not is_valid_library_path(current_path) and not is_in_target:
        return jsonify({"error": "Access denied - path not in any library"}), 403

    if not os.path.exists(current_path):
        return jsonify({"error": "Directory not found"}), 404

    library_roots = get_library_roots()
    all_roots = [os.path.normpath(r) for r in library_roots]
    all_roots.append(normalized_target)

    def get_parent_dir(path):
        normalized = os.path.normpath(path)
        if normalized in all_roots:
            return None
        return os.path.dirname(path)

    try:
        cleanup_cache()

        if is_cache_valid(current_path):
            cached_data = directory_cache[current_path]
            parent_dir = get_parent_dir(current_path)

            return jsonify({
                "current_path": current_path,
                "directories": cached_data["directories"],
                "files": cached_data["files"],
                "parent": parent_dir,
                "cached": True
            })

        listing_data = get_directory_listing(current_path)

        with cache_lock:
            cache_stats['misses'] += 1
            directory_cache[current_path] = listing_data
            cache_timestamps[current_path] = time.time()

            if len(directory_cache) > MAX_CACHE_SIZE:
                cleanup_cache()

        parent_dir = get_parent_dir(current_path)

        return jsonify({
            "current_path": current_path,
            "directories": listing_data["directories"],
            "files": listing_data["files"],
            "parent": parent_dir,
            "cached": False
        })
    except Exception as e:
        app_logger.error(f"Error in list_directories for {current_path}: {e}")
        return jsonify({"error": str(e)}), 500


@collection_bp.route('/list-new-files', methods=['GET'])
def list_new_files():
    """List files created in the past N days."""
    from app import DATA_DIR
    import time as time_module

    current_path = request.args.get('path', DATA_DIR)
    days = int(request.args.get('days', 7))
    max_results = int(request.args.get('max_results', 500))

    if not os.path.exists(current_path):
        return jsonify({"error": "Directory not found"}), 404

    try:
        cutoff_time = datetime.now() - timedelta(days=days)
        cutoff_timestamp = cutoff_time.timestamp()

        new_files = []
        excluded_extensions = {".png", ".jpg", ".jpeg", ".gif", ".txt", ".html", ".css", ".ds_store", "cvinfo", ".json", ".db"}

        files_scanned = 0
        dirs_scanned = 0
        start_time = time_module.time()
        max_scan_time = 30

        def scan_for_new_files():
            nonlocal files_scanned, dirs_scanned

            for root, dirs, files in os.walk(current_path):
                if time_module.time() - start_time > max_scan_time:
                    app_logger.warning(f"New files scan timed out after {max_scan_time}s")
                    break

                dirs[:] = [d for d in dirs if not d.startswith(('.', '_'))]
                dirs_scanned += 1

                for filename in files:
                    files_scanned += 1

                    if filename.startswith(('.', '_')):
                        continue

                    if any(filename.lower().endswith(ext) for ext in excluded_extensions):
                        continue

                    full_path = os.path.join(root, filename)

                    try:
                        stat = os.lstat(full_path)

                        if stat.st_ctime >= cutoff_timestamp:
                            yield {
                                "name": filename,
                                "size": stat.st_size,
                                "path": full_path,
                                "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                                "created_ts": stat.st_ctime
                            }
                    except (OSError, IOError):
                        continue

        for file_info in scan_for_new_files():
            new_files.append(file_info)
            if len(new_files) >= max_results:
                app_logger.info(f"Reached max_results limit of {max_results}")
                break

        new_files.sort(key=lambda f: f["created_ts"], reverse=True)

        for file_info in new_files:
            del file_info["created_ts"]

        elapsed_time = time_module.time() - start_time
        app_logger.info(f"New files scan completed: {len(new_files)} found, {files_scanned} files scanned, {dirs_scanned} dirs, {elapsed_time:.2f}s")

        return jsonify({
            "current_path": current_path,
            "files": new_files,
            "total_count": len(new_files),
            "days": days,
            "cutoff_date": cutoff_time.isoformat(),
            "limited": len(new_files) >= max_results,
            "max_results": max_results,
            "scan_stats": {
                "files_scanned": files_scanned,
                "dirs_scanned": dirs_scanned,
                "elapsed_seconds": round(elapsed_time, 2)
            }
        })

    except Exception as e:
        app_logger.error(f"Error in list_new_files for {current_path}: {e}")
        return jsonify({"error": str(e)}), 500


@collection_bp.route('/list-downloads', methods=['GET'])
def list_downloads():
    """List directories and files in the downloads/target path."""
    from app import (TARGET_DIR, directory_cache, cache_lock, cache_timestamps,
                     cache_stats, MAX_CACHE_SIZE, cleanup_cache, is_cache_valid,
                     get_directory_listing)

    current_path = request.args.get('path', TARGET_DIR)

    if not os.path.exists(current_path):
        return jsonify({"error": "Directory not found"}), 404

    try:
        cleanup_cache()

        if is_cache_valid(current_path):
            cached_data = directory_cache[current_path]
            parent_dir = os.path.dirname(current_path) if current_path != TARGET_DIR else None

            return jsonify({
                "current_path": current_path,
                "directories": cached_data["directories"],
                "files": cached_data["files"],
                "parent": parent_dir,
                "cached": True
            })

        listing_data = get_directory_listing(current_path)

        with cache_lock:
            cache_stats['misses'] += 1
            directory_cache[current_path] = listing_data
            cache_timestamps[current_path] = time.time()

            if len(directory_cache) > MAX_CACHE_SIZE:
                cleanup_cache()

        parent_dir = os.path.dirname(current_path) if current_path != TARGET_DIR else None

        return jsonify({
            "current_path": current_path,
            "directories": listing_data["directories"],
            "files": listing_data["files"],
            "parent": parent_dir,
            "cached": False
        })
    except Exception as e:
        app_logger.error(f"Error in list_downloads for {current_path}: {e}")
        return jsonify({"error": str(e)}), 500


@collection_bp.route('/list-recent-files', methods=['GET'])
def list_recent_files():
    """Get the last 100 files added to the /data directory."""
    try:
        limit = request.args.get('limit', 100, type=int)
        if limit > 100:
            limit = 100

        recent_files = get_recent_files(limit=limit)

        date_range = None
        if recent_files:
            oldest_date = recent_files[-1]['added_at']
            newest_date = recent_files[0]['added_at']
            date_range = {
                'oldest': oldest_date,
                'newest': newest_date
            }

        return jsonify({
            "success": True,
            "files": recent_files,
            "total_count": len(recent_files),
            "date_range": date_range
        })

    except Exception as e:
        app_logger.error(f"Error in list_recent_files: {e}")
        return jsonify({"error": str(e)}), 500


@collection_bp.route('/search-files', methods=['GET'])
def search_files():
    """Search for files and directories using file_index table"""
    from app import index_built

    query = request.args.get('query', '').strip()

    if not query:
        return jsonify({"error": "No search query provided"}), 400

    if len(query) < 2:
        return jsonify({"error": "Search query must be at least 2 characters"}), 400

    try:
        results = search_file_index(query, limit=100)

        return jsonify({
            "success": True,
            "results": results,
            "total_found": len(results),
            "query": query,
            "index_ready": index_built
        })

    except Exception as e:
        app_logger.error(f"Error searching files: {e}")
        return jsonify({"error": str(e)}), 500


@collection_bp.route('/count-files', methods=['GET'])
def count_files():
    """Count the total number of files in a directory (recursive)"""
    path = request.args.get('path')
    if not path or not os.path.exists(path):
        return jsonify({"error": "Invalid path"}), 400

    try:
        file_count = 0
        for root, _, files in os.walk(path):
            file_count += len(files)

        return jsonify({
            "file_count": file_count,
            "path": path
        })
    except Exception as e:
        app_logger.error(f"Error counting files in {path}: {e}")
        return jsonify({"error": str(e)}), 500


@collection_bp.route('/cbz-preview', methods=['GET'])
def cbz_preview():
    """Extract and return the first image from a CBZ file as base64"""
    file_path = request.args.get('path')
    size = request.args.get('size', 'large')

    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "Invalid file path"}), 400

    if not file_path.lower().endswith(('.cbz', '.zip')):
        return jsonify({"error": "File is not a CBZ"}), 400

    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            file_list = zf.namelist()

            image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
            image_files = []

            for file_name in file_list:
                ext = os.path.splitext(file_name.lower())[1]
                if ext in image_extensions:
                    image_files.append(file_name)

            if not image_files:
                return jsonify({"error": "No image files found in CBZ"}), 404

            image_files.sort()
            first_image = image_files[0]

            with zf.open(first_image) as image_file:
                img = Image.open(image_file)

                if img.mode in ('RGBA', 'LA', 'P'):
                    img = img.convert('RGB')

                original_width, original_height = img.width, img.height

                if size == 'small':
                    max_size = 300
                else:
                    max_size = 1200

                if img.width > max_size or img.height > max_size:
                    img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

                buffer = BytesIO()
                img.save(buffer, format='JPEG', quality=90)
                img_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')

                return jsonify({
                    "success": True,
                    "preview": f"data:image/jpeg;base64,{img_base64}",
                    "original_size": {"width": original_width, "height": original_height},
                    "display_size": {"width": img.width, "height": img.height},
                    "file_name": first_image,
                    "total_images": len(image_files)
                })

    except Exception as e:
        app_logger.error(f"Error previewing CBZ {file_path}: {e}")
        return jsonify({"error": str(e)}), 500
