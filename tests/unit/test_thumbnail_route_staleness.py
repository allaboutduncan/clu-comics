"""Structural checks on app.get_thumbnail and app.scan_library_task.

app.py cannot be imported in tests -- it starts the scheduler and spawns
monitor.py at import -- so these are asserted against the parsed AST, in the
style of tests/unit/test_folder_thumbnail_orchestrator.py.

What is being protected (all from #548):

* /api/thumbnail used to return the cached JPEG on bare existence. The cache is
  keyed on the file *path*, so every in-place rewrite -- rebuild, crop, page
  removal, a replacement download -- kept its key and the old image was served
  until the container restarted. The explicit regeneration calls fix the paths
  we know about; this check is what catches the ones a future change forgets.
* An 'error' row used to be a life sentence in two places at once: the serving
  route returned error.svg forever, and the startup scan explicitly refused to
  retry. A .cbz that is really a RAR fails, gets rebuilt into a real CBZ, and
  must recover without a restart.
* The in-flight guard must NOT be conditioned on staleness. A stale cache file
  is exactly the state during regeneration, so gating 'processing' on it would
  re-queue a duplicate job on every poll -- a grid of covers against a
  two-worker executor.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")


@pytest.fixture(scope="module")
def tree():
    with open(APP_PATH, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name} not found in app.py")


def _src(tree, name):
    with open(APP_PATH, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    node = _func(tree, name)
    return "\n".join(lines[node.lineno - 1: node.end_lineno])


def _calls(node):
    return [
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]


class TestServingRoute:

    def test_uses_the_shared_cache_path_helper(self, tree):
        """The formula lived in nine places; app.py owning a tenth copy is how
        they drift apart."""
        assert "thumbnail_cache_path" in _calls(_func(tree, "get_thumbnail"))

    def test_consults_staleness(self, tree):
        assert "is_thumbnail_stale" in _calls(_func(tree, "get_thumbnail"))

    def test_every_cache_hit_is_gated_on_freshness(self, tree):
        """Both send_from_directory returns -- the fast path and the one behind
        the completed-job check -- must be guarded, or the second simply
        reinstates the bug."""
        src = _src(tree, "get_thumbnail")
        for line_no, line in enumerate(src.splitlines()):
            if "send_from_directory" in line:
                window = "\n".join(src.splitlines()[max(0, line_no - 3): line_no + 1])
                assert "not stale" in window, (
                    f"unguarded cache hit near: {line.strip()}"
                )

    def test_in_flight_guard_is_not_gated_on_staleness(self, tree):
        """A 'processing' row means a job is already running; re-queuing on
        every poll because the file on disk is still the old one would pile up
        duplicates."""
        src = _src(tree, "get_thumbnail")
        for line in src.splitlines():
            if '"processing"' in line and "==" in line:
                assert "stale" not in line, (
                    "the in-flight guard must not depend on staleness"
                )

    def test_error_rows_are_reconsidered_when_the_file_changes(self, tree):
        assert "file_changed_since" in _calls(_func(tree, "get_thumbnail"))


class TestStartupScan:

    def test_retries_errored_jobs(self, tree):
        """The old code documented its refusal to retry ('assume errors are
        permanent until file changes'), which turned one unwritable-cache
        failure into a permanently broken thumbnail."""
        src = _src(tree, "scan_library_task")
        assert "assume errors are permanent" not in src

        lines = src.splitlines()
        idx = next(
            (i for i, l in enumerate(lines) if 'status == "error"' in l),
            None,
        )
        assert idx is not None, "the errored-job branch is gone from the scan"
        branch = "\n".join(lines[idx: idx + 14])
        assert "should_process = True" in branch, (
            "errored thumbnail jobs are still never retried"
        )

    def test_uses_the_shared_cache_path_helper(self, tree):
        assert "thumbnail_cache_path" in _calls(_func(tree, "scan_library_task"))


class TestGenerators:
    """Both generators must delegate rather than keep a private copy of the
    extract/resize/save body -- that duplication is the root cause of #548."""

    def test_background_task_delegates(self, tree):
        assert "regenerate_thumbnail" in _calls(_func(tree, "generate_thumbnail_task"))

    def test_sync_generator_delegates(self, tree):
        assert "regenerate_thumbnail" in _calls(_func(tree, "generate_thumbnail_sync"))

    def test_no_module_keeps_its_own_copy_of_the_write(self, tree):
        """PIL saves straight onto the cache path are what fail with EACCES on a
        root-owned file. Every thumbnail write must go through
        core.thumbnail_cache.write_cached_thumbnail."""
        src = open(APP_PATH, encoding="utf-8").read()
        assert 'img.save(cache_path' not in src
