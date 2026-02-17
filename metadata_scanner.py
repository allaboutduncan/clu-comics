"""
metadata_scanner.py - Background worker for scanning ComicInfo.xml metadata

This module provides a priority queue-based background worker that:
1. Scans CBZ/ZIP files for ComicInfo.xml metadata
2. Updates the file_index table with extracted metadata
3. Tracks progress for UI feedback

The scanner runs as daemon threads and processes files in priority order:
- PRIORITY_NEW_FILE (1): Files just added via file_watcher (highest priority)
- PRIORITY_MODIFIED (2): Files modified since last scan
- PRIORITY_UNSCANNED (3): Files never scanned
- PRIORITY_BATCH (4): Batch scan during startup (lowest priority)
"""

import threading
from queue import PriorityQueue
import time
import os
import zipfile

from app_logging import app_logger
from config import config
from database import (
    get_files_needing_metadata_scan,
    get_metadata_scan_stats,
    update_file_metadata,
    update_metadata_scanned_at,
    get_file_index_entry_by_path
)
from comicinfo import read_comicinfo_from_zip

# Priority levels (lower = higher priority)
PRIORITY_NEW_FILE = 1      # Files just added via file_watcher
PRIORITY_MODIFIED = 2      # Files modified (modified_at > metadata_scanned_at)
PRIORITY_UNSCANNED = 3     # Files never scanned (metadata_scanned_at IS NULL)
PRIORITY_BATCH = 4         # Batch scan during startup

# Global state
metadata_queue = PriorityQueue()
scanner_progress = {
    'total_pending': 0,
    'scanned_count': 0,
    'errors': 0,
    'is_running': False,
    'current_file': None,
    'started_at': None,
    'last_update': None
}
scanner_lock = threading.Lock()
worker_threads = []
monitor_thread = None
monitor_stop_event = threading.Event()


class ScanTask:
    """
    Comparable task for priority queue.

    Tasks are ordered by priority (lower = higher priority),
    then by creation time (FIFO within same priority).
    """
    def __init__(self, priority, file_path, file_id, modified_at):
        self.priority = priority
        self.file_path = file_path
        self.file_id = file_id
        self.modified_at = modified_at
        self.created_at = time.time()

    def __lt__(self, other):
        # Lower priority number = higher priority
        # For same priority, older tasks first (FIFO within priority)
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.created_at < other.created_at


def scan_worker():
    """
    Worker thread that processes metadata scan tasks from the queue.

    Runs indefinitely until shutdown signal (None) is received.
    """
    while True:
        task = None
        try:
            task = metadata_queue.get()
            if task is None:  # Shutdown signal
                break

            with scanner_lock:
                scanner_progress['current_file'] = os.path.basename(task.file_path)

            process_metadata_scan(task)

            with scanner_lock:
                scanner_progress['scanned_count'] += 1
                scanner_progress['last_update'] = time.time()

        except Exception as e:
            app_logger.error(f"Metadata scanner worker error: {e}")
            with scanner_lock:
                scanner_progress['errors'] += 1
        finally:
            # Always mark task as done (if we got one)
            if task is not None:
                metadata_queue.task_done()


