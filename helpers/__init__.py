# Re-export everything from the original helpers.py module
# so that `from helpers import is_hidden` etc. continues to work
# now that helpers/ is a package directory.

import os
import stat
import zipfile
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import math
import shutil
from app_logging import app_logger
import subprocess
import gc
import io
from contextlib import contextmanager

#########################
# Hidden File Handling  #
#########################

def is_hidden(filepath):
    """
    Returns True if the file or directory is considered hidden.
    This function marks files as hidden if their names start with a '.' or '_',
    and it also checks the Windows hidden attribute.
    """
    name = os.path.basename(filepath)
    # Check for names starting with '.' or '_'
    if name.startswith('.') or name.startswith('_'):
        return True
    # For Windows, check the hidden attribute
    if os.name == 'nt':
        try:
            attrs = os.stat(filepath).st_file_attributes
            return bool(attrs & stat.FILE_ATTRIBUTE_HIDDEN)
        except AttributeError:
            pass
    return False

#########################
#   File Extraction     #
#########################

def unzip_file(file_path):
    """
    Extracts all files from a ZIP archive into a directory with the same name as the ZIP file.
    Uses memory-efficient streaming extraction.

    Parameters:
        zip_file_path (str): The path to the ZIP archive.

    Returns:
        str: The full path to the directory where the files were extracted.
    """
    # Remove the .zip extension to form the directory name.
    base_dir, ext = os.path.splitext(file_path)
    if ext.lower() != '.bak':
        raise ValueError("The provided file does not have a .bak extension.")

    # Create the directory if it doesn't exist.
    if not os.path.exists(base_dir):
        os.makedirs(base_dir)

    # Extract all files into the created directory using streaming.
    with zipfile.ZipFile(file_path, 'r') as zip_ref:
        # Get list of files first to avoid memory issues with large archives
        file_list = zip_ref.namelist()

        for filename in file_list:
            try:
                zip_ref.extract(filename, base_dir)
            except Exception as e:
                app_logger.warning(f"Failed to extract {filename}: {e}")
                continue

    return base_dir


