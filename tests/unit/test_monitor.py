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
import time

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


def test_maybe_unwrap_real_zip_end_to_end(handler, monkeypatch):
    """End-to-end with real zip extraction (no RAR binary needed): a release of
    obfuscated zip parts carrying a .cbz is unwrapped, renamed, and moved to
    TARGET, with the parts cleaned up."""
    import zipfile
    import helpers.unwrap as U
    from cbz_ops.rename import clean_directory_name

    monkeypatch.setattr(U, "is_allowed_path", lambda p: True)  # avoid DB coupling

    h, watch, target = handler
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


def _write_zip(path, names):
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, b"x" * 16)
    return path


def test_process_file_pack_of_comics_unzips(handler, monkeypatch):
    """A lone, normally-named zip of comics in a subfolder is NOT treated as
    multipart — it goes through the plain unzip path."""
    h, watch, target = handler
    sub = os.path.join(watch, "Batman (2024)")
    os.makedirs(sub)
    zpath = _write_zip(os.path.join(sub, "Batman 001-002 (2024).zip"),
                       ["Batman 001.cbz", "Batman 002.cbz"])

    calls = []
    monkeypatch.setattr(h, "unzip_file", lambda p: calls.append(p))

    h._process_file(zpath)

    assert calls == [zpath], "a pack of comics must unzip, not route to unwrap"


def test_process_file_zip_of_pages_becomes_cbz(handler, monkeypatch):
    """A zip whose members are page images IS the comic: rename, never explode
    it into loose images the pipeline would then move one page at a time."""
    h, watch, target = handler
    zpath = _write_zip(os.path.join(watch, "Batman 001 (2024).zip"),
                       ["001.jpg", "002.jpg", "003.jpg"])

    monkeypatch.setattr(h, "unzip_file",
                        lambda p: pytest.fail("a comic archive must not be unzipped"))
    moved = []
    monkeypatch.setattr(h, "_move_file", lambda p: moved.append(p))

    h._process_file(zpath)

    cbz = os.path.join(watch, "Batman 001 (2024).cbz")
    assert os.path.exists(cbz), "zip of pages renamed to .cbz"
    assert not os.path.exists(zpath), "original zip is gone"
    assert moved == [cbz], "the new comic re-enters the normal pipeline"


def test_new_comic_is_claimed_while_it_is_processed(handler, monkeypatch):
    """Renaming a zip to .cbz creates a fresh path in WATCH, so its own watchdog
    event can land mid-processing. It must be in-flight while we handle it, or
    both threads move the same file."""
    h, watch, target = handler
    zpath = _write_zip(os.path.join(watch, "Batman 001 (2024).zip"), ["001.jpg"])
    cbz = os.path.abspath(os.path.join(watch, "Batman 001 (2024).cbz"))

    seen = {}
    monkeypatch.setattr(h, "_move_file",
                        lambda p: seen.update(claimed=cbz in h._in_flight))

    h._process_file(zpath)

    assert seen.get("claimed") is True, "the derived comic must be claimed"
    assert cbz not in h._in_flight, "and released afterwards"


def test_process_file_unreadable_zip_falls_back_to_extract(handler, monkeypatch):
    """An archive we cannot classify is still opened — never left in WATCH."""
    h, watch, target = handler
    zpath = os.path.join(watch, "Mystery.zip")
    _write(zpath)          # not a real zip: the listing fails

    calls = []
    monkeypatch.setattr(h, "unzip_file", lambda p: calls.append(p))

    h._process_file(zpath)

    assert calls == [zpath]


