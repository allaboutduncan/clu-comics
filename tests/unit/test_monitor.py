"""
Unit tests for monitor.py folder monitoring.

Focus: the reconciliation sweep (self-healing for files that miss a watchdog
event) and the in-flight concurrency guard that keeps the sweep from colliding
with live watchdog callbacks. These cover the regression where a completed
comic sat in the WATCH dir forever because nothing re-drove the move.

The handler is exercised directly (no observer/threads) with temp WATCH/TARGET
dirs. Real time.sleep and the size-stability helpers are stubbed so the tests
run fast and deterministically.
"""
import os
import pytest


@pytest.fixture
def handler(tmp_path, monkeypatch):
    """A DownloadCompleteHandler wired to temp WATCH/TARGET dirs.

    Returns (handler, watch_dir, target_dir). Sleeps and the size-stability
    checks are neutralized so a file present on disk counts as "complete".
    reload_settings() is stubbed so our temp dirs aren't clobbered by prod config.
    """
    import monitor

    watch = tmp_path / "watch"
    target = tmp_path / "target"
    watch.mkdir()
    target.mkdir()

    h = monitor.DownloadCompleteHandler(
        directory=str(watch),
        target_directory=str(target),
        ignored_extensions=[".crdownload", ".tmp"],
    )
    h.directory = str(watch)
    h.target_directory = str(target)
    h.auto_rename_monitor = False   # test the move path, not renaming
    h.auto_unpack = False
    h.autoconvert = False
    h.consolidate_directories = False
    h.move_directories = False

    # No real waiting: a file that exists on disk is "complete".
    monkeypatch.setattr(h, "_is_download_complete", lambda fp: os.path.exists(fp))
    monkeypatch.setattr(monitor, "_wait_for_download_completion", lambda *a, **k: True)
    monkeypatch.setattr(monitor.time, "sleep", lambda *a, **k: None)
    # reconcile_directory() calls reload_settings(); keep our temp dirs.
    monkeypatch.setattr(h, "reload_settings", lambda: None)

    return h, str(watch), str(target)


def _write(path, content=b"comic-bytes"):
    with open(path, "wb") as f:
        f.write(content)


def _moved_path(target_dir, name):
    """Where _move_file lands a file: it runs clean_directory_name over the
    destination dir. (In pytest temp paths that rewrites underscores to spaces,
    so we must mirror it rather than assume the raw path.)"""
    from cbz_ops.rename import clean_directory_name
    return os.path.join(clean_directory_name(target_dir), name)


def test_reconcile_directory_moves_stranded_cbz(handler):
    """A completed .cbz sitting in WATCH with no event gets drained to TARGET."""
    h, watch, target = handler
    name = "Series 001 (2024).cbz"
    _write(os.path.join(watch, name))

    h.reconcile_directory()

    assert not os.path.exists(os.path.join(watch, name)), "file should leave WATCH"
    assert os.path.exists(_moved_path(target, name)), "file should land in TARGET"


def test_in_flight_guard_prevents_double_processing(handler, monkeypatch):
    """A file already claimed by another thread is skipped, not re-processed."""
    h, watch, target = handler
    name = "Series 002 (2024).cbz"
    path = os.path.join(watch, name)
    _write(path)

    called = []
    monkeypatch.setattr(h, "_process_file", lambda fp: called.append(fp))

    # Simulate the observer thread currently holding this file.
    h._in_flight.add(os.path.abspath(path))

    h._handle_file_if_complete(path)

    assert called == [], "_process_file must not run while file is in-flight"
    assert os.path.exists(path), "file must stay put when skipped"


def test_in_flight_claim_released_after_processing(handler):
    """After a normal move the in-flight set is empty (claim released)."""
    h, watch, target = handler
    name = "Series 003 (2024).cbz"
    _write(os.path.join(watch, name))

    h._handle_file_if_complete(os.path.join(watch, name))

    assert h._in_flight == set(), "claim must be released via finally"
    assert os.path.exists(_moved_path(target, name))


