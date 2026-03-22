from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, current_app, Response
import requests
import os
import uuid
import hashlib
import threading
import time as time_module
from urllib.parse import urlparse
from core.database import (
    create_reading_list,
    add_reading_list_entry,
    get_reading_lists,
    get_reading_list,
    update_reading_list_entry_match,
    delete_reading_list,
    delete_reading_list_entry,
    reorder_reading_list_entries,
    get_user_reading_lists_summary,
    get_file_metadata_for_reading_list,
    search_file_index,
    update_reading_list_thumbnail,
    clear_thumbnail_if_matches_entry,
    update_reading_list_name,
    update_reading_list_tags,
    get_all_reading_list_tags,
    update_reading_list_source_hash,
    get_reading_lists_with_source,
    sync_reading_list_entries,
)
from models.cbl import CBLLoader
from core.app_logging import app_logger
import core.app_state as app_state

reading_lists_bp = Blueprint('reading_lists', __name__)

# In-memory store for background import tasks
import_tasks = {}

# GitHub tree cache (module-level with TTL)
_github_tree_cache = {"tree": None, "fetched_at": 0}
_GITHUB_TREE_TTL = 1800  # 30 minutes

# Semaphore to limit concurrent batch imports
_import_semaphore = threading.Semaphore(5)

_GITHUB_HOSTS = {"github.com", "raw.githubusercontent.com"}


def _is_github_url(url):
    """Check if a URL is from github.com or raw.githubusercontent.com using proper URL parsing."""
    try:
        parsed = urlparse(url)
        return parsed.hostname in _GITHUB_HOSTS
    except Exception:
        return False


def _convert_github_blob_to_raw(url):
    """Convert a github.com blob URL to a raw.githubusercontent.com URL.

    Only transforms URLs whose hostname is exactly github.com and whose path
    contains /blob/.  Returns the URL unchanged otherwise.
    """
    try:
        parsed = urlparse(url)
        if parsed.hostname == "github.com" and "/blob/" in parsed.path:
            new_path = parsed.path.replace("/blob/", "/", 1)
            return parsed._replace(
                netloc="raw.githubusercontent.com", path=new_path
            ).geturl()
    except Exception:
        pass
    return url

@reading_lists_bp.route('/reading-lists')
def index():
    """View all reading lists."""
    lists = get_reading_lists()
    return render_template('reading_lists.html', lists=lists)

@reading_lists_bp.route('/reading-lists/<int:list_id>')
def view_list(list_id):
    """View details of a specific reading list."""
    reading_list = get_reading_list(list_id)
    if not reading_list:
        flash('Reading list not found', 'error')
        return redirect(url_for('reading_lists.index'))

    # Get rename pattern for search formatting
    rename_pattern = current_app.config.get('CUSTOM_RENAME_PATTERN', '{series_name} {issue_number}')
    if not rename_pattern:
        rename_pattern = '{series_name} {issue_number}'

    return render_template('reading_list_view.html', reading_list=reading_list, rename_pattern=rename_pattern)