def test_process_file_rar_of_pages_converts_to_cbz(handler, monkeypatch):
    """A loose .rar of pages goes to the existing RAR->CBZ conversion, not to a
    blind extraction that would strand its pages in WATCH."""
    import monitor
    h, watch, target = handler
    rpath = os.path.join(watch, "Batman 002 (2024).rar")
    _write(rpath)
    cbz = os.path.join(watch, "Batman 002 (2024).cbz")

    monkeypatch.setattr(monitor, "classify_archive", lambda p: monitor.COMIC_ARCHIVE)
    monkeypatch.setattr(h, "_unrar_file",
                        lambda p: pytest.fail("a comic archive must not be extracted"))

    def _convert(path):
        os.remove(path)
        _write(cbz)
        return True
    monkeypatch.setattr(monitor, "convert_to_cbz", _convert)
    moved = []
    monkeypatch.setattr(h, "_move_file", lambda p: moved.append(p))

    h._process_file(rpath)

    assert os.path.exists(cbz)
    assert moved == [cbz]


def test_process_file_rar_pack_extracts_and_deletes(handler, monkeypatch):
    """A .rar holding ready comics is extracted beside itself and removed."""
    import monitor
    h, watch, target = handler
    rpath = os.path.join(watch, "Pack.rar")
    _write(rpath)

    monkeypatch.setattr(monitor, "classify_archive", lambda p: monitor.PACKED_COMICS)

    def _extract(src, out_dir):
        _write(os.path.join(out_dir, "Batman 003.cbz"))
        return True, 0
    monkeypatch.setattr(monitor, "extract_rar_with_unar", _extract)

    h._process_file(rpath)

    assert os.path.exists(os.path.join(watch, "Batman 003.cbz"))
    assert not os.path.exists(rpath), "archive removed once extraction succeeded"


def test_failed_rar_extraction_keeps_the_archive(handler, monkeypatch):
    """A .rar that could not be opened stays put, so nothing is lost."""
    import monitor
    h, watch, target = handler
    rpath = os.path.join(watch, "Broken.rar")
    _write(rpath)

    monkeypatch.setattr(monitor, "classify_archive", lambda p: monitor.PACKED_COMICS)
    monkeypatch.setattr(monitor, "extract_rar_with_unar", lambda *a, **k: (False, 0))

    h._process_file(rpath)

    assert os.path.exists(rpath)


def test_rar_in_a_release_folder_defers_to_the_unwrapper(handler, monkeypatch):
    """A .rar that is one part of a multipart release must not be opened alone —
    the folder-level unwrapper owns it."""
    import monitor
    h, watch, target = handler
    rel = os.path.join(watch, "Some.Release")
    os.makedirs(rel)
    rpath = os.path.join(rel, "--bbyvt3ga.rar")
    _write(rpath)

    monkeypatch.setattr(h, "_maybe_unwrap_release_folder", lambda p: True)
    monkeypatch.setattr(monitor, "classify_archive",
                        lambda p: pytest.fail("must not classify a release part"))

    h._process_file(rpath)

    assert os.path.exists(rpath), "the part is left for the unwrapper"


def test_strip_comic_extension():
    """Only real comic extensions come off; ordinary folder names are untouched."""
    import monitor
    assert monitor.strip_comic_extension("Heavy Metal 5.cbz") == "Heavy Metal 5"
    assert monitor.strip_comic_extension("Batman 001.CBR") == "Batman 001"
    assert monitor.strip_comic_extension("Batman (2024)") == "Batman (2024)"
    assert monitor.strip_comic_extension("Saga Vol. 1") == "Saga Vol. 1"
    assert monitor.strip_comic_extension("") == ""


def test_consolidate_strips_comic_extension_from_job_folder(handler):
    """A download client's job folder named after the requested comic file
    ("Heavy Metal 5.cbz") must not become a ".cbz" series folder in TARGET —
    and the trailing issue number must still be stripped."""
    h, watch, target = handler
    h.consolidate_directories = True
    job_dir = os.path.join(watch, "Heavy Metal 5.cbz")
    os.makedirs(job_dir)
    name = "Heavy Metal 005.cbz"
    _write(os.path.join(job_dir, name))

    h._move_file(os.path.join(job_dir, name))

    assert os.path.exists(_moved_path(os.path.join(target, "Heavy Metal"), name)), \
        "should land in a 'Heavy Metal' series folder"
    assert not os.path.exists(os.path.join(target, "Heavy Metal 5.cbz")), \
        "TARGET must not gain a directory with a comic extension"


