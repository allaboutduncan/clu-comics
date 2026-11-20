"""Wrapped styles: the looks a recap can be drawn in.

A style is a palette, a font family and a background painter. ``wrapped.py``
asks the active style for those three things in the places it already asked
the theme (``get_theme_colors``, ``get_font``, ``create_base_image``), so every
slide, present and future, follows a style without knowing styles exist.

- ``theme`` follows the user's CLU theme: no palette of its own.
- ``classic`` is the dark comic-panel artwork Monthly Wrapped has always used
  (``static/images/recap-bg.png``, with its title painted in). It is monthly
  only -- the yearly slides put a logo where that title sits -- and it stays
  the monthly default so nobody's recap changes look unasked.
- ``halftone``, ``newsprint`` and ``neon`` are drawn here, from nothing but
  Pillow primitives and the fonts the image already ships (Liberation and
  DejaVu): no bundled font files, nothing to download.

Adding a style is one entry in ``STYLES``. Every palette needs the keys the
theme palettes have (``wrapped.THEME_COLORS``); a test checks that, and that
every style draws every slide.
"""
from dataclasses import dataclass
from typing import Callable, Optional

YEARLY = "yearly"
MONTHLY = "monthly"

PALETTE_KEYS = (
    "primary", "secondary", "success", "info", "warning", "danger",
    "bg", "bg_secondary", "text", "text_muted", "is_dark",
)


def _rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def _frame(draw, width, height, color, inset, thickness):
    draw.rectangle(
        [inset, inset, width - inset - 1, height - inset - 1],
        outline=color, width=thickness,
    )


def paint_halftone(width, height, palette):
    """Four-colour-press yellow with a field of dots that swells toward one
    corner, inside a heavy ink border."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), _rgb(palette["bg"]))
    draw = ImageDraw.Draw(img)
    dot = _rgb(palette["bg_secondary"])
    step = 36
    for row, y in enumerate(range(0, height + step, step)):
        # Offset alternate rows, like a real screen.
        for x in range(-(step // 2) * (row % 2), width + step, step):
            # 0 at the top-left, 1 at the bottom-right.
            t = (x / width + y / height) / 2
            radius = 2 + 13 * t * t
            draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=dot)
    _frame(draw, width, height, _rgb(palette["text"]), 18, 12)
    return img


def paint_newsprint(width, height, palette):
    """Off-white paper, faintly ruled, inside a double rule."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), _rgb(palette["bg"]))
    draw = ImageDraw.Draw(img)
    rule = _rgb(palette["bg_secondary"])
    for y in range(0, height, 9):
        draw.line([(0, y), (width, y)], fill=rule, width=1)
    ink = _rgb(palette["text"])
    _frame(draw, width, height, ink, 24, 6)
    _frame(draw, width, height, ink, 38, 2)
    return img


def paint_neon(width, height, palette):
    """Near-black to deep violet, with a grid that fades out toward the top."""
    from PIL import Image, ImageDraw

    top, bottom = _rgb(palette["bg"]), _rgb(palette["bg_secondary"])
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / (height - 1)
        draw.line([(0, y), (width, y)],
                  fill=tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))

    grid = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    grid_draw = ImageDraw.Draw(grid)
    line = _rgb(palette["primary"])
    step = 90
    for x in range(0, width + 1, step):
        grid_draw.line([(x, 0), (x, height)], fill=line + (38,), width=2)
    for y in range(0, height + 1, step):
        # Brighter toward the bottom, as if lit from below.
        grid_draw.line([(0, y), (width, y)], fill=line + (round(14 + 50 * y / height),), width=2)
    img.paste(grid, (0, 0), grid)

    draw = ImageDraw.Draw(img)
    _frame(draw, width, height, _rgb(palette["info"]), 20, 4)
    return img


@dataclass(frozen=True)
class Style:
    id: str
    label: str
    scopes: tuple
    # None: use the theme's palette.
    palette: Optional[dict] = None
    # "sans", "serif" or "mono" -- resolved to a font file by wrapped.get_font.
    font_family: str = "sans"
    # (width, height, palette) -> Image, or None for wrapped.py's own
    # background (a blurred cover or a gradient in the theme's colours).
    paint: Optional[Callable] = None
    # The monthly slides load recap-bg.png, whose title is part of the art.
    classic_artwork: bool = False


STYLES = {
    style.id: style for style in (
        Style("theme", "Theme", (YEARLY, MONTHLY)),
        Style(
            "classic", "Comic Panels", (MONTHLY,),
            palette={
                "primary": "#ffd700", "secondary": "#b0c4de", "success": "#3cf281",
                "info": "#b0c4de", "warning": "#ffd700", "danger": "#ff6b6b",
                "bg": "#1e252e", "bg_secondary": "#2a333f",
                "text": "#ffffff", "text_muted": "#b0c4de", "is_dark": True,
            },
            classic_artwork=True,
        ),
        Style(
            "halftone", "Halftone", (YEARLY, MONTHLY),
            palette={
                "primary": "#d62828", "secondary": "#1d3557", "success": "#2a9d8f",
                "info": "#1d3557", "warning": "#d62828", "danger": "#d62828",
                "bg": "#ffd23f", "bg_secondary": "#f4a300",
                "text": "#111111", "text_muted": "#3a2f00", "is_dark": False,
            },
            paint=paint_halftone,
        ),
        Style(
            "newsprint", "Newsprint", (YEARLY, MONTHLY),
            palette={
                "primary": "#8c1c13", "secondary": "#5c5346", "success": "#2f5d50",
                "info": "#1f3a5f", "warning": "#8c1c13", "danger": "#8c1c13",
                "bg": "#f4efe4", "bg_secondary": "#ebe4d6",
                "text": "#1b1b1b", "text_muted": "#5c5346", "is_dark": False,
            },
            font_family="serif",
            paint=paint_newsprint,
        ),
        Style(
            "neon", "Neon", (YEARLY, MONTHLY),
            palette={
                "primary": "#ff2bd6", "secondary": "#7b2cbf", "success": "#39ff14",
                "info": "#00f0ff", "warning": "#fff01f", "danger": "#ff3131",
                "bg": "#07070f", "bg_secondary": "#1d0b33",
                "text": "#ffffff", "text_muted": "#b8b5d6", "is_dark": True,
            },
            font_family="mono",
            paint=paint_neon,
        ),
    )
}

# What a recap is drawn in when the user has not chosen: the look each has
# always had.
DEFAULTS = {YEARLY: "theme", MONTHLY: "classic"}


def get_style(style_id):
    return STYLES.get(style_id)


def styles_for(scope):
    return [style for style in STYLES.values() if scope in style.scopes]


def resolve_style(style_id, scope):
    """*style_id* if it is a style this scope can be drawn in, else the
    scope's default. Never raises: the id comes from a query string or a
    stored preference."""
    style = STYLES.get(style_id) if isinstance(style_id, str) else None
    if style is not None and scope in style.scopes:
        return style.id
    return DEFAULTS[scope]
