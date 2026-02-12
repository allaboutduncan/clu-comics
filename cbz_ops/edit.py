import os
import zipfile
import shutil
import io
import base64
from flask import render_template_string, request, jsonify
from PIL import Image
from app_logging import app_logger
from config import config, load_config
from helpers import create_thumbnail_streaming, safe_image_open
import gc

load_config()
skipped_exts = config.get("SETTINGS", "SKIPPED_FILES", fallback="")
deleted_exts = config.get("SETTINGS", "DELETED_FILES", fallback="")

skippedFiles = [ext.strip().lower() for ext in skipped_exts.split(",") if ext.strip()]
deletedFiles = [ext.strip().lower() for ext in deleted_exts.split(",") if ext.strip()]

# Partial template for the modal body (the grid of Bootstrap Cards)
modal_body_template = '''
    {% for card in file_cards %}
      <div class="col">
        <div class="card h-100 shadow-sm">
          <div class="row g-0">
            <div class="col-3">
              {% if card.img_data %}
                <img src="{{ card.img_data }}" class="img-fluid rounded-start object-fit-scale border rounded" alt="{{ card.filename }}">
              {% else %}
                <img src="https://via.placeholder.com/100" class="img-fluid rounded-start object-fit-scale border rounded" alt="No image">
              {% endif %}
            </div>
            <div class="col-9">
              <div class="card-body">
                <p class="card-text small">
                    <span class="editable-filename" data-rel-path="{{ card.rel_path }}" onclick="enableFilenameEdit(this)">
                      {{ card.filename }}
                    </span>
                    <input type="text" class="form-control d-none filename-input form-control-sm" value="{{ card.filename }}" data-rel-path="{{ card.rel_path }}">
                </p>
                <div class="d-flex justify-content-end">
                <div class="btn-group" role="group" aria-label="Basic example">
                  <button type="button" class="btn btn-outline-primary btn-sm" onclick="cropImageFreeForm(this)" title="Free Form Crop">
                    <i class="bi bi-crop"></i> Free
                  </button>
                  <button type="button" class="btn btn-outline-secondary btn-sm" onclick="cropImageLeft(this)" title="Crop Image Left">
                    <i class="bi bi-arrow-bar-left"></i> Left
                  </button>
                  <button type="button" class="btn btn-outline-secondary" onclick="cropImageCenter(this)" title="Crop Image Center">Middle</button>
                  <button type="button" class="btn btn-outline-secondary btn-sm" onclick="cropImageRight(this)" title="Crop Image Right">
                    Right <i class="bi bi-arrow-bar-right"></i>
                  </button>
                  <button type="button" class="btn btn-outline-danger btn-sm" onclick="deleteCardImage(this)">
                    <i class="bi bi-trash"></i>
                  </button>
                </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    {% endfor %}
'''

def process_cbz_file(file_path):
    """
    Process the CBZ file:
      1. Rename the .cbz file to .zip.
      2. Create a folder based on the file name.
      3. Extract the ZIP contents into the folder.
      4. If the ZIP contains a single directory (ignoring files), update folder_name to that inner directory.
         (This is done recursively in case there are multiple nested directories.)
      5. Delete all .nfo, .sfv, .db and .DS_Store files.
    Returns a dictionary with 'folder_name' and 'zip_file_path'.
    Uses memory-efficient streaming extraction.
    """
    app_logger.info("********************// Editing CBZ File //********************")
    if not file_path.lower().endswith(('.cbz', '.zip')):
        app_logger.info("Provided file is not a CBZ file.")
        raise ValueError("Provided file is not a CBZ file.")
    
    base_name = os.path.splitext(file_path)[0]
    zip_path = base_name + '.zip'
    folder_name = base_name + '_folder'
    
    app_logger.info(f"Processing CBZ: {file_path} --> {zip_path}")
    
    try:
        # Step 1: Rename .cbz to .zip
        os.rename(file_path, zip_path)
        
        # Step 2: Create folder for extraction
        os.makedirs(folder_name, exist_ok=True)
        
        # Step 3: Extract ZIP contents into folder using streaming
        with zipfile.ZipFile(zip_path, 'r') as zf:
            # Get list of files first to avoid memory issues
            file_list = zf.namelist()
            
            for filename in file_list:
                try:
                    zf.extract(filename, folder_name)
                except Exception as e:
                    app_logger.warning(f"Failed to extract {filename}: {e}")
                    continue
        
        # Step 4: Delete files that match deleted extensions (case-insensitive)
        # This must happen BEFORE checking for nested folders, to ensure files in parent folders are deleted
        for root, _, files in os.walk(folder_name):
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in deletedFiles:
                    file_path = os.path.join(root, file)
                    try:
                        os.remove(file_path)
                        app_logger.info(f"Deleted unwanted file: {file_path}")
                    except Exception as e:
                        app_logger.error(f"Error deleting file {file_path}: {e}")

        # Step 5: Check if the extracted content contains a single directory.
        # Do this recursively in case the ZIP nests multiple single-directory levels.
        while True:
            # List only directories, ignoring any loose files.
            inner_dirs = [d for d in os.listdir(folder_name) if os.path.isdir(os.path.join(folder_name, d))]
            if len(inner_dirs) == 1:
                folder_name = os.path.join(folder_name, inner_dirs[0])
                app_logger.info(f"Found a single nested folder, updating folder_name to: {folder_name}")
            else:
                break

        app_logger.info(f"Extraction complete: {folder_name}")
        return {"folder_name": folder_name, "zip_file_path": zip_path}
        
    except Exception as e:
        app_logger.error(f"Error processing CBZ file: {e}")
        # Clean up on error
        if os.path.exists(folder_name):
            try:
                shutil.rmtree(folder_name)
            except Exception as cleanup_error:
                app_logger.error(f"Error cleaning up folder on failure: {cleanup_error}")
        raise


