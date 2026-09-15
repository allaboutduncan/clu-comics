"""The problem_files store against a real SQLite schema.

The lifecycle rules here are the ones most likely to be "simplified" by a later
change, so each has a test that says why it exists.
"""

import os
import time
from unittest.mock import patch

import pytest

from core.problem_files import (
    CLASS_CACHE_WRITE,
    SOURCE_REBUILD,
    SOURCE_THUMBNAIL,
    clear_problem,
    count_problems,
    get_problem,
    list_problems,
    record_problem,
    retry_problem,
    set_dismissed,
)


@pytest.fixture
def comic(tmp_path):
    """A real file on disk, so existence checks behave like production."""
    library = tmp_path / "library"
    library.mkdir()
    path = library / "Wizard Magazine 150 (2004).cbz"
    path.write_bytes(b"not really a cbz")
    return str(path)


@pytest.fixture
def store(db_connection, db_path):
    """Point the store's lazy get_db_connection at the test database."""
    with patch("core.database.get_db_path", return_value=db_path):
        yield


class TestRecording:
    def test_first_record_creates_one_row(self, store, comic):
        assert record_problem(comic, SOURCE_THUMBNAIL, error_class="BadZipFile",
                              error_message="Bad CRC-32 for file 'p0.jpg'") is True
        row = get_problem(comic, SOURCE_THUMBNAIL)
        assert row["occurrences"] == 1
        assert row["error_class"] == "BadZipFile"
        assert row["first_seen"] and row["last_seen"]
        assert row["file_mtime"] is not None

    def test_repeat_bumps_occurrences_instead_of_duplicating(self, store, comic):
        for _ in range(3):
            record_problem(comic, SOURCE_THUMBNAIL, error_class="BadZipFile",
                           error_message="Bad CRC-32")
        assert get_problem(comic, SOURCE_THUMBNAIL)["occurrences"] == 3
        assert len(list_problems()) == 1

    def test_newest_message_wins(self, store, comic):
        record_problem(comic, SOURCE_THUMBNAIL, error_class="BadZipFile", error_message="old")
        record_problem(comic, SOURCE_THUMBNAIL, error_class="error", error_message="new")
        row = get_problem(comic, SOURCE_THUMBNAIL)
        assert row["error_class"] == "error"
        assert row["error_message"] == "new"

    def test_two_sources_coexist_for_one_path(self, store, comic):
        """The composite-key assertion.

        With `path` alone as the key, the rebuild row would overwrite the
        thumbnail row and a successful thumbnail would then clear a rebuild
        failure nobody fixed.
        """
        record_problem(comic, SOURCE_THUMBNAIL, error_class="BadZipFile", error_message="a")
        record_problem(comic, SOURCE_REBUILD, error_class="OSError", error_message="b")
        assert len(list_problems()) == 2
        assert get_problem(comic, SOURCE_THUMBNAIL)["error_message"] == "a"
        assert get_problem(comic, SOURCE_REBUILD)["error_message"] == "b"

    def test_exception_is_sanitised_and_split(self, store, comic):
        """Class and message land in their own columns, blob stripped."""
        import zipfile

        exc = zipfile.BadZipFile(
            "File name in directory 'x.jpg' and header b'\\x00+\\x00rubbish' differ"
        )
        record_problem(comic, SOURCE_THUMBNAIL, exc=exc)
        row = get_problem(comic, SOURCE_THUMBNAIL)
        assert row["error_class"] == "BadZipFile"
        # The message column carries no class prefix and no raw blob.
        assert not row["error_message"].startswith("BadZipFile:")
        assert "<binary>" in row["error_message"]

    def test_never_raises_without_a_database(self, comic):
        with patch("core.database.get_db_connection", return_value=None):
            assert record_problem(comic, SOURCE_THUMBNAIL, error_class="X",
                                  error_message="y") is False


class TestClearing:
    def test_clear_removes_only_that_source(self, store, comic):
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        record_problem(comic, SOURCE_REBUILD, error_class="B", error_message="b")
        assert clear_problem(comic, SOURCE_THUMBNAIL) == 1
        assert get_problem(comic, SOURCE_THUMBNAIL) is None
        assert get_problem(comic, SOURCE_REBUILD) is not None

    def test_clear_without_source_removes_everything_for_the_path(self, store, comic):
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        record_problem(comic, SOURCE_REBUILD, error_class="B", error_message="b")
        assert clear_problem(comic) == 2
        assert list_problems() == []

    def test_a_fixed_file_leaves_no_history(self, store, comic):
        """Ledger, not history -- nothing reads a resolved row."""
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        clear_problem(comic, SOURCE_THUMBNAIL)
        assert list_problems(include_dismissed=True) == []


