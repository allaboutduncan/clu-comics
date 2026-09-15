"""Filing a downloaded replacement onto the damaged file it replaces.

This is the one part of the Problem Files feature that destroys data, so the
safety properties are pinned hardest: a replacement that is itself damaged must
never be swapped in, and the file it replaces must always be recoverable.
"""

import io
import os
import zipfile
from unittest.mock import patch

import pytest
from PIL import Image

from core.problem_replacements import (
    STATUS_APPLIED,
    STATUS_FAILED,
    STATUS_PENDING,
    acknowledge,
    apply_pending,
    cancel,
    claim_replacement,
    get_replacement,
    list_replacements,
    verify_replacement,
)


def _rar_bytes(size=4000):
    """Minimal RAR-looking payload.

    Built from code points rather than escape literals: a raw control byte in a
    source file makes the whole module unparseable and is invisible in review.
    """
    return bytes([0x52, 0x61, 0x72, 0x21, 0x1A, 0x07, 0x00]) + bytes(size)


def _page():
    buf = io.BytesIO()
    Image.new("RGB", (400, 600), (40, 90, 140)).save(buf, format="JPEG")
    return buf.getvalue()


def _good_cbz(path, pages=("001.jpg", "002.jpg")):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in pages:
            zf.writestr(name, _page())
    return path


def _bad_crc_cbz(path):
    """A structurally valid ZIP whose entry data fails its checksum."""
    _good_cbz(path)
    raw = bytearray(path.read_bytes())
    start = len(raw) // 2
    for i in range(start, min(start + 400, len(raw) - 120)):
        raw[i] ^= 0xFF
    path.write_bytes(bytes(raw))
    return path


@pytest.fixture
def library(tmp_path):
    """A series folder holding the damaged issue, and a TARGET beside it."""
    series_dir = tmp_path / "data" / "DC Comics" / "Tales of the Unexpected" / "v2006"
    series_dir.mkdir(parents=True)
    damaged = series_dir / "Tales of the Unexpected 008 (2007).cbz"
    _bad_crc_cbz(damaged)

    target = tmp_path / "downloads" / "processed"
    target.mkdir(parents=True)

    trash = tmp_path / "trash"
    trash.mkdir()
    return {"series_dir": series_dir, "damaged": damaged, "target": target,
            "trash": trash}


@pytest.fixture
def store(db_connection, db_path):
    with patch("core.database.get_db_path", return_value=db_path):
        yield


@pytest.fixture
def no_aliases():
    """match_wanted_issues_to_files' alias lookup is injectable; keep it off DB."""
    return lambda name: ""


class _CacheDirOnly:
    """Stand-in for core.config.config, swapped onto thumbnail_cache only.

    Patching `.get` on the real ConfigParser would mutate the object every other
    module shares. Same shape as the one in tests/unit/test_thumbnail_cache.py.
    """

    def __init__(self, root):
        self._root = root

    def get(self, section, option, fallback=None, **kwargs):
        if (section, option) == ("SETTINGS", "CACHE_DIR"):
            return self._root
        return fallback


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    """Point the thumbnail cache at tmp_path for every test in this module.

    A successful swap refreshes the replaced file's thumbnail, and an
    unwritable cache is recorded as a ThumbnailCacheUnwritable problem against
    that file -- correctly: the comic is healthy and the cache is not. Left on
    the default /cache, which no CI runner can write to, that new row lands on
    the path the swap just cleared, and "the problem entry is cleared" fails on
    a machine where nothing is wrong with the swap.
    """
    import core.thumbnail_cache as thumbnail_cache

    root = tmp_path / "cache"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(thumbnail_cache, "config", _CacheDirOnly(str(root)))
    return root


PATTERN = "{series_name} {issue_number}"


