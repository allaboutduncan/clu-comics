import os
import sys
import subprocess
import zipfile
import shutil
import time
from core.app_logging import app_logger
from core.thumbnail_cache import regenerate_thumbnail
from core.config import config, load_config
from helpers import extract_rar_with_unar, open_zip_for_write, describe_archive_error

load_config()


def _flatten_single_wrapper_dir(extraction_dir):
    """
    If extraction produced a single top-level directory containing all files,
    move its contents up to extraction_dir so the CBZ archive has no wrapper folder.
    This preserves ComicInfo.xml at the archive root where comic readers expect it.
    """
    entries = os.listdir(extraction_dir)
    if len(entries) == 1:
        single_entry = os.path.join(extraction_dir, entries[0])
        if os.path.isdir(single_entry):
            for item in os.listdir(single_entry):
                src = os.path.join(single_entry, item)
                dst = os.path.join(extraction_dir, item)
                shutil.move(src, dst)
            os.rmdir(single_entry)


# Large file threshold (configurable)
LARGE_FILE_THRESHOLD = config.getint("SETTINGS", "LARGE_FILE_THRESHOLD", fallback=500) * 1024 * 1024  # Convert MB to bytes


def get_file_size_mb(file_path):
    """Get file size in MB."""
    try:
        size_bytes = os.path.getsize(file_path)
        return size_bytes / (1024 * 1024)
    except OSError:
        return 0


