import os
import json
import uuid
import zipfile
import shutil
import io
import base64
from flask import render_template_string, request, jsonify
from PIL import Image
from core.app_logging import app_logger
from core.config import config, load_config
from helpers import create_thumbnail_streaming, safe_image_open, open_zip_for_write
import gc


def _safe_join(base_folder, rel_path):
    """Resolve rel_path under base_folder, rejecting traversal escapes.

    Returns the absolute path or raises ValueError.
    """
    base_abs = os.path.abspath(base_folder)
    candidate = os.path.abspath(os.path.join(base_abs, rel_path))
    if candidate != base_abs and not candidate.startswith(base_abs + os.sep):
        raise ValueError(f"Path escapes base folder: {rel_path}")
    return candidate


def _validate_final_name(name):
    """Reject final filenames that could escape the base folder."""
    if not name or not isinstance(name, str):
        raise ValueError("final_name must be a non-empty string")
    if '/' in name or '\\' in name or '\0' in name:
        raise ValueError(f"final_name contains path separators or NUL: {name!r}")
    if name in ('.', '..'):
        raise ValueError(f"final_name reserved: {name!r}")

load_config()
skipped_exts = config.get("SETTINGS", "SKIPPED_FILES", fallback="")
deleted_exts = config.get("SETTINGS", "DELETED_FILES", fallback="")

skippedFiles = [ext.strip().lower() for ext in skipped_exts.split(",") if ext.strip()]
deletedFiles = [ext.strip().lower() for ext in deleted_exts.split(",") if ext.strip()]

