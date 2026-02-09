"""
OPDS Feed Blueprint

Provides OPDS (Open Publication Distribution System) catalog feeds for browsing
and downloading comics from OPDS-compatible readers like Panels, Chunky, etc.
"""

from flask import Blueprint, render_template, request, url_for, Response
from database import get_to_read_items, get_libraries
from app_logging import app_logger
import os
import hashlib
from datetime import datetime
from urllib.parse import quote

opds_bp = Blueprint('opds', __name__, url_prefix='/opds')


def get_library_roots():
    """Get list of all enabled library root paths."""
    libraries = get_libraries(enabled_only=True)
    if libraries:
        return [lib['path'] for lib in libraries]
    # Fallback for backwards compatibility
    return ['/data'] if os.path.exists('/data') else []


def is_valid_library_path(path):
    """Check if a path is within any enabled library."""
    if not path:
        return False
    normalized = os.path.normpath(path)
    for root in get_library_roots():
        root_normalized = os.path.normpath(root)
        if normalized == root_normalized or normalized.startswith(root_normalized + os.sep):
            return True
    return False

# MIME types for comic files
COMIC_MIME_TYPES = {
    '.cbz': 'application/vnd.comicbook+zip',
    '.cbr': 'application/vnd.comicbook-rar',
    '.pdf': 'application/pdf',
    '.epub': 'application/epub+zip',
}

# Extensions to include as comics
COMIC_EXTENSIONS = {'.cbz', '.cbr', '.pdf', '.epub'}

# Extensions to exclude from listing
EXCLUDED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.html', '.css', '.ds_store', 'cvinfo', '.json', '.db', '.xml'}

# OPDS MIME type
OPDS_MIME = 'application/atom+xml;profile=opds-catalog;kind=navigation'


def generate_feed_id(path):
    """Generate a stable UUID-like ID from a path."""
    hash_val = hashlib.md5(path.encode()).hexdigest()
    return f"urn:uuid:{hash_val[:8]}-{hash_val[8:12]}-{hash_val[12:16]}-{hash_val[16:20]}-{hash_val[20:32]}"


def get_timestamp():
    """Get current timestamp in ISO 8601 format."""
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')


def get_directory_listing_for_opds(path):
    """Get directory listing optimized for OPDS (directories and comic files only)."""
    directories = []
    files = []

    try:
        entries = os.listdir(path)

        for entry in entries:
            # Skip hidden files and directories
            if entry.startswith(('.', '_')):
                continue

            full_path = os.path.join(path, entry)
            try:
                stat = os.stat(full_path)
                if stat.st_mode & 0o40000:  # Directory
                    directories.append({
                        'name': entry,
                        'path': full_path
                    })
                else:  # File
                    ext = os.path.splitext(entry)[1].lower()
                    if ext in COMIC_EXTENSIONS:
                        files.append({
                            'name': entry,
                            'path': full_path,
                            'size': stat.st_size,
                            'mime_type': COMIC_MIME_TYPES.get(ext, 'application/octet-stream')
                        })
            except (OSError, IOError):
                continue

        # Sort alphabetically
        directories.sort(key=lambda d: d['name'].lower())
        files.sort(key=lambda f: f['name'].lower())

    except Exception as e:
        app_logger.error(f"Error listing directory {path}: {e}")

    return directories, files


def check_folder_thumbnail(folder_path):
    """Check if a folder has a thumbnail image (folder.png, folder.jpg, etc.)."""
    for ext in ['.png', '.jpg', '.jpeg']:
        thumb_path = os.path.join(folder_path, f'folder{ext}')
        if os.path.exists(thumb_path):
            return thumb_path
    return None


@opds_bp.route('/')
def root():
    """Root OPDS catalog with navigation to Browse and Reading List."""
    entries = [
        {
            'id': generate_feed_id('/opds/browse'),
            'title': 'Browse',
            'updated': get_timestamp(),
            'type': 'navigation',
            'href': url_for('opds.browse', _external=True),
            'thumbnail_url': None
        },
        {
            'id': generate_feed_id('/opds/to-read'),
            'title': 'Reading List',
            'updated': get_timestamp(),
            'type': 'navigation',
            'href': url_for('opds.to_read', _external=True),
            'thumbnail_url': None
        }
    ]

    xml = render_template(
        'opds_feed.xml',
        feed_id=generate_feed_id('/opds'),
        feed_title='Comic Library',
        updated=get_timestamp(),
        start_url=url_for('opds.root', _external=True),
        self_url=url_for('opds.root', _external=True),
        parent_url=None,
        entries=entries
    )

    return Response(xml, mimetype=OPDS_MIME)


