"""Single-file /stream actions report through the operations registry.

Crop Cover and friends used to show only in the page's own progress card (a
different card on each page) and were followed by nothing once the page went
away. app.py cannot be imported in tests, so its half is asserted structurally.
"""
import ast
import io
import os
import subprocess
import sys

import pytest

import core.app_state as app_state
from core import stream_ops

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def clean_registry():
    with app_state._operations_lock:
        app_state._operations.clear()
    yield
    with app_state._operations_lock:
        app_state._operations.clear()


class TestRegister:

    @pytest.mark.parametrize("script_type,op_type,label", [
        ("crop", "crop", "Crop Cover: a.cbz"),
        ("remove", "remove", "Remove 1st Image: a.cbz"),
        ("single_file", "convert", "Rebuild: a.cbz"),
        ("enhance_single", "enhance", "Enhance: a.cbz"),
        ("add", "add", "Add Blank to End: a.cbz"),
        ("delete", "delete", "Delete: a.cbz"),
    ])
    def test_every_single_file_action_is_registered(self, script_type, op_type, label):
        op_id = stream_ops.register_single_file_op(script_type, "/data/x/a.cbz", user_id=7)
        op = app_state.get_operation(op_id)
        assert op["op_type"] == op_type
        assert op["label"] == label
        assert op["status"] == "running"
        assert op["user_id"] == 7

    def test_progress_lines_update_the_op(self):
        op_id = stream_ops.register_single_file_op("single_file", "/data/a.cbz", user_id=1)
        stream_ops.track_line(op_id, "Compression progress: 40.0% (4/10 files)\n")
        op = app_state.get_operation(op_id)
        assert (op["current"], op["total"]) == (40, 100)
        stream_ops.track_line(op_id, "Step 2/3: Extracting.\n")
        assert app_state.get_operation(op_id)["detail"] == "Extracting"

    def test_finish_reports_a_non_zero_exit_as_an_error(self):
        ok = stream_ops.register_single_file_op("crop", "/data/a.cbz", user_id=1)
        bad = stream_ops.register_single_file_op("crop", "/data/b.cbz", user_id=1)
        stream_ops.finish(ok, 0)
        stream_ops.finish(bad, 1)
        assert app_state.get_operation(ok)["status"] == "completed"
        assert app_state.get_operation(bad)["status"] == "error"


class TestFollowInBackground:

    def _run(self, code):
        return subprocess.Popen(
            [sys.executable, "-u", "-c", code],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_drains_and_completes_after_the_page_left(self):
        # More output than a pipe buffer holds: undrained, this never exits.
        proc = self._run("import sys\nfor i in range(20000): print('x' * 80)\n")
        op_id = stream_ops.register_single_file_op("crop", "/data/a.cbz", user_id=1)
        thread = stream_ops.follow_in_background(proc, op_id)
        assert thread is not None
        thread.join(timeout=60)
        assert not thread.is_alive()
        assert proc.returncode == 0
        assert app_state.get_operation(op_id)["status"] == "completed"

    def test_a_failed_run_is_an_error(self):
        proc = self._run("import sys; sys.exit(3)")
        op_id = stream_ops.register_single_file_op("crop", "/data/a.cbz", user_id=1)
        thread = stream_ops.follow_in_background(proc, op_id)
        if thread is not None:
            thread.join(timeout=60)
        assert app_state.get_operation(op_id)["status"] == "error"

    def test_an_already_finished_process_is_completed_inline(self):
        proc = self._run("pass")
        proc.wait()
        op_id = stream_ops.register_single_file_op("crop", "/data/a.cbz", user_id=1)
        assert stream_ops.follow_in_background(proc, op_id) is None
        assert app_state.get_operation(op_id)["status"] == "completed"


class TestWiring:

    @pytest.fixture(scope="class")
    def stream_logs(self):
        with io.open(os.path.join(ROOT, "app.py"), encoding="utf-8") as fh:
            src = fh.read()
        for node in ast.parse(src).body:
            if isinstance(node, ast.FunctionDef) and node.name == "stream_logs":
                return ast.get_source_segment(src, node)
        pytest.fail("stream_logs not found")

    def test_route_registers_every_single_file_action(self, stream_logs):
        assert "stream_ops.register_single_file_op(" in stream_logs
        assert 'if script_type == "single_file":' not in stream_logs

    def test_op_owner_is_resolved_inside_the_request(self, stream_logs):
        assert stream_logs.index("op_user_id = current_user_id()") < \
            stream_logs.index("def generate_logs():")

    def test_a_disconnect_always_hands_the_process_on(self, stream_logs):
        assert "stream_ops.follow_in_background(process, op_id)" in stream_logs

    def test_page_script_uses_the_header_indicator_not_a_progress_card(self):
        path = os.path.join(ROOT, "static", "js", "clu-streaming.js")
        with io.open(path, encoding="utf-8") as fh:
            src = fh.read()
        start = src.index("CLU.executeStreamingOp = function")
        body = src[start:src.index("CLU.executeDirectoryOp = function")]
        for call in ("showProgressIndicator", "updateProgress", "resetProgress",
                     "progress-text"):
            assert call not in body
        assert "updateOperationsIndicator" in src
        # A second action must not close the first one's stream.
        assert "_currentEventSource" not in src


class TestBusyOverlay:
    """The file being rewritten carries a spinner where it is listed: the header
    indicator is out of sight once the page is scrolled."""

    def test_op_names_the_file_it_rewrites(self):
        op_id = stream_ops.register_single_file_op("crop", "/data/x/a.cbz", user_id=1)
        assert app_state.get_operation(op_id)["path"] == "/data/x/a.cbz"

    def test_ops_without_a_file_carry_no_path(self):
        op_id = app_state.register_operation("metadata", "batch")
        assert app_state.get_operation(op_id)["path"] is None

    def test_poller_publishes_every_poll_including_an_empty_one(self):
        with io.open(os.path.join(ROOT, "templates", "base.html"), encoding="utf-8") as fh:
            src = fh.read()
        publish = src.index("new CustomEvent('clu:operations'")
        assert publish < src.index("if (ops.length === 0) {")

    def test_page_script_marks_and_unmarks_the_file(self):
        path = os.path.join(ROOT, "static", "js", "clu-streaming.js")
        with io.open(path, encoding="utf-8") as fh:
            src = fh.read()
        assert "addEventListener('clu:operations'" in src
        for attr in ("data-path", "data-fullpath", "data-issue-path"):
            assert "'" + attr + "'" in src
        body = src[src.index("CLU.executeStreamingOp = function"):
                   src.index("CLU.executeDirectoryOp = function")]
        assert "_setLocalBusy(filePath, label)" in body
        assert body.count("_setLocalBusy(filePath, null)") == 2  # done, and failed
        # File names go in as text, never as HTML.
        marker = src[src.index("function _applyBusy"):src.index("function _setLocalBusy")]
        assert "innerHTML" not in marker

    def test_overlay_is_styled_globally(self):
        with io.open(os.path.join(ROOT, "static", "css", "clu-page.css"), encoding="utf-8") as fh:
            assert ".clu-file-busy-overlay" in fh.read()
