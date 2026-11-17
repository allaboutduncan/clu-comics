"""``/move-batch`` (and ``/move``) across two mounts (#582).

The move phase used ``shutil.move``: across devices that is ``copy2`` for a
file and ``copytree(copy2)`` for a folder, and a library mount that refuses
``utime``/``chmod`` raised EPERM after every byte was copied. The item was
reported as an error, the source stayed where it was, a full copy sat in the
library, and ``update_index_on_move`` never ran. The moved comic also kept the
mode it arrived with (``root:0600`` from a downloader breaks thumbnails).

The move is real here; only ``app`` (never importable in tests) is faked, and
the background thread is run inline.
"""
import errno
import os
import shutil
import sys
import types
from unittest.mock import patch

import pytest


@pytest.fixture
def mounts(tmp_path, monkeypatch):
    """``tmp_path/<name>`` folders that behave like separate filesystems;
    ``copystat`` onto ``library`` raises EPERM."""
    root = os.path.abspath(str(tmp_path))
    real_rename, real_replace, real_copystat = os.rename, os.replace, shutil.copystat

    def _volume(p):
        return os.path.relpath(os.path.abspath(str(p)), root).split(os.sep)[0]

    def _guard(real):
        def _move(src, dst, *a, **k):
            if _volume(src) != _volume(dst):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(src, dst, *a, **k)
        return _move

    def _copystat(src, dst, *a, **k):
        if _volume(dst) == "library":
            raise PermissionError(errno.EPERM, "Operation not permitted", str(dst))
        return real_copystat(src, dst, *a, **k)

    monkeypatch.setattr(os, "rename", _guard(real_rename))
    monkeypatch.setattr(os, "replace", _guard(real_replace))
    monkeypatch.setattr(shutil, "copystat", _copystat)
    # Windows' copy2 is CopyFile2 and never calls copystat; take the POSIX path.
    monkeypatch.setattr(shutil, "_winapi", None)
    (tmp_path / "library").mkdir()
    (tmp_path / "downloads").mkdir()
    return tmp_path


class _InlineThread:
    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def fake_app():
    index = []
    fake = types.ModuleType("app")
    fake.auto_fetch_metron_metadata = lambda p: p
    fake.auto_fetch_comicvine_metadata = lambda p: p
    fake.auto_fetch_comicvine_sqlite_metadata = lambda p: p
    fake.log_file_if_in_data = lambda p: None
    fake.update_index_on_move = lambda s, d, **k: index.append((s, d))
    fake.get_target_dir_live = lambda: None
    fake.schedule_target_cleanup = lambda *a, **k: None
    old = sys.modules.get("app")
    sys.modules["app"] = fake
    try:
        yield index
    finally:
        if old is not None:
            sys.modules["app"] = old
        else:
            sys.modules.pop("app", None)


@pytest.fixture
def run_batch(client, fake_app):
    """POST /move-batch with the worker run inline; returns (response, op calls)."""
    def _run(items):
        normalised = []
        with patch("routes.files.threading.Thread", _InlineThread), \
             patch("routes.files.is_critical_path", return_value=False), \
             patch("routes.files.app_state") as state, \
             patch("routes.files.memory_context"), \
             patch("core.config.is_auto_metadata_on_move_enabled", return_value=False), \
             patch("helpers.collection._series_id_for_path", return_value=None), \
             patch("helpers.match_parent_permissions", side_effect=normalised.append):
            state.register_operation.return_value = "op-1"
            state.get_operation.return_value = {}
            resp = client.post("/move-batch", json={"items": items})
        return resp, state, normalised
    return _run


def test_a_file_moves_onto_a_refusing_mount(mounts, run_batch, fake_app):
    src = mounts / "downloads" / "Batman 001 (2016).cbz"
    src.write_bytes(b"comic")
    dst = mounts / "library" / "Batman" / "Batman 001 (2016).cbz"
    dst.parent.mkdir()

    resp, state, normalised = run_batch([{"source": str(src), "destination": str(dst)}])

    assert resp.status_code == 200
    state.complete_operation.assert_called_once_with("op-1", error=False)
    assert dst.read_bytes() == b"comic"
    assert not src.exists()
    assert normalised == [str(dst)], "the moved comic takes its new folder's permissions"
    assert fake_app == [(str(src), str(dst))], "the index still follows the move"
    assert [p.name for p in dst.parent.iterdir()] == [dst.name]


def test_a_folder_moves_onto_a_refusing_mount(mounts, run_batch, fake_app):
    src = mounts / "downloads" / "Batman (2016)"
    (src / "Annuals").mkdir(parents=True)
    (src / "Batman 001 (2016).cbz").write_bytes(b"one")
    (src / "Annuals" / "Batman Annual 001.cbz").write_bytes(b"annual")
    dst = mounts / "library" / "Batman (2016)"

    resp, state, _ = run_batch([{"source": str(src), "destination": str(dst)}])

    assert resp.status_code == 200
    state.complete_operation.assert_called_once_with("op-1", error=False)
    assert (dst / "Annuals" / "Batman Annual 001.cbz").read_bytes() == b"annual"
    assert not src.exists()
    assert fake_app == [(str(src), str(dst))]
    assert [p.name for p in (mounts / "library").iterdir()] == ["Batman (2016)"]


def test_a_folder_is_not_nested_into_an_existing_one(mounts, run_batch, fake_app):
    """shutil.move put src *inside* an existing dst, and the index then
    recorded a path the folder never landed at."""
    src = mounts / "downloads" / "Batman (2016)"
    src.mkdir()
    (src / "Batman 001 (2016).cbz").write_bytes(b"one")
    dst = mounts / "library" / "Batman (2016)"
    dst.mkdir()

    resp, state, _ = run_batch([{"source": str(src), "destination": str(dst)}])

    assert resp.status_code == 200
    state.complete_operation.assert_called_once_with("op-1", error=True)
    assert (src / "Batman 001 (2016).cbz").exists()
    assert list(dst.iterdir()) == []
    assert fake_app == []


def test_a_file_over_an_existing_file_is_replaced_as_before(mounts, run_batch, fake_app):
    src = mounts / "downloads" / "Batman 001.cbz"
    src.write_bytes(b"new")
    dst = mounts / "library" / "Batman 001.cbz"
    dst.write_bytes(b"old")

    resp, state, _ = run_batch([{"source": str(src), "destination": str(dst)}])

    state.complete_operation.assert_called_once_with("op-1", error=False)
    assert dst.read_bytes() == b"new"
    assert not src.exists()
