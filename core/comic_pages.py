"""Reading one page out of a RAR archive when ``rarfile`` cannot.

The reader's page route (``routes/reading.py``) asks ``rarfile`` first. A solid
or damaged archive makes that return truncated bytes or raise, and then the
page comes from a command-line tool instead: single-entry extraction with
unrar, 7z and unar in turn, and as a last resort a full extraction that is
cached, because a solid archive has to be unpacked from the start for every
page.

This lived in app.py, which cannot be imported in tests, so the only tests it
had were AST assertions. Put new logic here, not in the route.

- ``_rar_cache`` is single-flight per archive with reference-counted eviction
  (``core/rar_extract_cache.py``): a page is read while its reference is held,
  so eviction cannot remove the folder mid-read.
- ``_is_valid_image_data`` is what makes each step fall through: a tool that
  exits 0 with half a JPEG has not produced the page.
"""
import atexit
import os
import shutil
import subprocess
import tempfile

from core.app_logging import app_logger
from core.rar_extract_cache import RarExtractCache
from helpers.library import is_within_roots


def _find_extracted_file(directory, filename):
    """Walk *directory* and return the path of the first file matching *filename* (case-insensitive)."""
    target = filename.lower()
    for root, _dirs, files in os.walk(directory):
        for f in files:
            if f.lower() == target:
                full = os.path.join(root, f)
                # Path traversal guard (realpath, separator-bounded)
                if is_within_roots(full, [directory]):
                    return full
    return None


def _is_valid_image_data(data):
    """Check if image data appears complete (not truncated)."""
    if not data or len(data) < 10:
        return False
    # JPEG: must start with FF D8 and end with FF D9
    if data[:2] == b'\xff\xd8':
        return data[-2:] == b'\xff\xd9'
    # PNG: must start with PNG signature and end with IEND chunk
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return b'IEND' in data[-12:]
    # Other formats — assume valid if non-empty
    return True



def _try_full_extraction(tool_cmd, rar_path, tmp_dir):
    """Run a full-archive extraction with the given tool. Returns True if files were produced."""
    try:
        result = subprocess.run(tool_cmd, capture_output=True, timeout=120)
        has_files = any(os.scandir(tmp_dir))
        if has_files:
            tool_name = tool_cmd[0]
            if result.returncode != 0:
                app_logger.warning(
                    f"{tool_name} full extraction partial success (rc={result.returncode}) for {rar_path}"
                )
            else:
                app_logger.info(f"{tool_name} full extraction succeeded for {rar_path}")
            return True
    except FileNotFoundError:
        pass  # tool not installed
    except Exception as e:
        app_logger.warning(f"{tool_cmd[0]} full extraction failed for {rar_path}: {e}")
    return False


def _extract_full_rar_into(rar_path, dest_dir):
    """Extract the whole RAR into *dest_dir* (an empty folder the cache owns).

    Tries unrar first (proprietary, best solid-archive support), then 7z, then
    unar. Each attempt gets its own sub-folder, created and (on failure)
    removed here, so a discarded attempt never mixes with the next one.
    Returns True when an attempt produced files.
    """
    tools = [
        ["unrar", "x", "-y", "-o+", rar_path],       # unrar x -y -o+ archive.rar tmp_dir/
        ["7z", "x", f"-y", rar_path],                  # 7z x -y archive.rar -o{tmp_dir}
        ["unar", "-f", "-o", None, rar_path],           # unar -f -o tmp_dir archive.rar
    ]

    for attempt, tool_cmd in enumerate(tools):
        tmp_dir = os.path.join(dest_dir, f"attempt{attempt}")
        os.mkdir(tmp_dir)
        # Fix up output dir argument per tool
        tool_name = tool_cmd[0]
        if tool_name == "unrar":
            cmd = tool_cmd + [tmp_dir + "/"]
        elif tool_name == "7z":
            cmd = tool_cmd[:2] + [f"-o{tmp_dir}"] + tool_cmd[2:]
        else:  # unar
            cmd = tool_cmd.copy()
            cmd[3] = tmp_dir  # replace None placeholder

        if _try_full_extraction(cmd, rar_path, tmp_dir):
            return True
        shutil.rmtree(tmp_dir, ignore_errors=True)

    app_logger.error(f"All full extraction tools failed for {rar_path}")
    return False


_rar_cache = RarExtractCache(_extract_full_rar_into, max_entries=3)
atexit.register(_rar_cache.shutdown)


def _extract_single_rar_entry(rar_path, entry_name):
    """Extract a single file from a RAR archive.

    Fallback chain:
    1. unrar single-entry (proprietary, handles solid archives)
    2. 7z single-entry
    3. unar single-entry
    4. Full-archive extraction (cached) + file lookup
    Each step validates image completeness before returning.
    """
    rar_path = os.path.realpath(rar_path)
    from helpers.library import is_allowed_path
    if not is_allowed_path(rar_path):
        app_logger.error(f"Path not in allowed directory: {rar_path}")
        return None

    safe_name = os.path.basename(entry_name)
    if not safe_name:
        app_logger.error(f"Invalid RAR entry name: {entry_name}")
        return None

    # --- Attempts 1-3: single-entry extraction with each tool ---
    single_tools = [
        ["unrar", "e", "-y", "-o+"],  # unrar e -y -o+ archive entry dest/
        ["7z", "e", "-y"],             # 7z e -y -o{dir} archive entry
        ["unar", "-f", "-o"],          # unar -f -o dir archive entry
    ]
    for tool_cmd in single_tools:
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tool_name = tool_cmd[0]
                if tool_name == "unrar":
                    cmd = tool_cmd + [rar_path, entry_name, tmp_dir + "/"]
                elif tool_name == "7z":
                    cmd = tool_cmd + [f"-o{tmp_dir}", rar_path, entry_name]
                else:  # unar
                    cmd = tool_cmd + [tmp_dir, rar_path, entry_name]

                result = subprocess.run(cmd, capture_output=True, timeout=60)
                found = _find_extracted_file(tmp_dir, safe_name)
                if found:
                    with open(found, "rb") as f:
                        data = f.read()
                    if _is_valid_image_data(data):
                        return data
                    app_logger.warning(f"{tool_name} extracted truncated data for {entry_name}")
        except FileNotFoundError:
            continue  # tool not installed
        except Exception as e:
            app_logger.warning(f"{tool_cmd[0]} single-entry extraction failed for {entry_name}: {e}")

    # --- Attempt 4: full-archive extraction (cached) ---
    app_logger.info(
        f"Single-entry extraction failed for {entry_name}, trying full archive extraction"
    )
    # The folder stays in place for the whole block: the cache holds a
    # reference until it exits, so eviction cannot remove it mid-read.
    with _rar_cache.acquire(rar_path) as cache_dir:
        found = _find_extracted_file(cache_dir, safe_name) if cache_dir else None
        if found:
            try:
                with open(found, "rb") as f:
                    data = f.read()
                if _is_valid_image_data(data):
                    return data
                app_logger.warning(f"Full extraction produced truncated data for {entry_name}")
            except Exception as e:
                app_logger.error(f"Failed to read extracted file {found}: {e}")

    app_logger.error(f"All extraction methods failed for {entry_name} in {rar_path}")
    return None