def process_cbl_import(task_id, content, filename, source, rename_pattern=None):
    """Background worker to process CBL import."""
    op_id = None
    try:
        app_logger.info(f"[Import {task_id[:8]}] Starting import for: {filename}")
        import_tasks[task_id]['status'] = 'processing'
        import_tasks[task_id]['message'] = 'Parsing CBL file...'

        loader = CBLLoader(content, filename=filename, rename_pattern=rename_pattern)

        # Parse entries first (fast - just XML parsing)
        entries = loader.parse_entries()
        total = len(entries)

        # Extract clean display name from filename
        display_name = filename
        if display_name.endswith('.cbl'):
            display_name = display_name[:-4]

        op_id = app_state.register_operation("import", f"Import: {display_name}", total=total)

        app_logger.info(f"[Import {task_id[:8]}] Parsed {total} entries from CBL")
        import_tasks[task_id]['message'] = f'Matching {total} issues to library...'
        import_tasks[task_id]['total'] = total
        import_tasks[task_id]['processed'] = 0

        # Compute source hash for sync detection
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        # Create reading list
        list_id = create_reading_list(loader.name, source=source, source_hash=content_hash)
        if not list_id:
            app_logger.error(f"[Import {task_id[:8]}] Failed to create reading list")
            import_tasks[task_id]['status'] = 'error'
            import_tasks[task_id]['message'] = 'Failed to create reading list'
            if op_id:
                app_state.complete_operation(op_id, error=True)
            return

        app_logger.info(f"[Import {task_id[:8]}] Created reading list: {loader.name} (id={list_id})")

        # Match and add entries one by one (this is the slow part)
        for i, entry in enumerate(entries):
            # Match file for this entry
            entry['matched_file_path'] = loader.match_file(
                entry['series'], entry['issue_number'], entry['volume'], entry['year']
            )
            # Add to database
            add_reading_list_entry(list_id, entry)

            # Update progress
            import_tasks[task_id]['processed'] = i + 1
            app_state.update_operation(
                op_id, current=i + 1,
                detail=f"{entry.get('series', '')} #{entry.get('issue_number', '')}"
            )
            if (i + 1) % 10 == 0:
                app_logger.info(f"[Import {task_id[:8]}] Progress: {i + 1}/{total} issues")

        import_tasks[task_id]['status'] = 'complete'
        import_tasks[task_id]['message'] = f'Imported {total} issues'
        import_tasks[task_id]['list_id'] = list_id
        import_tasks[task_id]['list_name'] = loader.name
        app_state.complete_operation(op_id)
        app_logger.info(f"[Import {task_id[:8]}] Complete: {total} issues imported to '{loader.name}'")

    except Exception as e:
        app_logger.error(f"[Import {task_id[:8]}] Error: {str(e)}")
        import_tasks[task_id]['status'] = 'error'
        import_tasks[task_id]['message'] = str(e)
        if op_id:
            app_state.complete_operation(op_id, error=True)


@reading_lists_bp.route('/api/reading-lists/upload', methods=['POST'])
def upload_list():
    """Upload and parse a CBL file (runs in background)."""
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': 'No file part'})

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': 'No selected file'})

    if file:
        try:
            content = file.read().decode('utf-8')
            filename = file.filename
            app_logger.info(f"Received CBL upload: {filename}")

            # Get rename pattern for matching
            rename_pattern = current_app.config.get('CUSTOM_RENAME_PATTERN', '{series_name} {issue_number}')

            # Create task and start background processing
            task_id = str(uuid.uuid4())
            import_tasks[task_id] = {
                'status': 'pending',
                'message': 'Starting import...',
                'processed': 0,
                'total': 0
            }
            app_logger.info(f"Created import task: {task_id[:8]} for {filename}")

            thread = threading.Thread(
                target=process_cbl_import,
                args=(task_id, content, filename, filename, rename_pattern)
            )
            thread.daemon = True
            thread.start()

            return jsonify({
                'success': True,
                'background': True,
                'task_id': task_id,
                'message': 'Import started in background'
            })

        except Exception as e:
            app_logger.error(f"Error starting upload: {str(e)}")
            return jsonify({'success': False, 'message': f'Error: {str(e)}'})

    return jsonify({'success': False, 'message': 'Unknown error'})

@reading_lists_bp.route('/api/reading-lists/import', methods=['POST'])
def import_list():
    """Import a CBL file from a URL (runs in background)."""
    data = request.json
    url = data.get('url')

    if not url:
        return jsonify({'success': False, 'message': 'URL is required'})

    try:
        app_logger.info(f"Importing CBL from URL: {url}")

        # Handle GitHub blob URLs by converting to raw
        converted = _convert_github_blob_to_raw(url)
        if converted != url:
            url = converted
            app_logger.info(f"Converted to raw URL: {url}")

        response = requests.get(url, timeout=30)
        response.raise_for_status()

        content = response.text
        import_filename = url.split('/')[-1]
        app_logger.info(f"Downloaded CBL file: {import_filename} ({len(content)} bytes)")

        # Get rename pattern for matching
        rename_pattern = current_app.config.get('CUSTOM_RENAME_PATTERN', '{series_name} {issue_number}')

        # Create task and start background processing
        task_id = str(uuid.uuid4())
        import_tasks[task_id] = {
            'status': 'pending',
            'message': 'Starting import...',
            'processed': 0,
            'total': 0
        }
        app_logger.info(f"Created import task: {task_id[:8]} for {import_filename}")

        thread = threading.Thread(
            target=process_cbl_import,
            args=(task_id, content, import_filename, url, rename_pattern)
        )
        thread.daemon = True
        thread.start()

        return jsonify({
            'success': True,
            'background': True,
            'task_id': task_id,
            'message': 'Import started in background'
        })

    except Exception as e:
        app_logger.error(f"Error importing from URL: {str(e)}")
        return jsonify({'success': False, 'message': f'Error importing from URL: {str(e)}'})

