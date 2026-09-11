"""api.py must retire a challenged cloudscraper session, not just drop it.

``resolve_final_url`` swaps the shared getcomics scraper whenever a hop comes
back as a Cloudflare interstitial or raises. The session being replaced has to
be closed: a dropped cloudscraper session is not reclaimed even after
``gc.collect()`` -- its adapter, pool and SSL context stay alive and hold one
TLS connection open (CLOSE_WAIT once Cloudflare hangs up) for the life of the
worker, which is ~1 MB per challenge and over a gigabyte a day on a busy
instance.

Both halves of that swap live in ``core.download_utils.SharedScraper`` and are
tested directly in tests/mocked/test_download_cloudflare.py: closing the old
session, and doing it as a compare-and-swap so two workers challenged at once
cannot each publish a replacement and leak the one that loses. What is asserted
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


def _assignments_to(node, name):
    """Every ``<name> = ...`` under *node*, as (lineno, value) pairs."""
    found = []
    for stmt in ast.walk(node):
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if isinstance(target, ast.Name) and target.id == name:
                found.append((stmt.lineno, stmt.value))
    return found


def _calls_under(node):
    """Every call under *node*, unparsed."""
    return [ast.unparse(n) for n in ast.walk(node) if isinstance(n, ast.Call)]


class TestSharedScraperHoldsTheSession:
    def test_the_module_level_scraper_is_a_shared_scraper(self):
        """A bare session as a module global is what made the swap racy: the
        rebind that replaced it could not be made atomic with the close."""
        module_level = [
            stmt
            for stmt in _api_tree().body
            if isinstance(stmt, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "gc_scraper" for t in stmt.targets
            )
        ]
        assert len(module_level) == 1
        assert ast.unparse(module_level[0].value) == "SharedScraper(_make_gc_scraper)"

    def test_shared_scraper_is_imported_from_the_shared_module(self):
        """It lives in core.download_utils so it stays testable; a local copy in
        api.py would be unreachable from the suite."""
        imported = {
            alias.name
            for node in _api_tree().body
            if isinstance(node, ast.ImportFrom) and node.module == "core.download_utils"
            for alias in node.names
        }
        assert "SharedScraper" in imported


class TestResolveFinalUrlRetiresTheOldScraper:
    def test_the_shared_scraper_is_never_rebound_in_place(self):
        """Every swap must go through retire(), which closes the old session
        under the lock. A direct rebind here drops it on the floor."""
        assignments = _assignments_to(_api_function("resolve_final_url"), "gc_scraper")
        assert assignments == [], (
            "resolve_final_url assigns gc_scraper directly at "
            f"{[lineno for lineno, _ in assignments]}; use gc_scraper.retire(session)"
        )

    def test_both_retry_branches_retire_the_challenged_session(self):
        """One for the Cloudflare interstitial, one for a raising hop."""
        calls = _calls_under(_api_function("resolve_final_url"))
        assert calls.count("gc_scraper.retire(session)") == 2, (
            "expected exactly two scraper swaps in resolve_final_url; a new one "
            "must also retire the session it was challenged on"
        )

    def test_the_session_used_for_the_request_is_held_in_a_local(self):
        """retire() is a compare-and-swap against the session this attempt
        actually used. Calling gc_scraper.current again at the swap would
        compare the shared session against itself and could retire a
        replacement another worker had installed in between."""
        source = ast.unparse(_api_function("resolve_final_url"))
        assert "session = gc_scraper.current" in source
        assert "session.get(current, allow_redirects=False, timeout=30)" in source
        assert "gc_scraper.current.get(" not in source

    def test_no_bare_scraper_is_built_inside_the_resolver(self):
        """_make_gc_scraper() called here would sidestep SharedScraper: the
        session it replaces is then neither closed nor published."""
        calls = _calls_under(_api_function("resolve_final_url"))
        assert "_make_gc_scraper()" not in calls
