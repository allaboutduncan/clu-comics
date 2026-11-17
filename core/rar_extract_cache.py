"""Full-archive RAR extraction cache for the reader.

When a CBR page cannot be pulled out on its own (a solid archive, a tool that
truncates single entries), the reader extracts the whole archive once and
serves pages from that folder. The reader asks for several pages at once and
gunicorn runs eight threads in one worker, so this cache is shared state:

- **Single-flight per archive.** The first request for an archive extracts it;
  concurrent requests for the same archive wait on that entry's event and reuse
  the result. The global lock is held only for bookkeeping, never during an
  extraction, so different archives extract concurrently.
- **All or nothing.** An extraction runs in a hidden ``.staging_*`` folder and
  is renamed to an ``entry_*`` folder only when the extractor reports success.
  A failure releases every waiter with ``None``, removes the staging folder and
  drops the entry, so the next request retries.
- **Eviction never pulls a folder out from under a reader.** Each request holds
  a reference for as long as it reads (``acquire`` is a context manager). Only
  ready entries with no references are evicted; if every entry is busy the
  cache runs over its bound and evicts on the next release. An evicted entry is
  unpublished under the lock and its folder removed outside it.
- **The entry table is the per-key lock table.** An entry exists only while it
  is extracting or cached, so the table never holds more than the bound plus
  the in-flight requests.

The single gunicorn worker (``CLAUDE.md``) is what makes in-process locks
sufficient here.

Every directory this module removes is one it created, directly under its own
root, with one of its own prefixes; ``_remove_owned_dir`` refuses anything else.
"""

import os
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager

from core.app_logging import app_logger

STAGING_PREFIX = ".staging_"
ENTRY_PREFIX = "entry_"
DEFAULT_MAX_ENTRIES = 3


class _Entry:
    __slots__ = ("key", "dir", "ready", "refs", "last_used", "event")

    def __init__(self, key):
        self.key = key
        self.dir = None
        self.ready = False
        self.refs = 0
        self.last_used = time.monotonic()
        self.event = threading.Event()


class RarExtractCache:
    """Bounded, thread-safe cache of fully extracted archives.

    :param extractor: ``extractor(archive_path, dest_dir) -> bool``. ``dest_dir``
        is an empty folder owned by the cache; the extractor may create anything
        inside it. True publishes the folder, False (or an exception) discards it.
    :param root: the folder entries live in. ``None`` creates a private temp
        folder on first use.
    :param max_entries: how many idle extractions to keep.
    """

    def __init__(self, extractor, root=None, max_entries=DEFAULT_MAX_ENTRIES):
        self._extractor = extractor
        self._root = os.path.realpath(root) if root else None
        self._owns_root = root is None
        self._max = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._entries = {}

    # -- public ---------------------------------------------------------

    @contextmanager
    def acquire(self, archive_path):
        """Yield the extracted folder for *archive_path*, or None on failure.

        The folder is guaranteed to stay in place until the ``with`` block ends.
        """
        key = os.path.realpath(archive_path)
        entry, leader = self._join(key)
        try:
            if leader:
                self._extract(entry)
            else:
                entry.event.wait()
            yield entry.dir if entry.ready else None
        finally:
            self._release(entry)

    def entry_count(self):
        with self._lock:
            return len(self._entries)

    def snapshot(self):
        """``{key: (ready, refs, dir)}`` -- for tests and diagnostics."""
        with self._lock:
            return {k: (e.ready, e.refs, e.dir) for k, e in self._entries.items()}

    def shutdown(self):
        """Remove every cached folder (process exit)."""
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
            root = self._root
        for e in entries:
            if e.dir:
                self._remove_owned_dir(e.dir)
        if root and os.path.isdir(root):
            # Staging folders of extractions still running at exit.
            for name in os.listdir(root):
                self._remove_owned_dir(os.path.join(root, name))
            if self._owns_root:
                try:
                    os.rmdir(root)
                except OSError:
                    pass

    # -- internals ------------------------------------------------------

    def _ensure_root(self):
        # Called with self._lock held.
        if self._root is None:
            self._root = os.path.realpath(tempfile.mkdtemp(prefix="clu_rar_cache_"))
        else:
            os.makedirs(self._root, exist_ok=True)
        return self._root

    def _join(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.ready and not os.path.isdir(entry.dir):
                # Removed behind our back: unpublish and extract again.
                del self._entries[key]
                entry = None
            leader = entry is None
            if leader:
                entry = _Entry(key)
                self._entries[key] = entry
            entry.refs += 1
            entry.last_used = time.monotonic()
        return entry, leader

    def _extract(self, entry):
        staging = None
        ok = False
        try:
            with self._lock:
                root = self._ensure_root()
            staging = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=root)
            ok = bool(self._extractor(entry.key, staging))
            if ok:
                final = os.path.join(root, ENTRY_PREFIX + uuid.uuid4().hex)
                os.rename(staging, final)
                staging = None
                entry.dir = final
        except Exception as e:
            ok = False
            app_logger.warning(f"Full extraction failed for {entry.key}: {e}")
        finally:
            if staging:
                self._remove_owned_dir(staging)
            with self._lock:
                if ok:
                    entry.ready = True
                elif self._entries.get(entry.key) is entry:
                    del self._entries[entry.key]
            entry.event.set()

    def _release(self, entry):
        with self._lock:
            entry.refs -= 1
            entry.last_used = time.monotonic()
            victims = self._pick_victims()
        for d in victims:
            self._remove_owned_dir(d)

    def _pick_victims(self):
        # Called with self._lock held. Unpublishes idle ready entries beyond
        # the bound, oldest first, and returns their folders for removal.
        ready = [e for e in self._entries.values() if e.ready]
        excess = len(ready) - self._max
        if excess <= 0:
            return []
        idle = sorted((e for e in ready if e.refs == 0), key=lambda e: e.last_used)
        victims = []
        for e in idle[:excess]:
            del self._entries[e.key]
            victims.append(e.dir)
        return victims

    def _remove_owned_dir(self, path):
        """rmtree *path* only if it is one of ours, directly under the root."""
        root = self._root
        if not root or not path:
            return False
        name = os.path.basename(os.path.normpath(path))
        parent = os.path.realpath(os.path.dirname(os.path.normpath(path)))
        if (
            parent != root
            or not (name.startswith(STAGING_PREFIX) or name.startswith(ENTRY_PREFIX))
            or os.path.islink(path)
        ):
            app_logger.warning(f"RAR cache refused to remove {path}: not a cache folder")
            return False
        shutil.rmtree(path, ignore_errors=True)
        return True
