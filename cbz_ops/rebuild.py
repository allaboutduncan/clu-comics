import os
import sys
import subprocess
import zipfile
import shutil
import time
from app_logging import app_logger
from config import config, load_config
from helpers import is_hidden, extract_rar_with_unar

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


def count_rebuildable_files(directory):
    """
    Count the total number of files that will be rebuilt (RAR/CBR for conversion + CBZ for rebuild).
    
    :param directory: Path to the directory containing files.
    :return: Total count of files to process
    """
    total_files = 0
    
    for file_name in os.listdir(directory):
        file_path = os.path.join(directory, file_name)
        # Skip hidden files in the source directory.
        if is_hidden(file_path):
            continue
        # Count RAR/CBR files (for conversion) and CBZ files (for rebuild)
        if file_name.lower().endswith(('.rar', '.cbr', '.cbz')):
            total_files += 1
    
    return total_files


def convert_single_rar_file(rar_path, zip_path, temp_extraction_dir):
    """
    Convert a single RAR file to CBZ with progress reporting.
    
    :param rar_path: Path to the RAR file
    :param zip_path: Path for the output CBZ file
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
            dirs[:] = [d for d in dirs if not is_hidden(os.path.join(root, d))]
            for file in files:
                file_path = os.path.join(root, file)
                if not is_hidden(file_path):
                    extracted_files.append(file_path)
        
        total_files = len(extracted_files)
        app_logger.info(f"Step 2/3: Found {total_files} files to compress...")
        
        # Step 3: Create CBZ file with progress reporting
        app_logger.info(f"Step 3/3: Creating CBZ file...")
        processed_files = 0
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for extract_root, extract_dirs, extract_files in os.walk(temp_extraction_dir):
                # Skip hidden directories within the extraction folder.
                extract_dirs[:] = [d for d in extract_dirs if not is_hidden(os.path.join(extract_root, d))]
                for extract_file in extract_files:
                    file_path_inner = os.path.join(extract_root, extract_file)
                    if is_hidden(file_path_inner):
                        continue

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
        return True
        
    except Exception as e:
        app_logger.error(f"Failed to convert {os.path.basename(rar_path)}: {e}")
        return False


def rebuild_single_cbz_file(cbz_path, directory):
    """
    Rebuild a single CBZ file with progress reporting.
    
    :param cbz_path: Path to the CBZ file
    :param directory: Directory containing the file
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
        new_zip_file = os.path.join(directory, base_name + ".zip")
        shutil.move(cbz_path, new_zip_file)
        
        # Step 2: Create extraction folder
        app_logger.info(f"Step 2/4: Creating extraction folder...")
        folder_path = os.path.join(directory, base_name)
        os.makedirs(folder_path, exist_ok=True)
        
        # Step 3: Extract ZIP file
        app_logger.info(f"Step 3/4: Extracting {filename}...")
        with zipfile.ZipFile(new_zip_file, 'r') as zip_ref:
            file_list = zip_ref.namelist()
            total_files = len(file_list)
            extracted_files = 0
            
            for file_info in zip_ref.infolist():
                zip_ref.extract(file_info, folder_path)
                extracted_files += 1
                
                # Progress reporting for large files
                if is_large_file and extracted_files % max(1, total_files // 10) == 0:
                    progress_percent = (extracted_files / total_files) * 100
                    app_logger.info(f"Extraction progress: {progress_percent:.1f}% ({extracted_files}/{total_files} files)")
        
        # Step 4: Recompress to CBZ
        app_logger.info(f"Step 4/4: Recompressing {filename}...")
        bak_file = os.path.join(directory, base_name + ".bak")
        shutil.move(new_zip_file, bak_file)
        
        cbz_file = os.path.join(directory, base_name + ".cbz")
        with zipfile.ZipFile(cbz_file, 'w', zipfile.ZIP_DEFLATED) as zip_ref:
            file_count = 0
            total_files = 0

            # Count total files first
            for root, _, files in os.walk(folder_path):
                total_files += len(files)

            # Compress files with progress reporting
            for root, _, files in os.walk(folder_path):
                for file in files:
                    file_full_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_full_path, folder_path)

                    # Create ZipInfo manually to control the timestamp
                    # ZIP format requires dates >= 1980-01-01
                    zip_info = zipfile.ZipInfo(filename=arcname)
                    zip_info.compress_type = zipfile.ZIP_DEFLATED

                    # Get file stats
                    file_stat = os.stat(file_full_path)
                    file_time = time.localtime(file_stat.st_mtime)

                    # Check if timestamp is before 1980
                    if file_time.tm_year < 1980:
                        # Use a safe default timestamp: 1980-01-01 00:00:00
                        zip_info.date_time = (1980, 1, 1, 0, 0, 0)
                    else:
                        zip_info.date_time = file_time[:6]

                    # Write file with controlled timestamp
                    with open(file_full_path, 'rb') as f:
                        zip_ref.writestr(zip_info, f.read())

                    file_count += 1

                    # Progress reporting for large files
                    if is_large_file and file_count % max(1, total_files // 10) == 0:
                        progress_percent = (file_count / total_files) * 100
                        app_logger.info(f"Compression progress: {progress_percent:.1f}% ({file_count}/{total_files} files)")
        
        # Clean up
        for root, dirs, files in os.walk(folder_path, topdown=False):
            for file in files:
                os.remove(os.path.join(root, file))
            for dir in dirs:
                os.rmdir(os.path.join(root, dir))
        os.rmdir(folder_path)
        
        # Remove backup file
        os.remove(bak_file)
        
        app_logger.info(f"Successfully rebuilt: {filename}")
        return True
        
    except zipfile.BadZipFile as e:
        # Handle the case where a .cbz file is actually a RAR file
        if "File is not a zip file" in str(e) or "BadZipFile" in str(e):
            app_logger.warning(f"Detected that {filename} is not a valid ZIP file. Attempting to rename to .rar and retry...")
            
            # Rename the file back to .rar
            rar_file = os.path.join(directory, base_name + ".rar")
            if os.path.exists(new_zip_file):
                shutil.move(new_zip_file, rar_file)
            elif os.path.exists(cbz_path):
                shutil.move(cbz_path, rar_file)
            
            # Clean up any partial extraction folder
            if os.path.exists(folder_path):
                shutil.rmtree(folder_path)
            
            # Try to convert as RAR file
            temp_extraction_dir = os.path.join(directory, f"temp_{base_name}")
            zip_path = os.path.join(directory, base_name + '.cbz')
            
            app_logger.info(f"Attempting to convert {base_name}.rar as RAR file...")
            success = convert_single_rar_file(rar_file, zip_path, temp_extraction_dir)
            
            if success:
                # Delete the original RAR file
                os.remove(rar_file)
                # Clean up temp directory
                if os.path.exists(temp_extraction_dir):
                    shutil.rmtree(temp_extraction_dir)
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


def convert_rar_to_zip_in_directory(directory, total_files=None, processed_files=None):
    """
    Convert all RAR/CBR files in a directory to CBZ files using unar for extraction,
    skipping hidden system files and directories.
    
    :param directory: Path to the directory containing RAR/CBR files.
    :param total_files: Total number of files to process (for progress tracking)
    :param processed_files: Current processed count (for progress tracking)
    :return: List of successfully converted files (without extensions).
    """
    app_logger.info("********************// Rebuild ALL Files in Directory //********************")
    os.makedirs(directory, exist_ok=True)
    converted_files = []

    for file_name in os.listdir(directory):
        file_path = os.path.join(directory, file_name)
        # Skip hidden files in the source directory.
        if is_hidden(file_path):
            continue

        if file_name.lower().endswith(('.rar', '.cbr')):
            if total_files and processed_files is not None:
                processed_files[0] += 1
            
            rar_path = file_path
            temp_extraction_dir = os.path.join(directory, f"temp_{file_name[:-4]}")
            zip_path = os.path.join(directory, file_name[:-4] + '.cbz')

            app_logger.info(f"Processing file: {file_name} ({processed_files[0] if processed_files else '?'}/{total_files if total_files else '?'})")
            
            success = convert_single_rar_file(rar_path, zip_path, temp_extraction_dir)
            
            if success:
                converted_files.append(file_name[:-4])  # Store the filename without extension.
                # Delete the original RAR/CBR file.
                os.remove(rar_path)
            
            # Clean up temp directory
            if os.path.exists(temp_extraction_dir):
                shutil.rmtree(temp_extraction_dir)

    return converted_files


def rebuild_task(directory):
    if not os.path.isdir(directory):
        app_logger.error(f"Directory {directory} not found.")
        return

    # Count total files for progress tracking first
    total_rebuildable = count_rebuildable_files(directory)
    processed_files = 0
    
    if total_rebuildable == 0:
        app_logger.info("No files found to rebuild.")
        return
    
    app_logger.info(f"Found {total_rebuildable} files to process.")
    app_logger.info(f"Checking for rar/cbr files in directory: {directory}...")

    converted_files = convert_rar_to_zip_in_directory(directory, total_rebuildable, [processed_files])

    app_logger.info(f"Rebuilding project in directory: {directory}...")

    # Get CBZ files, but also check for any files that might have been renamed during processing
    cbz_files = [f for f in os.listdir(directory) if f.lower().endswith(".cbz")]
    total_files = len(cbz_files)
    app_logger.info(f"Total .cbz files to process: {total_files}")

    i = 1
    while i <= total_files:
        # Re-scan directory in case files were renamed during processing
        current_cbz_files = [f for f in os.listdir(directory) if f.lower().endswith(".cbz")]
        
        if i > len(current_cbz_files):
            break
            
        filename = current_cbz_files[i-1]
        base_name, original_ext = os.path.splitext(filename)

        # Skip files that were just converted
        if base_name in converted_files:
            app_logger.info(f"Skipping rebuild for recently converted file: {filename}")
            i += 1
            continue

        file_path = os.path.join(directory, filename)
        # Double-check if the file is hidden.
        if is_hidden(file_path):
            app_logger.info(f"Skipping hidden file: {file_path}")
            i += 1
            continue

        app_logger.info(f"Processing file: {filename} ({i}/{total_files})")
        
        success = rebuild_single_cbz_file(file_path, directory)
        
        if not success:
            app_logger.error(f"Failed to rebuild {filename}, continuing with next file...")
        
        i += 1

    app_logger.info(f"Rebuild completed in {directory}!")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        app_logger.info("No directory provided!")
    else:
        directory = sys.argv[1]
        rebuild_task(directory)
