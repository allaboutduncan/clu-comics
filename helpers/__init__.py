# Re-export everything from the original helpers.py module
# so that `from helpers import is_hidden` etc. continues to work
# now that helpers/ is a package directory.

import os
import re
import stat
import tempfile
import zipfile
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import math
import shutil
from core.app_logging import app_logger
import subprocess
import gc
import io
from contextlib import contextmanager

#########################
# Hidden File Handling  #
#########################

_hidden_directories = None

def _get_hidden_directories():
    global _hidden_directories
    if _hidden_directories is None:
        reload_hidden_directories()
    return _hidden_directories

def reload_hidden_directories():
    """Reload hidden directories from user_preferences. Call after preference changes."""
    global _hidden_directories
    try:
        from core.database import get_user_preference
        dirs = get_user_preference("hidden_directories", default=["@eaDir"])
        if isinstance(dirs, list):
            _hidden_directories = set(d.strip() for d in dirs if d.strip())
        elif isinstance(dirs, str):
            _hidden_directories = set(d.strip() for d in dirs.split(",") if d.strip())
        else:
            _hidden_directories = {"@eaDir"}
    except Exception:
        _hidden_directories = {"@eaDir"}

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
    # Check against user-configured hidden directory names
    if name in _get_hidden_directories():
        return True
    # For Windows, check the hidden attribute
    if os.name == 'nt':
        try:
            attrs = os.stat(filepath).st_file_attributes
            return bool(attrs & stat.FILE_ATTRIBUTE_HIDDEN)
        except AttributeError:
            pass
    return False


def prune_empty_dirs(root):
    """Remove directories under ``root`` that are empty or contain only hidden junk.

    A folder is deleted when it holds no non-hidden entries; any hidden/system junk
    (dotfiles, ``@eaDir`` subtrees, etc.) it contains is removed first. Walks
    bottom-up so nested and batch-emptied folders collapse in a single pass. Never
    removes ``root`` itself. Returns the number of directories removed.

    Configured roots nested inside ``root`` are skipped, along with everything
    under them — see :func:`helpers.library.get_protected_roots`. WATCH inside
    TARGET is a supported layout, and this sweep used to delete it: the automated
    trigger (``api.check_wanted_after_watch_empty``) waits for WATCH to be *empty*
    before running the wanted match that schedules the sweep, so it fired on
    precisely the condition that made WATCH deletable.

    Interiors are skipped too, not just the roots themselves. A download client
    creates its job folder before writing into it, so an empty folder inside WATCH
    is usually a download about to start, not litter.
    """
    from helpers.library import get_protected_roots, is_valid_library_path

    if not os.path.isdir(root):
        return 0
    root_real = os.path.realpath(root)

    # Refuse outright rather than skip: a TARGET misconfigured to the collection
    # would otherwise have every empty series folder swept. Mirrors the guard in
    # app.process_incoming_wanted_issues.
    try:
        if is_valid_library_path(root_real):
            app_logger.error(
                f"Refusing to prune empty folders under {root_real}: it is a "
                f"library root or inside one. Check the TARGET setting."
            )
            return 0
    except Exception as e:
        app_logger.debug(f"Library check skipped for {root_real}: {e}")

    # Fail closed: if we cannot establish what is off-limits, skipping the sweep
    # leaves wrapper folders to accumulate, while sweeping blind can delete WATCH.
    try:
        protected = get_protected_roots()
    except Exception as e:
        app_logger.error(
            f"Skipping empty-folder sweep of {root_real}: could not resolve "
            f"protected directories ({e})."
        )
        return 0
    # Only roots *strictly inside* the sweep root make a subtree off-limits. The
    # sweep root itself is normally TARGET, and a WATCH that is a parent of TARGET
    # (WATCH=/downloads, TARGET=/downloads/processed — what the Unraid template
    # nudges people toward) would otherwise mark the whole tree protected and
    # silently turn the feature off.
    blocked = tuple(sorted(
        p for p in protected
        if p != root_real and p.startswith(root_real + os.sep)
    ))
    if blocked:
        app_logger.warning(
            f"Empty-folder sweep of {root_real}: configured directories are nested "
            f"inside it and will be left alone: {', '.join(blocked)}"
        )

    def _off_limits(path):
        """True if *path* is a protected root or lives inside a nested one."""
        if path in protected:
            return True
        return any(path.startswith(b + os.sep) for b in blocked)

    removed = 0
    for cur, _dirs, _files in os.walk(root_real, topdown=False):
        cur_real = os.path.realpath(cur)
        if cur_real == root_real:
            continue  # never delete the root itself
        if _off_limits(cur_real):
            app_logger.debug(f"Sweep skipped protected directory: {cur_real}")
            continue
        try:
            entries = os.listdir(cur_real)
            # A single non-hidden entry keeps the folder alive.
            if any(not is_hidden(os.path.join(cur_real, e)) for e in entries):
                continue
            # Only hidden entries remain — but a configured root can itself be
            # hidden-named ("_incoming", ".staging") or sit under a hidden folder,
            # and this branch deletes by rmtree without ever visiting it as `cur`.
            # Re-check the entries or the parent's sweep takes the root out.
            paths = [os.path.join(cur_real, e) for e in entries]
            if any(_off_limits(os.path.realpath(pth)) for pth in paths):
                app_logger.warning(
                    f"Sweep kept {cur_real}: it holds a configured directory."
                )
                continue
            for pth in paths:
                if os.path.isdir(pth) and not os.path.islink(pth):
                    shutil.rmtree(pth, ignore_errors=True)
                else:
                    os.remove(pth)
            os.rmdir(cur_real)
            removed += 1
            app_logger.info(f"Pruned empty directory under {root_real}: {cur_real}")
        except Exception as e:
            app_logger.error(f"Error pruning directory {cur_real}: {e}")
    return removed

