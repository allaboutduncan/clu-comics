import os
import time
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from core.database import (
    add_file_index_entry,
    forget_deleted_path,
    invalidate_collection_status_for_path,
    move_indexed_path,
)
from core.app_logging import app_logger
from core.metadata_scanner import queue_file_for_scan, PRIORITY_NEW_FILE
from helpers.collection import _series_id_for_path, reconcile_wanted_for_series
from helpers.library import is_valid_library_path


COMIC_EXTENSIONS = ('.cbz', '.cbr')


def _in_trash(path):
    """``helpers.trash.is_trash_path`` reads ``current_app``, which a watcher
    thread does not have; "could not tell" reads as "not the trash"."""
    try:
        from helpers.trash import is_trash_path
        return is_trash_path(path)
    except Exception:
        return False


class DebouncedFileHandler(FileSystemEventHandler):
    """
    File system event handler with debouncing to prevent duplicate events.
    Only tracks file creation/modification events in the /data directory.
    """

    def __init__(self, debounce_seconds=2):
        """
        Initialize the debounced file handler.

        Args:
            debounce_seconds: Time window to debounce events (default 2 seconds)
        """
        super().__init__()
        self.debounce_seconds = debounce_seconds
        self.pending_events = {}  # {file_path: last_event_time}
        self.lock = threading.Lock()
        self.debounce_timer = None

    @staticmethod
    def _is_comic_name(file_path):
        """Name-only test: a comic archive that is not hidden or a temp file.

        Cheap (no stat), so event handlers can drop series.json, .tmp files and
        the like before they reach the pending queue.
        """
        basename = os.path.basename(file_path)
        if basename.startswith('.') or basename.startswith('~'):
            return False
        return os.path.splitext(basename)[1].lower() in COMIC_EXTENSIONS

    def _should_process_file(self, file_path):
        """
        Check if the file should be processed.
        Only process comic book files (cbz, cbr) and ignore hidden files.
        """
        if not os.path.isfile(file_path):
            return False

        if not self._is_comic_name(file_path):
            return False

        # Ignore files in the trash directory
        try:
            from helpers.trash import is_trash_path
            if is_trash_path(file_path):
                return False
        except Exception:
            pass

        return True

    def _process_pending_events(self):
        """Process all pending events that have exceeded the debounce window."""
        with self.lock:
            current_time = time.time()
            events_to_process = []

            # Find events that are ready to process
            for file_path, event_time in list(self.pending_events.items()):
                if current_time - event_time >= self.debounce_seconds:
                    events_to_process.append(file_path)
                    del self.pending_events[file_path]

            # Series touched in this batch — reconcile each once after the loop
            # so a bulk import doesn't re-scan the same series folder per file.
            affected_series = set()
            indexed_count = 0

            # Process the events
            for file_path in events_to_process:
                if self._should_process_file(file_path):
                    try:
                        file_name = os.path.basename(file_path)
                        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else None
                        modified_at = os.path.getmtime(file_path) if os.path.exists(file_path) else None
                        parent = os.path.dirname(file_path)

                        add_file_index_entry(file_name, file_path, 'file', size=file_size, parent=parent, modified_at=modified_at)
                        indexed_count += 1
                        app_logger.debug(f"Indexed recent file from watcher: {file_name}")

                        # Queue CBZ files for metadata scanning (high priority for new files)
                        if file_path.lower().endswith('.cbz'):
                            queue_file_for_scan(file_path, PRIORITY_NEW_FILE)

                        # Resolve the mapped series this file belongs to so we can
                        # reconcile the wanted list (and collection-status cache)
                        # below. Fall back to path-based invalidation when the file
                        # is not under any mapped series folder.
                        series_id = _series_id_for_path(file_path)
                        if series_id:
                            affected_series.add(series_id)
                        else:
                            invalidate_collection_status_for_path(file_path)
                    except Exception as e:
                        app_logger.error(f"Error processing file event for {file_path}: {e}")
                else:
                    app_logger.debug(f"File watcher skipped (filtered): {file_path}")

            # Reconcile each affected series once: prunes satisfied wanted issues
            # and refreshes the collection-status cache so the nightly
            # auto-download stops re-downloading files that already exist.
            for series_id in affected_series:
                try:
                    reconcile_wanted_for_series(series_id)
                except Exception as e:
                    app_logger.error(f"Error reconciling wanted for series {series_id}: {e}")

            if indexed_count:
                app_logger.info(
                    f"File watcher indexed {indexed_count} file(s), "
                    f"{len(affected_series)} series reconciled"
                )

            # Schedule next check if there are still pending events
            if self.pending_events:
                self.debounce_timer = threading.Timer(self.debounce_seconds, self._process_pending_events)
                self.debounce_timer.daemon = True
                self.debounce_timer.start()

    def _add_event(self, file_path):
        """Add or update an event in the pending queue."""
        with self.lock:
            self.pending_events[file_path] = time.time()

            # Start the debounce timer if not already running
            if self.debounce_timer is None or not self.debounce_timer.is_alive():
                self.debounce_timer = threading.Timer(self.debounce_seconds, self._process_pending_events)
                self.debounce_timer.daemon = True
                self.debounce_timer.start()

    def on_any_event(self, event):
        """Log all events for debugging."""
        app_logger.debug(f"File watcher received event: {event.event_type} - {event.src_path}")

    def on_created(self, event):
        """Handle file creation events."""
        if event.is_directory or not self._is_comic_name(event.src_path):
            return

        file_path = event.src_path
        self._add_event(file_path)

    def on_modified(self, event):
        """Handle file modification events."""
        if event.is_directory or not self._is_comic_name(event.src_path):
            return

        file_path = event.src_path
        self._add_event(file_path)

    def on_moved(self, event):
        """Follow a move: the index rows and path references go with the file.

        This used to only queue the destination, so the source row stayed and
        every comic moved outside CLU was indexed twice, at its old and its new
        path (#693). A directory event is the one that carries the subtree;
        watchdog's per-child events that follow find their rows already moved
        and only refresh the destination.
        """
        src, dest = event.src_path, event.dest_path
        if not event.is_directory and not (
            self._is_comic_name(src) or self._is_comic_name(dest)
        ):
            return

        try:
            src_in = is_valid_library_path(src)
            dest_in = is_valid_library_path(dest) and not _in_trash(dest)
            if src_in and dest_in:
                move_indexed_path(src, dest, is_dir=event.is_directory)
            elif src_in:
                # Moved out of the libraries (the trash included): a deletion.
                forget_deleted_path(src)
        except Exception as e:
            app_logger.error(f"Error following move {src} -> {dest}: {e}")

        if not event.is_directory and self._is_comic_name(dest):
            self._add_event(dest)

    def on_deleted(self, event):
        """Handle file deletion events."""
        if event.is_directory:
            # We also want to remove directories, but for now focusing on files as per request
            # Logic for directories would be recursive delete which
            # delete_file_index_entry (inside forget_deleted_path) handles
            forget_deleted_path(event.src_path)
            return

        file_path = event.src_path
        # Check if it was a comic file (extension check)
        # Since file is gone, we can't check isfile or open it, but we can check extension
        if os.path.splitext(file_path)[1].lower() not in COMIC_EXTENSIONS:
            return

        try:
            forget_deleted_path(file_path)
            app_logger.debug(f"Removed deleted file from index: {os.path.basename(file_path)}")

            # Reconcile the owning series (robustly invalidates the
            # collection-status cache by id so the deleted file is re-scanned);
            # fall back to path-based invalidation if unresolved.
            series_id = _series_id_for_path(file_path)
            if series_id:
                reconcile_wanted_for_series(series_id)
            else:
                invalidate_collection_status_for_path(file_path)
        except Exception as e:
            app_logger.error(f"Error removing deleted file {file_path}: {e}")



class FileWatcher:
    """
    Manages the file system watcher for the /data directory.
    """

    def __init__(self, watch_path, debounce_seconds=2):
        """
        Initialize the file watcher.

        Args:
            watch_path: Path to watch for file changes
            debounce_seconds: Debounce time for events (default 2 seconds)
        """
        self.watch_path = watch_path
        self.observer = Observer()
        self.event_handler = DebouncedFileHandler(debounce_seconds=debounce_seconds)

    def start(self):
        """Start watching the directory in a background thread."""
        try:
            if not os.path.exists(self.watch_path):
                app_logger.error(f"Watch path does not exist: {self.watch_path}")
                return False

            self.observer.schedule(self.event_handler, self.watch_path, recursive=True)
            self.observer.start()
            app_logger.info(f"File watcher started for: {self.watch_path}")
            return True

        except Exception as e:
            app_logger.error(f"Failed to start file watcher: {e}")
            return False

    def stop(self):
        """Stop the file watcher."""
        try:
            self.observer.stop()
            self.observer.join(timeout=5)
            app_logger.info("File watcher stopped")
        except Exception as e:
            app_logger.error(f"Error stopping file watcher: {e}")

    def is_alive(self):
        """Check if the watcher is running."""
        return self.observer.is_alive()
