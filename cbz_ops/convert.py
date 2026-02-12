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

convertSubdirectories = config.getboolean("SETTINGS", "CONVERT_SUBDIRECTORIES", fallback=False)

# Large file threshold (configurable)
LARGE_FILE_THRESHOLD = config.getint("SETTINGS", "LARGE_FILE_THRESHOLD", fallback=500) * 1024 * 1024  # Convert MB to bytes


def get_file_size_mb(file_path):
    """Get file size in MB."""
    try:
        size_bytes = os.path.getsize(file_path)
        return size_bytes / (1024 * 1024)
    except OSError:
        return 0


def count_convertable_files(directory):
    """
    Count the total number of RAR and CBR files that will be converted.
    
    :param directory: Path to the directory containing RAR and CBR files.
    :return: Total count of files to convert
    """
    total_files = 0
    
    if convertSubdirectories:
        # Recursively traverse the directory tree.
        for root, dirs, files in os.walk(directory):
            # Skip hidden directories.
            dirs[:] = [d for d in dirs if not is_hidden(os.path.join(root, d))]
            for file_name in files:
                file_path = os.path.join(root, file_name)
                if is_hidden(file_path):
                    continue
                # Count only .rar and .cbr files.
                if file_name.lower().endswith(('.rar', '.cbr')):
                    total_files += 1
    else:
        # Non-recursive count: only count files in the given directory.
        for file_name in os.listdir(directory):
            file_path = os.path.join(directory, file_name)
            if is_hidden(file_path):
                continue
            if file_name.lower().endswith(('.rar', '.cbr')):
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
                    zf.write(file_path_inner, arcname)
                    
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


def convert_rar_directory(directory):
    """
    Convert all RAR and CBR files in a directory (and optionally its subdirectories)
    to CBZ files using unar for extraction, skipping hidden system files and directories.

    :param directory: Path to the directory containing RAR and CBR files.
    :return: List of successfully converted files (without extensions)
    """
    app_logger.info("********************// Convert Directory to CBZ //********************")
    os.makedirs(directory, exist_ok=True)
    converted_files = []
    
    # Count total files first for progress tracking
    total_files = count_convertable_files(directory)
    processed_files = 0
    
    if total_files == 0:
        app_logger.info("No RAR or CBR files found to convert.")
        return converted_files
    
    app_logger.info(f"Found {total_files} files to convert.")
    
    if convertSubdirectories:
        # Recursively traverse the directory tree.
        for root, dirs, files in os.walk(directory):
            # Skip hidden directories.
            dirs[:] = [d for d in dirs if not is_hidden(os.path.join(root, d))]
            for file_name in files:
                file_path = os.path.join(root, file_name)
                if is_hidden(file_path):
                    continue

                # Process only .rar and .cbr files.
                if file_name.lower().endswith(('.rar', '.cbr')):
                    processed_files += 1
                    
                    rar_path = file_path
                    temp_extraction_dir = os.path.join(root, f"temp_{file_name[:-4]}")
                    zip_path = os.path.join(root, f"{file_name[:-4]}.cbz")

                    app_logger.info(f"Processing file: {file_name} ({processed_files}/{total_files})")
                    
                    success = convert_single_rar_file(rar_path, zip_path, temp_extraction_dir)
                    
                    if success:
                        converted_files.append(file_name[:-4])
                        # Delete the original RAR/CBR file.
                        os.remove(rar_path)
                    
                    # Clean up temp directory
                    if os.path.exists(temp_extraction_dir):
                        shutil.rmtree(temp_extraction_dir)
    else:
        # Non-recursive conversion: only process files in the given directory.
        for file_name in os.listdir(directory):
            file_path = os.path.join(directory, file_name)
            if is_hidden(file_path):
                continue

            if file_name.lower().endswith(('.rar', '.cbr')):
                processed_files += 1
                
                rar_path = file_path
                temp_extraction_dir = os.path.join(directory, f"temp_{file_name[:-4]}")
                zip_path = os.path.join(directory, f"{file_name[:-4]}.cbz")

                app_logger.info(f"Processing file: {file_name} ({processed_files}/{total_files})")
                
                success = convert_single_rar_file(rar_path, zip_path, temp_extraction_dir)
                
                if success:
                    converted_files.append(file_name[:-4])
                    os.remove(rar_path)
                
                # Clean up temp directory
                if os.path.exists(temp_extraction_dir):
                    shutil.rmtree(temp_extraction_dir)

    return converted_files


def main(directory):
    if not os.path.isdir(directory):
        app_logger.error(f"Directory '{directory}' does not exist.")
        return

    app_logger.info(f"Starting conversion in directory: {directory}")
    converted_files = convert_rar_directory(directory)
    app_logger.info(f"Conversion completed. Total files converted: {len(converted_files)}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        app_logger.error("No directory provided! Usage: python script.py <directory_path>")
    else:
        directory = sys.argv[1]
        main(directory)
