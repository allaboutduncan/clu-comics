"""The file watcher must not log (or queue) one INFO line per raw event."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core import file_watcher
from core.file_watcher import DebouncedFileHandler


def ev(path, is_dir=False, dest=None):
    return SimpleNamespace(src_path=path, dest_path=dest, is_directory=is_dir, event_type="x")


@pytest.fixture
def handler():
    return DebouncedFileHandler(debounce_seconds=0)


@pytest.fixture
def queued(handler):
    calls = []
    handler._add_event = lambda p: calls.append(p)
    return calls


@pytest.mark.parametrize("path,is_dir", [
    ("/data/Pub/Series/series.json", False),
    ("/data/komga_page_removal_1.tmp", False),
    ("/data/Pub/.hidden.cbz", False),
    ("/data/Pub/Series", True),
])
def test_noise_events_are_silent_and_unqueued(handler, queued, path, is_dir):
    with patch.object(file_watcher, "app_logger") as log:
        handler.on_modified(ev(path, is_dir))
        handler.on_created(ev(path, is_dir))
        handler.on_moved(ev("/data/a", is_dir, dest=path))
    assert queued == []
    log.info.assert_not_called()


def test_comic_event_is_queued_without_info(handler, queued):
    with patch.object(file_watcher, "app_logger") as log:
        handler.on_modified(ev("/data/Pub/S/S 001.cbz"))
    assert queued == ["/data/Pub/S/S 001.cbz"]
    log.info.assert_not_called()


def test_batch_emits_one_info_summary(handler, tmp_path):
    files = []
    for i in range(3):
        f = tmp_path / f"S {i:03d}.cbz"
        f.write_bytes(b"x")
        files.append(str(f))
        handler.pending_events[str(f)] = 0
    with patch.object(file_watcher, "app_logger") as log, \
         patch.object(file_watcher, "add_file_index_entry"), \
         patch.object(file_watcher, "queue_file_for_scan"), \
         patch.object(file_watcher, "_series_id_for_path", return_value=None), \
         patch.object(file_watcher, "invalidate_collection_status_for_path"):
        handler._process_pending_events()
    assert log.info.call_count == 1
    assert "3 file(s)" in log.info.call_args[0][0]
