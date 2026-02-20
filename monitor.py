import time
import logging
import shutil
import os
import zipfile
import re # Added for _is_temporary_download_file
import math # Added for format_size
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler
from cbz_ops.rename import rename_file, clean_directory_name
from cbz_ops.single_file import convert_to_cbz
from config import config, load_config
from helpers import is_hidden
from app_logging import MONITOR_LOG
from database import init_db

load_config()

# Initialize database
init_db()

# These initial reads remain for startup.
directory = config.get("SETTINGS", "WATCH", fallback="/temp")
target_directory = config.get("SETTINGS", "TARGET", fallback="/processed")
ignored_exts_config = config.get("SETTINGS", "IGNORED_EXTENSIONS", fallback=".crdownload")
ignored_extensions = [ext.strip() for ext in ignored_exts_config.split(",") if ext.strip()]
autoconvert = config.getboolean("SETTINGS", "AUTOCONVERT", fallback=False)
subdirectories = config.getboolean("SETTINGS", "READ_SUBDIRECTORIES", fallback=False)
move_directories = config.getboolean("SETTINGS", "MOVE_DIRECTORY", fallback=False)
consolidate_directories = config.getboolean("SETTINGS", "CONSOLIDATE_DIRECTORIES", fallback=False)
auto_unpack = config.getboolean("SETTINGS", "AUTO_UNPACK", fallback=False)
auto_cleanup = config.getboolean("SETTINGS", "AUTO_CLEANUP_ORPHAN_FILES", fallback=True)
cleanup_interval_hours = config.getint("SETTINGS", "CLEANUP_INTERVAL_HOURS", fallback=1)

# Logging setup - MONITOR_LOG imported from app_logging
monitor_logger = logging.getLogger("monitor_logger")
monitor_logger.setLevel(logging.INFO)
# Only add handler if not already added (prevents duplicate handlers)
if not monitor_logger.handlers:
    monitor_handler = logging.FileHandler(MONITOR_LOG)
    monitor_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    monitor_logger.addHandler(monitor_handler)

monitor_logger.info("Monitor script started!")
monitor_logger.info(f"1. Monitoring: {directory}")
monitor_logger.info(f"2. Target: {target_directory}")
monitor_logger.info(f"3. Ignored Extensions: {ignored_extensions}")
monitor_logger.info(f"4. Auto-Conversion Enabled: {autoconvert}")
monitor_logger.info(f"5. Monitor Sub-Directories Enabled: {subdirectories}")
monitor_logger.info(f"6. Move Sub-Directories Enabled: {move_directories}")
monitor_logger.info(f"7. Consolidate Directories: {consolidate_directories}")
monitor_logger.info(f"8. Auto Unpack Enabled: {auto_unpack}")
monitor_logger.info(f"9. Auto Cleanup Orphan Files: {auto_cleanup}")
monitor_logger.info(f"10. Cleanup Interval: {cleanup_interval_hours} hour(s)")

