"""Background auto-generation of folder cover art.

Folders get their `folder.png` from four places: a user-uploaded image, a
pinned issue, the per-folder "Generate Thumbnail" menu action, or one of the
two recursive sweeps ("Generate All Missing Thumbnails" and "Regenerate All
Thumbnails"). The first two are manual by nature; the rest mean a new series
sits behind a generic folder icon until someone remembers to run them.

This module closes that gap by generating art lazily for folders the user
actually looks at. `/api/browse-thumbnails` — the endpoint the collection grid
already calls for every folder it cannot show art for — hands those paths to
`enqueue()`, and a single background worker fills them in. Browsing is the hook
because it is reliable: unlike filesystem events, it fires for exactly the
folders on screen and re-fires on every visit, so a folder that failed to
render once gets another chance the next time it matters.

Two rules keep this from becoming a nuisance:

* An existing `folder.*` image is never touched. Uploaded art always wins.
  This holds for *auto*-generation only — the explicit menu actions, and the
  "Regenerate All Thumbnails" sweep in particular, pass `overwrite=True` and
  will replace it. That sweep is destructive by design and sits behind a
  confirmation in the UI.
* A folder that cannot produce art (no comics in it or below it) is remembered
  as failed and not retried for `FAILURE_TTL_SECONDS`, so a library full of
  empty folders does not re-attempt generation on every page view.

Generation is single-threaded on purpose: it opens comic archives to build
missing cover thumbnails, and the pre-existing `thumbnail_executor` already
competes for the same disk.

The module also owns the *look* of that art: `select_cover_files()` decides
which comics contribute covers, and the `STYLES` registry maps the site-wide
`folder_thumbnail_style` preference to one of four composers. Everything from
`select_cover_files` down is pure PIL with no Flask or filesystem-write
dependency, which is what makes it testable — the orchestrator that reads
preferences, resolves the cover cache and writes `folder.png` stays in
`app.generate_folder_thumbnail_internal`.
"""

import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw, ImageFilter, ImageOps

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

# Every piece of folder art is exactly this size. The grid sizes cards from
# the container (aspect-ratio 2/3 in collection.css), so art of a different
# size renders as a differently-sized card next to its neighbours.
CANVAS_SIZE = (200, 300)


def is_enabled():
    """True when auto-generation is turned on (default) in preferences."""
    try:
        from core.database import get_user_preference

        return bool(get_user_preference("auto_folder_thumbnails", default=True))
    except Exception:
        return True


def create_nested_folder_thumbnail(
    comic_stack_img, folder_icon_path, canvas_size=CANVAS_SIZE
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


# ---------------------------------------------------------------------------
# Cover selection
# ---------------------------------------------------------------------------

# Non-comic clutter that lives alongside comics and must never be mistaken for
# one.
EXCLUDED_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".html",
    ".css",
    ".ds_store",
    "cvinfo",
    ".json",
    ".db",
    ".xml",
}

COMIC_EXTENSIONS = (".cbz", ".cbr", ".zip")


