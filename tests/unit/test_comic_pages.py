"""core/comic_pages.py: one page out of a RAR when rarfile cannot read it.

This was app.py code and could only be checked through the AST. The tools are
replaced by stand-ins that write (or fail to write) the file they were asked
for, so the fallback order and the completeness check are exercised for real.
"""
import contextlib
import os
import subprocess

import pytest

import core.comic_pages as cp

JPEG = b"\xff\xd8" + b"x" * 40 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 40 + b"\x00\x00\x00\x00IEND\xaeB`\x82"


class TestIsValidImageData:
    @pytest.mark.parametrize("data", [JPEG, PNG, b"GIF89a" + b"x" * 20, b"RIFF" + b"x" * 20])
    def test_complete_images_pass(self, data):
        assert cp._is_valid_image_data(data) is True

    @pytest.mark.parametrize("data", [
        None, b"", b"\xff\xd8short",          # too short to be anything
        JPEG[:-2],                             # JPEG with no end-of-image marker
        PNG[:-12] + b"x" * 12,                 # PNG with no IEND chunk
    ])
    def test_truncated_images_fail(self, data):
        assert cp._is_valid_image_data(data) is False


class TestFindExtractedFile:
    def test_finds_a_nested_file_case_insensitively(self, tmp_path):
        nested = tmp_path / "Chapter 1"
        nested.mkdir()
        (nested / "Page01.JPG").write_bytes(b"x")
        assert cp._find_extracted_file(str(tmp_path), "page01.jpg") == str(nested / "Page01.JPG")

    def test_no_match_is_none(self, tmp_path):
        (tmp_path / "other.jpg").write_bytes(b"x")
        assert cp._find_extracted_file(str(tmp_path), "page01.jpg") is None


@pytest.fixture
def rar(tmp_path, monkeypatch):
    """A stand-in .cbr inside an allowed folder."""
    path = tmp_path / "Batman 001.cbr"
    path.write_bytes(b"Rar!\x1a\x07\x00")
    monkeypatch.setattr("helpers.library.is_allowed_path", lambda p: True)
    return str(path)


def _tool(results):
    """A subprocess.run stand-in. ``results`` maps tool name -> bytes to write
    into the tool's output folder, ``None`` for "ran but produced nothing", or
    an exception to raise."""
    calls = []

    def run(cmd, **kwargs):
        name = cmd[0]
        calls.append(name)
        outcome = results.get(name, FileNotFoundError(name))
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not None:
            if name == "unrar":
                out_dir = cmd[-1].rstrip("/")
            elif name == "7z":
                out_dir = next(a[2:] for a in cmd if a.startswith("-o"))
            else:
                out_dir = cmd[3]
            with open(os.path.join(out_dir, "page01.jpg"), "wb") as fh:
                fh.write(outcome)
        return subprocess.CompletedProcess(cmd, 0)

    run.calls = calls
    return run


@contextlib.contextmanager
def _no_cache(_rar_path):
    yield None


class TestExtractSingleRarEntry:
    def test_a_path_outside_the_allowed_folders_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr("helpers.library.is_allowed_path", lambda p: False)
        run = _tool({"unrar": JPEG})
        monkeypatch.setattr(cp.subprocess, "run", run)
        assert cp._extract_single_rar_entry(str(tmp_path / "x.cbr"), "page01.jpg") is None
        assert run.calls == [], "no tool is started for a refused path"

    def test_an_entry_with_no_file_name_is_refused(self, rar, monkeypatch):
        run = _tool({"unrar": JPEG})
        monkeypatch.setattr(cp.subprocess, "run", run)
        assert cp._extract_single_rar_entry(rar, "folder/") is None
        assert run.calls == []

    def test_the_first_tool_that_gives_a_complete_page_wins(self, rar, monkeypatch):
        run = _tool({"unrar": JPEG, "7z": b"never asked"})
        monkeypatch.setattr(cp.subprocess, "run", run)
        assert cp._extract_single_rar_entry(rar, "sub/page01.jpg") == JPEG
        assert run.calls == ["unrar"]

    def test_a_truncated_page_falls_through_to_the_next_tool(self, rar, monkeypatch):
        """Exit 0 with half a JPEG is not the page."""
        run = _tool({"unrar": JPEG[:-2], "7z": JPEG})
        monkeypatch.setattr(cp.subprocess, "run", run)
        assert cp._extract_single_rar_entry(rar, "page01.jpg") == JPEG
        assert run.calls == ["unrar", "7z"]

    def test_a_missing_or_failing_tool_is_skipped(self, rar, monkeypatch):
        run = _tool({"7z": subprocess.TimeoutExpired("7z", 60), "unar": JPEG})
        monkeypatch.setattr(cp.subprocess, "run", run)
        assert cp._extract_single_rar_entry(rar, "page01.jpg") == JPEG
        assert run.calls == ["unrar", "7z", "unar"]

    def test_the_cached_full_extraction_is_the_last_resort(self, rar, tmp_path, monkeypatch):
        extracted = tmp_path / "cache" / "attempt0"
        extracted.mkdir(parents=True)
        (extracted / "page01.jpg").write_bytes(JPEG)
        held = []

        @contextlib.contextmanager
        def acquire(rar_path):
            held.append(rar_path)
            yield str(tmp_path / "cache")

        monkeypatch.setattr(cp.subprocess, "run", _tool({}))
        monkeypatch.setattr(cp._rar_cache, "acquire", acquire)
        assert cp._extract_single_rar_entry(rar, "page01.jpg") == JPEG
        assert held == [os.path.realpath(rar)]

    def test_a_truncated_page_from_the_cache_is_not_served(self, rar, tmp_path, monkeypatch):
        extracted = tmp_path / "cache"
        extracted.mkdir()
        (extracted / "page01.jpg").write_bytes(JPEG[:-2])

        @contextlib.contextmanager
        def acquire(rar_path):
            yield str(extracted)

        monkeypatch.setattr(cp.subprocess, "run", _tool({}))
        monkeypatch.setattr(cp._rar_cache, "acquire", acquire)
        assert cp._extract_single_rar_entry(rar, "page01.jpg") is None

    def test_nothing_readable_anywhere_is_none(self, rar, monkeypatch):
        monkeypatch.setattr(cp.subprocess, "run", _tool({"unrar": None, "7z": None, "unar": None}))
        monkeypatch.setattr(cp._rar_cache, "acquire", _no_cache)
        assert cp._extract_single_rar_entry(rar, "page01.jpg") is None