def get_edit_modal(file_path):
    """
    Processes the provided CBZ file and returns a dictionary with keys:
      - modal_body: rendered HTML for the modal body (Bootstrap cards)
      - folder_name, zip_file_path, original_file_path: for the hidden form fields.
    This function walks through subdirectories and ignores .xml files.
    Uses memory-efficient thumbnail generation.
    """
    result = process_cbz_file(file_path)
    folder_name = result["folder_name"]
    zip_file_path = result["zip_file_path"]
    
    file_cards = []
    # Walk the extraction folder recursively
    for root, _, files in os.walk(folder_name):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            # Skip files based on config
            if ext in skippedFiles:
                app_logger.info(f"Skipping file in edit modal: {f}")
                continue

            # Create a relative path that includes subdirectories
            rel_path = os.path.relpath(os.path.join(root, f), folder_name)
            filename_only = os.path.basename(rel_path)  # Extract only the file name
            img_data = None

            # Attempt thumbnail generation for common image types using streaming
            if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')):
                try:
                    full_path = os.path.join(root, f)
                    
                    # Use streaming thumbnail generation
                    thumbnail_data = create_thumbnail_streaming(full_path, max_size=(100, 100), quality=85)
                    
                    if thumbnail_data:
                        encoded = base64.b64encode(thumbnail_data).decode('utf-8')
                        img_data = f"data:image/jpeg;base64,{encoded}"
                    else:
                        app_logger.warning(f"Failed to generate thumbnail for: {rel_path}")
                        
                except Exception as e:
                    app_logger.info(f"Thumbnail generation failed for '{rel_path}': {e}")

            file_cards.append({"filename": filename_only, "rel_path": rel_path, "img_data": img_data})
            
            # Force garbage collection periodically to prevent memory buildup
            if len(file_cards) % 10 == 0:
                gc.collect()

    modal_body_html = render_template_string(modal_body_template, file_cards=file_cards)
    
    return {
        "modal_body": modal_body_html,
        "folder_name": folder_name,
        "zip_file_path": zip_file_path,
        "original_file_path": file_path
    }

