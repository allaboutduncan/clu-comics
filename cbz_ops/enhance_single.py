from PIL import Image, ImageEnhance, ImageFilter
from helpers import is_hidden, unzip_file, enhance_image, enhance_image_streaming, safe_image_open
import os
import zipfile
import shutil
from app_logging import app_logger
import sys
from config import config, load_config
import gc

load_config()
skipped_exts = config.get("SETTINGS", "SKIPPED_FILES", fallback="")
deleted_exts = config.get("SETTINGS", "DELETED_FILES", fallback="")

skippedFiles = [ext.strip().lower() for ext in skipped_exts.split(",") if ext.strip()]
deletedFiles = [ext.strip().lower() for ext in deleted_exts.split(",") if ext.strip()]

def enhance_comic(file_path):
    """
    Enhanced comic processing with memory-efficient operations.
    """
    # If the file is hidden, skip it
    if is_hidden(file_path):
        print(f"Skipping hidden file: {file_path}")
        return

    # Process only if the file is a ZIP archive with a .cbz extension.
    if file_path.lower().endswith('.cbz'):
        enhance_cbz_file(file_path)
    else:
        # Enhance a single image file using streaming approach
        enhance_single_image(file_path)


def enhance_cbz_file(file_path):
    """
    Enhanced CBZ file processing with memory management.
    """
    # Determine the backup file path (with .bak extension).
    bak_file_path = os.path.splitext(file_path)[0] + '.bak'
    base_cbz_path = os.path.splitext(file_path)[0] + '.cbz'
    
    try:
        # Check if the original .cbz file exists.
        if os.path.exists(file_path):
            # Rename the original .cbz file to .bak before extraction.
            os.rename(file_path, bak_file_path)
            app_logger.info(f"Renamed '{file_path}' to '{bak_file_path}'")
        elif os.path.exists(bak_file_path):
            # The file may have already been renamed.
            app_logger.info(f"File '{file_path}' not found; using backup '{bak_file_path}'")
        else:
            # Neither file exists – raise an error.
            raise FileNotFoundError(f"Neither {file_path} nor {bak_file_path} exists.")

        # Extract the ZIP archive from the backup file using streaming.
        extracted_dir = unzip_file(bak_file_path)
        app_logger.info(f"Extracted to: {extracted_dir}")

        # Find and filter files in the extracted directory.
        image_files = []
        for root, _, files in os.walk(extracted_dir):
            for file in files:
                file_path = os.path.join(root, file)
                ext = os.path.splitext(file)[1].lower()

                # Delete files with deleted extensions
                if ext in deletedFiles:
                    try:
                        os.remove(file_path)
                        app_logger.info(f"Deleted unwanted file: {file_path}")
                    except Exception as e:
                        app_logger.error(f"Error deleting file {file_path}: {e}")
                    continue

                # Skip files with skipped extensions
                if ext in skippedFiles:
                    app_logger.info(f"Skipped file: {file_path}")
                    continue

                # Only include image files
                if ext in ('.png', '.jpg', '.jpeg', '.gif'):
                    image_files.append(file_path)

        # Enhance each image file using streaming approach
        enhanced_count = 0
        for i, image_file in enumerate(image_files):
            try:
                app_logger.info(f"Enhancing image {i+1}/{len(image_files)}: {os.path.basename(image_file)}")
                
                # Use streaming enhancement for large images
                file_size = os.path.getsize(image_file)
                if file_size > 50 * 1024 * 1024:  # 50MB threshold
                    app_logger.info(f"Using streaming enhancement for large file: {os.path.basename(image_file)}")
                    success = enhance_image_streaming(image_file, image_file)
                    if success:
                        enhanced_count += 1
                else:
                    # Use regular enhancement for smaller images
                    enhanced_image = enhance_image(image_file)
                    if enhanced_image:
                        # Build a temporary filename that keeps the original extension.
                        base, ext = os.path.splitext(image_file)
                        tmp_path = base + "_tmp" + ext  # e.g., image_tmp.jpg
                        enhanced_image.save(tmp_path, optimize=True)
                        # Atomically replace the original image with the enhanced one.
                        os.replace(tmp_path, image_file)
                        enhanced_image.close()
                        enhanced_count += 1
                        app_logger.info(f"Enhanced: {image_file}")
                    else:
                        app_logger.warning(f"Failed to enhance: {image_file}")
                
                # Force garbage collection periodically
                if (i + 1) % 10 == 0:
                    gc.collect()
                    
            except Exception as e:
                app_logger.error(f"Error enhancing {image_file}: {e}")
                continue
        
        app_logger.info(f"Successfully enhanced {enhanced_count}/{len(image_files)} images")
        
        # Compress the enhanced files back into a ZIP archive with a .cbz extension.
        enhanced_cbz_path = base_cbz_path
        create_enhanced_cbz(extracted_dir, enhanced_cbz_path)
        
        # Clean up the extracted directory.
        cleanup_extracted_dir(extracted_dir)
        
        # Once processing is complete, delete the backup (.bak) file.
        try:
            os.remove(bak_file_path)
            app_logger.info(f"Deleted backup file '{bak_file_path}'")
        except Exception as e:
            app_logger.error(f"Error deleting backup file: {e}")
            
        # Force final garbage collection
        gc.collect()
        
    except Exception as e:
        app_logger.error(f"Error processing CBZ file {file_path}: {e}")
        # Clean up on error
        if os.path.exists(extracted_dir):
            cleanup_extracted_dir(extracted_dir)


