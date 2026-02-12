import os
import sys
import zipfile
import shutil
from PIL import Image, ImageFilter
from app_logging import app_logger


def handle_cbz_file(file_path):
    """
    Handle the conversion of a .cbz file: unzip, process images, compress, and clean up.

    :param file_path: Path to the .cbz file.
    :return: None
    """
    app_logger.info(f"********************// Add Blank Image //********************")
    app_logger.info(f"-- Handling CBZ file: {file_path}")
    
    if not file_path.lower().endswith('.cbz'):
        app_logger.error("Provided file is not a CBZ file.")
        return

    base_name = os.path.splitext(file_path)[0]  # Removes the .cbz extension
    zip_path = base_name + '.zip'
    folder_name = base_name + '_folder'
    
    app_logger.info(f"Processing CBZ: {file_path} -> {zip_path}")

    try:
        # Step 1: Rename .cbz to .zip
        os.rename(file_path, zip_path)

        # Step 2: Create a folder with the file name
        os.makedirs(folder_name, exist_ok=True)

        # Step 3: Unzip the .zip file contents into the folder
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(folder_name)
        
        # Step 4: Add the new image to the folder
        add_image_to_folder(folder_name)

        # Step 5: Rename the original .zip file to .bak
        bak_file_path = zip_path + '.bak'
        os.rename(zip_path, bak_file_path)

        # Step 6: Compress the folder contents back into a .cbz file
        with zipfile.ZipFile(file_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(folder_name):
                for file in files:
                    file_path_in_folder = os.path.join(root, file)
                    arcname = os.path.relpath(file_path_in_folder, folder_name)
                    zf.write(file_path_in_folder, arcname)

        app_logger.info(f"Successfully re-compressed: {file_path}")

        # Step 7: Delete the .bak file
        os.remove(bak_file_path)

    except Exception as e:
        app_logger.error(f"Failed to process {file_path}: {e}")
    finally:
        # Clean up the temporary folder
        if os.path.exists(folder_name):
            shutil.rmtree(folder_name)


def add_image_to_folder(folder_path):
    """
    Add the specific image "/app/images/zzzz9999.png" to the given folder.

    :param folder_path: Path to the folder where the image will be added.
    :return: None
    """
    # Define the fixed path to the image to be added
    source_image_path = "/app/app-images/zzzz9999.png"
    
    if not os.path.exists(source_image_path):
        app_logger.error(f"The image {source_image_path} does not exist.")
        return

    # Define the destination path with the fixed image name
    destination_image_path = os.path.join(folder_path, "zzzz9999.png")
    
    try:
        shutil.copy(source_image_path, destination_image_path)
        app_logger.info(f"Added image: {destination_image_path}")
    except Exception as e:
        app_logger.error(f"Failed to add image zzzzz9999.png: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        app_logger.error("No file provided!")
    else:
        file_path = sys.argv[1]
        handle_cbz_file(file_path)