def convert_single_rar_file(rar_path, cbz_path, temp_extraction_dir,
                            problem_source=None):
    """
    Convert a single RAR file to CBZ with progress reporting.

    :param rar_path: Path to the RAR file
    :param cbz_path: Path for the output CBZ file
    :param temp_extraction_dir: Temporary directory for extraction
    :param problem_source: When set, a failure is recorded against *rar_path*
        under this :mod:`core.problem_files` source, with the real exception so
        the page can classify it. Left unset by the rebuild fallback, which
        records its own row against the ``.cbz`` path it started from.
    :return: bool: True if conversion was successful
    """
    file_size_mb = get_file_size_mb(rar_path)
    is_large_file = file_size_mb > (LARGE_FILE_THRESHOLD / (1024 * 1024))
    
    if is_large_file:
        app_logger.info(f"Processing large file ({file_size_mb:.1f}MB): {os.path.basename(rar_path)}")
        app_logger.info("This may take several minutes. Progress updates will be provided.")
    
    try:
        # Create temp directory
        os.makedirs(temp_extraction_dir, exist_ok=True)
        
        # Step 1: Extract RAR file
        app_logger.info(f"Step 1/3: Extracting {os.path.basename(rar_path)}...")
        extraction_success, failed_count = extract_rar_with_unar(rar_path, temp_extraction_dir)

        if not extraction_success:
            app_logger.error(f"Failed to extract any files from {os.path.basename(rar_path)}")
            _record_convert_problem(
                rar_path, problem_source,
                error_message=(
                    f"Nothing could be extracted from "
                    f"{os.path.basename(rar_path)}"
                ),
            )
            return False

        if failed_count > 0:
            app_logger.warning(f"Partial extraction: {failed_count} file(s) skipped in {os.path.basename(rar_path)}")
            try:
                from core.app_state import add_notification
                add_notification(f"{os.path.basename(rar_path)}: {failed_count} file(s) could not be extracted (corrupt archive)")
            except Exception:
                pass

        # Flatten wrapper directory so ComicInfo.xml stays at archive root
        _flatten_single_wrapper_dir(temp_extraction_dir)

        # Step 2: Count extracted files for progress tracking
        extracted_files = []
        for root, dirs, files in os.walk(temp_extraction_dir):
            for file in files:
                file_path = os.path.join(root, file)
                extracted_files.append(file_path)
        
        total_files = len(extracted_files)
        app_logger.info(f"Step 2/3: Found {total_files} files to compress...")
        
        # Step 3: Create CBZ file with progress reporting
        app_logger.info(f"Step 3/3: Creating CBZ file...")
        processed_files = 0
        
        with open_zip_for_write(cbz_path) as zf:
            for extract_root, extract_dirs, extract_files in os.walk(temp_extraction_dir):
                for extract_file in extract_files:
                    file_path_inner = os.path.join(extract_root, extract_file)
                    arcname = os.path.relpath(file_path_inner, temp_extraction_dir)

                    # Create ZipInfo manually to control the timestamp
                    # ZIP format requires dates >= 1980-01-01
                    zip_info = zipfile.ZipInfo(filename=arcname)
                    zip_info.compress_type = zipfile.ZIP_DEFLATED

                    # Get file stats
                    file_stat = os.stat(file_path_inner)
                    file_time = time.localtime(file_stat.st_mtime)

                    # Check if timestamp is before 1980
                    if file_time.tm_year < 1980:
                        # Use a safe default timestamp: 1980-01-01 00:00:00
                        zip_info.date_time = (1980, 1, 1, 0, 0, 0)
                    else:
                        zip_info.date_time = file_time[:6]

                    # Write file with controlled timestamp
                    with open(file_path_inner, 'rb') as f:
                        zf.writestr(zip_info, f.read())

                    processed_files += 1

                    # Progress reporting for large files
                    if is_large_file and processed_files % max(1, total_files // 10) == 0:
                        progress_percent = (processed_files / total_files) * 100
                        app_logger.info(f"Compression progress: {progress_percent:.1f}% ({processed_files}/{total_files} files)")

        # Permissions are matched by open_zip_for_write when the archive is moved
        # into place.

        app_logger.info(f"Successfully converted: {os.path.basename(rar_path)}")

        regenerate_thumbnail(cbz_path)
        
        return True
        
    except Exception as e:
        app_logger.error(f"Failed to convert {os.path.basename(rar_path)}: {e}")
        _record_convert_problem(rar_path, problem_source, exc=e)
        return False


def _record_convert_problem(path, source, exc=None, error_class=None,
                            error_message=None):
    """Best-effort hand-off for a failed CBR/RAR conversion. Never raises.

    A no-op unless the caller named a source: the rebuild fallback records its
    own row against the ``.cbz`` it started from, and two rows for one failure
    is how they drift apart.
    """
    if not source:
        return
    try:
        from core.problem_files import CLASS_RAR_FAILED, record_problem

        if exc is None:
            error_class = error_class or CLASS_RAR_FAILED
        record_problem(
            path, source, exc=exc,
            error_class=error_class, error_message=error_message,
        )
    except Exception:
        pass


def _clear_convert_problem(rar_path):
    """Drop the conversion problem row -- this archive converted cleanly.

    Keyed on the source path, which is the key the failure was recorded under.
    The CBZ that replaces it is a genuinely different file and carries no
    history from the CBR.
    """
    try:
        from core.problem_files import SOURCE_CONVERT, clear_problem

        clear_problem(rar_path, SOURCE_CONVERT)
    except Exception:
        pass


def _record_rebuild_problem(cbz_path, exc=None, error_class=None, error_message=None):
    """Best-effort hand-off to the problem-files worklist. Never raises.

    Imported lazily: this module is also run as a subprocess by /stream, and a
    diagnostic must never be the thing that breaks the operation it describes.
    """
    try:
        from core.problem_files import SOURCE_REBUILD, record_problem

        record_problem(
            cbz_path,
            SOURCE_REBUILD,
            exc=exc,
            error_class=error_class,
            error_message=error_message,
        )
    except Exception:
        pass


def _clear_rebuild_problem(cbz_path):
    """Drop every recorded problem for this path -- the rebuild succeeded.

    Clears all sources, not just 'rebuild': a successful rebuild produces a
    genuinely different archive, so a stale thumbnail or metadata row against
    the old one is wrong too.
    """
    try:
        from core.problem_files import clear_problem

        clear_problem(cbz_path)
    except Exception:
        pass


def _restore_after_failed_rebuild(cbz_path, zip_path, folder_name):
    """Put the comic back where it was after a rebuild that could not finish.

    A rebuild renames the comic to ``.zip`` *before* extracting, then to
    ``.zip.bak`` before recompressing. A per-entry CRC error -- by far the most
    common failure on a damaged archive, and exactly what the Problem Files page
    points this operation at -- aborts mid-way, and the original code left the
    comic stranded under a name nothing in the library recognises, with a scratch
    folder of loose pages beside it.

    Best-effort and silent about its own failures: the rebuild has already
    failed, and the caller is reporting that.
    """
    bak_path = zip_path + '.bak'
    try:
        if not os.path.exists(cbz_path):
            for candidate in (zip_path, bak_path):
                if os.path.exists(candidate):
                    shutil.move(candidate, cbz_path)
                    app_logger.info(
                        f"Restored {os.path.basename(cbz_path)} after a failed rebuild"
                    )
                    break
    except Exception as e:
        app_logger.error(f"Could not restore {cbz_path} after a failed rebuild: {e}")

    try:
        if folder_name and os.path.isdir(folder_name):
            shutil.rmtree(folder_name)
    except Exception as e:
        app_logger.error(f"Could not clean up {folder_name}: {e}")


def rebuild_single_cbz_file(cbz_path):
    """
    Rebuild a single CBZ file with progress reporting.
    
    :param cbz_path: Path to the CBZ file
    :return: bool: True if rebuild was successful
    """
    file_size_mb = get_file_size_mb(cbz_path)
    is_large_file = file_size_mb > (LARGE_FILE_THRESHOLD / (1024 * 1024))
    filename = os.path.basename(cbz_path)
    base_name = os.path.splitext(filename)[0]

    # Derived before the try so the failure handlers can always put the comic
    # back -- see _restore_after_failed_rebuild.
    directory = os.path.dirname(cbz_path)
    zip_path = os.path.join(directory, base_name + '.zip')
    folder_name = os.path.join(directory, base_name + '_folder')

    if is_large_file:
        app_logger.info(f"Processing large file ({file_size_mb:.1f}MB): {filename}")
        app_logger.info("This may take several minutes. Progress updates will be provided.")
    
    try:
        # Step 1: Rename CBZ to ZIP
        app_logger.info(f"Step 1/4: Preparing {filename} for rebuild...")
        shutil.move(cbz_path, zip_path)
        
        # Step 2: Create extraction folder
        app_logger.info(f"Step 2/4: Creating extraction folder...")
        os.makedirs(folder_name, exist_ok=True)
        
        # Step 3: Extract ZIP file
        app_logger.info(f"Step 3/4: Extracting {filename}...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            file_list = zip_ref.namelist()
            total_files = len(file_list)
            extracted_files = 0
            
            for file_info in zip_ref.infolist():
                zip_ref.extract(file_info, folder_name)
                extracted_files += 1
                
                # Progress reporting for large files
                if is_large_file and extracted_files % max(1, total_files // 10) == 0:
                    progress_percent = (extracted_files / total_files) * 100
                    app_logger.info(f"Extraction progress: {progress_percent:.1f}% ({extracted_files}/{total_files} files)")
        
        # Step 4: Recompress to CBZ
        app_logger.info(f"Step 4/4: Recompressing {filename}...")
        bak_file_path = zip_path + '.bak'
        shutil.move(zip_path, bak_file_path)
        
        with open_zip_for_write(cbz_path) as zf:
            file_count = 0
            total_files = 0

            # Count total files first
            for root, _, files in os.walk(folder_name):
                total_files += len(files)

            # Compress files with progress reporting
            for root, _, files in os.walk(folder_name):
                for file in files:
                    file_path_in_folder = os.path.join(root, file)
                    arcname = os.path.relpath(file_path_in_folder, folder_name)

                    # Create ZipInfo manually to control the timestamp
                    # ZIP format requires dates >= 1980-01-01
                    zip_info = zipfile.ZipInfo(filename=arcname)
                    zip_info.compress_type = zipfile.ZIP_DEFLATED

                    # Get file stats
                    file_stat = os.stat(file_path_in_folder)
                    file_time = time.localtime(file_stat.st_mtime)

                    # Check if timestamp is before 1980
                    if file_time.tm_year < 1980:
                        # Use a safe default timestamp: 1980-01-01 00:00:00
                        zip_info.date_time = (1980, 1, 1, 0, 0, 0)
                    else:
                        zip_info.date_time = file_time[:6]

                    # Write file with controlled timestamp
                    with open(file_path_in_folder, 'rb') as f:
                        zf.writestr(zip_info, f.read())

                    file_count += 1

                    # Progress reporting for large files
                    if is_large_file and file_count % max(1, total_files // 10) == 0:
                        progress_percent = (file_count / total_files) * 100
                        app_logger.info(f"Compression progress: {progress_percent:.1f}% ({file_count}/{total_files} files)")
        
        # Clean up
        os.remove(bak_file_path)
        if os.path.exists(folder_name):
            shutil.rmtree(folder_name)

        # Permissions are matched by open_zip_for_write when the archive is moved
        # into place.

        app_logger.info(f"Successfully rebuilt: {filename}")
        
        regenerate_thumbnail(cbz_path)
        _clear_rebuild_problem(cbz_path)
        
        return True

    except zipfile.BadZipFile as e:
        # Handle the case where a .cbz file is actually a RAR file
        if "File is not a zip file" in str(e) or "BadZipFile" in str(e):
            app_logger.warning(f"Detected that {filename} is not a valid ZIP file. Attempting to rename to .rar and retry...")

            # Rename the file to .rar
            rar_file = os.path.join(directory, base_name + ".rar")
            if os.path.exists(zip_path):
                shutil.move(zip_path, rar_file)
            elif os.path.exists(cbz_path):
                shutil.move(cbz_path, rar_file)

            # Clean up any partial extraction folder
            if os.path.exists(folder_name):
                shutil.rmtree(folder_name)

            # Try to convert as RAR file
            temp_extraction_dir = os.path.join(directory, f".temp_{base_name}")
            final_cbz_path = os.path.join(directory, base_name + '.cbz')

            app_logger.info(f"Attempting to convert {base_name}.rar as RAR file...")
            success = convert_single_rar_file(rar_file, final_cbz_path, temp_extraction_dir)

            if success:
                # Delete the original RAR file
                if os.path.exists(rar_file):
                    os.remove(rar_file)
                # Clean up temp directory
                if os.path.exists(temp_extraction_dir):
                    shutil.rmtree(temp_extraction_dir)

                # Invalidate browse cache for parent directory
                from core.database import invalidate_browse_cache
                invalidate_browse_cache(directory)
                app_logger.info(f"Invalidated browse cache for: {directory}")

                app_logger.info(f"Successfully converted {filename} (was actually a RAR file)")
                _clear_rebuild_problem(cbz_path)
                return True
            else:
                app_logger.error(f"Failed to convert {base_name}.rar after renaming from {filename}")
                _record_rebuild_problem(
                    cbz_path,
                    error_class="RarConversionFailed",
                    error_message=(
                        f"{filename} is a RAR archive and could not be "
                        "converted to CBZ"
                    ),
                )
                return False
        else:
            detail = describe_archive_error(e)
            app_logger.error(f"Failed to rebuild {filename}: {detail}")
            _restore_after_failed_rebuild(cbz_path, zip_path, folder_name)
            _record_rebuild_problem(cbz_path, exc=e)
            return False

    except Exception as e:
        detail = describe_archive_error(e)
        app_logger.error(f"Failed to rebuild {filename}: {detail}")
        _restore_after_failed_rebuild(cbz_path, zip_path, folder_name)
        _record_rebuild_problem(cbz_path, exc=e)
        return False


def handle_cbz_file(file_path):
    """
    Handle the conversion of a .cbz file: unzip, rename, compress, and clean up.

    :param file_path: Path to the .cbz file.
    :return: bool: True if the rebuild succeeded.
    """
    app_logger.info(f"Handling CBZ file: {file_path}")

    if not file_path.lower().endswith('.cbz'):
        app_logger.info("Provided file is not a CBZ file.")
        return False

    success = rebuild_single_cbz_file(file_path)
    if not success:
        app_logger.error(f"Failed to rebuild CBZ file: {file_path}")
    return success


def convert_to_cbz(file_path):
    """
    Convert a single RAR or CBR file to a ZIP file.

    :param file_path: Path to the RAR or CBR file.
    :return: bool: True when the file was converted (or rebuilt, for a ``.cbz``)
        and the source archive has been removed.

    Callers must branch on this return value and **not** on
    ``os.path.exists(<base>.cbz)``. Those are different questions: a conversion
    can write a complete CBZ and still fail afterwards, in which case the source
    ``.cbr`` is deliberately left in place. Testing the filesystem instead is how
    monitor.py logged "Converted to" one second after app.log said "Failed to
    convert", and how every download left a CBR/CBZ pair behind in TARGET with
    nothing to retry it.
    """
    app_logger.info(f"********************// Single File Conversion //********************")
    app_logger.info(f"-- Path to file: {file_path}")

    # Check if the file exists
    if not os.path.exists(file_path):
        app_logger.error(f"File does not exist: {file_path}")
        return False

    # Check if it's a .rar or .cbr file
    if file_path.lower().endswith(('.rar', '.cbr')):
        app_logger.info("Converting RAR/CBR to CBZ format")

        base_name = os.path.splitext(file_path)[0]  # Removes the extension
        parent_dir = os.path.dirname(file_path)
        base_only = os.path.splitext(os.path.basename(file_path))[0]
        # Hidden ("." prefix) so helpers.is_hidden() skips it. This dir is a
        # sibling of the file being converted, so when api.py converts a fresh
        # download it lands *inside* WATCH -- and monitor.py's recursive sweep
        # would otherwise treat each extracted page as a completed download and
        # move it to TARGET, strip-mining the conversion still in progress.
        # Same reasoning as monitor.py's ".clu_unwrap" staging root.
        temp_extraction_dir = os.path.join(parent_dir, f".temp_{base_only}")
        cbz_file_path = base_name + '.cbz'

        # Get parent directory for cache invalidation
        parent_dir = os.path.dirname(file_path)

        # Imported lazily and defensively for the same reason every other
        # problem-files hand-off is: this module is run as a subprocess by
        # /stream, and a diagnostic must never break the operation it describes.
        try:
            from core.problem_files import SOURCE_CONVERT
        except Exception:
            SOURCE_CONVERT = None

        success = convert_single_rar_file(
            file_path, cbz_file_path, temp_extraction_dir,
            problem_source=SOURCE_CONVERT,
        )

        if success:
            # Delete the original file (RAR or CBR)
            os.remove(file_path)

            # Invalidate browse cache for parent directory
            from core.database import invalidate_browse_cache, delete_file_index_entry, add_file_index_entry
            invalidate_browse_cache(parent_dir)
            app_logger.info(f"Invalidated browse cache for: {parent_dir}")

            # Update file index: remove old CBR entry and add new CBZ entry
            try:
                delete_file_index_entry(file_path)
                file_size = os.path.getsize(cbz_file_path) if os.path.exists(cbz_file_path) else None
                add_file_index_entry(
                    name=os.path.basename(cbz_file_path),
                    path=cbz_file_path,
                    entry_type='file',
                    size=file_size,
                    parent=parent_dir
                )
                # This is a RENAME wearing a delete's clothes, which is why it
                # calls delete_file_index_entry and NOT forget_deleted_path:
                # the reading-list mappings must survive the conversion and be
                # re-pointed below, not cleared. Everything else keyed on the
                # raw path needs following too -- without this, converting a
                # comic silently discards the reader's saved position.
                from core.database import move_path_references
                move_path_references(file_path, cbz_file_path)
                app_logger.info(f"Updated file index: removed CBR, added CBZ")
            except Exception as index_error:
                app_logger.warning(f"Failed to update file index: {index_error}")

            _clear_convert_problem(file_path)
        else:
            app_logger.error(f"Failed to convert {file_path}")

        # Clean up temporary extraction directory
        if os.path.exists(temp_extraction_dir):
            try:
                shutil.rmtree(temp_extraction_dir)
                app_logger.info(f"Cleaned up temporary directory: {temp_extraction_dir}")
            except Exception as cleanup_error:
                app_logger.error(f"Failed to clean up temporary directory {temp_extraction_dir}: {cleanup_error}")

        return success

    # Check if it's a .cbz file
    elif file_path.lower().endswith('.cbz'):
        return handle_cbz_file(file_path)

    else:
        app_logger.info("File is not a recognized .rar, .cbr, or .cbz file.")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        app_logger.error("No file provided!")
    else:
        file_path = sys.argv[1]
        convert_to_cbz(file_path)
