from helpers import is_hidden
from .enhance_single import enhance_comic
import os
from app_logging import app_logger
import sys


def enhance_directory(directory):
    """
    Processes all files (no subdirectories) in the given directory by calling
    enhance_comic(file_path) on each file. Only files directly in 'directory_path'
    will be processed—no subdirectories are traversed.
    """
    # List all files in the directory (not diving into subdirectories).
    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        
        # Skip hidden files or directories. Then ensure we are only processing files.
        if not is_hidden(file_path) and os.path.isfile(file_path):
            enhance_comic(file_path)


if __name__ == "__main__":
    # The directory path is passed as the first argument
    if len(sys.argv) < 2:
        app_logger.info("No directory provided!")
    else:
        directory = sys.argv[1]
        enhance_directory(directory)
        app_logger.info("********************// Enhance Directory //********************")
        app_logger.info(f"Enhance Images starting for directory: {directory}")