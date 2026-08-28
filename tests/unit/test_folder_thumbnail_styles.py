"""Tests for the four folder-thumbnail composers in core/folder_thumbnails.py.

The composers are the only part of folder art that is pure PIL -- no Flask, no
preferences, no disk writes -- which is why they live in core/ and why these
tests can assert on geometry directly.

Two invariants matter more than anything the eye would catch:

* Every style returns exactly CANVAS_SIZE. The grid sizes cards from the
  container (aspect-ratio 2/3), so a composer returning a different size renders
  as a differently-sized card next to its neighbours.
* Element 0 of the input is the *primary* cover in every style. That is the
  contract the per-folder pin relies on -- the orchestrator moves the pinned
  comic to the front of the list and expects each composer to feature it.
"""
import os
import random

import pytest
from PIL import Image

from core import folder_thumbnails as ft


# Distinct flat colours so a tile can be identified by sampling one pixel.
COVER_COLOURS = [
    (220, 20, 20),
    (20, 20, 220),
    (240, 200, 20),
    (20, 180, 20),
]


@pytest.fixture
def covers(tmp_path):
    """Four flat-colour covers on disk, at the 2:3 shape real covers have."""
    paths = []
    for i, colour in enumerate(COVER_COLOURS):
        path = tmp_path / f"cover{i}.jpg"
        Image.new("RGB", (200, 300), colour).save(path, "JPEG", quality=95)
        paths.append(str(path))
    return paths


ALL_COMPOSERS = [
    ("fanned", ft.compose_fanned),
    ("single", ft.compose_single),
    ("cascade", ft.compose_cascade),
    ("mosaic", ft.compose_mosaic),
]


class TestCanvasContract:
    """Every style, every cover count, must produce the same-sized RGBA image."""

    @pytest.mark.parametrize("name,compose", ALL_COMPOSERS)
    @pytest.mark.parametrize("count", [1, 2, 3, 4])
    def test_returns_canvas_size_rgba(self, name, compose, count, covers):
        result = compose(covers[:count])
        assert result.size == ft.CANVAS_SIZE
        assert result.mode == "RGBA"

    @pytest.mark.parametrize("name,compose", ALL_COMPOSERS)
    def test_no_covers_returns_blank_canvas(self, name, compose):
        result = compose([])
        assert result.size == ft.CANVAS_SIZE
        assert result.getbbox() is None  # fully transparent

    @pytest.mark.parametrize("name,compose", ALL_COMPOSERS)
    def test_unreadable_cover_is_skipped_not_fatal(self, name, compose, covers, tmp_path):
        """One bad cache entry must not cost the folder its whole thumbnail."""
        broken = tmp_path / "broken.jpg"
        broken.write_bytes(b"not an image")

        result = compose([str(broken)] + covers)
        assert result.size == ft.CANVAS_SIZE
        # Something was still drawn from the covers that did load.
        assert result.getbbox() is not None

    @pytest.mark.parametrize("name,compose", ALL_COMPOSERS)
    def test_extra_covers_are_ignored(self, name, compose, covers):
        """More than four covers must not overflow or crash a composer."""
        result = compose(covers * 3)
        assert result.size == ft.CANVAS_SIZE


class TestSingle:

    def test_fills_the_whole_canvas(self, covers):
        result = ft.compose_single(covers)
        # Full-bleed: no transparent border anywhere.
        assert result.getbbox() == (0, 0, *ft.CANVAS_SIZE)
        assert result.getpixel((0, 0))[3] == 255
        assert result.getpixel((199, 299))[3] == 255

    def test_uses_only_the_primary_cover(self, covers):
        result = ft.compose_single(covers)
        centre = result.getpixel((100, 150))[:3]
        assert _closest_cover(centre) == 0