def extract_rar_with_unar(rar_path, output_dir):
    """
    Extract a RAR file using the unar command-line tool.
    Returns True if extraction was successful (even with some failed files),
    False if extraction completely failed.

    :param rar_path: Path to the RAR file.
    :param output_dir: Directory to extract the contents into.
    :return: bool: True if any files were extracted successfully
    """
    try:
        # First check if unar is available
        try:
            subprocess.run(["unar", "--version"], check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            app_logger.error("unar command not found. Please install unar first.")
            raise RuntimeError("unar command not found. Please install unar first.")

        # Check if the input file exists
        if not os.path.exists(rar_path):
            app_logger.error(f"Input file does not exist: {rar_path}")
            raise RuntimeError(f"Input file does not exist: {rar_path}")

        app_logger.info(f"Extracting {rar_path} to {output_dir}")
        result = subprocess.run(
            ["unar", "-o", output_dir, "-f", rar_path],
            capture_output=True,
            text=True
        )

        stdout_msg = result.stdout.strip() if result.stdout else ""
        stderr_msg = result.stderr.strip() if result.stderr else ""

        # Check if any files were extracted successfully
        extracted_files = []
        failed_files = []

        if stdout_msg:
            for line in stdout_msg.split('\n'):
                if '... OK.' in line:
                    # Extract filename from line like "filename.jpg  (size B)... OK."
                    filename = line.split('...')[0].strip().split('  ')[0]
                    extracted_files.append(filename)
                elif '... Failed!' in line:
                    # Extract filename from line like "filename.jpg  (size B)... Failed!"
                    filename = line.split('...')[0].strip().split('  ')[0]
                    failed_files.append(filename)

        if extracted_files:
            app_logger.info(f"Successfully extracted {len(extracted_files)} files from {rar_path}")
            if failed_files:
                app_logger.warning(f"Failed to extract {len(failed_files)} files: {', '.join(failed_files)}")

            # Check if the output directory has any files
            if os.path.exists(output_dir) and any(os.listdir(output_dir)):
                app_logger.info(f"Extraction completed. Output directory: {output_dir}")
                return True
            else:
                app_logger.error("No files were actually extracted despite successful status")
                return False
        else:
            app_logger.error(f"No files were extracted from {rar_path}")
            if stderr_msg:
                app_logger.error(f"stderr: {stderr_msg}")
            return False

    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode().strip() if e.stderr else "Unknown error"
        app_logger.error(f"Failed to extract {rar_path}: {error_msg}")
        raise RuntimeError(f"Failed to extract {rar_path}: {error_msg}")
    except Exception as e:
        app_logger.error(f"Unexpected error extracting {rar_path}: {str(e)}")
        raise RuntimeError(f"Unexpected error extracting {rar_path}: {str(e)}")

#########################
#   Image Enhancement   #
#########################

@contextmanager
def safe_image_open(image_path):
    """
    Context manager for safely opening images with proper cleanup.
    """
    img = None
    try:
        img = Image.open(image_path)
        yield img
    finally:
        if img is not None:
            img.close()
        gc.collect()


def apply_gamma(image, gamma=0.9):
    """
    Apply gamma correction with memory-efficient processing.
    """
    try:
        inv = 1.0 / gamma
        table = [int(((i/255)**inv)*255) for i in range(256)]
        if image.mode == "RGB":
            table = table*3
        return image.point(table)
    except Exception as e:
        app_logger.error(f"Error applying gamma correction: {e}")
        return image


def modified_s_curve_lut(shadow_lift=0.1):
    """
    Generate lookup table for S-curve adjustment.
    """
    lut = []
    for i in range(256):
        s = 0.5 - 0.5*math.cos(math.pi*(i/255))
        s_val = 255*s
        # lift the darkest 20% by a fixed offset
        if i < 64:
            s_val = s_val + shadow_lift*(64 - i)
        # blend into original in highlights as before…
        blend = max(0, (i-128)/(127))
        new_val = (1-blend)*s_val + blend*i
        lut.append(int(round(new_val)))
    return lut


def apply_modified_s_curve(image):
    """
    Apply modified S-curve with memory-efficient processing.
    """
    try:
        single_lut = modified_s_curve_lut()

        # If the image is grayscale, apply the LUT directly.
        if image.mode == "L":
            return image.point(single_lut)
        # For RGB images, replicate the LUT for each channel.
        elif image.mode == "RGB":
            full_lut = single_lut * 3
            return image.point(full_lut)
        # For RGBA images, apply the curve to RGB channels only.
        elif image.mode == "RGBA":
            r, g, b, a = image.split()
            r = r.point(single_lut)
            g = g.point(single_lut)
            b = b.point(single_lut)
            result = Image.merge("RGBA", (r, g, b, a))
            # Clean up intermediate images
            r.close()
            g.close()
            b.close()
            a.close()
            return result
        else:
            raise ValueError(f"Unsupported image mode: {image.mode}")
    except Exception as e:
        app_logger.error(f"Error applying S-curve: {e}")
        return image


def enhance_image(path):
    """
    Enhanced image processing with memory management and error handling.
    """
    try:
        # Check file size to avoid processing extremely large images
        file_size = os.path.getsize(path)
        max_file_size = 100 * 1024 * 1024  # 100MB limit

        if file_size > max_file_size:
            app_logger.warning(f"Image file too large ({file_size / 1024 / 1024:.1f}MB), skipping enhancement: {path}")
            return None

        with safe_image_open(path) as img:
            # Check image dimensions
            width, height = img.size
            max_pixels = 50_000_000  # 50MP limit

            if width * height > max_pixels:
                app_logger.warning(f"Image too large ({width}x{height}), resizing before enhancement: {path}")
                # Calculate new dimensions maintaining aspect ratio
                ratio = (max_pixels / (width * height)) ** 0.5
                new_width = int(width * ratio)
                new_height = int(height * ratio)
                img = img.resize((new_width, new_height), Image.LANCZOS)

            # Apply enhancements with memory management
            enhanced = apply_modified_s_curve(img)
            enhanced = apply_gamma(enhanced, gamma=0.9)
            enhanced = ImageEnhance.Brightness(enhanced).enhance(1.03)
            enhanced = ImageEnhance.Contrast(enhanced).enhance(1.05)
            enhanced = ImageOps.autocontrast(enhanced, cutoff=1)

            return enhanced

    except Exception as e:
        app_logger.error(f"Error enhancing image {path}: {e}")
        return None


def enhance_image_streaming(path, output_path=None):
    """
    Stream-based image enhancement for very large images.
    Processes the image in chunks to minimize memory usage.
    """
    try:
        if output_path is None:
            output_path = path

        with safe_image_open(path) as img:
            # For very large images, process in tiles
            width, height = img.size
            tile_size = 2048  # Process in 2K tiles

            if width * height > 100_000_000:  # 100MP threshold for tiled processing
                app_logger.info(f"Using tiled processing for large image: {path}")

                # Create output image with same mode
                output_img = Image.new(img.mode, img.size)

                # Process image in tiles
                for y in range(0, height, tile_size):
                    for x in range(0, width, tile_size):
                        # Extract tile
                        tile = img.crop((x, y, min(x + tile_size, width), min(y + tile_size, height)))

                        # Enhance tile
                        enhanced_tile = enhance_image_tile(tile)

                        # Paste enhanced tile back
                        output_img.paste(enhanced_tile, (x, y))

                        # Clean up tile
                        tile.close()
                        enhanced_tile.close()

                        # Force garbage collection periodically
                        if (x + tile_size) % (tile_size * 4) == 0:
                            gc.collect()

                # Save and clean up
                output_img.save(output_path, optimize=True)
                output_img.close()

            else:
                # Use regular enhancement for smaller images
                enhanced = enhance_image(path)
                if enhanced:
                    enhanced.save(output_path, optimize=True)
                    enhanced.close()

        return True

    except Exception as e:
        app_logger.error(f"Error in streaming enhancement {path}: {e}")
        return False


def enhance_image_tile(tile):
    """
    Enhance a single image tile with basic operations.
    """
    try:
        enhanced = apply_modified_s_curve(tile)
        enhanced = apply_gamma(enhanced, gamma=0.9)
        enhanced = ImageEnhance.Brightness(enhanced).enhance(1.03)
        enhanced = ImageEnhance.Contrast(enhanced).enhance(1.05)
        return enhanced
    except Exception as e:
        app_logger.error(f"Error enhancing tile: {e}")
        return tile


def create_thumbnail_streaming(image_path, max_size=(100, 100), quality=85):
    """
    Create thumbnail with streaming approach to avoid loading large images entirely into memory.
    """
    try:
        with safe_image_open(image_path) as img:
            # Calculate thumbnail size maintaining aspect ratio
            img.thumbnail(max_size, Image.LANCZOS)

            # Convert to RGB if necessary for JPEG
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                img = background

            # Save to bytes buffer
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=quality, optimize=True)
            buffer.seek(0)

            return buffer.getvalue()

    except Exception as e:
        app_logger.error(f"Error creating thumbnail for {image_path}: {e}")
        return None
