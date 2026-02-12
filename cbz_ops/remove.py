import os
import sys
import zipfile
import shutil
import re
from PIL import Image, ImageFilter, features
from app_logging import app_logger

# Define supported image extensions
SUPPORTED_IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.bmp', '.gif', '.png', '.webp']

def natural_sort_key(file_path):
    """
    Create a sort key that mimics JavaScript's natural sorting behavior:
    - Files starting with special characters (non-alphanumeric) come first
    - Then case-insensitive natural (numeric-aware) sorting

    This matches the sortInlineEditCards function in static/js/index.js
    """
    # Get just the filename without the path
    filename = os.path.basename(file_path)

    # Check if filename starts with alphanumeric character
    starts_with_alphanum = bool(re.match(r'^[a-zA-Z0-9]', filename))

    # Create a list to hold sort key components
    # First element: 0 if starts with special char (sorts first), 1 if alphanumeric
    # Second element: case-insensitive natural sort key
    def convert(text):
        """Convert text to lowercase and numbers to integers for natural sorting"""
        return int(text) if text.isdigit() else text.lower()

    # Split filename into text and number parts for natural sorting
    alphanum_key = [convert(c) for c in re.split('([0-9]+)', filename)]

    return (1 if starts_with_alphanum else 0, alphanum_key)

def check_webp_support():
    """Log WebP support status"""
    webp_supported = features.check('webp')
    app_logger.info(f"WebP support available: {webp_supported}")
    if not webp_supported:
        app_logger.warning("WebP support not available in PIL. Install libwebp-dev and reinstall pillow.")
    return webp_supported

