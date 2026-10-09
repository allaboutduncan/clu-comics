"""What WATCH and TARGET are allowed to point at.

The rule used to be a hardcoded ``/data`` literal, copy-pasted into three
places: the wanted scan's own guard and both settings-save handlers. It said
nothing about a *configured* library, so ``TARGET = /library/media`` with a
library at ``/library/media`` sailed through — and the wanted scan then walked
the collection and moved filed comics between series folders.

The predicate lives in helpers/ so it can be tested: app.py cannot be imported.
"""
import os

import pytest

from helpers.library import library_root_for, watch_target_verdict


@pytest.fixture
def libraries(monkeypatch):
    def _set(*roots):
        monkeypatch.setattr(
            "helpers.library.get_library_roots", lambda: [str(r) for r in roots]
        )
    return _set


class TestLibraryRootFor:

    def test_a_library_root_is_its_own_root(self, tmp_path, libraries):
        libraries(tmp_path)
        assert library_root_for(str(tmp_path)) == str(tmp_path)

    def test_a_subdirectory_resolves_to_its_library(self, tmp_path, libraries):
        libraries(tmp_path)
        inner = tmp_path / "DC Comics" / "(2016) Batman v3"
        inner.mkdir(parents=True)
        assert library_root_for(str(inner)) == str(tmp_path)

    def test_an_unrelated_path_has_no_library(self, tmp_path, libraries):
        libraries(tmp_path / "library")
        (tmp_path / "library").mkdir()
        assert library_root_for(str(tmp_path / "downloads")) is None

    def test_a_sibling_sharing_a_prefix_is_not_inside(self, tmp_path, libraries):
        """/library/media must not swallow /library/media-old."""
        libraries(tmp_path / "media")
        (tmp_path / "media").mkdir()
        assert library_root_for(str(tmp_path / "media-old")) is None

    @pytest.mark.parametrize("value", ["", None])
    def test_missing_input_has_no_library(self, value, libraries):
        libraries("/data")
        assert library_root_for(value) is None

    @pytest.mark.skipif(
        os.name == "nt", reason="symlink creation needs privileges on Windows"
    )
    def test_symlinks_are_resolved(self, tmp_path, libraries):
        """A symlinked TARGET is how a library sneaks back into a path that
        looks unrelated -- which is why this is realpath, not normpath."""
        real = tmp_path / "library"
        real.mkdir()
        link = tmp_path / "elsewhere"
        link.symlink_to(real)
        libraries(real)
        assert library_root_for(str(link)) == str(real)


class TestWatchTargetVerdict:

    def test_data_is_blocked(self, libraries, monkeypatch):
        libraries()  # no libraries configured at all
        monkeypatch.setattr("os.path.realpath", lambda p: str(p))
        blocked, message = watch_target_verdict("TARGET", "/data")
        assert blocked is True
        assert "/data" in message

    def test_inside_data_is_blocked(self, libraries, monkeypatch):
        libraries()
        monkeypatch.setattr("os.path.realpath", lambda p: str(p))
        # os.path.join so the separator matches the platform -- the guard
        # compares with os.sep, and a hand-written "/" would pass on Windows
        # for the wrong reason.
        blocked, message = watch_target_verdict(
            "WATCH", os.path.join("/data", "DC Comics")
        )
        assert blocked is True
        assert message.startswith("WATCH")

    def test_a_library_root_warns_but_saves(self, tmp_path, libraries):
        """Blocking would make an existing install's config unsaveable on
        upgrade. The layout is safe -- collect_target_candidates refuses to
        descend -- but the restriction has to be said out loud."""
        libraries(tmp_path)
        blocked, message = watch_target_verdict("TARGET", str(tmp_path))
        assert blocked is False
        assert message and "top level" in message

    def test_inside_a_library_warns_but_saves(self, tmp_path, libraries):
        libraries(tmp_path)
        inner = tmp_path / "media"
        inner.mkdir()
        blocked, message = watch_target_verdict("TARGET", str(inner))
        assert blocked is False
        assert message is not None

    def test_a_staging_folder_is_clean(self, tmp_path, libraries):
        libraries(tmp_path / "library")
        (tmp_path / "library").mkdir()
        staging = tmp_path / "downloads" / "processed"
        staging.mkdir(parents=True)
        assert watch_target_verdict("TARGET", str(staging)) == (False, None)

    @pytest.mark.parametrize("value", ["", None])
    def test_an_empty_value_is_not_rejected(self, value, libraries):
        """The save handlers skip empty values and fall back to defaults."""
        libraries("/data")
        assert watch_target_verdict("TARGET", value) == (False, None)


