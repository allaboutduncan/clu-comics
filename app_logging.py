import logging
import os
import sys

# Logging setup - use /config/logs if available, otherwise local logs directory
CONFIG_DIR = os.environ.get('CONFIG_DIR', '/config' if os.path.exists('/config') else os.getcwd())
LOG_DIR = os.path.join(CONFIG_DIR, "logs")
APP_LOG = os.path.join(LOG_DIR, "app.log")
MONITOR_LOG = os.path.join(LOG_DIR, "monitor.log")

# Ensure log directory exists
os.makedirs(LOG_DIR, exist_ok=True)

# Ensure log files exist
for log_file in [APP_LOG, MONITOR_LOG]:
    if not os.path.exists(log_file):
        with open(log_file, "w") as f:
            f.write("")  # Create an empty file

# Create logger
app_logger = logging.getLogger("app_logger")
app_logger.setLevel(logging.INFO)

# Create file handler
file_handler = logging.FileHandler(APP_LOG)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

# Create stream handler to log to stdout
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

# Add handlers to logger
if not app_logger.handlers:  # Prevent adding multiple handlers in case of multiple imports
    app_logger.addHandler(file_handler)
    app_logger.addHandler(console_handler)
    # Log the log file location
    app_logger.info(f"📋 Log files location: {LOG_DIR}")

# Suppress noisy pyrate_limiter logs (Mokkari already handles rate limits internally)
logging.getLogger("pyrate_limiter").setLevel(logging.CRITICAL)