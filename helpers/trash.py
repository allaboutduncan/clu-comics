"""
Trash Can module — soft-delete files to a trash directory before permanent removal.

When TRASH_ENABLED is True, deleted files are moved to TRASH_DIR (default: CACHE_DIR/trash).
Oldest items are automatically evicted when the trash exceeds TRASH_MAX_SIZE_MB.
When TRASH_ENABLED is False, move_to_trash() falls back to permanent deletion.
"""

import os
import shutil
import time
from app_logging import app_logger


def get_trash_dir():
    """
    Return the trash directory path, creating it if needed.
    Returns None if trash is disabled.
    """
    from flask import current_app

    if not current_app.config.get("TRASH_ENABLED", True):
        return None

    trash_dir = current_app.config.get("TRASH_DIR", "").strip()
    if not trash_dir:
        cache_dir = current_app.config.get("CACHE_DIR", "/cache")
        trash_dir = os.path.join(cache_dir, "trash")

    os.makedirs(trash_dir, exist_ok=True)
    return trash_dir


def get_trash_max_size_bytes():
    """Return the max trash size in bytes from config (default 1024 MB)."""
    from flask import current_app
    max_mb = current_app.config.get("TRASH_MAX_SIZE_MB", 1024)
    return int(max_mb) * 1024 * 1024


def is_trash_path(path):
    """Check if a path is within the trash directory."""
    from flask import current_app

    if not current_app.config.get("TRASH_ENABLED", True):
        return False

    trash_dir = current_app.config.get("TRASH_DIR", "").strip()
    if not trash_dir:
        cache_dir = current_app.config.get("CACHE_DIR", "/cache")
        trash_dir = os.path.join(cache_dir, "trash")

    normalized_path = os.path.normpath(path)
    normalized_trash = os.path.normpath(trash_dir)

    return normalized_path == normalized_trash or normalized_path.startswith(normalized_trash + os.sep)


def get_trash_size():
    """Calculate total size of trash directory contents in bytes."""
    trash_dir = get_trash_dir()
    if not trash_dir or not os.path.exists(trash_dir):
        return 0

    total = 0
    for entry in os.scandir(trash_dir):
        if entry.is_file(follow_symlinks=False):
            total += entry.stat().st_size
        elif entry.is_dir(follow_symlinks=False):
            for root, dirs, files in os.walk(entry.path):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
    return total


def get_trash_contents():
    """
    List trash items sorted by modification time (oldest first).
    Returns list of dicts: {name, path, size, is_dir, mtime}.
    """
    trash_dir = get_trash_dir()
    if not trash_dir or not os.path.exists(trash_dir):
        return []

    items = []
    for entry in os.scandir(trash_dir):
        try:
            stat = entry.stat(follow_symlinks=False)
            if entry.is_dir(follow_symlinks=False):
                # Calculate directory size
                dir_size = 0
                for root, dirs, files in os.walk(entry.path):
                    for f in files:
                        try:
                            dir_size += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
                size = dir_size
            else:
                size = stat.st_size

            items.append({
                "name": entry.name,
                "path": entry.path,
                "size": size,
                "is_dir": entry.is_dir(follow_symlinks=False),
                "mtime": stat.st_mtime,
            })
        except OSError:
            pass

    items.sort(key=lambda x: x["mtime"])
    return items


def _evict_oldest(needed_bytes):
    """Evict oldest trash items until enough space is freed."""
    contents = get_trash_contents()  # sorted oldest first
    max_size = get_trash_max_size_bytes()
    current_size = sum(item["size"] for item in contents)

    for item in contents:
        if current_size + needed_bytes <= max_size:
            break
        try:
            if item["is_dir"]:
                shutil.rmtree(item["path"])
            else:
                os.remove(item["path"])
            current_size -= item["size"]
            app_logger.info(f"Trash evicted oldest item: {item['name']} ({item['size']} bytes)")
        except OSError as e:
            app_logger.error(f"Error evicting trash item {item['name']}: {e}")


