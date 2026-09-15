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

And, from the AppleDouble report that followed:

* scan_library_task must enumerate the same set as build_file_index. It is the
  only library walker whose output never reaches file_index, so nothing
  downstream catches its mistakes -- it filtered on extension alone and queued
  every "._Foo.cbz" resource fork in the library, which then failed and, since
  #548 made errored rows retryable, was re-queued at every boot.
* A 'skipped' row is terminal and the serving route must say so. It had no
  branch of its own, so such a request fell through to the 'processing' upsert
  and submitted another doomed job on every poll.
* The job-row writes must go through core.thumbnail_cache, which stamps
  file_mtime. A raw UPDATE/INSERT that omits it leaves NULL, which the scan
  reads as "mtime unknown" and re-queues on every restart.
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
        """Both cache-hit returns -- the fast path and the one behind the
        completed-job check -- must be guarded, or the second simply reinstates
        the bug.

        The scanned token covers the serve helper as well as a direct
        send_from_directory: the route delegates through
        _serve_cached_thumbnail so an unreadable JPEG falls through instead of
        500ing, and looking only for the old name would leave this test passing
        while checking nothing.
        """
        src = _src(tree, "get_thumbnail")
        hits = 0
        for line_no, line in enumerate(src.splitlines()):
            if "send_from_directory" in line or "_serve_cached_thumbnail" in line:
                hits += 1
                window = "\n".join(src.splitlines()[max(0, line_no - 4): line_no + 1])
                assert "not stale" in window, (
                    f"unguarded cache hit near: {line.strip()}"
                )
        assert hits == 2, f"expected two cache-hit returns, found {hits}"

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

    def test_a_skipped_job_is_never_requeued(self, tree):
        """'skipped' is a verdict on the file *type* -- no reader (PDF), or
        declined by the background task (CBR/RAR). A type cannot change while
        the path does not, so it is terminal. Without a branch of its own the
        request falls through to the 'processing' upsert and submits another
        doomed job, and the grid re-polls every 2s."""
        lines = _src(tree, "get_thumbnail").splitlines()

        verdict = next((i for i, l in enumerate(lines) if '"skipped"' in l), None)
        assert verdict is not None, "get_thumbnail no longer answers a skipped row"

        submit = next(
            (i for i, l in enumerate(lines) if "thumbnail_executor.submit" in l), None
        )
        assert submit is not None
        assert verdict < submit, "the skipped branch runs after the job is re-queued"
        assert any("return" in l for l in lines[verdict: submit])


class TestAnUnreadableCacheFileHeals:
    """The /cache ownership walk in entrypoint.sh is bounded to -maxdepth 2, so
    it no longer reaches the JPEGs themselves. It never needed to -- os.replace
    needs the shard *directory* -- but it did re-own them incidentally, and that
    hid one case: a root-fallback start plus a UMASK that clears other-read
    leaves a cached thumbnail we cannot open. Serving it blind was a 500 that
    never healed, because is_thumbnail_stale is False and so nothing downstream
    ever regenerated it.
    """

    def test_the_route_does_not_serve_the_cache_file_blind(self, tree):
        src = _src(tree, "get_thumbnail")
        assert "send_from_directory" not in src, (
            "get_thumbnail serves the cached file directly again; an unreadable "
            "JPEG raises a 500 that no later request can heal"
        )

    def test_the_helper_falls_through_on_an_unreadable_file(self, tree):
        src = _src(tree, "_serve_cached_thumbnail")
        assert "except OSError" in src
        assert "return None" in src, (
            "the helper must hand control back so the route regenerates over "
            "the unreadable file"
        )


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

    def test_the_cbr_skip_goes_through_the_cache(self, tree):
        """core.thumbnail_cache.set_job_status stamps file_mtime; the raw
        UPDATE this replaces did not, and /api/thumbnail's 'processing' insert
        leaves it NULL. scan_library_task reads NULL as "mtime unknown", so
        every CBR ever viewed was re-queued on every restart."""
        fn = _func(tree, "generate_thumbnail_task")
        assert "set_job_status" in _calls(fn)
        assert "UPDATE thumbnail_jobs" not in _src(tree, "generate_thumbnail_task")

    def test_no_module_keeps_its_own_copy_of_the_write(self, tree):
        """PIL saves straight onto the cache path are what fail with EACCES on a
        root-owned file. Every thumbnail write must go through
        core.thumbnail_cache.write_cached_thumbnail."""
        src = open(APP_PATH, encoding="utf-8").read()
        assert 'img.save(cache_path' not in src


class TestStartupScanEnumeratesWhatTheIndexHolds:
    """The scan and build_file_index must agree. Anything the scan generates
    that the index does not hold is a thumbnail nothing can ever request;
    anything the index holds that the scan skips is a spinner."""

    def test_prunes_hidden_directories(self, tree):
        src = _src(tree, "scan_library_task")
        assert "dirs[:]" in src, "hidden directories are descended into again"
        assert "is_hidden" in src

    def test_skips_hidden_files_before_testing_the_extension(self, tree):
        """An AppleDouble sidecar carries a comic extension, so an
        extension-first test still lets it through."""
        lines = _src(tree, "scan_library_task").splitlines()
        guard = next(
            (i for i, l in enumerate(lines) if "file.startswith" in l), None
        )
        assert guard is not None, "the '.'/'_' guard on files is gone"
        assert any("continue" in l for l in lines[guard: guard + 3])

        ext = next((i for i, l in enumerate(lines) if '".cbz"' in l), None)
        assert ext is not None
        assert guard < ext, "the extension test runs before the name guard"

    def test_is_hidden_cannot_depend_on_module_import_order(self, tree):
        """scan_library_task is defined above app.py's module-level
        `from helpers import is_hidden` AND is reached from a thread started at
        import time, so a module-level reference resolves only because that
        thread sleeps first. Either remedy is fine; relying on the sleep is
        not."""
        fn = _func(tree, "scan_library_task")

        local = any(
            isinstance(n, ast.ImportFrom)
            and any(a.name == "is_hidden" for a in n.names)
            for n in ast.walk(fn)
        )
        module_level_before = any(
            isinstance(n, ast.ImportFrom)
            and any(a.name == "is_hidden" for a in n.names)
            and n.lineno < fn.lineno
            for n in tree.body
        )

        assert local or module_level_before

    def test_prunes_rows_left_by_the_old_walk(self, tree):
        assert "prune_hidden_jobs" in _calls(_func(tree, "scan_library_task"))
