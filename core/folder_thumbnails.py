"""Background auto-generation of folder cover art.

Folders get their `folder.png` from three places: a user-uploaded image, the
per-folder "Generate Thumbnail" menu action, or the recursive "Generate All
Missing Thumbnails" sweep. The first is manual by nature; the other two mean a
new series sits behind a generic folder icon until someone remembers to run
them.

This module closes that gap by generating art lazily for folders the user
actually looks at. `/api/browse-thumbnails` — the endpoint the collection grid
already calls for every folder it cannot show art for — hands those paths to
`enqueue()`, and a single background worker fills them in. Browsing is the hook
because it is reliable: unlike filesystem events, it fires for exactly the
folders on screen and re-fires on every visit, so a folder that failed to
render once gets another chance the next time it matters.

Two rules keep this from becoming a nuisance:

* An existing `folder.*` image is never touched. Uploaded art always wins.
* A folder that cannot produce art (no comics in it or below it) is remembered
  as failed and not retried for `FAILURE_TTL_SECONDS`, so a library full of
  empty folders does not re-attempt generation on every page view.

Generation is single-threaded on purpose: it opens comic archives to build
missing cover thumbnails, and the pre-existing `thumbnail_executor` already
competes for the same disk.
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from core.app_logging import app_logger

# Folders whose generation failed are not retried for this long. Long enough
# that browsing back and forth costs nothing, short enough that adding comics
# to a previously empty folder is picked up the same day.
FAILURE_TTL_SECONDS = 6 * 60 * 60

# Upper bound on paths waiting in the queue. A user paging through a large
# library can enqueue faster than one worker drains; past this point new
# requests are dropped and picked up on a later visit rather than growing the
# backlog without limit.
MAX_QUEUED = 250

# Ceiling on remembered failures before expired ones are pruned.
MAX_FAILED_TRACKED = 5000

_executor = None
_executor_lock = threading.Lock()

_state_lock = threading.Lock()
_queued = set()        # paths queued or in flight
_failed = {}           # path -> monotonic timestamp of last failure


def is_enabled():
    """True when auto-generation is turned on (default) in preferences."""
    try:
        from core.database import get_user_preference

        return bool(get_user_preference("auto_folder_thumbnails", default=True))
    except Exception:
        return True


def create_nested_folder_thumbnail(
    comic_stack_img, folder_icon_path, canvas_size=(200, 300)
):
    """Composite the comic stack behind a folder icon for nested folder thumbnails.

    Returns an image at ``canvas_size`` — the same dimensions as the plain
    fanned stack, and the same 300px height every comic cover thumbnail is
    normalized to. The two folder variants sit side by side in the grid, so a
    composite of a different size reads as a different-sized card.
    """
    folder_icon = Image.open(folder_icon_path).convert("RGBA")

    stack = comic_stack_img.convert("RGBA")

    # Scale stack to 190px wide with proportionate height
    new_w = 190
    aspect_ratio = stack.height / stack.width
    new_h = int(new_w * aspect_ratio)

    stack_resized = stack.resize((new_w, new_h), Image.Resampling.LANCZOS)

    # Position stack: centered horizontally, 20px from bottom
    x_pos = (canvas_size[0] - new_w) // 2
    y_pos = canvas_size[1] - new_h - 20  # 20px from bottom

    # Create final canvas
    final_thumb = Image.new("RGBA", canvas_size, (0, 0, 0, 0))

    # Paste stack FIRST (behind)
    final_thumb.paste(stack_resized, (x_pos, y_pos), mask=stack_resized)

    # Paste folder icon ON TOP (in front)
    final_thumb.paste(folder_icon, (0, 0), mask=folder_icon)

    return final_thumb


def may_write(folder_path, overwrite):
    """Whether generation is allowed to write ``folder.png`` into a folder.

    The explicit menu actions pass ``overwrite=True`` — the user asked for new
    art and expects the old image replaced. Auto-generation passes False, and
    any existing ``folder.*`` (including formats we generate but never produce,
    like an uploaded .gif) makes this the user's image to keep.
    """
    if overwrite:
        return True

    from helpers import find_folder_thumbnail

    return find_folder_thumbnail(folder_path) is None


def _get_executor():
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="folder-thumb"
            )
        return _executor


def reset_state():
    """Clear queue bookkeeping. Test-support only."""
    with _state_lock:
        _queued.clear()
        _failed.clear()


def _should_queue(folder_path):
    """Decide whether to accept a path, and claim it if so."""
    with _state_lock:
        if folder_path in _queued:
            return False
        if len(_queued) >= MAX_QUEUED:
            return False

        failed_at = _failed.get(folder_path)
        if failed_at is not None:
            if (time.monotonic() - failed_at) < FAILURE_TTL_SECONDS:
                return False
            del _failed[folder_path]

        _queued.add(folder_path)
        return True


def _record_result(folder_path, succeeded):
    with _state_lock:
        _queued.discard(folder_path)
        if succeeded:
            _failed.pop(folder_path, None)
        else:
            _failed[folder_path] = time.monotonic()
            # A library can hold more art-less folders than we want to remember
            # forever. Expired entries are dead weight, so drop them once the
            # record grows past the point of being a useful short-term memory.
            if len(_failed) > MAX_FAILED_TRACKED:
                cutoff = time.monotonic() - FAILURE_TTL_SECONDS
                for path in [p for p, t in _failed.items() if t < cutoff]:
                    del _failed[path]


def _generate(folder_path):
    """Worker body: generate art for one folder, never raising."""
    succeeded = False
    try:
        # Imported here: app imports this module's enqueue(), so a module-level
        # import would be circular.
        from app import generate_folder_thumbnail_internal

        succeeded = generate_folder_thumbnail_internal(folder_path, overwrite=False)
        if succeeded:
            app_logger.info(f"Auto-generated folder thumbnail for {folder_path}")
        else:
            app_logger.debug(
                f"No folder thumbnail could be generated for {folder_path}"
            )
    except Exception as e:
        app_logger.warning(f"Auto folder-thumbnail generation failed for {folder_path}: {e}")
    finally:
        _record_result(folder_path, succeeded)


def enqueue(folder_path):
    """Queue a folder for background art generation.

    Returns True when the path was accepted (it was a real directory, is not
    already queued, has not recently failed, and auto-generation is enabled).
    Callers treat the return value as advisory — a False costs the user
    nothing beyond the generic folder icon they already have.
    """
    if not folder_path or not is_enabled():
        return False

    try:
        if not os.path.isdir(folder_path):
            return False
    except OSError:
        return False

    if not _should_queue(folder_path):
        return False

    try:
        _get_executor().submit(_generate, folder_path)
        return True
    except Exception as e:
        # Executor rejected the work (shutdown, thread exhaustion). Release the
        # claim so a later visit can retry.
        _record_result(folder_path, False)
        app_logger.warning(f"Could not queue folder thumbnail for {folder_path}: {e}")
        return False


def enqueue_many(folder_paths):
    """Queue several folders. Returns the number accepted."""
    return sum(1 for path in folder_paths if enqueue(path))
