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
* A 'processing' row is trusted only while the job is in flight in this
  process. The executor queue is in memory, so a restart orphaned every queued
  row: the route answered loading.svg forever and the scan skipped the row as
  up to date (20,000 stuck rows on one install).
* The job-row writes must go through core.thumbnail_cache, which stamps
  file_mtime. A raw UPDATE/INSERT that omits it leaves NULL, which the scan
  reads as "mtime unknown" and re-queues on every restart.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")


# The startup scan's walk and decision moved out of app.scan_library_task into
# core/thumbnail_scan.py (so it can be tested with a real database, and so no
# write transaction is held across file I/O). The structural checks on that
# logic follow it there; app.scan_library_task keeps the submit wiring.
SCAN_PATH = os.path.join(PROJECT_ROOT, "core", "thumbnail_scan.py")


@pytest.fixture(scope="module")
def tree():
    with open(APP_PATH, encoding="utf-8") as fh:
        return ast.parse(fh.read())


@pytest.fixture(scope="module")
def scan_tree():
    with open(SCAN_PATH, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name} not found")


def _src(tree, name, path=APP_PATH):
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    node = _func(tree, name)
    return "\n".join(lines[node.lineno - 1: node.end_lineno])


def _scan_src(scan_tree, name):
    return _src(scan_tree, name, SCAN_PATH)


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
            (i for i, l in enumerate(lines) if "submit_thumbnail_job" in l), None
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


class TestCoverCacheHeaders:
    """A cache hit is ``private, max-age=300`` (core.thumbnail_cache); nothing
    else on this route may be cacheable. A redirect to loading.svg that a
    browser stored would freeze a card on the spinner."""

    def test_the_cache_hit_gets_the_private_short_max_age(self, tree):
        calls = _calls(_func(tree, "_serve_cached_thumbnail"))
        assert "apply_cache_hit_headers" in calls
        assert "send_from_directory" in calls

    def test_the_headers_are_applied_inside_the_oserror_guard(self, tree):
        node = _func(tree, "_serve_cached_thumbnail")
        trys = [n for n in ast.walk(node) if isinstance(n, ast.Try)]
        assert trys, "_serve_cached_thumbnail lost its try/except OSError"
        body_calls = [
            n.func.id for t in trys for stmt in t.body for n in ast.walk(stmt)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]
        assert "apply_cache_hit_headers" in body_calls

    def test_redirects_carry_no_max_age(self, tree):
        node = _func(tree, "get_thumbnail")
        assert "is_thumbnail_stale" in _calls(node)
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) \
                    and call.func.id == "redirect":
                assert not any(k.arg == "max_age" for k in call.keywords)
        assert "max_age" not in _src(tree, "get_thumbnail")


