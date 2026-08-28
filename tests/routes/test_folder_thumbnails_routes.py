"""Tests for the folder-thumbnail endpoints in routes/collection.py.

Covers three things that are easy to get wrong:

* the pin round-trip, including that /api/browse reports it back (the grid
  needs it to label the menu item "Set" vs "Unset");
* the style-settings endpoint, which must reject an unknown style rather than
  storing it and breaking every folder;
* the two sweeps, which differ only in whether they replace existing art --
  "regenerate all" is destructive and must not silently behave like "missing".

The sweeps run on a background thread, so these patch the generator and join
the worker rather than rendering real images.
"""

import os
import time
from unittest.mock import patch

import pytest

from core.database import get_folder_pin, set_folder_pin


def _wait_for_operations(timeout=5.0):
    """Block until no thumbnail operation is still running."""
    import core.app_state as app_state

    deadline = time.time() + timeout
    while time.time() < deadline:
        running = [
            op for op in app_state.get_active_operations()
            if op["op_type"] == "thumbnails" and op["status"] == "running"
        ]
        if not running:
            return
        time.sleep(0.02)
    raise AssertionError("thumbnail sweep did not finish")


@pytest.fixture
def library(tmp_path):
    """A small library: two series folders, each with one comic."""
    root = tmp_path / "data"
    for series in ("Batman", "Superman"):
        folder = root / series
        folder.mkdir(parents=True)
        (folder / f"{series} 001.cbz").write_bytes(b"not a real cbz")
    return root


class TestPinFolderThumbnail:

    def test_pins_and_regenerates(self, client, library):
        comic = str(library / "Batman" / "Batman 001.cbz")

        with patch("app.generate_folder_thumbnail_internal", return_value=True) as gen:
            response = client.post(
                "/api/folder-thumbnail/pin", json={"comic_path": comic}
            )

        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert body["folder_path"] == str(library / "Batman")

        # The art is rebuilt before replying, so the caller can show it at once.
        gen.assert_called_once_with(str(library / "Batman"), overwrite=True)
        assert get_folder_pin(str(library / "Batman")) == comic

    def test_unpin_clears_the_pin_and_regenerates(self, client, library):
        folder = str(library / "Batman")
        comic = str(library / "Batman" / "Batman 001.cbz")
        set_folder_pin(folder, comic)

        with patch("app.generate_folder_thumbnail_internal", return_value=True) as gen:
            response = client.post(
                "/api/folder-thumbnail/unpin", json={"folder_path": folder}
            )

        assert response.status_code == 200
        assert response.get_json()["success"] is True
        gen.assert_called_once_with(folder, overwrite=True)
        assert get_folder_pin(folder) is None

    def test_unpin_accepts_a_comic_path(self, client, library):
        """The grid knows the file it is un-pinning, not always the folder."""
        folder = str(library / "Batman")
        comic = str(library / "Batman" / "Batman 001.cbz")
        set_folder_pin(folder, comic)

        with patch("app.generate_folder_thumbnail_internal", return_value=True):
            response = client.post(
                "/api/folder-thumbnail/unpin", json={"comic_path": comic}
            )

        assert response.status_code == 200
        assert get_folder_pin(folder) is None

    def test_rejects_a_path_that_is_not_a_file(self, client, library):
        with patch("app.generate_folder_thumbnail_internal") as gen:
            response = client.post(
                "/api/folder-thumbnail/pin", json={"comic_path": str(library / "Batman")}
            )

        assert response.status_code == 400
        gen.assert_not_called()

    def test_rejects_a_file_that_is_not_a_comic(self, client, library):
        """A pinned .txt occupies a cover slot forever and renders nothing.

        In Single Image mode it is the *only* slot, so the folder would lose
        the ability to build art at all.
        """
        note = library / "Batman" / "notes.txt"
        note.write_text("read order")

        with patch("app.generate_folder_thumbnail_internal") as gen:
            response = client.post(
                "/api/folder-thumbnail/pin", json={"comic_path": str(note)}
            )

        assert response.status_code == 400
        gen.assert_not_called()
        assert get_folder_pin(str(library / "Batman")) is None

    @pytest.mark.parametrize("ext", [".cbz", ".cbr", ".zip", ".CBZ"])
    def test_accepts_every_comic_extension(self, client, library, ext):
        comic = library / "Batman" / f"issue{ext}"
        comic.write_bytes(b"comic")

        with patch("app.generate_folder_thumbnail_internal", return_value=True):
            response = client.post(
                "/api/folder-thumbnail/pin", json={"comic_path": str(comic)}
            )

        assert response.status_code == 200

    def test_rejects_a_missing_body(self, client):
        assert client.post("/api/folder-thumbnail/pin", json={}).status_code == 400
        assert client.post("/api/folder-thumbnail/unpin", json={}).status_code == 400

    def test_a_failed_regeneration_still_keeps_the_pin(self, client, library):
        """A folder with no renderable covers is not a reason to lose the choice."""
        comic = str(library / "Batman" / "Batman 001.cbz")

        with patch("app.generate_folder_thumbnail_internal", return_value=False):
            response = client.post(
                "/api/folder-thumbnail/pin", json={"comic_path": comic}
            )

        assert response.status_code == 200
        assert response.get_json()["thumbnail_url"] is None
        assert get_folder_pin(str(library / "Batman")) == comic


