import os
import sys
from app_logging import app_logger

def delete_file(file_path):
    """
    Deletes the file at the specified file_path.

    Args:
        file_path (str): The path to the file to be deleted.

    Returns:
        None

    Raises:
        FileNotFoundError: If the file does not exist.
        PermissionError: If the file cannot be deleted due to permission issues.
        Exception: For any other exceptions that may occur.
    """
    app_logger.info(f"********************// Delete File //********************")

    try:
        if not os.path.isfile(file_path):
            app_logger.error(f"ERROR: The file '{file_path}' does not exist.", file=sys.stderr)
            sys.exit(1)
        
        os.remove(file_path)
        app_logger.info(f"SUCCESS: The file '{file_path}' has been deleted.")
    
    except PermissionError:
        app_logger.error(f"ERROR: Permission denied while trying to delete '{file_path}'.", file=sys.stderr)
        sys.exit(1)
    
    except Exception as e:
        app_logger.error(f"ERROR: An unexpected error occurred: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    if len(sys.argv) != 2:
        app_logger.error("ERROR: Incorrect number of arguments.", file=sys.stderr)
        app_logger.error("Usage: python delete.py <file_path>", file=sys.stderr)
        sys.exit(1)
    
    file_path = sys.argv[1]
    delete_file(file_path)

if __name__ == "__main__":
    main()
