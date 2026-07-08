"""Unit tests for core.app_logging.read_log_tail (incremental log polling)."""
import os

from core.app_logging import read_log_tail


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _append(path, text):
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def test_missing_file(tmp_path):
    lines, pos, reset = read_log_tail(str(tmp_path / "nope.log"))
    assert lines == []
    assert pos == 0
    assert reset is False


def test_initial_load_returns_all_lines_and_eof_pos(tmp_path):
    p = tmp_path / "app.log"
    _write(p, "a\nb\nc\n")
    lines, pos, reset = read_log_tail(str(p))
    assert lines == ["a", "b", "c"]
    assert pos == os.path.getsize(p)
    assert reset is False


def test_initial_load_respects_max_lines(tmp_path):
    p = tmp_path / "app.log"
    _write(p, "".join(f"line{i}\n" for i in range(100)))
    lines, pos, _ = read_log_tail(str(p), max_lines=10)
    assert lines == [f"line{i}" for i in range(90, 100)]
    assert pos == os.path.getsize(p)


def test_incremental_returns_only_new_lines(tmp_path):
    p = tmp_path / "app.log"
    _write(p, "a\nb\n")
    _, pos, _ = read_log_tail(str(p))

    _append(p, "c\nd\n")
    lines, new_pos, reset = read_log_tail(str(p), pos=pos)
    assert lines == ["c", "d"]
    assert new_pos == os.path.getsize(p)
    assert reset is False


def test_partial_line_not_emitted_until_newline(tmp_path):
    p = tmp_path / "app.log"
    _write(p, "a\n")
    _, pos, _ = read_log_tail(str(p))

    # Half-written line (no trailing newline) must not be emitted yet.
    _append(p, "partial")
    lines, pos_after, _ = read_log_tail(str(p), pos=pos)
    assert lines == []
    assert pos_after == pos  # position does not advance past incomplete line

    # Once the newline arrives, the full line comes through.
    _append(p, " done\n")
    lines, new_pos, _ = read_log_tail(str(p), pos=pos_after)
    assert lines == ["partial done"]
    assert new_pos == os.path.getsize(p)


def test_no_new_data(tmp_path):
    p = tmp_path / "app.log"
    _write(p, "a\nb\n")
    _, pos, _ = read_log_tail(str(p))
    lines, new_pos, reset = read_log_tail(str(p), pos=pos)
    assert lines == []
    assert new_pos == pos
    assert reset is False


def test_truncation_triggers_reset_and_fresh_tail(tmp_path):
    p = tmp_path / "app.log"
    _write(p, "old1\nold2\nold3\n")
    _, pos, _ = read_log_tail(str(p))

    # Simulate log rotation / clear-on-restart: file is now smaller than pos.
    _write(p, "new1\n")
    lines, new_pos, reset = read_log_tail(str(p), pos=pos)
    assert reset is True
    assert lines == ["new1"]
    assert new_pos == os.path.getsize(p)


def test_empty_file(tmp_path):
    p = tmp_path / "app.log"
    _write(p, "")
    lines, pos, reset = read_log_tail(str(p))
    assert lines == []
    assert pos == 0
    assert reset is False
