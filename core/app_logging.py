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


def read_log_tail(path, pos=None, max_lines=1000, max_bytes=262144):
    """Read new complete lines from a log file for incremental polling.

    Returns ``(lines, new_pos, reset)`` where ``lines`` is a list of decoded
    strings (no trailing newlines), ``new_pos`` is the byte offset the caller
    should send back on its next poll, and ``reset`` signals the client to
    replace its buffer rather than append.

    The file is read in **binary** mode so byte offsets are exact (text-mode
    ``tell()`` returns opaque cookies, not byte counts). Only bytes up to the
    last newline are consumed, so a half-written final line is never emitted
    until its newline arrives.

    - Missing file            -> ``([], 0, False)``.
    - ``pos is None``         -> initial load: the last ``max_lines`` complete
                                 lines, ``new_pos == file_size``.
    - ``pos > file_size``     -> file was cleared/rotated: a fresh tail with
                                 ``reset=True``.
    - ``pos == file_size``    -> no new data: ``([], pos, False)``.
    - otherwise               -> complete lines from ``pos`` onward, ``new_pos``
                                 advanced to just past the last newline consumed.
    """
    try:
        file_size = os.path.getsize(path)
    except OSError:
        return [], 0, False

    reset = False

    # Initial load, or file truncated/rotated since the caller's last poll.
    if pos is None or pos > file_size:
        reset = pos is not None
        return _tail_lines(path, file_size, max_lines, max_bytes), file_size, reset

    if pos == file_size:
        return [], pos, False

    with open(path, "rb") as f:
        f.seek(pos)
        chunk = f.read(file_size - pos)

    # Only consume up to the last newline; leave any partial trailing line.
    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        # No complete line yet (partial line still being written).
        return [], pos, False

    new_pos = pos + last_nl + 1
    text = chunk[:last_nl].decode("utf-8", errors="replace")
    lines = text.splitlines()  # handles \n and \r\n
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines, new_pos, reset


def _tail_lines(path, file_size, max_lines, max_bytes):
    """Return the last ``max_lines`` complete lines of ``path``.

    Reads backward from EOF in chunks (bounded by ``max_bytes``) so a huge log
    file never has to be read in full.
    """
    if file_size <= 0:
        return []

    read_size = min(file_size, max_bytes)
    with open(path, "rb") as f:
        # Read enough from the end to (usually) contain max_lines lines,
        # growing the window until we have enough lines or hit the cap.
        data = b""
        position = file_size
        while position > 0 and data.count(b"\n") <= max_lines and len(data) < max_bytes:
            position = max(0, position - read_size)
            f.seek(position)
            data = f.read(file_size - position)
            if position == 0:
                break

    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-max_lines:]