def save_cbz():
    """
    Processes the CBZ file by:
      - Renaming the .zip file (created during extraction) to .bak (Step 5)
      - Re-compressing the extracted folder (sorted) into a .cbz file (Step 6)
      - Deleting the .bak file and cleaning up (Step 7)
    This function is meant to be used as a route handler and is imported in app.py.
    Uses memory-efficient streaming compression.
    """
    app_logger.info(f"Clean up and re-compressing the CBZ file.")
    folder_name = request.form.get('folder_name')
    zip_file_path = request.form.get('zip_file_path')
    original_file_path = request.form.get('original_file_path')
    
    if not folder_name or not zip_file_path or not original_file_path:
        return "Missing required data", 400

    try:
        # Step 6: Rename the original .zip file to .bak.
        bak_file_path = zip_file_path + '.bak'
        os.rename(zip_file_path, bak_file_path)
        
        # Step 7: Re-compress the folder contents into a .cbz file (sorted).
        # Use streaming approach to avoid loading all files into memory
        with zipfile.ZipFile(original_file_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as cbz:
            # Collect all files first
            file_list = []
            for root, _, files in os.walk(folder_name):
                for file in files:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, folder_name)
                    file_list.append((rel_path, full_path))
            
            # Sort files for consistent ordering
            file_list.sort(key=lambda x: x[0])
            
            # Add files to zip one by one
            for rel_path, full_path in file_list:
                try:
                    cbz.write(full_path, rel_path)
                except Exception as e:
                    app_logger.warning(f"Failed to add {rel_path} to CBZ: {e}")
                    continue
        
        # Step 8: Delete the .bak file.
        try:
            os.remove(bak_file_path)
            app_logger.info(f"Deleted backup file: {bak_file_path}")
        except Exception as e:
            app_logger.error(f"Error deleting backup file {bak_file_path}: {e}")
        
        # Step 9: Clean up the extracted folder(s).
        try:
            # Clean up the current folder (which might be a nested folder)
            if os.path.exists(folder_name):
                shutil.rmtree(folder_name)
                app_logger.info(f"Cleaned up extracted folder: {folder_name}")
            
            # Also clean up the outer extraction folder (with _folder suffix)
            # This handles the case where we extracted to a nested folder
            outer_folder = os.path.splitext(original_file_path)[0] + '_folder'
            if os.path.exists(outer_folder) and outer_folder != folder_name:
                shutil.rmtree(outer_folder)
                app_logger.info(f"Cleaned up outer extraction folder: {outer_folder}")
                
        except Exception as e:
            app_logger.error(f"Error cleaning up folders: {e}")
        
        # Force garbage collection
        gc.collect()
        
        # Regenerate thumbnail for the edited file
        try:
            import hashlib
            from database import get_db_connection
            
            file_hash = hashlib.md5(original_file_path.encode()).hexdigest()
            shard_dir = file_hash[:2]
            cache_dir = config.get("SETTINGS", "CACHE_DIR", fallback="/cache")
            cache_subdir = os.path.join(cache_dir, 'thumbnails', shard_dir)
            cache_path = os.path.join(cache_subdir, f"{file_hash}.jpg")
            os.makedirs(cache_subdir, exist_ok=True)
            
            with zipfile.ZipFile(original_file_path, 'r') as zf:
                file_list = zf.namelist()
                image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
                image_files = sorted([f for f in file_list if os.path.splitext(f.lower())[1] in image_extensions])
                
                if image_files:
                    with zf.open(image_files[0]) as image_file:
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
                            file_mtime = int(os.path.getmtime(original_file_path))
                            conn.execute(
                                'INSERT OR REPLACE INTO thumbnail_jobs (path, status, file_mtime, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)',
                                (original_file_path, 'completed', file_mtime)
                            )
                            conn.commit()
                            conn.close()
                        app_logger.info(f"Thumbnail regenerated for {original_file_path}")
        except Exception as e:
            app_logger.error(f"Error regenerating thumbnail: {e}")
        
        return jsonify({"success": True, "message": "CBZ file saved successfully"})
        
    except Exception as e:
        app_logger.error(f"Error saving CBZ file: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    

def cropRight(image_path):
    file_name, file_extension = os.path.splitext(image_path)

    try:
        # Open the image
        with Image.open(image_path) as img:
            width, height = img.size

            # Split the image in half (right half)
            right_half = (width // 2, 0, width, height)

            # Save the original image by appending "b" to the file name
            backup_path = f"{file_name}b{file_extension}"
            img.save(backup_path)

            # Save the right half by appending "a" to the file name
            right_half_img = img.crop(right_half)
            new_image_path = f"{file_name}a{file_extension}"
            right_half_img.save(new_image_path)

        # Delete the original image
        os.remove(image_path)

        app_logger.info(f"Processed: {os.path.basename(image_path)} original saved as {backup_path}, right half saved as {new_image_path}.")

        return new_image_path, backup_path
    
    except Exception as e:
        app_logger.error(f"Error processing the image: {e}")


def cropLeft(image_path):
    file_name, file_extension = os.path.splitext(image_path)

    try:
        # Open the image
        with Image.open(image_path) as img:
            width, height = img.size

            # Split the image in half (left half)
            left_half = (0, 0, width // 2, height)

            # Save the original image by appending "b" to the file name
            backup_path = f"{file_name}b{file_extension}"
            img.save(backup_path)

            # Save the left half by appending "a" to the file name
            left_half_img = img.crop(left_half)
            new_image_path = f"{file_name}a{file_extension}"
            left_half_img.save(new_image_path)

        # Delete the original image
        os.remove(image_path)

        app_logger.info(f"Processed: {os.path.basename(image_path)} original saved as {backup_path}, left half saved as {new_image_path}.")

        return new_image_path, backup_path
    
    except Exception as e:
        app_logger.error(f"Error processing the image: {e}")


def cropCenter(image_path):
    file_name, file_extension = os.path.splitext(image_path)

    try:
        # Open the image
        with Image.open(image_path) as img:
            width, height = img.size

            # Calculate the coordinates for the left, center, and right thirds
            third_width = width // 3
            left_half = (0, 0, third_width, height)
            center_half = (third_width, 0, 2 * third_width, height)
            right_half = (2 * third_width, 0, width, height)

            # Save the original image as backup
            backup_path = f"{file_name}b{file_extension}"
            img.save(backup_path)

            # Crop and save each third
            left_img = img.crop(left_half)
            left_image_path = f"{file_name}_left{file_extension}"
            left_img.save(left_image_path)

            center_img = img.crop(center_half)
            center_image_path = f"{file_name}_center{file_extension}"
            center_img.save(center_image_path)

            right_img = img.crop(right_half)
            right_image_path = f"{file_name}_right{file_extension}"
            right_img.save(right_image_path)

        # Delete the original image
        os.remove(image_path)

        app_logger.info(
            f"Processed: {os.path.basename(image_path)}\n"
            f"  Original saved as: {backup_path}\n"
            f"  Left third saved as: {left_image_path}\n"
            f"  Center third saved as: {center_image_path}\n"
            f"  Right third saved as: {right_image_path}"
        )

        return {
            "backup": backup_path,
            "left": left_image_path,
            "center": center_image_path,
            "right": right_image_path
        }

    except Exception as e:
        app_logger.error(f"Error processing the image: {e}")


def cropFreeForm(image_path, x, y, width, height):
    """
    Crop an image using custom coordinates.
    Renames the original to {filename}-a{ext} as backup and saves cropped as {filename}{ext}.

    Args:
        image_path: Full path to the image file
        x: X coordinate of top-left corner of crop area
        y: Y coordinate of top-left corner of crop area
        width: Width of the crop area
        height: Height of the crop area

    Returns:
        Tuple of (cropped_image_path, backup_image_path)
        - cropped_image_path: Path to the cropped image (same as original filename)
        - backup_image_path: Path to the backup of the original image (with -a suffix)
    """
    file_name, file_extension = os.path.splitext(image_path)
    backup_image_path = f"{file_name}-a{file_extension}"

    try:
        # Open the image
        with Image.open(image_path) as img:
            img_width, img_height = img.size

            # Validate and clamp coordinates to image boundaries
            x = max(0, min(int(x), img_width))
            y = max(0, min(int(y), img_height))
            width = max(1, min(int(width), img_width - x))
            height = max(1, min(int(height), img_height - y))

            # Log validation info
            app_logger.info(f"Image size: {img_width}x{img_height}, Crop: x={x}, y={y}, w={width}, h={height}")

            # Ensure crop doesn't exceed image bounds
            if x + width > img_width:
                width = img_width - x
                app_logger.warning(f"Crop width adjusted to {width} to fit within image bounds")
            if y + height > img_height:
                height = img_height - y
                app_logger.warning(f"Crop height adjusted to {height} to fit within image bounds")

            # Define the crop box (left, upper, right, lower)
            crop_box = (x, y, x + width, y + height)

            # Crop the image
            cropped_img = img.crop(crop_box)

            # Rename original to backup (with -a suffix)
            os.rename(image_path, backup_image_path)

            # Save the cropped image with the original filename
            cropped_img.save(image_path)

        app_logger.info(f"Free form crop processed: {os.path.basename(image_path)}, original backed up as {os.path.basename(backup_image_path)}")

        # Return: cropped image path (original name), backup path (with -a suffix)
        return image_path, backup_image_path

    except Exception as e:
        app_logger.error(f"Error processing free form crop: {e}")
        raise


def get_image_data_url(image_path):
    """Open an image, resize it to a height of 100 (keeping aspect ratio),
    encode it as a PNG in memory, and return a data URL."""
    try:
        with Image.open(image_path) as img:
            if img.height > 0:
                ratio = 100 / float(img.height)
                new_width = int(img.width * ratio)
                img = img.resize((new_width, 100), Image.LANCZOS)
            buffered = io.BytesIO()
            img.save(buffered, format="PNG")
            encoded = base64.b64encode(buffered.getvalue()).decode('utf-8')
            return f"data:image/png;base64,{encoded}"
    except Exception as e:
        app_logger.error(f"Error encoding image {image_path}: {e}")
        raise