def test_move_directories_strips_comic_extension_from_job_folder(handler):
    """The same job folder under move_directories keeps its structure but loses
    the bogus comic extension."""
    h, watch, target = handler
    h.move_directories = True
    job_dir = os.path.join(watch, "Heavy Metal 5.cbz")
    os.makedirs(job_dir)
    name = "Heavy Metal 005.cbz"
    _write(os.path.join(job_dir, name))

    h._move_file(os.path.join(job_dir, name))

    assert os.path.exists(_moved_path(os.path.join(target, "Heavy Metal 5"), name))
    assert not os.path.exists(os.path.join(target, "Heavy Metal 5.cbz"))


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


# ---------------------------------------------------------------------------
# Conversion scratch dirs
#
# cbz_ops writes its extraction dir next to the file being converted, so when
# api.py converts a fresh download it lands inside WATCH. Those are extracted
# pages, not downloads: moving them litters TARGET with loose images *and*
# strip-mines the CBZ still being assembled. ".temp_*" is the current name,
# bare "temp_*" survives from older versions and crashed conversions.
# ---------------------------------------------------------------------------

def _jpgs_under(root):
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith(".jpg"):
                found.append(os.path.join(dirpath, f))
    return found


def test_reconcile_never_moves_pages_from_conversion_scratch_dir(handler):
    """The headline regression: extraction pages must never reach TARGET.

    Covers both the current hidden name and the legacy bare one, and proves a
    real comic sitting beside them is still drained.
    """
    h, watch, target = handler

    hidden_scratch = os.path.join(watch, ".temp_Series 007 (2024)")
    legacy_scratch = os.path.join(watch, "temp_Series 008 (2024)")
    os.makedirs(hidden_scratch)
    os.makedirs(legacy_scratch)
    hidden_page = os.path.join(hidden_scratch, "page001.jpg")
    legacy_page = os.path.join(legacy_scratch, "page001.jpg")
    _write(hidden_page, b"jpeg-bytes")
    _write(legacy_page, b"jpeg-bytes")

    real = "Series 009 (2024).cbz"
    _write(os.path.join(watch, real))

    h.reconcile_directory()

    assert os.path.exists(hidden_page), "page in .temp_* must stay put"
    assert os.path.exists(legacy_page), "page in legacy temp_* must stay put"
    assert _jpgs_under(target) == [], "no loose images may reach TARGET"
    assert os.path.exists(_moved_path(target, real)), "real comic still drains"


def test_live_event_inside_scratch_dir_is_ignored(handler, monkeypatch):
    """Guards the live-event path, which the dirs[:] prunes do not cover.

    PollingObserver snapshots hidden directories, and is_hidden() only inspects a
    basename -- so on_created for ".temp_Foo/page001.jpg" arrives here with a
    perfectly ordinary-looking filename.
    """
    h, watch, target = handler
    scratch = os.path.join(watch, ".temp_Series 007 (2024)")
    os.makedirs(scratch)
    page = os.path.join(scratch, "page001.jpg")
    _write(page, b"jpeg-bytes")

    called = []
    monkeypatch.setattr(h, "_process_file", lambda fp: called.append(fp))

    h._handle_file_if_complete(page)

    assert called == [], "page under a scratch dir must not be processed"
    assert os.path.exists(page)


def test_live_event_inside_legacy_scratch_dir_is_ignored(handler, monkeypatch):
    """Same guard for the pre-fix, non-hidden scratch name."""
    h, watch, target = handler
    scratch = os.path.join(watch, "temp_Series 008 (2024)")
    os.makedirs(scratch)
    page = os.path.join(scratch, "page001.jpg")
    _write(page, b"jpeg-bytes")

    called = []
    monkeypatch.setattr(h, "_process_file", lambda fp: called.append(fp))

    h._handle_file_if_complete(page)

    assert called == [], "page under a legacy scratch dir must not be processed"
    assert os.path.exists(page)


