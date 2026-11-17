"""Replacing the contents of app.py's in-memory ``file_index`` list.

The list is read by request threads (``/api/file-index-status``,
``/api/rebuild-file-index``) while a scheduled or manual sync refreshes it.
It used to be ``.clear()``ed and then refilled from the database, so a reader
in between saw an empty index, and a failed database read
(``get_file_index_from_db`` answers ``[]`` on error) left it empty until the
next rebuild. The fresh list is now built aside and swapped in with one slice
assignment, which other threads see as a single step.

Lives outside app.py so it can be tested: app.py cannot be imported by the
suite.
"""

from core.app_logging import app_logger


def replace_index_contents(index: list, fresh, expected_entries: int) -> bool:
    """Swap *fresh* into *index* in one step; return True if it was swapped.

    *expected_entries* is how many entries the caller knows the database
    should hold (the sync's added + unchanged). An empty *fresh* when entries
    are expected is a failed read, not an empty library, so the previous
    contents are kept rather than replaced with nothing.
    """
    fresh = list(fresh or ())
    if not fresh and expected_entries > 0:
        app_logger.warning(
            "File index reload returned no entries after a sync that kept "
            f"{expected_entries}; keeping the previous in-memory index"
        )
        return False
    index[:] = fresh
    return True
