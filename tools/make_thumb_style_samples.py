"""Regenerate the folder-thumbnail style previews shown on /config.

The Personalization picker needs a picture of each style, and a screenshot
would drift the moment a composer changes. This renders the previews from the
composers themselves, over four synthetic covers, so the samples are always
what the code actually produces.

Run from the repo root after changing anything in core/folder_thumbnails.py:

    python tools/make_thumb_style_samples.py

Writes static/images/thumb-style-<style>.png for every key in STYLES.
"""

import os
import random
import sys
import tempfile

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import folder_thumbnails as ft  # noqa: E402

OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "images"
)

# Four flat, distinguishable covers. Deliberately not real art: the preview has
# to communicate the *arrangement*, and detailed covers would compete with it.
COVER_COLOURS = [
    ((198, 40, 40), (255, 138, 128)),
    ((21, 101, 192), (130, 177, 255)),
    ((245, 168, 0), (255, 224, 130)),
    ((56, 142, 60), (165, 214, 167)),
]

# The real inputs are cached cover thumbnails normalised to 300px tall.
COVER_SIZE = (200, 300)


def _make_cover(path, base, accent):
    img = Image.new("RGB", COVER_SIZE, base)
    draw = ImageDraw.Draw(img)
    # A masthead band and a couple of panel blocks, so the covers read as comic
    # covers at thumbnail size without any text to localise.
    draw.rectangle((0, 0, COVER_SIZE[0], 46), fill=accent)
    draw.rectangle((18, 76, COVER_SIZE[0] - 18, 196), fill=accent)
    draw.rectangle((18, 216, COVER_SIZE[0] - 74, 244), fill=accent)
    img.save(path, "JPEG", quality=90)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        covers = []
        for i, (base, accent) in enumerate(COVER_COLOURS):
            path = os.path.join(tmp, f"cover{i}.jpg")
            _make_cover(path, base, accent)
            covers.append(path)

        os.makedirs(OUT_DIR, exist_ok=True)
        for name, spec in ft.STYLES.items():
            compose = spec["compose"]
            # Seeded so the fan's angles are stable across runs -- otherwise
            # every regeneration would show up as a spurious asset diff.
            if name == "fanned":
                canvas = compose(covers, rng=random.Random(7))
            else:
                canvas = compose(covers[: spec["max_covers"]])

            out = os.path.join(OUT_DIR, f"thumb-style-{name}.png")
            canvas.save(out, "PNG")
            print(f"wrote {out}")


if __name__ == "__main__":
    main()
