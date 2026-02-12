"""
Files Blueprint

Provides routes for file operations:
- Rename files and directories
- Delete files (single and bulk)
- Move files and folders
- Crop images (left, right, center, freeform)
- Combine CBZ files
- Upload files to folders
- Create folders
- Cleanup orphan files
- Check missing files
"""

import os
import re
import shutil
import time
import threading
import zipfile
from flask import Blueprint, request, jsonify, render_template_string, Response, stream_with_context
from app_logging import app_logger
from helpers.library import is_critical_path, get_critical_path_error_message, is_valid_library_path
from helpers import is_hidden
from config import config
from cbz_ops.edit import cropCenter, cropLeft, cropRight, cropFreeForm, get_image_data_url, modal_body_template
from memory_utils import memory_context

files_bp = Blueprint('files', __name__)


# =============================================================================
# Move
# =============================================================================

@files_bp.route('/move', methods=['POST'])
def move():
    """
    Move a file or folder from the source path to the destination.
    If the "X-Stream" header is true, streams progress updates as SSE.
    """
    from app import auto_fetch_metron_metadata, auto_fetch_comicvine_metadata, log_file_if_in_data, update_index_on_move

    data = request.get_json()
    source = data.get('source')
    destination = data.get('destination')
    stream = request.headers.get('X-Stream', 'false').lower() == 'true'

    app_logger.info("********************// Move File //********************")
    app_logger.info(f"Requested move from: {source} to: {destination}")
    app_logger.info(f"Streaming mode: {stream}")

    if not source or not destination:
        app_logger.error("Missing source or destination in request")
        return jsonify({"success": False, "error": "Missing source or destination"}), 400

    if not os.path.exists(source):
        app_logger.warning(f"Source path does not exist: {source}")
        return jsonify({"success": False, "error": "Source path does not exist"}), 404

    # Check if trying to move critical folders
    if is_critical_path(source):
        app_logger.error(f"Attempted to move critical folder: {source}")
        return jsonify({"success": False, "error": get_critical_path_error_message(source, "move")}), 403

    # Check if destination would overwrite critical folders
    if is_critical_path(destination):
        app_logger.error(f"Attempted to move to critical folder location: {destination}")
        return jsonify({"success": False, "error": get_critical_path_error_message(destination, "move to")}), 403

    # Prevent moving a directory into itself or its subdirectories
    if os.path.isdir(source):
        # Normalize paths for comparison
        source_normalized = os.path.normpath(source)
        destination_normalized = os.path.normpath(destination)

        # Check if destination is the same as source or a subdirectory of source
        if (destination_normalized == source_normalized or
            destination_normalized.startswith(source_normalized + os.sep)):
            app_logger.error(f"Attempted to move directory into itself: {source} -> {destination}")
            return jsonify({"success": False, "error": "Cannot move a directory into itself or its subdirectories"}), 400

    if stream:
        app_logger.info(f"Starting streaming move operation")
        # Streaming move for both files and directories
        if os.path.isfile(source):
            file_size = os.path.getsize(source)

            # Use memory context for large file operations
            cleanup_threshold = 1000 if file_size > 100 * 1024 * 1024 else 500  # 100MB threshold

            def generate():
                with memory_context("file_move", cleanup_threshold):
                    bytes_copied = 0
                    chunk_size = 1024 * 1024  # 1 MB
                    try:
                        app_logger.info(f"Streaming file move with progress: {source}")
                        with open(source, 'rb') as fsrc, open(destination, 'wb') as fdst:
                            while True:
                                chunk = fsrc.read(chunk_size)
                                if not chunk:
                                    break
                                fdst.write(chunk)
                                bytes_copied += len(chunk)
                                progress = int((bytes_copied / file_size) * 100)
                                yield f"data: {progress}\n\n"
                        os.remove(source)
                        app_logger.info(f"Move complete (streamed): Removed {source}")

                        # Auto-fetch metadata (try Metron first, then ComicVine as fallback)
                        final_path = auto_fetch_metron_metadata(destination)
                        # If Metron didn't process, try ComicVine
                        final_path = auto_fetch_comicvine_metadata(final_path)

                        yield "data: 100\n\n"
                    except Exception as e:
                        app_logger.exception(f"Error during streaming move from {source} to {destination}")
                        yield f"data: error: {str(e)}\n\n"
                    yield "data: done\n\n"
        else:
            # Streaming move for directories
            def generate():
                with memory_context("file_move"):
                    try:
                        app_logger.info(f"Streaming directory move with progress: {source}")

                        # Calculate total size and file count for progress tracking
                        total_size = 0
                        file_count = 0
                        file_list = []
                        try:
                            for root, _, files in os.walk(source):
                                for file in files:
                                    file_path = os.path.join(root, file)
                                    if os.path.exists(file_path):
                                        file_size_item = os.path.getsize(file_path)
                                        total_size += file_size_item
                                        file_count += 1
                                        file_list.append((file_path, file_size_item))
                        except Exception as e:
                            app_logger.warning(f"Could not calculate directory size: {e}")

                        app_logger.info(f"Directory contains {file_count} files, total size: {total_size}")

                        if total_size == 0:
                            # Empty directory or couldn't calculate size
                            shutil.move(source, destination)
                            yield "data: 100\n\n"
                        else:
                            # Create destination directory if it doesn't exist
                            os.makedirs(os.path.dirname(destination), exist_ok=True)

                            # Copy files individually with progress tracking
                            bytes_moved = 0
                            chunk_size = 1024 * 1024  # 1 MB chunks
                            last_progress_update = time.time()
                            start_time = time.time()

                            for i, (file_path, file_size_item) in enumerate(file_list):
                                # Check for timeout every 100 files
                                if i % 100 == 0 and i > 0:
                                    elapsed = time.time() - start_time
                                    if elapsed > 3600:  # 1 hour timeout
                                        raise Exception(f"Directory move operation timed out after {elapsed:.0f} seconds")

                                # Calculate relative path from source
                                rel_path = os.path.relpath(file_path, source)
                                dest_file_path = os.path.join(destination, rel_path)

                                # Create destination directory structure
                                os.makedirs(os.path.dirname(dest_file_path), exist_ok=True)

                                # Copy file with progress updates
                                try:
                                    with open(file_path, 'rb') as fsrc, open(dest_file_path, 'wb') as fdst:
                                        while True:
                                            chunk = fsrc.read(chunk_size)
                                            if not chunk:
                                                break
                                            fdst.write(chunk)
                                            bytes_moved += len(chunk)

                                            # Calculate overall progress
                                            progress = int((bytes_moved / total_size) * 100)
                                            current_time = time.time()

                                            # Send progress update every 2 seconds or when progress changes significantly
                                            if (current_time - last_progress_update > 2.0 or
                                                progress % 5 == 0):
                                                yield f"data: {progress}\n\n"
                                                last_progress_update = current_time
                                except Exception as e:
                                    app_logger.error(f"Error copying file {file_path}: {e}")
                                    # Try to continue with other files
                                    continue

                                # Send keepalive every 10 files to prevent connection timeout
                                if i % 10 == 0:
                                    yield f"data: keepalive: {i+1}/{file_count} files processed\n\n"

                                # Update status every few files
                                if i % 10 == 0 or i == len(file_list) - 1:
                                    app_logger.info(f"Copied {i+1}/{file_count} files ({bytes_moved}/{total_size} bytes)")

                            # Remove source directory after successful copy
                            try:
                                shutil.rmtree(source)
                            except Exception as e:
                                app_logger.warning(f"Could not remove source directory {source}: {e}")
                                # Continue anyway since files were copied successfully

                            yield "data: 100\n\n"

                        app_logger.info(f"Directory move complete: {source} -> {destination}")

                        # Auto-fetch metadata (try Metron first, then ComicVine as fallback)
                        auto_fetch_metron_metadata(destination)
                        auto_fetch_comicvine_metadata(destination)

                        # Log all comic files in the moved directory to recent_files
                        try:
                            for root, _, files_in_dir in os.walk(destination):
                                for file in files_in_dir:
                                    file_path = os.path.join(root, file)
                                    log_file_if_in_data(file_path)
                        except Exception as e:
                            app_logger.warning(f"Error logging files from directory {destination}: {e}")

                        # Update file index incrementally (no cache invalidation needed with DB-first approach)
                        update_index_on_move(source, destination)

                    except Exception as e:
                        app_logger.exception(f"Error during streaming directory move from {source} to {destination}")
                        yield f"data: error: {str(e)}\n\n"

                    yield "data: done\n\n"

        headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive"
        }
        return Response(stream_with_context(generate()), headers=headers)

    else:
        # Non-streaming move for folders or when streaming is disabled
        with memory_context("file_move"):
            try:
                is_file = os.path.isfile(source)

                if is_file:
                    shutil.move(source, destination)
                else:
                    shutil.move(source, destination)
                app_logger.info(f"Move complete: {source} -> {destination}")

                # Auto-fetch metadata (try Metron first, then ComicVine as fallback)
                final_path = auto_fetch_metron_metadata(destination)
                # If Metron didn't process, try ComicVine
                final_path = auto_fetch_comicvine_metadata(final_path)

                # Log file to recent_files with the final path (renamed or original)
                if is_file:
                    log_file_if_in_data(final_path)
                else:
                    # For directories, log all comic files inside (after any renames)
                    try:
                        for root, _, files in os.walk(destination):
                            for file in files:
                                file_path = os.path.join(root, file)
                                log_file_if_in_data(file_path)
                    except Exception as e:
                        app_logger.warning(f"Error logging files from directory {destination}: {e}")

                # Update file index incrementally (no cache invalidation needed with DB-first approach)
                update_index_on_move(source, final_path if is_file else destination)

                return jsonify({"success": True})
            except Exception as e:
                app_logger.error(f"Error moving {source} to {destination}: {e}")
                return jsonify({"success": False, "error": str(e)}), 500