def test_live_event_inside_hidden_dir_is_ignored(handler, monkeypatch):
    """The unwrap staging root (.clu_unwrap) had the same hole."""
    h, watch, target = handler
    staging = os.path.join(watch, ".clu_unwrap", "job1")
    os.makedirs(staging)
    page = os.path.join(staging, "page001.jpg")
    _write(page, b"jpeg-bytes")

    called = []
    monkeypatch.setattr(h, "_process_file", lambda fp: called.append(fp))

    h._handle_file_if_complete(page)

    assert called == [], "file under a hidden dir must not be processed"
    assert os.path.exists(page)


def test_scan_directory_prunes_scratch_dirs(handler, monkeypatch):
    """The READ_SUBDIRECTORIES walk must not descend into scratch dirs."""
    h, watch, target = handler
    scratch = os.path.join(watch, ".temp_Series 007 (2024)")
    os.makedirs(scratch)
    _write(os.path.join(scratch, "page001.jpg"), b"jpeg-bytes")

    seen = []
    monkeypatch.setattr(h, "_handle_file_if_complete", lambda fp: seen.append(fp))

    h._scan_directory(watch)

    assert seen == [], "scan must not surface scratch-dir contents"


def test_ordinary_subdirectory_is_still_processed(handler):
    """The scratch-dir prune must not swallow normal release folders."""
    h, watch, target = handler
    sub = os.path.join(watch, "Series 010 (2024)")
    os.makedirs(sub)
    name = "Series 010 (2024).cbz"
    _write(os.path.join(sub, name))

    h.reconcile_directory()

    assert not os.path.exists(os.path.join(sub, name)), "file should leave WATCH"
    assert os.path.exists(_moved_path(target, name))


def test_is_scratch_dir_matching():
    """Prefix-only, case-insensitive, hidden or not."""
    import monitor

    assert monitor.is_scratch_dir("temp_Batman 001") is True
    assert monitor.is_scratch_dir(".temp_Batman 001") is True
    assert monitor.is_scratch_dir("TEMP_Batman 001") is True
    assert monitor.is_scratch_dir(os.path.join("a", "b", ".temp_x")) is True
    # Not scratch: the word has to be the prefix.
    assert monitor.is_scratch_dir("Temperature Rising 001") is False
    assert monitor.is_scratch_dir("my_temp_folder") is False
    assert monitor.is_scratch_dir("") is False
    assert monitor.is_scratch_dir(None) is False


def test_monitor_claims_loose_rar(handler, monkeypatch):
    """A loose .rar is claimed for unpacking even though it is on the ignore list.

    .zip and .rar ship on IGNORED_EXTENSIONS because they are not comics to move,
    but unpacking is unconditional, so the ignore list must not stop the monitor
    from opening one. core.download_utils.monitor_claims mirrors this decision for
    api.py's hand-off; the two must not drift apart.
    """
    from core.download_utils import monitor_claims

    h, watch, target = handler
    h.ignored_extensions = {".crdownload", ".rar", ".zip"}
    name = "Series 011 (2024).rar"
    _write(os.path.join(watch, name))

    seen = []
    monkeypatch.setattr(h, "_process_archive", lambda p: seen.append(p))

    h.reconcile_directory()

    assert seen == [os.path.join(watch, name)], "monitor must claim a loose .rar"
    assert monitor_claims(name, ".crdownload,.rar,.zip") is True


# ---------------------------------------------------------------------------
# The CBR -> CBZ conversion branch (previously untested: the base fixture
# disables autoconvert).
# ---------------------------------------------------------------------------

@pytest.fixture
def converting_handler(handler, monkeypatch):
    """handler with autoconvert on and convert_to_cbz faked.

    The fake records the path it was handed and performs the real observable
    effect (write <base>.cbz, drop the .cbr) so no unar binary is needed.
    """
    import monitor

    h, watch, target = handler
    h.autoconvert = True
    calls = []

    def fake_convert(path):
        calls.append(path)
        cbz = os.path.splitext(path)[0] + ".cbz"
        _write(cbz, b"converted")
        if os.path.exists(path):
            os.remove(path)
        return True

    monkeypatch.setattr(monitor, "convert_to_cbz", fake_convert)
    return h, watch, target, calls