class TestVerification:
    def test_a_healthy_archive_passes(self, tmp_path):
        ok, why = verify_replacement(str(_good_cbz(tmp_path / "ok.cbz")))
        assert ok is True, why

    def test_a_bad_crc_replacement_is_refused(self, tmp_path):
        ok, why = verify_replacement(str(_bad_crc_cbz(tmp_path / "bad.cbz")))
        assert ok is False
        assert "damaged" in why

    def test_an_archive_with_no_pages_is_refused(self, tmp_path):
        p = tmp_path / "empty.cbz"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("ComicInfo.xml", "<ComicInfo/>")
        ok, why = verify_replacement(str(p))
        assert ok is False
        assert "no page images" in why

    def test_an_empty_file_is_refused(self, tmp_path):
        p = tmp_path / "zero.cbz"
        p.write_bytes(b"")
        ok, why = verify_replacement(str(p))
        assert ok is False

    def test_a_missing_file_is_refused(self, tmp_path):
        ok, why = verify_replacement(str(tmp_path / "nope.cbz"))
        assert ok is False

    def test_a_rar_is_accepted_rather_than_blocked(self, tmp_path):
        """CLU has no CRC check for RAR; refusing them would reject good
        downloads that the WATCH pipeline converts anyway."""
        p = tmp_path / "book.cbr"
        p.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 400)
        ok, _ = verify_replacement(str(p))
        assert ok is True


class TestClaims:
    def test_claim_and_read_back(self, store, library):
        target = str(library["damaged"])
        assert claim_replacement(target, series="Tales of the Unexpected",
                                 issue="8", query="Tales of the Unexpected 8") is True
        row = get_replacement(target)
        assert row["status"] == STATUS_PENDING
        assert row["series"] == "Tales of the Unexpected"

    def test_reclaiming_clears_a_previous_failure(self, store, library):
        """A user trying a different result must not be blocked by the last try."""
        target = str(library["damaged"])
        claim_replacement(target, series="S", issue="8")
        from core.problem_replacements import _set_status

        _set_status(target, STATUS_FAILED, detail="the replacement is damaged too")
        claim_replacement(target, series="S", issue="8")

        row = get_replacement(target)
        assert row["status"] == STATUS_PENDING
        assert row["detail"] is None

    def test_cancel_and_acknowledge(self, store, library):
        target = str(library["damaged"])
        claim_replacement(target, series="S", issue="8")
        assert acknowledge(target) is True
        assert list_replacements() == []
        assert list_replacements(include_acknowledged=True)
        assert cancel(target) is True
        assert get_replacement(target) is None


