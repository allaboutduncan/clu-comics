from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, current_app
import requests
import os
import uuid
import threading
from database import (
    create_reading_list,
    add_reading_list_entry,
    get_reading_lists,
    get_reading_list,
    update_reading_list_entry_match,
    delete_reading_list,
    search_file_index,
    update_reading_list_thumbnail,
    clear_thumbnail_if_matches_entry,
    update_reading_list_name,
    update_reading_list_tags,
    get_all_reading_list_tags
)
from models.cbl import CBLLoader
from app_logging import app_logger
import app_state

reading_lists_bp = Blueprint('reading_lists', __name__)

# In-memory store for background import tasks
import_tasks = {}

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

        # Create reading list
        list_id = create_reading_list(loader.name, source=source)
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
        if 'github.com' in url and '/blob/' in url:
            url = url.replace('github.com', 'raw.githubusercontent.com').replace('/blob/', '/')
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