def test_cbr_converted_after_move_into_target(converting_handler):
    """The monitor converts in TARGET, never in WATCH.

    This is why the monitor's own conversion was never part of the race: its
    scratch dir lands in TARGET, which nothing watches.
    """
    h, watch, target, calls = converting_handler
    name = "Series 012 (2024).cbr"
    _write(os.path.join(watch, name))

    h.reconcile_directory()

    assert len(calls) == 1, "must convert exactly once"
    converted = os.path.abspath(calls[0])
    assert not converted.startswith(os.path.abspath(watch) + os.sep), \
        "conversion must not run inside WATCH"
    assert os.path.exists(_moved_path(target, "Series 012 (2024).cbz"))
    assert not os.path.exists(os.path.join(watch, name)), "WATCH should drain"


def test_existing_cbz_in_target_skips_conversion(converting_handler):
    """If the CBZ is already there, don't convert again."""
    h, watch, target, calls = converting_handler
    name = "Series 013 (2024).cbr"
    _write(os.path.join(watch, name))
    from cbz_ops.rename import clean_directory_name
    os.makedirs(clean_directory_name(target), exist_ok=True)
    _write(_moved_path(target, "Series 013 (2024).cbz"), b"already-there")

    h.reconcile_directory()

    assert calls == [], "must not re-convert an existing CBZ"


def test_empty_cbr_not_converted(converting_handler):
    """A zero-byte CBR is skipped rather than fed to the converter."""
    h, watch, target, calls = converting_handler
    name = "Series 014 (2024).cbr"
    _write(os.path.join(watch, name), b"")

    h.reconcile_directory()

    assert calls == [], "must not convert an empty file"


# ---------------------------------------------------------------------------
# Orphan cleanup must not delete a download that is still running
# ---------------------------------------------------------------------------

def test_orphan_cleanup_leaves_an_active_download_alone(handler):
    """The sweep had no age check and ran hourly, so it deleted in-flight
    downloads: 64 MB and 32 MB mid-transfer in one reported case. The writer
    kept filling its unlinked handle and then failed at the final rename with
    ENOENT -- a retryable failure -- so the download was re-queued under a fresh
    name and killed again an hour later, sixteen times over for one pack."""
    h, watch, _ = handler
    active = os.path.join(watch, "Big Pack.zip.0.crdownload")
    _write(active, b"x" * 1024)

    h.cleanup_orphan_files()

    assert os.path.exists(active), (
        "cleanup deleted a temp file that was just written to — the download "
        "owning it is still running"
    )


def test_orphan_cleanup_still_removes_an_abandoned_temp_file(handler):
    """The grace period must not turn the sweep into a no-op."""
    import monitor

    h, watch, _ = handler
    stale = os.path.join(watch, "Abandoned.cbz.0.crdownload")
    _write(stale)
    old = time.time() - (monitor.ORPHAN_MIN_AGE_SECONDS + 60)
    os.utime(stale, (old, old))

    h.cleanup_orphan_files()

    assert not os.path.exists(stale)


def test_orphan_cleanup_grace_period_is_a_positive_number():
    """A zero or negative value silently restores the original bug."""
    import monitor

    assert monitor.ORPHAN_MIN_AGE_SECONDS > 0


def test_orphan_cleanup_never_touches_finished_comics(handler):
    """Only temporary download files are in scope, at any age."""
    h, watch, _ = handler
    comic = os.path.join(watch, "Real Comic.cbz")
    _write(comic)
    old = time.time() - 86400
    os.utime(comic, (old, old))

    h.cleanup_orphan_files()

    assert os.path.exists(comic)


