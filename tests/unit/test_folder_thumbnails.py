"""Tests for core/folder_thumbnails.py -- the background auto-generation queue
and the nested-folder composite.

The queue's whole job is to be cheap to call from a hot path: browsing a folder
enqueues every child that has no cover art, so the guards that stop it from
re-doing work (dedupe, failure back-off, size cap) are what these cover.
"""
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from PIL import Image

from core import folder_thumbnails

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOLDER_ICON = os.path.join(PROJECT_ROOT, "static", "images", "folder-fill-200x300.png")


@pytest.fixture(autouse=True)
def clean_queue_state():
    folder_thumbnails.reset_state()
    yield
    folder_thumbnails.reset_state()


@pytest.fixture
def fake_executor():
    """Executor that records submissions without running them.

    Leaving the work unfinished is deliberate: it holds paths in the in-flight
    set, which is the state the dedupe guard is meant to react to.
    """
    executor = MagicMock()
    with patch.object(folder_thumbnails, "_get_executor", return_value=executor):
        yield executor


class TestEnqueue:

    def test_accepts_a_directory(self, fake_executor, tmp_path):
        assert folder_thumbnails.enqueue(str(tmp_path)) is True
        assert fake_executor.submit.call_count == 1

    def test_rejects_empty_path(self, fake_executor):
        assert folder_thumbnails.enqueue("") is False
        assert folder_thumbnails.enqueue(None) is False
        fake_executor.submit.assert_not_called()

    def test_rejects_missing_directory(self, fake_executor, tmp_path):
        assert folder_thumbnails.enqueue(str(tmp_path / "nope")) is False
        fake_executor.submit.assert_not_called()

    def test_rejects_a_file(self, fake_executor, tmp_path):
        target = tmp_path / "folder.png"
        target.write_bytes(b"png")
        assert folder_thumbnails.enqueue(str(target)) is False
        fake_executor.submit.assert_not_called()

    def test_dedupes_while_in_flight(self, fake_executor, tmp_path):
        assert folder_thumbnails.enqueue(str(tmp_path)) is True
        assert folder_thumbnails.enqueue(str(tmp_path)) is False
        assert fake_executor.submit.call_count == 1

    def test_requeues_after_completion(self, fake_executor, tmp_path):
        assert folder_thumbnails.enqueue(str(tmp_path)) is True
        folder_thumbnails._record_result(str(tmp_path), True)
        assert folder_thumbnails.enqueue(str(tmp_path)) is True
        assert fake_executor.submit.call_count == 2

    def test_honours_the_queue_cap(self, fake_executor, tmp_path):
        for i in range(folder_thumbnails.MAX_QUEUED):
            folder = tmp_path / f"series{i}"
            folder.mkdir()
            assert folder_thumbnails.enqueue(str(folder)) is True

        overflow = tmp_path / "one-too-many"
        overflow.mkdir()
        assert folder_thumbnails.enqueue(str(overflow)) is False

    def test_releases_the_claim_when_submit_fails(self, tmp_path):
        executor = MagicMock()
        executor.submit.side_effect = RuntimeError("shutting down")
        with patch.object(folder_thumbnails, "_get_executor", return_value=executor):
            assert folder_thumbnails.enqueue(str(tmp_path)) is False

        # Rejected work must not leave the path wedged in the in-flight set, or
        # it could never be generated again for the life of the process.
        assert str(tmp_path) not in folder_thumbnails._queued


class TestFailureBackoff:

    def test_recent_failure_is_not_retried(self, fake_executor, tmp_path):
        folder_thumbnails._failed[str(tmp_path)] = time.monotonic()
        assert folder_thumbnails.enqueue(str(tmp_path)) is False
        fake_executor.submit.assert_not_called()

    def test_expired_failure_is_retried(self, fake_executor, tmp_path):
        folder_thumbnails._failed[str(tmp_path)] = (
            time.monotonic() - folder_thumbnails.FAILURE_TTL_SECONDS - 1
        )
        assert folder_thumbnails.enqueue(str(tmp_path)) is True
        assert str(tmp_path) not in folder_thumbnails._failed

    def test_success_clears_a_previous_failure(self, tmp_path):
        folder_thumbnails._failed[str(tmp_path)] = time.monotonic()
        folder_thumbnails._record_result(str(tmp_path), True)
        assert str(tmp_path) not in folder_thumbnails._failed

    def test_expired_records_are_pruned_once_the_ledger_grows(self):
        """A library of art-less folders must not grow the record without end."""
        stale = time.monotonic() - folder_thumbnails.FAILURE_TTL_SECONDS - 1
        for i in range(folder_thumbnails.MAX_FAILED_TRACKED + 1):
            folder_thumbnails._failed[f"/data/stale{i}"] = stale
        fresh = time.monotonic()
        folder_thumbnails._failed["/data/fresh"] = fresh

        folder_thumbnails._record_result("/data/trigger", False)

        assert "/data/stale0" not in folder_thumbnails._failed
        # Still-valid back-off entries survive the prune.
        assert "/data/fresh" in folder_thumbnails._failed
        assert "/data/trigger" in folder_thumbnails._failed

    def test_prune_leaves_unexpired_records_alone(self):
        fresh = time.monotonic()
        for i in range(folder_thumbnails.MAX_FAILED_TRACKED + 1):
            folder_thumbnails._failed[f"/data/recent{i}"] = fresh

        folder_thumbnails._record_result("/data/trigger", False)

        assert len(folder_thumbnails._failed) > folder_thumbnails.MAX_FAILED_TRACKED