class TestStartupScan:

    def test_retries_errored_jobs(self, scan_tree):
        """The old code documented its refusal to retry ('assume errors are
        permanent until file changes'), which turned one unwritable-cache
        failure into a permanently broken thumbnail."""
        src = _scan_src(scan_tree, "needs_thumbnail")
        assert "assume errors are permanent" not in src

        lines = src.splitlines()
        idx = next(
            (i for i, l in enumerate(lines) if 'status == "error"' in l),
            None,
        )
        assert idx is not None, "the errored-job branch is gone from the scan"
        branch = "\n".join(lines[idx: idx + 14])
        assert "return True" in branch, (
            "errored thumbnail jobs are still never retried"
        )

    def test_uses_the_shared_cache_path_helper(self, tree):
        assert "thumbnail_cache_path" in _calls(_func(tree, "scan_library_task"))

    def test_app_delegates_to_the_core_scan(self, tree):
        """The walk lives in core.thumbnail_scan; a second copy in app.py is
        how the two would drift, and app.py cannot be tested."""
        fn = _func(tree, "scan_library_task")
        assert "scan_library_thumbnails" in _calls(fn)
        src = _src(tree, "scan_library_task")
        assert "os.walk" not in src
        assert "conn.execute" not in src


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

    def test_prunes_hidden_directories(self, scan_tree):
        src = _scan_src(scan_tree, "iter_candidate_dirs")
        assert "dirs[:]" in src, "hidden directories are descended into again"
        assert "is_hidden" in src or "is_library_hidden" in src

    def test_skips_hidden_files_before_testing_the_extension(self, scan_tree):
        """An AppleDouble sidecar carries a comic extension, so an
        extension-first test still lets it through."""
        lines = _scan_src(scan_tree, "iter_candidate_dirs").splitlines()
        guard = next(
            (i for i, l in enumerate(lines) if "file.startswith" in l), None
        )
        assert guard is not None, "the '.'/'_' guard on files is gone"
        assert any("continue" in l for l in lines[guard: guard + 3])

        ext = next(
            (i for i, l in enumerate(lines) if "SCAN_EXTENSIONS" in l), None
        )
        assert ext is not None
        assert guard < ext, "the extension test runs before the name guard"

    def test_the_scan_import_cannot_depend_on_module_import_order(self, tree):
        """scan_library_task is reached from a thread started at app.py's
        import time, so a module-level reference defined further down resolves
        only because that thread sleeps first. It imports the core scan
        locally; core.thumbnail_scan itself imports is_hidden at its top."""
        fn = _func(tree, "scan_library_task")
        assert any(
            isinstance(n, ast.ImportFrom)
            and n.module == "core.thumbnail_scan"
            and any(a.name == "scan_library_thumbnails" for a in n.names)
            for n in ast.walk(fn)
        )

    def test_prunes_rows_left_by_the_old_walk(self, scan_tree):
        assert "prune_hidden_jobs" in _calls(
            _func(scan_tree, "scan_library_thumbnails")
        )


class TestOrphanedProcessingRows:
    """A 'processing' row outlives the in-memory executor queue that was meant
    to settle it. Both readers must ask core.thumbnail_cache.is_in_flight."""

    def test_route_trusts_processing_only_while_in_flight(self, tree):
        src = _src(tree, "get_thumbnail")
        guard = [l for l in src.splitlines() if '"processing"' in l and "==" in l]
        assert guard, "the in-flight guard is gone from get_thumbnail"
        assert all("is_in_flight" in l for l in guard), (
            "a 'processing' row alone answers loading.svg again; an orphaned "
            "row then spins forever"
        )

    def test_scan_requeues_orphaned_processing_rows(self, scan_tree):
        """The decision takes in_flight as an argument (checked before any
        status branch); the scan feeds it core.thumbnail_cache.is_in_flight.
        The behaviour is in tests/unit/test_thumbnail_scan.py."""
        lines = _scan_src(scan_tree, "needs_thumbnail").splitlines()
        guard = next((i for i, l in enumerate(lines) if "if in_flight" in l), None)
        idx = next(
            (i for i, l in enumerate(lines) if 'status == "processing"' in l), None
        )
        assert guard is not None and idx is not None, (
            "the scan no longer reconsiders 'processing' rows"
        )
        assert guard < idx
        assert "return True" in "\n".join(lines[idx: idx + 10])

        fn = _func(scan_tree, "scan_library_thumbnails")
        defaults = {
            a.arg: d for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults)
        }
        assert isinstance(defaults["in_flight"], ast.Name)
        assert defaults["in_flight"].id == "is_in_flight"

    @pytest.mark.parametrize("name", ["get_thumbnail", "scan_library_task"])
    def test_jobs_are_submitted_through_the_registry(self, tree, name):
        """A bare executor.submit is invisible to is_in_flight, so its row
        would look orphaned and be queued a second time."""
        src = _src(tree, name)
        assert "thumbnail_executor.submit" not in src
        assert "submit_thumbnail_job" in _calls(_func(tree, name))
