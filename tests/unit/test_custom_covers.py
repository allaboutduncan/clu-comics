"""core/custom_covers.py: the clu-custom-covers index, matching and download guard.

GitHub is never contacted: ``requests.get`` is patched in every test that
would reach it.
"""

import json
import os
import time
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from core import custom_covers as cc


def _png():
    buf = BytesIO()
    Image.new("RGB", (4, 6), "red").save(buf, format="PNG")
    return buf.getvalue()


# A slice of the real repository's shape (paths as GitHub lists them).
TREE = [
    {"type": "blob", "path": ".gitattributes", "sha": "g"},
    {"type": "blob", "path": "folder.png", "sha": "root"},
    {"type": "blob", "path": "template.psd", "sha": "psd"},
    {"type": "tree", "path": "Marvel", "sha": "t1"},
    {"type": "blob", "path": "Marvel/.codex-cover.png", "sha": "m"},
    {"type": "blob", "path": "Marvel/Marvel.png", "sha": "m"},  # identical bytes
    {"type": "blob", "path": "DC Comics/.codex-cover.png", "sha": "d1"},
    {"type": "blob", "path": "DC Comics/DC Comics.png", "sha": "d2"},
    {"type": "blob", "path": "DC Comics/folder.png", "sha": "d3"},
    {"type": "blob", "path": "DC Comics/header.jpg", "sha": "d4"},
    {"type": "blob", "path": "Devil_s Due/folder.png", "sha": "dd"},
    {"type": "blob", "path": "Valiant/v-header.png", "sha": "v1"},
    {"type": "blob", "path": "Valiant/folder.png", "sha": "v2"},
    {"type": "blob", "path": "Amerotica/folder.jpg", "sha": "a"},
    {"type": "blob", "path": "Notes/readme.txt", "sha": "n"},
    {"type": "blob", "path": "Deep/nested/folder.png", "sha": "x"},
]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "get_cache_dir", lambda: str(tmp_path / "cache"))
    cc.reset_state()
    yield
    cc.reset_state()


def _tree_response(tree=TREE):
    resp = MagicMock()
    resp.json.return_value = {"tree": tree}
    resp.raise_for_status.return_value = None
    return resp


class TestBuildIndex:

    def test_only_publisher_level_images(self):
        index = cc.build_index(TREE)
        assert set(index) == {"Marvel", "DC Comics", "Devil_s Due", "Valiant", "Amerotica"}

    def test_cover_order_folder_then_publisher_then_codex(self):
        assert cc.build_index(TREE)["DC Comics"]["covers"] == [
            "DC Comics/folder.png", "DC Comics/DC Comics.png", "DC Comics/.codex-cover.png",
        ]

    def test_identical_blobs_listed_once_under_the_better_name(self):
        assert cc.build_index(TREE)["Marvel"]["covers"] == ["Marvel/Marvel.png"]

    def test_headers_are_separated(self):
        index = cc.build_index(TREE)
        assert index["DC Comics"]["headers"] == ["DC Comics/header.jpg"]
        assert index["Valiant"]["headers"] == ["Valiant/v-header.png"]
        assert "DC Comics/header.jpg" not in index["DC Comics"]["covers"]


class TestMatching:

    @pytest.mark.parametrize("folder,publisher", [
        ("Marvel", "Marvel"),
        ("marvel", "Marvel"),
        ("DC Comics", "DC Comics"),
        ("Devil's Due", "Devil_s Due"),
        ("The Marvel", "Marvel"),
    ])
    def test_match(self, folder, publisher):
        assert cc.match(folder, cc.build_index(TREE))["name"] == publisher

    def test_no_match(self):
        assert cc.match("Image", cc.build_index(TREE)) is None

    def test_search_prefix_first(self):
        index = cc.build_index(TREE)
        names = [e["name"] for e in cc.search("d", index)]
        assert names[:2] == ["DC Comics", "Devil_s Due"]

    def test_empty_search_lists_everything(self):
        index = cc.build_index(TREE)
        assert len(cc.search("", index)) == len(index)

    def test_pages_cover_every_publisher_once(self):
        index = cc.build_index(TREE)
        seen, offset = [], 0
        while True:
            page, total = cc.search_page("", index, offset, 2)
            assert total == len(index)
            if not page:
                break
            seen += [e["name"] for e in page]
            offset += len(page)
        assert sorted(seen) == sorted(index)
        assert len(seen) == len(set(seen))

    def test_page_past_the_end_is_empty(self):
        index = cc.build_index(TREE)
        assert cc.search_page("", index, 999, 10) == ([], len(index))