class TestPinnedCoverInBrowse:

    def test_browse_reports_the_pin(self, client, library):
        folder = str(library / "Batman")
        comic = str(library / "Batman" / "Batman 001.cbz")
        set_folder_pin(folder, comic)

        response = client.get(f"/api/browse?path={folder}")

        assert response.status_code == 200
        assert response.get_json()["pinned_cover"] == comic

    def test_browse_reports_none_when_unpinned(self, client, library):
        response = client.get(f"/api/browse?path={library / 'Superman'}")

        assert response.status_code == 200
        assert response.get_json()["pinned_cover"] is None


class TestThumbnailConfig:

    def test_persists_style_and_overlay(self, client):
        with patch("core.database.set_user_preference") as save:
            response = client.post("/api/config/thumbnails", json={
                "folderThumbnailStyle": "mosaic",
                "folderThumbnailNestedOverlay": False,
            })

        assert response.status_code == 200
        assert response.get_json()["success"] is True

        stored = {call.args[0]: call.args[1] for call in save.call_args_list}
        assert stored["folder_thumbnail_style"] == "mosaic"
        assert stored["folder_thumbnail_nested_overlay"] is False

    @pytest.mark.parametrize("style", ["fanned", "single", "cascade", "mosaic"])
    def test_accepts_every_registered_style(self, client, style):
        with patch("core.database.set_user_preference"):
            response = client.post(
                "/api/config/thumbnails", json={"folderThumbnailStyle": style}
            )
        assert response.status_code == 200

    def test_rejects_an_unknown_style(self, client):
        """Storing junk here would break folder art site-wide, not just once."""
        with patch("core.database.set_user_preference") as save:
            response = client.post(
                "/api/config/thumbnails", json={"folderThumbnailStyle": "spiral"}
            )

        assert response.status_code == 400
        assert response.get_json()["success"] is False
        save.assert_not_called()

    def test_rejects_an_empty_body(self, client):
        assert client.post("/api/config/thumbnails", json={}).status_code == 400

    def test_overlay_defaults_to_on(self, client):
        with patch("core.database.set_user_preference") as save:
            client.post("/api/config/thumbnails", json={"folderThumbnailStyle": "fanned"})

        stored = {call.args[0]: call.args[1] for call in save.call_args_list}
        assert stored["folder_thumbnail_nested_overlay"] is True


