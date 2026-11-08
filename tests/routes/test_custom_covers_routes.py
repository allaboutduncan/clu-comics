"""/api/custom-covers/* -- the folder menu's clu-custom-covers picker.

The index and the download are stubbed; core/custom_covers.py is tested on
its own in tests/unit/test_custom_covers.py.
"""

import os
from io import BytesIO
from unittest.mock import patch

import pytest
from PIL import Image

from core import custom_covers as cc

TREE = [
    {"type": "blob", "path": "DC Comics/folder.png", "sha": "d1"},
    {"type": "blob", "path": "DC Comics/header.jpg", "sha": "d2"},
    {"type": "blob", "path": "Marvel/Marvel.png", "sha": "m1"},
    {"type": "blob", "path": "Coffin Comics/folder.bmp", "sha": "c1"},
]


def _png():
    buf = BytesIO()
    Image.new("RGB", (4, 6), "red").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def index():
    idx = cc.build_index(TREE)
    with patch("core.custom_covers.get_index", return_value=idx):
        yield idx


@pytest.fixture
def offline():
    with patch("core.custom_covers.get_index", return_value=None):
        yield


@pytest.fixture
def lib_root(data_dir_library):
    from pathlib import Path

    return Path(data_dir_library)


@pytest.fixture
def library(lib_root):
    folder = lib_root / "DC Comics"
    folder.mkdir()
    return folder


class TestMatch:

    def test_matches_the_folder_name(self, client, index, library):
        body = client.get("/api/custom-covers/match",
                          query_string={"folder": str(library)}).get_json()
        assert body["available"] is True
        assert body["match"]["name"] == "DC Comics"
        assert body["match"]["covers"] == ["DC Comics/folder.png"]

    def test_no_match(self, client, index, lib_root):
        folder = lib_root / "Image"
        folder.mkdir()
        body = client.get("/api/custom-covers/match",
                          query_string={"folder": str(folder)}).get_json()
        assert body["match"] is None
        assert body["folder_name"] == "Image"

    def test_unavailable_is_503_not_no_match(self, client, offline, library):
        resp = client.get("/api/custom-covers/match", query_string={"folder": str(library)})
        assert resp.status_code == 503
        assert resp.get_json()["available"] is False

    def test_folder_outside_libraries_denied(self, client, index):
        resp = client.get("/api/custom-covers/match",
                          query_string={"folder": "/definitely/not/a/library"})
        assert resp.status_code == 403

    def test_missing_folder(self, client, index):
        assert client.get("/api/custom-covers/match").status_code == 400


class TestSearch:

    def test_search(self, client, index):
        body = client.get("/api/custom-covers/search", query_string={"q": "marv"}).get_json()
        assert [p["name"] for p in body["publishers"]] == ["Marvel"]

    def test_unavailable(self, client, offline):
        assert client.get("/api/custom-covers/search").status_code == 503

    def test_pages_until_next_offset_is_null(self, client, index):
        names, offset = [], 0
        while offset is not None:
            body = client.get("/api/custom-covers/search",
                              query_string={"offset": offset, "limit": 2}).get_json()
            assert body["total"] == len(index)
            names += [p["name"] for p in body["publishers"]]
            offset = body["next_offset"]
        assert sorted(names) == sorted(index)

    def test_limit_is_capped(self, client, index):
        with patch.object(cc, "MAX_SEARCH_RESULTS", 1):
            body = client.get("/api/custom-covers/search",
                              query_string={"limit": 500}).get_json()
        assert len(body["publishers"]) == 1
        assert body["next_offset"] == 1

    def test_bad_paging_params_fall_back(self, client, index):
        body = client.get("/api/custom-covers/search",
                          query_string={"offset": "x", "limit": "-3"}).get_json()
        assert body["publishers"]


class TestPreview:

    def test_proxies_an_indexed_image(self, client, index):
        with patch("core.custom_covers.download", return_value=(_png(), ".png")) as dl:
            resp = client.get("/api/custom-covers/preview",
                              query_string={"path": "DC Comics/folder.png"})
        assert resp.status_code == 200
        assert resp.content_type == "image/png"
        assert resp.data == _png()
        assert "max-age" in resp.headers["Cache-Control"]
        dl.assert_called_once()

    def test_unknown_path_is_never_fetched(self, client, index):
        with patch("core.custom_covers.download") as dl:
            resp = client.get("/api/custom-covers/preview",
                              query_string={"path": "https://evil.example/x.png"})
        assert resp.status_code == 404
        dl.assert_not_called()

    def test_download_failure_is_502(self, client, index):
        with patch("core.custom_covers.download",
                   side_effect=cc.CustomCoverError("Could not download the image from GitHub")):
            resp = client.get("/api/custom-covers/preview",
                              query_string={"path": "DC Comics/folder.png"})
        assert resp.status_code == 502