def process_metadata_scan(task):
    """
    Extract metadata from a CBZ file and update file_index.

    Performance: read_comicinfo_from_zip() takes ~5-50ms per file.

    Args:
        task: ScanTask with file_path, file_id, modified_at
    """
    try:
        # Get the actual filesystem path
        file_path = task.file_path

        # Handle /data/ prefix (Docker mount point)
        if file_path.startswith('/data/'):
            data_dir = config.get('SETTINGS', 'DATA_DIR', fallback='/data')
            # Remove /data prefix and join with actual data dir
            relative_path = file_path[6:]  # Remove '/data/'
            file_path = os.path.join(data_dir, relative_path)

        # Skip if file doesn't exist
        if not os.path.exists(file_path):
            app_logger.debug(f"Metadata scan skipped (file missing): {task.file_path}")
            update_metadata_scanned_at(task.file_id, time.time())
            return

        # Extract metadata (~5-50ms)
        try:
            metadata = read_comicinfo_from_zip(file_path)
        except zipfile.BadZipFile:
            app_logger.debug(f"Metadata scan skipped (invalid ZIP): {task.file_path}")
            update_metadata_scanned_at(task.file_id, time.time())
            return
        except Exception as e:
            app_logger.warning(f"Error reading ComicInfo.xml from {task.file_path}: {e}")
            update_metadata_scanned_at(task.file_id, time.time())
            return

        # Determine if ComicInfo.xml was present (metadata is non-empty dict)
        has_comicinfo = 1 if metadata else 0

        # Map ComicInfo fields to database columns
        db_metadata = {
            'ci_title': metadata.get('Title', ''),
            'ci_series': metadata.get('Series', ''),
            'ci_number': metadata.get('Number', ''),
            'ci_count': metadata.get('Count', ''),
            'ci_volume': metadata.get('Volume', ''),
            'ci_year': metadata.get('Year', ''),
            'ci_writer': metadata.get('Writer', ''),
            'ci_penciller': metadata.get('Penciller', ''),
            'ci_inker': metadata.get('Inker', ''),
            'ci_colorist': metadata.get('Colorist', ''),
            'ci_letterer': metadata.get('Letterer', ''),
            'ci_coverartist': metadata.get('CoverArtist', ''),
            'ci_publisher': metadata.get('Publisher', ''),
            'ci_genre': metadata.get('Genre', ''),
            'ci_characters': metadata.get('Characters', '')
        }

        # Update database
        update_file_metadata(task.file_id, db_metadata, time.time(), has_comicinfo)

        app_logger.debug(f"Metadata scanned: {os.path.basename(task.file_path)}")

    except Exception as e:
        # Mark as scanned even on error to prevent infinite retry loops
        app_logger.warning(f"Metadata scan error for {task.file_path}: {e}")
        update_metadata_scanned_at(task.file_id, time.time())
        with scanner_lock:
            scanner_progress['errors'] += 1


def queue_pending_files():
    """
    Queue all files that need metadata scanning.

    Called on startup and can be triggered manually via API.
    """
    try:
        files = get_files_needing_metadata_scan(limit=10000)

        with scanner_lock:
            scanner_progress['total_pending'] = len(files)

        for f in files:
            task = ScanTask(
                priority=PRIORITY_BATCH,
                file_path=f['path'],
                file_id=f['id'],
                modified_at=f['modified_at']
            )
            metadata_queue.put(task)

        if files:
            app_logger.info(f"Queued {len(files)} files for metadata scanning")

        return len(files)

    except Exception as e:
        app_logger.error(f"Error queuing pending files for metadata scan: {e}")
        return 0


def queue_file_for_scan(file_path, priority=PRIORITY_NEW_FILE):
    """
    Queue a single file for metadata scanning.

    Called by file_watcher when new/modified files are detected.

    Args:
        file_path: Full filesystem path to the file
        priority: Scan priority (default: high priority for new files)
    """
    try:
        # Only process CBZ/ZIP files
        if not file_path.lower().endswith(('.cbz', '.zip')):
            return

        # Convert filesystem path to database path format (/data/...)
        data_dir = config.get('SETTINGS', 'DATA_DIR', fallback='/data')
        if file_path.startswith(data_dir):
            db_path = '/data/' + file_path[len(data_dir):].lstrip('/').lstrip('\\')
        else:
            db_path = file_path

        # Normalize path separators
        db_path = db_path.replace('\\', '/')

        # Get file_id from database
        entry = get_file_index_entry_by_path(db_path)

        if entry:
            task = ScanTask(
                priority=priority,
                file_path=db_path,
                file_id=entry['id'],
                modified_at=entry['modified_at'] or time.time()
            )
            metadata_queue.put(task)

            with scanner_lock:
                scanner_progress['total_pending'] += 1

            app_logger.debug(f"Queued for metadata scan: {os.path.basename(file_path)}")

    except Exception as e:
        app_logger.error(f"Error queuing file for metadata scan: {e}")


def queue_files_for_scan(file_paths, priority=PRIORITY_NEW_FILE):
    """
    Queue multiple files for metadata scanning.

    Called by incremental sync when new files are detected.

    Args:
        file_paths: List of database paths (already in /data/... format)
        priority: Scan priority (default: high priority for new files)
    """
    queued_count = 0

    for file_path in file_paths:
        try:
            # Only process CBZ/ZIP files
            if not file_path.lower().endswith(('.cbz', '.zip')):
                continue

            # Normalize path separators
            db_path = file_path.replace('\\', '/')

            # Get file_id from database
            entry = get_file_index_entry_by_path(db_path)

            if entry:
                task = ScanTask(
                    priority=priority,
                    file_path=db_path,
                    file_id=entry['id'],
                    modified_at=entry['modified_at'] or time.time()
                )
                metadata_queue.put(task)
                queued_count += 1

        except Exception as e:
            app_logger.error(f"Error queuing file {file_path} for metadata scan: {e}")

    if queued_count > 0:
        with scanner_lock:
            scanner_progress['total_pending'] += queued_count
        app_logger.debug(f"Queued {queued_count} files for metadata scan")

    return queued_count


