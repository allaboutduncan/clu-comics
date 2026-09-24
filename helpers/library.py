import os
import tempfile
from core.app_logging import app_logger


def get_library_roots():
    """
    Get list of all enabled library root paths.

    Returns:
        List of path strings for enabled libraries.
        Falls back to ['/data'] if no libraries configured.
    """
    from core.database import get_libraries
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
    from core.database import get_libraries
    libraries = get_libraries(enabled_only=True)
    return libraries[0] if libraries else None


def is_allowed_path(path):
    """Check if path is within any allowed directory (libraries, downloads, temp)."""
    if not path:
        return False
    normalized = os.path.normpath(os.path.realpath(path))

    allowed_roots = list(get_library_roots())

    # Add WATCH and TARGET (user_preferences-backed)
    from core.config import get_watch_dir, get_target_dir
    for val in (get_target_dir(), get_watch_dir()):
        if val:
            allowed_roots.append(val)

    # Add system temp directory
    allowed_roots.append(tempfile.gettempdir())

    for root in allowed_roots:
        root_normalized = os.path.normpath(os.path.realpath(root))
        if normalized == root_normalized or normalized.startswith(root_normalized + os.sep):
            return True
    return False


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


def library_root_for(path):
    """The enabled library root containing *path*, or None.

    Resolves symlinks on both sides, which :func:`is_valid_library_path` does
    not — that one compares normpaths and has many callers relying on it. Use
    this where the answer decides whether an automated pass may move or delete
    something, because a symlinked TARGET is exactly how a library sneaks back
    into a path that looks unrelated.
    """
    if not path:
        return None
    try:
        normalized = os.path.realpath(path)
    except Exception:
        return None
    for root in get_library_roots():
        try:
            root_normalized = os.path.realpath(root)
        except Exception:
            continue
        if normalized == root_normalized or normalized.startswith(
            root_normalized + os.sep
        ):
            return root
    return None


def watch_target_verdict(label, path):
    """Whether *path* may be used as WATCH/TARGET, and what to tell the user.

    Returns ``(blocked, message)``. ``message`` is present for a warning too,
    so the caller can save the value and still say something.

    Two verdicts, deliberately different:

    * **/data or inside it — blocked.** That is where an unconfigured install
      puts the whole collection, and pointing a staging folder at it turns the
      wanted scan loose on the library.
    * **Inside any other enabled library root — allowed, with a warning.**
      This is a layout people really run (downloads land flat in the library
      root and are filed into series folders from there). It is safe because
      ``helpers.collection.collect_target_candidates`` refuses to descend in
      that case, but the restriction has to be *said*, or a wrapper-folder
      download simply appears not to be filed.

    The ``/data`` clause is kept explicitly rather than left to
    ``get_library_roots()``'s fallback: that fallback only applies when no
    libraries are configured *and* /data exists, and the guard this replaces
    was unconditional. It must not get weaker.
    """
    if not path:
        return False, None
    try:
        real_value = os.path.realpath(path)
    except Exception:
        return False, None

    try:
        real_data = os.path.realpath("/data")
        if real_value == real_data or real_value.startswith(real_data + os.sep):
            return True, f"{label} cannot be /data or a subdirectory of it."
    except Exception:
        pass

    root = library_root_for(path)
    if root:
        return False, (
            f"{label} is inside the library at {root}. Saved, but downloads "
            f"will only be filed from the top level of that folder — CLU will "
            f"not walk into it, so a comic already filed in a series folder is "
            f"never moved."
        )
    return False, None


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
    from core.database import get_libraries
    normalized = os.path.normpath(path)
    for lib in get_libraries(enabled_only=True):
        root = os.path.normpath(lib['path'])
        if normalized == root or normalized.startswith(root + os.sep):
            return lib
    return None


def _resolve_trash_dir():
    """TRASH path without needing a Flask app context.

    ``helpers.trash.get_trash_dir`` reads ``current_app`` and also *creates* the
    directory, so it is unusable from a scheduler thread. This mirrors its
    fallback chain against the RawConfigParser instead.
    """
    from core.config import config

    try:
        trash_dir = config.get("SETTINGS", "TRASH_DIR", fallback="").strip()
        if not trash_dir:
            cache_dir = config.get("SETTINGS", "CACHE_DIR", fallback="/cache")
            trash_dir = os.path.join(cache_dir, "trash")
        return trash_dir
    except Exception:
        return ""


def get_protected_roots():
    """Configured roots an automated sweep must never delete or descend into.

    WATCH, TARGET, TRASH and every enabled library root, as realpaths.

    Deliberately separate from :func:`is_critical_path`, which answers the same
    question for interactive routes. That one re-reads preferences from the DB on
    every call and compares raw strings — fine for a single "can the user delete
    this?" check, wrong inside a directory walk, where it would cost a DB hit per
    directory and where a stored trailing slash ("/downloads/temp/") would defeat
    the equality test. Callers resolve this set once and reuse it.

    Keep the two in step: a new protected location belongs in both.
    """
    from core.config import get_watch_dir, get_target_dir

    candidates = [
        get_watch_dir() or "/downloads/temp",
        get_target_dir() or "/downloads/processed",
        _resolve_trash_dir(),
    ]
    try:
        candidates.extend(get_library_roots())
    except Exception as e:
        # A library lookup failure must not silently shrink the protected set.
        app_logger.error(f"Could not resolve library roots for protection: {e}")

    roots = set()
    for path in candidates:
        if not path:
            continue
        try:
            roots.add(os.path.realpath(path))
        except Exception:
            continue
    return roots


def is_critical_path(path):
    """
    Check if a path is a critical system path (WATCH, TARGET, or TRASH folders).
    Returns True if the path is critical, False otherwise.

    For automated sweeps use :func:`get_protected_roots` instead — see its
    docstring for why. A new protected location belongs in both.
    """
    from core.config import config, get_watch_dir, get_target_dir

    if not path:
        return False

    # Get current watch and target folders (user_preferences-backed)
    watch_folder = get_watch_dir() or "/downloads/temp"
    target_folder = get_target_dir() or "/downloads/processed"

    # Check if path is exactly a critical folder
    if path == watch_folder or path == target_folder:
        return True

    # Check if path is a parent directory of critical folders
    if (path in watch_folder and watch_folder.startswith(path)) or (path in target_folder and target_folder.startswith(path)):
        return True

    # Protect the trash directory root
    try:
        trash_dir = config.get("SETTINGS", "TRASH_DIR", fallback="").strip()
        if not trash_dir:
            cache_dir = config.get("SETTINGS", "CACHE_DIR", fallback="/cache")
            trash_dir = os.path.join(cache_dir, "trash")
        if os.path.normpath(path) == os.path.normpath(trash_dir):
            return True
    except Exception:
        pass

    return False


def get_critical_path_error_message(path, operation="modify"):
    """
    Generate an error message for critical path operations.
    """
    from core.config import get_watch_dir, get_target_dir

    watch_folder = get_watch_dir() or "/downloads/temp"
    target_folder = get_target_dir() or "/downloads/processed"

    if path == watch_folder:
        return f"Cannot {operation} watch folder: {path}. Please use the configuration page to change the watch folder."
    elif path == target_folder:
        return f"Cannot {operation} target folder: {path}. Please use the configuration page to change the target folder."
    else:
        return f"Cannot {operation} parent directory of critical folders: {path}. Please use the configuration page to change watch/target folders."