# =============================================================================
# Folder Size
# =============================================================================

@files_bp.route('/folder-size', methods=['GET'])
def folder_size():
    path = request.args.get('path')
    if not path or not os.path.exists(path):
        return jsonify({"error": "Invalid path"}), 400

    def get_directory_stats(path):
        total_size = 0
        comic_count = 0
        magazine_count = 0
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    fp = os.path.join(root, f)
                    if os.path.exists(fp):
                        total_size += os.path.getsize(fp)
                        ext = f.lower()
                        if ext.endswith(('.cbz', '.cbr', '.zip')):
                            comic_count += 1
                        elif ext.endswith('.pdf'):
                            magazine_count += 1
                except Exception:
                    pass
        return total_size, comic_count, magazine_count

    size, comic_count, magazine_count = get_directory_stats(path)
    return jsonify({
        "size": size,
        "comic_count": comic_count,
        "magazine_count": magazine_count
    })


# =============================================================================
# Upload
# =============================================================================

@files_bp.route('/upload-to-folder', methods=['POST'])
def upload_to_folder():
    """
    Upload files to a specific folder.
    Accepts multiple files and a target directory path.
    Only allows image files, CBZ, and CBR files.
    """
    from app import log_file_if_in_data, resize_upload

    try:
        # Get target directory from form data
        target_dir = request.form.get('target_dir')

        if not target_dir:
            return jsonify({"success": False, "error": "No target directory specified"}), 400

        # Validate target directory exists
        if not os.path.exists(target_dir):
            return jsonify({"success": False, "error": "Target directory does not exist"}), 404

        if not os.path.isdir(target_dir):
            return jsonify({"success": False, "error": "Target path is not a directory"}), 400

        # Check if files were uploaded
        if 'files' not in request.files:
            return jsonify({"success": False, "error": "No files provided"}), 400

        files = request.files.getlist('files')

        if not files or all(f.filename == '' for f in files):
            return jsonify({"success": False, "error": "No files selected"}), 400

        # Allowed file extensions
        allowed_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.cbz', '.cbr'}

        uploaded_files = []
        skipped_files = []
        errors = []

        for file in files:
            if file.filename == '':
                continue

            # Sanitize filename: strip path separators but preserve spaces
            filename = os.path.basename(file.filename)
            filename = filename.lstrip('.')  # Remove leading dots
            if not filename:
                skipped_files.append({'name': file.filename, 'reason': 'Invalid filename'})
                continue
            file_ext = os.path.splitext(filename)[1].lower()

            # Validate file type
            if file_ext not in allowed_extensions:
                skipped_files.append({
                    'name': filename,
                    'reason': f'File type not allowed ({file_ext})'
                })
                continue

            # Construct full path
            file_path = os.path.join(target_dir, filename)

            # Check if file already exists
            if os.path.exists(file_path):
                # Add a number to make it unique
                base_name, ext = os.path.splitext(filename)
                counter = 1
                while os.path.exists(os.path.join(target_dir, f"{base_name}_{counter}{ext}")):
                    counter += 1
                filename = f"{base_name}_{counter}{ext}"
                file_path = os.path.join(target_dir, filename)

            try:
                # Save the file
                file.save(file_path)

                # Resize to match existing images in directory
                # Skip resizing for 'header' and 'folder' images
                base_name_check = os.path.splitext(filename)[0].lower()
                if base_name_check not in ('header', 'folder'):
                    resize_upload(file_path, target_dir)

                file_size = os.path.getsize(file_path)  # Get size after resize

                uploaded_files.append({
                    'name': filename,
                    'path': file_path,
                    'size': file_size
                })

                # Log to recent files if it's a comic file in /data
                log_file_if_in_data(file_path)

                app_logger.info(f"Uploaded file: {filename} to {target_dir}")

            except Exception as e:
                errors.append({
                    'name': filename,
                    'error': str(e)
                })
                app_logger.error(f"Error uploading file {filename}: {e}")

        # Return results
        response = {
            "success": True,
            "uploaded": uploaded_files,
            "skipped": skipped_files,
            "errors": errors,
            "total_uploaded": len(uploaded_files),
            "total_skipped": len(skipped_files),
            "total_errors": len(errors)
        }

        return jsonify(response)

    except Exception as e:
        app_logger.error(f"Error in upload_to_folder: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# =============================================================================
# Combine CBZ
# =============================================================================

@files_bp.route('/api/combine-cbz', methods=['POST'])
def combine_cbz():
    """Combine multiple CBZ files into a single CBZ file."""
    data = request.get_json()
    files = data.get('files', [])
    output_name = data.get('output_name', 'Combined')
    directory = data.get('directory')

    if len(files) < 2:
        return jsonify({"error": "At least 2 files required"}), 400

    if not directory:
        return jsonify({"error": "Directory not specified"}), 400

    # Security: Validate all paths
    watch_dir = config.get("SETTINGS", "WATCH", fallback="/temp")
    target_dir = config.get("SETTINGS", "TARGET", fallback="/processed")

    for f in files:
        normalized = os.path.normpath(f)
        if not (is_valid_library_path(normalized) or
                normalized.startswith(os.path.normpath(watch_dir)) or
                normalized.startswith(os.path.normpath(target_dir))):
            return jsonify({"error": "Access denied"}), 403

    temp_dir = None
    try:
        # Create temp extraction directory
        temp_dir = os.path.join(directory, f'.tmp_combine_{os.getpid()}')
        os.makedirs(temp_dir, exist_ok=True)

        file_counter = {}  # Track duplicate filenames
        extracted_count = 0

        # Extract all files from each CBZ
        for cbz_path in files:
            if not os.path.exists(cbz_path):
                app_logger.warning(f"CBZ file not found, skipping: {cbz_path}")
                continue

            try:
                with zipfile.ZipFile(cbz_path, 'r') as zf:
                    for name in zf.namelist():
                        # Skip directories and metadata files
                        if name.endswith('/') or name.lower() == 'comicinfo.xml':
                            continue

                        # Get base filename (flatten nested directories)
                        base_name = os.path.basename(name)
                        if not base_name:  # Skip empty names
                            continue

                        name_part, ext = os.path.splitext(base_name)

                        # Handle duplicates: append a, b, c, etc.
                        if base_name in file_counter:
                            count = file_counter[base_name]
                            suffix = chr(ord('a') + count)
                            new_name = f"{name_part}{suffix}{ext}"
                            file_counter[base_name] += 1
                        else:
                            new_name = base_name
                            file_counter[base_name] = 1

                        # Extract to temp dir with new name
                        content = zf.read(name)
                        dest_path = os.path.join(temp_dir, new_name)
                        with open(dest_path, 'wb') as f:
                            f.write(content)
                        extracted_count += 1

            except zipfile.BadZipFile:
                app_logger.warning(f"Invalid CBZ file, skipping: {cbz_path}")
                continue

        if extracted_count == 0:
            shutil.rmtree(temp_dir)
            return jsonify({"error": "No files could be extracted from the selected CBZ files"}), 400

        # Create output CBZ
        output_path = os.path.join(directory, f"{output_name}.cbz")

        # Handle existing file - append (1), (2), etc.
        counter = 1
        while os.path.exists(output_path):
            output_path = os.path.join(directory, f"{output_name} ({counter}).cbz")
            counter += 1

        # Compress temp dir to CBZ
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            extracted_files = sorted(os.listdir(temp_dir))
            for filename in extracted_files:
                file_path_full = os.path.join(temp_dir, filename)
                zf.write(file_path_full, filename)

        # Cleanup temp directory
        shutil.rmtree(temp_dir)
        temp_dir = None

        app_logger.info(f"Combined {len(files)} CBZ files into {output_path} ({extracted_count} images)")
        return jsonify({
            "success": True,
            "output_file": os.path.basename(output_path),
            "total_images": extracted_count
        })

    except Exception as e:
        # Cleanup on error
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        app_logger.error(f"Error combining CBZ files: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Check Missing Files
# =============================================================================

@files_bp.route('/api/check-missing-files', methods=['POST'])
def check_missing_files():
    """Check for missing comic files in a folder."""
    from missing import check_missing_issues
    from app import DATA_DIR

    data = request.get_json()
    folder_path = data.get('folder_path')

    if not folder_path:
        return jsonify({"error": "Missing folder_path"}), 400

    if not os.path.exists(folder_path) or not os.path.isdir(folder_path):
        return jsonify({"error": "Invalid folder path"}), 400

    try:
        app_logger.info(f"Running missing file check on: {folder_path}")

        # Run the missing file check
        check_missing_issues(folder_path)

        # Read the missing.txt file to count missing issues
        missing_file_path = os.path.join(folder_path, "missing.txt")
        missing_count = 0
        summary_message = ""

        if os.path.exists(missing_file_path):
            with open(missing_file_path, 'r') as f:
                content = f.read()
                # Count lines that contain '.cbz' or '.cbr' to get missing issue count
                # Exclude lines that are just headers or blank
                lines = content.strip().split('\n')
                for line in lines:
                    if '.cbz' in line or '.cbr' in line:
                        missing_count += 1
                    elif '[Total missing:' in line:
                        # Extract count from condensed format
                        match = re.search(r'\[Total missing: (\d+)\]', line)
                        if match:
                            missing_count += int(match.group(1))

        if missing_count == 0:
            summary_message = "No missing issues found."
        else:
            summary_message = f"Found {missing_count} missing issue(s) in {os.path.basename(folder_path)}."

        app_logger.info(f"Missing file check complete. {summary_message}")

        # Get relative path for the missing.txt file
        relative_missing_file = os.path.relpath(missing_file_path, DATA_DIR)

        return jsonify({
            "success": True,
            "missing_count": missing_count,
            "missing_file": missing_file_path,
            "relative_missing_file": relative_missing_file,
            "folder_name": os.path.basename(folder_path),
            "summary": summary_message
        })

    except Exception as e:
        app_logger.error(f"Error checking missing files: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Rename
# =============================================================================

@files_bp.route('/rename', methods=['POST'])
def rename():
    from app import update_index_on_move

    data = request.get_json()
    old_path = data.get('old')
    new_path = data.get('new')

    app_logger.info(f"Renaming: {old_path} to {new_path}")

    # Validate input
    if not old_path or not new_path:
        return jsonify({"error": "Missing old or new path"}), 400

    # Check if the old path exists
    if not os.path.exists(old_path):
        return jsonify({"error": "Source file or directory does not exist"}), 404

    # Check if trying to rename critical folders
    if is_critical_path(old_path):
        app_logger.error(f"Attempted to rename critical folder: {old_path}")
        return jsonify({"error": get_critical_path_error_message(old_path, "rename")}), 403

    # Check if new path would be a critical folder
    if is_critical_path(new_path):
        app_logger.error(f"Attempted to rename to critical folder location: {new_path}")
        return jsonify({"error": get_critical_path_error_message(new_path, "rename to")}), 403

    # Check if the new path already exists to avoid overwriting
    # Allow case-only changes (e.g., "file.txt" -> "File.txt") on case-insensitive filesystems
    if os.path.exists(new_path):
        # Check if this is a case-only rename by checking if they're the same file
        try:
            if not os.path.samefile(old_path, new_path):
                return jsonify({"error": "Destination already exists"}), 400
        except (OSError, ValueError):
            # If samefile fails, fall back to normcase comparison
            if os.path.normcase(os.path.abspath(old_path)) != os.path.normcase(os.path.abspath(new_path)):
                return jsonify({"error": "Destination already exists"}), 400

    try:
        os.rename(old_path, new_path)

        # Update file index incrementally (no cache invalidation needed with DB-first approach)
        update_index_on_move(old_path, new_path)

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@files_bp.route('/rename-directory', methods=['POST'])
def rename_directory():
    """Rename all files in a directory using rename.py patterns"""
    try:
        data = request.get_json()
        directory_path = data.get('directory')

        app_logger.info("********************// Rename Directory Files //********************")
        app_logger.info(f"Directory: {directory_path}")

        # Validate input
        if not directory_path:
            return jsonify({"error": "Missing directory path"}), 400

        # Check if the directory exists
        if not os.path.exists(directory_path):
            return jsonify({"error": "Directory does not exist"}), 404

        if not os.path.isdir(directory_path):
            return jsonify({"error": "Path is not a directory"}), 400

        # Check if trying to rename files in critical folders
        if is_critical_path(directory_path):
            app_logger.error(f"Attempted to rename files in critical folder: {directory_path}")
            return jsonify({"error": get_critical_path_error_message(directory_path, "rename files in")}), 403

        # Import and call the rename_files function from rename.py
        from cbz_ops.rename import rename_files

        # Call the rename function
        rename_files(directory_path)

        app_logger.info(f"Successfully renamed files in directory: {directory_path}")
        return jsonify({"success": True, "message": f"Successfully renamed files in {os.path.basename(directory_path)}"})

    except ImportError as e:
        app_logger.error(f"Failed to import rename module: {e}")
        return jsonify({"error": "Rename module not available"}), 500
    except Exception as e:
        app_logger.error(f"Error renaming files in directory {directory_path}: {e}")
        return jsonify({"error": str(e)}), 500


@files_bp.route('/custom-rename', methods=['POST'])
def custom_rename():
    """
    Custom rename route that handles bulk renaming operations
    specifically for removing text from filenames.
    """
    from app import update_index_on_move

    data = request.get_json()
    old_path = data.get('old')
    new_path = data.get('new')

    app_logger.info(f"Custom rename request: {old_path} -> {new_path}")

    # Validate input
    if not old_path or not new_path:
        return jsonify({"error": "Missing old or new path"}), 400

    # Check if the old path exists
    if not os.path.exists(old_path):
        return jsonify({"error": "Source file does not exist"}), 404

    # Check if trying to rename critical folders
    if is_critical_path(old_path):
        app_logger.error(f"Attempted to rename critical folder: {old_path}")
        return jsonify({"error": get_critical_path_error_message(old_path, "rename")}), 403

    # Check if new path would be a critical folder
    if is_critical_path(new_path):
        app_logger.error(f"Attempted to rename to critical folder location: {new_path}")
        return jsonify({"error": get_critical_path_error_message(new_path, "rename to")}), 403

    # Check if the new path already exists to avoid overwriting
    if os.path.exists(new_path):
        return jsonify({"error": "Destination already exists"}), 400

    try:
        os.rename(old_path, new_path)

        # Update file index incrementally (no cache invalidation needed with DB-first approach)
        update_index_on_move(old_path, new_path)

        app_logger.info(f"Custom rename successful: {old_path} -> {new_path}")
        return jsonify({"success": True})
    except Exception as e:
        app_logger.error(f"Error in custom rename: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Crop
# =============================================================================

@files_bp.route('/crop', methods=['POST'])
def crop_image():
    try:
        data = request.json
        file_path = data.get('target')
        crop_type = data.get('cropType')
        app_logger.info("********************// Crop Image //********************")
        app_logger.info(f"File Path: {file_path}")
        app_logger.info(f"Crop Type: {crop_type}")

        # Validate input
        if not file_path or not crop_type:
            return jsonify({'success': False, 'error': 'Missing file path or crop type'}), 400

        file_cards = []

        if crop_type == 'left':
            new_image_path, backup_path = cropLeft(file_path)
            for path in [new_image_path, backup_path]:
                file_cards.append({
                    "filename": os.path.basename(path),
                    "rel_path": path,
                    "img_data": get_image_data_url(path)
                })

        elif crop_type == 'right':
            new_image_path, backup_path = cropRight(file_path)
            for path in [new_image_path, backup_path]:
                file_cards.append({
                    "filename": os.path.basename(path),
                    "rel_path": path,
                    "img_data": get_image_data_url(path)
                })

        elif crop_type == 'center':
            result = cropCenter(file_path)
            for key, path in result.items():
                file_cards.append({
                    "filename": os.path.basename(path),
                    "rel_path": path,
                    "img_data": get_image_data_url(path)
                })
        else:
            return jsonify({'success': False, 'error': 'Invalid crop type'}), 400

        # Render the cards as HTML

        modal_card_html = render_template_string(modal_body_template, file_cards=file_cards)

        return jsonify({
            'success': True,
            'html': modal_card_html,
            'message': f'{crop_type.capitalize()} crop completed.',
        })

    except Exception as e:
        app_logger.error(f"Crop error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@files_bp.route('/get-image-data', methods=['POST'])
def get_full_image_data():
    """Get full-size image data as base64 for display in modal"""
    try:
        data = request.json
        file_path = data.get('target')

        if not file_path:
            return jsonify({'success': False, 'error': 'Missing file path'}), 400

        if not os.path.exists(file_path):
            return jsonify({'success': False, 'error': 'File not found'}), 404

        # Read the image and encode as base64
        from PIL import Image
        import io
        import base64

        with Image.open(file_path) as img:
            # Convert to RGB if necessary
            if img.mode in ('RGBA', 'LA', 'P'):
                rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                rgb_img.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                img = rgb_img

            # Encode as JPEG
            buffered = io.BytesIO()
            img.save(buffered, format="JPEG", quality=95)
            encoded = base64.b64encode(buffered.getvalue()).decode('utf-8')
            image_data = f"data:image/jpeg;base64,{encoded}"

        return jsonify({
            'success': True,
            'imageData': image_data
        })

    except Exception as e:
        app_logger.error(f"Error getting image data: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@files_bp.route('/crop-freeform', methods=['POST'])
def crop_image_freeform():
    """Handle free form crop with custom coordinates"""
    try:
        data = request.json
        file_path = data.get('target')
        x = data.get('x')
        y = data.get('y')
        width = data.get('width')
        height = data.get('height')

        app_logger.info("********************// Free Form Crop Image //********************")
        app_logger.info(f"File Path: {file_path}")
        app_logger.info(f"Crop coords: x={x}, y={y}, width={width}, height={height}")

        # Validate input
        if not file_path or x is None or y is None or width is None or height is None:
            return jsonify({'success': False, 'error': 'Missing file path or crop coordinates'}), 400

        # Perform the crop
        new_image_path, backup_path = cropFreeForm(file_path, x, y, width, height)

        # Return the updated image data and backup image data
        return jsonify({
            'success': True,
            'newImagePath': new_image_path,
            'newImageData': get_image_data_url(new_image_path),
            'backupImagePath': backup_path,
            'backupImageData': get_image_data_url(backup_path),
            'message': 'Free form crop completed.'
        })

    except Exception as e:
        app_logger.error(f"Free form crop error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================================================
# Delete
# =============================================================================

@files_bp.route('/delete', methods=['POST'])
def delete():
    from app import update_index_on_delete

    data = request.get_json()
    target = data.get('target')
    if not target:
        return jsonify({"error": "Missing target path"}), 400
    if not os.path.exists(target):
        return jsonify({"error": "Target does not exist"}), 404

    # Check if trying to delete critical folders
    if is_critical_path(target):
        app_logger.error(f"Attempted to delete critical folder: {target}")
        return jsonify({"error": get_critical_path_error_message(target, "delete")}), 403

    try:
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)

        # Update file index in background — skip for temp extraction folders (never indexed)
        if '/.tmp_extract_' not in target.replace('\\', '/'):
            threading.Thread(target=update_index_on_delete, args=(target,), daemon=True).start()

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@files_bp.route('/api/delete-multiple', methods=['POST'])
def delete_multiple():
    """Bulk-delete multiple files/folders in a single request."""
    from database import delete_file_index_entries

    data = request.get_json()
    targets = data.get('targets', [])

    if not targets:
        return jsonify({"error": "Missing targets"}), 400

    results = []
    deleted_paths = []
    dir_paths = []

    for target in targets:
        if not os.path.exists(target):
            results.append({"path": target, "success": False, "error": "Not found"})
            continue

        if is_critical_path(target):
            results.append({"path": target, "success": False, "error": "Protected path"})
            continue

        try:
            is_dir = os.path.isdir(target)
            if is_dir:
                shutil.rmtree(target)
                dir_paths.append(target)
            else:
                os.remove(target)
            deleted_paths.append(target)
            results.append({"path": target, "success": True})
        except Exception as e:
            results.append({"path": target, "success": False, "error": str(e)})

    # Single background DB transaction for all index updates
    if deleted_paths:
        threading.Thread(
            target=delete_file_index_entries,
            args=(deleted_paths, dir_paths if dir_paths else None),
            daemon=True
        ).start()

    return jsonify({"success": True, "results": results})


@files_bp.route('/api/delete-file', methods=['POST'])
def api_delete_file():
    """Delete a file from the collection view (handles relative paths from DATA_DIR)"""
    from app import DATA_DIR, update_index_on_delete

    data = request.get_json()
    relative_path = data.get('path')

    if not relative_path:
        return jsonify({"error": "Missing file path"}), 400

    # Convert relative path to absolute path
    if os.path.isabs(relative_path):
        target = relative_path
    else:
        target = os.path.join(DATA_DIR, relative_path)

    if not os.path.exists(target):
        return jsonify({"error": "File does not exist"}), 404

    # Check if trying to delete critical folders
    if is_critical_path(target):
        app_logger.error(f"Attempted to delete critical folder: {target}")
        return jsonify({"error": get_critical_path_error_message(target, "delete")}), 403

    try:
        if os.path.isdir(target):
            shutil.rmtree(target)
            app_logger.info(f"Deleted directory: {target}")
        else:
            os.remove(target)
            app_logger.info(f"Deleted file: {target}")

        # Update file index incrementally (no cache invalidation needed with DB-first approach)
        update_index_on_delete(target)

        return jsonify({"success": True})
    except Exception as e:
        app_logger.error(f"Error deleting file {target}: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Create Folder
# =============================================================================

@files_bp.route('/create-folder', methods=['POST'])
def create_folder():
    from app import update_index_on_create

    data = request.json
    path = data.get('path')
    if not path:
        return jsonify({"success": False, "error": "No path specified"}), 400

    # Check if trying to create folder inside critical paths
    if is_critical_path(path):
        app_logger.error(f"Attempted to create folder in critical path: {path}")
        return jsonify({"success": False, "error": get_critical_path_error_message(path, "create folder in")}), 403

    try:
        os.makedirs(path)

        # Update file index incrementally (no cache invalidation needed with DB-first approach)
        update_index_on_create(path)

        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# =============================================================================
# Cleanup Orphan Files
# =============================================================================

@files_bp.route('/cleanup-orphan-files', methods=['POST'])
def cleanup_orphan_files():
    """
    Clean up orphan temporary download files in the WATCH directory.
    This endpoint allows manual cleanup of files that shouldn't be there.
    """
    try:
        watch_directory = config.get("SETTINGS", "WATCH", fallback="/temp")

        if not os.path.exists(watch_directory):
            return jsonify({"success": False, "error": "Watch directory does not exist"}), 400

        cleaned_count = 0
        total_size_cleaned = 0
        cleaned_files = []

        # Define temporary download file patterns
        temp_patterns = [
            '.crdownload', '.tmp', '.part', '.mega', '.bak',
            '.download', '.downloading', '.incomplete'
        ]

        def is_temporary_download_file(filename):
            """Check if a filename indicates a temporary download file"""
            filename_lower = filename.lower()

            # Check for common temporary download patterns
            for pattern in temp_patterns:
                if pattern in filename_lower:
                    return True

            # Check for numbered temporary files (e.g., .0, .1, .2)
            if re.search(r'\.\d+\.(crdownload|tmp|part|download)$', filename_lower):
                return True

            # Check for files that look like incomplete downloads
            if re.search(r'\.(crdownload|tmp|part|download)$', filename_lower):
                return True

            return False

        def format_size(size_bytes):
            """Helper function to format file sizes in human-readable format"""
            if size_bytes == 0:
                return "0B"

            import math
            size_names = ["B", "KB", "MB", "GB", "TB"]
            i = int(math.floor(math.log(size_bytes, 1024)))
            p = math.pow(1024, i)
            s = round(size_bytes / p, 2)
            return f"{s} {size_names[i]}"

        # Walk through watch directory and clean up orphan files
        for root, dirs, files in os.walk(watch_directory):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not is_hidden(os.path.join(root, d))]

            for file in files:
                file_path = os.path.join(root, file)

                # Skip hidden files
                if is_hidden(file_path):
                    continue

                # Check if this is a temporary download file
                if is_temporary_download_file(file):
                    try:
                        file_size = os.path.getsize(file_path)
                        os.remove(file_path)
                        cleaned_count += 1
                        total_size_cleaned += file_size

                        # Add to cleaned files list for reporting
                        rel_path = os.path.relpath(file_path, watch_directory)
                        cleaned_files.append({
                            "file": rel_path,
                            "size": format_size(file_size)
                        })

                        app_logger.info(f"Cleaned up orphan file: {file_path} ({format_size(file_size)})")
                    except Exception as e:
                        app_logger.error(f"Error cleaning up orphan file {file_path}: {e}")

        if cleaned_count > 0:
            app_logger.info(f"Manual cleanup completed: {cleaned_count} files removed, {format_size(total_size_cleaned)} freed")
            return jsonify({
                "success": True,
                "message": f"Cleanup completed: {cleaned_count} files removed, {format_size(total_size_cleaned)} freed",
                "cleaned_count": cleaned_count,
                "total_size_cleaned": format_size(total_size_cleaned),
                "cleaned_files": cleaned_files
            })
        else:
            app_logger.info("No orphan files found during manual cleanup")
            return jsonify({
                "success": True,
                "message": "No orphan files found",
                "cleaned_count": 0,
                "total_size_cleaned": "0B",
                "cleaned_files": []
            })

    except Exception as e:
        app_logger.error(f"Error during manual orphan file cleanup: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