def select_cover_files(folder_path, max_covers=4):
    """Pick the comics whose covers make up a folder's art.

    Returns ``(comic_paths, is_nested)``. A folder holding comics directly uses
    its own first few, alphabetically. A folder holding only subfolders is
    ``is_nested`` and borrows covers from each child, spreading ``max_covers``
    slots across them so a publisher folder shows a sample of the series inside
    rather than four issues of whichever series sorts first.

    Ordering is a plain filename sort -- no issue-number awareness, so "Issue
    10" precedes "Issue 2". That is pre-existing behaviour and deliberately
    unchanged; a pinned cover (see the orchestrator in app.py) is how a user
    overrides it.
    """
    comic_files = []
    is_nested = False

    for item in sorted(os.listdir(folder_path)):
        item_path = os.path.join(folder_path, item)
        if os.path.isfile(item_path):
            _, ext = os.path.splitext(item.lower())
            if ext not in EXCLUDED_EXTENSIONS and not item.startswith((".", "-", "_")):
                if ext in COMIC_EXTENSIONS:
                    comic_files.append(item_path)

    if not comic_files:
        is_nested = True
        subfolder_comics = {}

        for item in sorted(os.listdir(folder_path)):
            item_path = os.path.join(folder_path, item)
            if os.path.isdir(item_path) and not item.startswith((".", "_")):
                folder_comics = []
                for subitem in sorted(os.listdir(item_path)):
                    subitem_path = os.path.join(item_path, subitem)
                    if os.path.isfile(subitem_path):
                        _, ext = os.path.splitext(subitem.lower())
                        if ext in COMIC_EXTENSIONS:
                            folder_comics.append(subitem_path)
                if folder_comics:
                    subfolder_comics[item_path] = folder_comics

        if not subfolder_comics:
            return [], True

        subfolders = list(subfolder_comics.keys())
        num_folders = len(subfolders)

        if num_folders >= max_covers:
            for i in range(max_covers):
                comic_files.append(subfolder_comics[subfolders[i]][0])
        else:
            per_folder = max_covers // num_folders
            remainder = max_covers % num_folders
            for i, folder in enumerate(subfolders):
                count = per_folder + (1 if i < remainder else 0)
                comic_files.extend(subfolder_comics[folder][:count])

    return comic_files[:max_covers], is_nested


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------
#
# Every composer takes a list of cached cover-thumbnail paths -- element 0 is
# the primary cover -- and returns an RGBA image at exactly CANVAS_SIZE. The
# grid sizes cards from the container (aspect-ratio 2/3 in collection.css), so a
# composer returning a different size would render as a different-sized card
# next to its neighbours.

# Fanned Stack
FAN_COVER_SIZE = (150, 245)
FAN_ROTATION_LIMIT = 10

# Isometric Cascade
CASCADE_COVER_SIZE = (150, 225)
CASCADE_STEP = 7

# 2x2 Mosaic
MOSAIC_MARGIN = 6
MOSAIC_GAP = 4
# Mid grey at low alpha: the gutters have to read against both a light and a
# dark card background, and any saturated colour would fight the covers.
MOSAIC_DIVIDER = (128, 128, 128, 90)


def _open_cover(thumb_path):
    """Open one cached cover as RGBA, or None if it cannot be read.

    A single unreadable cache entry must not cost the folder its whole
    thumbnail, so callers skip Nones and compose with what is left.
    """
    try:
        return Image.open(thumb_path).convert("RGBA")
    except Exception as e:
        app_logger.error(f"Error processing thumbnail {thumb_path}: {e}")
        return None


def _load_covers(thumb_paths, limit=4):
    """The first ``limit`` covers that actually open.

    Filters before capping, not after: an unreadable cache entry would
    otherwise consume one of the four slots and leave a hole in the layout.
    """
    covers = []
    for path in thumb_paths:
        if len(covers) >= limit:
            break
        img = _open_cover(path)
        if img is not None:
            covers.append(img)
    return covers