@reading_lists_bp.route('/api/reading-lists/<int:list_id>/map', methods=['POST'])
def map_entry(list_id):
    """Map a reading list entry to a specific file."""
    data = request.json
    entry_id = data.get('entry_id')
    file_path = data.get('file_path')

    if not entry_id:
        return jsonify({'success': False, 'message': 'Entry ID is required'})

    # If clearing mapping, also clear thumbnail if it matches this entry
    if file_path is None:
        clear_thumbnail_if_matches_entry(list_id, entry_id)

    if update_reading_list_entry_match(entry_id, file_path):
        return jsonify({'success': True, 'message': 'Entry mapped successfully'})
    else:
        return jsonify({'success': False, 'message': 'Failed to map entry'})

@reading_lists_bp.route('/api/reading-lists/<int:list_id>', methods=['DELETE'])
def delete_list(list_id):
    """Delete a reading list."""
    if delete_reading_list(list_id):
        return jsonify({'success': True, 'message': 'Reading list deleted'})
    else:
        return jsonify({'success': False, 'message': 'Failed to delete reading list'})

@reading_lists_bp.route('/api/reading-lists/import-status/<task_id>')
def import_status(task_id):
    """Check the status of a background import task."""
    task = import_tasks.get(task_id)
    if not task:
        return jsonify({'success': False, 'message': 'Task not found'})

    return jsonify({
        'success': True,
        'status': task.get('status', 'unknown'),
        'message': task.get('message', ''),
        'processed': task.get('processed', 0),
        'total': task.get('total', 0),
        'list_id': task.get('list_id'),
        'list_name': task.get('list_name')
    })

@reading_lists_bp.route('/api/reading-lists/search-file')
def search_file():
    """Search for files to map."""
    query = request.args.get('q', '')
    if not query:
        return jsonify([])

    results = search_file_index(query, limit=20)
    return jsonify(results)

@reading_lists_bp.route('/api/reading-lists/<int:list_id>/thumbnail', methods=['POST'])
def set_thumbnail(list_id):
    """Set the thumbnail for a reading list."""
    data = request.json
    file_path = data.get('file_path')

    if not file_path:
        return jsonify({'success': False, 'message': 'File path is required'})

    if update_reading_list_thumbnail(list_id, file_path):
        return jsonify({'success': True, 'message': 'Thumbnail updated'})
    else:
        return jsonify({'success': False, 'message': 'Failed to update thumbnail'})


@reading_lists_bp.route('/api/reading-lists/<int:list_id>/name', methods=['POST'])
def update_name(list_id):
    """Update the name of a reading list."""
    data = request.json
    name = data.get('name', '').strip()

    if not name:
        return jsonify({'success': False, 'message': 'Name is required'})

    if update_reading_list_name(list_id, name):
        return jsonify({'success': True, 'message': 'Name updated'})
    else:
        return jsonify({'success': False, 'message': 'Failed to update name'})


@reading_lists_bp.route('/api/reading-lists/<int:list_id>/tags', methods=['POST'])
def update_tags(list_id):
    """Update the tags for a reading list."""
    data = request.json
    tags = data.get('tags', [])

    # Ensure tags is a list of strings
    if not isinstance(tags, list):
        return jsonify({'success': False, 'message': 'Tags must be a list'})

    # Clean tags - strip whitespace and remove empty
    tags = [t.strip() for t in tags if isinstance(t, str) and t.strip()]

    if update_reading_list_tags(list_id, tags):
        return jsonify({'success': True, 'message': 'Tags updated'})
    else:
        return jsonify({'success': False, 'message': 'Failed to update tags'})


@reading_lists_bp.route('/api/reading-lists/tags')
def get_tags():
    """Get all unique tags across all reading lists for autocomplete."""
    tags = get_all_reading_list_tags()
    return jsonify({'tags': tags})


@reading_lists_bp.route('/api/reading-lists/create', methods=['POST'])
def create_list():
    """Create an empty reading list."""
    data = request.json
    name = data.get('name', '').strip() if data else ''

    if not name:
        return jsonify({'success': False, 'message': 'Name is required'})

    list_id = create_reading_list(name)
    if list_id:
        return jsonify({'success': True, 'list_id': list_id, 'message': 'Reading list created'})
    else:
        return jsonify({'success': False, 'message': 'Failed to create reading list'})


