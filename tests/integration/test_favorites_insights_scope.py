"""
PR4: per-user scoping of favorites, insights, and reading lists.

Verifies two users keep independent favorite series/publishers, insights
(library stats read counts, reading history, heatmap, timeline, wrapped), and
reading lists.
"""
from unittest.mock import patch

import pytest

import models.stats as stats
import models.timeline as timeline
import wrapped
from core.database import (
    add_favorite_series,
    add_reading_list_entry,
    create_reading_list,
    get_all_reading_lists,
    get_favorite_publishers,
    get_favorite_series,
    get_reading_lists,
    is_favorite_publisher,
    is_favorite_series,
    mark_issue_read,
    remove_favorite_series,
    set_publisher_favorite,
)

get_library_stats = stats.get_library_stats

A, B = 1, 2


# ---------------------------------------------------------------------------
# Favorite series
# ---------------------------------------------------------------------------
class TestFavoriteSeriesIsolation:
    def test_independent(self, db_connection):
        add_favorite_series("/data/DC/Batman", user_id=A)
        assert is_favorite_series("/data/DC/Batman", user_id=A) is True
        assert is_favorite_series("/data/DC/Batman", user_id=B) is False
        assert [f["series_path"] for f in get_favorite_series(user_id=A)] == ["/data/DC/Batman"]
        assert get_favorite_series(user_id=B) == []

    def test_remove_scoped(self, db_connection):
        add_favorite_series("/data/X", user_id=A)
        add_favorite_series("/data/X", user_id=B)
        remove_favorite_series("/data/X", user_id=A)
        assert is_favorite_series("/data/X", user_id=A) is False
        assert is_favorite_series("/data/X", user_id=B) is True


# ---------------------------------------------------------------------------
# Favorite publishers (per-user join table)
# ---------------------------------------------------------------------------
class TestFavoritePublisherIsolation:
    def test_independent(self, db_connection):
        set_publisher_favorite("/data/DC", favorite=True, user_id=A)
        assert is_favorite_publisher("/data/DC", user_id=A) is True
        assert is_favorite_publisher("/data/DC", user_id=B) is False
        assert [p["publisher_path"] for p in get_favorite_publishers(user_id=A)] == ["/data/DC"]
        assert get_favorite_publishers(user_id=B) == []

    def test_unfavorite_scoped(self, db_connection):
        set_publisher_favorite("/data/Marvel", favorite=True, user_id=A)
        set_publisher_favorite("/data/Marvel", favorite=True, user_id=B)
        set_publisher_favorite("/data/Marvel", favorite=False, user_id=A)
        assert is_favorite_publisher("/data/Marvel", user_id=A) is False
        assert is_favorite_publisher("/data/Marvel", user_id=B) is True


# ---------------------------------------------------------------------------
# Insights: library stats, reading history, heatmap (models/stats.py)
# ---------------------------------------------------------------------------
class TestInsightsIsolation:
    def _seed(self):
        mark_issue_read("/data/a.cbz", read_at="2024-06-01T10:00:00",
                        page_count=20, user_id=A)
        mark_issue_read("/data/b.cbz", read_at="2024-06-02T10:00:00",
                        page_count=10, user_id=A)
        mark_issue_read("/data/c.cbz", read_at="2024-07-01T10:00:00",
                        page_count=5, user_id=B)

    def test_library_stats_read_count_per_user(self, db_connection):
        self._seed()
        with patch.object(stats, "current_user_id", return_value=A):
            assert get_library_stats()["total_read"] == 2
        with patch.object(stats, "current_user_id", return_value=B):
            assert get_library_stats()["total_read"] == 1

    def test_reading_history_per_user(self, db_connection):
        self._seed()
        with patch.object(stats, "current_user_id", return_value=A):
            total_a = sum(d["count"] for d in stats.get_reading_history_stats())
        with patch.object(stats, "current_user_id", return_value=B):
            total_b = sum(d["count"] for d in stats.get_reading_history_stats())
        assert total_a == 2
        assert total_b == 1

    def test_heatmap_per_user(self, db_connection):
        self._seed()
        with patch.object(stats, "current_user_id", return_value=A):
            hm_a = stats.get_reading_heatmap_data()
        with patch.object(stats, "current_user_id", return_value=B):
            hm_b = stats.get_reading_heatmap_data()
        # A read 2 in June 2024; B read 1 in July 2024.
        assert hm_a["2024"][5] == 2 and hm_a["2024"][6] == 0
        assert hm_b["2024"][6] == 1 and hm_b["2024"][5] == 0


# ---------------------------------------------------------------------------
# Timeline + wrapped
# ---------------------------------------------------------------------------
class TestTimelineWrappedIsolation:
    def _seed(self):
        mark_issue_read("/data/a.cbz", read_at="2024-06-01T10:00:00", user_id=A)
        mark_issue_read("/data/b.cbz", read_at="2024-06-02T10:00:00", user_id=A)
        mark_issue_read("/data/c.cbz", read_at="2024-06-03T10:00:00", user_id=B)

    def test_timeline_total_read_per_user(self, db_connection):
        self._seed()
        assert timeline.get_reading_timeline(user_id=A)["stats"]["total_read"] == 2
        assert timeline.get_reading_timeline(user_id=B)["stats"]["total_read"] == 1

    def test_wrapped_yearly_total_per_user(self, db_connection):
        self._seed()
        with patch.object(wrapped, "current_user_id", return_value=A):
            assert wrapped.get_yearly_total_read(2024) == 2
        with patch.object(wrapped, "current_user_id", return_value=B):
            assert wrapped.get_yearly_total_read(2024) == 1


# ---------------------------------------------------------------------------
# Reading lists
# ---------------------------------------------------------------------------
class TestReadingListIsolation:
    def test_lists_are_per_user(self, db_connection):
        create_reading_list("A's List", user_id=A)
        create_reading_list("B's List", user_id=B)
        names_a = {rl["name"] for rl in get_reading_lists(user_id=A)}
        names_b = {rl["name"] for rl in get_reading_lists(user_id=B)}
        assert names_a == {"A's List"}
        assert names_b == {"B's List"}

    def test_get_all_reading_lists_is_global(self, db_connection):
        # The grid reader shows every user's lists to whoever is viewing, so a
        # reader can browse lists an admin/clerk imported. Per-user isolation
        # (test_lists_are_per_user, above) is preserved on get_reading_lists().
        create_reading_list("A's List", user_id=A)
        create_reading_list("B's List", user_id=B)
        for viewer in (A, B):
            names = {rl["name"] for rl in get_all_reading_lists(viewer_id=viewer)}
            assert names == {"A's List", "B's List"}

    def test_get_all_reading_lists_read_count_scoped_to_viewer(self, db_connection):
        # The list set is global, but the progress badge (read_count) must reflect
        # the viewing user's own reads, not the list owner's or another user's.
        list_id = create_reading_list("Shared", user_id=A)
        path = "/data/DC/Batman/Batman 001.cbz"
        add_reading_list_entry(list_id, {"series": "Batman", "issue_number": "1",
                                         "matched_file_path": path})
        mark_issue_read(path, user_id=A)

        by_id = {rl["id"]: rl for rl in get_all_reading_lists(viewer_id=A)}
        assert by_id[list_id]["entry_count"] == 1
        assert by_id[list_id]["read_count"] == 1  # A read it

        by_id_b = {rl["id"]: rl for rl in get_all_reading_lists(viewer_id=B)}
        assert by_id_b[list_id]["entry_count"] == 1
        assert by_id_b[list_id]["read_count"] == 0  # B has not
