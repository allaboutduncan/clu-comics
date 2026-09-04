"""Structural guard: every provider that can produce a near-miss must also
have a selected_match dispatch branch in /api/search-metadata.

The bug this guards against (PR #539's review, finding 1): try_inducks (the
batch path) could put a file into result['unmatched'] with can_select_issue
True via note_near_miss('inducks', ...), which opens the issue-picker modal
(CLU.resolveUnmatchedIssues). But the selected_match dispatch in
/api/search-metadata had no 'inducks' branch, so picking an issue always
404'd -- the picker opened but could never apply. The single-file cascade's
own near-miss recorder (_record_near_miss) has the identical shape and the
identical failure mode.

Parsed via ast rather than imported: this asserts a structural property of
the source (every provider name that reaches note_near_miss / _record_near_miss
is also a literal compared against `provider` in the selection dispatch), not
runtime behaviour, so it stays meaningful even if the module can't be
imported in some environment. Mirrors the AST-assertion style used for app.py
elsewhere in this suite (e.g. tests/unit/test_wanted_digest_hook.py).
"""
import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
METADATA_PATH = os.path.join(PROJECT_ROOT, "routes", "metadata.py")


@pytest.fixture(scope="module")
def tree():
    with open(METADATA_PATH, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _string_literals(node):
    """String constants directly inside a tuple/list/set literal, or the
    literal itself if it already is one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def _near_miss_providers(tree):
    """Provider slugs passed to note_near_miss(...) or
    _record_near_miss(near_misses, ...) anywhere in the module."""
    providers = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        if name == "note_near_miss" and node.args:
            providers.update(_string_literals(node.args[0]))
        elif name == "_record_near_miss" and len(node.args) >= 2:
            providers.update(_string_literals(node.args[1]))
    return providers


def _provider_literals_from_test(test):
    """String literals compared against a bare `provider` name in
    `provider == 'x'` or `provider in ('x', 'y')`."""
    if not isinstance(test, ast.Compare):
        return []
    if not (isinstance(test.left, ast.Name) and test.left.id == "provider"):
        return []
    literals = []
    for op, comparator in zip(test.ops, test.comparators):
        if isinstance(op, ast.Eq):
            literals.extend(_string_literals(comparator))
        elif isinstance(op, ast.In):
            literals.extend(_string_literals(comparator))
    return literals


def _selection_dispatch_providers(tree):
    """Providers handled in /api/search-metadata's `if selected_match:`
    if/elif chain (the dispatch that runs after the user picks from the
    selection or issue-picker modal)."""
    func = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "search_metadata"),
        None,
    )
    assert func is not None, "search_metadata not found in routes/metadata.py"

    selected_if = next(
        (n for n in ast.walk(func)
         if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
         and n.test.id == "selected_match"),
        None,
    )
    assert selected_if is not None, "`if selected_match:` block not found"

    chain_start = next(
        (stmt for stmt in selected_if.body
         if isinstance(stmt, ast.If) and _provider_literals_from_test(stmt.test)),
        None,
    )
    assert chain_start is not None, "provider dispatch if/elif chain not found"

    providers = []
    node = chain_start
    while node is not None:
        providers.extend(_provider_literals_from_test(node.test))
        if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            node = node.orelse[0]
        else:
            node = None
    return set(providers)


class TestSelectionDispatchCoversEveryNearMissProvider:

    def test_extraction_actually_finds_the_known_providers(self, tree):
        """Pin the extraction itself, so a refactor of either shape can't
        silently make the real assertion below vacuous."""
        near_miss = _near_miss_providers(tree)
        assert {"metron", "comicvine", "comicvine_sqlite", "gcd", "gcd_api",
                "inducks"} <= near_miss

        dispatched = _selection_dispatch_providers(tree)
        assert {"metron", "comicvine", "comicvine_sqlite", "gcd", "gcd_api",
                "inducks"} <= dispatched

    def test_every_near_miss_provider_has_a_dispatch_branch(self, tree):
        near_miss = _near_miss_providers(tree)
        dispatched = _selection_dispatch_providers(tree)
        missing = near_miss - dispatched
        assert not missing, (
            f"{sorted(missing)} can produce a near-miss / issue-picker "
            f"(note_near_miss or _record_near_miss) but has no "
            f"`selected_match` dispatch branch in /api/search-metadata -- "
            f"picking an issue for it will 404. See PR #539's review."
        )
