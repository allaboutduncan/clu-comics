import os
import sys
import subprocess
import zipfile
import shutil
import time
from app_logging import app_logger
from config import config, load_config
from helpers import extract_rar_with_unar

load_config()

# Large file threshold (configurable)
LARGE_FILE_THRESHOLD = config.getint("SETTINGS", "LARGE_FILE_THRESHOLD", fallback=500) * 1024 * 1024  # Convert MB to bytes


def get_file_size_mb(file_path):
    """Get file size in MB."""
    try:
        size_bytes = os.path.getsize(file_path)
        return size_bytes / (1024 * 1024)
    except OSError:
        return 0


def convert_single_rar_file(rar_path, cbz_path, temp_extraction_dir):
    """
    Convert a single RAR file to CBZ with progress reporting.
    
    :param rar_path: Path to the RAR file
    :param cbz_path: Path for the output CBZ file
    :param temp_extraction_dir: Temporary directory for extraction
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
        extraction_success = extract_rar_with_unar(rar_path, temp_extraction_dir)
        
        if not extraction_success:
            app_logger.error(f"Failed to extract any files from {os.path.basename(rar_path)}")
            return False
        
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
        
        with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_DEFLATED) as zf:
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
        
        app_logger.info(f"Successfully converted: {os.path.basename(rar_path)}")
        
        # Regenerate thumbnail for the converted file
        try:
            import hashlib
            from database import get_db_connection
            
            file_hash = hashlib.md5(cbz_path.encode()).hexdigest()
            shard_dir = file_hash[:2]
            cache_dir = config.get("SETTINGS", "CACHE_DIR", fallback="/cache")
            cache_subdir = os.path.join(cache_dir, 'thumbnails', shard_dir)
            cache_path = os.path.join(cache_subdir, f"{file_hash}.jpg")
            os.makedirs(cache_subdir, exist_ok=True)
            
            with zipfile.ZipFile(cbz_path, 'r') as zf:
                file_list = zf.namelist()
                image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
                image_files = sorted([f for f in file_list if os.path.splitext(f.lower())[1] in image_extensions])
                
                if image_files:
                    with zf.open(image_files[0]) as image_file:
                        from PIL import Image
                        img = Image.open(image_file)
                        if img.mode in ('RGBA', 'LA', 'P'):
                            img = img.convert('RGB')
                        aspect_ratio = img.width / img.height
                        new_height = 300
                        new_width = int(new_height * aspect_ratio)
                        img.thumbnail((new_width, new_height), Image.Resampling.LANCZOS)
                        img.save(cache_path, format='JPEG', quality=85)
                        
                        conn = get_db_connection()
                        if conn:
                            file_mtime = int(os.path.getmtime(cbz_path))
                            conn.execute(
                                'INSERT OR REPLACE INTO thumbnail_jobs (path, status, file_mtime, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)',
                                (cbz_path, 'completed', file_mtime)
                            )
                            conn.commit()
                            conn.close()
                        app_logger.info(f"Thumbnail regenerated for {cbz_path}")
        except Exception as e:
            app_logger.error(f"Error regenerating thumbnail: {e}")
        
        return True
        
    except Exception as e:
        app_logger.error(f"Failed to convert {os.path.basename(rar_path)}: {e}")
        return False


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
    
    if is_large_file:
        app_logger.info(f"Processing large file ({file_size_mb:.1f}MB): {filename}")
        app_logger.info("This may take several minutes. Progress updates will be provided.")
    
    try:
        # Step 1: Rename CBZ to ZIP
        app_logger.info(f"Step 1/4: Preparing {filename} for rebuild...")
        directory = os.path.dirname(cbz_path)
        zip_path = os.path.join(directory, base_name + '.zip')
        shutil.move(cbz_path, zip_path)
        
        # Step 2: Create extraction folder
        app_logger.info(f"Step 2/4: Creating extraction folder...")
        folder_name = os.path.join(directory, base_name + '_folder')
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
        
        with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_DEFLATED) as zf:
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
        
        app_logger.info(f"Successfully rebuilt: {filename}")
        
        # Regenerate thumbnail for the rebuilt file
        try:
            import hashlib
            from database import get_db_connection
            
            file_hash = hashlib.md5(cbz_path.encode()).hexdigest()
            shard_dir = file_hash[:2]
            cache_dir = config.get("SETTINGS", "CACHE_DIR", fallback="/cache")
            cache_subdir = os.path.join(cache_dir, 'thumbnails', shard_dir)
            cache_path = os.path.join(cache_subdir, f"{file_hash}.jpg")
            os.makedirs(cache_subdir, exist_ok=True)
            
            with zipfile.ZipFile(cbz_path, 'r') as zf:
                file_list = zf.namelist()
                image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
                image_files = sorted([f for f in file_list if os.path.splitext(f.lower())[1] in image_extensions])
                
                if image_files:
                    with zf.open(image_files[0]) as image_file:
                        from PIL import Image
                        img = Image.open(image_file)
                        if img.mode in ('RGBA', 'LA', 'P'):
                            img = img.convert('RGB')
                        aspect_ratio = img.width / img.height
                        new_height = 300
                        new_width = int(new_height * aspect_ratio)
                        img.thumbnail((new_width, new_height), Image.Resampling.LANCZOS)
                        img.save(cache_path, format='JPEG', quality=85)
                        
                        conn = get_db_connection()
                        if conn:
                            file_mtime = int(os.path.getmtime(cbz_path))
                            conn.execute(
                                'INSERT OR REPLACE INTO thumbnail_jobs (path, status, file_mtime, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)',
                                (cbz_path, 'completed', file_mtime)
                            )
                            conn.commit()
                            conn.close()
                        app_logger.info(f"Thumbnail regenerated for {cbz_path}")
        except Exception as e:
            app_logger.error(f"Error regenerating thumbnail: {e}")
        
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
            temp_extraction_dir = os.path.join(directory, f"temp_{base_name}")
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
                from database import invalidate_browse_cache
                invalidate_browse_cache(directory)
                app_logger.info(f"Invalidated browse cache for: {directory}")

                app_logger.info(f"Successfully converted {filename} (was actually a RAR file)")
                return True
            else:
                app_logger.error(f"Failed to convert {base_name}.rar after renaming from {filename}")
                return False
        else:
            app_logger.error(f"Failed to rebuild {filename}: {e}")
            return False

    except Exception as e:
        app_logger.error(f"Failed to rebuild {filename}: {e}")
        return False


def handle_cbz_file(file_path):
    """
    Handle the conversion of a .cbz file: unzip, rename, compress, and clean up.

    :param file_path: Path to the .cbz file.
    :return: None
    """
    app_logger.info(f"Handling CBZ file: {file_path}")
    
    if not file_path.lower().endswith('.cbz'):
        app_logger.info("Provided file is not a CBZ file.")
        return

    success = rebuild_single_cbz_file(file_path)
    if not success:
        app_logger.error(f"Failed to rebuild CBZ file: {file_path}")


def convert_to_cbz(file_path):
    """
    Convert a single RAR or CBR file to a ZIP file using unar for extraction.

    :param file_path: Path to the RAR or CBR file.
    :return: None
    """
    app_logger.info(f"********************// Single File Conversion //********************")
    app_logger.info(f"-- Path to file: {file_path}")

    # Check if the file exists
    if not os.path.exists(file_path):
        app_logger.error(f"File does not exist: {file_path}")
        return

    # Check if it's a .rar or .cbr file
    if file_path.lower().endswith(('.rar', '.cbr')):
        app_logger.info("Converting RAR/CBR to CBZ format")

        base_name = os.path.splitext(file_path)[0]  # Removes the extension
        temp_extraction_dir = f"temp_{base_name}"
        cbz_file_path = base_name + '.cbz'

        # Get parent directory for cache invalidation
        parent_dir = os.path.dirname(file_path)

        success = convert_single_rar_file(file_path, cbz_file_path, temp_extraction_dir)

        if success:
            # Delete the original file (RAR or CBR)
            os.remove(file_path)

            # Invalidate browse cache for parent directory
            from database import invalidate_browse_cache, delete_file_index_entry, add_file_index_entry
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
                app_logger.info(f"Updated file index: removed CBR, added CBZ")
            except Exception as index_error:
                app_logger.warning(f"Failed to update file index: {index_error}")
        else:
            app_logger.error(f"Failed to convert {file_path}")

        # Clean up temporary extraction directory
        if os.path.exists(temp_extraction_dir):
            try:
                shutil.rmtree(temp_extraction_dir)
                app_logger.info(f"Cleaned up temporary directory: {temp_extraction_dir}")
            except Exception as cleanup_error:
                app_logger.error(f"Failed to clean up temporary directory {temp_extraction_dir}: {cleanup_error}")

    # Check if it's a .cbz file
    elif file_path.lower().endswith('.cbz'):
        handle_cbz_file(file_path)

    else:
        app_logger.info("File is not a recognized .rar, .cbr, or .cbz file.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        app_logger.error("No file provided!")
    else:
        file_path = sys.argv[1]
        convert_to_cbz(file_path)