class TestApply:
    def test_a_good_replacement_is_swapped_in(self, store, library, no_aliases):
        damaged = library["damaged"]
        target = str(damaged)
        original_bytes = damaged.read_bytes()
        claim_replacement(target, series="Tales of the Unexpected", issue="8")

        incoming = library["target"] / "Tales of the Unexpected 008.cbz"
        _good_cbz(incoming)

        with patch("helpers.trash.move_to_trash",
                   return_value={"trashed": True, "path": "/trash/old.cbz"}) as trash:
            changed = apply_pending(str(library["target"]), PATTERN,
                                    alias_lookup=no_aliases)

        assert len(changed) == 1
        assert changed[0]["status"] == STATUS_APPLIED

        # The replacement now occupies the damaged file's exact path.
        assert damaged.exists()
        assert damaged.read_bytes() != original_bytes
        with zipfile.ZipFile(damaged) as zf:
            assert zf.testzip() is None
        # And it came out of TARGET.
        assert not incoming.exists()
        trash.assert_called_once()

    def test_the_damaged_file_goes_to_the_trash_not_the_void(
        self, store, library, no_aliases
    ):
        """'Replace' is not a promise to destroy. A wrong swap must cost a
        restore, not a re-download."""
        target = str(library["damaged"])
        claim_replacement(target, series="Tales of the Unexpected", issue="8")
        _good_cbz(library["target"] / "Tales of the Unexpected 008.cbz")

        with patch("helpers.trash.move_to_trash",
                   return_value={"trashed": True, "path": "/trash/old.cbz"}) as trash:
            apply_pending(str(library["target"]), PATTERN, alias_lookup=no_aliases)

        trash.assert_called_once_with(target)
        assert get_replacement(target)["trashed_path"] == "/trash/old.cbz"

    def test_a_damaged_replacement_is_refused_and_nothing_is_touched(
        self, store, library, no_aliases
    ):
        """The property that matters most: a partly-readable original is worth
        more than a broken replacement."""
        damaged = library["damaged"]
        target = str(damaged)
        original_bytes = damaged.read_bytes()
        claim_replacement(target, series="Tales of the Unexpected", issue="8")

        incoming = library["target"] / "Tales of the Unexpected 008.cbz"
        _bad_crc_cbz(incoming)

        with patch("helpers.trash.move_to_trash") as trash:
            changed = apply_pending(str(library["target"]), PATTERN,
                                    alias_lookup=no_aliases)

        assert changed[0]["status"] == STATUS_FAILED
        assert "damaged" in changed[0]["detail"]
        trash.assert_not_called()
        assert damaged.read_bytes() == original_bytes
        assert incoming.exists()

    def test_a_failure_is_terminal_until_the_user_acts(
        self, store, library, no_aliases
    ):
        """Otherwise every sweep retries the same broken download forever."""
        target = str(library["damaged"])
        claim_replacement(target, series="Tales of the Unexpected", issue="8")
        _bad_crc_cbz(library["target"] / "Tales of the Unexpected 008.cbz")

        apply_pending(str(library["target"]), PATTERN, alias_lookup=no_aliases)
        second = apply_pending(str(library["target"]), PATTERN, alias_lookup=no_aliases)

        assert second == []
        assert get_replacement(target)["status"] == STATUS_FAILED

    def test_an_unrelated_file_in_target_is_left_alone(
        self, store, library, no_aliases
    ):
        damaged = library["damaged"]
        original_bytes = damaged.read_bytes()
        claim_replacement(str(damaged), series="Tales of the Unexpected", issue="8")

        other = library["target"] / "Batman 001.cbz"
        _good_cbz(other)

        changed = apply_pending(str(library["target"]), PATTERN,
                                alias_lookup=no_aliases)

        assert changed == []
        assert other.exists()
        assert damaged.read_bytes() == original_bytes

    def test_the_wrong_issue_number_does_not_match(self, store, library, no_aliases):
        damaged = library["damaged"]
        claim_replacement(str(damaged), series="Tales of the Unexpected", issue="8")
        _good_cbz(library["target"] / "Tales of the Unexpected 009.cbz")

        assert apply_pending(str(library["target"]), PATTERN,
                             alias_lookup=no_aliases) == []

    def test_a_claim_without_series_or_issue_is_never_matched(
        self, store, library, no_aliases
    ):
        """A blank claim would match the first file in TARGET."""
        claim_replacement(str(library["damaged"]), series="", issue="")
        _good_cbz(library["target"] / "Anything At All 001.cbz")

        assert apply_pending(str(library["target"]), PATTERN,
                             alias_lookup=no_aliases) == []

    def test_nothing_pending_is_a_cheap_no_op(self, store, library, no_aliases):
        assert apply_pending(str(library["target"]), PATTERN,
                             alias_lookup=no_aliases) == []

    def test_a_missing_target_folder_is_survivable(self, store, library, no_aliases):
        claim_replacement(str(library["damaged"]), series="S", issue="8")
        assert apply_pending("/no/such/folder", PATTERN, alias_lookup=no_aliases) == []

    def test_the_problem_entry_is_cleared_on_success(
        self, store, library, no_aliases
    ):
        from core.problem_files import SOURCE_THUMBNAIL, get_problem, record_problem

        target = str(library["damaged"])
        record_problem(target, SOURCE_THUMBNAIL, error_class="BadZipFile",
                       error_message="Bad CRC-32")
        claim_replacement(target, series="Tales of the Unexpected", issue="8")
        _good_cbz(library["target"] / "Tales of the Unexpected 008.cbz")

        with patch("helpers.trash.move_to_trash",
                   return_value={"trashed": True, "path": "/trash/old.cbz"}):
            apply_pending(str(library["target"]), PATTERN, alias_lookup=no_aliases)

        assert get_problem(target, SOURCE_THUMBNAIL) is None


