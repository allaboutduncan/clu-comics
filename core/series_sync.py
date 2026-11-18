"""Decisions for the scheduled Metron series sync.

``app.scheduled_series_sync`` makes one cheap series request per mapped series
and then decides whether the expensive issue fetch is needed. That decision
lives here, not in app.py, because app.py cannot be imported in tests.
"""

FINISHED_STATUSES = ("Ended", "Completed", "Cancelled")


def _as_count(value):
    """An issue count as an int, or None when it is not known.

    ``series.issue_count`` is NULL until a series has been synced once, and a
    provider record can leave the field out. Neither is zero.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def should_skip_issue_fetch(local_issue_count, api_status, api_issue_count):
    """True when the stored issues are already current and the fetch can be skipped.

    A skip needs both counts. A series that has never been synced has a NULL
    local count: comparing that raised ``TypeError`` for every finished series
    (#779), which counted it as failed, left the count NULL and failed it again
    on every run. Reading NULL as 0 instead would skip a never-synced series
    whose provider count is 0 and leave it NULL for good, so an unknown count
    on either side always fetches. The fetch stores a real count, which makes
    the next run an ordinary comparison.
    """
    local = _as_count(local_issue_count)
    remote = _as_count(api_issue_count)
    if local is None or remote is None:
        return False

    # Finished and we already hold every issue.
    if api_status in FINISHED_STATUSES and local >= remote:
        return True

    # Unchanged count (the API's modified date is unreliable for new issues).
    return local == remote
