"""api.py must retire a challenged cloudscraper session, not just drop it.

``resolve_final_url`` rebinds the module-level ``gc_scraper`` whenever a
getcomics hop comes back as a Cloudflare interstitial or raises. The session
being replaced has to be closed: a dropped cloudscraper session is not
reclaimed even after ``gc.collect()`` -- its adapter, pool and SSL context stay
alive and hold one TLS connection open (CLOSE_WAIT once Cloudflare hangs up)
for the life of the worker, which is ~1 MB per challenge and over a gigabyte a
day on a busy instance.

The swap lives in ``core.download_utils.SharedSession`` and is tested
directly below; ``replace_session`` (the close) in
tests/mocked/test_download_cloudflare.py. What is asserted
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
    def test_module_level_session_is_a_shared_holder(self):
        """The workers share one holder, not a rebindable global: rebinding a
        global from each worker let two of them rebuild it at once, or swap out
        a session another had just put FlareSolverr's clearance on."""
        module_level = [
            stmt
            for stmt in _api_tree().body
            if isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id in ("gc_session", "gc_scraper")
                    for t in stmt.targets)
        ]
        assert len(module_level) == 1
        assert ast.unparse(module_level[0]) == "gc_session = SharedSession(_make_gc_scraper)"

    def test_resolve_final_url_never_rebinds_a_global(self):
        fn = _api_function("resolve_final_url")
        assert not any(isinstance(n, ast.Global) for n in ast.walk(fn))

    def test_each_hop_takes_one_local_reference(self):
        values = [ast.unparse(v) for _, v in
                  _gc_scraper_assignments(_api_function("resolve_final_url"))]
        assert values.count("gc_session.get() if is_gc else None") == 1

    def test_both_retry_branches_replace_the_session(self):
        """One for the Cloudflare interstitial, one for a raising hop."""
        values = [ast.unparse(v) for _, v in
                  _gc_scraper_assignments(_api_function("resolve_final_url"))]
        replaces = [v for v in values if "get()" not in v]
        assert replaces == ["gc_session.replace(gc_scraper)"] * 2, (
            "a replaced cloudscraper session must go through "
            "SharedSession.replace(), which closes it and rebuilds it only "
            "once per failure"
        )

    def test_shared_session_is_imported_from_the_shared_module(self):
        """It lives in core.download_utils so it stays testable; a local copy in
        api.py would be unreachable from the suite."""
        imported = {
            alias.name
            for node in _api_tree().body
            if isinstance(node, ast.ImportFrom) and node.module == "core.download_utils"
            for alias in node.names
        }
        assert "SharedSession" in imported


class _FakeSession:
    def __init__(self, n):
        self.n = n
        self.closed = 0

    def close(self):
        self.closed += 1


class TestSharedSession:
    def _holder(self):
        from core.download_utils import SharedSession

        built = []

        def make():
            built.append(_FakeSession(len(built)))
            return built[-1]

        return SharedSession(make), built

    def test_replace_builds_a_new_session_and_closes_the_old(self):
        holder, built = self._holder()
        first = holder.get()
        second = holder.replace(first)
        assert second is not first and holder.get() is second
        assert first.closed == 1 and second.closed == 0

    def test_a_stale_failure_does_not_rebuild_again(self):
        """Two workers saw the same session fail: one rebuild, not two, and the
        second worker must not close the replacement the first just built."""
        holder, built = self._holder()
        first = holder.get()
        second = holder.replace(first)
        assert holder.replace(first) is second
        assert len(built) == 2
        assert first.closed == 1 and second.closed == 0

    def test_a_local_reference_survives_another_workers_replace(self):
        """A worker that put clearance cookies on its session keeps that object
        for its retry even if another worker replaced the shared one."""
        holder, _ = self._holder()
        mine = holder.get()
        mine.cookies = "cleared"
        holder.replace(holder.get())
        assert mine.cookies == "cleared"

    def test_concurrent_failures_rebuild_once(self):
        import threading

        holder, built = self._holder()
        first = holder.get()
        barrier = threading.Barrier(8)
        results = []

        def worker():
            barrier.wait()
            results.append(holder.replace(first))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(built) == 2
        assert all(r is built[1] for r in results)
        assert first.closed == 1

    def test_a_failing_build_keeps_the_old_session(self):
        from core.download_utils import SharedSession

        calls = []

        def make():
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("boom")
            return _FakeSession(0)

        holder = SharedSession(make)
        first = holder.get()
        with pytest.raises(RuntimeError):
            holder.replace(first)
        assert holder.get() is first and first.closed == 0
