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
