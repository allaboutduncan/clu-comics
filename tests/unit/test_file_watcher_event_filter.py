"""The library watcher must subscribe only to the events it handles (#732).

With no ``event_filter`` watchdog asks inotify for every event type, including
IN_OPEN and IN_CLOSE_NOWRITE. Reads then fill the kernel queue, it overflows,
and watchdog >= 7.1 recovers by re-walking the tree -- which opens every
directory and overflows it again, forever.
"""
import queue
import sys
from unittest.mock import patch

import pytest
import watchdog.events as wd_events

from core import file_watcher
from core.file_watcher import DebouncedFileHandler, FileWatcher, WATCHED_EVENTS


# Handler method -> the event classes watchdog dispatches to it.
_HANDLED_BY = {
    "on_created": ("FileCreatedEvent", "DirCreatedEvent"),
    "on_modified": ("FileModifiedEvent", "DirModifiedEvent"),
    "on_moved": ("FileMovedEvent", "DirMovedEvent"),
    "on_deleted": ("FileDeletedEvent", "DirDeletedEvent"),
    "on_closed": ("FileClosedEvent",),
    "on_closed_no_write": ("FileClosedNoWriteEvent",),
    "on_opened": ("FileOpenedEvent",),
    "on_attrib": ("FileAttribEvent", "DirAttribEvent"),
}

# Events a read produces. Subscribing to either one brings the loop back.
_READ_EVENTS = ("FileOpenedEvent", "FileClosedNoWriteEvent")


def test_start_schedules_a_recursive_watch_with_the_filter(tmp_path):
    with patch.object(file_watcher, "Observer") as observer_cls:
        watcher = FileWatcher(str(tmp_path))
        assert watcher.start() is True

    schedule = observer_cls.return_value.schedule
    schedule.assert_called_once()
    assert schedule.call_args.kwargs["recursive"] is True
    assert schedule.call_args.kwargs["event_filter"] is WATCHED_EVENTS
    observer_cls.return_value.start.assert_called_once()


def test_filter_never_subscribes_to_read_events():
    names = {cls.__name__ for cls in WATCHED_EVENTS}
    assert not names & set(_READ_EVENTS)


def test_handler_does_not_implement_read_event_callbacks():
    """Adding on_opened would be the reason someone widens the filter."""
    overridden = set(vars(DebouncedFileHandler))
    assert not overridden & {"on_opened", "on_closed_no_write"}


def test_filter_covers_every_file_event_the_handler_implements():
    """A handler method whose events are filtered out silently never runs."""
    names = {cls.__name__ for cls in WATCHED_EVENTS}
    overridden = set(vars(DebouncedFileHandler)) & set(_HANDLED_BY)
    assert overridden, "the handler implements no event callbacks at all"
    for method in overridden:
        file_event = _HANDLED_BY[method][0]
        assert file_event in names, f"{method} needs {file_event} in WATCHED_EVENTS"


def test_filter_covers_the_directory_events_the_handler_uses():
    # on_moved carries a whole subtree; on_deleted forgets one.
    assert wd_events.DirMovedEvent in WATCHED_EVENTS
    assert wd_events.DirDeletedEvent in WATCHED_EVENTS


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify only")
def test_inotify_mask_excludes_open_and_close_nowrite(tmp_path):
    from watchdog.observers.api import ObservedWatch
    from watchdog.observers.inotify import InotifyEmitter
    from watchdog.observers.inotify_c import InotifyConstants

    watch = ObservedWatch(str(tmp_path), recursive=True, event_filter=WATCHED_EVENTS)
    emitter = InotifyEmitter(queue.Queue(), watch, event_filter=WATCHED_EVENTS)
    mask = emitter.get_event_mask_from_filter()

    assert not mask & InotifyConstants.IN_OPEN
    assert not mask & InotifyConstants.IN_CLOSE_NOWRITE
    for needed in (
        InotifyConstants.IN_CREATE,
        InotifyConstants.IN_MODIFY,
        InotifyConstants.IN_DELETE,
        InotifyConstants.IN_MOVED_FROM,
        InotifyConstants.IN_MOVED_TO,
    ):
        assert mask & needed
