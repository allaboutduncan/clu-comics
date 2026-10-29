"""core/page_width.py: how the page_width setting picks the shell and chrome."""
import pytest

from core.page_width import (
    CONSTRAINED_SHELL,
    PAGE_WIDTHS,
    WIDE_SHELL,
    chrome_container,
    is_valid_page_width,
    shell_classes,
)
from core.settings import SETTINGS_SCHEMA

# What base.html's page_shell block renders for an ordinary page and for a
# cover grid that overrides it.
DEFAULT_PAGE = "container-lg"
COVER_GRID = "container-fluid clu-shell-wide"


@pytest.mark.parametrize("mode, page_shell, expected", [
    ("auto", DEFAULT_PAGE, CONSTRAINED_SHELL),
    ("auto", COVER_GRID, WIDE_SHELL),
    ("wide", DEFAULT_PAGE, WIDE_SHELL),
    ("wide", COVER_GRID, WIDE_SHELL),
    ("constrained", DEFAULT_PAGE, CONSTRAINED_SHELL),
    ("constrained", COVER_GRID, CONSTRAINED_SHELL),
])
def test_shell_classes(mode, page_shell, expected):
    assert shell_classes(mode, page_shell) == expected


def test_auto_normalises_block_whitespace():
    # A block rendered across lines must not leak newlines into class="".
    assert shell_classes("auto", "\n  container-fluid\n clu-shell-wide \n") == COVER_GRID


@pytest.mark.parametrize("mode", [None, "", "bogus", "WIDE"])
def test_unknown_mode_reads_as_auto(mode):
    assert shell_classes(mode, COVER_GRID) == COVER_GRID
    assert shell_classes(mode, DEFAULT_PAGE) == DEFAULT_PAGE


def test_empty_block_falls_back_to_constrained():
    assert shell_classes("auto", "") == CONSTRAINED_SHELL
    assert shell_classes("auto", None) == CONSTRAINED_SHELL


def test_chrome_follows_the_shell():
    assert chrome_container(WIDE_SHELL) == "container-fluid clu-shell-wide-chrome"
    assert chrome_container(CONSTRAINED_SHELL) == "container-lg"
    # login.html and other templates without a shell.
    assert chrome_container(None) == "container-lg"
    assert chrome_container("") == "container-lg"


def test_validation():
    for mode in PAGE_WIDTHS:
        assert is_valid_page_width(mode)
    for junk in (None, "", "full", "Auto"):
        assert not is_valid_page_width(junk)


def test_schema_default_is_auto():
    """Auto is the behaviour from before the setting existed; an upgrade must
    not change anybody's layout."""
    spec = SETTINGS_SCHEMA["PAGE_WIDTH"]
    assert spec.pref == "page_width"
    assert spec.default == "auto"
    assert spec.category == "personalization"