def handle_cbz_file(file_path):
    """
    Handle the conversion of a .cbz file: unzip, process images, compress, and clean up.

    :param file_path: Path to the .cbz file.
    :return: None
    """
    app_logger.info(f"********************// Remove First Image //********************")
    
    # Check WebP support at startup
    check_webp_support()
    
    if not file_path.lower().endswith('.cbz'):
        app_logger.info("Provided file is not a CBZ file.")
        return

    base_name = os.path.splitext(file_path)[0]  # Removes the .cbz extension
    zip_path = base_name + '.zip'
    folder_name = base_name + '_folder'
    
    app_logger.info(f"Processing CBZ: {file_path} --> {zip_path}")

    try:
        # Step 1: Rename .cbz to .zip
        os.rename(file_path, zip_path)

        # Step 2: Create a folder with the file name
        os.makedirs(folder_name, exist_ok=True)

        # Step 3: Unzip the .zip file contents into the folder
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(folder_name)
        
        # Step 4: Process the extracted images
        remove_first_image_file(folder_name)

        # Optional: Apply image processing to all supported images
        # Uncomment the following line if you want to process images
        # process_images(folder_name)

        # Step 5: Rename the original .zip file to .bak
        bak_file_path = zip_path + '.bak'
        os.rename(zip_path, bak_file_path)

        # Step 6: Compress the folder contents back into a .cbz file
        with zipfile.ZipFile(file_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(folder_name):
                for file in files:
                    file_ext = os.path.splitext(file)[1].lower()
                    app_logger.info(f"Processing file: {file} (extension: {file_ext})")
                    
                    if file_ext in SUPPORTED_IMAGE_EXTENSIONS:
                        file_path_in_folder = os.path.join(root, file)
                        
                        # Test if we can actually open the image
                        try:
                            with Image.open(file_path_in_folder) as img:
                                app_logger.info(f"Successfully verified image: {file} (format: {img.format})")
                        except Exception as e:
                            app_logger.warning(f"Cannot open image {file} with PIL: {e}")
                            continue
                        
                        arcname = os.path.relpath(file_path_in_folder, folder_name)
                        zf.write(file_path_in_folder, arcname)
                        app_logger.info(f"Added to archive: {file}")
                    else:
                        app_logger.info(f"Skipping unsupported file type: {file}")

        app_logger.info(f"Successfully re-compressed: {file_path}")

        # Regenerate thumbnail for the modified file
        try:
            import hashlib
            from database import get_db_connection
            from config import config
            
            # Calculate cache path using the same logic as app.py
            file_hash = hashlib.md5(file_path.encode()).hexdigest()
            shard_dir = file_hash[:2]
            cache_dir = config.get("SETTINGS", "CACHE_DIR", fallback="/cache")
            cache_subdir = os.path.join(cache_dir, 'thumbnails', shard_dir)
            cache_path = os.path.join(cache_subdir, f"{file_hash}.jpg")
            
            # Ensure cache directory exists
            os.makedirs(cache_subdir, exist_ok=True)
            
            # Generate thumbnail
            with zipfile.ZipFile(file_path, 'r') as zf:
                file_list = zf.namelist()
                image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
                image_files = sorted([f for f in file_list if os.path.splitext(f.lower())[1] in image_extensions])
                
                if image_files:
                    with zf.open(image_files[0]) as image_file:
                        img = Image.open(image_file)
                        if img.mode in ('RGBA', 'LA', 'P'):
                            img = img.convert('RGB')
                        
                        # Resize to 300px height
                        aspect_ratio = img.width / img.height
                        new_height = 300
                        new_width = int(new_height * aspect_ratio)
                        img.thumbnail((new_width, new_height), Image.Resampling.LANCZOS)
                        
                        img.save(cache_path, format='JPEG', quality=85)
                        
                        # Update DB
                        conn = get_db_connection()
                        if conn:
                            file_mtime = int(os.path.getmtime(file_path))
                            conn.execute(
                                'INSERT OR REPLACE INTO thumbnail_jobs (path, status, file_mtime, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)',
                                (file_path, 'completed', file_mtime)
                            )
                            conn.commit()
                            conn.close()
                            
                        app_logger.info(f"Thumbnail regenerated successfully for {file_path}")
        except Exception as e:
            app_logger.error(f"Error regenerating thumbnail: {e}")

        # Step 7: Delete the .bak file
        os.remove(bak_file_path)

    except Exception as e:
        app_logger.error(f"Failed to process {file_path}: {e}")
    finally:
        # Clean up the temporary folder
        if os.path.exists(folder_name):
            shutil.rmtree(folder_name)

def remove_first_image_file(dir_path):
    """
    Remove the first image file in alphanumerical order from the directory or its subdirectories.

    :param dir_path: Path to the directory.
    :return: None
    """
    # Check if the given directory exists
    if not os.path.exists(dir_path):
        app_logger.info(f"The directory {dir_path} does not exist.")
        return
    
    # List to hold all supported image file paths
    image_files = []

    # Traverse the directory to collect all supported image files
    for root, dirs, files in os.walk(dir_path):
        for file in files:
            file_ext = os.path.splitext(file)[1].lower()
            app_logger.info(f"Found file: {file} (extension: {file_ext})")
            
            if file_ext in SUPPORTED_IMAGE_EXTENSIONS:
                file_path = os.path.join(root, file)
                
                # Verify we can open the image before adding to list
                try:
                    with Image.open(file_path) as img:
                        image_files.append(file_path)
                        app_logger.info(f"Added to processing list: {file}")
                except Exception as e:
                    app_logger.warning(f"Cannot open {file} with PIL, skipping: {e}")
    
    if not image_files:
        app_logger.info(f"No supported image files found in {dir_path} or its subdirectories.")
        return

    # Sort the image files using natural sort (matches JavaScript sorting in index.js)
    # Files starting with special characters come first, then case-insensitive natural sort
    image_files.sort(key=natural_sort_key)

    # The first image in natural sort order
    first_image = image_files[0]
    
    try:
        os.remove(first_image)
        app_logger.info(f"Removed: {first_image}")
    except Exception as e:
        app_logger.info(f"Failed to remove {first_image}. Error: {e}")
        

# Optional: Function to process images (e.g., apply a filter)
def process_images(dir_path):
    """
    Apply a filter to all supported image files in the directory and its subdirectories.

    :param dir_path: Path to the directory.
    :return: None
    """
    for root, dirs, files in os.walk(dir_path):
        for file in files:
            file_ext = os.path.splitext(file)[1].lower()
            if file_ext in SUPPORTED_IMAGE_EXTENSIONS:
                file_path = os.path.join(root, file)
                try:
                    with Image.open(file_path) as img:
                        # Example: Apply a blur filter
                        processed_img = img.filter(ImageFilter.BLUR)
                        processed_img.save(file_path)
                        app_logger.info(f"Processed: {file_path}")
                except Exception as e:
                    app_logger.error(f"Failed to process image {file_path}: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        app_logger.error("No file provided!")
    else:
        file_path = sys.argv[1]
        handle_cbz_file(file_path)