def test_move_file_tolerates_file_renamed_midflight(handler, monkeypatch):
    """If the file vanishes mid-move (api.py in-place rename), no exception
    escapes and the sweep keeps going."""
    import monitor
    h, watch, target = handler
    name = "Series 004 (2024).cbz"
    path = os.path.join(watch, name)
    _write(path)

    # Wait "succeeds" but the file is renamed away right before shutil.move.
    def _steal(fp, *a, **k):
        os.remove(fp)
        return True
    monkeypatch.setattr(monitor, "_wait_for_download_completion", _steal)

    # Must not raise even though shutil.move will hit FileNotFoundError.
    h._move_file(path)

    assert not os.path.exists(os.path.join(target, name))


def test_reconcile_does_not_crash_on_disappearing_file(handler, monkeypatch):
    """A file that disappears during the stability check is handled quietly."""
    h, watch, target = handler
    path = os.path.join(watch, "Series 005 (2024).cbz")
    _write(path)

    # Report "not complete" and delete it, mimicking an in-place rename race.
    def _not_complete(fp):
        if os.path.exists(fp):
            os.remove(fp)
        return False
    monkeypatch.setattr(h, "_is_download_complete", _not_complete)

    h.reconcile_directory()  # must not raise
    assert h._in_flight == set()


def test_reconcile_skips_temp_and_ignored_files(handler):
    """Temporary downloads and ignored-extension files stay in WATCH; only the
    real comic is moved."""
    h, watch, target = handler
    temp = "Series 006 (2024).cbz.crdownload"   # temp download in progress
    ignored = "notes.tmp"                        # ignored extension
    real = "Series 006 (2024).cbz"

    _write(os.path.join(watch, temp))
    _write(os.path.join(watch, ignored))
    _write(os.path.join(watch, real))

    h.reconcile_directory()

    assert os.path.exists(os.path.join(watch, temp)), "temp file must remain"
    assert os.path.exists(os.path.join(watch, ignored)), "ignored file must remain"
    assert os.path.exists(_moved_path(target, real)), "real comic must move"
    assert not os.path.exists(os.path.join(watch, real))


def test_reconcile_missing_watch_dir_is_noop(handler):
    """A missing WATCH dir doesn't raise."""
    h, watch, target = handler
    h.directory = os.path.join(watch, "does-not-exist")
    h.reconcile_directory()  # must not raise


# --------------------- multipart / hybrid release unwrapping ---------------------

def _make_release(watch, name):
    rel = os.path.join(watch, name)
    os.makedirs(rel)
    _write(os.path.join(rel, "--bbyvt3ga.zip"))
    _write(os.path.join(rel, "--bb.nfo"))
    _write(os.path.join(rel, "file_id.diz"))
    return rel


def test_maybe_unwrap_moves_comic_and_cleans_cruft(handler, tmp_path, monkeypatch):
    """A multipart release is unwrapped: the emerged comic lands in TARGET under
    the cleaned release name, and the archive parts + cruft are deleted."""
    import monitor
    from helpers.unwrap import UnwrapResult
    from cbz_ops.rename import clean_directory_name

    h, watch, target = handler
    h.auto_unpack = True
    rel = _make_release(watch, "Europe.Comics-Pin.Up.10 (2022)")

    # Fake unwrap: emit a comic sitting in an isolated work dir.
    work = tmp_path / "work"
    work.mkdir()
    comic = work / "obfuscated.cbz"
    _write(str(comic))

    monkeypatch.setattr(monitor, "classify_release_folder",
                        lambda p: monitor.MULTIPART_ARCHIVE)
    monkeypatch.setattr(monitor, "unwrap_release",
                        lambda folder, root, **k: UnwrapResult([str(comic)], True, None, False, str(work)))

    handled = h._maybe_unwrap_release_folder(rel)

    assert handled is True
    base = clean_directory_name("Europe.Comics-Pin.Up.10 (2022)")
    assert os.path.exists(_moved_path(target, base + ".cbz")), "comic should reach TARGET"
    assert not os.path.exists(os.path.join(rel, "--bbyvt3ga.zip")), "parts deleted"
    assert not os.path.exists(str(work)), "work dir removed"
    assert h._in_flight_dirs == set(), "dir claim released"


