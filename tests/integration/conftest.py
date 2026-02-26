"""
Integration test fixtures.

Provides a populated database with sample publishers, series, issues,
file_index entries, and read history using the factory helpers.
"""
import pytest
from unittest.mock import patch
from tests.factories.db_factories import (
    reset_counters,
    create_publisher,
    create_series,
    create_issue,
    create_file_index_entry,
    create_directory_entry,
    create_issue_read,
    create_reading_position,
    create_reading_list,
    create_reading_list_entry,
    create_user_preference,
    create_library,
)


@pytest.fixture(autouse=True)
def _reset_factory_counters():
    """Reset factory counters before each test."""
    reset_counters()
    yield
    reset_counters()


@pytest.fixture
def populated_db(db_connection):
    """
    Seed the database with sample data via factory helpers.

    Creates:
    - 2 publishers (DC Comics, Marvel)
    - 2 series (Batman, Spider-Man)
    - 5 issues per series (10 total)
    - File index entries for each issue
    - 3 read records
    - 1 reading position (in-progress)
    - 1 reading list with 2 entries
    - 1 user preference

    Returns the db_connection for further queries.
    """
    # Publishers
    dc_id = create_publisher(publisher_id=10, name="DC Comics", path="/data/DC Comics")
    marvel_id = create_publisher(publisher_id=20, name="Marvel", path="/data/Marvel")

    # Series
    batman_id = create_series(
        series_id=100,
        name="Batman",
        volume=2020,
        publisher_id=dc_id,
        mapped_path="/data/DC Comics/Batman",
    )
    spidey_id = create_series(
        series_id=200,
        name="Amazing Spider-Man",
        volume=2018,
        publisher_id=marvel_id,
        mapped_path="/data/Marvel/Amazing Spider-Man",
    )

    # Directory entries for series
    create_directory_entry(name="DC Comics", path="/data/DC Comics", parent="/data")
    create_directory_entry(name="Batman", path="/data/DC Comics/Batman", parent="/data/DC Comics")
    create_directory_entry(name="Marvel", path="/data/Marvel", parent="/data")
    create_directory_entry(name="Amazing Spider-Man", path="/data/Marvel/Amazing Spider-Man", parent="/data/Marvel")

    # Issues and file index entries
    batman_paths = []
    for i in range(1, 6):
        issue_id = create_issue(
            issue_id=1000 + i,
            series_id=batman_id,
            number=str(i),
            cover_date=f"2020-{i:02d}-15",
            store_date=f"2020-{i:02d}-10",
        )
        path = create_file_index_entry(
            name=f"Batman {i:03d} (2020).cbz",
            path=f"/data/DC Comics/Batman/Batman {i:03d} (2020).cbz",
            parent="/data/DC Comics/Batman",
            size=50_000_000 + i * 1_000_000,
        )
        batman_paths.append(path)

    spidey_paths = []
    for i in range(1, 6):
        issue_id = create_issue(
            issue_id=2000 + i,
            series_id=spidey_id,
            number=str(i),
            cover_date=f"2018-{i:02d}-15",
            store_date=f"2018-{i:02d}-10",
        )
        path = create_file_index_entry(
            name=f"Amazing Spider-Man {i:03d} (2018).cbz",
            path=f"/data/Marvel/Amazing Spider-Man/Amazing Spider-Man {i:03d} (2018).cbz",
            parent="/data/Marvel/Amazing Spider-Man",
            size=40_000_000 + i * 1_000_000,
        )
        spidey_paths.append(path)

    # Mark some issues as read
    create_issue_read(
        issue_path=batman_paths[0],
        page_count=24,
        time_spent=600,
        writer="Tom King",
        penciller="David Finch",
        characters="Batman, Catwoman",
        publisher="DC Comics",
    )
    create_issue_read(
        issue_path=batman_paths[1],
        page_count=22,
        time_spent=550,
        writer="Tom King",
        penciller="Mikel Janin",
        characters="Batman, Robin",
        publisher="DC Comics",
    )
    create_issue_read(
        issue_path=spidey_paths[0],
        page_count=30,
        time_spent=700,
        writer="Nick Spencer",
        penciller="Ryan Ottley",
        characters="Spider-Man, MJ",
        publisher="Marvel",
    )

    # In-progress reading position
    create_reading_position(
        comic_path=batman_paths[2],
        page_number=10,
        total_pages=24,
    )

    # Reading list
    list_id = create_reading_list(name="DC Essentials")
    create_reading_list_entry(list_id, series="Batman", issue_number="1", volume=2020, year=2020)
    create_reading_list_entry(list_id, series="Batman", issue_number="2", volume=2020, year=2020)

    # User preference
    create_user_preference(key="theme", value="darkly")

    return db_connection