#########################
#  Path Segment Safety  #
#########################

# Baseline set stripped from a single path segment (a folder/file name, never a
# separator). Slash and colon get readable smart-replacements; the rest are
# removed outright. Mirrors cbz_ops/rename.py:FILENAME_ILLEGAL_CHARS and the JS
# sanitizePathSegment() in templates/series.html — keep all copies in lockstep.
_PATH_SEGMENT_STRIP_RE = re.compile(r'[\\*?"<>|&$;]')


def sanitize_path_segment(name):
    """
    Make a single path segment (one folder or file name, not a full path) safe
    for the filesystem. Keeps readable smart-replacements for '/' and ':' and
    strips the remaining filesystem-hostile characters.

    - '/'  -> '-'      (a slash inside a segment is not a separator)
    - ':'  -> ' -'     (e.g. "Batman: Year One" -> "Batman - Year One")
    - \\ * ? " < > | & $ ;  -> removed
    """
    if not name:
        return name
    name = name.replace("/", "-").replace(":", " -")
    name = _PATH_SEGMENT_STRIP_RE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


#########################
#   Sidecar Permissions #
#########################

def match_parent_permissions(path):
    """Best-effort: make a freshly written file inherit its parent folder's
    accessibility (owner, group, and rwx bits minus execute) so shared/NAS
    accounts can read and write files CLU creates — both sidecars (cvinfo,
    series.json, ...) and the comic archives themselves.

    Files are otherwise created with the process umask and, for series.json and
    the CBZ-rewrite paths, a 0o600 temp file from mkstemp — leaving them
    inaccessible even when the containing folder is world-readable. Worse, when
    the container falls back to running as root (non-writable mount at startup),
    new files land as root:0600 and become unreadable once the app runs as the
    unprivileged app user. We mirror the parent folder's mode (dropping the
    execute bits, which only matter on directories) and set the owner/group to
    the parent's, so the file is as accessible as its folder. The owner change
    silently no-ops when unprivileged (a same-uid rewrite) and only takes effect
    when we have the privilege to do it (e.g. running as root). Silently ignored
    where unsupported (Windows-backed mounts, files CLU does not own).
    """
    try:
        parent = os.path.dirname(os.path.abspath(path)) or "."
        pst = os.stat(parent)
        os.chmod(path, stat.S_IMODE(pst.st_mode) & ~0o111)  # drop x for files
        if hasattr(os, "chown"):                            # absent on Windows
            try:
                os.chown(path, pst.st_uid, pst.st_gid)      # match folder owner+group
            except (PermissionError, OSError):
                pass
    except OSError:
        pass


def _zip_assembly_dir():
    """Local, seekable directory for staging archives before they're moved onto
    the data volume.

    Prefers ``CACHE_DIR/tmp`` — a real local volume (SQLite lives there, so it
    must support seek/locking). Falls back to the system temp dir if that can't
    be created for any reason. Returns ``None`` to mean "use the system default".
    """
    try:
        from core.config import config
        cache_dir = config.get("SETTINGS", "CACHE_DIR", fallback="/cache")
        tmp_root = os.path.join(cache_dir, "tmp")
        os.makedirs(tmp_root, exist_ok=True)
        return tmp_root
    except Exception:
        return None


