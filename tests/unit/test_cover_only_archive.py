"""helpers.archive_content: is this archive a comic, or only its covers? (#794)

Built from real zips in tmp_path. The archives sit in a neutral sub-folder
because the wrapper folder's name is part of the evidence, and pytest names
tmp_path after the test.
"""
import zipfile

import pytest

from helpers import archive_content
from helpers.archive_content import cover_only_reason, judge_members


@pytest.fixture(autouse=True)
def _fresh_memo(monkeypatch):
    monkeypatch.setattr(archive_content, "_verdicts", {})


@pytest.fixture
def incoming(tmp_path):
    d = tmp_path / "incoming"
    d.mkdir()
    return d


def _zip(path, names):
    with zipfile.ZipFile(path, "w") as zf:
        for n in names:
            zf.writestr(n, b"\xff\xd8\xff")
    return str(path)


class TestHeldBack:

    def test_no_page_images(self, incoming):
        p = _zip(incoming / "Nova 001.cbz", ["ComicInfo.xml", "readme.txt"])
        assert "no page images" in cover_only_reason(p)

    def test_every_page_is_a_cover(self, incoming):
        p = _zip(
            incoming / "Nova 001.cbz",
            ["Nova_001_cover_A.jpg", "Nova 001 Variant B.jpg", "001cvr.jpg",
             "Nova 001 (Covers) 4.png", "x/Nova 001 cover E.jpg"],
        )
        assert "is a cover" in cover_only_reason(p)

    def test_few_pages_and_cover_wording_in_the_archive_name(self, incoming):
        p = _zip(incoming / "Nova 001 (Variant Covers).cbz", ["01.jpg", "02.jpg"])
        assert cover_only_reason(p)

    def test_few_pages_and_cover_wording_in_the_wrapper_folder(self, tmp_path):
        d = tmp_path / "Nova 001 covers"
        d.mkdir()
        p = _zip(d / "Nova 001.cbz", ["01.jpg", "02.jpg", "03.jpg"])
        assert cover_only_reason(p)

    def test_few_pages_and_one_cover_named_page(self, incoming):
        p = _zip(incoming / "Nova 001.cbz", ["cover.jpg", "zzz-scanner-tag.jpg"])
        assert cover_only_reason(p)

    def test_macos_cruft_is_not_a_page(self, incoming):
        p = _zip(
            incoming / "Nova 001.cbz",
            ["cover.jpg", "__MACOSX/._cover.jpg", ".DS_Store"],
        )
        assert cover_only_reason(p)


class TestFiledAsBefore:

    def test_full_comic_with_a_cover_page(self, incoming):
        names = ["000 cover.jpg"] + [f"{n:03d}.jpg" for n in range(1, 23)]
        assert cover_only_reason(_zip(incoming / "Nova 001.cbz", names)) is None

    def test_full_comic_named_as_a_variant(self, incoming):
        names = [f"{n:03d}.jpg" for n in range(22)]
        p = _zip(incoming / "Nova 001 (Variant Cover).cbz", names)
        assert cover_only_reason(p) is None

    def test_short_one_shot_has_no_page_count_rule(self, incoming):
        p = _zip(incoming / "Nova 001.cbz", ["01.jpg", "02.jpg"])
        assert cover_only_reason(p) is None

    @pytest.mark.parametrize(
        "name", ["Discovery 001.jpg", "Undiscovered 01.jpg", "recovered.jpg"]
    )
    def test_cover_inside_another_word_is_not_a_cover(self, name):
        assert judge_members([name], filename="X 001.cbz") is None


class TestCannotTell:
    """Unknown is never a reason to hold a download back."""

    def test_missing_file(self, incoming):
        assert cover_only_reason(str(incoming / "gone.cbz")) is None

    def test_not_an_archive_spawns_no_tool(self, incoming, monkeypatch):
        import helpers

        def boom(_path):
            raise AssertionError("a non-archive must not reach the RAR tools")

        monkeypatch.setattr(helpers, "list_rar_entries", boom)
        p = incoming / "Nova 001 covers.cbz"
        p.write_bytes(b"stub")
        assert cover_only_reason(str(p)) is None

    def test_unlistable_rar_is_filed_and_not_remembered(self, incoming, monkeypatch):
        import helpers

        monkeypatch.setattr(helpers, "list_rar_entries", lambda _p: None)
        p = incoming / "Nova 001 covers.cbr"
        p.write_bytes(b"Rar!\x1a\x07\x00rest")
        assert cover_only_reason(str(p)) is None
        assert archive_content._verdicts == {}

    def test_rar_is_judged_from_its_listing(self, incoming, monkeypatch):
        import helpers

        monkeypatch.setattr(
            helpers, "list_rar_entries", lambda _p: ["cover A.jpg", "cover B.jpg"]
        )
        p = incoming / "Nova 001.cbr"
        p.write_bytes(b"Rar!\x1a\x07\x00rest")
        assert cover_only_reason(str(p))


class TestMemo:

    def test_second_ask_does_not_reopen_the_archive(self, incoming, monkeypatch):
        p = _zip(incoming / "Nova 001.cbz", ["cover.jpg"])
        first = cover_only_reason(p)

        def boom(_path):
            raise AssertionError("the archive was opened a second time")

        monkeypatch.setattr(archive_content, "_list_members", boom)
        assert cover_only_reason(p) == first

    def test_a_rewritten_file_is_asked_again(self, incoming):
        import os

        path = incoming / "Nova 001.cbz"
        p = _zip(path, ["cover.jpg"])
        assert cover_only_reason(p)
        _zip(path, [f"{n:03d}.jpg" for n in range(22)])
        st = os.stat(p)
        os.utime(p, (st.st_atime, st.st_mtime + 5))
        assert cover_only_reason(p) is None
