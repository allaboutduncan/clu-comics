#!/usr/bin/env python3
"""Write the four Catppuccin palette stylesheets (static/css/themes/).

Each flavour is two files: its palette, written here, and the shared rules in
static/css/themes/catppuccin.css, written by hand. The rules only ever read
--ctp-* variables, so the flavours cannot drift apart.

Usage:  python tools/build_catppuccin_themes.py           # write the files
        python tools/build_catppuccin_themes.py --check   # exit 1 if stale

tests/unit/test_themes.py runs the check, so a hand edit to a generated file
fails the suite instead of being overwritten later.

The palette is @catppuccin/palette 1.8.0 (https://catppuccin.com/palette),
copied below so this script needs no network. Role choices follow the
Catppuccin style guide: links blue, success green, warnings yellow, errors
red, information teal, text on an accent fill is base. Bootstrap's
"primary" has no guide entry; it is mauve, the usual Catppuccin accent.

One deviation, Latte only: green, teal, yellow and blue are too light to read
on Latte's near-white base (as low as 2.3:1). Each role colour is darkened
(HLS lightness, hue kept) until it reaches 4.5:1 on base, mantle and crust --
the page, cards and headers. Against base, "accent text on the page" and
"base text on an accent fill" are the same ratio, so one shade serves both.
The dark flavours' roles pass as they are. Alert text (role text on the role
washed 15% into base) and .text-muted are checked the same way, and moved
where they fall short.
"""

import colorsys
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "static", "css", "themes")

PALETTE = {
    "latte": {
        "rosewater": "#dc8a78", "flamingo": "#dd7878", "pink": "#ea76cb", "mauve": "#8839ef", "red": "#d20f39",
        "maroon": "#e64553", "peach": "#fe640b", "yellow": "#df8e1d", "green": "#40a02b", "teal": "#179299",
        "sky": "#04a5e5", "sapphire": "#209fb5", "blue": "#1e66f5", "lavender": "#7287fd", "text": "#4c4f69",
        "subtext1": "#5c5f77", "subtext0": "#6c6f85", "overlay2": "#7c7f93", "overlay1": "#8c8fa1", "overlay0": "#9ca0b0",
        "surface2": "#acb0be", "surface1": "#bcc0cc", "surface0": "#ccd0da", "base": "#eff1f5", "mantle": "#e6e9ef",
        "crust": "#dce0e8",
    },
    "frappe": {
        "rosewater": "#f2d5cf", "flamingo": "#eebebe", "pink": "#f4b8e4", "mauve": "#ca9ee6", "red": "#e78284",
        "maroon": "#ea999c", "peach": "#ef9f76", "yellow": "#e5c890", "green": "#a6d189", "teal": "#81c8be",
        "sky": "#99d1db", "sapphire": "#85c1dc", "blue": "#8caaee", "lavender": "#babbf1", "text": "#c6d0f5",
        "subtext1": "#b5bfe2", "subtext0": "#a5adce", "overlay2": "#949cbb", "overlay1": "#838ba7", "overlay0": "#737994",
        "surface2": "#626880", "surface1": "#51576d", "surface0": "#414559", "base": "#303446", "mantle": "#292c3c",
        "crust": "#232634",
    },
    "macchiato": {
        "rosewater": "#f4dbd6", "flamingo": "#f0c6c6", "pink": "#f5bde6", "mauve": "#c6a0f6", "red": "#ed8796",
        "maroon": "#ee99a0", "peach": "#f5a97f", "yellow": "#eed49f", "green": "#a6da95", "teal": "#8bd5ca",
        "sky": "#91d7e3", "sapphire": "#7dc4e4", "blue": "#8aadf4", "lavender": "#b7bdf8", "text": "#cad3f5",
        "subtext1": "#b8c0e0", "subtext0": "#a5adcb", "overlay2": "#939ab7", "overlay1": "#8087a2", "overlay0": "#6e738d",
        "surface2": "#5b6078", "surface1": "#494d64", "surface0": "#363a4f", "base": "#24273a", "mantle": "#1e2030",
        "crust": "#181926",
    },
    "mocha": {
        "rosewater": "#f5e0dc", "flamingo": "#f2cdcd", "pink": "#f5c2e7", "mauve": "#cba6f7", "red": "#f38ba8",
        "maroon": "#eba0ac", "peach": "#fab387", "yellow": "#f9e2af", "green": "#a6e3a1", "teal": "#94e2d5",
        "sky": "#89dceb", "sapphire": "#74c7ec", "blue": "#89b4fa", "lavender": "#b4befe", "text": "#cdd6f4",
        "subtext1": "#bac2de", "subtext0": "#a6adc8", "overlay2": "#9399b2", "overlay1": "#7f849c", "overlay0": "#6c7086",
        "surface2": "#585b70", "surface1": "#45475a", "surface0": "#313244", "base": "#1e1e2e", "mantle": "#181825",
        "crust": "#11111b",
    },
}

