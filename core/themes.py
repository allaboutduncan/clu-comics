"""Bootswatch theme catalog.

Single source of truth for the themes offered in the UI. Lives in ``core`` so
both ``app.py`` (context processor, site default) and ``routes/auth.py``
(per-user override validation) can import it — ``routes`` cannot import
``app.py`` without a circular import.
"""

# Bootswatch themes that ship a dark palette. Setting data-bs-theme="dark" for
# these activates Bootstrap 5.3's dark helper variables (emphasis/tertiary-bg/
# etc.), which the themes otherwise leave at light-mode defaults at :root.
DARK_BOOTSWATCH_THEMES = {"cyborg", "darkly", "slate", "solar", "superhero", "vapor"}

# Theme ids in the order they are offered. "default" is stock Bootstrap (no
# Bootswatch stylesheet). Labels are derived below so the "(Dark)" suffix can
# never drift from DARK_BOOTSWATCH_THEMES again — it previously did, with the
# picker labelling quartz "(Dark)" while the rendering set omitted it.
THEME_ORDER = (
    "default",
    "brite",
    "cerulean",
    "cosmo",
    "cyborg",
    "darkly",
    "flatly",
    "journal",
    "litera",
    "lumen",
    "lux",
    "materia",
    "minty",
    "morph",
    "pulse",
    "quartz",
    "sandstone",
    "simplex",
    "sketchy",
    "slate",
    "solar",
    "spacelab",
    "superhero",
    "united",
    "vapor",
    "yeti",
    "zephyr",
)

_LABEL_OVERRIDES = {"default": "Default (Bootstrap)"}


def _label(theme_id):
    base = _LABEL_OVERRIDES.get(theme_id, theme_id.title())
    return f"{base} (Dark)" if theme_id in DARK_BOOTSWATCH_THEMES else base


# [(id, label), ...] — what the theme <select> renders.
BOOTSWATCH_THEMES = [(t, _label(t)) for t in THEME_ORDER]

THEME_IDS = frozenset(THEME_ORDER)


def is_valid_theme(theme_id):
    """True if ``theme_id`` is a theme we actually offer."""
    return theme_id in THEME_IDS


def is_dark_theme(theme_id):
    """True if ``theme_id`` needs data-bs-theme="dark"."""
    return theme_id in DARK_BOOTSWATCH_THEMES