@reading_lists_bp.route('/api/reading-lists/<int:list_id>/add-entry', methods=['POST'])
def add_entry(list_id):
    """Add an issue to a reading list from a file path, auto-filling metadata from file_index."""
    data = request.json
    file_path = data.get('file_path', '').strip() if data else ''

    if not file_path:
        return jsonify({'success': False, 'message': 'file_path is required'})

    # Look up metadata from file_index
    meta = get_file_metadata_for_reading_list(file_path)

    entry_data = {
        'series': meta.get('ci_series') if meta else None,
        'issue_number': meta.get('ci_number') if meta else None,
        'volume': meta.get('ci_volume') if meta else None,
        'year': meta.get('ci_year') if meta else None,
        'matched_file_path': file_path,
    }

    # Fallback: extract from filename if no metadata
    if not entry_data['series']:
        basename = os.path.splitext(os.path.basename(file_path))[0]
        entry_data['series'] = basename

    entry_id = add_reading_list_entry(list_id, entry_data)
    if entry_id:
        return jsonify({'success': True, 'entry_id': entry_id, 'message': 'Entry added'})
    else:
        return jsonify({'success': False, 'message': 'Failed to add entry'})


@reading_lists_bp.route('/api/reading-lists/<int:list_id>/entry/<int:entry_id>', methods=['DELETE'])
def remove_entry(list_id, entry_id):
    """Remove a single entry from a reading list."""
    if delete_reading_list_entry(entry_id):
        return jsonify({'success': True, 'message': 'Entry removed'})
    else:
        return jsonify({'success': False, 'message': 'Failed to remove entry'})


@reading_lists_bp.route('/api/reading-lists/<int:list_id>/reorder', methods=['POST'])
def reorder_entries(list_id):
    """Reorder entries in a reading list."""
    data = request.json
    entry_ids = data.get('entry_ids', []) if data else []

    if not entry_ids:
        return jsonify({'success': False, 'message': 'entry_ids is required'})

    if reorder_reading_list_entries(list_id, entry_ids):
        return jsonify({'success': True, 'message': 'Entries reordered'})
    else:
        return jsonify({'success': False, 'message': 'Failed to reorder entries'})


@reading_lists_bp.route('/api/reading-lists/<int:list_id>/export')
def export_cbl(list_id):
    """Export a reading list as a CBL XML file."""
    reading_list = get_reading_list(list_id)
    if not reading_list:
        return jsonify({'success': False, 'message': 'Reading list not found'}), 404

    import xml.etree.ElementTree as ET

    root = ET.Element('ReadingList')
    name_el = ET.SubElement(root, 'Name')
    name_el.text = reading_list['name']

    books_el = ET.SubElement(root, 'Books')
    for entry in reading_list.get('entries', []):
        attrs = {}
        if entry.get('series'):
            attrs['Series'] = str(entry['series'])
        if entry.get('issue_number'):
            attrs['Number'] = str(entry['issue_number'])
        if entry.get('volume'):
            attrs['Volume'] = str(entry['volume'])
        if entry.get('year'):
            attrs['Year'] = str(entry['year'])
        ET.SubElement(books_el, 'Book', **attrs)

    xml_str = '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding='unicode')

    safe_name = reading_list['name'].replace(' ', '_').replace('/', '_')
    return Response(
        xml_str,
        mimetype='application/xml',
        headers={'Content-Disposition': f'attachment; filename="{safe_name}.cbl"'}
    )