@contextmanager
def open_zip_for_write(dest_path, compression=zipfile.ZIP_DEFLATED,
                       set_permissions=True, **zip_kwargs):
    """Yield a ``zipfile.ZipFile`` open for writing on local storage, then move
    the finished archive onto ``dest_path``.

    Assemble the archive on a local (seekable) volume rather than writing it
    straight onto the data mount. Mounts commonly used for ``/data`` (mergerfs /
    network / FUSE) may not support ``seek()`` on a file that is still being
    written, and ``zipfile`` must seek backward to patch local headers and write
    the central directory when it finalizes the archive. Writing directly there
    raises ``OSError: [Errno 29] Illegal seek`` at close. Moving the completed
    file into place is a sequential copy that never seeks the destination.

    On success ``dest_path`` inherits its parent folder's permissions (unless
    ``set_permissions=False``). If the body or the move raises, the temp file is
    removed and ``dest_path`` is left untouched.

    Usage mirrors ``zipfile.ZipFile``::

        with open_zip_for_write(cbz_path) as zf:
            zf.write(img_path, arcname)
            zf.writestr(zip_info, data)

    ``compression`` and any extra keyword args (e.g. ``compresslevel``) are
    forwarded to ``zipfile.ZipFile``.
    """
    suffix = os.path.splitext(dest_path)[1] or ".zip"
    tmp_dir = _zip_assembly_dir()
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="clu_zip_", dir=tmp_dir)
    os.close(fd)
    moved = False
    try:
        with zipfile.ZipFile(tmp_path, 'w', compression, **zip_kwargs) as zf:
            yield zf
        # The archive is fully written and closed on the local (seekable)
        # volume; move it onto the destination, which is a plain sequential copy
        # when the temp and destination live on different devices.
        shutil.move(tmp_path, dest_path)
        moved = True
        if set_permissions:
            match_parent_permissions(dest_path)
    finally:
        if not moved and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

#########################
#   Folder Thumbnails   #
#########################

# Every extension that counts as folder cover art, in lookup priority order.
# Anything that clears art before regenerating it must sweep this same list —
# a missed extension survives the write and keeps winning find_folder_thumbnail
# afterwards, so the new image is generated and then never shown.
FOLDER_THUMBNAIL_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def find_folder_thumbnail(folder_path):
    """Find a folder thumbnail image in the given directory.

    Stat-based: probes only the lowercase canonical names. Avoids
    os.listdir on huge directories, which is slow on Docker bind mounts
    where a publisher folder can contain thousands of series subfolders.

    Args:
        folder_path: Path to the directory to search

    Returns:
        Path to the thumbnail image if found, None otherwise
    """
    for ext in FOLDER_THUMBNAIL_EXTENSIONS:
        candidate = os.path.join(folder_path, f"folder{ext}")
        try:
            if os.path.exists(candidate):
                return candidate
        except (OSError, IOError):
            continue

    return None


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
    # Validate path is within allowed directories
    file_path = os.path.realpath(file_path)
    from helpers.library import is_allowed_path
    if not is_allowed_path(file_path):
        raise ValueError(f"Path not in allowed directory: {file_path}")

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


def _count_unar_failures(stdout_bytes):
    """Count files that failed during unar extraction from its stdout."""
    try:
        text = stdout_bytes.decode("utf-8", errors="replace")
        import re
        # unar outputs "filename... Failed! (reason)" for each failed file
        return len(re.findall(r'\.\.\..*Failed!', text))
    except Exception:
        return 0


def _try_extract_with_tool(tool_name, rar_path, output_dir):
    """Attempt full extraction with a single tool.

    Returns (has_files, failed_count) or raises FileNotFoundError if tool missing.
    """
    if tool_name == "unrar":
        cmd = ["unrar", "x", "-y", "-o+", rar_path, output_dir + "/"]
    elif tool_name == "7z":
        cmd = ["7z", "x", f"-o{output_dir}", "-y", rar_path]
    else:
        cmd = ["unar", "-f", "-o", output_dir, rar_path]

    result = subprocess.run(cmd, capture_output=True)
    has_files = os.path.exists(output_dir) and any(os.listdir(output_dir))
    failed_count = _count_unar_failures(result.stdout) if tool_name == "unar" and result.stdout else 0

    return result.returncode, has_files, failed_count


