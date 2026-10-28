"""The /collection grid keeps folder, view, letter, search and page in the URL.

There is no JS test runner in this repo, so the wiring is asserted
structurally against ``static/js/collection.js`` (same approach as
``test_issue_badge.py``). The behaviour was checked in a real browser:
filter -> open folder -> Back restores the filter, Forward still works, a
reload keeps All Books + letter, and the first load adds no history entry.
"""
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COLLECTION_JS = os.path.join(REPO, "static", "js", "collection.js")


@pytest.fixture(scope="module")
def js_src():
    with open(COLLECTION_JS, encoding="utf-8") as fh:
        return fh.read()


def _code_only(block):
    """Drop // comments so an assertion cannot match prose about the code."""
    return "\n".join(line.split("//", 1)[0] for line in block.splitlines())


def _function_body(src, name):
    """Source of a top-level JS function, up to the next top-level declaration."""
    m = re.search(rf"^(?:async )?function {name}\(.*?(?=^(?:async )?function |\Z)", src, re.S | re.M)
    assert m, f"function {name} not found -- was it renamed?"
    return _code_only(m.group(0))


def test_only_sync_nav_url_pushes_history(js_src):
    """A push anywhere else (the old loadDirectory did one per call) adds a
    duplicate entry on first load and wipes Forward when Back re-loads."""
    pushes = [m.start() for m in re.finditer(r"history\.pushState\(", _code_only(js_src))]
    assert len(pushes) == 1
    assert "history.pushState(" in _function_body(js_src, "syncNavUrl")


def test_popstate_restores_without_loading_directly(js_src):
    m = re.search(r"window\.onpopstate = .*?\n\};", js_src, re.S)
    assert m, "onpopstate handler not found"
    handler = _code_only(m.group(0))
    assert "restoreNavState(" in handler
    assert "loadDirectory(" not in handler


def test_restore_suppresses_history_writes(js_src):
    body = _function_body(js_src, "restoreNavState")
    assert "navRestoring = true" in body
    assert "navRestoring = false" in body
    assert "if (navRestoring) return;" in _function_body(js_src, "syncNavUrl")


@pytest.mark.parametrize("name", [
    "loadDirectory", "loadAllBooks", "fetchAllBooksPage", "loadMissingXml",
    "filterItems", "changePage", "onGridSearch", "changeItemsPerPage",
])
def test_state_changes_write_the_url(js_src, name):
    assert "syncNavUrl()" in _function_body(js_src, name)


@pytest.mark.parametrize("name", ["loadDirectory", "loadAllBooks", "loadMissingXml"])
def test_scroll_saved_before_the_grid_is_torn_down(js_src, name):
    assert "rememberScroll()" in _function_body(js_src, name)


def test_url_params_are_validated(js_src):
    body = _function_body(js_src, "readNavStateFromUrl")
    assert "NAV_VIEWS.includes(view)" in body
    assert "/^[A-Z]$/" in body
    assert "page > 0" in body
