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
import subprocess
from types import SimpleNamespace

import pytest

import helpers
from helpers import extract_rar_with_unar

TOTAL_PAGES = 104


class FakeTools:
    """Stands in for unrar / 7z / unar / lsar.

    ``extract`` maps a tool name to ``(returncode, pages_written)``, to
    ``FileNotFoundError`` for a tool that is not installed, or to
    ``("timeout", pages_written)`` for a tool that wrote that many pages and
    then ran past its timeout. ``listed`` is the number of pages the archive
    claims to hold, or ``None`` when no tool can list it; ``list_timeout``
    names listing tools that hang. Every call's keyword arguments are kept.
    """

    def __init__(self, extract, listed=TOTAL_PAGES, unar_stdout=b"",
                 list_timeout=()):
        self.extract = extract
        self.listed = listed
        self.unar_stdout = unar_stdout
        self.list_timeout = set(list_timeout)
        self.extract_calls = []
        self.list_calls = []
        self.call_kwargs = []

    def __call__(self, cmd, capture_output=True, **kwargs):
        self.call_kwargs.append((cmd[0], kwargs))
        tool = cmd[0]
        if tool == "lsar" or cmd[1] in ("lb", "l"):
            self.list_calls.append(tool)
            if tool in self.list_timeout:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
            return self._list(tool)

        self.extract_calls.append(tool)
        outcome = self.extract.get(tool, FileNotFoundError)
        if outcome is FileNotFoundError:
            raise FileNotFoundError(tool)
        returncode, pages = outcome
        timed_out = returncode == "timeout"
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
        if timed_out:
            # What subprocess.run does: kill the child, then raise.
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
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


# ---------------------------------------------------------------------------
# A tool that hangs is a failed attempt, never a caller blocked forever.
# ---------------------------------------------------------------------------

class _Log:
    """Stands in for app_logger and keeps every WARNING."""

    def __init__(self):
        self.warnings = []

    def warning(self, msg, *a, **k):
        self.warnings.append(str(msg))

    def info(self, *a, **k):
        pass

    debug = error = info


@pytest.fixture
def log(monkeypatch):
    recorder = _Log()
    monkeypatch.setattr(helpers, "app_logger", recorder)
    return recorder


def test_a_timed_out_tool_is_discarded_and_the_next_tool_is_tried(
        rar, tmp_path, monkeypatch, log):
    out = tmp_path / "out"
    tools = FakeTools({"unrar": ("timeout", 50), "7z": (0, TOTAL_PAGES)})

    assert _run(monkeypatch, tools, rar, out) == (True, 0)
    assert len(_pages(out)) == TOTAL_PAGES
    assert tools.extract_calls == ["unrar", "7z"]
    timeouts = [w for w in log.warnings if "timed out" in w]
    assert len(timeouts) == 1, "one WARNING per timed-out attempt, not per member"
    assert "unrar" in timeouts[0] and "Warchild 001.cbr" in timeouts[0]
    assert f"{helpers.ARCHIVE_EXTRACT_TIMEOUT_MIN}s" in timeouts[0]


def test_every_tool_timing_out_is_a_failure_with_nothing_left(
        rar, tmp_path, monkeypatch, log):
    out = tmp_path / "out"
    tools = FakeTools({"unrar": ("timeout", 50), "7z": ("timeout", 3),
                       "unar": ("timeout", 0)})

    ok, missing = _run(monkeypatch, tools, rar, out)

    assert (ok, missing) == (False, 0)
    assert os.listdir(out) == [], "a timed-out attempt's output must be removed"
    assert tools.extract_calls == ["unrar", "7z", "unar"]
    assert len([w for w in log.warnings if "timed out" in w]) == 3


def test_a_timeout_after_a_partial_attempt_reports_the_shortfall(
        rar, tmp_path, monkeypatch, log):
    out = tmp_path / "out"
    tools = FakeTools({"unrar": (3, 80), "7z": ("timeout", 100), "unar": (1, 10)})

    assert _run(monkeypatch, tools, rar, out) == (False, TOTAL_PAGES - 80)
    assert os.listdir(out) == []


def test_a_timeout_never_touches_what_was_already_in_the_directory(
        rar, tmp_path, monkeypatch, log):
    out = tmp_path / "watch"
    out.mkdir()
    bystander = out / "Someone Elses Comic.cbz"
    bystander.write_bytes(b"keep me")

    ok, _missing = _run(
        monkeypatch,
        FakeTools({"unrar": ("timeout", 60), "7z": ("timeout", 60),
                   "unar": ("timeout", 60)}),
        rar, out,
    )

    assert ok is False
    assert os.listdir(out) == ["Someone Elses Comic.cbz"]
    assert bystander.read_bytes() == b"keep me"


def test_every_tool_call_has_a_timeout_and_no_stdin(rar, tmp_path, monkeypatch):
    """A password prompt reads EOF instead of waiting on a stdin nobody feeds."""
    tools = FakeTools({"unrar": (3, 39), "7z": (2, 80), "unar": (0, TOTAL_PAGES)})

    _run(monkeypatch, tools, rar, tmp_path / "out")

    assert {name for name, _ in tools.call_kwargs} >= {"unrar", "7z", "unar"}
    for name, kwargs in tools.call_kwargs:
        assert kwargs.get("timeout"), f"{name} was run without a timeout"
        assert kwargs.get("stdin") is subprocess.DEVNULL, name
        assert kwargs.get("start_new_session") is True, name


