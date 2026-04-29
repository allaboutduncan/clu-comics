"""
Trash Can module — soft-delete files to a trash directory before permanent removal.

When TRASH_ENABLED is True, deleted files are moved to TRASH_DIR (default: CACHE_DIR/trash).
Oldest items are automatically evicted when the trash exceeds TRASH_MAX_SIZE_MB.
When TRASH_ENABLED is False, move_to_trash() falls back to permanent deletion.
"""

import json
import os
import shutil
import time
from core.app_logging import app_logger


MANIFEST_FILENAME = "trash_manifest.json"


def _get_manifest_path():
    """Return the path to the trash manifest file, or None if trash is disabled."""
    trash_dir = get_trash_dir()
    if not trash_dir:
        return None
    return os.path.join(trash_dir, MANIFEST_FILENAME)


def _load_manifest():
    """Load the trash manifest. Returns {} on missing or corrupt file."""
    path = _get_manifest_path()
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_manifest(manifest):
    """Atomically write the manifest dict to disk."""
    path = _get_manifest_path()
    if not path:
        return
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        os.replace(tmp_path, path)
    except OSError as e:
        app_logger.error(f"Failed to save trash manifest: {e}")


def get_trash_manifest():
    """Public accessor for the trash manifest dict."""
    return _load_manifest()


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
        if entry.name == MANIFEST_FILENAME:
            continue
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
        if entry.name == MANIFEST_FILENAME:
            continue
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

    manifest = _load_manifest()
    manifest_changed = False

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
            if item["name"] in manifest:
                del manifest[item["name"]]
                manifest_changed = True
        except OSError as e:
            app_logger.error(f"Error evicting trash item {item['name']}: {e}")

    if manifest_changed:
        _save_manifest(manifest)


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


def _cleanup_empty_parent(folder_path):
    """Remove the parent folder if it's empty or only contains a cvinfo file."""
    from flask import current_app

    if not folder_path or not os.path.isdir(folder_path):
        return

    # Don't clean up DATA_DIR roots or the trash dir itself
    data_dir = current_app.config.get("DATA_DIR", "/data")
    normalized_folder = os.path.normpath(folder_path)
    normalized_data = os.path.normpath(data_dir)
    if normalized_folder == normalized_data:
        return
    if is_trash_path(folder_path):
        return

    try:
        remaining = os.listdir(folder_path)
        # Empty folder — remove it
        if not remaining:
            shutil.rmtree(folder_path)
            app_logger.info(f"Removed empty folder after trash: {folder_path}")
            return
        # Only cvinfo file(s) remain — remove folder and contents
        if all(item.lower() == 'cvinfo' for item in remaining):
            shutil.rmtree(folder_path)
            app_logger.info(f"Removed folder with only cvinfo after trash: {folder_path}")
    except OSError as e:
        app_logger.error(f"Error cleaning up empty parent folder {folder_path}: {e}")


def move_to_trash(source_path):
    """
    Move a file or directory to the trash.

    Returns dict with:
      - trashed (bool): True if moved to trash, False if permanently deleted
      - path (str): destination path in trash (if trashed) or original path

    Falls back to permanent delete if trash is disabled.
    """
    # If it's an empty directory or only contains cvinfo, just delete it directly
    if os.path.isdir(source_path):
        try:
            contents = os.listdir(source_path)
            if not contents or all(item.lower() == 'cvinfo' for item in contents):
                shutil.rmtree(source_path)
                app_logger.info(f"Deleted empty/cvinfo-only folder (not trashed): {source_path}")
                return {"trashed": False, "path": source_path}
        except OSError:
            pass

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
        parent_dir = os.path.dirname(source_path)
        shutil.move(source_path, dest_path)
        app_logger.info(f"Moved to trash: {source_path} -> {dest_path}")

        # Record original path in manifest for restore
        manifest = _load_manifest()
        manifest[os.path.basename(dest_path)] = {
            "original_path": source_path,
            "deleted_at": time.time(),
        }
        _save_manifest(manifest)

        _cleanup_empty_parent(parent_dir)
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
        if entry.name == MANIFEST_FILENAME:
            continue
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

    # Clear the manifest file
    manifest_path = _get_manifest_path()
    if manifest_path and os.path.exists(manifest_path):
        try:
            os.remove(manifest_path)
        except OSError:
            pass

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

        # Remove from manifest
        manifest = _load_manifest()
        if item_name in manifest:
            del manifest[item_name]
            _save_manifest(manifest)

        return {"success": True, "size_freed": size}
    except OSError as e:
        app_logger.error(f"Error deleting trash item {item_name}: {e}")
        return {"success": False, "size_freed": 0, "error": str(e)}


def restore_from_trash(item_name):
    """
    Restore a trashed item to its original location.

    Returns dict: {success, restored_path, error}.
    May also include no_manifest or conflict flags.
    """
    trash_dir = get_trash_dir()
    if not trash_dir:
        return {"success": False, "error": "Trash is disabled"}

    item_path = os.path.join(trash_dir, item_name)

    if not os.path.exists(item_path):
        return {"success": False, "error": "Item not found in trash"}

    # Prevent path traversal
    normalized_item = os.path.normpath(item_path)
    normalized_trash = os.path.normpath(trash_dir)
    if not normalized_item.startswith(normalized_trash + os.sep):
        return {"success": False, "error": "Invalid item path"}

    # Look up original path in manifest
    manifest = _load_manifest()
    entry = manifest.get(item_name)
    if not entry:
        return {
            "success": False,
            "error": "No original path recorded for this item. Use drag-and-drop to restore it manually.",
            "no_manifest": True,
        }

    original_path = entry["original_path"]

    # Check for conflict at original location
    if os.path.exists(original_path):
        return {
            "success": False,
            "error": "A file already exists at the original location",
            "conflict": True,
            "original_path": original_path,
        }

    # Recreate parent directory if needed
    parent_dir = os.path.dirname(original_path)
    os.makedirs(parent_dir, exist_ok=True)

    try:
        shutil.move(item_path, original_path)
        app_logger.info(f"Restored from trash: {item_name} -> {original_path}")

        # Remove from manifest
        del manifest[item_name]
        _save_manifest(manifest)

        return {"success": True, "restored_path": original_path}
    except Exception as e:
        app_logger.error(f"Failed to restore from trash {item_name}: {e}")
        return {"success": False, "error": str(e)}
