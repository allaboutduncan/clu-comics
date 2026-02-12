import os
import sys
import zipfile
from pdf2image import convert_from_path, pdfinfo_from_path
from app_logging import app_logger
from PIL import Image
from helpers import is_hidden
import gc
import tempfile
import shutil


def scan_and_convert(directory):
    """
    Recursively scans a directory for PDF files, converts each PDF's pages to images, 
    organizes them into folders, and creates a CBZ file for each PDF.
    Uses memory-efficient streaming and batch processing.

    :param directory: Root directory to scan
    """
    # Increase PIL's image pixel limit but still reasonable
    Image.MAX_IMAGE_PIXELS = 500000000

    app_logger.info("********************// Convert All PDF to CBZ //********************")

    for root, dirs, files in os.walk(directory):
        # Skip hidden directories.
        dirs[:] = [d for d in dirs if not is_hidden(os.path.join(root, d))]
        for file in files:
            file_path = os.path.join(root, file)
            # Skip hidden files.
            if is_hidden(file_path):
                continue

            if file.lower().endswith('.pdf'):
                process_pdf_file(file_path)
                # Force garbage collection after each PDF
                gc.collect()


def process_pdf_file(pdf_path):
    """
    Process a single PDF file with memory-efficient streaming.
    """
    pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]
    output_folder = os.path.join(os.path.dirname(pdf_path), pdf_name)
    cbz_path = os.path.join(os.path.dirname(pdf_path), f"{pdf_name}.cbz")
    
    app_logger.info(f"Processing: {pdf_path}")
    
    try:
        # Get PDF info first
        pdf_info = pdfinfo_from_path(pdf_path)
        total_pages = pdf_info["Pages"]
        
        # Create output folder
        os.makedirs(output_folder, exist_ok=True)
        
        # Process pages in batches to reduce memory usage
        batch_size = 5  # Process 5 pages at a time
        for batch_start in range(1, total_pages + 1, batch_size):
            batch_end = min(batch_start + batch_size - 1, total_pages)
            
            app_logger.info(f"Processing pages {batch_start}-{batch_end} of {total_pages}")
            
            # Convert batch of pages
            pages = convert_from_path(
                pdf_path, 
                first_page=batch_start, 
                last_page=batch_end, 
                thread_count=1,
                fmt="jpeg",
                dpi=150  # Reduce DPI to save memory
            )
            
            # Process each page in the batch
            for i, page in enumerate(pages):
                page_number = batch_start + i
                process_single_page(page, page_number, pdf_name, output_folder)
                # Explicitly close the page to free memory
                page.close()
            
            # Clear the pages list to free memory
            pages.clear()
            gc.collect()
        
        # Create CBZ file using streaming approach
        create_cbz_file(output_folder, cbz_path)
        
        # Clean up source PDF
        try:
            os.remove(pdf_path)
            app_logger.info(f"Deleted source PDF: {pdf_path}")
        except OSError as e:
            app_logger.info(f"Failed to delete source PDF {pdf_path}: {e}")
        
        # Clean up temporary folder
        cleanup_temp_folder(output_folder)
        
    except Exception as e:
        app_logger.error(f"Error processing {pdf_path}: {e}")
        # Clean up on error
        if os.path.exists(output_folder):
            cleanup_temp_folder(output_folder)


def process_single_page(page, page_number, pdf_name, output_folder):
    """
    Process a single page with memory-efficient operations.
    """
    try:
        width, height = page.size
        total_pixels = width * height
        
        app_logger.info(f"Page {page_number} size: {width}x{height} pixels ({total_pixels}px)")
        
        page_filename = f"{pdf_name} page_{page_number}.jpg"
        page_path = os.path.join(output_folder, page_filename)
        
        # Resize image if too large to prevent memory issues
        max_pixels = 50_000_000  # 50MP limit
        if total_pixels > max_pixels:
            app_logger.info(f"Resizing large image: {page_filename}")
            # Calculate new dimensions maintaining aspect ratio
            ratio = (max_pixels / total_pixels) ** 0.5
            new_width = int(width * ratio)
            new_height = int(height * ratio)
            page = page.resize((new_width, new_height), Image.LANCZOS)
        
        # Save with optimized settings
        page.save(page_path, "JPEG", dpi=(96, 96), quality=75, optimize=True)
        app_logger.info(f"Saved page {page_number} as {page_filename}")
        
    except Exception as e:
        app_logger.error(f"Error processing page {page_number}: {e}")


def create_cbz_file(output_folder, cbz_path):
    """
    Create CBZ file using streaming approach to avoid loading all files into memory.
    """
    try:
        with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as cbz:
            # Walk through the folder and add files one by one
            for folder_root, _, folder_files in os.walk(output_folder):
                for folder_file in folder_files:
                    file_path_in_folder = os.path.join(folder_root, folder_file)
                    arcname = os.path.relpath(file_path_in_folder, output_folder)
                    
                    # Add file to zip without loading it entirely into memory
                    cbz.write(file_path_in_folder, arcname)
        
        app_logger.info(f"CBZ file created: {cbz_path}")
        
    except Exception as e:
        app_logger.error(f"Error creating CBZ file {cbz_path}: {e}")


def cleanup_temp_folder(folder_path):
    """
    Clean up temporary folder and its contents.
    """
    try:
        if os.path.exists(folder_path):
            shutil.rmtree(folder_path)
            app_logger.info(f"Cleaned up temporary folder: {folder_path}")
    except Exception as e:
        app_logger.error(f"Error cleaning up folder {folder_path}: {e}")


if __name__ == "__main__":
    # The directory path is passed as the first argument
    if len(sys.argv) < 2:
        app_logger.info("No directory provided!")
    else:
        directory = sys.argv[1]
        scan_and_convert(directory)