# ---------------------------------------------------------------------------
# AirDC++ .dctmp partials, and the copy a failed move used to leave in TARGET
#
# One AirDC++ download of "Transformers 036 (2026) (Digital)
# (Mephisto-Empire).cbr.dctmp" was processed 28 times in 20 minutes and left 28
# copies of itself in TARGET, named " (1).dctmp" through " (28).dctmp". Three
# independent defects had to line up, and each gets its own test below.
# ---------------------------------------------------------------------------

DCTMP = "Transformers 036 (2026) (Digital) (Mephisto-Empire).cbr.dctmp"


def test_dctmp_is_recognised_as_a_partial_download():
    """Defect 1: AirDC++'s partial slipped the temp-file gate entirely."""
    from core.download_utils import is_temporary_download_file

    assert is_temporary_download_file(DCTMP)
    assert is_temporary_download_file("/downloads/temp/" + DCTMP)


def test_dot_tmp_does_not_match_dctmp():
    """Why '.dctmp' needs its own entry and must not be "simplified" away.

    The pattern list already held '.tmp' and the match is substring-anywhere,
    so it looks as though .dctmp were covered. It is not: there is no dot
    immediately before "tmp" in ".dctmp". That single character is the whole
    bug.
    """
    assert ".tmp" not in DCTMP.lower()


def test_reconcile_skips_a_dctmp_partial(handler):
    """The gate is the one thing that can catch this.

    Both of the monitor's completion checks are size-stability only, and
    AirDC++ *preallocates* the .dctmp at its full final size -- so a live
    transfer reads as "complete" within seconds of first being seen. No
    stability heuristic can detect it; the name is the only evidence.
    """
    h, watch, target = handler
    _write(os.path.join(watch, DCTMP))

    h.reconcile_directory()

    assert os.path.exists(os.path.join(watch, DCTMP)), \
        "a live AirDC++ transfer must be left alone"
    assert os.listdir(target) == [], "nothing may reach TARGET"


def test_orphan_cleanup_never_deletes_a_dctmp(handler):
    """Recognising .dctmp must not also make CLU delete it.

    The same predicate decides what the orphan sweep may remove. A DC++ queue
    item legitimately sits idle for hours waiting for a source slot, so the
    mtime rule reads live queue state as abandoned -- and deleting it makes
    AirDC++ start the transfer over. "Must not process" and "safe to reap" are
    two different questions.
    """
    import monitor

    h, watch, _ = handler
    partial = os.path.join(watch, DCTMP)
    _write(partial)
    old = time.time() - (monitor.ORPHAN_MIN_AGE_SECONDS + 3600)
    os.utime(partial, (old, old))

    h.cleanup_orphan_files()

    assert os.path.exists(partial), (
        "an AirDC++ partial belongs to another client's queue; CLU must never "
        "reap it, however old it looks"
    )


def test_monitor_claims_refuses_a_dctmp():
    """core.download_utils.monitor_claims promises lockstep with the monitor."""
    from core.download_utils import monitor_claims

    assert monitor_claims(DCTMP, ".crdownload,.tmp,.zip,.rar") is False


# ---------------------------------------------------------------------------
# Defect 2: a failed move left a full copy in TARGET
# ---------------------------------------------------------------------------

def _locked_move(src, dst):
    """What CPython's shutil.move does when os.rename hits EACCES.

    It falls back to copy2() + unlink() on ANY OSError, not just EXDEV, so the
    copy succeeds and only the unlink of the *source* fails.
    """
    import shutil
    shutil.copy2(src, dst)
    raise PermissionError(13, "Permission denied", src)


def test_failed_move_leaves_no_copy_in_target(tmp_path, monkeypatch):
    """The copy2 half of shutil.move's fallback had already run."""
    import monitor

    src = tmp_path / "Comic 001.cbz"
    dst = tmp_path / "out" / "Comic 001.cbz"
    dst.parent.mkdir()
    _write(str(src))

    monkeypatch.setattr(monitor.shutil, "move", _locked_move)

    with pytest.raises(PermissionError):
        monitor.move_download(str(src), str(dst))

    assert not dst.exists(), "a partial copy was left in TARGET"
    assert src.exists(), "the source must be untouched"


