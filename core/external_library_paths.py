"""Resolve a file path reported by another media server to a file in CLU.

Komga and Kavita both describe a comic by the path *they* see, which is rarely
the path CLU sees (different container mounts), and for some accounts is only a
bare filename. The answer is the same for both: try the configured per-library
prefix mappings, then fall back to an exact filename match against the file
index. ``models/komga.py`` re-exports these under its original names.
"""
import os


def file_name_of(path):
    """Last path segment, with extension.

    Both separators are normalized: os.path.basename() ignores '\\' on POSIX, so
    a server running on Windows would otherwise yield the whole path.
    """
    return (path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _is_absolute_path(path):
    """
    True for a POSIX-absolute or natively-absolute path.

    os.path.isabs() alone is not enough: on Windows it rejects '/data/x' (since
    Python 3.13 a lone leading slash is no longer absolute), and the paths CLU
    actually runs on in Docker are POSIX.
    """
    return path.startswith("/") or os.path.isabs(path)


def _parent_dir_name(path):
    """Name of the directory containing `path`, or None if it has no parent."""
    parts = (path or "").replace("\\", "/").rstrip("/").split("/")
    if len(parts) < 2 or not parts[-2]:
        return None
    return parts[-2]


def map_prefix(remote_path, remote_prefix, clu_prefix):
    """
    Convert a remote file path to a CLU file path using prefix mapping.

    Returns the path unchanged when the prefix doesn't apply.

    Example:
        remote_path:   /comics/Marvel/Spider-Man 001.cbz
        remote_prefix: /comics
        clu_prefix:    /data
        result:        /data/Marvel/Spider-Man 001.cbz
    """
    if not remote_prefix or not clu_prefix:
        return remote_path

    # Normalize path separators
    remote_path = remote_path.replace("\\", "/")
    remote_prefix = remote_prefix.rstrip("/").replace("\\", "/")
    clu_prefix = clu_prefix.rstrip("/").replace("\\", "/")

    # Match on a path boundary: prefix '/comics' must not swallow
    # '/comics-archive/X.cbz' and rewrite it to '/data-archive/X.cbz'.
    if remote_path == remote_prefix or remote_path.startswith(remote_prefix + "/"):
        relative = remote_path[len(remote_prefix):]
        return clu_prefix + relative

    return remote_path


def map_prefix_multi(remote_path, mappings):
    """
    Try each mapping; return the first match or the original path.

    Args:
        remote_path: The file path as the other server sees it
        mappings: List of dicts with 'remote_prefix' and 'clu_prefix', sorted by
            prefix length descending so longer prefixes match first
    """
    for m in mappings:
        result = map_prefix(remote_path, m["remote_prefix"], m["clu_prefix"])
        if result != remote_path:
            return result
    return remote_path


def build_mappings(rows, prefix_key):
    """Turn library-mapping rows into the sorted list ``resolve_external_path`` takes.

    Args:
        rows: Dicts with 'library_path' and the remote prefix under `prefix_key`
        prefix_key: e.g. 'kavita_path_prefix'
    """
    active = []
    for row in rows or []:
        remote = (row.get(prefix_key) or "").strip()
        clu = (row.get("library_path") or "").strip()
        if remote and clu:
            active.append({"remote_prefix": remote, "clu_prefix": clu})
    active.sort(key=lambda m: len(m["remote_prefix"]), reverse=True)
    return active


def resolve_external_path(remote_path, mappings, file_name=None, lookup=None):
    """
    Resolve a file another server reports to a file in CLU's library.

    Tries the configured path prefixes first (exact and unambiguous), then falls
    back to an exact filename match against the file index -- which is the only
    thing that works when the server hands out a bare filename, or when no
    prefixes are set.

    Args:
        remote_path: Path (or bare filename) as the other server reports it
        mappings: List of dicts with 'remote_prefix' and 'clu_prefix', sorted by
            prefix length descending
        file_name: Filename with extension, when the caller already has it
        lookup: Filename lookup, injectable for tests. Defaults to
            core.database.find_file_index_paths_by_name.

    Returns:
        Tuple of (clu_path or None, reason) where reason is one of
        'mapping', 'file_index', 'no_match', 'ambiguous'.
    """
    if lookup is None:
        from core.database import find_file_index_paths_by_name as lookup

    url = (remote_path or "").replace("\\", "/")
    file_name = file_name or file_name_of(url)

    # Prefix mapping only means anything when the path carries a directory.
    # A bare filename has nothing to map, and os.path.exists() would resolve
    # it against CLU's working directory.
    if "/" in url:
        mapped = map_prefix_multi(url, mappings)
        # exists() is what separates "mapped correctly" from "no mapping
        # applied" -- map_prefix_multi returns its input on no match, and
        # another server's path must never reach the database as a CLU path.
        if mapped and _is_absolute_path(mapped) and os.path.exists(mapped):
            return mapped, "mapping"

    if not file_name:
        return None, "no_match"

    # No exists() check on this branch: the path came out of CLU's own index,
    # so it is a CLU path by construction. A stale row costs one orphan history
    # entry; a stat() per book would re-introduce the environment coupling that
    # made every match fail in the first place.
    candidates = lookup(file_name)
    if len(candidates) == 1:
        return candidates[0], "file_index"
    if not candidates:
        return None, "no_match"

    # The same filename in two libraries. Try the directory the server reports,
    # then give up: marking the wrong comic read silently corrupts reading
    # history and cannot be undone.
    parent = _parent_dir_name(url)
    if parent:
        narrowed = [p for p in candidates if _parent_dir_name(p) == parent]
        if len(narrowed) == 1:
            return narrowed[0], "file_index"
    return None, "ambiguous"