class TestSwapOrdering:
    """Regressions from a real end-to-end run.

    The first implementation trashed the damaged file and then moved the
    replacement in. ``move_to_trash`` prunes the parent directory once it
    empties, so on a single-issue folder the destination stopped existing
    between the two steps: the move failed, the library slot was left empty,
    and the download stayed in TARGET.
    """

    def test_a_single_issue_folder_survives_the_swap(
        self, store, library, no_aliases
    ):
        """The damaged file is the only one in its folder -- the case that broke.

        The stand-in reproduces the one behaviour that matters here: the real
        ``move_to_trash`` calls ``_cleanup_empty_parent``, which rmtree's the
        folder the moment it empties.
        """
        damaged = library["damaged"]
        assert list(damaged.parent.iterdir()) == [damaged], "single-issue folder"

        claim_replacement(str(damaged), series="Tales of the Unexpected", issue="8")
        _good_cbz(library["target"] / "Tales of the Unexpected 008.cbz")

        with patch("helpers.trash.move_to_trash", side_effect=_make_trash(library["trash"])):
            changed = apply_pending(str(library["target"]), PATTERN,
                                    alias_lookup=no_aliases)

        assert changed[0]["status"] == STATUS_APPLIED, changed[0].get("detail")
        assert damaged.exists(), "the library slot must not be left empty"
        with zipfile.ZipFile(damaged) as zf:
            assert zf.testzip() is None

    def test_no_staging_file_is_left_behind(
        self, store, library, no_aliases
    ):
        claim_replacement(str(library["damaged"]), series="Tales of the Unexpected",
                          issue="8")
        _good_cbz(library["target"] / "Tales of the Unexpected 008.cbz")

        with patch("helpers.trash.move_to_trash", side_effect=_make_trash(library["trash"])):
            apply_pending(str(library["target"]), PATTERN, alias_lookup=no_aliases)

        leftovers = [p.name for p in library["damaged"].parent.iterdir()
                     if "clu_incoming" in p.name]
        assert leftovers == []

    def test_a_failed_move_restores_the_damaged_file(
        self, store, library, no_aliases, monkeypatch
    ):
        """A failed swap has to be a no-op. A damaged comic is still the comic
        the user had, and it is what the Problem Files entry describes."""
        damaged = library["damaged"]
        original = damaged.read_bytes()
        claim_replacement(str(damaged), series="Tales of the Unexpected", issue="8")
        _good_cbz(library["target"] / "Tales of the Unexpected 008.cbz")

        real_replace = os.replace

        def boom(src, dst):
            if str(dst) == str(damaged):
                raise OSError("simulated failure putting the replacement in place")
            return real_replace(src, dst)

        monkeypatch.setattr("core.problem_replacements.os.replace", boom)
        with patch("helpers.trash.move_to_trash", side_effect=_make_trash(library["trash"])):
            changed = apply_pending(str(library["target"]), PATTERN,
                                    alias_lookup=no_aliases)

        assert changed[0]["status"] == STATUS_FAILED
        assert damaged.exists(), "the damaged file must come back"
        assert damaged.read_bytes() == original


def _make_trash(trash_dir):
    """Stand-in for helpers.trash.move_to_trash, bound to a trash folder.

    Faithful in the two respects this module has to survive: the file leaves
    its folder entirely (the real trash is a different directory), and the real
    one then calls ``_cleanup_empty_parent``, which removes the folder as soon
    as it is empty. Stashing the file in place instead would leave the folder
    non-empty and the regression would go unnoticed.
    """
    import shutil as _sh

    def _trash(path):
        parent = os.path.dirname(str(path))
        dest = os.path.join(str(trash_dir), os.path.basename(str(path)))
        _sh.move(str(path), dest)
        if os.path.isdir(parent) and not os.listdir(parent):
            _sh.rmtree(parent)
        return {"trashed": True, "path": dest}

    return _trash