class TestCascade:

    def test_layers_step_up_and_right(self):
        """Positions are derived, so assert the arithmetic rather than pixels."""
        cover_w, cover_h = ft.CASCADE_COVER_SIZE
        n = 4
        spread = (n - 1) * ft.CASCADE_STEP
        base_x = (ft.CANVAS_SIZE[0] - cover_w - spread) // 2
        base_y = (ft.CANVAS_SIZE[1] - cover_h - spread) // 2

        # Draw order is back-to-front; i=0 is the backmost layer.
        positions = [
            (base_x + (n - 1 - i) * ft.CASCADE_STEP, base_y + i * ft.CASCADE_STEP)
            for i in range(n)
        ]

        xs = [p[0] for p in positions]
        ys = [p[1] for p in positions]
        # Successive layers move left and down as they come forward, i.e. the
        # ones behind sit up and to the right.
        assert xs == sorted(xs, reverse=True)
        assert ys == sorted(ys)
        # And the whole deck fits on the canvas.
        assert min(xs) >= 0 and max(xs) + cover_w <= ft.CANVAS_SIZE[0]
        assert min(ys) >= 0 and max(ys) + cover_h <= ft.CANVAS_SIZE[1]

    def test_primary_cover_is_in_front(self, covers):
        result = ft.compose_cascade(covers)
        cover_w, cover_h = ft.CASCADE_COVER_SIZE
        n = len(covers)
        spread = (n - 1) * ft.CASCADE_STEP
        front_x = (ft.CANVAS_SIZE[0] - cover_w - spread) // 2
        front_y = (ft.CANVAS_SIZE[1] - cover_h - spread) // 2 + spread

        centre = result.getpixel((front_x + cover_w // 2, front_y + cover_h // 2))[:3]
        assert _closest_cover(centre) == 0

    def test_does_not_rotate(self, covers):
        """A rotated layer leaves soft, off-colour corners; a stepped one doesn't.

        Sampled just inside the front cover's corner: with rotation that pixel
        would be partly transparent background rather than solid cover.
        """
        result = ft.compose_cascade(covers[:1])
        cover_w, cover_h = ft.CASCADE_COVER_SIZE
        x = (ft.CANVAS_SIZE[0] - cover_w) // 2
        y = (ft.CANVAS_SIZE[1] - cover_h) // 2

        for corner in [(x + 2, y + 2), (x + cover_w - 3, y + 2), (x + 2, y + cover_h - 3)]:
            assert result.getpixel(corner)[3] == 255


class TestMosaic:

    def test_four_covers_land_in_their_cells(self, covers):
        result = ft.compose_mosaic(covers)
        for index, (x, y, w, h) in enumerate(ft.mosaic_cells(4)):
            centre = result.getpixel((x + w // 2, y + h // 2))[:3]
            assert _closest_cover(centre) == index, f"cell {index} has the wrong cover"

    def test_primary_cover_is_top_left(self, covers):
        result = ft.compose_mosaic(covers)
        x, y, w, h = ft.mosaic_cells(4)[0]
        assert _closest_cover(result.getpixel((x + w // 2, y + h // 2))[:3]) == 0

    def test_cells_do_not_overlap_and_stay_inside_the_margin(self):
        cells = ft.mosaic_cells(4)
        for x, y, w, h in cells:
            assert x >= ft.MOSAIC_MARGIN
            assert y >= ft.MOSAIC_MARGIN
            assert x + w <= ft.CANVAS_SIZE[0] - ft.MOSAIC_MARGIN
            assert y + h <= ft.CANVAS_SIZE[1] - ft.MOSAIC_MARGIN

        # Left column ends before the right column starts, with the gutter
        # between them; same vertically.
        assert cells[0][0] + cells[0][2] + ft.MOSAIC_GAP == cells[1][0]
        assert cells[0][1] + cells[0][3] + ft.MOSAIC_GAP == cells[2][1]

    def test_two_covers_split_into_full_width_rows(self):
        cells = ft.mosaic_cells(2)
        assert len(cells) == 2
        inner_w = ft.CANVAS_SIZE[0] - ft.MOSAIC_MARGIN * 2
        assert all(w == inner_w for _, _, w, _ in cells)

    def test_one_cover_fills_the_inner_frame(self):
        cells = ft.mosaic_cells(1)
        assert cells == [
            (
                ft.MOSAIC_MARGIN,
                ft.MOSAIC_MARGIN,
                ft.CANVAS_SIZE[0] - ft.MOSAIC_MARGIN * 2,
                ft.CANVAS_SIZE[1] - ft.MOSAIC_MARGIN * 2,
            )
        ]

    def test_three_covers_leave_the_fourth_cell_as_divider(self, covers):
        """The empty cell must still be filled, not punched through to nothing."""
        result = ft.compose_mosaic(covers[:3])
        x, y, w, h = ft.mosaic_cells(4)[3]
        assert result.getpixel((x + w // 2, y + h // 2))[3] > 0

    def test_leaves_room_for_its_shadow(self, covers):
        """The margin is what keeps the baked shadow from being clipped away.

        .thumbnail-container sets overflow: clip, so a full-bleed block would
        have its shadow cropped off entirely.
        """
        assert ft.MOSAIC_MARGIN > 0
        result = ft.compose_mosaic(covers)
        # The very corner is outside the tile block.
        assert result.getpixel((0, 0))[3] < 255


class TestFanned:

    def test_is_reproducible_with_a_seeded_rng(self, covers):
        a = ft.compose_fanned(covers, rng=random.Random(0))
        b = ft.compose_fanned(covers, rng=random.Random(0))
        assert a.tobytes() == b.tobytes()

    def test_different_seeds_fan_differently(self, covers):
        a = ft.compose_fanned(covers, rng=random.Random(0))
        b = ft.compose_fanned(covers, rng=random.Random(99))
        assert a.tobytes() != b.tobytes()

    def test_primary_cover_is_upright_on_top(self, covers):
        """The front card is forced to 0 degrees, whatever the rng says."""
        result = ft.compose_fanned(covers, rng=random.Random(0))
        assert _closest_cover(result.getpixel((100, 150))[:3]) == 0


class TestStyleRegistry:

    def test_every_style_has_a_sample_image(self):
        """The /config picker renders one preview per style, by id."""
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for style_id in ft.STYLES:
            sample = os.path.join(root, "static", "images", f"thumb-style-{style_id}.png")
            assert os.path.exists(sample), f"missing preview for '{style_id}'"

    def test_default_style_is_registered(self):
        assert ft.DEFAULT_STYLE in ft.STYLES

    def test_every_style_is_complete(self):
        for style_id, spec in ft.STYLES.items():
            assert callable(spec["compose"]), style_id
            assert spec["max_covers"] >= 1, style_id
            assert spec["label"], style_id
            assert spec["description"], style_id

    def test_style_choices_is_plain_data(self):
        """Templates get this, so it must not carry callables."""
        choices = ft.style_choices()
        assert [c["id"] for c in choices] == list(ft.STYLES)
        for choice in choices:
            assert set(choice) == {"id", "label", "description"}


def _closest_cover(pixel):
    """Index of the COVER_COLOURS entry a sampled pixel came from.

    Sampled pixels are never exactly the source colour -- JPEG, LANCZOS and the
    shadow compositing all shift them -- so identify by nearest colour rather
    than equality.
    """
    return min(
        range(len(COVER_COLOURS)),
        key=lambda i: sum(
            (pixel[c] - COVER_COLOURS[i][c]) ** 2 for c in range(3)
        ),
    )
