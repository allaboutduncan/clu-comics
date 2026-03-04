import os
import time
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from database import add_file_index_entry, delete_file_index_entry, invalidate_collection_status_for_path
from app_logging import app_logger
from metadata_scanner import queue_file_for_scan, PRIORITY_NEW_FILE


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

    def _should_process_file(self, file_path):
        """
        Check if the file should be processed.
        Only process comic book files (cbz, cbr) and ignore hidden files.
        """
        if not os.path.isfile(file_path):
            return False

        # Ignore hidden files and system files
        basename = os.path.basename(file_path)
        if basename.startswith('.') or basename.startswith('~'):
            return False

        # Ignore files in the trash directory
        try:
            from helpers.trash import is_trash_path
            if is_trash_path(file_path):
                return False
        except Exception:
            pass

        # Only process comic book files
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in ['.cbz', '.cbr']:
            return False

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

            # Process the events
            for file_path in events_to_process:
                if self._should_process_file(file_path):
                    try:
                        file_name = os.path.basename(file_path)
                        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else None
                        modified_at = os.path.getmtime(file_path) if os.path.exists(file_path) else None
                        parent = os.path.dirname(file_path)

                        add_file_index_entry(file_name, file_path, 'file', size=file_size, parent=parent, modified_at=modified_at)
                        app_logger.info(f"✅ Indexed recent file from watcher: {file_name}")

                        # Queue CBZ files for metadata scanning (high priority for new files)
                        if file_path.lower().endswith('.cbz'):
                            queue_file_for_scan(file_path, PRIORITY_NEW_FILE)

                        # Invalidate collection status cache for this directory
                        invalidate_collection_status_for_path(file_path)
                    except Exception as e:
                        app_logger.error(f"Error processing file event for {file_path}: {e}")
                else:
                    app_logger.debug(f"File watcher skipped (filtered): {file_path}")

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
        app_logger.info(f"File watcher CREATE event: {event.src_path} (is_dir: {event.is_directory})")
        if event.is_directory:
            return

        file_path = event.src_path
        self._add_event(file_path)

    def on_modified(self, event):
        """Handle file modification events."""
        app_logger.info(f"File watcher MODIFY event: {event.src_path} (is_dir: {event.is_directory})")
        if event.is_directory:
            return

        file_path = event.src_path
        self._add_event(file_path)

    def on_moved(self, event):
        """Handle file move events (treat destination as a new file)."""
        app_logger.info(f"File watcher MOVE event: {event.src_path} -> {event.dest_path} (is_dir: {event.is_directory})")
        if event.is_directory:
            return

        file_path = event.dest_path
        self._add_event(file_path)

    def on_deleted(self, event):
        """Handle file deletion events."""
        app_logger.info(f"File watcher DELETE event: {event.src_path} (is_dir: {event.is_directory})")
        if event.is_directory:
            # We also want to remove directories, but for now focusing on files as per request
            # Logic for directories would be recursive delete which delete_file_index_entry handles
            delete_file_index_entry(event.src_path)
            return

        file_path = event.src_path
        # Check if it was a comic file (extension check)
        # Since file is gone, we can't check isfile or open it, but we can check extension
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in ['.cbz', '.cbr']:
            return

        try:
            delete_file_index_entry(file_path)
            app_logger.info(f"❌ Removed deleted file from index: {os.path.basename(file_path)}")

            # Invalidate collection status cache for this directory
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