@reading_lists_bp.route('/api/reading-lists/github-tree')
def github_tree():
    """Proxy endpoint to browse DieselTech/CBL-ReadingLists repo tree."""
    global _github_tree_cache

    now = time_module.time()
    if _github_tree_cache["tree"] is not None and (now - _github_tree_cache["fetched_at"]) < _GITHUB_TREE_TTL:
        tree = _github_tree_cache["tree"]
    else:
        try:
            resp = requests.get(
                "https://api.github.com/repos/DieselTech/CBL-ReadingLists/git/trees/main?recursive=1",
                timeout=15,
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            resp.raise_for_status()
            data = resp.json()

            # Filter to only .cbl files and their parent folders
            cbl_files = []
            folder_paths = set()
            for item in data.get("tree", []):
                if item["type"] == "blob" and item["path"].lower().endswith(".cbl"):
                    cbl_files.append({"path": item["path"], "type": "blob"})
                    # Add all parent folders
                    parts = item["path"].split("/")
                    for i in range(1, len(parts)):
                        folder_paths.add("/".join(parts[:i]))

            folders = [{"path": p, "type": "tree"} for p in sorted(folder_paths)]
            tree = folders + sorted(cbl_files, key=lambda x: x["path"])

            _github_tree_cache["tree"] = tree
            _github_tree_cache["fetched_at"] = now
        except Exception as e:
            app_logger.error(f"Error fetching GitHub tree: {e}")
            return jsonify({"success": False, "message": f"Failed to fetch repository: {str(e)}"}), 500

    return jsonify({"success": True, "tree": tree})


@reading_lists_bp.route('/api/reading-lists/import-batch', methods=['POST'])
def import_batch():
    """Import multiple CBL files from the DieselTech repo."""
    data = request.json
    files = data.get('files', []) if data else []

    if not files:
        return jsonify({'success': False, 'message': 'No files selected'})

    rename_pattern = current_app.config.get('CUSTOM_RENAME_PATTERN', '{series_name} {issue_number}')
    tasks = []

    for file_path in files:
        raw_url = f"https://raw.githubusercontent.com/DieselTech/CBL-ReadingLists/main/{file_path}"
        filename = file_path.split('/')[-1]
        task_id = str(uuid.uuid4())
        import_tasks[task_id] = {
            'status': 'pending',
            'message': 'Queued...',
            'processed': 0,
            'total': 0,
        }

        thread = threading.Thread(
            target=_batch_import_worker,
            args=(task_id, raw_url, filename, rename_pattern),
        )
        thread.daemon = True
        thread.start()

        tasks.append({'task_id': task_id, 'filename': filename})

    return jsonify({'success': True, 'tasks': tasks})


def _batch_import_worker(task_id, url, filename, rename_pattern):
    """Worker that acquires semaphore then downloads and imports a CBL file."""
    _import_semaphore.acquire()
    try:
        import_tasks[task_id]['message'] = 'Downloading...'
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        content = resp.text
        process_cbl_import(task_id, content, filename, url, rename_pattern)
    except Exception as e:
        app_logger.error(f"[Batch import {task_id[:8]}] Error: {e}")
        import_tasks[task_id]['status'] = 'error'
        import_tasks[task_id]['message'] = str(e)
    finally:
        _import_semaphore.release()


@reading_lists_bp.route('/api/reading-lists/<int:list_id>/sync', methods=['POST'])
def sync_list(list_id):
    """Sync a reading list with its GitHub source."""
    reading_list = get_reading_list(list_id)
    if not reading_list:
        return jsonify({'success': False, 'message': 'Reading list not found'}), 404

    source = reading_list.get('source', '')
    if not source or not _is_github_url(source):
        return jsonify({'success': False, 'message': 'This list does not have a GitHub source'}), 400

    try:
        # Handle GitHub blob URLs by converting to raw
        url = _convert_github_blob_to_raw(source)

        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        content = resp.text

        # Compute hash and compare
        new_hash = hashlib.sha256(content.encode()).hexdigest()
        if new_hash == reading_list.get('source_hash'):
            return jsonify({'success': True, 'changed': False, 'message': 'No changes detected'})

        # Parse new entries
        filename = url.split('/')[-1]
        rename_pattern = current_app.config.get('CUSTOM_RENAME_PATTERN', '{series_name} {issue_number}')
        loader = CBLLoader(content, filename=filename, rename_pattern=rename_pattern)
        new_entries = loader.parse_entries()

        # Match files for new entries
        for entry in new_entries:
            entry['matched_file_path'] = loader.match_file(
                entry['series'], entry['issue_number'], entry['volume'], entry['year']
            )

        # Sync entries
        result = sync_reading_list_entries(list_id, new_entries)
        if result is None:
            return jsonify({'success': False, 'message': 'Failed to sync entries'}), 500

        # Update hash
        update_reading_list_source_hash(list_id, new_hash)

        return jsonify({
            'success': True,
            'changed': True,
            'added': result['added'],
            'removed': result['removed'],
            'message': f"Synced: {result['added']} added, {result['removed']} removed",
        })

    except Exception as e:
        app_logger.error(f"Error syncing reading list {list_id}: {e}")
        return jsonify({'success': False, 'message': f'Sync failed: {str(e)}'}), 500


@reading_lists_bp.route('/api/reading-lists/summary')
def summary():
    """Get a lightweight list of all reading lists for picker modals."""
    lists = get_user_reading_lists_summary()
    return jsonify(lists)