class TestBindMountAlias:
    """TARGET=/library2 and a library at /data can be ONE host folder under two
    names. ``realpath`` cannot see that, so the scan walked all 12,010 library
    comics as "incoming". Identity is device + inode."""

    def test_same_directory_by_stat(self, tmp_path):
        from helpers.library import _same_directory
        (tmp_path / "x").mkdir()
        assert _same_directory(str(tmp_path), str(tmp_path / "x" / ".."))
        assert not _same_directory(str(tmp_path), str(tmp_path / "x"))

    def test_missing_path_is_never_the_same(self, tmp_path):
        from helpers.library import _same_directory
        assert not _same_directory(str(tmp_path), str(tmp_path / "nope"))

    def test_zero_inode_is_unknown_not_a_match(self, tmp_path, monkeypatch):
        from helpers.library import _same_directory
        real = os.stat(tmp_path)
        zeroed = os.stat_result((real.st_mode, 0, real.st_dev, 1, 0, 0, 0, 0, 0, 0))
        monkeypatch.setattr("helpers.library.os.stat", lambda p: zeroed)
        assert not _same_directory("a", "b")

    def test_an_alias_of_a_library_root_is_inside_it(self, tmp_path, libraries, monkeypatch):
        library = tmp_path / "data"
        alias = tmp_path / "library2"
        library.mkdir()
        alias.mkdir()
        libraries(library)
        monkeypatch.setattr(
            "helpers.library._same_directory",
            lambda a, b: os.path.realpath(a) == str(alias.resolve())
            and os.path.realpath(b) == str(library.resolve()),
        )
        assert library_root_for(str(alias)) == str(library)

    def test_a_folder_inside_an_alias_is_inside_it(self, tmp_path, libraries, monkeypatch):
        library = tmp_path / "data"
        alias = tmp_path / "library2"
        (alias / "staging").mkdir(parents=True)
        library.mkdir()
        libraries(library)
        monkeypatch.setattr(
            "helpers.library._same_directory",
            lambda a, b: os.path.realpath(a) == str(alias.resolve()),
        )
        assert library_root_for(str(alias / "staging")) == str(library)

    def test_alias_saves_with_a_warning_and_restricts_the_scan(
        self, tmp_path, libraries, monkeypatch
    ):
        from helpers.collection import collect_target_candidates
        library = tmp_path / "data"
        alias = tmp_path / "library2"
        (alias / "Series A").mkdir(parents=True)
        library.mkdir()
        (alias / "Loose 001.cbz").write_bytes(b"x")
        (alias / "Series A" / "Filed 001.cbz").write_bytes(b"x")
        libraries(library)
        monkeypatch.setattr(
            "helpers.library._same_directory",
            lambda a, b: os.path.realpath(a) == str(alias.resolve()),
        )
        blocked, message = watch_target_verdict("TARGET", str(alias))
        assert blocked is False
        assert message and "another mount" in message

        files, restricted = collect_target_candidates(str(alias), mapped_dirs=[])
        assert restricted is True
        assert [name for name, _ in files] == ["Loose 001.cbz"]

    def test_unrelated_staging_folder_is_not_an_alias(self, tmp_path, libraries):
        (tmp_path / "library").mkdir()
        (tmp_path / "staging").mkdir()
        libraries(tmp_path / "library")
        assert library_root_for(str(tmp_path / "staging")) is None
