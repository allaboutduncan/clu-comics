"""
Factory helpers for creating test database records.

These call actual database.py CRUD functions with sensible defaults,
so they validate the same code paths as production.  Every factory
returns the ID / primary key of the created record when possible.
"""
import time


# ---------------------------------------------------------------------------
# Counters for unique defaults
# ---------------------------------------------------------------------------
_counters = {}


def _next(prefix="item"):
    _counters.setdefault(prefix, 0)
    _counters[prefix] += 1
    return _counters[prefix]


def reset_counters():
    _counters.clear()


# ---------------------------------------------------------------------------
# File Index
# ---------------------------------------------------------------------------
def create_file_index_entry(
    name=None,
    path=None,
    entry_type="file",
    size=1024,
    parent="/data/Publisher/Series",
    has_thumbnail=0,
    modified_at=None,
):
    """Add a file_index row via the real add_file_index_entry()."""
    from database import add_file_index_entry

    n = _next("file")
    name = name or f"Comic Issue {n:03d}.cbz"
    path = path or f"{parent}/{name}"
    modified_at = modified_at or time.time()

    ok = add_file_index_entry(
        name=name,
        path=path,
        entry_type=entry_type,
        size=size,
        parent=parent,
        has_thumbnail=has_thumbnail,
        modified_at=modified_at,
    )
    assert ok, f"create_file_index_entry failed for {path}"
    return path


def create_directory_entry(
    name=None,
    path=None,
    parent="/data",
):
    """Convenience wrapper for directory entries."""
    n = _next("dir")
    name = name or f"Series {n}"
    path = path or f"{parent}/{name}"
    return create_file_index_entry(
        name=name,
        path=path,
        entry_type="directory",
        size=None,
        parent=parent,
    )


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------
def create_publisher(
    publisher_id=None,
    name=None,
    path=None,
    logo=None,
):
    """Save a publisher via the real save_publisher()."""
    from database import save_publisher

    n = _next("pub")
    publisher_id = publisher_id or (1000 + n)
    name = name or f"Test Publisher {n}"

    ok = save_publisher(publisher_id=publisher_id, name=name, path=path, logo=logo)
    assert ok, f"create_publisher failed for {name}"
    return publisher_id


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------
def create_series(
    series_id=None,
    name=None,
    volume=2020,
    publisher_id=None,
    mapped_path=None,
    cover_image=None,
    year_began=None,
    year_end=None,
    desc=None,
    status="Ongoing",
):
    """Save a series mapping via the real save_series_mapping()."""
    from database import save_series_mapping

    n = _next("series")
    series_id = series_id or (2000 + n)
    name = name or f"Test Series {n}"
    mapped_path = mapped_path or f"/data/Publisher/{name}"

    # Ensure publisher exists
    if publisher_id is None:
        publisher_id = create_publisher()

    series_data = {
        "id": series_id,
        "name": name,
        "sort_name": name,
        "volume": volume,
        "status": status,
        "publisher": {"id": publisher_id},
        "imprint": None,
        "year_began": year_began or volume,
        "year_end": year_end,
        "desc": desc or f"Description for {name}",
        "cv_id": None,
        "gcd_id": None,
        "resource_url": None,
    }

    ok = save_series_mapping(series_data, mapped_path, cover_image=cover_image)
    assert ok, f"create_series failed for {name}"
    return series_id


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------
def create_issue(
    issue_id=None,
    series_id=None,
    number="1",
    name=None,
    cover_date="2020-01-15",
    store_date="2020-01-10",
    image=None,
):
    """Save an issue via the real save_issue()."""
    from database import save_issue

    n = _next("issue")
    issue_id = issue_id or (3000 + n)

    # Ensure series exists
    if series_id is None:
        series_id = create_series()

    issue_data = {
        "id": issue_id,
        "number": number,
        "issue_name": name or f"Issue {number}",
        "cover_date": cover_date,
        "store_date": store_date,
        "image": image,
        "resource_url": None,
        "cv_id": None,
    }

    ok = save_issue(issue_data, series_id)
    assert ok, f"create_issue failed for issue {issue_id}"
    return issue_id


# ---------------------------------------------------------------------------
# Issue Read
# ---------------------------------------------------------------------------
def create_issue_read(
    issue_path=None,
    read_at=None,
    page_count=24,
    time_spent=600,
    writer="Test Writer",
    penciller="Test Penciller",
    characters="Hero, Villain",
    publisher="Test Publisher",
):
    """Mark an issue as read via the real mark_issue_read()."""
    from database import mark_issue_read

    n = _next("read")
    issue_path = issue_path or f"/data/Publisher/Series/Issue {n:03d}.cbz"

    ok = mark_issue_read(
        issue_path=issue_path,
        read_at=read_at,
        page_count=page_count,
        time_spent=time_spent,
        writer=writer,
        penciller=penciller,
        characters=characters,
        publisher=publisher,
    )
    assert ok, f"create_issue_read failed for {issue_path}"
    return issue_path


# ---------------------------------------------------------------------------
# Reading Position
# ---------------------------------------------------------------------------
def create_reading_position(
    comic_path=None,
    page_number=5,
    total_pages=24,
    time_spent=300,
):
    """Save a reading position via the real save_reading_position()."""
    from database import save_reading_position

    n = _next("pos")
    comic_path = comic_path or f"/data/Publisher/Series/Comic {n:03d}.cbz"

    ok = save_reading_position(
        comic_path=comic_path,
        page_number=page_number,
        total_pages=total_pages,
        time_spent=time_spent,
    )
    assert ok, f"create_reading_position failed for {comic_path}"
    return comic_path


# ---------------------------------------------------------------------------
# Reading List
# ---------------------------------------------------------------------------
def create_reading_list(name=None, source=None):
    """Create a reading list via the real create_reading_list()."""
    from database import create_reading_list as db_create

    n = _next("rlist")
    name = name or f"Test Reading List {n}"

    list_id = db_create(name=name, source=source)
    assert list_id is not None, f"create_reading_list failed for {name}"
    return list_id


def create_reading_list_entry(
    list_id,
    series="Batman",
    issue_number="1",
    volume=2020,
    year=2020,
    matched_file_path=None,
):
    """Add an entry to a reading list via the real add_reading_list_entry()."""
    from database import add_reading_list_entry

    ok = add_reading_list_entry(list_id, {
        "series": series,
        "issue_number": issue_number,
        "volume": volume,
        "year": year,
        "matched_file_path": matched_file_path,
    })
    assert ok, f"create_reading_list_entry failed for {series} #{issue_number}"
    return ok


# ---------------------------------------------------------------------------
# User Preference
# ---------------------------------------------------------------------------
def create_user_preference(key=None, value="test_value", category="general"):
    """Set a user preference via the real set_user_preference()."""
    from database import set_user_preference

    n = _next("pref")
    key = key or f"test_pref_{n}"

    ok = set_user_preference(key=key, value=value, category=category)
    assert ok, f"create_user_preference failed for {key}"
    return key


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------
def create_library(name=None, path=None):
    """Add a library via the real add_library()."""
    from database import add_library

    n = _next("lib")
    name = name or f"Test Library {n}"
    path = path or f"/data/library_{n}"

    library_id = add_library(name=name, path=path)
    assert library_id is not None, f"create_library failed for {name}"
    return library_id
