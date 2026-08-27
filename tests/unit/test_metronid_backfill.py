"""ci_metronid: the indexed copy of <MetronId> the credit backfill joins on.

The id lives only inside the archive, so the metadata scanner -- which already
opens every CBZ -- is what puts it in the index. Two things have to hold or the
column is worse than useless: a scan must record 0 rather than NULL when the
file has no id (NULL means "never looked", and a file that keeps coming back
NULL is re-queued forever), and the backfill of files indexed before the column
existed must ride the scanner's idle capacity rather than invalidating the whole
library at migration time.
"""

import ast
import os
from unittest.mock import patch

import pytest

SCANNER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "core", "metadata_scanner.py",
)


class TestScanRecordsTheId:

    def _scan(self, tmp_path, comicinfo):
        from core.metadata_scanner import ScanTask, process_metadata_scan

        cbz = tmp_path / "x.cbz"
        cbz.write_bytes(b"x")
        task = ScanTask(priority=1, file_path=str(cbz), file_id=7, modified_at=1.0)

        with patch("core.metadata_scanner.read_comicinfo_from_zip",
                   return_value=comicinfo), \
             patch("core.metadata_scanner.update_file_metadata") as write:
            process_metadata_scan(task)

        return write.call_args[0][1] if write.call_args else None

    def test_a_metron_tagged_file_records_its_id(self, tmp_path):
        db_metadata = self._scan(tmp_path, {"Series": "Absolute Catwoman",
                                            "MetronId": "172615"})
        assert db_metadata["ci_metronid"] == "172615"

    def test_a_file_without_one_records_nothing_to_parse(self, tmp_path):
        """update_file_metadata turns this into the 0 marker; what matters here
        is that the scanner doesn't invent a value."""
        db_metadata = self._scan(tmp_path, {"Series": "Batman"})
        assert db_metadata.get("ci_metronid") is None


class TestQueueMetronidBackfill:

    def test_queues_what_the_query_returns(self):
        from core.metadata_scanner import metadata_queue, queue_metronid_backfill

        rows = [{"id": 1, "path": "/data/a.cbz", "modified_at": 1.0},
                {"id": 2, "path": "/data/b.cbz", "modified_at": 2.0}]

        while not metadata_queue.empty():
            metadata_queue.get_nowait()

        with patch("core.metadata_scanner.get_files_needing_metronid_backfill",
                   return_value=rows):
            assert queue_metronid_backfill() == 2

        assert metadata_queue.qsize() == 2
        while not metadata_queue.empty():
            metadata_queue.get_nowait()

    def test_nothing_to_do_is_not_an_error(self):
        from core.metadata_scanner import queue_metronid_backfill

        with patch("core.metadata_scanner.get_files_needing_metronid_backfill",
                   return_value=[]):
            assert queue_metronid_backfill() == 0

    def test_a_database_failure_is_swallowed(self):
        """Housekeeping must never take the scanner thread down."""
        from core.metadata_scanner import queue_metronid_backfill

        with patch("core.metadata_scanner.get_files_needing_metronid_backfill",
                   side_effect=RuntimeError("db gone")):
            assert queue_metronid_backfill() == 0


class TestQueueMonitorWiring:
    """The backfill has to be the *fallback*, not an extra job: real scanning
    work -- a new file, a modified one -- always goes first. Asserted against
    the AST because queue_monitor is a 30-second wait loop."""

    @pytest.fixture(scope="class")
    def monitor(self):
        with open(SCANNER_PATH, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "queue_monitor":
                return node
        pytest.fail("queue_monitor not found in core/metadata_scanner.py")

    def _calls(self, node, name):
        return [
            child for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == name
        ]

    def test_backfill_runs_only_when_nothing_else_is_pending(self, monitor):
        branches = [
            stmt for stmt in ast.walk(monitor)
            if isinstance(stmt, ast.If)
            and any(self._calls(b, "queue_metronid_backfill") for b in stmt.orelse)
        ]
        assert len(branches) == 1, (
            "queue_metronid_backfill belongs in exactly one else branch"
        )
        # ...and the branch it falls back from is the one that does real work.
        assert any(
            self._calls(b, "queue_pending_files") for b in branches[0].body
        )

    def test_backfill_is_not_queued_alongside_real_work(self, monitor):
        for stmt in ast.walk(monitor):
            if isinstance(stmt, ast.If) and any(
                self._calls(b, "queue_metronid_backfill") for b in stmt.orelse
            ):
                for node in stmt.body:
                    assert not self._calls(node, "queue_metronid_backfill")