class TestIndexCache:

    def test_fetches_once_then_serves_memory(self):
        with patch("core.custom_covers.requests.get", return_value=_tree_response()) as get:
            cc.get_index()
            cc.get_index()
        assert get.call_count == 1

    def test_disk_cache_survives_a_restart(self):
        with patch("core.custom_covers.requests.get", return_value=_tree_response()):
            cc.get_index()
        cc.reset_state()
        with patch("core.custom_covers.requests.get", side_effect=AssertionError("refetched")):
            assert "Marvel" in cc.get_index()

    def test_stale_index_is_refreshed(self):
        with patch("core.custom_covers.requests.get", return_value=_tree_response()):
            cc.get_index()
        later = time.time() + cc.INDEX_TTL_SECONDS + 5
        with patch("core.custom_covers.time.time", return_value=later), \
                patch("core.custom_covers.requests.get",
                      return_value=_tree_response(TREE[:5])) as get:
            index = cc.get_index()
        assert get.call_count == 1
        assert set(index) == {"Marvel"}

    def test_a_failed_refresh_serves_the_stale_copy(self):
        with patch("core.custom_covers.requests.get", return_value=_tree_response()):
            cc.get_index()
        later = time.time() + cc.INDEX_TTL_SECONDS + 5
        with patch("core.custom_covers.time.time", return_value=later), \
                patch("core.custom_covers.requests.get", side_effect=OSError("offline")):
            assert "Marvel" in cc.get_index()

    def test_never_fetched_and_offline_is_none_not_empty(self):
        with patch("core.custom_covers.requests.get", side_effect=OSError("offline")):
            assert cc.get_index() is None

    def test_corrupt_disk_cache_is_ignored(self, tmp_path):
        path = tmp_path / "cache" / "custom_covers_index.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        with patch("core.custom_covers.requests.get", return_value=_tree_response()):
            assert "Marvel" in cc.get_index()
        assert json.loads(path.read_text())["index"]


class TestDownload:

    def _stream(self, body):
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        resp.raise_for_status.return_value = None
        resp.iter_content.return_value = [body[i:i + 1000] for i in range(0, len(body), 1000)]
        return resp

    def test_refuses_a_path_not_in_the_index(self):
        index = cc.build_index(TREE)
        with patch("core.custom_covers.requests.get") as get:
            for bad in ("../../etc/passwd", "folder.png", "Marvel/missing.png",
                        "https://evil.example/x.png", "Notes/readme.txt"):
                with pytest.raises(cc.CustomCoverError):
                    cc.download(bad, index)
        get.assert_not_called()

    def test_builds_the_raw_url_itself(self):
        index = cc.build_index(TREE)
        with patch("core.custom_covers.requests.get", return_value=self._stream(_png())) as get:
            data, ext = cc.download("DC Comics/folder.png", index)
        assert ext == ".png"
        assert data == _png()
        url = get.call_args[0][0]
        assert url == ("https://raw.githubusercontent.com/allaboutduncan/"
                       "clu-custom-covers/HEAD/DC%20Comics/folder.png")

    def test_oversize_is_refused(self):
        index = cc.build_index(TREE)
        big = b"x" * (cc.MAX_IMAGE_BYTES + 10)
        with patch("core.custom_covers.requests.get", return_value=self._stream(big)):
            with pytest.raises(cc.CustomCoverError, match="too large"):
                cc.download("DC Comics/folder.png", index)

    def test_non_image_is_refused(self):
        index = cc.build_index(TREE)
        with patch("core.custom_covers.requests.get",
                   return_value=self._stream(b"<html>rate limited</html>")):
            with pytest.raises(cc.CustomCoverError, match="not a readable image"):
                cc.download("DC Comics/folder.png", index)

    def test_network_failure_is_a_user_message(self):
        index = cc.build_index(TREE)
        with patch("core.custom_covers.requests.get", side_effect=OSError("boom")):
            with pytest.raises(cc.CustomCoverError, match="Could not download"):
                cc.download("DC Comics/folder.png", index)


class TestKindOf:

    def test_kinds(self):
        index = cc.build_index(TREE)
        assert cc.kind_of("DC Comics/header.jpg", index) == "header"
        assert cc.kind_of("DC Comics/folder.png", index) == "folder"
        assert cc.kind_of("DC Comics/nope.png", index) is None