def test_repeated_failed_moves_do_not_accumulate_numbered_copies(tmp_path, monkeypatch):
    """The exact reported symptom: ' (1)', ' (2)', ' (3)' ... without bound.

    get_unique_filepath is a pure os.path.exists scan with no memory, so every
    copy left behind by the previous failure pushes the next one to a new name.
    """
    import monitor

    watch = tmp_path / "watch"
    target = tmp_path / "target"
    watch.mkdir()
    target.mkdir()
    src = watch / DCTMP
    _write(str(src))

    monkeypatch.setattr(monitor.shutil, "move", _locked_move)

    for _ in range(5):
        dst = monitor.get_unique_filepath(str(target / DCTMP))
        with pytest.raises(PermissionError):
            monitor.move_download(str(src), dst)

    assert os.listdir(str(target)) == [], (
        "five failed moves must leave TARGET empty; before the fix they left "
        "the original plus ' (1)' through ' (4)'"
    )


def test_move_download_still_moves_a_healthy_file(tmp_path):
    """The cleanup must not break the ordinary path."""
    import monitor

    src = tmp_path / "Comic 002.cbz"
    dst = tmp_path / "out" / "Comic 002.cbz"
    dst.parent.mkdir()
    _write(str(src))

    monitor.move_download(str(src), str(dst))

    assert dst.exists() and not src.exists()


def test_move_download_keeps_a_file_it_did_not_create(tmp_path, monkeypatch):
    """Cleanup is only safe because the source proves the move never completed.

    If the source is gone the move did happen, so whatever sits at the
    destination is the real file and must survive a later error.
    """
    import monitor

    src = tmp_path / "Comic 003.cbz"
    dst = tmp_path / "out" / "Comic 003.cbz"
    dst.parent.mkdir()
    _write(str(src))

    # Captured before the patch: monitor.shutil IS the shutil module, so a
    # lookup inside the stand-in would resolve back to the stand-in.
    real_shutil_move = monitor.shutil.move

    def move_then_fail(s, d):
        real_shutil_move(s, d)     # genuinely succeeds; source is gone
        raise OSError("something else went wrong afterwards")

    monkeypatch.setattr(monitor.shutil, "move", move_then_fail)

    with pytest.raises(OSError):
        monitor.move_download(str(src), str(dst))

    assert dst.exists(), "a completed move must not be undone by the cleanup"


# ---------------------------------------------------------------------------
# Defect 3: no failure memory -- 28 identical ERRORs in 20 minutes
# ---------------------------------------------------------------------------

def test_a_failed_move_backs_off_before_retrying(handler, monkeypatch):
    """The second sweep must not re-attempt immediately."""
    import monitor

    h, watch, target = handler
    src = os.path.join(watch, "Comic 004.cbz")
    _write(src)

    attempts = []

    def failing_move(s, d):
        attempts.append(s)
        raise PermissionError(13, "Permission denied", s)

    monkeypatch.setattr(monitor, "move_download", failing_move)

    h._move_file(src)
    assert len(attempts) == 1

    h._move_file(src)
    assert len(attempts) == 1, "a file in backoff must not be retried"


def test_backoff_clears_when_the_file_changes(handler, monkeypatch):
    """A file that changed deserves a fresh attempt.

    This is why the backoff is keyed on (mtime, size) rather than on the path:
    the real .dctmp case ends with AirDC++ rewriting the file, and a path-only
    key would hold the finished comic back for the whole cooldown.
    """
    import monitor

    h, watch, target = handler
    src = os.path.join(watch, "Comic 005.cbz")
    _write(src)

    attempts = []

    def failing_move(s, d):
        attempts.append(s)
        raise PermissionError(13, "Permission denied", s)

    monkeypatch.setattr(monitor, "move_download", failing_move)

    h._move_file(src)
    assert len(attempts) == 1

    _write(src, b"different-and-longer-content")
    h._move_file(src)
    assert len(attempts) == 2, "changed content must clear the backoff"