def extract_rar_with_unar(rar_path, output_dir):
    """
    Extract a RAR file, trying tools in order: unrar (proprietary) → 7z → unar.

    The proprietary unrar handles all RAR features including solid archives.
    7z and unar are used as fallbacks if unrar is not installed.

    :param rar_path: Path to the RAR file.
    :param output_dir: Directory to extract the contents into.
    :return: tuple(bool, int): (success, failed_file_count)
    """
    try:
        # Resolve to real paths to prevent path traversal
        rar_path = os.path.realpath(rar_path)
        output_dir = os.path.realpath(output_dir)

        # Validate paths are within allowed directories
        from helpers.library import is_allowed_path
        if not is_allowed_path(rar_path):
            raise ValueError(f"Path not in allowed directory: {rar_path}")
        if not is_allowed_path(output_dir):
            raise ValueError(f"Output path not in allowed directory: {output_dir}")

        # Check if the input file exists
        if not os.path.exists(rar_path):
            app_logger.error(f"Input file does not exist: {rar_path}")
            raise RuntimeError(f"Input file does not exist: {rar_path}")

        os.makedirs(output_dir, exist_ok=True)

        # Try each tool in order of capability
        tools = ["unrar", "7z", "unar"]
        last_error = None

        for tool_name in tools:
            try:
                app_logger.info(f"Extracting {rar_path} to {output_dir} using {tool_name}")
                returncode, has_files, failed_count = _try_extract_with_tool(
                    tool_name, rar_path, output_dir
                )

                if returncode == 0 and has_files:
                    app_logger.info(f"Extraction completed with {tool_name}. Output directory: {output_dir}")
                    return True, 0
                elif returncode != 0 and has_files:
                    app_logger.warning(
                        f"{tool_name} partial extraction (rc={returncode}), "
                        f"continuing with extracted files"
                    )
                    return True, failed_count
                else:
                    app_logger.warning(f"{tool_name} produced no files (rc={returncode})")
                    # Clean output_dir for next tool attempt
                    import shutil
                    shutil.rmtree(output_dir, ignore_errors=True)
                    os.makedirs(output_dir, exist_ok=True)

            except FileNotFoundError:
                app_logger.debug(f"{tool_name} not found, trying next tool")
                continue
            except Exception as e:
                last_error = e
                app_logger.warning(f"{tool_name} failed: {e}, trying next tool")
                # Clean output_dir for next tool attempt
                import shutil
                shutil.rmtree(output_dir, ignore_errors=True)
                os.makedirs(output_dir, exist_ok=True)
                continue

        # All tools failed
        msg = f"No extraction tool could extract {rar_path}"
        if last_error:
            msg += f": {last_error}"
        app_logger.error(msg)
        return False, 0

    except (ValueError, RuntimeError):
        raise
    except Exception as e:
        app_logger.error(f"Unexpected error extracting {rar_path}: {str(e)}")
        raise RuntimeError(f"Unexpected error extracting {rar_path}: {str(e)}")


def _parse_rar_listing(tool_name, stdout_bytes):
    """Turn one listing tool's stdout into member names.

    Each tool has its own shape: ``unrar lb`` prints one bare path per line,
    ``7z l -ba -slt`` prints ``Path = <name>`` records (``-slt`` because the
    column layout mangles names containing spaces), and ``lsar`` prints an
    ``<archive>: <format>`` banner followed by one name per line.
    """
    text = (stdout_bytes or b"").decode("utf-8", errors="replace")
    lines = text.splitlines()
    if tool_name == "lsar" and lines:
        lines = lines[1:]           # drop the "<archive>: RAR" banner
    names = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if tool_name == "7z":
            if line.startswith("Path = "):
                names.append(line[len("Path = "):].strip())
            continue
        names.append(line)
    return names


