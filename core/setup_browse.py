"""Folder picker for Guided Setup (``GET /api/setup/browse``).

``/list-directories`` only serves library roots and TARGET, so it cannot pick a
folder for a library that does not exist yet, or for a WATCH/TARGET that is
still unset. This lists **directories only**, under the folders a new install
can point at: ``/data`` and ``/downloads`` (the documented mounts) plus every
other volume the user mapped into the container, read from the mount table.
Owner-only by the ``/api/setup`` prefix in ``core/auth.py``.

Things that look arbitrary and are not:

- **Other volumes come from ``/proc/self/mountinfo``.** ``POST /api/libraries``
  accepts any existing folder, and docker-compose.yaml says ``/comics`` works,
  so a picker limited to ``/data`` would strand exactly the user who copied
  someone else's compose file. Only mounts a user could have mapped are
  offered: pseudo filesystems, the image's system directories and CLU's own
  ``/config``, ``/cache`` and ``/app`` are left out, so the root filesystem is
  never browsable. Without a mount table (Windows, macOS dev) the roots are
  just ``/data`` and ``/downloads``.
- **A mount inside another root is not a root of its own** (``/data/comics``
  is reached through ``/data``), so the list is what to click first, not every
  bind mount.
- **Containment is decided on the realpath**, of both the request and the
  roots. A symlink under a root that points at ``/etc`` resolves outside and
  is refused, and the paths returned are the resolved ones, so the folder the
  wizard saves is the folder that was validated.
- **A refused path and a missing one are told apart** (403 vs 404) only after
  containment, so the picker cannot be used to probe the rest of the
  filesystem.
- **All filesystem work is time-bounded** (``setup_preflight._bounded``),
  including ``realpath`` and the roots' existence checks: a hung CIFS/NFS
  mount is exactly what a new install is likely to have, and one ``stat``
  against it would hold the request until gunicorn's 120s timeout.
- **Hidden folders are left out** with the same ``is_hidden`` the preflight's
  ``/data`` listing uses, so the two agree on what a candidate folder is.
"""
import os

from core.setup_preflight import CHECK_TIMEOUT_SECONDS, _bounded

MAX_LISTED_DIRECTORIES = 1000

# Filesystems that are never a user's volume.
PSEUDO_FSTYPES = frozenset({
    "proc", "sysfs", "devpts", "devtmpfs", "cgroup", "cgroup2", "mqueue",
    "securityfs", "debugfs", "tracefs", "pstore", "bpf", "autofs",
    "binfmt_misc", "hugetlbfs", "configfs", "fusectl", "nsfs", "rpc_pipefs",
})

# The image's own directories: Docker bind-mounts files and pseudo filesystems
# here (/etc/hosts, /dev/shm, the docker.sock under /tmp), and none of them is
# a comics folder. /app is CLU itself.
SYSTEM_DIRS = (
    "/proc", "/sys", "/dev", "/run", "/etc", "/usr", "/lib", "/lib32",
    "/lib64", "/bin", "/sbin", "/boot", "/var", "/tmp", "/app",  # nosec B108 - excluded from browsing, never written
)


