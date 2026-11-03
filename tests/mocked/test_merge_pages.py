"""Tests for cbz_ops/merge_pages.py -- build one CBZ from pages of two copies."""
import io
import os
import shutil
import zipfile
from unittest.mock import patch

import pytest


def _png(size=(50, 75), color=(100, 100, 200)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _jpg(size=(50, 75)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color=(10, 200, 10)).save(buf, format="JPEG")
    return buf.getvalue()


def _make_cbz(path, names, size=(50, 75), comicinfo=None, extra=None):
    with zipfile.ZipFile(str(path), "w") as zf:
        for n in names:
            zf.writestr(n, _jpg(size) if n.lower().endswith(".jpg") else _png(size))
        if comicinfo is not None:
            zf.writestr("ComicInfo.xml", comicinfo)
        for n, data in (extra or {}).items():
            zf.writestr(n, data)
    return str(path)


@pytest.fixture
def trash(tmp_path):
    """Stand-in for move_to_trash: moves into tmp_path/trash and records it."""
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    calls = []

    def fake(src):
        calls.append(src)
        shutil.move(src, str(trash_dir / os.path.basename(src)))
        return {"trashed": True, "path": str(trash_dir / os.path.basename(src))}

    with patch("cbz_ops.merge_pages.move_to_trash", side_effect=fake):
        yield trash_dir, calls


@pytest.fixture
def pair(tmp_path):
    """Base: 3 flat, larger pages. Donor: 4 smaller pages inside a wrapper folder."""
    a_dir = tmp_path / "downloads"
    b_dir = tmp_path / "library"
    a_dir.mkdir()
    b_dir.mkdir()
    base = _make_cbz(a_dir / "Batman 041 (1947).cbz",
                     ["Batman 041 01.jpg", "Batman 041 02.jpg", "Batman 041 10.jpg"],
                     size=(100, 150), comicinfo=b"<ComicInfo><Series>Base</Series></ComicInfo>")
    donor = _make_cbz(b_dir / "Batman 041 (1947).cbz",
                      [f"Batman 041 52p/batman 041 00{i}.jpg" for i in range(1, 5)],
                      size=(80, 120), comicinfo=b"<ComicInfo><Series>Donor</Series></ComicInfo>")
    return base, donor


class TestListPages:

    def test_natural_order_and_dimensions(self, pair):
        from cbz_ops.merge_pages import list_pages
        info = list_pages(pair[0])
        assert [p["basename"] for p in info["pages"]] == [
            "Batman 041 01.jpg", "Batman 041 02.jpg", "Batman 041 10.jpg"]
        assert info["pages"][0]["width"] == 100 and info["pages"][0]["height"] == 150
        assert info["median"] == [100, 150]
        assert info["has_comicinfo"] is True
        assert info["page_count"] == 3

    def test_nested_entries_keep_full_name(self, pair):
        from cbz_ops.merge_pages import list_pages
        info = list_pages(pair[1])
        assert info["pages"][1]["entry"] == "Batman 041 52p/batman 041 002.jpg"
        assert info["pages"][1]["basename"] == "batman 041 002.jpg"

    def test_skips_non_pages(self, tmp_path):
        from cbz_ops.merge_pages import list_pages
        cbz = _make_cbz(tmp_path / "x.cbz", ["p1.png"], extra={
            "__MACOSX/._p1.png": b"x", "._p2.png": b"x", "notes.txt": b"x", "dir/": b""})
        info = list_pages(cbz)
        assert [p["entry"] for p in info["pages"]] == ["p1.png"]
        assert info["has_comicinfo"] is False


class TestRenderPageThumb:

    def test_unknown_entry_raises(self, pair):
        from cbz_ops.merge_pages import render_page_thumb
        with pytest.raises(KeyError):
            render_page_thumb(pair[0], "../../etc/passwd")

    def test_returns_jpeg(self, pair):
        from cbz_ops.merge_pages import render_page_thumb
        data = render_page_thumb(pair[1], "Batman 041 52p/batman 041 002.jpg")
        assert data[:2] == b"\xff\xd8"


class TestPageNames:

    def test_width_grows_past_999(self):
        from cbz_ops.merge_pages import page_names
        assert page_names("X", [".jpg"] * 2) == ["X 001.jpg", "X 002.jpg"]
        assert page_names("X", [".png"] * 1000)[-1] == "X 1000.png"


class TestCommitMerge:

    def _pages(self):
        return [
            {"src": "base", "entry": "Batman 041 01.jpg"},
            {"src": "donor", "entry": "Batman 041 52p/batman 041 002.jpg"},
            {"src": "base", "entry": "Batman 041 10.jpg"},
        ]

    def test_flat_sequential_in_chosen_order(self, pair, trash):
        from cbz_ops.merge_pages import commit_merge
        base, donor = pair
        result = commit_merge(base, donor, self._pages(), donor)

        with zipfile.ZipFile(donor) as zf:
            names = zf.namelist()
            assert names == ["Batman 041 (1947) 001.jpg", "Batman 041 (1947) 002.jpg",
                             "Batman 041 (1947) 003.jpg", "ComicInfo.xml"]
            assert not any("/" in n for n in names)
            # Page 2 came from the donor: its pixels are the donor's size.
            from PIL import Image
            with zf.open(names[1]) as fp, Image.open(fp) as img:
                assert img.size == (80, 120)
            assert b"Base" in zf.read("ComicInfo.xml")
        assert result["page_count"] == 3
        assert result["comicinfo"] is True
        trash_dir, calls = trash
        assert calls == [donor]
        assert os.path.isfile(base)                       # the other copy is untouched
        assert len(zipfile.ZipFile(str(trash_dir / os.path.basename(donor))).namelist()) == 5

    def test_target_can_be_base(self, pair, trash):
        from cbz_ops.merge_pages import commit_merge
        base, donor = pair
        commit_merge(base, donor, self._pages(), base)
        with zipfile.ZipFile(base) as zf:
            assert len([n for n in zf.namelist() if n.endswith(".jpg")]) == 3
        assert trash[1] == [base]

    def test_donor_comicinfo_when_base_has_none(self, tmp_path, trash):
        from cbz_ops.merge_pages import commit_merge
        base = _make_cbz(tmp_path / "a.cbz", ["1.png"])
        donor = _make_cbz(tmp_path / "b.cbz", ["1.png"], comicinfo=b"<ComicInfo>D</ComicInfo>")
        commit_merge(base, donor, [{"src": "base", "entry": "1.png"}], base)
        with zipfile.ZipFile(base) as zf:
            assert zf.read("ComicInfo.xml") == b"<ComicInfo>D</ComicInfo>"
            assert zf.namelist()[0] == "a 001.png"

    def test_no_comicinfo_when_neither_has_one(self, tmp_path, trash):
        from cbz_ops.merge_pages import commit_merge
        base = _make_cbz(tmp_path / "a.cbz", ["1.png"])
        donor = _make_cbz(tmp_path / "b.cbz", ["1.png"])
        result = commit_merge(base, donor, [{"src": "donor", "entry": "1.png"}], base)
        assert result["comicinfo"] is False
        with zipfile.ZipFile(base) as zf:
            assert zf.namelist() == ["a 001.png"]

    @pytest.mark.parametrize("pages", [
        [{"src": "base", "entry": "missing.jpg"}],
        [{"src": "other", "entry": "Batman 041 01.jpg"}],
        [{"src": "base", "entry": "ComicInfo.xml"}],
        [],
    ])
    def test_bad_pages_rejected_before_writing(self, pair, trash, pages):
        from cbz_ops.merge_pages import commit_merge
        base, donor = pair
        before = open(donor, "rb").read()
        with pytest.raises(ValueError):
            commit_merge(base, donor, pages, donor)
        assert open(donor, "rb").read() == before
        assert trash[1] == []
        assert not [n for n in os.listdir(os.path.dirname(donor)) if n.startswith(".clu_merge_")]

    def test_target_must_be_a_source(self, pair, trash, tmp_path):
        from cbz_ops.merge_pages import commit_merge
        with pytest.raises(ValueError):
            commit_merge(pair[0], pair[1], self._pages(), str(tmp_path / "other.cbz"))

    def test_same_file_rejected(self, pair, trash):
        from cbz_ops.merge_pages import commit_merge
        with pytest.raises(ValueError):
            commit_merge(pair[0], pair[0], self._pages(), pair[0])

    def test_write_failure_leaves_target_untouched(self, pair, trash):
        from cbz_ops import merge_pages
        base, donor = pair
        before = open(donor, "rb").read()
        with patch.object(merge_pages, "open_zip_for_write", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                merge_pages.commit_merge(base, donor, self._pages(), donor)
        assert open(donor, "rb").read() == before
        assert trash[1] == []

    def test_trash_failure_leaves_target_and_no_staging(self, pair):
        from cbz_ops import merge_pages
        base, donor = pair
        before = open(donor, "rb").read()
        with patch.object(merge_pages, "move_to_trash", side_effect=OSError("denied")):
            with pytest.raises(OSError):
                merge_pages.commit_merge(base, donor, self._pages(), donor)
        assert open(donor, "rb").read() == before
        assert not [n for n in os.listdir(os.path.dirname(donor)) if n.startswith(".clu_merge_")]