class TestApply:

    def _apply(self, client, folder, path):
        return client.post("/api/custom-covers/apply",
                           json={"folder": str(folder), "path": path})

    def test_stores_a_cover_in_the_cache(self, client, index, library):
        from core import folder_art

        with patch("core.custom_covers.download", return_value=(_png(), ".png")):
            body = self._apply(client, library, "DC Comics/folder.png").get_json()

        assert body["success"] is True
        assert body["kind"] == "folder"
        assert "kind=folder" in body["url"]
        rec = folder_art.get_art(str(library), "folder")
        assert rec["source"] == "custom"
        assert list(library.iterdir()) == [], "nothing is written into the library"

    def test_a_header_file_becomes_the_header(self, client, index, library):
        from core import folder_art

        with patch("core.custom_covers.download", return_value=(_png(), ".jpg")):
            body = self._apply(client, library, "DC Comics/header.jpg").get_json()
        assert body["kind"] == "header"
        assert folder_art.get_art(str(library), "header")
        assert folder_art.get_art(str(library), "folder") is None

    def test_replaces_uploaded_art(self, client, index, library):
        from core import folder_art

        folder_art.put_art(str(library), "folder", _png(), ".png", "uploaded")
        with patch("core.custom_covers.download", return_value=(_png(), ".png")):
            self._apply(client, library, "DC Comics/folder.png")
        assert folder_art.get_art(str(library), "folder")["source"] == "custom"

    def test_any_publisher_can_be_applied_to_any_folder(self, client, index, library):
        with patch("core.custom_covers.download", return_value=(_png(), ".png")):
            resp = self._apply(client, library, "Marvel/Marvel.png")
        assert resp.status_code == 200

    def test_unknown_path_is_refused_before_downloading(self, client, index, library):
        with patch("core.custom_covers.download") as dl:
            resp = self._apply(client, library, "../../config/comic_utils.db")
        assert resp.status_code == 400
        dl.assert_not_called()

    def test_an_extension_the_kind_cannot_take(self, client, index, lib_root):
        folder = lib_root / "Coffin Comics"
        folder.mkdir()
        with patch("core.custom_covers.download", return_value=(_png(), ".bmp")):
            resp = self._apply(client, folder, "Coffin Comics/folder.bmp")
        # .bmp is not an image extension the index takes at all.
        assert resp.status_code == 400

    def test_folder_outside_libraries_denied(self, client, index):
        with patch("core.custom_covers.download") as dl:
            resp = self._apply(client, "/definitely/not/a/library", "DC Comics/folder.png")
        assert resp.status_code == 403
        dl.assert_not_called()

    def test_missing_folder_is_404(self, client, index, lib_root):
        resp = self._apply(client, lib_root / "Gone", "DC Comics/folder.png")
        assert resp.status_code == 404

    def test_download_failure_is_reported(self, client, index, library):
        with patch("core.custom_covers.download",
                   side_effect=cc.CustomCoverError("The downloaded file is not a readable image")):
            resp = self._apply(client, library, "DC Comics/folder.png")
        assert resp.status_code == 502
        assert "readable image" in resp.get_json()["error"]

    def test_unavailable(self, client, offline, library):
        assert self._apply(client, library, "DC Comics/folder.png").status_code == 503


class TestPickerWiring:
    """The picker is JavaScript; pin the pieces a refactor could drop."""

    def _read(self, rel):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_menu_item_in_both_folder_menus(self):
        js = self._read("static/js/collection.js")
        assert js.count('folder-action-custom-cover') == 3  # two menus + one binding

    def test_modal_is_on_the_page(self, client):
        html = client.get("/collection").get_data(as_text=True)
        assert 'id="customCoverModal"' in html

    def test_grid_loads_more_on_scroll(self):
        js = self._read("static/js/collection.js")
        assert "IntersectionObserver" in js
        assert "next_offset" in js
        assert 'id="customCoverGrid"' in js

    def test_previews_go_through_the_proxy(self):
        js = self._read("static/js/collection.js")
        assert "/api/custom-covers/preview?path=" in js
        assert "raw.githubusercontent.com" not in js