def queue_monitor():
    """
    Background thread that continuously monitors and queues pending files.

    Runs every 30 seconds to check if the queue is empty and there are
    more files needing scanning. This ensures all files eventually get scanned,
    even if the initial batch limit was exceeded.
    """
    check_interval = 30  # seconds between checks

    while not monitor_stop_event.is_set():
        try:
            # Wait for the specified interval or until stop event is set
            if monitor_stop_event.wait(timeout=check_interval):
                break  # Stop event was set

            # Only queue more files if queue is nearly empty
            if metadata_queue.qsize() < 100:
                # Check if there are more files needing scan
                files_needing_scan = get_files_needing_metadata_scan(limit=1)
                if files_needing_scan:
                    queued = queue_pending_files()
                    if queued > 0:
                        app_logger.info(f"Queue monitor: Queued {queued} additional files for metadata scanning")

        except Exception as e:
            app_logger.error(f"Queue monitor error: {e}")


def start_metadata_scanner(num_workers=None):
    """
    Initialize and start the metadata scanner background workers.

    Called from app.py during startup after file index is built.

    Args:
        num_workers: Number of worker threads (default from config or 2)
    """
    global worker_threads, monitor_thread

    # Check if scanning is enabled
    enabled = config.getboolean('SETTINGS', 'ENABLE_METADATA_SCAN', fallback=True)
    if not enabled:
        app_logger.info("Metadata scanning disabled in config")
        return

    # Get configured thread count
    if num_workers is None:
        num_workers = config.getint('SETTINGS', 'METADATA_SCAN_THREADS', fallback=2)
    num_workers = max(1, min(num_workers, 4))  # Clamp between 1-4

    with scanner_lock:
        scanner_progress['is_running'] = True
        scanner_progress['started_at'] = time.time()
        scanner_progress['scanned_count'] = 0
        scanner_progress['errors'] = 0

    # Clear the stop event in case scanner was previously stopped
    monitor_stop_event.clear()

    # Start worker threads
    for i in range(num_workers):
        t = threading.Thread(
            target=scan_worker,
            daemon=True,
            name=f"MetadataScanner-{i}"
        )
        t.start()
        worker_threads.append(t)

    app_logger.info(f"Started {num_workers} metadata scanner worker thread(s)")

    # Start queue monitor thread to continuously queue pending files
    monitor_thread = threading.Thread(
        target=queue_monitor,
        daemon=True,
        name="MetadataQueueMonitor"
    )
    monitor_thread.start()
    app_logger.info("Started metadata queue monitor thread")

    # Queue initial batch of files needing scan
    queue_pending_files()


def stop_metadata_scanner():
    """Gracefully stop the metadata scanner workers."""
    global worker_threads, monitor_thread

    with scanner_lock:
        scanner_progress['is_running'] = False

    # Signal the monitor thread to stop
    monitor_stop_event.set()
    if monitor_thread:
        monitor_thread.join(timeout=5)
        monitor_thread = None

    # Send shutdown signal to all workers
    for _ in worker_threads:
        metadata_queue.put(None)

    # Wait for workers to finish (with timeout)
    for t in worker_threads:
        t.join(timeout=5)

    worker_threads = []
    app_logger.info("Metadata scanner stopped")


def get_scanner_status():
    """
    Get comprehensive scanner status for API.

    Returns:
        Dict with scanner status, progress, and statistics
    """
    db_stats = get_metadata_scan_stats()

    with scanner_lock:
        return {
            'enabled': config.getboolean('SETTINGS', 'ENABLE_METADATA_SCAN', fallback=True),
            'is_running': scanner_progress['is_running'],
            'queue_size': metadata_queue.qsize(),
            'scanned_this_session': scanner_progress['scanned_count'],
            'error_count': scanner_progress['errors'],
            'current_file': scanner_progress['current_file'],
            'started_at': scanner_progress['started_at'],
            'last_update': scanner_progress['last_update'],
            'db_stats': db_stats,
            'threads': len(worker_threads)
        }