class TestThumbnailSweeps:

    def test_generate_missing_skips_folders_that_have_art(self, client, library):
        (library / "Batman" / "folder.png").write_bytes(b"existing")

        with patch("app.generate_folder_thumbnail_internal", return_value=True) as gen:
            response = client.post(
                "/api/generate-all-missing-thumbnails", json={"path": str(library)}
            )
            assert response.status_code == 200
            _wait_for_operations()

        touched = {call.args[0] for call in gen.call_args_list}
        assert str(library / "Batman") not in touched
        assert str(library / "Superman") in touched

    def test_generate_missing_recognises_every_art_extension(self, client, library):
        """A folder.webp counts as art, even though we only ever write .png.

        The old inline extension list omitted it, so a folder with uploaded
        webp art was regenerated on every sweep.
        """
        (library / "Batman" / "folder.webp").write_bytes(b"existing")

        with patch("app.generate_folder_thumbnail_internal", return_value=True) as gen:
            client.post(
                "/api/generate-all-missing-thumbnails", json={"path": str(library)}
            )
            _wait_for_operations()

        touched = {call.args[0] for call in gen.call_args_list}
        assert str(library / "Batman") not in touched

    def test_regenerate_all_replaces_existing_art(self, client, library):
        (library / "Batman" / "folder.png").write_bytes(b"existing")

        with patch("app.generate_folder_thumbnail_internal", return_value=True) as gen:
            response = client.post(
                "/api/regenerate-all-thumbnails", json={"path": str(library)}
            )
            assert response.status_code == 200
            _wait_for_operations()

        touched = {call.args[0] for call in gen.call_args_list}
        assert touched == {str(library / "Batman"), str(library / "Superman")}
        # Never overwrite=False: this sweep exists to replace what is there.
        assert all(c.kwargs["overwrite"] is True for c in gen.call_args_list)

    def test_returns_an_operation_id_to_poll(self, client, library):
        """The work outlasts the request, so the reply is a handle, not a count."""
        with patch("app.generate_folder_thumbnail_internal", return_value=True):
            response = client.post(
                "/api/regenerate-all-thumbnails", json={"path": str(library)}
            )
            body = response.get_json()
            assert body["operation_id"]
            assert body["total"] == 2
            _wait_for_operations()

    def test_the_root_itself_is_never_given_art(self, client, library):
        """A library root holds series folders; it is not a series."""
        with patch("app.generate_folder_thumbnail_internal", return_value=True) as gen:
            client.post("/api/regenerate-all-thumbnails", json={"path": str(library)})
            _wait_for_operations()

        assert str(library) not in {call.args[0] for call in gen.call_args_list}

    def test_the_invoked_folder_keeps_its_own_uploaded_art(self, client, tmp_path):
        """Running this on a publisher folder rebuilds what's inside it, not it.

        /data/DC Comics may carry a hand-uploaded publisher image while the user
        wants every series *below* it restyled. The modal promises this, so the
        exclusion is behaviour, not an implementation detail.
        """
        dc = tmp_path / "data" / "DC Comics"
        (dc / "Batman").mkdir(parents=True)
        (dc / "Superman").mkdir()
        uploaded = dc / "folder.png"
        uploaded.write_bytes(b"USER UPLOADED")

        with patch("app.generate_folder_thumbnail_internal", return_value=True) as gen:
            response = client.post(
                "/api/regenerate-all-thumbnails", json={"path": str(dc)}
            )
            assert response.status_code == 200
            _wait_for_operations()

        touched = {call.args[0] for call in gen.call_args_list}
        assert str(dc) not in touched, "the folder the sweep was run on must be left alone"
        assert touched == {str(dc / "Batman"), str(dc / "Superman")}
        # Nothing went near the file itself.
        assert uploaded.read_bytes() == b"USER UPLOADED"

    def test_generate_missing_also_leaves_the_invoked_folder_alone(self, client, tmp_path):
        """Same rule for the non-destructive sweep, so the two stay consistent."""
        dc = tmp_path / "data" / "DC Comics"
        (dc / "Batman").mkdir(parents=True)

        with patch("app.generate_folder_thumbnail_internal", return_value=True) as gen:
            client.post(
                "/api/generate-all-missing-thumbnails", json={"path": str(dc)}
            )
            _wait_for_operations()

        assert str(dc) not in {call.args[0] for call in gen.call_args_list}

    def test_hidden_folders_are_skipped(self, client, library):
        (library / "_junk").mkdir()
        (library / ".hidden").mkdir()

        with patch("app.generate_folder_thumbnail_internal", return_value=True) as gen:
            client.post("/api/regenerate-all-thumbnails", json={"path": str(library)})
            _wait_for_operations()

        touched = {call.args[0] for call in gen.call_args_list}
        assert str(library / "_junk") not in touched
        assert str(library / ".hidden") not in touched

    def test_rejects_an_invalid_path(self, client, tmp_path):
        response = client.post(
            "/api/regenerate-all-thumbnails", json={"path": str(tmp_path / "nope")}
        )
        assert response.status_code == 400

    def test_an_empty_library_reports_nothing_to_do(self, client, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()

        response = client.post(
            "/api/regenerate-all-thumbnails", json={"path": str(empty)}
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert body["operation_id"] is None
        assert body["total"] == 0

    def test_one_failing_folder_does_not_abort_the_sweep(self, client, library):
        def flaky(path, overwrite=True):
            if path.endswith("Batman"):
                raise OSError("disk hiccup")
            return True

        with patch("app.generate_folder_thumbnail_internal", side_effect=flaky) as gen:
            client.post("/api/regenerate-all-thumbnails", json={"path": str(library)})
            _wait_for_operations()

        touched = {call.args[0] for call in gen.call_args_list}
        assert str(library / "Superman") in touched
