"""api.py must retire a challenged cloudscraper session, not just drop it.

``resolve_final_url`` rebinds the module-level ``gc_scraper`` whenever a
getcomics hop comes back as a Cloudflare interstitial or raises. The session
being replaced has to be closed: a dropped cloudscraper session is not
reclaimed even after ``gc.collect()`` -- its adapter, pool and SSL context stay
alive and hold one TLS connection open (CLOSE_WAIT once Cloudflare hangs up)
for the life of the worker, which is ~1 MB per challenge and over a gigabyte a
day on a busy instance.

The closing itself lives in ``core.download_utils.replace_session`` and is
tested directly in tests/mocked/test_download_cloudflare.py. What is asserted
here is that api.py actually *goes through* it, because api.py starts worker
threads and a cloudscraper session at import time and cannot be imported by the
suite -- the same reason test_download_notify_hooks.py and
test_download_auto_retry.py assert against its AST. Without this, a
``gc_scraper = _make_gc_scraper()`` reinstated at either branch would restore
the leak with every test still green.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API_PATH = os.path.join(PROJECT_ROOT, "api.py")


def _api_tree():
    with open(API_PATH, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _api_function(name):
    """The AST of an api.py function, without importing api."""
    for node in ast.walk(_api_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name} not found in api.py")


def _gc_scraper_assignments(node):
    """Every ``gc_scraper = ...`` under *node*, as (lineno, value) pairs."""
    found = []
    for stmt in ast.walk(node):
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if isinstance(target, ast.Name) and target.id == "gc_scraper":
                found.append((stmt.lineno, stmt.value))
    return found


class TestResolveFinalUrlRetiresTheOldScraper:
    def test_module_level_scraper_is_still_built_directly(self):
        """Sanity check on the fixture: the one assignment outside the function
        is the initial build, which has no predecessor to close."""
        module_level = [
            stmt
            for stmt in _api_tree().body
            if isinstance(stmt, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "gc_scraper" for t in stmt.targets
            )
        ]
        assert len(module_level) == 1
        assert ast.unparse(module_level[0].value) == "_make_gc_scraper()"

    def test_both_retry_branches_replace_the_session(self):
        """One for the Cloudflare interstitial, one for a raising hop."""
        assignments = _gc_scraper_assignments(_api_function("resolve_final_url"))
        assert len(assignments) == 2, (
            "expected exactly two scraper swaps in resolve_final_url; a new one "
            "must also close the session it replaces"
        )

    def test_no_swap_drops_the_old_session_on_the_floor(self):
        for lineno, value in _gc_scraper_assignments(_api_function("resolve_final_url")):
            call = ast.unparse(value)
            assert call == "replace_session(gc_scraper, _make_gc_scraper)", (
                f"api.py:{lineno} rebinds gc_scraper as `{call}`. A replaced "
                "cloudscraper session must go through replace_session(), which "
                "closes it -- dropping it leaks a TLS connection and ~1 MB for "
                "the life of the worker."
            )

    def test_replace_session_is_imported_from_the_shared_module(self):
        """It lives in core.download_utils so it stays testable; a local copy in
        api.py would be unreachable from the suite."""
        imported = {
            alias.name
            for node in _api_tree().body
            if isinstance(node, ast.ImportFrom) and node.module == "core.download_utils"
            for alias in node.names
        }
        assert "replace_session" in imported
