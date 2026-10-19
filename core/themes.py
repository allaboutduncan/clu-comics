"""Bootswatch theme catalog.

Single source of truth for the themes offered in the UI. Lives in ``core`` so
both ``app.py`` (context processor, site default) and ``routes/auth.py``
(per-user override validation) can import it — ``routes`` cannot import
``app.py`` without a circular import.
"""

# Themes with a dark palette (Bootswatch and local). Setting data-bs-theme="dark"
# for these activates Bootstrap 5.3's dark helper variables (emphasis/
# tertiary-bg/etc.), which the themes otherwise leave at light-mode defaults.
DARK_THEMES = {
    "cyborg", "darkly", "slate", "solar", "superhero", "vapor", "gruvbox-dark",
}

# Themes Bootswatch does not ship. Each is stock Bootstrap (BOOTSTRAP_CSS)
# plus a stylesheet of ours under static/, and a preview image of ours for
# the picker -- bootswatch.com has no thumbnail for them.
BOOTSWATCH_CSS = "https://cdn.jsdelivr.net/npm/bootswatch@5.3.8/dist/{theme}/bootstrap.min.css"
BOOTSWATCH_PREVIEW = "https://bootswatch.com/{theme}/thumbnail.png"
# The stock Bootstrap the "default" theme has always used.
DEFAULT_BOOTSTRAP_CSS = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/css/bootstrap.min.css"
# Local themes are written against the release Bootswatch builds on.
BOOTSTRAP_CSS = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css"

LOCAL_THEMES = {
    "gruvbox-dark": {
        "label": "Gruvbox",
        "css": "css/themes/gruvbox-dark.css",
        "preview": "images/themes/gruvbox-dark.png",
    },
}

# Theme ids in the order they are offered. "default" is stock Bootstrap (no
# Bootswatch stylesheet). Labels are derived below so the "(Dark)" suffix can
# never drift from DARK_THEMES again — it previously did, with the
# picker labelling quartz "(Dark)" while the rendering set omitted it.
THEME_ORDER = (
    "default",
    "brite",
    "cerulean",
    "cosmo",
    "cyborg",
    "darkly",
    "flatly",
    "gruvbox-dark",
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
    base = (LOCAL_THEMES.get(theme_id, {}).get("label")
            or _LABEL_OVERRIDES.get(theme_id, theme_id.title()))
    return f"{base} (Dark)" if theme_id in DARK_THEMES else base


# [(id, label), ...] — what the theme <select> renders.
BOOTSWATCH_THEMES = [(t, _label(t)) for t in THEME_ORDER]

THEME_IDS = frozenset(THEME_ORDER)


def is_valid_theme(theme_id):
    """True if ``theme_id`` is a theme we actually offer."""
    return theme_id in THEME_IDS


def is_dark_theme(theme_id):
    """True if ``theme_id`` needs data-bs-theme="dark"."""
    return theme_id in DARK_THEMES


def theme_stylesheets(theme_id):
    """``(bootstrap_url, local_css_or_None)`` for ``theme_id``.

    ``local_css`` is a path under static/, loaded after Bootstrap. An unknown
    id falls back to the default theme rather than a 404 stylesheet.
    """
    local = LOCAL_THEMES.get(theme_id)
    if local:
        return BOOTSTRAP_CSS, local["css"]
    if theme_id != "default" and is_valid_theme(theme_id):
        return BOOTSWATCH_CSS.format(theme=theme_id), None
    return DEFAULT_BOOTSTRAP_CSS, None


def theme_preview_url(theme_id, static_url):
    """The picker's preview image for ``theme_id``.

    ``static_url`` maps a path under static/ to its URL -- pass
    ``url_for('static', ...)`` so a local preview honours the proxy prefix
    the same way its stylesheet does.
    """
    local = LOCAL_THEMES.get(theme_id)
    if local:
        return static_url(local["preview"])
    return BOOTSWATCH_PREVIEW.format(theme=theme_id)


def theme_previews(static_url):
    """{id: preview url} for every offered theme -- what the picker renders."""
    return {t: theme_preview_url(t, static_url) for t in THEME_ORDER}