def test_a_successful_move_clears_the_backoff(handler, monkeypatch):
    """Nothing may linger in _failed_moves once the file has gone."""
    import monitor

    h, watch, target = handler
    src = os.path.join(watch, "Comic 006.cbz")
    _write(src)

    calls = {"n": 0}
    real_move = monitor.move_download

    def flaky_move(s, d):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "Permission denied", s)
        return real_move(s, d)

    monkeypatch.setattr(monitor, "move_download", flaky_move)

    h._move_file(src)
    assert h._failed_moves, "the failure should be remembered"

    h._failed_moves.clear()          # simulate the cooldown expiring
    h._move_file(src)

    assert h._failed_moves == {}, "a successful move must clear the record"


def test_failed_rename_is_not_logged_as_no_rename_needed(handler, monkeypatch):
    """_rename_file returned None for both outcomes.

    A permission error on the source therefore logged the reassuring
    "No rename needed for: ...", which is what made the real incident so hard
    to read: the log claimed the filename was fine while the rename was in fact
    failing on every pass.
    """
    import monitor

    h, watch, target = handler
    src = os.path.join(watch, "Comic 007.cbz")
    _write(src)

    def boom(path):
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(monitor, "rename_file", boom)

    assert h._rename_file(src) is monitor.RENAME_FAILED

    # ... and the file is still moved rather than stranded in WATCH.
    h.auto_rename_monitor = True
    h._process_file(src)
    assert os.path.exists(_moved_path(target, "Comic 007.cbz"))


def test_failed_conversion_that_wrote_a_cbz_is_not_reported_as_converted(
        converting_handler, monkeypatch, caplog):
    """A conversion can write a complete CBZ and still fail.

    convert_to_cbz deliberately keeps the source .cbr when that happens, so the
    monitor must branch on its return value -- not on os.path.exists(<base>.cbz),
    which answers a different question and answers it "yes". Testing the
    filesystem is what produced these two lines, one second apart, for every
    download in a library:

        app.log     ERROR  Failed to convert ... 001.cbr: [Errno 1] ...001.cbz
        monitor.log INFO   Converted to: /downloads/processed/...001.cbz

    Nothing retried, and the CBR stayed beside its CBZ in TARGET forever.
    """
    import logging
    import monitor

    h, watch, target, calls = converting_handler

    def wrote_then_failed(path):
        calls.append(path)
        _write(os.path.splitext(path)[0] + ".cbz", b"a complete cbz")
        return False

    # Overrides the fixture's own fake; same function-scoped monkeypatch, so
    # this wins and is undone the same way.
    monkeypatch.setattr(monitor, "convert_to_cbz", wrote_then_failed)

    _write(os.path.join(watch, "Series 021 (2024).cbr"))
    with caplog.at_level(logging.INFO):
        h.reconcile_directory()

    assert calls, "the conversion must still be attempted"
    cbr_in_target = _moved_path(target, "Series 021 (2024).cbr")
    assert os.path.exists(cbr_in_target), \
        "a failed conversion must leave the source where it is"
    assert "Converted to:" not in caplog.text, \
        "a failed conversion must never be logged as a success"
    assert "Problem Files" in caplog.text, \
        "the leftover pair must be reported somewhere the user can see it"


def test_archive_of_pages_is_not_handed_on_when_conversion_fails(
        handler, monkeypatch):
    """_archive_to_comic has the same trap: exists() != converted.

    Handing the CBZ on while the source .rar is still there puts both into
    TARGET.
    """
    import monitor

    h, watch, target = handler
    rar = os.path.join(watch, "Batman 003 (2024).rar")
    _write(rar)

    def wrote_then_failed(path):
        _write(os.path.splitext(path)[0] + ".cbz", b"a complete cbz")
        return False

    monkeypatch.setattr(monitor, "convert_to_cbz", wrote_then_failed)

    assert h._archive_to_comic(rar, ".rar") is None