class DownloadCompleteHandler(FileSystemEventHandler):
    def __init__(self, directory, target_directory, ignored_extensions):
        """
        Store initial values. We'll refresh them on each event
        to get updated config values.
        """
        self.directory = directory
        self.target_directory = target_directory
        self.ignored_extensions = set(ext.lower() for ext in ignored_extensions)
        self.autoconvert = autoconvert
        self.subdirectories = subdirectories
        self.move_directories = move_directories
        self.consolidate_directories = consolidate_directories
        self.auto_unpack = auto_unpack
        self._initial_dir_file_counts = {}


    def reload_settings(self):
        ###
        # Re-reads config values so that if config.ini changes,
        # this handler will use the latest settings.
        ###
        self.directory = config.get("SETTINGS", "WATCH", fallback="/temp")
        self.target_directory = config.get("SETTINGS", "TARGET", fallback="/processed")

        ignored_exts_config = config.get("SETTINGS", "IGNORED_EXTENSIONS", fallback=".crdownload")
        self.ignored_extensions = set(ext.strip().lower() for ext in ignored_exts_config.split(",") if ext.strip())

        self.autoconvert = config.getboolean("SETTINGS", "AUTOCONVERT", fallback=False)
        self.subdirectories = config.getboolean("SETTINGS", "READ_SUBDIRECTORIES", fallback=False)
        self.move_directories = config.getboolean("SETTINGS", "MOVE_DIRECTORY", fallback=False)
        self.consolidate_directories = config.getboolean("SETTINGS", "CONSOLIDATE_DIRECTORIES", fallback=False)
        self.auto_unpack = config.getboolean("SETTINGS", "AUTO_UNPACK", fallback=False)
        self.auto_cleanup = config.getboolean("SETTINGS", "AUTO_CLEANUP_ORPHAN_FILES", fallback=True)
        self.cleanup_interval_hours = config.getint("SETTINGS", "CLEANUP_INTERVAL_HOURS", fallback=1)

        monitor_logger.info(f"********************// Config Reloaded //********************")
        monitor_logger.info(
            f"Directory: {self.directory}, Target: {self.target_directory}, "
            f"Ignored: {self.ignored_extensions}, autoconvert: {self.autoconvert}, "
            f"subdirectories: {self.subdirectories}, move_directories: {self.move_directories}, "
            f"consolidate_directories: {self.consolidate_directories}, "
            f"auto_unpack: {self.auto_unpack}, auto_cleanup: {self.auto_cleanup}, "
            f"cleanup_interval: {self.cleanup_interval_hours}h"
        )


    def unzip_file(self, zip_filename):
        """
        Unzips the specified .zip file located in the current directory.
        Extracts all contents into the current directory.
        """
        # Check if the file exists in the current directory
        if not os.path.isfile(zip_filename):
            print(f"Error: {zip_filename} not found in the current directory.")
            return

        # Open and extract the zip file
        with zipfile.ZipFile(zip_filename, 'r') as zip_ref:
            zip_ref.extractall(directory)  # Defaults to current directory

        monitor_logger.info(f"Successfully extracted {zip_filename} into {os.getcwd()}")

        # Delete the zip file after extraction if it still exists
        if os.path.exists(zip_filename):
            try:
                os.remove(zip_filename)
                monitor_logger.info(f"Deleted zip file: {zip_filename}")
            except Exception as e:
                monitor_logger.error(f"Error deleting {zip_filename}: {e}")
        else:
            monitor_logger.info(f"Zip file {zip_filename} not found during deletion; it may have been already removed.")


    def on_created(self, event):
        # Refresh settings on every event
        self.reload_settings()

        if not event.is_directory:
            self._handle_file_if_complete(event.src_path)
            monitor_logger.info(f"File created: {event.src_path}")
        else:
            monitor_logger.info(f"Directory created: {event.src_path}")
            self._scan_directory(event.src_path)


    def on_modified(self, event):
        self.reload_settings()

        if not event.is_directory:
            self._handle_file_if_complete(event.src_path)
            monitor_logger.info(f"File Modified: {event.src_path}")


    def on_moved(self, event):
        self.reload_settings()

        if not event.is_directory:
            self._handle_file_if_complete(event.dest_path)
            monitor_logger.info(f"File Moved: {event.dest_path}")
        else:
            monitor_logger.info(f"Directory Moved: {event.dest_path}")
            self._scan_directory(event.dest_path)


    def _scan_directory(self, directory):
        for root, dirs, files in os.walk(directory):
            # Skip hidden directories from being traversed.
            dirs[:] = [d for d in dirs if not is_hidden(os.path.join(root, d))]
            for file in files:
                file_path = os.path.join(root, file)
                # Skip hidden files.
                if is_hidden(file_path):
                    continue
                self._handle_file_if_complete(file_path)
                monitor_logger.info(f"Scanning directory - found file: {file_path}")


    def _handle_file_if_complete(self, filepath):
        # Skip hidden files.
        if is_hidden(filepath):
            monitor_logger.info(f"Skipping hidden file: {filepath}")
            return

        _, extension = os.path.splitext(filepath)
        extension = extension.lower()

        # Check if this is a temporary download file that should be ignored
        if self._is_temporary_download_file(filepath, extension):
            monitor_logger.info(f"Ignoring temporary download file: {filepath}")
            return

        # If the extension is in the ignored list, ignore it—unless it's a .zip file and auto_unpack is enabled.
        if extension in self.ignored_extensions:
            if extension == '.zip' and getattr(self, 'auto_unpack', False):
                monitor_logger.info(f"Zip file detected with auto_unpack enabled: {filepath}")
            else:
                monitor_logger.info(f"Ignoring file with extension '{extension}': {filepath}")
                return

        if self._is_download_complete(filepath):
            self._process_file(filepath)
            monitor_logger.info(f"File Download Complete: {filepath}")
        else:
            monitor_logger.info(f"File not yet complete: {filepath}")

    def _is_temporary_download_file(self, filepath, extension):
        """
        Check if a file is a temporary download file that should be ignored.
        This includes files with multiple extensions like .zip.0.crdownload
        """
        filename = os.path.basename(filepath)
        
        # Check for common temporary download patterns
        temp_patterns = [
            '.crdownload', '.tmp', '.part', '.mega', '.bak',
            '.download', '.downloading', '.incomplete'
        ]
        
        # Check if the filename contains any temporary patterns
        for pattern in temp_patterns:
            if pattern in filename.lower():
                return True
        
        # Check for numbered temporary files (e.g., .0, .1, .2)
        if re.search(r'\.\d+\.(crdownload|tmp|part|download)$', filename.lower()):
            return True
        
        # Check for files that look like incomplete downloads
        if re.search(r'\.(crdownload|tmp|part|download)$', filename.lower()):
            return True
            
        return False

    def cleanup_orphan_files(self):
        """
        Clean up orphan temporary files in the WATCH directory.
        This should be called periodically or on startup.
        """
        try:
            monitor_logger.info(f"Starting cleanup of orphan files in: {self.directory}")
            
            if not os.path.exists(self.directory):
                monitor_logger.info("Watch directory does not exist, skipping cleanup")
                return
            
            cleaned_count = 0
            total_size_cleaned = 0
            
            for root, dirs, files in os.walk(self.directory):
                # Skip hidden directories
                dirs[:] = [d for d in dirs if not is_hidden(os.path.join(root, d))]
                
                for file in files:
                    file_path = os.path.join(root, file)
                    
                    # Skip hidden files
                    if is_hidden(file_path):
                        continue
                    
                    # Check if this is a temporary download file
                    _, extension = os.path.splitext(file)
                    if self._is_temporary_download_file(file_path, extension):
                        try:
                            file_size = os.path.getsize(file_path)
                            os.remove(file_path)
                            cleaned_count += 1
                            total_size_cleaned += file_size
                            monitor_logger.info(f"Cleaned up orphan file: {file_path} ({format_size(file_size)})")
                        except Exception as e:
                            monitor_logger.error(f"Error cleaning up orphan file {file_path}: {e}")
            
            if cleaned_count > 0:
                monitor_logger.info(f"Cleanup completed: {cleaned_count} files removed, {format_size(total_size_cleaned)} freed")
            else:
                monitor_logger.info("No orphan files found during cleanup")
                
        except Exception as e:
            monitor_logger.error(f"Error during orphan file cleanup: {e}")


    def _rename_file(self, filepath):
        # Skip renaming if the file is hidden.
        if is_hidden(filepath):
            monitor_logger.info(f"Skipping renaming for hidden file: {filepath}")
            return None

        try:
            new_filepath = rename_file(filepath)
            if new_filepath:
                monitor_logger.info(f"Renamed File: {new_filepath}")
            return new_filepath
        except Exception as e:
            monitor_logger.info(f"Error renaming file {filepath}: {e}")
            return None


    def _process_file(self, filepath):
        try:
            monitor_logger.info(f"Processing file: {filepath}")
            
            # Check if the file is a zip file
            if filepath.lower().endswith('.zip'):
                if self.auto_unpack:
                    monitor_logger.info(f"Zip file detected and auto_unpack is enabled. Unzipping: {filepath}")
                    self.unzip_file(filepath)
                    return  # Exit after unzipping
                else:
                    monitor_logger.info(f"Zip file detected, but auto_unpack is disabled. Processing as normal file: {filepath}")
            
            # Continue with the normal processing for non-zip files (or zip files when auto_unpack is disabled)
            renamed_filepath = self._rename_file(filepath)
            if not renamed_filepath or renamed_filepath == filepath:
                monitor_logger.info(f"No rename needed for: {filepath}")
                self._move_file(filepath)
            else:
                monitor_logger.info(f"Renamed file: {renamed_filepath}")
                self._move_file(renamed_filepath)
                    
        except Exception as e:
            monitor_logger.info(f"Error processing {filepath}: {e}")


    def _move_file(self, filepath):
        """
        Moves the file from its source location to the target directory,
        ensuring the move is completed before proceeding with conversion.
        If move_directories is True, the file is renamed based on its original
        sub-directory structure (flattening the hierarchy).
        """

        if not os.path.exists(filepath):
            monitor_logger.info(f"File not found for moving: {filepath}")
            return

        # Skip moving hidden files.
        if is_hidden(filepath):
            monitor_logger.info(f"Skipping moving hidden file: {filepath}")
            return

        # Wait for file download completion.
        monitor_logger.info(f"Waiting for '{filepath}' to finish downloading before moving...")
        if not _wait_for_download_completion(filepath):
            monitor_logger.warning(f"File not yet complete: {filepath}")
            return  # Exit early; do not move an incomplete file

        target_path = None

        # Consolidate single-file directories into a series folder
        if self.consolidate_directories:
            source_dir = os.path.dirname(filepath)
            watch_dir = os.path.abspath(self.directory)
            abs_source_dir = os.path.abspath(source_dir)

            # Only consolidate if file is in a subdirectory of the watch folder
            if abs_source_dir != watch_dir:
                # Cache the original file count when we first see this directory
                if abs_source_dir not in self._initial_dir_file_counts:
                    try:
                        dir_files = [f for f in os.listdir(source_dir)
                                    if os.path.isfile(os.path.join(source_dir, f))]
                        self._initial_dir_file_counts[abs_source_dir] = len(dir_files)
                    except Exception:
                        self._initial_dir_file_counts[abs_source_dir] = 0

                original_count = self._initial_dir_file_counts.get(abs_source_dir, 0)

                if original_count == 1:
                    # Derive series name from directory name
                    dir_name = os.path.basename(abs_source_dir)
                    series_name = re.sub(r'\s*\([^)]*\)', '', dir_name)  # Strip parenthetical groups
                    series_name = re.sub(r'\s+\d+\s*$', '', series_name)  # Strip trailing issue number
                    series_name = series_name.strip()

                    if series_name:
                        filename = os.path.basename(filepath)
                        target_path = os.path.join(self.target_directory, series_name, filename)
                        monitor_logger.info(
                            f"Consolidating: '{dir_name}' -> series folder '{series_name}'"
                        )

        if target_path is None:
            if self.move_directories:
                # Calculate the relative path from the source directory.
                rel_path = os.path.relpath(filepath, self.directory)
                # Build the target path preserving the sub-directory structure.
                target_path = os.path.join(self.target_directory, rel_path)
            else:
                # If not moving directories, keep the original filename.
                filename = os.path.basename(filepath)
                target_path = os.path.join(self.target_directory, filename)

        # Apply cleaning to the directory portion of target_path
        # This cleans the folder names (as per our directory cleaning rules)
        target_dir = os.path.dirname(target_path)
        cleaned_target_dir = clean_directory_name(target_dir)
        target_path = os.path.join(cleaned_target_dir, os.path.basename(target_path))

        try:
            # Ensure that the target sub-directory exists.
            os.makedirs(os.path.dirname(target_path), exist_ok=True)

            # Check if target file already exists and generate unique filename if needed
            original_target_path = target_path
            target_path = get_unique_filepath(target_path)

            if target_path != original_target_path:
                monitor_logger.warning(f"File already exists at destination. Using unique filename to prevent overwrite.")
                monitor_logger.info(f"Original target: {original_target_path}")
                monitor_logger.info(f"New target: {target_path}")

            shutil.move(filepath, target_path)
            monitor_logger.info(f"Moved file to: {target_path}")

            # Allow filesystem update
            time.sleep(1)

            # Track the final file path (may change after conversion)
            final_target_path = target_path

            if os.path.exists(target_path):
                monitor_logger.info(f"Checking if '{target_path}' is a CBR file")
                if target_path.lower().endswith('.cbr'):
                    if self.autoconvert:
                        monitor_logger.info(f"Sending Convert Request for '{target_path}'")
                        retries = 3
                        for _ in range(retries):
                            if os.path.exists(target_path):
                                break
                            time.sleep(0.5)
                        try:
                            convert_to_cbz(target_path)
                            # After conversion, the file will be .cbz
                            final_target_path = os.path.splitext(target_path)[0] + '.cbz'
                            monitor_logger.info(f"Converted to: {final_target_path}")
                        except Exception as e:
                            monitor_logger.error(f"Conversion failed for '{target_path}': {e}")
                            # If conversion failed, keep the original .cbr path
                    else:
                        monitor_logger.info("Auto-conversion is disabled.")
                else:
                    monitor_logger.info(f"File '{target_path}' is not a CBR file. No conversion needed.")
            else:
                monitor_logger.warning(f"File move verification failed: {target_path} not found.")

        except Exception as e:
            monitor_logger.error(f"Error moving file: {e}")
            # Allow filesystem update
            time.sleep(1)

        # Remove empty directories along the processed file's source path,
        # but only those in the chain up to the main watch folder.
        source_folder = os.path.dirname(filepath)
        watch_dir = os.path.abspath(self.directory)
        current_dir = os.path.abspath(source_folder)

        while current_dir != watch_dir:
            # Do not attempt to remove hidden directories.
            if is_hidden(current_dir):
                break
            try:
                # Only remove the directory if it's empty.
                if not os.listdir(current_dir):
                    os.rmdir(current_dir)
                    monitor_logger.info(f"Deleted empty sub-directory: {current_dir}")
                    self._initial_dir_file_counts.pop(os.path.abspath(current_dir), None)
                else:
                    # Stop if the directory contains any files or non-empty folders.
                    break
            except Exception as e:
                monitor_logger.error(f"Error removing directory {current_dir}: {e}")
                break
            # Move one level up in the directory hierarchy.
            current_dir = os.path.dirname(current_dir)


    def _is_download_complete(self, filepath):
        try:
            initial_size = os.path.getsize(filepath)
            time.sleep(5)  # Adjust sleep time as needed
            final_size = os.path.getsize(filepath)
            return initial_size == final_size
        except (PermissionError, FileNotFoundError):
            return False


