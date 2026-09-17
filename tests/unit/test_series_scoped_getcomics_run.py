"""Invariants of the series-scoped GetComics run.

``app.scheduled_getcomics_download(only_series_id=...)`` backs the "Check for
Missing Issues" button on a series page. Three properties make the scoped run
different from the unattended sweep, and all three are easy to undo by editing
the shared body:

* it narrows ``mapped_series`` to the requested id;
* it ignores the series' Monitor toggle — that toggle exists to keep the
  *unattended* sweep off a series, and a click is an explicit request;
* it leaves the schedule's last-run stamp alone, so a one-series check can't
  make the nightly sweep look as though it has already run.

app.py cannot be imported in tests (importing it starts the scheduler and
spawns monitor.py), so these are asserted against the parsed AST.
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


def _guards_on(func_node, name):
    """Every ``if`` whose test mentions ``name``."""
    out = []
    for stmt in ast.walk(func_node):
        if not isinstance(stmt, ast.If):
            continue
        if any(
            isinstance(n, ast.Name) and n.id == name
            for n in ast.walk(stmt.test)
        ):
            out.append(stmt)
    return out


def _calls_named(node, name):
    return [
        child for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == name
    ]


class TestScopedRunSignature:
    def test_accepts_only_series_id_and_op_id(self, func_node):
        args = [a.arg for a in func_node.args.args]
        assert "only_series_id" in args
        assert "op_id" in args

    def test_scope_parameter_is_not_named_series_id(self, func_node):
        # The per-series loop rebinds ``series_id`` to the series it is
        # processing; a parameter of that name would be shadowed and the
        # scoping silently lost.
        assert "series_id" not in [a.arg for a in func_node.args.args]

    def test_both_new_parameters_default_to_none(self, func_node):
        args = func_node.args.args
        defaults = dict(
            zip([a.arg for a in args[-len(func_node.args.defaults):]],
                func_node.args.defaults)
        )
        for name in ("only_series_id", "op_id"):
            node = defaults[name]
            assert isinstance(node, ast.Constant) and node.value is None


class TestScopedRunBehaviour:
    def test_mapped_series_is_narrowed_to_the_requested_id(self, func_node):
        """A scoped run filters the mapped-series list before the loop."""
        narrowing = [
            stmt for stmt in _guards_on(func_node, "only_series_id")
            if any(
                isinstance(n, ast.Name) and n.id == "mapped_series"
                for sub in stmt.body for n in ast.walk(sub)
            )
        ]
        assert narrowing, "no `if only_series_id is not None:` narrowing mapped_series"

    def test_monitor_toggle_is_bypassed_when_scoped(self, func_node):
        """The `monitored == 0` skip must also require an unscoped run."""
        monitored_guards = [
            stmt for stmt in ast.walk(func_node)
            if isinstance(stmt, ast.If)
            and any(
                isinstance(n, ast.Constant) and n.value == "monitored"
                for n in ast.walk(stmt.test)
            )
        ]
        assert monitored_guards, "the monitored skip disappeared from the sweep"
        for stmt in monitored_guards:
            names = {n.id for n in ast.walk(stmt.test) if isinstance(n, ast.Name)}
            assert "only_series_id" in names, (
                "the Monitor toggle would skip an explicitly requested series"
            )

    def test_last_run_stamp_is_only_written_by_an_unscoped_run(self, func_node):
        calls = _calls_named(func_node, "update_last_getcomics_run")
        assert len(calls) == 1, "expected exactly one last-run write"
        guarded = [
            stmt for stmt in _guards_on(func_node, "only_series_id")
            if _calls_named(stmt, "update_last_getcomics_run")
        ]
        assert guarded, (
            "update_last_getcomics_run() is not guarded by `only_series_id is None` — "
            "a one-series check would move the schedule's last-run stamp"
        )

    def test_progress_is_reported_through_the_operations_registry(self, func_node):
        """The op the route hands back must be updated and completed."""
        attr_calls = {
            child.func.attr for child in ast.walk(func_node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
        }
        assert "update_operation" in attr_calls
        assert "complete_operation" in attr_calls

    def test_the_operation_is_completed_on_the_failure_path_too(self, func_node):
        """An abandoned op would sit in the header indicator until it goes stale."""
        handlers = [
            h for h in ast.walk(func_node) if isinstance(h, ast.ExceptHandler)
        ]
        completing = [
            h for h in handlers
            if any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and c.func.attr == "complete_operation"
                for c in ast.walk(h)
            )
        ]
        assert completing, "no except handler completes the operation"


class TestReadingListSource:
    """The sweep's second source of wanted issues.

    Mapped series are not the only thing a user wants: an unmatched entry in an
    opted-in reading list is wanted too. It is collected in the same flat
    work-item list so the 390-line search body has one implementation.
    """

    def test_reading_list_items_are_only_collected_on_an_unscoped_run(self, func_node):
        """A scoped run is an explicit request for ONE series.

        Dragging a reading list into it would search for issues the user did
        not ask about, under an operation labelled with a series name.
        """
        calls = _calls_named(func_node, "build_reading_list_work_items")
        assert len(calls) == 1, "expected exactly one reading-list collection"

        guarded = [
            stmt for stmt in _guards_on(func_node, "only_series_id")
            if _calls_named(stmt, "build_reading_list_work_items")
        ]
        assert guarded, (
            "build_reading_list_work_items() is not guarded by only_series_id — "
            "a one-series check would search a reading list too"
        )

    def test_tracked_lists_are_rematched_before_they_are_collected(self, func_node):
        """Otherwise the sweep re-downloads the same issue every night.

        A reading-list entry has no mapped_path, so
        process_incoming_wanted_issues cannot file a finished download back
        onto it. Re-matching first picks up whatever the WATCH/TARGET pipeline
        has since filed, so it is no longer considered wanted.
        """
        for stmt in _guards_on(func_node, "only_series_id"):
            rematch = _calls_named(stmt, "rematch_tracked_lists")
            collect = _calls_named(stmt, "build_reading_list_work_items")
            if not collect:
                continue
            assert rematch, "reading-list items are collected without a re-match pass"
            body = ast.dump(stmt)
            assert body.index("rematch_tracked_lists") < body.index(
                "build_reading_list_work_items"
            ), "the re-match must run before the wanted set is decided"
            return
        pytest.fail("no guarded block collects reading-list work items")

    def test_a_queued_reading_list_entry_is_stamped(self, func_node):
        """The cooldown that bounds the re-download loop when a re-match never
        closes it — because the file never arrived, or landed out of reach."""
        assert _calls_named(func_node, "_mark_queued"), (
            "nothing stamps last_queued_at, so a reading-list issue would be "
            "re-queued on every run"
        )

    def test_the_search_body_runs_over_one_flat_work_item_list(self, func_node):
        """Two loops would mean two copies of the ~390-line search body."""
        loop_targets = {
            stmt.target.id for stmt in ast.walk(func_node)
            if isinstance(stmt, ast.For) and isinstance(stmt.target, ast.Name)
        }
        assert "item" in loop_targets, "the flat work-item loop disappeared"


# ===================================================================
# Duplicate-URL suppression and the per-issue year
# ===================================================================

def _work_item_loop(func_node):
    """The ``for item in work_items:`` loop -- the search/score/queue body."""
    for stmt in ast.walk(func_node):
        if (isinstance(stmt, ast.For)
                and isinstance(stmt.target, ast.Name) and stmt.target.id == "item"
                and isinstance(stmt.iter, ast.Name) and stmt.iter.id == "work_items"):
            return stmt
    pytest.fail("the `for item in work_items:` loop is gone")


class TestNoDuplicateDownloadsInOneRun:
    """One resolved download URL is queued at most once per run.

    20 wanted Star Wars issues each resolved to the same GetComics listing-page
    entry and queued it independently -- 20 downloads of one file, 1.19 GB, in
    106 seconds. `downloaded_ranges` could not catch it: it records only a
    labelled split part or a range-fallback tier, and that was an unlabelled
    ACCEPT part. This is the backstop.
    """

    def test_the_seen_url_map_exists(self, func_node):
        assigned = {
            t.id
            for stmt in ast.walk(func_node) if isinstance(stmt, (ast.Assign, ast.AnnAssign))
            for t in ([stmt.target] if isinstance(stmt, ast.AnnAssign) else stmt.targets)
            if isinstance(t, ast.Name)
        }
        assert "queued_download_urls" in assigned, (
            "nothing tracks which download URLs this run already queued, so the "
            "same file can be fetched once per wanted issue"
        )

    def test_it_is_consulted_before_queueing(self, func_node):
        """The guard must precede the put, in the same loop body."""
        put_loops = [
            loop for loop in ast.walk(func_node)
            if isinstance(loop, ast.For)
            and any(
                isinstance(c.func, ast.Attribute) and c.func.attr == "put"
                for c in ast.walk(loop) if isinstance(c, ast.Call)
            )
        ]
        assert put_loops, "download_queue.put is no longer inside a loop"

        innermost = min(put_loops, key=lambda n: len(list(ast.walk(n))))
        reads = [
            n for n in ast.walk(innermost)
            if isinstance(n, ast.Name) and n.id == "queued_download_urls"
        ]
        assert reads, (
            "the queue loop never consults queued_download_urls, so a duplicate "
            "URL is queued again"
        )
        assert any(isinstance(n, ast.Continue) for n in ast.walk(innermost)), (
            "nothing skips a duplicate -- the guard must `continue`"
        )

    def test_the_url_is_recorded_when_queued(self, func_node):
        """A guard that never records anything can never fire."""
        writes = [
            stmt for stmt in ast.walk(func_node)
            if isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Name)
                    and t.value.id == "queued_download_urls"
                    for t in stmt.targets)
        ]
        assert writes, "queued_download_urls is read but never written"


class TestIssueYearIsPerIssue:
    """``issue_year`` comes from the work item and is never recomputed.

    ``store_date`` is bound only in the phase-A collection loop. Reading it in
    the work-item loop returned the last issue of the last series scanned -- one
    frozen year for the whole run, applied to every search and every score --
    and raised NameError outright on a run with no qualifying mapped series
    (reading-lists-only, or a scoped run on an unmapped series), which the
    per-item handler swallowed as "skipping ... after error" for every item.
    """

    def test_store_date_is_not_read_in_the_work_item_loop(self, func_node):
        loop = _work_item_loop(func_node)
        leaked = [
            n for n in ast.walk(loop)
            if isinstance(n, ast.Name) and n.id == "store_date"
        ]
        assert not leaked, (
            "store_date is read inside the work-item loop, where it is a stale "
            "value left over from the collection loop; the per-issue year "
            "belongs in the work item (see item['issue_year'])"
        )

    def test_issue_year_comes_from_the_work_item(self, func_node):
        loop = _work_item_loop(func_node)
        from_item = [
            stmt for stmt in ast.walk(loop)
            if isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "issue_year" for t in stmt.targets)
            and isinstance(stmt.value, ast.Subscript)
            and isinstance(stmt.value.value, ast.Name)
            and stmt.value.value.id == "item"
        ]
        assert from_item, "issue_year is no longer taken from item['issue_year']"

    def test_issue_year_is_assigned_exactly_once(self, func_node):
        """A second assignment is what clobbered the correct per-issue value."""
        loop = _work_item_loop(func_node)
        assigns = [
            stmt for stmt in ast.walk(loop)
            if isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "issue_year" for t in stmt.targets)
        ]
        assert len(assigns) == 1, (
            f"issue_year is assigned {len(assigns)} times in the work-item loop; "
            f"the per-item value must not be recomputed or overwritten"
        )