@opds_bp.route('/browse')
def browse():
    """Browse library directories and comics."""
    current_path = request.args.get('path', None)
    library_roots = get_library_roots()

    # If no path specified, show all libraries at root level
    if not current_path:
        entries = []
        libraries = get_libraries(enabled_only=True)

        for lib in libraries:
            if os.path.exists(lib['path']):
                thumb_path = check_folder_thumbnail(lib['path'])
                thumbnail_url = None
                if thumb_path:
                    thumbnail_url = url_for('collection.serve_folder_thumbnail', path=thumb_path, _external=True)

                entries.append({
                    'id': generate_feed_id(lib['path']),
                    'title': lib['name'],
                    'updated': get_timestamp(),
                    'type': 'navigation',
                    'href': url_for('opds.browse', path=lib['path'], _external=True),
                    'thumbnail_url': thumbnail_url
                })

        xml = render_template(
            'opds_feed.xml',
            feed_id=generate_feed_id('/opds/browse'),
            feed_title='Libraries',
            updated=get_timestamp(),
            start_url=url_for('opds.root', _external=True),
            self_url=url_for('opds.browse', _external=True),
            parent_url=url_for('opds.root', _external=True),
            entries=entries
        )
        return Response(xml, mimetype=OPDS_MIME)

    # Security check - ensure path is within any configured library
    if not is_valid_library_path(current_path):
        return Response("Access denied", status=403)

    if not os.path.exists(current_path):
        return Response("Directory not found", status=404)

    directories, files = get_directory_listing_for_opds(current_path)

    entries = []

    # Add directories as navigation entries
    for dir_info in directories:
        # Check for folder thumbnail
        thumb_path = check_folder_thumbnail(dir_info['path'])
        thumbnail_url = None
        if thumb_path:
            thumbnail_url = url_for('collection.serve_folder_thumbnail', path=thumb_path, _external=True)

        entries.append({
            'id': generate_feed_id(dir_info['path']),
            'title': dir_info['name'],
            'updated': get_timestamp(),
            'type': 'navigation',
            'href': url_for('opds.browse', path=dir_info['path'], _external=True),
            'thumbnail_url': thumbnail_url
        })

    # Add files as acquisition entries
    for file_info in files:
        thumbnail_url = url_for('get_thumbnail', path=file_info['path'], _external=True)

        entries.append({
            'id': generate_feed_id(file_info['path']),
            'title': file_info['name'],
            'updated': get_timestamp(),
            'type': 'acquisition',
            'download_url': url_for('download_file', path=file_info['path'], _external=True),
            'mime_type': file_info['mime_type'],
            'size': file_info['size'],
            'thumbnail_url': thumbnail_url
        })

    # Determine parent URL - check if we're at a library root
    parent_url = None
    normalized_path = os.path.normpath(current_path)
    is_library_root = normalized_path in [os.path.normpath(r) for r in library_roots]

    if is_library_root:
        # At library root, parent is the browse root (library listing)
        parent_url = url_for('opds.browse', _external=True)
    else:
        parent_path = os.path.dirname(current_path)
        parent_url = url_for('opds.browse', path=parent_path, _external=True)

    # Feed title is the folder name or library name for root
    feed_title = os.path.basename(current_path) or 'Library'

    xml = render_template(
        'opds_feed.xml',
        feed_id=generate_feed_id(current_path),
        feed_title=feed_title,
        updated=get_timestamp(),
        start_url=url_for('opds.root', _external=True),
        self_url=url_for('opds.browse', path=current_path, _external=True),
        parent_url=parent_url,
        entries=entries
    )

    return Response(xml, mimetype=OPDS_MIME)


@opds_bp.route('/to-read')
def to_read():
    """List items marked as 'Want to Read'."""
    try:
        items = get_to_read_items()
    except Exception as e:
        app_logger.error(f"Error getting to-read items: {e}")
        items = []

    entries = []

    for item in items:
        item_path = item.get('path', '')
        item_type = item.get('type', 'file')
        item_name = os.path.basename(item_path)

        if item_type == 'folder':
            # Folder - navigation entry
            thumb_path = check_folder_thumbnail(item_path)
            thumbnail_url = None
            if thumb_path:
                thumbnail_url = url_for('collection.serve_folder_thumbnail', path=thumb_path, _external=True)

            entries.append({
                'id': generate_feed_id(item_path),
                'title': item_name,
                'updated': item.get('created_at', get_timestamp()),
                'type': 'navigation',
                'href': url_for('opds.browse', path=item_path, _external=True),
                'thumbnail_url': thumbnail_url
            })
        else:
            # File - acquisition entry
            ext = os.path.splitext(item_path)[1].lower()
            mime_type = COMIC_MIME_TYPES.get(ext, 'application/octet-stream')

            try:
                size = os.path.getsize(item_path) if os.path.exists(item_path) else 0
            except (OSError, IOError):
                size = 0

            thumbnail_url = url_for('get_thumbnail', path=item_path, _external=True)

            entries.append({
                'id': generate_feed_id(item_path),
                'title': item_name,
                'updated': item.get('created_at', get_timestamp()),
                'type': 'acquisition',
                'download_url': url_for('download_file', path=item_path, _external=True),
                'mime_type': mime_type,
                'size': size,
                'thumbnail_url': thumbnail_url
            })

    xml = render_template(
        'opds_feed.xml',
        feed_id=generate_feed_id('/opds/to-read'),
        feed_title='Reading List',
        updated=get_timestamp(),
        start_url=url_for('opds.root', _external=True),
        self_url=url_for('opds.to_read', _external=True),
        parent_url=url_for('opds.root', _external=True),
        entries=entries
    )

    return Response(xml, mimetype=OPDS_MIME)
