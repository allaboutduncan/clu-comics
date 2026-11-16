"""``extract_rar_with_unar`` is all or nothing.

It used to return ``(True, 0)`` whenever a tool exited non-zero but left at
least one file behind. Every caller reads that as a clean extraction and then
deletes the source, so a CBR that gave up 39 of its 104 pages became a 39-page
CBZ, was logged as "Successfully converted", and the original was removed. The
same file gave 80 pages on the next run, so this is not only about archives
that are really damaged.

No RAR binary is needed: ``subprocess.run`` is replaced by a fake that plays
each tool's part (Python cannot write a RAR, so there are no real fixtures).
"""
import os
from types import SimpleNamespace

import pytest

import helpers
from helpers import extract_rar_with_unar

TOTAL_PAGES = 104


class FakeTools:
    """Stands in for unrar / 7z / unar / lsar.

    ``extract`` maps a tool name to ``(returncode, pages_written)`` or to
    ``FileNotFoundError`` for a tool that is not installed. ``listed`` is the
    number of pages the archive claims to hold, or ``None`` when no tool can
    list it.
    """

    def __init__(self, extract, listed=TOTAL_PAGES, unar_stdout=b""):
        self.extract = extract
        self.listed = listed
        self.unar_stdout = unar_stdout
        self.extract_calls = []

    def __call__(self, cmd, capture_output=True, **kwargs):
        tool = cmd[0]
        if tool == "lsar" or cmd[1] in ("lb", "l"):
            return self._list(tool)

        self.extract_calls.append(tool)
        outcome = self.extract.get(tool, FileNotFoundError)
        if outcome is FileNotFoundError:
            raise FileNotFoundError(tool)
        returncode, pages = outcome
        if tool == "unrar":
            dest = cmd[-1].rstrip("/")
        elif tool == "7z":
            dest = cmd[2][2:]
        else:
            dest = cmd[3]
        for i in range(pages):
            with open(os.path.join(dest, f"page{i:03d}.jpg"), "wb") as f:
                f.write(b"x")
        if pages:
            with open(os.path.join(dest, "ComicInfo.xml"), "wb") as f:
                f.write(b"<ComicInfo/>")
        stdout = self.unar_stdout if tool == "unar" else b""
        stderr = b"" if returncode == 0 else b"checksum error in page.jpg\nTotal errors: 65"
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    def _list(self, tool):
        if self.listed is None or tool != "unrar":
            return SimpleNamespace(returncode=10, stdout=b"", stderr=b"")
        names = [f"page{i:03d}.jpg" for i in range(self.listed)] + ["ComicInfo.xml"]
        return SimpleNamespace(
            returncode=0, stdout="\n".join(names).encode(), stderr=b"",
        )


@pytest.fixture
def rar(tmp_path, monkeypatch):
    monkeypatch.setattr("helpers.library.is_allowed_path", lambda p: True)
    path = tmp_path / "Warchild 001.cbr"
    path.write_bytes(b"not really a rar")
    return str(path)


def _run(monkeypatch, tools, rar, out):
    monkeypatch.setattr(helpers.subprocess, "run", tools)
    return extract_rar_with_unar(rar, str(out))


def _pages(out):
    return sorted(n for n in os.listdir(out) if n.endswith(".jpg"))


def test_a_clean_extraction_succeeds(rar, tmp_path, monkeypatch):
    out = tmp_path / "out"
    tools = FakeTools({"unrar": (0, TOTAL_PAGES)})

    assert _run(monkeypatch, tools, rar, out) == (True, 0)
    assert len(_pages(out)) == TOTAL_PAGES
    assert tools.extract_calls == ["unrar"]


def test_a_partial_unrar_is_discarded_and_the_next_tool_is_tried(
        rar, tmp_path, monkeypatch):
    """The reported case: unrar rc=3 with 39 of 104 pages on disk."""
    out = tmp_path / "out"
    tools = FakeTools({"unrar": (3, 39), "7z": (0, TOTAL_PAGES)})

    assert _run(monkeypatch, tools, rar, out) == (True, 0)
    assert len(_pages(out)) == TOTAL_PAGES
    assert tools.extract_calls == ["unrar", "7z"]