# Partial template for the modal body (the grid of Bootstrap Cards)
modal_body_template = '''
    {% for card in file_cards %}
      <div class="col" data-rel-path="{{ card.rel_path }}">
        <div class="card h-100 shadow-sm cbz-edit-card">
          <div class="cbz-edit-thumb-wrap">
            {% if card.img_data %}
              <img src="{{ card.img_data }}" class="cbz-edit-thumb" alt="{{ card.filename }}">
            {% else %}
              <img src="https://via.placeholder.com/400x600" class="cbz-edit-thumb" alt="No image">
            {% endif %}
          </div>
          <div class="card-body d-flex flex-column p-2">
            <p class="card-text small text-break mb-2 cbz-edit-filename-wrap">
                <span class="editable-filename" data-rel-path="{{ card.rel_path }}" onclick="CLU.enableFilenameEdit(this)">{{ card.filename }}</span>
                <input type="text" class="form-control d-none filename-input form-control-sm" value="{{ card.filename }}" data-rel-path="{{ card.rel_path }}">
            </p>
            <div class="btn-group btn-group-sm w-100 mt-auto" role="group">
              <button type="button" class="btn btn-outline-primary" onclick="CLU.cropImageFreeForm(this)" title="Free Form Crop"><i class="bi bi-crop"></i></button>
              <button type="button" class="btn btn-outline-primary" onclick="CLU.cropImageLeft(this)" title="Crop Left"><i class="bi bi-arrow-bar-left"></i></button>
              <button type="button" class="btn btn-outline-primary" onclick="CLU.cropImageCenter(this)" title="Crop Center"><i class="bi bi-bounding-box"></i></button>
              <button type="button" class="btn btn-outline-primary" onclick="CLU.cropImageRight(this)" title="Crop Right"><i class="bi bi-arrow-bar-right"></i></button>
              <button type="button" class="btn btn-outline-danger" onclick="CLU.deleteCardImage(this)" title="Delete"><i class="bi bi-trash"></i></button>
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
                    thumbnail_data = create_thumbnail_streaming(full_path, max_size=(400, 600), quality=85)
                    
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
    pending_deletes_raw = request.form.get('pending_deletes', '[]')
    pending_order_raw = request.form.get('pending_order', '[]')

    if not folder_name or not zip_file_path or not original_file_path:
        return "Missing required data", 400

    # Parse + validate pending operations BEFORE touching disk.
    try:
        pending_deletes = json.loads(pending_deletes_raw) if pending_deletes_raw else []
        pending_order = json.loads(pending_order_raw) if pending_order_raw else []
        if not isinstance(pending_deletes, list):
            raise ValueError("pending_deletes must be a list")
        if not isinstance(pending_order, list):
            raise ValueError("pending_order must be a list")
        # Validate every delete resolves under folder_name.
        for rel in pending_deletes:
            if not isinstance(rel, str):
                raise ValueError("pending_deletes entries must be strings")
            _safe_join(folder_name, rel)
        # Validate every order entry.
        for entry in pending_order:
            if not isinstance(entry, dict):
                raise ValueError("pending_order entries must be objects")
            rel = entry.get('rel_path')
            final = entry.get('final_name')
            if not isinstance(rel, str):
                raise ValueError("pending_order rel_path must be a string")
            _safe_join(folder_name, rel)
            _validate_final_name(final)
    except (ValueError, json.JSONDecodeError) as e:
        app_logger.error(f"Invalid pending operations: {e}")
        return jsonify({"success": False, "error": f"Invalid pending operations: {e}"}), 400

    try:
        # ── Apply pending deletes ────────────────────────────────────
        for rel in pending_deletes:
            target = _safe_join(folder_name, rel)
            if os.path.isfile(target):
                try:
                    os.remove(target)
                    app_logger.info(f"Pending-delete applied: {rel}")
                except Exception as e:
                    app_logger.warning(f"Failed to delete {rel}: {e}")
            else:
                app_logger.info(f"Pending-delete target missing (skipped): {rel}")

        # ── Apply pending renames in two phases ──────────────────────
        # Phase A: source → unique temp name (avoids collisions)
        # Phase B: temp name → final_name (flat in folder_name)
        phase_a = []   # list of (orig_abs, temp_abs, final_name)
        deleted_set = set(pending_deletes)
        for entry in pending_order:
            rel = entry['rel_path']
            if rel in deleted_set:
                continue
            final_name = entry['final_name']
            src_abs = _safe_join(folder_name, rel)
            if not os.path.isfile(src_abs):
                app_logger.warning(f"Pending-order source missing (skipped): {rel}")
                continue
            target_abs = os.path.join(os.path.abspath(folder_name), final_name)
            # No-op when source basename already matches final and it's already at the root.
            if os.path.abspath(src_abs) == target_abs:
                continue
            temp_abs = os.path.join(os.path.abspath(folder_name), f".__reorder_{uuid.uuid4().hex}.tmp")
            phase_a.append((src_abs, temp_abs, target_abs, rel, final_name))

        # Execute phase A
        for src_abs, temp_abs, _target_abs, rel, _final in phase_a:
            try:
                os.rename(src_abs, temp_abs)
            except Exception as e:
                app_logger.error(f"Phase A rename failed for {rel}: {e}")
                raise

        # Execute phase B
        for _src_abs, temp_abs, target_abs, rel, final_name in phase_a:
            try:
                # If a stale file sits at target_abs (e.g., another entry's source
                # we already temped), this is fine — that file was renamed in
                # phase A so it no longer occupies target_abs. If something else
                # blocks, raise.
                os.rename(temp_abs, target_abs)
                app_logger.info(f"Pending-rename applied: {rel} -> {final_name}")
            except Exception as e:
                app_logger.error(f"Phase B rename failed for {rel} -> {final_name}: {e}")
                raise

        # Step 6: Rename the original .zip file to .bak.
        bak_file_path = zip_file_path + '.bak'
        os.rename(zip_file_path, bak_file_path)
        
        # Step 7: Re-compress the folder contents into a .cbz file (sorted).
        # Use streaming approach to avoid loading all files into memory
        with open_zip_for_write(original_file_path, zipfile.ZIP_DEFLATED, compresslevel=6) as cbz:
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
            from core.database import get_db_connection
            
            file_hash = hashlib.md5(original_file_path.encode('utf-8'), usedforsecurity=False).hexdigest()
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


def get_image_data_url(image_path, target_height=600):
    """Open an image, resize it to the given height (keeping aspect ratio),
    encode it as a PNG in memory, and return a data URL.

    Default target_height matches the edit-modal main thumbnail (600px) so
    crop-result cards render at the same size as the rest of the grid.
    """
    try:
        with Image.open(image_path) as img:
            if img.height > 0 and img.height > target_height:
                ratio = target_height / float(img.height)
                new_width = max(1, int(img.width * ratio))
                img = img.resize((new_width, target_height), Image.LANCZOS)
            buffered = io.BytesIO()
            img.save(buffered, format="PNG")
            encoded = base64.b64encode(buffered.getvalue()).decode('utf-8')
            return f"data:image/png;base64,{encoded}"
    except Exception as e:
        app_logger.error(f"Error encoding image {image_path}: {e}")
        raise