def _wait_for_download_completion(filepath, wait_time=2.0, retries=20):
    """
    Waits until a file is fully downloaded before proceeding.
    - Checks for stable file size.
    - Ensures any ".part" or ".tmp" files are gone.
    - Retries several times with a delay before confirming the file is complete.
    """
    if not os.path.exists(filepath):
        return False

    previous_size = -1
    for _ in range(retries):
        if not os.path.exists(filepath):  # Ensure file hasn't disappeared
            return False

        current_size = os.path.getsize(filepath)
        if current_size == previous_size:
            return True  # File size is stable, assume complete

        previous_size = current_size
        time.sleep(wait_time)

    return False

def format_size(size_bytes):
    """Helper function to format file sizes in human-readable format"""
    if size_bytes == 0:
        return "0B"

    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_names[i]}"

def get_unique_filepath(target_path):
    """
    Generate a unique filepath by appending (1), (2), etc. if the file already exists.
    This prevents files from being overwritten during the move operation.

    Args:
        target_path: The desired target path for the file

    Returns:
        A unique filepath that doesn't exist yet
    """
    if not os.path.exists(target_path):
        return target_path

    # Split the path into directory, filename, and extension
    directory = os.path.dirname(target_path)
    filename = os.path.basename(target_path)
    name, ext = os.path.splitext(filename)

    # Keep incrementing the counter until we find a unique name
    counter = 1
    while True:
        new_name = f"{name} ({counter}){ext}"
        new_path = os.path.join(directory, new_name)
        if not os.path.exists(new_path):
            return new_path
        counter += 1