def enhance_single_image(file_path):
    """
    Enhanced single image processing with memory management.
    """
    try:
        file_size = os.path.getsize(file_path)
        if file_size > 100 * 1024 * 1024:  # 100MB threshold
            app_logger.info(f"Using streaming enhancement for large single image: {file_path}")
            success = enhance_image_streaming(file_path, file_path)
            if success:
                app_logger.info(f"Enhanced single image: {file_path}")
            else:
                app_logger.error(f"Failed to enhance single image: {file_path}")
        else:
            enhanced_image = enhance_image(file_path)
            if enhanced_image:
                # Create temporary file
                base, ext = os.path.splitext(file_path)
                tmp_path = base + "_tmp" + ext
                enhanced_image.save(tmp_path, optimize=True)
                # Atomically replace original
                os.replace(tmp_path, file_path)
                enhanced_image.close()
                app_logger.info(f"Enhanced single image: {file_path}")
            else:
                app_logger.error(f"Failed to enhance single image: {file_path}")
    except Exception as e:
        app_logger.error(f"Error enhancing single image {file_path}: {e}")


def create_enhanced_cbz(extracted_dir, enhanced_cbz_path):
    """
    Create enhanced CBZ file using streaming approach.
    """
    try:
        with zipfile.ZipFile(enhanced_cbz_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as cbz_file:
            # Collect all files first
            file_list = []
            for root, _, files in os.walk(extracted_dir):
                for file in files:
                    full_path = os.path.join(root, file)
                    relative_path = os.path.relpath(full_path, extracted_dir)
                    file_list.append((relative_path, full_path))
            
            # Sort files for consistent ordering
            file_list.sort(key=lambda x: x[0])
            
            # Add files to zip one by one
            for relative_path, full_path in file_list:
                try:
                    cbz_file.write(full_path, relative_path)
                except Exception as e:
                    app_logger.warning(f"Failed to add {relative_path} to CBZ: {e}")
                    continue
        
        app_logger.info(f"Compressed to: {enhanced_cbz_path}")
        if not os.path.exists(enhanced_cbz_path):
            app_logger.error(f"Failed to create CBZ at: {enhanced_cbz_path}")
        else:
            # Regenerate thumbnail for the enhanced file
            try:
                import hashlib
                from database import get_db_connection
                
                file_hash = hashlib.md5(enhanced_cbz_path.encode()).hexdigest()
                shard_dir = file_hash[:2]
                cache_dir = config.get("SETTINGS", "CACHE_DIR", fallback="/cache")
                cache_subdir = os.path.join(cache_dir, 'thumbnails', shard_dir)
                cache_path = os.path.join(cache_subdir, f"{file_hash}.jpg")
                os.makedirs(cache_subdir, exist_ok=True)
                
                with zipfile.ZipFile(enhanced_cbz_path, 'r') as zf:
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
                                file_mtime = int(os.path.getmtime(enhanced_cbz_path))
                                conn.execute(
                                    'INSERT OR REPLACE INTO thumbnail_jobs (path, status, file_mtime, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)',
                                    (enhanced_cbz_path, 'completed', file_mtime)
                                )
                                conn.commit()
                                conn.close()
                            app_logger.info(f"Thumbnail regenerated for {enhanced_cbz_path}")
            except Exception as e:
                app_logger.error(f"Error regenerating thumbnail: {e}")
            
    except Exception as e:
        app_logger.error(f"Error creating enhanced CBZ: {e}")


def cleanup_extracted_dir(extracted_dir):
    """
    Clean up extracted directory with error handling.
    """
    try:
        if os.path.exists(extracted_dir):
            shutil.rmtree(extracted_dir)
            app_logger.info(f"Cleaned up extracted directory: {extracted_dir}")
    except Exception as e:
        app_logger.error(f"Error cleaning up extracted directory {extracted_dir}: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        app_logger.error("No file provided!")
    else:
        file_path = sys.argv[1]
        app_logger.info("********************// Enhance Single //********************")
        app_logger.info(f"Starting Image Enhancement for: {file_path}")
        enhance_comic(file_path)