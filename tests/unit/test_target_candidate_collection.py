"""Which files in TARGET the wanted scan is allowed to consider.

``process_incoming_wanted_issues`` used to walk TARGET with a bare
``os.walk`` and no exclusions. With ``TARGET`` pointed at a library root --
a layout people really run, because monitor.py drops finished downloads flat
there -- that handed the matcher every comic in the library, so a comic
already filed in a series folder was offered up as a fresh download. Twenty-six
of them were moved into another series' folder as a result.

``collect_target_candidates`` is where that judgement lives now, out of app.py
so it can actually be tested.
"""
import os

import pytest

from helpers.collection import collect_target_candidates


def _touch(path, data=b"stub"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _names(files):
    return sorted(name for name, _ in files)


@pytest.fixture
def no_libraries(monkeypatch):
    """TARGET is outside every library -- the majority layout."""
    monkeypatch.setattr("helpers.library.get_library_roots", lambda: [])


@pytest.fixture
def library_is(monkeypatch):
    def _set(*roots):
        monkeypatch.setattr(
            "helpers.library.get_library_roots", lambda: [str(r) for r in roots]
        )
    return _set


class TestLooseFilesAreStillFiled:
    """Non-regression: the thing this pass exists to do must keep working."""

    def test_a_loose_file_at_the_top_is_a_candidate(self, tmp_path, no_libraries):
        _touch(str(tmp_path / "Batman 005 (2020).cbz"))
        files, restricted = collect_target_candidates(str(tmp_path), mapped_dirs=[])
        assert _names(files) == ["Batman 005 (2020).cbz"]
        assert restricted is False

    def test_wrapper_folders_are_still_walked_outside_a_library(
        self, tmp_path, no_libraries
    ):
        """The common layout: TARGET is a staging folder, downloads arrive
        inside their own release folder. Nothing here may change."""
        _touch(str(tmp_path / "Thor 801" / "Thor 801 (2026).cbz"))
        files, restricted = collect_target_candidates(str(tmp_path), mapped_dirs=[])
        assert _names(files) == ["Thor 801 (2026).cbz"]
        assert restricted is False

    def test_missing_target_is_empty_not_an_error(self, tmp_path, no_libraries):
        files, restricted = collect_target_candidates(
            str(tmp_path / "nope"), mapped_dirs=[]
        )
        assert files == []
        assert restricted is False


class TestFiledComicsAreNotCandidates:
    """The guard that holds in every layout, mapped or not."""

    def test_a_file_inside_a_series_folder_is_skipped(self, tmp_path, no_libraries):
        series = tmp_path / "(2016) Batman v3"
        _touch(str(series / "Batman 053 (2018).cbz"))
        _touch(str(tmp_path / "Batman 999 (2026).cbz"))

        files, _ = collect_target_candidates(
            str(tmp_path), mapped_dirs=[str(series)]
        )
        assert _names(files) == ["Batman 999 (2026).cbz"]

    def test_a_nested_subfolder_of_a_series_folder_is_skipped(
        self, tmp_path, no_libraries
    ):
        series = tmp_path / "(2016) Batman v3"
        _touch(str(series / "Annuals" / "Batman Annual 001.cbz"))

        files, _ = collect_target_candidates(
            str(tmp_path), mapped_dirs=[str(series)]
        )
        assert files == []

    def test_the_target_root_itself_is_never_pruned(self, tmp_path, no_libraries):
        """Even a TARGET that happens to be a mapped folder still collects.

        The caller named it as the staging area; silently collecting nothing
        would be worse than this edge case.
        """
        _touch(str(tmp_path / "Batman 005 (2020).cbz"))
        files, _ = collect_target_candidates(
            str(tmp_path), mapped_dirs=[str(tmp_path)]
        )
        assert _names(files) == ["Batman 005 (2020).cbz"]

    def test_a_database_failure_does_not_open_the_library_up(
        self, tmp_path, no_libraries, monkeypatch
    ):
        """Fail closed on the *guard*: an unknown series list protects nothing,
        so it must not be mistaken for "no series folders exist"."""
        series = tmp_path / "(2016) Batman v3"
        _touch(str(series / "Batman 053 (2018).cbz"))

        def boom():
            raise RuntimeError("database disk image is malformed")

        monkeypatch.setattr("core.database.get_all_mapped_series", boom)
        files, _ = collect_target_candidates(str(tmp_path), mapped_dirs=None)
        # Nothing is protected, but nothing crashes either -- the per-move
        # check in app.py is the backstop for this case.
        assert _names(files) == ["Batman 053 (2018).cbz"]


class TestTargetInsideALibrary:
    """The reported configuration: TARGET == the library root."""

    def test_the_walk_does_not_descend(self, tmp_path, library_is):
        library_is(tmp_path)
        _touch(str(tmp_path / "(2016) Batman v3" / "Batman 053 (2018).cbz"))
        _touch(str(tmp_path / "(2003) Superman - Batman v1" / "Superman Batman 001.cbz"))
        _touch(str(tmp_path / "Bishop 004 (2026).cbz"))

        files, restricted = collect_target_candidates(str(tmp_path), mapped_dirs=[])
        assert restricted is True
        assert _names(files) == ["Bishop 004 (2026).cbz"]

    def test_unmapped_series_folders_are_protected_too(self, tmp_path, library_is):
        """Mapped-path pruning alone would leave these exposed."""
        library_is(tmp_path)
        _touch(str(tmp_path / "Some Folder CLU Knows Nothing About" / "x.cbz"))

        files, restricted = collect_target_candidates(str(tmp_path), mapped_dirs=[])
        assert restricted is True
        assert files == []

    def test_a_subfolder_of_a_library_counts(self, tmp_path, library_is):
        library_is(tmp_path / "library")
        target = tmp_path / "library" / "media"
        _touch(str(target / "Bishop 004 (2026).cbz"))
        _touch(str(target / "(2016) Batman v3" / "Batman 053 (2018).cbz"))

        files, restricted = collect_target_candidates(str(target), mapped_dirs=[])
        assert restricted is True
        assert _names(files) == ["Bishop 004 (2026).cbz"]


class TestConvertedSiblings:
    """Carried over verbatim from the inline walk; both callers need a say."""

    def test_a_cbr_beside_its_own_cbz_is_skipped(self, tmp_path, no_libraries):
        _touch(str(tmp_path / "Batman 001.cbr"))
        _touch(str(tmp_path / "Batman 001.cbz"))
        files, _ = collect_target_candidates(str(tmp_path), mapped_dirs=[])
        assert _names(files) == ["Batman 001.cbz"]

    def test_a_cbr_without_a_cbz_is_still_a_candidate(self, tmp_path, no_libraries):
        _touch(str(tmp_path / "Batman 001.cbr"))
        files, _ = collect_target_candidates(str(tmp_path), mapped_dirs=[])
        assert _names(files) == ["Batman 001.cbr"]

    def test_the_replacement_pass_may_opt_out(self, tmp_path, no_libraries):
        """core.problem_replacements decides this in is_acceptable_replacement."""
        _touch(str(tmp_path / "Batman 001.cbr"))
        _touch(str(tmp_path / "Batman 001.cbz"))
        files, _ = collect_target_candidates(
            str(tmp_path), mapped_dirs=[], skip_converted_siblings=False
        )
        assert _names(files) == ["Batman 001.cbr", "Batman 001.cbz"]

    def test_non_comics_are_never_candidates(self, tmp_path, no_libraries):
        _touch(str(tmp_path / "cover.jpg"))
        _touch(str(tmp_path / "series.json"))
        files, _ = collect_target_candidates(str(tmp_path), mapped_dirs=[])
        assert files == []