def list_rar_entries(rar_path):
    """List the member names of a RAR without extracting it.

    Same tool ladder as :func:`extract_rar_with_unar` (unrar -> 7z -> unar's
    ``lsar``), so a build that can open an archive can also look inside one.
    Returns a list of member names, or ``None`` when every tool failed -- the
    caller must treat "cannot tell" differently from "archive is empty".
    """
    try:
        rar_path = os.path.realpath(rar_path)
        from helpers.library import is_allowed_path
        if not is_allowed_path(rar_path):
            raise ValueError(f"Path not in allowed directory: {rar_path}")
        if not os.path.exists(rar_path):
            return None

        commands = [
            ("unrar", ["unrar", "lb", "-y", "--", rar_path]),
            ("7z", ["7z", "l", "-ba", "-slt", rar_path]),
            ("lsar", ["lsar", rar_path]),
        ]
        for tool_name, cmd in commands:
            try:
                result = subprocess.run(cmd, capture_output=True)
            except FileNotFoundError:
                app_logger.debug(f"{tool_name} not found, trying next listing tool")
                continue
            except Exception as e:
                app_logger.warning(f"{tool_name} listing failed: {e}, trying next tool")
                continue
            if result.returncode != 0:
                app_logger.debug(f"{tool_name} listing returned rc={result.returncode}")
                continue
            names = _parse_rar_listing(tool_name, result.stdout)
            if names:
                return names
            # rc==0 with no names is a genuinely empty archive, not a tool miss.
            return []

        app_logger.warning(f"No tool could list {rar_path}")
        return None
    except ValueError:
        raise
    except Exception as e:
        app_logger.warning(f"Unexpected error listing {rar_path}: {e}")
        return None


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


#########################
# Comic file serving    #
#########################

_COMIC_MIME_TYPES = {
    ".cbz": "application/vnd.comicbook+zip",
    ".cbr": "application/vnd.comicbook-rar",
    ".pdf": "application/pdf",
    ".epub": "application/epub+zip",
    ".zip": "application/zip",
}


def _parse_range_header(range_header, file_size):
    """
    Parse an HTTP Range header value (e.g. "bytes=0-1023" or "bytes=500-").
    Returns (start, end) inclusive byte offsets, or None if unparseable / unsatisfiable.
    Only single-range requests are supported.
    """
    if not range_header or not range_header.startswith("bytes="):
        return None
    try:
        spec = range_header[len("bytes="):].split(",")[0].strip()
        if "-" not in spec:
            return None
        start_s, end_s = spec.split("-", 1)
        if start_s == "":
            # Suffix range: last N bytes
            suffix = int(end_s)
            if suffix <= 0:
                return None
            start = max(0, file_size - suffix)
            end = file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else file_size - 1
        if start < 0 or end < start or start >= file_size:
            return None
        end = min(end, file_size - 1)
        return start, end
    except (ValueError, IndexError):
        return None


def serve_comic_file(file_path, range_header=None, as_attachment=True):
    """
    Build a Flask response that streams a comic file (CBZ/CBR/PDF/EPUB/ZIP).
    Supports HTTP Range requests for resumable mobile downloads.

    Args:
        file_path: Absolute path to the file on disk.
        range_header: Value of the request's "Range" header (or None).
        as_attachment: When True, sends Content-Disposition: attachment.

    Returns:
        A Flask Response object ready to be returned from a route.
        Status: 200 (full content), 206 (partial), 416 (unsatisfiable range),
        404 (missing file).
    """
    from flask import Response, send_file

    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return Response('{"error":"File not found"}', status=404,
                        mimetype="application/json")

    ext = os.path.splitext(file_path)[1].lower()
    mime_type = _COMIC_MIME_TYPES.get(ext, "application/octet-stream")
    file_size = os.path.getsize(file_path)
    filename = os.path.basename(file_path)

    if range_header:
        parsed = _parse_range_header(range_header, file_size)
        if parsed is None:
            resp = Response(status=416, mimetype=mime_type)
            resp.headers["Content-Range"] = f"bytes */{file_size}"
            return resp
        start, end = parsed
        length = end - start + 1

        def _stream():
            with open(file_path, "rb") as fh:
                fh.seek(start)
                remaining = length
                chunk_size = 64 * 1024
                while remaining > 0:
                    data = fh.read(min(chunk_size, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        resp = Response(_stream(), status=206, mimetype=mime_type,
                        direct_passthrough=True)
        resp.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Content-Length"] = str(length)
        if as_attachment:
            resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp

    # Full-file response — delegate to send_file for sendfile() / mtime handling.
    resp = send_file(file_path, as_attachment=as_attachment, mimetype=mime_type,
                     download_name=filename)
    resp.headers["Accept-Ranges"] = "bytes"
    return resp