def test_maybe_unwrap_failure_keeps_source(handler, tmp_path, monkeypatch):
    """When nothing emerges, the source folder is kept and put on cooldown."""
    import monitor
    from helpers.unwrap import UnwrapResult

    h, watch, target = handler
    h.auto_unpack = True
    rel = _make_release(watch, "Broken.Release")

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(monitor, "classify_release_folder",
                        lambda p: monitor.MULTIPART_ARCHIVE)
    monkeypatch.setattr(monitor, "unwrap_release",
                        lambda folder, root, **k: UnwrapResult([], False, "no_comics", False, str(work)))

    handled = h._maybe_unwrap_release_folder(rel)

    assert handled is True
    assert os.path.exists(os.path.join(rel, "--bbyvt3ga.zip")), "parts kept for recovery"
    assert os.path.abspath(rel) in h._failed_unwraps, "folder placed on cooldown"
    assert not os.path.exists(str(work)), "work dir removed even on failure"


def test_maybe_unwrap_disabled_when_autounpack_off(handler, monkeypatch):
    """AUTO_UNPACK off -> multipart folder is not claimed (normal loop handles it)."""
    import monitor
    h, watch, target = handler
    h.auto_unpack = False
    rel = _make_release(watch, "Some.Release")
    monkeypatch.setattr(monitor, "classify_release_folder",
                        lambda p: monitor.MULTIPART_ARCHIVE)
    assert h._maybe_unwrap_release_folder(rel) is False


def test_maybe_unwrap_real_zip_end_to_end(handler, monkeypatch):
    """End-to-end with real zip extraction (no RAR binary needed): a release of
    obfuscated zip parts carrying a .cbz is unwrapped, renamed, and moved to
    TARGET, with the parts cleaned up."""
    import zipfile
    import helpers.unwrap as U
    from cbz_ops.rename import clean_directory_name

    monkeypatch.setattr(U, "is_allowed_path", lambda p: True)  # avoid DB coupling

    h, watch, target = handler
    h.auto_unpack = True
    rel = os.path.join(watch, "Pin.Up 10 (2022)")
    os.makedirs(rel)
    with zipfile.ZipFile(os.path.join(rel, "--bbyvt3ga.zip"), "w") as z:
        z.writestr("TheComic.cbz", b"PK\x03\x04fake-comic")
    _write(os.path.join(rel, "--bb.nfo"))
    _write(os.path.join(rel, "file_id.diz"))

    handled = h._maybe_unwrap_release_folder(rel)

    assert handled is True
    base = clean_directory_name("Pin.Up 10 (2022)")
    assert os.path.exists(_moved_path(target, base + ".cbz")), "unwrapped comic in TARGET"
    assert not os.path.exists(os.path.join(rel, "--bbyvt3ga.zip")), "part cleaned up"


def test_process_file_normal_zip_still_unzips(handler, monkeypatch):
    """A lone, normally-named zip in a subfolder is NOT treated as multipart —
    it still goes through the plain auto_unpack unzip path."""
    h, watch, target = handler
    h.auto_unpack = True
    sub = os.path.join(watch, "Batman (2024)")
    os.makedirs(sub)
    zpath = os.path.join(sub, "Batman 001 (2024).zip")
    _write(zpath)

    calls = []
    monkeypatch.setattr(h, "unzip_file", lambda p: calls.append(p))

    h._process_file(zpath)

    assert calls == [zpath], "normal single zip must still unzip, not route to unwrap"


def test_file_under_in_flight_dir_is_skipped(handler, monkeypatch):
    """A file inside a release folder being unwrapped is not processed by the
    per-file loop (regression guard for 'parts moved individually')."""
    h, watch, target = handler
    rel = os.path.join(watch, "release")
    os.makedirs(rel)
    part = os.path.join(rel, "--bbyvt3ga.zip")
    _write(part)

    called = []
    monkeypatch.setattr(h, "_process_file", lambda fp: called.append(fp))
    h._in_flight_dirs.add(os.path.abspath(rel))

    h._handle_file_if_complete(part)

    assert called == [], "file under in-flight unwrap dir must not be processed"
    assert os.path.exists(part), "part must stay put"