class TestDismissal:
    def test_dismissed_rows_are_hidden_by_default(self, store, comic):
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        assert set_dismissed(comic, SOURCE_THUMBNAIL) is True
        assert list_problems() == []
        assert len(list_problems(include_dismissed=True)) == 1

    def test_same_failure_on_an_unchanged_file_stays_dismissed(self, store, comic):
        """Dismiss would be useless if any later sweep un-did it."""
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        set_dismissed(comic, SOURCE_THUMBNAIL)
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        assert list_problems() == []
        assert get_problem(comic, SOURCE_THUMBNAIL)["occurrences"] == 2

    def test_a_different_error_on_an_unchanged_file_stays_dismissed(self, store, comic):
        """A damaged archive raises different classes depending on which page
        is read first, so the error text is not a stable identity."""
        record_problem(comic, SOURCE_THUMBNAIL, error_class="BadZipFile", error_message="a")
        set_dismissed(comic, SOURCE_THUMBNAIL)
        record_problem(comic, SOURCE_THUMBNAIL, error_class="error", error_message="b")
        assert list_problems() == []

    def test_a_rewritten_file_that_still_fails_comes_back(self, store, comic):
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        set_dismissed(comic, SOURCE_THUMBNAIL)
        assert list_problems() == []

        # The user's knowledge is now stale: the file changed and still fails.
        os.utime(comic, (time.time() + 10, time.time() + 10))
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        assert len(list_problems()) == 1

    def test_undismiss(self, store, comic):
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        set_dismissed(comic, SOURCE_THUMBNAIL, dismissed=True)
        set_dismissed(comic, SOURCE_THUMBNAIL, dismissed=False)
        assert len(list_problems()) == 1

    def test_dismiss_reports_a_miss(self, store, comic):
        assert set_dismissed(comic, SOURCE_THUMBNAIL) is False


class TestPruning:
    def test_a_deleted_file_is_dropped_from_the_list_and_the_table(self, store, comic):
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        os.remove(comic)
        assert list_problems() == []
        # Actually gone, not merely filtered.
        assert get_problem(comic, SOURCE_THUMBNAIL) is None

    def test_an_unmounted_library_is_never_pruned(self, store, tmp_path):
        """The guard that stops a sleeping NAS emptying the whole table.

        A missing file only proves a deletion when its parent directory is
        still there. Do not replace this with a bare os.path.exists.
        """
        missing = str(tmp_path / "unmounted" / "Comic 1.cbz")
        record_problem(missing, SOURCE_THUMBNAIL, error_class="A", error_message="a")

        rows = list_problems()
        assert len(rows) == 1
        assert rows[0]["reachable"] is False
        assert get_problem(missing, SOURCE_THUMBNAIL) is not None

    def test_pruning_can_be_switched_off(self, store, comic):
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        os.remove(comic)
        assert len(list_problems(prune_missing=False)) == 1


class TestFilteringAndCounts:
    def test_source_filter(self, store, comic):
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        record_problem(comic, SOURCE_REBUILD, error_class="B", error_message="b")
        assert len(list_problems(source=SOURCE_THUMBNAIL)) == 1

    def test_path_query(self, store, comic, tmp_path):
        other = tmp_path / "library" / "Batman 1.cbz"
        other.write_bytes(b"x")
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        record_problem(str(other), SOURCE_THUMBNAIL, error_class="A", error_message="a")
        assert len(list_problems(query="Batman")) == 1

    def test_rows_carry_their_classification_and_display_fields(self, store, comic):
        record_problem(comic, SOURCE_THUMBNAIL, error_class=CLASS_CACHE_WRITE,
                       error_message="Could not write the thumbnail")
        row = list_problems()[0]
        assert row["filename"] == "Wizard Magazine 150 (2004).cbz"
        assert row["source_label"] == "Thumbnail"
        assert row["can_retry"] is True
        assert row["classification"]["healthy_file"] is True

    def test_counts(self, store, comic):
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        record_problem(comic, SOURCE_REBUILD, error_class="B", error_message="b")
        set_dismissed(comic, SOURCE_REBUILD)
        counts = count_problems()
        assert counts["open"] == 1
        assert counts["dismissed"] == 1
        assert counts["by_source"][SOURCE_THUMBNAIL] == 1


