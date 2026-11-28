"""The GitHub repo browser's rows must be able to indent.

Rows get a per-depth inline ``padding-left``. Bootstrap's ``px-*``/``ps-*``
utilities are ``!important`` and silently override it, which rendered the
whole tree flat at one indent.
"""
import re
from pathlib import Path

JS = Path(__file__).resolve().parents[2] / "static" / "js" / "reading_list.js"


def test_tree_rows_do_not_use_important_left_padding():
    src = JS.read_text(encoding="utf-8")
    row_classes = re.findall(r"className = '(tree-item[^']*)'", src)
    assert len(row_classes) == 2, row_classes  # folder header + file row
    for classes in row_classes:
        assert not re.search(r"\b(px|ps)-\d\b", classes), classes


def test_tree_rows_indent_by_depth():
    src = JS.read_text(encoding="utf-8")
    assert src.count("style.paddingLeft = _treeIndent(depth)") == 2


def test_shift_click_selects_a_range_of_visible_files():
    """Shift is read on mousedown (a label click reaches the checkbox as a
    synthetic event), and the range covers visible checkboxes only."""
    src = JS.read_text(encoding="utf-8")
    assert "addEventListener('mousedown', e => { _treeShiftDown = e.shiftKey; })" in src
    assert ".filter(c => c.offsetParent !== null)" in src
    assert "_bindTreeRangeSelect(container);" in src
    # One handler counts the selection; an inline onchange would bypass the range.
    assert 'onchange="updateSelectedCount()"' not in src