class TestProductionFailures:
    """Regressions from a live deployment.

    The automatic pass runs from `api.py`'s `check_wanted_after_watch_empty`,
    a bare daemon thread with no Flask application context. Every function in
    `helpers.trash` reads `current_app`, so every automatic replacement failed
    with "Working outside of application context" and then could not put the
    download back, because the wanted sweep's `schedule_target_cleanup` had
    pruned the folder it came from.
    """

    def test_a_trash_failure_leaves_the_library_untouched(
        self, store, library, no_aliases
    ):
        """The exact production failure: move_to_trash raises."""
        damaged = library["damaged"]
        original = damaged.read_bytes()
        claim_replacement(str(damaged), series="Tales of the Unexpected", issue="8")
        incoming = library["target"] / "Tales of the Unexpected 008.cbz"
        _good_cbz(incoming)

        with patch("helpers.trash.move_to_trash",
                   side_effect=RuntimeError("Working outside of application context.")):
            changed = apply_pending(str(library["target"]), PATTERN,
                                    alias_lookup=no_aliases)

        assert changed[0]["status"] == STATUS_FAILED
        assert damaged.exists()
        assert damaged.read_bytes() == original
        # The download went back to TARGET rather than being stranded.
        assert incoming.exists()
        assert not any("clu_incoming" in p.name for p in damaged.parent.iterdir())

    def test_the_download_is_recovered_even_if_target_was_pruned(
        self, store, library, no_aliases
    ):
        """`schedule_target_cleanup` prunes TARGET's empty folders, and staging
        the file is what empties one. Unstaging has to recreate it."""
        damaged = library["damaged"]
        claim_replacement(str(damaged), series="Tales of the Unexpected", issue="8")
        incoming = library["target"] / "Tales of the Unexpected 008.cbz"
        _good_cbz(incoming)

        def trash_and_prune_target(_path):
            # Stand in for the real interleaving: TARGET disappears mid-swap.
            import shutil as _sh
            _sh.rmtree(str(library["target"]))
            raise RuntimeError("Working outside of application context.")

        with patch("helpers.trash.move_to_trash", side_effect=trash_and_prune_target):
            changed = apply_pending(str(library["target"]), PATTERN,
                                    alias_lookup=no_aliases)

        assert changed[0]["status"] == STATUS_FAILED
        assert damaged.exists(), "the library file must survive"
        assert incoming.exists(), "the download must be put back, not stranded"
        assert not any("clu_incoming" in p.name for p in damaged.parent.iterdir())

    def test_a_cbr_never_replaces_a_cbz(self, store, library, no_aliases):
        """TARGET holds a .cbr only when the pipeline has not converted it yet.

        Swapping one in downgrades the library. The entry stays *pending*, not
        failed, so the next pass takes the file once it is a .cbz.
        """
        damaged = library["damaged"]          # .cbz
        original = damaged.read_bytes()
        target = str(damaged)
        claim_replacement(target, series="Tales of the Unexpected", issue="8")

        cbr = library["target"] / "Tales of the Unexpected 008.cbr"
        cbr.write_bytes(_rar_bytes())

        with patch("helpers.trash.move_to_trash") as trash:
            changed = apply_pending(str(library["target"]), PATTERN,
                                    alias_lookup=no_aliases)

        assert changed == []
        trash.assert_not_called()
        assert damaged.read_bytes() == original
        assert cbr.exists()
        assert get_replacement(target)["status"] == STATUS_PENDING

    def test_a_cbz_may_upgrade_a_damaged_cbr(self, store, library, no_aliases):
        """The reverse is an improvement and is allowed."""
        series_dir = library["series_dir"]
        damaged_cbr = series_dir / "Tales of the Unexpected 010 (2007).cbr"
        damaged_cbr.write_bytes(_rar_bytes())
        claim_replacement(str(damaged_cbr), series="Tales of the Unexpected",
                          issue="10")
        _good_cbz(library["target"] / "Tales of the Unexpected 010.cbz")

        with patch("helpers.trash.move_to_trash",
                   side_effect=_make_trash(library["trash"])):
            changed = apply_pending(str(library["target"]), PATTERN,
                                    alias_lookup=no_aliases)

        assert changed[0]["status"] == STATUS_APPLIED
        assert (series_dir / "Tales of the Unexpected 010 (2007).cbz").exists()
        assert not damaged_cbr.exists()

    def test_only_one_pass_runs_at_a_time(self, store, library, no_aliases):
        """The sweep thread and the page's 10s poll would otherwise race to
        move the same file out of TARGET."""
        import threading

        claim_replacement(str(library["damaged"]), series="Tales of the Unexpected",
                          issue="8")
        _good_cbz(library["target"] / "Tales of the Unexpected 008.cbz")

        started = threading.Event()
        release = threading.Event()
        results = {}

        def slow_trash(path):
            started.set()
            release.wait(timeout=5)
            return _make_trash(library["trash"])(path)

        def first():
            with patch("helpers.trash.move_to_trash", side_effect=slow_trash):
                results["first"] = apply_pending(str(library["target"]), PATTERN,
                                                 alias_lookup=no_aliases)

        t = threading.Thread(target=first)
        t.start()
        assert started.wait(timeout=5), "first pass did not start"

        # Second caller arrives while the first is mid-swap.
        results["second"] = apply_pending(str(library["target"]), PATTERN,
                                          alias_lookup=no_aliases)
        release.set()
        t.join(timeout=10)

        assert results["second"] == [], "the second pass must stand down"
        assert len(results["first"]) == 1