class TestCap:
    def test_the_cap_blocks_new_rows_but_not_updates(self, store, comic, tmp_path):
        """A revoked mount turns every file into an error; the page must not
        become a 40,000-row list."""
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")

        with patch("core.problem_files.MAX_OPEN_PROBLEMS", 1):
            other = tmp_path / "library" / "Other.cbz"
            other.write_bytes(b"x")
            assert record_problem(str(other), SOURCE_THUMBNAIL,
                                  error_class="A", error_message="a") is False
            # The existing row still updates -- its count stays truthful.
            assert record_problem(comic, SOURCE_THUMBNAIL,
                                  error_class="A", error_message="a") is True

        assert get_problem(comic, SOURCE_THUMBNAIL)["occurrences"] == 2


class TestRetryDispatch:
    def test_missing_file_reports_cleanly(self, store, tmp_path):
        ok, message = retry_problem(str(tmp_path / "nope.cbz"), SOURCE_THUMBNAIL)
        assert ok is False
        assert "not found" in message.lower()

    def test_rebuild_points_at_the_rebuild_action(self, store, comic):
        ok, message = retry_problem(comic, SOURCE_REBUILD)
        assert ok is False
        assert "rebuild" in message.lower()

    def test_unknown_source(self, store, comic):
        ok, message = retry_problem(comic, "nonsense")
        assert ok is False
        assert "unknown source" in message.lower()

    def test_thumbnail_retry_clears_the_cache_before_regenerating(self, store, comic):
        """invalidate first is required: /api/thumbnail refuses to re-attempt an
        errored row whose mtime has not changed, and a stale JPEG would go on
        being served."""
        calls = []
        with patch("core.thumbnail_cache.invalidate_thumbnail",
                   side_effect=lambda p: calls.append(("invalidate", p))), \
             patch("core.thumbnail_cache.regenerate_thumbnail",
                   side_effect=lambda p: calls.append(("regenerate", p)) or True):
            ok, _ = retry_problem(comic, SOURCE_THUMBNAIL)

        assert ok is True
        assert [c[0] for c in calls] == ["invalidate", "regenerate"]


class TestAFailedReadIsNotAnEmptyList:
    """The page reported "Nothing has failed" while its own summary counted 56.

    `list_problems` returned [] because the prune DELETE failed, and the count
    (a separate read) still saw the rows. An empty list has to mean "nothing is
    wrong"; anything else has to be distinguishable.
    """

    def test_a_read_failure_returns_none_not_empty(self, store, comic):
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        with patch("core.database.get_db_connection", return_value=None):
            assert list_problems() is None

    def test_an_empty_table_still_returns_a_list(self, store):
        assert list_problems() == []

    def test_rows_that_could_not_be_pruned_stay_listed(self, store, comic):
        """Hiding a row because we *meant* to delete it makes a read depend on a
        write succeeding -- which is exactly how the page lost 56 entries."""
        import core.problem_files as pf

        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        os.remove(comic)   # provably gone: parent still exists

        with patch.object(pf, "_prune", return_value=set()):
            rows = list_problems()

        assert len(rows) == 1, "a row we failed to delete must not vanish"
        assert rows[0]["reachable"] is False
        # And it is still in the table, so the counts agree with the listing.
        assert count_problems()["open"] == 1

    def test_a_successful_prune_still_removes_the_row(self, store, comic):
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")
        os.remove(comic)
        assert list_problems() == []
        assert count_problems()["open"] == 0

    def test_the_listing_and_the_count_never_disagree(self, store, comic, tmp_path):
        """The invariant the page depends on."""
        library = tmp_path / "library"
        for i in range(5):
            p = library / f"C{i}.cbz"
            p.write_bytes(b"x")
            record_problem(str(p), SOURCE_THUMBNAIL, error_class="A", error_message="a")
        record_problem(comic, SOURCE_THUMBNAIL, error_class="A", error_message="a")

        for i in range(3):
            (library / f"C{i}.cbz").unlink()

        rows = list_problems()
        assert len(rows) == count_problems()["open"]

