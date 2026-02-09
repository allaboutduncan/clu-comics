import os
from app_logging import app_logger


def get_library_roots():
    """
    Get list of all enabled library root paths.

    Returns:
        List of path strings for enabled libraries.
        Falls back to ['/data'] if no libraries configured.
    """
    from database import get_libraries
    libraries = get_libraries(enabled_only=True)
    if libraries:
        return [lib['path'] for lib in libraries]
    # Fallback for backwards compatibility
    return ['/data'] if os.path.exists('/data') else []


def get_default_library():
    """
    Get the first enabled library or None.

    Returns:
        Dictionary with library data, or None if no libraries configured.
    """
    from database import get_libraries
    libraries = get_libraries(enabled_only=True)
    return libraries[0] if libraries else None


def is_valid_library_path(path):
    """
    Check if a path is within any enabled library.

    Args:
        path: The path to validate

    Returns:
        True if path is within a configured library, False otherwise.
    """
    if not path:
        return False
    normalized = os.path.normpath(path)
    for root in get_library_roots():
        root_normalized = os.path.normpath(root)
        # Check if path equals root or is a subdirectory of root
        if normalized == root_normalized or normalized.startswith(root_normalized + os.sep):
            return True
    return False


def get_library_for_path(path):
    """
    Get the library that contains this path.

    Args:
        path: The path to look up

    Returns:
        Dictionary with library data, or None if path not in any library.
    """
    if not path:
        return None
    from database import get_libraries
    normalized = os.path.normpath(path)
    for lib in get_libraries(enabled_only=True):
        root = os.path.normpath(lib['path'])
        if normalized == root or normalized.startswith(root + os.sep):
            return lib
    return None


def is_critical_path(path):
    """
    Check if a path is a critical system path (WATCH or TARGET folders).
    Returns True if the path is critical, False otherwise.
    """
    from config import config

    if not path:
        return False

    # Get current watch and target folders from config
    watch_folder = config.get("SETTINGS", "WATCH", fallback="/temp")
    target_folder = config.get("SETTINGS", "TARGET", fallback="/processed")

    # Check if path is exactly a critical folder
    if path == watch_folder or path == target_folder:
        return True

    # Check if path is a parent directory of critical folders
    if (path in watch_folder and watch_folder.startswith(path)) or (path in target_folder and target_folder.startswith(path)):
        return True

    return False


def get_critical_path_error_message(path, operation="modify"):
    """
    Generate an error message for critical path operations.
    """
    from config import config

    watch_folder = config.get("SETTINGS", "WATCH", fallback="/temp")
    target_folder = config.get("SETTINGS", "TARGET", fallback="/processed")

    if path == watch_folder:
        return f"Cannot {operation} watch folder: {path}. Please use the configuration page to change the watch folder."
    elif path == target_folder:
        return f"Cannot {operation} target folder: {path}. Please use the configuration page to change the target folder."
    else:
        return f"Cannot {operation} parent directory of critical folders: {path}. Please use the configuration page to change watch/target folders."
