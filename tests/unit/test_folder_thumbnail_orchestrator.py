"""Structural checks on app.generate_folder_thumbnail_internal.

The composers live in core/folder_thumbnails.py and are unit-tested directly
(tests/unit/test_folder_thumbnail_styles.py). What is left in app.py is the I/O
around them, and app.py cannot be imported in tests -- it starts the scheduler
and spawns monitor.py at import -- so the properties that matter are asserted
against the parsed AST instead.

Three of them are load-bearing and easy to break with an innocent-looking edit:

* The pinned cover is prepended, *then* the list is truncated. Truncating first
  would silently drop the pin on any style that takes fewer covers than the
  folder has -- which is every folder in Single Image mode.
* Existing art is cleared using the shared FOLDER_THUMBNAIL_EXTENSIONS list.
  The old inline list omitted ``.webp``, so an uploaded folder.webp survived the
  write and kept winning find_folder_thumbnail -- the new image was generated
  and then never shown.
* The nested folder-icon overlay is gated on the preference, not applied
  unconditionally.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")

FUNC = "generate_folder_thumbnail_internal"


@pytest.fixture(scope="module")
def func_node():
    with open(APP_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == FUNC:
            return node
    pytest.fail(f"{FUNC} not found in app.py")


@pytest.fixture(scope="module")
def source_lines(func_node):
    with open(APP_PATH, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    return lines[func_node.lineno - 1:func_node.end_lineno]


def _line_index(source_lines, needle):
    for i, line in enumerate(source_lines):
        if needle in line:
            return i
    pytest.fail(f"{needle!r} not found in {FUNC}")


class TestSignature:

    def test_takes_a_style_override(self, func_node):
        """The sweeps and the auto-generator all share one renderer."""
        args = [a.arg for a in func_node.args.args]
        assert args == ["folder_path", "overwrite", "style"]

    def test_overwrite_still_defaults_to_true(self, func_node):
        """The background worker relies on passing False explicitly."""
        defaults = dict(zip([a.arg for a in func_node.args.args[-2:]],
                            func_node.args.defaults))
        assert isinstance(defaults["overwrite"], ast.Constant)
        assert defaults["overwrite"].value is True
        # style=None means "read the site preference".
        assert defaults["style"].value is None


class TestPinnedCover:

    def test_reads_the_pin(self, func_node):
        calls = [
            n for n in ast.walk(func_node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "get_folder_pin"
        ]
        assert len(calls) == 1, "the folder pin must be consulted exactly once"

    def test_pin_is_prepended_before_the_list_is_truncated(self, source_lines):
        """Truncating first would drop the pin in every single-cover style."""
        prepend = _line_index(source_lines, "comic_files = [pinned] +")
        truncate = _line_index(source_lines, "comic_files = comic_files[:max_covers]")
        assert prepend < truncate

    def test_a_stale_pin_is_ignored(self, source_lines):
        """A deleted or renamed file must not leave the folder without art."""
        guard = _line_index(source_lines, "if pinned and os.path.isfile(pinned):")
        prepend = _line_index(source_lines, "comic_files = [pinned] +")
        assert guard < prepend


class TestClearingExistingArt:

    def test_uses_the_shared_extension_list(self, func_node):
        names = {
            n.id for n in ast.walk(func_node) if isinstance(n, ast.Name)
        }
        assert "FOLDER_THUMBNAIL_EXTENSIONS" in names

    def test_does_not_reinline_an_extension_list(self, source_lines):
        """A second hardcoded list is how the .webp gap appeared the first time."""
        body = "\n".join(source_lines)
        assert '".jpeg"' not in body
        assert '".gif"' not in body


class TestNestedOverlay:

    def test_overlay_is_gated_on_the_preference(self, source_lines):
        assert any(
            "is_nested and ft.nested_overlay_enabled()" in line
            for line in source_lines
        ), "the folder-icon overlay must respect the site setting"


class TestWriteGuard:

    def test_may_write_is_checked_first(self, source_lines):
        """Auto-generation must never replace art the user uploaded."""
        guard = _line_index(source_lines, "ft.may_write(folder_path, overwrite)")
        compose = _line_index(source_lines, 'spec["compose"](cached_thumbs)')
        assert guard < compose

    def test_permissions_are_normalised_after_writing(self, source_lines):
        """Otherwise the file lands root:0600 and cannot be served back."""
        save = _line_index(source_lines, 'final_canvas.save(output_path, "PNG")')
        perms = _line_index(source_lines, "match_parent_permissions(output_path)")
        assert save < perms

    def test_the_cached_thumbnail_flag_is_updated(self, source_lines):
        """/api/browse serves folder art off this flag, not a stat call."""
        assert any(
            "set_directory_has_thumbnail(folder_path, True)" in line
            for line in source_lines
        )