NAMES = {"latte": "Latte", "frappe": "Frappé", "macchiato": "Macchiato", "mocha": "Mocha"}
DARK = {"latte": False, "frappe": True, "macchiato": True, "mocha": True}

# Bootstrap role -> palette colour (style guide, see module docstring).
ROLES = {
    "primary": "mauve",
    "success": "green",
    "info": "teal",
    "warning": "yellow",
    "danger": "red",
    "link": "blue",
    "link-hover": "sky",
}
# Roles that have an .alert-x / subtle background in Bootstrap.
ALERT_ROLES = ("primary", "success", "info", "warning", "danger")
MIN_CONTRAST = 4.5


def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _luminance(hex_color):
    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in _rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    """WCAG 2 contrast ratio between two hex colours."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _shift(hex_color, delta):
    """``hex_color`` with its HLS lightness moved by ``delta``; hue and
    saturation kept, so a deepened yellow is still amber, not brown."""
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in _rgb(hex_color)))
    r, g, b = colorsys.hls_to_rgb(h, min(1.0, max(0.0, l + delta)), s)
    return "#" + "".join(f"{round(c * 255):02x}" for c in (r, g, b))


def mix(a, b, share_of_a):
    """sRGB mix, as CSS ``color-mix(in srgb, a share%, b)`` computes it."""
    return "#" + "".join(
        f"{round(x * share_of_a + y * (1 - share_of_a)):02x}"
        for x, y in zip(_rgb(a), _rgb(b))
    )


def backgrounds(palette):
    """The surfaces text sits on: page, panes (cards, modals) and headers."""
    return [palette[n] for n in ("base", "mantle", "crust")]


def readable(value, against, step_sign):
    """``value``, moved in lightness until it reaches MIN_CONTRAST on every
    colour in ``against``. Raises if it cannot: a silent failing colour is
    worse than a build error."""
    original = value
    for step in range(0, 201):
        value = _shift(original, step_sign * step / 200)
        if min(contrast(value, bg) for bg in against) >= MIN_CONTRAST:
            return value
    raise ValueError(f"{original} cannot reach {MIN_CONTRAST}:1 on {against}")


def role_colour(palette, colour, dark):
    """``colour`` as is, or darkened (Latte) until it reads on every surface."""
    return readable(palette[colour], backgrounds(palette), +1 if dark else -1)


# Alerts and subtle badges draw the role's text on the role washed into base.
SUBTLE_SHARE = 0.15


def emphasis_colour(palette, role_value, dark):
    """Text for the role's subtle background, moved away from it until it
    reads: lighter on a dark flavour, darker on Latte."""
    subtle = mix(role_value, palette["base"], SUBTLE_SHARE)
    return readable(role_value, [subtle], +1 if dark else -1)


def muted_colour(palette):
    """.text-muted: the first of subtext0/subtext1/text that reads on every
    surface (Latte's subtext0 is 3.7:1 on crust)."""
    for name in ("subtext0", "subtext1", "text"):
        if min(contrast(palette[name], bg) for bg in backgrounds(palette)) >= MIN_CONTRAST:
            return name
    return "text"


# Neutral buttons (secondary, light): hover and active move the fill toward
# base, away from the text on it, so a state never reads worse than the
# button at rest. catppuccin.css writes these shares into its color-mix().
NEUTRAL_HOVER_SHARE = 0.8
NEUTRAL_ACTIVE_SHARE = 0.6


def secondary_fill(palette):
    """Secondary fills carry text, not base: the darkest surface that still
    holds text at MIN_CONTRAST."""
    return next(
        s for s in ("surface1", "surface0")
        if contrast(palette["text"], palette[s]) >= MIN_CONTRAST
    )


def raised(dark):
    """A "light" panel (.bg-light, bg-body-secondary): a raised surface on a
    dark flavour; on Latte surface0 is a heavy grey, so crust instead."""
    return "surface0" if dark else "crust"


def on_dark(dark):
    """Light text for surfaces that are dark on every theme (the reader's
    chrome, the bulk-action bars): .text-light, .btn-outline-light. A dark
    flavour's text already is light; Latte's is dark, so its base."""
    return "text" if dark else "base"


def dark_fill(dark):
    """A fill that is dark on every theme (--bs-dark, .bg-dark), carrying
    white or on_dark text. Latte's crust is a light grey, so its text."""
    return "crust" if dark else "text"


def year_in_review_colours(flavour):
    """The flavour's entry in wrapped.THEME_COLORS, from the same choices as
    its stylesheet, so the two cannot drift."""
    p, dark = PALETTE[flavour], DARK[flavour]
    return {
        "primary": role_colour(p, ROLES["primary"], dark),
        "secondary": p[secondary_fill(p)],
        "success": role_colour(p, ROLES["success"], dark),
        "info": role_colour(p, ROLES["info"], dark),
        "warning": role_colour(p, ROLES["warning"], dark),
        "danger": role_colour(p, ROLES["danger"], dark),
        "bg": p["base"],
        "bg_secondary": p[raised(dark)],
        "text": p["text"],
        "text_muted": p[muted_colour(p)],
        "is_dark": dark,
    }


def _svg_url(svg):
    # Percent-encoded the way Bootstrap writes its own icons.
    encoded = (svg.replace("%", "%25").replace("#", "%23").replace("<", "%3c")
               .replace(">", "%3e").replace('"', "'"))
    return 'url("data:image/svg+xml,' + encoded + '")'


def render(flavour):
    p = PALETTE[flavour]
    dark = DARK[flavour]
    lines = [
        f"/* Catppuccin {NAMES[flavour]}: palette for static/css/themes/catppuccin.css.",
        " * GENERATED by tools/build_catppuccin_themes.py -- edit that, not this. */",
        "",
        ":root {",
        f"    color-scheme: {'dark' if dark else 'light'};",
        "",
    ]
    for name, value in p.items():
        r, g, b = _rgb(value)
        lines.append(f"    --ctp-{name}: {value}; --ctp-{name}-rgb: {r}, {g}, {b};")

    lines += ["", "    /* Bootstrap roles. */"]
    for role, colour in ROLES.items():
        value = role_colour(p, colour, dark)
        r, g, b = _rgb(value)
        note = "" if value == p[colour] else f"  /* {colour}, deepened to read on every surface */"
        lines.append(f"    --ctp-{role}: {value}; --ctp-{role}-rgb: {r}, {g}, {b};{note}")
        if role in ALERT_ROLES:
            lines.append(f"    --ctp-{role}-emphasis: {emphasis_colour(p, value, dark)};")
    lines.append(f"    --ctp-muted: var(--ctp-{muted_colour(p)});")
    secondary = secondary_fill(p)
    r, g, b = _rgb(p[secondary])
    lines.append(f"    --ctp-secondary: {p[secondary]}; --ctp-secondary-rgb: {r}, {g}, {b};")

    lines += [
        "",
        "    /* What differs between the light flavour and the dark ones. */",
        # .text-white is white text for a dark fill. On a dark flavour the page
        # itself is that fill; on Latte it stays white (cover overlays etc.).
        f"    --ctp-white-text: {'var(--ctp-text)' if dark else '#ffffff'};",
        # Bootstrap sets color-scheme: dark on every [data-bs-theme=dark],
        # which includes Latte's header.
        f"    --ctp-color-scheme: {'dark' if dark else 'light'};",
        f"    --ctp-raised: var(--ctp-{raised(dark)}); "
        f"--ctp-raised-rgb: var(--ctp-{raised(dark)}-rgb);",
        f"    --ctp-on-dark: var(--ctp-{on_dark(dark)}); "
        f"--ctp-on-dark-rgb: var(--ctp-{on_dark(dark)}-rgb);",
        f"    --ctp-dark-fill: var(--ctp-{dark_fill(dark)}); "
        f"--ctp-dark-fill-rgb: var(--ctp-{dark_fill(dark)}-rgb);",
        # The close X on an accent fill, drawn in base like the text there.
        "    --ctp-close-on-fill: " + _svg_url(
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' "
            f"fill='{p['base']}'><path d='M.293.293a1 1 0 0 1 1.414 0L8 6.586 "
            "14.293.293a1 1 0 1 1 1.414 1.414L9.414 8l6.293 6.293a1 1 0 0 1-1.414 "
            "1.414L8 9.414l-6.293 6.293a1 1 0 0 1-1.414-1.414L6.586 8 .293 1.707a1 "
            "1 0 0 1 0-1.414'/></svg>") + ";",
        "    --ctp-select-chevron: " + _svg_url(
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><path fill='none' "
            f"stroke='{p['text']}' stroke-linecap='round' stroke-linejoin='round' "
            "stroke-width='2' d='m2 5 6 6 6-6'/></svg>") + ";",
        "    --ctp-switch-focus: " + _svg_url(
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='-4 -4 8 8'>"
            f"<circle r='3' fill='{p['lavender']}'/></svg>") + ";",
        # Form check marks, drawn in base like any text on an accent fill.
        "    --ctp-check-mark: " + _svg_url(
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20'><path fill='none' "
            f"stroke='{p['base']}' stroke-linecap='round' stroke-linejoin='round' "
            "stroke-width='3' d='m6 10 3 3 6-6'/></svg>") + ";",
        "    --ctp-check-indeterminate: " + _svg_url(
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20'><path fill='none' "
            f"stroke='{p['base']}' stroke-linecap='round' stroke-linejoin='round' "
            "stroke-width='3' d='M6 10h8'/></svg>") + ";",
        "    --ctp-radio-dot: " + _svg_url(
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='-4 -4 8 8'>"
            f"<circle r='2' fill='{p['base']}'/></svg>") + ";",
        "    --ctp-switch-on: " + _svg_url(
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='-4 -4 8 8'>"
            f"<circle r='3' fill='{p['base']}'/></svg>") + ";",
        "    --ctp-toggler-icon: " + _svg_url(
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 30 30'><path "
            f"stroke='{p['text']}' stroke-linecap='round' stroke-miterlimit='10' "
            "stroke-width='2' d='M4 7h22M4 15h22M4 23h22'/></svg>") + ";",
        "}",
        "",
    ]
    return "\n".join(lines)


def outputs():
    return {
        os.path.join(OUT_DIR, f"catppuccin-{flavour}.css"): render(flavour)
        for flavour in PALETTE
    }


def main(argv):
    check = "--check" in argv
    stale = []
    for path, text in outputs().items():
        current = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if current == text:
            continue
        if check:
            stale.append(os.path.relpath(path, ROOT))
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            print("wrote", os.path.relpath(path, ROOT))
    if stale:
        print("stale, run tools/build_catppuccin_themes.py:", *stale, sep="\n  ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