class TestPreferenceGate:

    def test_disabled_preference_blocks_enqueue(self, fake_executor, tmp_path):
        with patch("core.database.get_user_preference", return_value=False):
            assert folder_thumbnails.enqueue(str(tmp_path)) is False
        fake_executor.submit.assert_not_called()

    def test_enabled_by_default(self, tmp_path):
        with patch("core.database.get_user_preference",
                   side_effect=lambda key, default=None: default):
            assert folder_thumbnails.is_enabled() is True

    def test_database_error_leaves_it_enabled(self):
        with patch("core.database.get_user_preference",
                   side_effect=Exception("no db")):
            assert folder_thumbnails.is_enabled() is True


class TestEnqueueMany:

    def test_counts_only_accepted_paths(self, fake_executor, tmp_path):
        real = tmp_path / "series"
        real.mkdir()
        paths = [str(real), str(tmp_path / "missing"), str(real)]
        assert folder_thumbnails.enqueue_many(paths) == 1

    def test_empty_list(self, fake_executor):
        assert folder_thumbnails.enqueue_many([]) == 0


class TestCreateNestedFolderThumbnail:
    """Geometry of the folder-of-folders composite.

    This art sits next to the plain fanned stack in the same grid row, so its
    dimensions are not cosmetic: they decide whether the two variants read as
    the same size on screen.
    """

    # The canvas generate_folder_thumbnail_internal renders the stack on, and
    # the size it saves the files-only variant at.
    STACK_CANVAS = (200, 300)

    @pytest.fixture
    def stack(self):
        """Stand-in for the fanned cover stack, at the size app.py renders it."""
        img = Image.new("RGBA", self.STACK_CANVAS, (0, 0, 0, 0))
        # Opaque block in the middle, mimicking covers on a transparent canvas.
        img.paste(Image.new("RGBA", (150, 245), (200, 30, 30, 255)), (25, 27))
        return img

    def test_matches_the_files_only_variant(self, stack):
        """The whole point: both folder variants save at identical dimensions.

        A downscale here is what made folder-of-folders art render ~20% smaller
        than a sibling folder-of-files card.
        """
        result = folder_thumbnails.create_nested_folder_thumbnail(stack, FOLDER_ICON)
        assert result.size == self.STACK_CANVAS

    def test_is_300px_tall(self, stack):
        """generate_thumbnail_sync normalizes every comic cover to 300px tall;
        folder art follows the same convention."""
        result = folder_thumbnails.create_nested_folder_thumbnail(stack, FOLDER_ICON)
        assert result.height == 300

    def test_honours_a_custom_canvas_size(self, stack):
        result = folder_thumbnails.create_nested_folder_thumbnail(
            stack, FOLDER_ICON, canvas_size=(400, 600)
        )
        assert result.size == (400, 600)

    def test_folder_glyph_is_not_clipped(self, stack):
        """The icon's art runs to y=297 of 300. Any resize that shortens the
        canvas would cut the bottom off the folder."""
        icon_bottom = Image.open(FOLDER_ICON).convert("RGBA").getchannel("A").getbbox()[3]
        result = folder_thumbnails.create_nested_folder_thumbnail(stack, FOLDER_ICON)

        assert result.getchannel("A").getbbox()[3] >= icon_bottom

    def test_result_is_two_by_three(self, stack):
        """The grid frame is aspect-ratio 2/3, so art at that ratio fills it
        under object-fit: cover without cropping anything."""
        result = folder_thumbnails.create_nested_folder_thumbnail(stack, FOLDER_ICON)
        assert result.width / result.height == pytest.approx(2 / 3, abs=0.01)

    def test_stack_shows_through_the_folder_glyph(self, stack):
        """The glyph is translucent (peak alpha 216), so covers pasted behind it
        must tint it. If the paste order flipped, the glyph would read flat."""
        icon = Image.open(FOLDER_ICON).convert("RGBA")
        glyph_box = icon.getchannel("A").getbbox()

        with_covers = folder_thumbnails.create_nested_folder_thumbnail(
            stack, FOLDER_ICON
        ).crop(glyph_box)
        without_covers = folder_thumbnails.create_nested_folder_thumbnail(
            Image.new("RGBA", self.STACK_CANVAS, (0, 0, 0, 0)), FOLDER_ICON
        ).crop(glyph_box)

        assert with_covers.tobytes() != without_covers.tobytes()

    def test_folder_glyph_is_drawn_over_the_stack(self, stack):
        """...and the glyph is still on top, not buried under the covers."""
        icon = Image.open(FOLDER_ICON).convert("RGBA")
        glyph_box = icon.getchannel("A").getbbox()

        result = folder_thumbnails.create_nested_folder_thumbnail(stack, FOLDER_ICON)

        # The same stack placed with no icon over it -- what the glyph region
        # would look like if the paste order were reversed.
        resized = stack.resize((190, int(190 * (stack.height / stack.width))),
                               Image.Resampling.LANCZOS)
        bare = Image.new("RGBA", self.STACK_CANVAS, (0, 0, 0, 0))
        bare.paste(resized, ((200 - 190) // 2, 300 - resized.height - 20), resized)

        assert result.crop(glyph_box).tobytes() != bare.crop(glyph_box).tobytes()

    def test_stack_is_visible_above_the_glyph(self, stack):
        """...but the covers must still show, or the composite is just an icon."""
        icon = Image.open(FOLDER_ICON).convert("RGBA")
        top_of_glyph = icon.getchannel("A").getbbox()[1]
        result = folder_thumbnails.create_nested_folder_thumbnail(stack, FOLDER_ICON)

        # Above the glyph, only the stack can be contributing opaque pixels.
        band = result.crop((0, 0, result.width, top_of_glyph))
        assert band.getchannel("A").getbbox() is not None

    def test_leaves_the_source_stack_untouched(self, stack):
        """The caller may keep using its canvas; compositing must not mutate it."""
        before = stack.tobytes()
        folder_thumbnails.create_nested_folder_thumbnail(stack, FOLDER_ICON)
        assert stack.tobytes() == before


class TestMayWrite:
    """The guard that stops auto-generation from clobbering uploaded art."""

    @pytest.mark.parametrize("existing", [
        "folder.png", "folder.jpg", "folder.jpeg", "folder.gif", "folder.webp",
    ])
    def test_refuses_when_art_exists(self, tmp_path, existing):
        (tmp_path / existing).write_bytes(b"image")
        assert folder_thumbnails.may_write(str(tmp_path), overwrite=False) is False

    def test_allows_when_folder_has_no_art(self, tmp_path):
        (tmp_path / "Batman 001.cbz").write_bytes(b"cbz")
        assert folder_thumbnails.may_write(str(tmp_path), overwrite=False) is True

    @pytest.mark.parametrize("existing", ["folder.png", "folder.gif"])
    def test_explicit_overwrite_replaces_existing_art(self, tmp_path, existing):
        """The menu action means "make me a new one" -- it may replace art."""
        (tmp_path / existing).write_bytes(b"image")
        assert folder_thumbnails.may_write(str(tmp_path), overwrite=True) is True

    def test_unrelated_images_are_not_folder_art(self, tmp_path):
        (tmp_path / "cover.png").write_bytes(b"image")
        (tmp_path / "header.jpg").write_bytes(b"image")
        assert folder_thumbnails.may_write(str(tmp_path), overwrite=False) is True


class TestWorker:

    def _run(self, folder, returns=True, raises=None):
        fake_app = MagicMock()
        if raises is not None:
            fake_app.generate_folder_thumbnail_internal.side_effect = raises
        else:
            fake_app.generate_folder_thumbnail_internal.return_value = returns
        with patch.dict("sys.modules", {"app": fake_app}):
            folder_thumbnails._generate(folder)
        return fake_app

    def test_never_overwrites_existing_art(self, tmp_path):
        fake_app = self._run(str(tmp_path))
        _, kwargs = fake_app.generate_folder_thumbnail_internal.call_args
        assert kwargs["overwrite"] is False

    def test_success_leaves_no_failure_record(self, tmp_path):
        self._run(str(tmp_path), returns=True)
        assert str(tmp_path) not in folder_thumbnails._queued
        assert str(tmp_path) not in folder_thumbnails._failed

    def test_failure_is_recorded_for_backoff(self, tmp_path):
        self._run(str(tmp_path), returns=False)
        assert str(tmp_path) in folder_thumbnails._failed

    def test_exception_is_swallowed_and_recorded(self, tmp_path):
        self._run(str(tmp_path), raises=OSError("disk gone"))
        assert str(tmp_path) in folder_thumbnails._failed
        assert str(tmp_path) not in folder_thumbnails._queued


class TestStylePreferences:
    """Resolving the two site-wide preferences.

    Both are read on every folder generated, including from the background
    worker, so neither may raise -- a database hiccup has to degrade to the
    default rather than leave a library without art.
    """

    def _pref(self, value):
        """Patch get_user_preference at its source, where the module imports it."""
        return patch("core.database.get_user_preference", return_value=value)

    @pytest.mark.parametrize("style", ["fanned", "single", "cascade", "mosaic"])
    def test_returns_a_registered_style(self, style):
        with self._pref(style):
            assert folder_thumbnails.get_style() == style

    def test_unknown_style_falls_back_to_the_default(self):
        """A hand-edited preference must not break every folder in the library."""
        with self._pref("spiral"):
            assert folder_thumbnails.get_style() == folder_thumbnails.DEFAULT_STYLE

    def test_missing_style_falls_back_to_the_default(self):
        with self._pref(None):
            assert folder_thumbnails.get_style() == folder_thumbnails.DEFAULT_STYLE

    def test_database_error_falls_back_to_the_default(self):
        with patch("core.database.get_user_preference", side_effect=OSError("no db")):
            assert folder_thumbnails.get_style() == folder_thumbnails.DEFAULT_STYLE

    def test_nested_overlay_defaults_to_on(self):
        """Off by accident would silently change how every publisher folder looks."""
        with patch("core.database.get_user_preference", side_effect=OSError("no db")):
            assert folder_thumbnails.nested_overlay_enabled() is True

    def test_nested_overlay_can_be_turned_off(self):
        with self._pref(False):
            assert folder_thumbnails.nested_overlay_enabled() is False


class TestSelectCoverFiles:

    def _comic(self, folder, name):
        path = folder / name
        path.write_bytes(b"cbz")
        return str(path)

    def test_flat_folder_uses_its_own_comics_in_order(self, tmp_path):
        for name in ("c.cbz", "a.cbz", "b.cbz"):
            self._comic(tmp_path, name)

        files, is_nested = folder_thumbnails.select_cover_files(str(tmp_path))

        assert is_nested is False
        assert [os.path.basename(f) for f in files] == ["a.cbz", "b.cbz", "c.cbz"]

    def test_respects_max_covers(self, tmp_path):
        for i in range(10):
            self._comic(tmp_path, f"issue{i}.cbz")

        files, _ = folder_thumbnails.select_cover_files(str(tmp_path), max_covers=1)

        assert len(files) == 1

    def test_ignores_non_comics_and_dotted_names(self, tmp_path):
        self._comic(tmp_path, "real.cbz")
        (tmp_path / "folder.png").write_bytes(b"img")
        (tmp_path / "ComicInfo.xml").write_bytes(b"xml")
        self._comic(tmp_path, "_hidden.cbz")
        self._comic(tmp_path, ".hidden.cbz")

        files, _ = folder_thumbnails.select_cover_files(str(tmp_path))

        assert [os.path.basename(f) for f in files] == ["real.cbz"]

    def test_nested_folder_samples_across_subfolders(self, tmp_path):
        """A publisher folder should show four series, not four issues of one."""
        for series in ("A", "B", "C", "D", "E"):
            sub = tmp_path / series
            sub.mkdir()
            for i in range(3):
                self._comic(sub, f"{series}{i}.cbz")

        files, is_nested = folder_thumbnails.select_cover_files(str(tmp_path))

        assert is_nested is True
        assert len(files) == 4
        # One from each of the first four series, and the first issue of each.
        assert [os.path.basename(f) for f in files] == ["A0.cbz", "B0.cbz", "C0.cbz", "D0.cbz"]

    def test_few_subfolders_share_the_slots(self, tmp_path):
        for series in ("A", "B"):
            sub = tmp_path / series
            sub.mkdir()
            for i in range(3):
                self._comic(sub, f"{series}{i}.cbz")

        files, _ = folder_thumbnails.select_cover_files(str(tmp_path))

        assert [os.path.basename(f) for f in files] == ["A0.cbz", "A1.cbz", "B0.cbz", "B1.cbz"]

    def test_empty_folder_yields_nothing(self, tmp_path):
        files, is_nested = folder_thumbnails.select_cover_files(str(tmp_path))
        assert files == []
        assert is_nested is True

    def test_folder_of_empty_subfolders_yields_nothing(self, tmp_path):
        (tmp_path / "empty").mkdir()
        files, _ = folder_thumbnails.select_cover_files(str(tmp_path))
        assert files == []
