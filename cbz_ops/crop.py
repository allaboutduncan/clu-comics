import os
import sys
import zipfile
import shutil
from PIL import Image, ImageFilter
from app_logging import app_logger
from config import config, load_config

load_config()
skipped_exts = config.get("SETTINGS", "SKIPPED_FILES", fallback="")
deleted_exts = config.get("SETTINGS", "DELETED_FILES", fallback="")

skippedFiles = [ext.strip().lower() for ext in skipped_exts.split(",") if ext.strip()]
deletedFiles = [ext.strip().lower() for ext in deleted_exts.split(",") if ext.strip()]


def handle_cbz_file(file_path):
    """
    Handle the conversion of a .cbz file: unzip, process images, compress, and clean up.

    :param file_path: Path to the .cbz file.
    :return: None
    """
    app_logger.info(f"********************// Crop Cover Image //********************")
    app_logger.info(f"-- Handling CBZ file: {file_path}")
    
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
        #logger.info(f"Processing images in folder: {folder_name}")
        process_image(folder_name)

        # Step 5: Rename the original .zip file to .bak
        bak_file_path = zip_path + '.bak'
        os.rename(zip_path, bak_file_path)

        # Step 6: Compress the folder contents back into a .cbz file in alpha-numerical order
        file_list = []
        for root, _, files in os.walk(folder_name):
            for file in files:
                file_path_in_folder = os.path.join(root, file)
                arcname = os.path.relpath(file_path_in_folder, folder_name)
                file_list.append((arcname, file_path_in_folder))
                
        # Sort the file list by arcname (alphabetical order)
        file_list.sort(key=lambda x: x[0])
        
        with zipfile.ZipFile(file_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for arcname, file_path_in_folder in file_list:
                zf.write(file_path_in_folder, arcname)

        app_logger.info(f"Successfully re-compressed: {file_path}")

        # Regenerate thumbnail for the modified file
        try:
            import hashlib
            from database import get_db_connection
            
            # Calculate cache path using the same logic as app.py
            file_hash = hashlib.md5(file_path.encode()).hexdigest()
            shard_dir = file_hash[:2]
            cache_dir = config.get("SETTINGS", "CACHE_DIR", fallback="/cache")
            cache_subdir = os.path.join(cache_dir, 'thumbnails', shard_dir)
            cache_path = os.path.join(cache_subdir, f"{file_hash}.jpg")
            
            # Ensure cache directory exists
            os.makedirs(cache_subdir, exist_ok=True)
            
            # Generate thumbnail
            file_list = []
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


def process_image(directory: str) -> None:
    # Ensure the directory exists
    if not os.path.exists(directory):
        app_logger.info(f"Directory {directory} does not exist.")
        return

    # Recursively search for files in the directory and subdirectories,
    # ignoring and removing files with a .sfv extension.
    def find_images(dir_path):
        for root, _, files in os.walk(dir_path):
            for file in files:
                file_path = os.path.join(root, file)
                ext = os.path.splitext(file)[1].lower()

                # 1) Delete any file whose extension is in DELETED_FILES
                if ext in deletedFiles:
                    try:
                        os.remove(file_path)
                        app_logger.info(f"Removed {file} file: {file_path}")
                    except Exception as e:
                        app_logger.error(f"Error removing {file} file {file_path}: {e}")
                    continue

                # 2) Skip (ignore) any file whose extension is in SKIPPED_FILES
                if ext in skippedFiles or file == "ComicInfo.xml":
                    app_logger.info(f"Skipping file: {file_path}")
                    continue

                # 3) Otherwise, yield it as a candidate image
                yield file_path

    # Get the first image file found
    image_files = list(find_images(directory))
    if not image_files:
        app_logger.info("No files found in the directory or its subdirectories.")
        return

    first_image_path = image_files[0]
    file_name, file_extension = os.path.splitext(first_image_path)

    try:
        # Open the image
        with Image.open(first_image_path) as img:
            width, height = img.size

            # Split the image in half
            right_half = (width // 2, 0, width, height)

            # Save the original image by appending "b" to the file name
            backup_path = f"{file_name}b{file_extension}"
            img.save(backup_path)

            # Save the right half by appending "a" to the file name
            right_half_img = img.crop(right_half)
            new_image_path = f"{file_name}a{file_extension}"
            right_half_img.save(new_image_path)

        # Delete the original image
        os.remove(first_image_path)

        app_logger.info(f"Processed: {os.path.basename(first_image_path)} original saved as {backup_path}, right half saved as {new_image_path}.")
    except Exception as e:
        app_logger.error(f"Error processing the image: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        app_logger.error("No file provided!")
    else:
        file_path = sys.argv[1]
        handle_cbz_file(file_path)