class TestExtractFullRarInto:
    def test_each_attempt_gets_its_own_folder_and_a_failed_one_is_removed(self, tmp_path, monkeypatch):
        seen = []

        def attempt(cmd, rar_path, tmp_dir):
            seen.append((cmd[0], os.path.basename(tmp_dir), os.path.isdir(tmp_dir)))
            return cmd[0] == "7z"

        monkeypatch.setattr(cp, "_try_full_extraction", attempt)
        assert cp._extract_full_rar_into("/x/a.cbr", str(tmp_path)) is True
        assert seen == [("unrar", "attempt0", True), ("7z", "attempt1", True)]
        assert sorted(os.listdir(tmp_path)) == ["attempt1"]

    def test_every_tool_failing_is_false_and_leaves_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cp, "_try_full_extraction", lambda *a: False)
        assert cp._extract_full_rar_into("/x/a.cbr", str(tmp_path)) is False
        assert os.listdir(tmp_path) == []

    def test_the_output_folder_reaches_each_tool_in_its_own_syntax(self, tmp_path, monkeypatch):
        cmds = []
        monkeypatch.setattr(cp, "_try_full_extraction", lambda cmd, r, d: cmds.append(cmd) or False)
        cp._extract_full_rar_into("/x/a.cbr", str(tmp_path))
        unrar, seven, unar = cmds
        assert unrar[-1].endswith("attempt0/") and unrar[-2] == "/x/a.cbr"
        assert any(a.startswith("-o") and a.endswith("attempt1") for a in seven)
        assert unar[2] == "-o" and unar[3].endswith("attempt2") and unar[4] == "/x/a.cbr"


class TestTryFullExtraction:
    def test_files_produced_is_success_even_on_a_non_zero_exit(self, tmp_path, monkeypatch):
        def run(cmd, **kwargs):
            (tmp_path / "p.jpg").write_bytes(JPEG)
            return subprocess.CompletedProcess(cmd, 3)

        monkeypatch.setattr(cp.subprocess, "run", run)
        assert cp._try_full_extraction(["unrar"], "/x/a.cbr", str(tmp_path)) is True

    def test_nothing_produced_is_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cp.subprocess, "run",
                            lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))
        assert cp._try_full_extraction(["unrar"], "/x/a.cbr", str(tmp_path)) is False

    def test_a_tool_that_is_not_installed_is_failure_not_an_error(self, tmp_path, monkeypatch):
        def run(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr(cp.subprocess, "run", run)
        assert cp._try_full_extraction(["unar"], "/x/a.cbr", str(tmp_path)) is False

    def test_the_tool_is_given_a_timeout(self, tmp_path, monkeypatch):
        seen = {}
        monkeypatch.setattr(cp.subprocess, "run",
                            lambda cmd, **k: seen.update(k) or subprocess.CompletedProcess(cmd, 0))
        cp._try_full_extraction(["unrar"], "/x/a.cbr", str(tmp_path))
        assert seen.get("timeout"), "a hung tool would hold a request thread for good"
