"""How wide the page shell, nav and footer are.

``page_width`` is a site default (``PAGE_WIDTH`` in ``SETTINGS_SCHEMA``) with a
per-user override in ``user_settings``, resolved per request by
``routes.auth`` exactly like the theme.

- ``auto``: each page decides. ``base.html`` gives every page ``container-lg``
  and the cover-grid pages override ``{% block page_shell %}`` to go full
  width -- the behaviour before this setting existed.
- ``wide``: every page is full width.
- ``constrained``: every page is ``container-lg``, cover grids included.

The nav and footer follow whatever the page resolved to, so the brand and the
page's first column line up. They used to be a third width (plain
``container``) that matched neither.

Pure functions only: templates call them through the context processor, and
the tests call them directly.
"""

PAGE_WIDTHS = ("auto", "wide", "constrained")
DEFAULT_PAGE_WIDTH = "auto"

WIDE_SHELL = "container-fluid clu-shell-wide"
CONSTRAINED_SHELL = "container-lg"


def is_valid_page_width(value):
    return value in PAGE_WIDTHS


def shell_classes(mode, page_shell=""):
    """The page shell's classes for ``mode``.

    ``page_shell`` is what the page's own ``page_shell`` block rendered: what
    it wants under ``auto``. An unknown mode reads as ``auto``, so a corrupt
    preference degrades to the old behaviour rather than breaking every page.
    """
    if mode == "wide":
        return WIDE_SHELL
    if mode == "constrained":
        return CONSTRAINED_SHELL
    return " ".join(str(page_shell or "").split()) or CONSTRAINED_SHELL


def is_wide(shell):
    return "container-fluid" in str(shell or "").split()


def chrome_container(shell=None):
    """The nav/footer container class for a page whose shell is ``shell``.

    A template that does not extend ``base.html`` (login) has no shell and
    gets the constrained width.
    """
    if is_wide(shell):
        return "container-fluid clu-shell-wide-chrome"
    return CONSTRAINED_SHELL
