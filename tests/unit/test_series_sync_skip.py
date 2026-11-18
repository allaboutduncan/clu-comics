"""The skip decision in the scheduled Metron series sync (#779).

A series that was added but never synced has ``series.issue_count`` NULL. The
sync compared that with ``>=`` for every Ended/Completed/Cancelled series, so
each one raised ``TypeError``, was counted as failed and stayed NULL -- failing
again on every run.

The decision is in ``core.series_sync``; app.py cannot be imported in tests, so
its use of that function is asserted against the parsed AST.
"""

import ast
import os

import pytest

from core.series_sync import should_skip_issue_fetch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")


class TestNeverSyncedSeries:
    @pytest.mark.parametrize("status", ["Ended", "Completed", "Cancelled", "Ongoing", "Hiatus"])
    def test_null_local_count_fetches_instead_of_raising(self, status):
        assert should_skip_issue_fetch(None, status, 12) is False

    @pytest.mark.parametrize("status", ["Completed", "Ongoing"])
    def test_null_local_count_fetches_even_when_the_api_reports_zero(self, status):
        """NULL read as 0 would match an API count of 0, skip, and leave the
        series NULL for good. The fetch is what stores a real count."""
        assert should_skip_issue_fetch(None, status, 0) is False

    @pytest.mark.parametrize("status", ["Completed", "Ongoing"])
    def test_unknown_api_count_fetches(self, status):
        assert should_skip_issue_fetch(6, status, None) is False
        assert should_skip_issue_fetch(None, status, None) is False


class TestSyncedSeries:
    def test_finished_series_with_every_issue_is_skipped(self):
        assert should_skip_issue_fetch(12, "Completed", 12) is True
        assert should_skip_issue_fetch(13, "Cancelled", 12) is True
        assert should_skip_issue_fetch(12, "Ended", 12) is True

    def test_finished_series_missing_issues_is_fetched(self):
        assert should_skip_issue_fetch(10, "Completed", 12) is False

    def test_ongoing_series_is_skipped_only_on_an_unchanged_count(self):
        assert should_skip_issue_fetch(12, "Ongoing", 12) is True
        assert should_skip_issue_fetch(12, "Ongoing", 13) is False
        assert should_skip_issue_fetch(13, "Ongoing", 12) is False

    def test_a_synced_series_with_no_issues_is_skipped(self):
        """A stored 0 is a real count, unlike NULL."""
        assert should_skip_issue_fetch(0, "Ongoing", 0) is True
        assert should_skip_issue_fetch(0, "Completed", 0) is True

    def test_counts_stored_as_text_still_compare(self):
        assert should_skip_issue_fetch("12", "Completed", 12) is True
        assert should_skip_issue_fetch("abc", "Completed", 12) is False


class TestAppUsesTheSharedDecision:
    @pytest.fixture(scope="class")
    def func_node(self):
        with open(APP_PATH, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "scheduled_series_sync":
                return node
        pytest.fail("scheduled_series_sync not found in app.py")

    def test_calls_should_skip_issue_fetch(self, func_node):
        calls = [
            n
            for n in ast.walk(func_node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "should_skip_issue_fetch"
        ]
        assert calls, "scheduled_series_sync must decide through core.series_sync"

    def test_does_not_compare_issue_counts_itself(self, func_node):
        """An inline ``local >= api`` is the comparison that raised on NULL."""
        for node in ast.walk(func_node):
            if not isinstance(node, ast.Compare):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            assert not {"local_issue_count", "api_issue_count"} & names, (
                "issue counts must be compared in core.series_sync, "
                "where a NULL count is handled"
            )