def test_listing_uses_the_short_timeout_and_extraction_the_scaled_one(
        rar, tmp_path, monkeypatch):
    tools = FakeTools({"unrar": (0, TOTAL_PAGES)})

    _run(monkeypatch, tools, rar, tmp_path / "out")

    unrar_timeouts = [k["timeout"] for name, k in tools.call_kwargs if name == "unrar"]
    # "lb" first, then "x".
    assert unrar_timeouts == [helpers.ARCHIVE_LIST_TIMEOUT,
                              helpers.ARCHIVE_EXTRACT_TIMEOUT_MIN]


def test_a_listing_timeout_tries_the_next_listing_tool(rar, monkeypatch, log):
    tools = FakeTools({}, list_timeout={"unrar"})
    monkeypatch.setattr(helpers.subprocess, "run", tools)

    # unrar hangs; 7z and lsar answer "cannot list" (rc=10) in this fake.
    assert helpers.list_rar_entries(rar) is None
    assert tools.list_calls == ["unrar", "7z", "lsar"]
    timeouts = [w for w in log.warnings if "timed out" in w]
    assert len(timeouts) == 1 and "Warchild 001.cbr" in timeouts[0]


def test_a_hanging_listing_leaves_the_extraction_judged_on_exit_status(
        rar, tmp_path, monkeypatch, log):
    out = tmp_path / "out"
    tools = FakeTools({"unrar": (0, 12)}, list_timeout={"unrar", "7z", "lsar"})

    assert _run(monkeypatch, tools, rar, out) == (True, 0)


def test_unrar_is_told_never_to_ask_for_a_password(rar, tmp_path, monkeypatch):
    seen = []

    def fake(cmd, **kwargs):
        seen.append(cmd)
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(helpers.subprocess, "run", fake)
    extract_rar_with_unar(rar, str(tmp_path / "out"))

    unrar_cmds = [c for c in seen if c[0] == "unrar"]
    assert unrar_cmds and all("-p-" in c for c in unrar_cmds)


# --------------------------- archive_extract_timeout --------------------------

MB = 1024 * 1024


def _sized(path, size):
    with open(path, "wb") as f:
        f.truncate(size)
    return str(path)


@pytest.fixture
def slow_rate(monkeypatch):
    """1 KB/s, so a few hundred KB of fixture stands in for gigabytes."""
    monkeypatch.setattr(helpers, "ARCHIVE_EXTRACT_BYTES_PER_SECOND", 1000)
    return 1000


def test_the_real_rate_gives_a_large_pack_minutes_not_seconds():
    """A 500MB pack keeps the floor; a 5GB multipart release gets ~20 minutes."""
    rate = helpers.ARCHIVE_EXTRACT_BYTES_PER_SECOND
    assert 500 * MB / rate <= helpers.ARCHIVE_EXTRACT_TIMEOUT_MIN
    assert 5 * 1024 * MB / rate > 15 * 60
    assert helpers.ARCHIVE_EXTRACT_TIMEOUT_MAX <= 3600


def test_extract_timeout_has_a_floor_for_a_small_comic(tmp_path, slow_rate):
    path = _sized(tmp_path / "Batman 001.cbr", 30 * slow_rate)
    assert helpers.archive_extract_timeout(path) == helpers.ARCHIVE_EXTRACT_TIMEOUT_MIN


def test_extract_timeout_scales_with_size(tmp_path, slow_rate):
    path = _sized(tmp_path / "Big Pack.rar", 500 * slow_rate)
    assert helpers.archive_extract_timeout(path) == 500


def test_extract_timeout_counts_every_volume_of_a_multipart_set(tmp_path, slow_rate):
    for i in range(1, 6):
        _sized(tmp_path / f"Release.part{i:02d}.rar", 100 * slow_rate)
    _sized(tmp_path / "Unrelated.part01.rar", 2000 * slow_rate)

    primary = str(tmp_path / "Release.part01.rar")
    assert helpers.archive_extract_timeout(primary) == 500


def test_extract_timeout_counts_old_style_volumes(tmp_path, slow_rate):
    _sized(tmp_path / "Release.rar", 100 * slow_rate)
    _sized(tmp_path / "Release.r00", 100 * slow_rate)
    _sized(tmp_path / "Release.r01", 100 * slow_rate)
    assert helpers.archive_extract_timeout(str(tmp_path / "Release.rar")) == 300


def test_extract_timeout_has_a_ceiling(tmp_path, monkeypatch):
    path = _sized(tmp_path / "Huge.rar", 1)
    monkeypatch.setattr(helpers, "_archive_set_size", lambda p: 200 * 1024 * MB)
    assert helpers.archive_extract_timeout(path) == helpers.ARCHIVE_EXTRACT_TIMEOUT_MAX


def test_extract_timeout_of_a_missing_file_is_the_floor(tmp_path):
    assert helpers.archive_extract_timeout(
        str(tmp_path / "gone.rar")) == helpers.ARCHIVE_EXTRACT_TIMEOUT_MIN
