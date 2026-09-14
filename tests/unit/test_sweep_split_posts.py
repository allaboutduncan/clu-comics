"""The GetComics sweep downloads the part of a split post that holds the issue.

A post split into several downloads (#542) must go through
``select_parts_for_issue``: reading it page-wide took the first part's
buttons, so every issue in the post resolved to #1-15. The selection logic
itself is tested in tests/mocked/test_getcomics_split_posts.py; the
simulation's mirror of this code in tests/routes/test_downloads_simulation.py.

app.py cannot be imported in tests (importing it starts the scheduler and
spawns monitor.py), so this is asserted against the parsed AST.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")

FUNC = "scheduled_getcomics_download"


@pytest.fixture(scope="module")
def func_node():
    with open(APP_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == FUNC:
            return node
    pytest.fail(f"{FUNC} not found in app.py")


def _calls_named(node, name):
    return [
        child for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == name
    ]


def test_links_come_from_the_selected_part(func_node):
    selects = _calls_named(func_node, "select_parts_for_issue")
    assert len(selects) == 1, "the sweep must pick parts through select_parts_for_issue"
    source = selects[0].args[0]
    assert isinstance(source, ast.Call) and getattr(source.func, "id", None) == "get_result_parts", (
        "select_parts_for_issue must be fed get_result_parts(...), which reuses "
        "the links scrape_and_score_candidate already scraped"
    )


def test_sweep_never_reads_a_post_page_wide(func_node):
    assert not _calls_named(func_node, "get_download_links"), (
        "get_download_links returns only a split post's first part"
    )


def _elif_chain(if_node):
    """The (test, body) of an if and each of its elifs, in order."""
    chain = []
    while isinstance(if_node, ast.If):
        chain.append((if_node.test, if_node.body))
        if_node = if_node.orelse[0] if len(if_node.orelse) == 1 else None
    return chain


def test_dry_run_reports_a_post_with_no_part_for_the_issue(func_node):
    """The sweep queues nothing when no part holds the issue, so the dry run
    must not report it as a match: the "no part" branch comes before the
    dry-run one and records its own status."""
    heads = [
        node for node in ast.walk(func_node)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name) and node.test.id == "pack_skipped"
    ]
    assert len(heads) == 1, "the pack_skipped/dry_run/queue branch disappeared"
    tests = [ast.unparse(test) for test, _ in _elif_chain(heads[0])]
    assert "not parts" in tests and "dry_run" in tests
    assert tests.index("not parts") < tests.index("dry_run"), (
        "a post with no part for the issue would be reported as a match"
    )
    no_part_body = _elif_chain(heads[0])[tests.index("not parts")][1]
    statuses = {
        node.value for stmt in no_part_body for node in ast.walk(stmt)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "no_part_matched" in statuses