def _get_item_size(source_path):
    """Get the size of a file or directory."""
    if os.path.isfile(source_path):
        return os.path.getsize(source_path)
    total = 0
    for root, dirs, files in os.walk(source_path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def move_to_trash(source_path):
    """
    Move a file or directory to the trash.

    Returns dict with:
      - trashed (bool): True if moved to trash, False if permanently deleted
      - path (str): destination path in trash (if trashed) or original path

    Falls back to permanent delete if trash is disabled.
    """
    trash_dir = get_trash_dir()

    if trash_dir is None:
        # Trash disabled — permanent delete
        if os.path.isdir(source_path):
            shutil.rmtree(source_path)
        else:
            os.remove(source_path)
        app_logger.info(f"Permanently deleted (trash disabled): {source_path}")
        return {"trashed": False, "path": source_path}

    # Calculate size and enforce limit
    item_size = _get_item_size(source_path)
    max_size = get_trash_max_size_bytes()
    current_size = get_trash_size()

    if current_size + item_size > max_size:
        _evict_oldest(item_size)

    # Determine destination name with collision handling
    basename = os.path.basename(source_path)
    dest_path = os.path.join(trash_dir, basename)

    if os.path.exists(dest_path):
        name, ext = os.path.splitext(basename)
        if os.path.isdir(source_path) and not ext:
            dest_path = os.path.join(trash_dir, f"{basename}_{int(time.time())}")
        else:
            dest_path = os.path.join(trash_dir, f"{name}_{int(time.time())}{ext}")

    try:
        shutil.move(source_path, dest_path)
        app_logger.info(f"Moved to trash: {source_path} -> {dest_path}")
        return {"trashed": True, "path": dest_path}
    except Exception as e:
        # Fall back to permanent delete on move failure
        app_logger.error(f"Failed to move to trash, permanently deleting: {e}")
        if os.path.isdir(source_path):
            shutil.rmtree(source_path)
        else:
            os.remove(source_path)
        return {"trashed": False, "path": source_path}


def empty_trash():
    """
    Permanently delete all trash contents.
    Returns dict: {count, size_freed}.
    """
    trash_dir = get_trash_dir()
    if not trash_dir or not os.path.exists(trash_dir):
        return {"count": 0, "size_freed": 0}

    count = 0
    size_freed = 0

    for entry in os.scandir(trash_dir):
        try:
            stat = entry.stat(follow_symlinks=False)
            if entry.is_dir(follow_symlinks=False):
                dir_size = 0
                for root, dirs, files in os.walk(entry.path):
                    for f in files:
                        try:
                            dir_size += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
                shutil.rmtree(entry.path)
                size_freed += dir_size
            else:
                size_freed += stat.st_size
                os.remove(entry.path)
            count += 1
        except OSError as e:
            app_logger.error(f"Error emptying trash item {entry.name}: {e}")

    app_logger.info(f"Emptied trash: {count} items, {size_freed} bytes freed")
    return {"count": count, "size_freed": size_freed}


def permanently_delete_from_trash(item_name):
    """
    Delete a specific item from trash by name.
    Returns dict: {success, size_freed, error}.
    """
    trash_dir = get_trash_dir()
    if not trash_dir:
        return {"success": False, "size_freed": 0, "error": "Trash is disabled"}

    item_path = os.path.join(trash_dir, item_name)

    if not os.path.exists(item_path):
        return {"success": False, "size_freed": 0, "error": "Item not found in trash"}

    # Ensure the item is actually within the trash dir (prevent path traversal)
    normalized_item = os.path.normpath(item_path)
    normalized_trash = os.path.normpath(trash_dir)
    if not normalized_item.startswith(normalized_trash + os.sep):
        return {"success": False, "size_freed": 0, "error": "Invalid item path"}

    try:
        size = _get_item_size(item_path)
        if os.path.isdir(item_path):
            shutil.rmtree(item_path)
        else:
            os.remove(item_path)
        app_logger.info(f"Permanently deleted from trash: {item_name} ({size} bytes)")
        return {"success": True, "size_freed": size}
    except OSError as e:
        app_logger.error(f"Error deleting trash item {item_name}: {e}")
        return {"success": False, "size_freed": 0, "error": str(e)}