if __name__ == "__main__":
    os.makedirs(directory, exist_ok=True)

    event_handler = DownloadCompleteHandler(
        directory=directory,
        target_directory=target_directory,
        ignored_extensions=ignored_extensions
    )

    # Initial cleanup of orphan files in target directory
    if auto_cleanup:
        monitor_logger.info("Performing initial cleanup of orphan files...")
        event_handler.cleanup_orphan_files()
    else:
        monitor_logger.info("Auto cleanup disabled, skipping initial cleanup")

    # Initial scan
    for root, _, files in os.walk(directory):
        for file in files:
            filepath = os.path.join(root, file)
            monitor_logger.info(f"Initial startup scan for: {filepath}")
            event_handler._handle_file_if_complete(filepath)

    observer = PollingObserver(timeout=30)
    observer.schedule(event_handler, directory, recursive=subdirectories)
    observer.start()

    # Set up periodic cleanup (every hour)
    last_cleanup_time = time.time()
    cleanup_interval = cleanup_interval_hours * 3600  # Convert hours to seconds

    try:
        while True:
            time.sleep(1)
            
            # Check if it's time for periodic cleanup (only if auto_cleanup is enabled)
            if auto_cleanup:
                current_time = time.time()
                if current_time - last_cleanup_time >= cleanup_interval:
                    monitor_logger.info("Running periodic cleanup of orphan files...")
                    event_handler.cleanup_orphan_files()
                    last_cleanup_time = current_time
                
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