def _fit_letterbox(img, size):
    """Scale to fit inside ``size`` and center on a transparent canvas."""
    img = img.copy()
    img.thumbnail(size, Image.Resampling.LANCZOS)
    fitted = Image.new("RGBA", size, (0, 0, 0, 0))
    fitted.paste(img, ((size[0] - img.width) // 2, (size[1] - img.height) // 2), img)
    return fitted


def compose_fanned(thumb_paths, rng=random):
    """Covers splayed at random angles, the primary square-on at the front.

    ``rng`` is injectable purely so tests can seed it -- the angles are random
    per run, which is why regenerating a folder never reproduces its old image.
    """
    canvas = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    covers = _load_covers(thumb_paths)
    if not covers:
        return canvas

    # Reversed so the primary cover is drawn last, on top, and forced upright.
    reversed_covers = list(reversed(covers))
    angles = [
        0
        if i == len(reversed_covers) - 1
        else rng.randint(-FAN_ROTATION_LIMIT, FAN_ROTATION_LIMIT)
        for i in range(len(reversed_covers))
    ]

    # Composited on an oversized layer so rotation has room and does not clip
    # the corners off.
    layer_size = (int(FAN_COVER_SIZE[0] * 1.5), int(FAN_COVER_SIZE[1] * 1.5))
    paste_x = (layer_size[0] - FAN_COVER_SIZE[0]) // 2
    paste_y = (layer_size[1] - FAN_COVER_SIZE[1]) // 2

    for i, img in enumerate(reversed_covers):
        fitted = _fit_letterbox(img, FAN_COVER_SIZE)

        shadow = Image.new("RGBA", layer_size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rectangle(
            (
                paste_x + 4,
                paste_y + 4,
                paste_x + FAN_COVER_SIZE[0] + 4,
                paste_y + FAN_COVER_SIZE[1] + 4,
            ),
            fill=(0, 0, 0, 120),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=5))

        layer = Image.alpha_composite(
            Image.new("RGBA", layer_size, (0, 0, 0, 0)), shadow
        )
        layer.paste(fitted, (paste_x, paste_y), fitted)

        rotated = layer.rotate(
            angles[i], resample=Image.Resampling.BICUBIC, expand=False
        )
        canvas.paste(
            rotated,
            (
                (CANVAS_SIZE[0] - rotated.width) // 2,
                (CANVAS_SIZE[1] - rotated.height) // 2,
            ),
            rotated,
        )

    return canvas


def compose_cascade(thumb_paths):
    """A neatly squared-off deck: no rotation, each layer stepped up and right."""
    canvas = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    covers = _load_covers(thumb_paths)
    if not covers:
        return canvas

    cover_w, cover_h = CASCADE_COVER_SIZE
    n = len(covers)
    spread = (n - 1) * CASCADE_STEP
    base_x = (CANVAS_SIZE[0] - cover_w - spread) // 2
    base_y = (CANVAS_SIZE[1] - cover_h - spread) // 2

    # Drawn back to front. covers[0] is the primary, so it lands last, in front,
    # at the bottom-left; the rest recede up and to the right behind it.
    for i, img in enumerate(reversed(covers)):
        x = base_x + (n - 1 - i) * CASCADE_STEP
        y = base_y + i * CASCADE_STEP

        # A tight blur, unlike the fan's soft one -- the point of this style is
        # crisp separation between layers.
        shadow = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rectangle(
            (x + 3, y + 3, x + cover_w + 2, y + cover_h + 2), fill=(0, 0, 0, 100)
        )
        canvas = Image.alpha_composite(
            canvas, shadow.filter(ImageFilter.GaussianBlur(radius=3))
        )

        fitted = _fit_letterbox(img, CASCADE_COVER_SIZE)
        canvas.paste(fitted, (x, y), fitted)

    return canvas


def mosaic_cells(count):
    """Tile rectangles ``(x, y, w, h)`` for ``count`` covers, front-first.

    Split out so the layout can be asserted without rendering: a 2x2 grid needs
    four covers, and a folder with fewer has to degrade to something that still
    fills the card rather than leaving holes.
    """
    margin, gap = MOSAIC_MARGIN, MOSAIC_GAP
    inner_w = CANVAS_SIZE[0] - margin * 2
    inner_h = CANVAS_SIZE[1] - margin * 2
    tile_w = (inner_w - gap) // 2
    tile_h = (inner_h - gap) // 2

    if count <= 1:
        return [(margin, margin, inner_w, inner_h)]
    if count == 2:
        return [
            (margin, margin, inner_w, tile_h),
            (margin, margin + tile_h + gap, inner_w, tile_h),
        ]
    # Three covers fill the first three cells; the fourth stays divider colour.
    return [
        (margin, margin, tile_w, tile_h),
        (margin + tile_w + gap, margin, tile_w, tile_h),
        (margin, margin + tile_h + gap, tile_w, tile_h),
        (margin + tile_w + gap, margin + tile_h + gap, tile_w, tile_h),
    ][:count]


def compose_mosaic(thumb_paths):
    """A quad-split grid with interior dividers and one outer drop shadow."""
    canvas = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    covers = _load_covers(thumb_paths)
    if not covers:
        return canvas

    margin = MOSAIC_MARGIN
    inner_w = CANVAS_SIZE[0] - margin * 2
    inner_h = CANVAS_SIZE[1] - margin * 2

    # One shadow under the whole block. The outer margin is what leaves room for
    # it -- .thumbnail-container clips overflow, so a full-bleed block's shadow
    # would be cropped away entirely.
    shadow = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle(
        (margin, margin + 3, margin + inner_w - 1, margin + inner_h + 2),
        fill=(0, 0, 0, 110),
    )
    canvas = Image.alpha_composite(
        canvas, shadow.filter(ImageFilter.GaussianBlur(radius=5))
    )

    # Painted before the tiles so it shows through the gutters -- and fills any
    # cell left empty when the folder has fewer than four covers.
    ImageDraw.Draw(canvas).rectangle(
        (margin, margin, margin + inner_w - 1, margin + inner_h - 1),
        fill=MOSAIC_DIVIDER,
    )

    # Cover-fit, not letterbox: a grid layout with letterboxed tiles reads as
    # four floating images rather than one quad-split card.
    for img, (x, y, w, h) in zip(covers, mosaic_cells(len(covers))):
        canvas.paste(ImageOps.fit(img, (w, h), Image.Resampling.LANCZOS), (x, y))

    return canvas


def compose_single(thumb_paths):
    """One cover, full-bleed -- the same look as an ordinary comic card.

    Takes the first cover that *loads*, not blindly the first path: an
    unreadable cache entry would otherwise leave the folder with a blank image
    where every other style would have fallen through to the next cover.
    """
    canvas = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    for path in thumb_paths:
        img = _open_cover(path)
        if img is not None:
            canvas.paste(
                ImageOps.fit(img, CANVAS_SIZE, Image.Resampling.LANCZOS), (0, 0)
            )
            break
    return canvas


STYLES = {
    "fanned": {
        "compose": compose_fanned,
        "max_covers": 4,
        "label": "Fanned Stack",
        "description": "Covers splayed at slight angles, like a hand of cards.",
    },
    "single": {
        "compose": compose_single,
        "max_covers": 1,
        "label": "Single Image",
        "description": "One cover, full frame. Pin any issue from its three-dots menu.",
    },
    "cascade": {
        "compose": compose_cascade,
        "max_covers": 4,
        "label": "Isometric Cascade",
        "description": "A neat deck, each cover stepped up and to the right.",
    },
    "mosaic": {
        "compose": compose_mosaic,
        "max_covers": 4,
        "label": "2x2 Mosaic Grid",
        "description": "The first four covers in a quad-split grid.",
    },
}

DEFAULT_STYLE = "fanned"


def style_choices():
    """STYLES as plain data for templates -- no callables, insertion-ordered.

    The picker renders straight from this, so adding a style to STYLES puts it
    in the settings UI with no template change.
    """
    return [
        {"id": key, "label": spec["label"], "description": spec["description"]}
        for key, spec in STYLES.items()
    ]


def get_style():
    """The site-wide thumbnail style, always a key of ``STYLES``.

    An unknown value (a hand-edited preference, or a style dropped in a later
    release) falls back to the default rather than breaking every folder.
    """
    try:
        from core.database import get_user_preference

        style = get_user_preference("folder_thumbnail_style", default=DEFAULT_STYLE)
    except Exception:
        return DEFAULT_STYLE

    return style if style in STYLES else DEFAULT_STYLE


def nested_overlay_enabled():
    """Whether nested folders get the folder-icon overlay. On by default."""
    try:
        from core.database import get_user_preference

        return bool(get_user_preference("folder_thumbnail_nested_overlay", default=True))
    except Exception:
        return True


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