def test_no_tool_extracting_everything_is_a_failure_with_nothing_left(
        rar, tmp_path, monkeypatch):
    out = tmp_path / "out"
    tools = FakeTools({"unrar": (3, 39), "7z": (2, 80), "unar": (1, 10)})

    ok, missing = _run(monkeypatch, tools, rar, out)

    assert ok is False
    assert missing == TOTAL_PAGES - 80, "reports the best attempt's shortfall"
    assert os.listdir(out) == [], "a partial payload must never reach the caller"
    assert tools.extract_calls == ["unrar", "7z", "unar"]


def test_exit_zero_with_fewer_pages_than_the_archive_lists_is_partial(
        rar, tmp_path, monkeypatch):
    """The exit status is not the only evidence; the page count is checked."""
    out = tmp_path / "out"
    tools = FakeTools({"unrar": (0, 39)})

    ok, missing = _run(monkeypatch, tools, rar, out)

    assert (ok, missing) == (False, TOTAL_PAGES - 39)
    assert os.listdir(out) == []


def test_a_non_zero_exit_fails_even_when_every_page_is_there(
        rar, tmp_path, monkeypatch):
    out = tmp_path / "out"
    tools = FakeTools({"unrar": (3, TOTAL_PAGES)})

    ok, missing = _run(monkeypatch, tools, rar, out)

    assert ok is False
    assert missing >= 1, "0 would read as 'nothing extracted'"


def test_an_unlistable_archive_is_judged_on_the_exit_status(
        rar, tmp_path, monkeypatch):
    """"Cannot tell how many pages" is unknown, never zero and never a veto."""
    out = tmp_path / "out"

    assert _run(monkeypatch, FakeTools({"unrar": (0, 12)}, listed=None),
                rar, out) == (True, 0)

    out2 = tmp_path / "out2"
    ok, missing = _run(monkeypatch, FakeTools({"unrar": (3, 12)}, listed=None),
                       rar, out2)
    assert ok is False and missing >= 1


def test_unar_reporting_failed_members_is_partial(rar, tmp_path, monkeypatch):
    """unar exits 0 and says "Failed!" per member on stdout."""
    out = tmp_path / "out"
    tools = FakeTools(
        {"unar": (0, 5)}, listed=None,
        unar_stdout=b"  page005.jpg  (10 B)... Failed! (checksum)\n"
                    b"  page006.jpg  (10 B)... Failed! (checksum)\n",
    )

    assert _run(monkeypatch, tools, rar, out) == (False, 2)


def test_nothing_extracted_reports_zero(rar, tmp_path, monkeypatch):
    out = tmp_path / "out"
    tools = FakeTools({"unrar": (10, 0), "7z": (2, 0)})

    assert _run(monkeypatch, tools, rar, out) == (False, 0)


def test_a_directory_with_other_files_in_it_is_never_wiped(
        rar, tmp_path, monkeypatch):
    """The monitor once extracted straight into WATCH.

    Discarding a partial attempt empties the directory being extracted into,
    so a directory that already holds something gets a private stage instead.
    """
    out = tmp_path / "watch"
    out.mkdir()
    bystander = out / "Someone Elses Comic.cbz"
    bystander.write_bytes(b"keep me")

    ok, _missing = _run(monkeypatch, FakeTools({"unrar": (3, 39)}), rar, out)

    assert ok is False
    assert os.listdir(out) == ["Someone Elses Comic.cbz"]
    assert bystander.read_bytes() == b"keep me"

    assert _run(monkeypatch, FakeTools({"unrar": (0, TOTAL_PAGES)}),
                rar, out) == (True, 0)
    assert len(_pages(out)) == TOTAL_PAGES
    assert bystander.read_bytes() == b"keep me"
    assert not [n for n in os.listdir(out) if n.startswith(".clu_rar_")]
