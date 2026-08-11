"""Cooperative-cancellation helpers used by every api.py download loop.

Regression context: clicking Cancel on the Status page only ever set a flag on
``api.download_progress``. GetComics honoured it between chunks but let the
failure it caused overwrite 'cancelled' with 'error'; PixelDrain never looked at
it at all, so a cancelled download ran to completion, landed in WATCH and got
imported anyway.

api.py itself is not importable here (worker threads, cloudscraper, download
dirs — see tests/routes/conftest.py), which is exactly why this logic lives in
core/download_utils.py.
"""
import os

import pytest

from core.download_utils import (
    cleanup_partial_files,
    is_cancel_requested,
    mark_cancelled,
    set_error_status,
)


@pytest.fixture
def progress():
    """A minimal stand-in for api.download_progress with one live download."""
    return {"dl-1": {"status": "in_progress", "progress": 42}}


class TestIsCancelRequested:

    def test_false_while_running(self, progress):
        assert is_cancel_requested(progress, "dl-1") is False

    def test_true_once_flagged(self, progress):
        progress["dl-1"]["cancelled"] = True
        assert is_cancel_requested(progress, "dl-1") is True

    def test_status_label_alone_does_not_count(self, progress):
        # The worker keys off the flag, not the label: a row shown as
        # 'cancelled' without the flag must not abort a running transfer.
        progress["dl-1"]["status"] = "cancelled"
        assert is_cancel_requested(progress, "dl-1") is False

    def test_missing_entry_reads_as_not_cancelled(self, progress):
        # Cleared/dismissed mid-download — must not raise inside a chunk loop.
        assert is_cancel_requested(progress, "gone") is False
        assert is_cancel_requested({}, "dl-1") is False

    def test_non_dict_progress_is_safe(self):
        assert is_cancel_requested(None, "dl-1") is False

    def test_non_dict_entry_is_safe(self):
        assert is_cancel_requested({"dl-1": 0}, "dl-1") is False


class TestCleanupPartialFiles:

    def test_removes_existing_partials(self, tmp_path):
        part = tmp_path / "Batman 001.cbz.part"
        part.write_bytes(b"half a comic")

        removed = cleanup_partial_files([str(part)])

        assert removed == [str(part)]
        assert not part.exists()

    def test_skips_missing_and_empty_paths(self, tmp_path):
        assert cleanup_partial_files([str(tmp_path / "nope.part"), None, ""]) == []

    def test_none_is_accepted(self):
        assert cleanup_partial_files(None) == []

    def test_undeletable_file_is_logged_not_raised(self, tmp_path, monkeypatch):
        part = tmp_path / "locked.crdownload"
        part.write_bytes(b"x")

        def boom(_path):
            raise PermissionError("file is open")

        monkeypatch.setattr(os, "remove", boom)

        class _Log:
            def __init__(self):
                self.warnings = []

            def warning(self, msg):
                self.warnings.append(msg)

        log = _Log()
        # A failed cleanup must never take down the worker thread.
        assert cleanup_partial_files([str(part)], log) == []
        assert len(log.warnings) == 1


class TestMarkCancelled:

    def test_sets_status_and_deletes_partial(self, progress, tmp_path):
        part = tmp_path / "Batman 001.cbz.part"
        part.write_bytes(b"partial")
        progress["dl-1"]["cancelled"] = True

        mark_cancelled(progress, "dl-1", (str(part),))

        assert progress["dl-1"]["status"] == "cancelled"
        assert not part.exists()

    def test_cleans_every_retry_temp_file(self, progress, tmp_path):
        # download_getcomics names its temp file per attempt.
        parts = []
        for i in range(3):
            p = tmp_path / f"Batman 001.cbz.{i}.crdownload"
            p.write_bytes(b"x")
            parts.append(str(p))

        mark_cancelled(progress, "dl-1", parts)

        assert not any(os.path.exists(p) for p in parts)

    def test_cleared_entry_does_not_resurrect_the_row(self, tmp_path):
        part = tmp_path / "x.part"
        part.write_bytes(b"x")
        progress = {}

        mark_cancelled(progress, "dl-1", (str(part),))

        assert progress == {}
        assert not part.exists()


class TestSetErrorStatus:

    def test_records_a_genuine_failure(self, progress):
        set_error_status(progress, "dl-1", "HTTP 500")

        assert progress["dl-1"]["status"] == "error"
        assert progress["dl-1"]["error"] == "HTTP 500"

    def test_cancel_is_not_overwritten_by_the_failure_it_caused(self, progress):
        # The regression: aborting the transfer raises, retries run out, and the
        # download the user just cancelled reappears as Failed with a Retry.
        progress["dl-1"]["cancelled"] = True

        set_error_status(progress, "dl-1", "Connection aborted")

        assert progress["dl-1"]["status"] == "cancelled"
        assert "error" not in progress["dl-1"]

    def test_error_is_optional(self, progress):
        set_error_status(progress, "dl-1")

        assert progress["dl-1"]["status"] == "error"
        assert "error" not in progress["dl-1"]

    def test_exception_object_is_stringified(self, progress):
        set_error_status(progress, "dl-1", RuntimeError("boom"))

        assert progress["dl-1"]["error"] == "boom"

    def test_missing_entry_is_a_no_op(self, progress):
        set_error_status(progress, "gone", "boom")

        assert "gone" not in progress


class TestChunkLoopContract:
    """The shape every streaming download in api.py now follows."""

    def _stream(self, progress, download_id, chunks, tmp_path):
        """Mimic download_pixeldrain/download_getcomics' write loop."""
        part = tmp_path / "out.cbz.part"
        written = 0
        with open(part, "wb") as f:
            for chunk in chunks:
                if is_cancel_requested(progress, download_id):
                    f.close()
                    mark_cancelled(progress, download_id, (str(part),))
                    return None, written
                f.write(chunk)
                written += len(chunk)
                progress[download_id]["bytes_downloaded"] = written
        return str(part), written

    def test_cancel_mid_stream_stops_and_leaves_nothing_behind(self, progress, tmp_path):
        def chunks():
            yield b"a" * 10
            # Cancel arrives from the request thread between chunks.
            progress["dl-1"]["cancelled"] = True
            yield b"b" * 10
            yield b"c" * 10

        result, written = self._stream(progress, "dl-1", chunks(), tmp_path)

        assert result is None
        assert written == 10, "must stop at the next chunk boundary, not finish"
        assert progress["dl-1"]["status"] == "cancelled"
        assert not (tmp_path / "out.cbz.part").exists()

    def test_uncancelled_stream_completes(self, progress, tmp_path):
        result, written = self._stream(
            progress, "dl-1", [b"a" * 10, b"b" * 10], tmp_path
        )

        assert result is not None
        assert written == 20
        assert progress["dl-1"]["status"] == "in_progress"