class BrowseError(Exception):
    """A request the picker refuses; ``status`` is the HTTP status to send."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _under(path, parent, sep):
    return path == parent or path.startswith(parent.rstrip(sep) + sep)


def user_mounts(table=None):
    """Mountpoints from the mount table that a user could have mapped.

    Paths as the mount table spells them, sorted; ``[]`` without a mount
    table. Reads no filesystem but ``/proc``.
    """
    from core.config import get_cache_dir, get_config_dir
    from core.db_health import _read_mountinfo

    if table is None:
        table = _read_mountinfo()
    excluded = SYSTEM_DIRS + (_posix(get_config_dir()), _posix(get_cache_dir()))
    found = set()
    for mountpoint, fstype, _source in table:
        if mountpoint == "/" or fstype in PSEUDO_FSTYPES:
            continue
        if any(_under(_posix(mountpoint), d, "/") for d in excluded):
            continue
        found.add(mountpoint)
    return sorted(found)


def _posix(path):
    return path.replace("\\", "/")


def _resolve_roots():
    """``[(realpath, mounted)]``: /data, /downloads, then other user mounts.

    ``mounted`` is True when the root is, or holds, a mountpoint, False when it
    is only the image's empty folder, and None without a mount table.
    """
    from core.config import get_data_dir, get_downloads_dir
    from core.db_health import _read_mountinfo

    table = _read_mountinfo()
    mounts = user_mounts(table)
    posix_mounts = [_posix(m) for m in mounts]
    candidates = [get_data_dir(), get_downloads_dir()] + mounts

    roots = []
    for candidate in candidates:
        resolved = os.path.realpath(candidate)
        if any(_under(os.path.normcase(resolved), os.path.normcase(r), os.sep)
               for r, _ in roots):
            continue
        if table:
            c = _posix(candidate)
            mounted = any(_under(m, c, "/") for m in posix_mounts)
        else:
            mounted = None
        roots.append((resolved, mounted))
    return roots


def _root_for(path, roots):
    """The root containing ``path`` (both realpaths), or None."""
    p = os.path.normcase(path)
    for root in roots:
        if _under(p, os.path.normcase(root), os.sep):
            return root
    return None


def _list_subdirectories(path):
    from helpers import is_hidden

    dirs = []
    with os.scandir(path) as it:
        for entry in it:
            try:
                if entry.is_dir() and not is_hidden(entry.path):
                    dirs.append(entry.name)
            except OSError:
                continue
    dirs.sort(key=str.lower)
    return dirs, os.access(path, os.W_OK)


def _browse(path):
    resolved = _resolve_roots()
    roots = [r for r, _ in resolved]
    root_info = [{"path": r, "exists": os.path.isdir(r), "mounted": m}
                 for r, m in resolved]

    if not path:
        return {
            "current_path": None,
            "parent": None,
            "writable": False,
            "directories": [{"name": r["path"], "path": r["path"]}
                            for r in root_info if r["exists"]],
            "truncated": False,
            "roots": root_info,
        }

    current = os.path.realpath(path)
    root = _root_for(current, roots)
    if root is None:
        raise BrowseError(403, "Access denied - only folders under "
                               + ", ".join(roots) + " can be browsed")
    if not os.path.isdir(current):
        raise BrowseError(404, "Directory not found")

    try:
        names, writable = _list_subdirectories(current)
    except OSError as e:
        raise BrowseError(500, f"Could not list {current}: {e.strerror or e}")

    truncated = len(names) > MAX_LISTED_DIRECTORIES
    names = names[:MAX_LISTED_DIRECTORIES]
    is_root = os.path.normcase(current) == os.path.normcase(root)
    return {
        "current_path": current,
        "parent": None if is_root else os.path.dirname(current),
        "writable": writable,
        "directories": [{"name": n, "path": os.path.join(current, n)}
                        for n in names],
        "truncated": truncated,
        "roots": root_info,
    }


def browse(path=None):
    """List the subdirectories of ``path``, or the roots when it is empty.

    Returns ``{current_path, parent, writable, directories, truncated, roots}``
    where each directory is ``{name, path}`` and each root is
    ``{path, exists, mounted}``. Raises ``BrowseError``.
    """
    finished, result, error = _bounded(lambda: _browse(path),
                                       CHECK_TIMEOUT_SECONDS)
    if not finished:
        raise BrowseError(504, f"Timed out listing {path or 'the mounts'}; a "
                               f"mount may be unreachable")
    if isinstance(error, BrowseError):
        raise error
    if error is not None:
        raise BrowseError(500, f"Could not list {path or 'the mounts'}: {error}")
    return result


def extra_mounts():
    """User mounts outside /data and /downloads, as realpaths (for preflight)."""
    from core.config import get_data_dir, get_downloads_dir

    defaults = {os.path.normcase(os.path.realpath(p))
                for p in (get_data_dir(), get_downloads_dir())}
    return [r for r, _ in _resolve_roots()
            if os.path.normcase(r) not in defaults]
