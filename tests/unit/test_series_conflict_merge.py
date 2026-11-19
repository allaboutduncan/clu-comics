"""Merge and Drop for series folder conflicts (core/series_conflict_merge.py).

Real folders under tmp_path and a real schema; only the trash, the library
predicates and the reconcile are stubbed. Each class names a rule a later
tidy-up could plausibly break.
"""

import os
import shutil
from unittest.mock import patch

import pytest

from core import series_conflict_merge as merge
from core import series_conflicts as sc


@pytest.fixture
def env(db_connection, tmp_path):
    trash = tmp_path / "trash"
    trash.mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "keep.txt").write_text("x")

    def fake_trash(path):
        dest = trash / os.path.basename(path)
        n = 1
        while dest.exists():
            dest = trash / f"{os.path.basename(path)}.{n}"
            n += 1
        shutil.move(path, dest)
        return {"trashed": True, "path": str(dest)}

    with patch("helpers.library.is_valid_library_path", return_value=True), \
         patch("helpers.library.is_critical_path", return_value=False), \
         patch("helpers.library.get_protected_roots", return_value={str(lib)}), \
         patch("helpers.trash.move_to_trash", side_effect=fake_trash), \
         patch("helpers.collection.reconcile_wanted_for_series") as reconcile:
        yield {"lib": lib, "trash": trash, "reconcile": reconcile, "db": db_connection}


def _comic(folder, name, data=b"PK" + b"x" * 10):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return str(folder / name)


def _conflict(env, here, mapped, series_id=7):
    sc.record_scan([{
        "folder": str(here), "metron_id": series_id, "series_name": "Kaya",
        "conflict_with": str(mapped), "source": "series.json:metron_id",
    }], [str(env["lib"])])
    return sc.list_conflicts()[0]


def _run(row, mode="merge", keep=None, rename=False):
    plan = merge.build_plan(row, mode=mode, keep=keep)
    with patch("core.app_state.update_operation"), patch("core.app_state.complete_operation"):
        return merge.run("op", row["id"], mode, keep or {}, rename, plan["signature"])


class TestPlan:
    def test_pairs_by_issue_number_across_names(self, env):
        here, mapped = env["lib"] / "Image" / "Kaya", env["lib"] / "Image Comics" / "Kaya"
        _comic(here, "Kaya 001 (2022).cbz")
        _comic(mapped, "Kaya #1.cbz")
        plan = merge.build_plan(_conflict(env, here, mapped))
        assert [p["label"] for p in plan["pairs"]] == ["#1"]
        assert plan["only_here"] == []

    def test_mapped_copy_wins_a_tie(self, env):
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        _comic(here, "Kaya 002.cbz", b"PK123")
        _comic(mapped, "Kaya 002.cbz", b"PK456")
        plan = merge.build_plan(_conflict(env, here, mapped))
        assert plan["pairs"][0]["keep"] == "mapped"

    def test_a_few_kb_of_difference_is_a_tie(self, env):
        """A re-tag changes the size slightly; that is no reason to move the file."""
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        _comic(here, "Kaya 002.cbz", b"PK" + b"x" * 1010)
        _comic(mapped, "Kaya 002.cbz", b"PK" + b"x" * 1000)
        assert merge.build_plan(_conflict(env, here, mapped))["pairs"][0]["keep"] == "mapped"

    def test_larger_copy_here_is_suggested(self, env):
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        _comic(here, "Kaya 003.cbz", b"PK" + b"x" * 500)
        _comic(mapped, "Kaya 003.cbz", b"PK")
        assert merge.build_plan(_conflict(env, here, mapped))["pairs"][0]["keep"] == "here"

    def test_damaged_copy_is_never_suggested(self, env):
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        big = _comic(here, "Kaya 004.cbz", b"PK" + b"x" * 500)
        _comic(mapped, "Kaya 004.cbz", b"PK")
        env["db"].execute(
            "INSERT INTO file_health (path, status, size, checked_at) VALUES (?, 'damaged', ?, 0)",
            (big, os.path.getsize(big)))
        env["db"].commit()
        assert merge.build_plan(_conflict(env, here, mapped))["pairs"][0]["keep"] == "mapped"

    def test_a_stale_damaged_verdict_is_ignored(self, env):
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        big = _comic(here, "Kaya 004.cbz", b"PK" + b"x" * 500)
        _comic(mapped, "Kaya 004.cbz", b"PK")
        env["db"].execute(
            "INSERT INTO file_health (path, status, size, checked_at) VALUES (?, 'damaged', 7, 0)", (big,))
        env["db"].commit()
        assert merge.build_plan(_conflict(env, here, mapped))["pairs"][0]["keep"] == "here"

    def test_two_variants_of_one_number_are_not_paired(self, env):
        """Nothing is trashed on a guess: both variants move instead."""
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        _comic(here, "Kaya 005 (Cover A).cbz")
        _comic(here, "Kaya 005 (Cover B).cbz")
        _comic(mapped, "Kaya 005.cbz")
        plan = merge.build_plan(_conflict(env, here, mapped))
        assert plan["pairs"] == []
        assert len(plan["only_here"]) == 2

    def test_a_taken_name_is_held_not_suffixed(self, env):
        """Two variants of #5 here are not paired; one's name is taken by a different file."""
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        _comic(here, "Kaya 005 (A).cbz", b"PK-here")
        _comic(here, "Kaya 005 (B).cbz")
        _comic(mapped, "Kaya 005 (A).cbz", b"PK-mapped-and-different")
        plan = merge.build_plan(_conflict(env, here, mapped))
        assert [h["name"] for h in plan["held"]] == ["Kaya 005 (A).cbz"]
        assert [a["dst"] for a in plan["actions"] if a["op"] == "move"] == [
            str(mapped / "Kaya 005 (B).cbz")]

    def test_identical_bytes_under_one_name_are_a_duplicate(self, env):
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        _comic(here, "Kaya 006 (Cover A).cbz", b"PKsame")
        _comic(here, "Kaya 006 (Cover B).cbz", b"PKother")
        _comic(mapped, "Kaya 006 (Cover A).cbz", b"PKsame")
        _comic(mapped, "Kaya 006 (Cover C).cbz", b"PKthird")
        plan = merge.build_plan(_conflict(env, here, mapped))
        trashed = [a["trash"] for a in plan["actions"] if a["op"] == "trash"]
        assert trashed == [str(here / "Kaya 006 (Cover A).cbz")]
        assert [m["name"] for m in plan["only_here"]] == ["Kaya 006 (Cover B).cbz"]

    def test_drop_moves_nothing(self, env):
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        _comic(here, "Kaya 001.cbz")
        _comic(here, "Kaya 009.cbz")
        _comic(mapped, "Kaya 001.cbz")
        plan = merge.build_plan(_conflict(env, here, mapped), mode="drop")
        assert [a["op"] for a in plan["actions"]] == ["trash"]
        assert plan["actions"][0]["trash"] == str(here / "Kaya 001.cbz")

    def test_signature_ignores_keeper_choices(self, env):
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        _comic(here, "Kaya 001.cbz")
        _comic(mapped, "Kaya 001.cbz")
        row = _conflict(env, here, mapped)
        a = merge.build_plan(row)
        b = merge.build_plan(row, keep={a["pairs"][0]["key"]: "here"})
        assert a["signature"] == b["signature"]
        _comic(here, "Kaya 002.cbz")
        assert merge.build_plan(row)["signature"] != a["signature"]


class TestRefusals:
    def test_nested_folders_are_refused(self, env):
        mapped = env["lib"] / "Kaya"
        here = mapped / "extras"
        _comic(here, "Kaya 001.cbz")
        with pytest.raises(merge.MergeRefused):
            merge.build_plan(_conflict(env, here, mapped))

    def test_outside_a_library_is_refused(self, env):
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        _comic(here, "Kaya 001.cbz")
        mapped.mkdir(parents=True)
        row = _conflict(env, here, mapped)
        with patch("helpers.library.is_valid_library_path", return_value=False):
            with pytest.raises(merge.MergeRefused):
                merge.build_plan(row)

    def test_missing_row_is_404(self, env):
        with pytest.raises(merge.MergeRefused) as e:
            merge.build_plan(None)
        assert e.value.status == 404


class TestRun:
    def test_merge_moves_trashes_and_removes_the_folder(self, env):
        here, mapped = env["lib"] / "Image" / "Kaya", env["lib"] / "Image Comics" / "Kaya"
        _comic(here, "Kaya 001.cbz", b"PK" + b"x" * 500)   # better copy here
        _comic(here, "Kaya 002.cbz")                        # only here
        (here / "series.json").write_text("{}")
        _comic(mapped, "Kaya 001.cbz", b"PK")
        row = _conflict(env, here, mapped)

        out = _run(row)

        assert out["moved"] == 2 and out["trashed"] == 1 and out["folder_removed"]
        assert sorted(os.listdir(mapped)) == ["Kaya 001.cbz", "Kaya 002.cbz"]
        assert (mapped / "Kaya 001.cbz").stat().st_size == 502
        assert not here.exists()
        assert sc.get_conflict(row["id"]) is None
        env["reconcile"].assert_called_once_with(7)

    def test_keep_mapped_trashes_this_folders_copy(self, env):
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        _comic(here, "Kaya 001.cbz", b"PK" + b"x" * 500)
        _comic(mapped, "Kaya 001.cbz", b"PK")
        row = _conflict(env, here, mapped)
        key = merge.build_plan(row)["pairs"][0]["key"]
        _run(row, keep={key: "mapped"})
        assert (mapped / "Kaya 001.cbz").stat().st_size == 2

    def test_a_held_file_keeps_the_folder_and_the_row(self, env):
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        _comic(here, "Kaya 005 (A).cbz", b"PK-here")
        _comic(here, "Kaya 005 (B).cbz")
        _comic(mapped, "Kaya 005 (A).cbz", b"PK-different")
        _comic(mapped, "Kaya 005 (C).cbz")
        row = _conflict(env, here, mapped)
        out = _run(row)
        assert len(out["held"]) == 1
        assert (here / "Kaya 005 (A).cbz").exists()
        assert (mapped / "Kaya 005 (A).cbz").read_bytes() == b"PK-different"
        assert sc.get_conflict(row["id"])["file_count"] == 1

    def test_changed_folder_refuses_before_anything_moves(self, env):
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        _comic(here, "Kaya 001.cbz")
        mapped.mkdir(parents=True)
        row = _conflict(env, here, mapped)
        sig = merge.build_plan(row)["signature"]
        _comic(here, "Kaya 002.cbz")
        with patch("core.app_state.update_operation"), patch("core.app_state.complete_operation") as done:
            assert merge.run("op", row["id"], "merge", {}, False, sig) is None
        done.assert_called_once_with("op", error=True)
        assert sorted(os.listdir(here)) == ["Kaya 001.cbz", "Kaya 002.cbz"]

    def test_a_subfolder_keeps_the_folder(self, env):
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        _comic(here, "Kaya 001.cbz")
        (here / "Specials").mkdir()
        mapped.mkdir(parents=True)
        out = _run(_conflict(env, here, mapped))
        assert out["moved"] == 1 and not out["folder_removed"]
        assert here.is_dir()

    def test_index_rows_follow_the_move(self, env):
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        src = _comic(here, "Kaya 001.cbz")
        mapped.mkdir(parents=True)
        env["db"].execute(
            "INSERT INTO file_index (path, name, type, parent) VALUES (?, ?, 'file', ?)",
            (src, "Kaya 001.cbz", str(here)))
        env["db"].commit()
        _run(_conflict(env, here, mapped))
        paths = [r[0] for r in env["db"].execute("SELECT path FROM file_index WHERE type='file'")]
        assert paths == [str(mapped / "Kaya 001.cbz")]

    def test_rename_option_applies_the_pattern(self, env):
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        _comic(here, "kaya_001.cbz")
        mapped.mkdir(parents=True)
        with patch("cbz_ops.rename.get_renamed_filename", return_value="Kaya 001.cbz"):
            _run(_conflict(env, here, mapped), rename=True)
        assert os.listdir(mapped) == ["Kaya 001.cbz"]

    def test_drop_leaves_unique_comics_listed(self, env):
        here, mapped = env["lib"] / "A" / "Kaya", env["lib"] / "B" / "Kaya"
        _comic(here, "Kaya 001.cbz")
        _comic(here, "Kaya 009.cbz")
        _comic(mapped, "Kaya 001.cbz")
        row = _conflict(env, here, mapped)
        out = _run(row, mode="drop")
        assert out["trashed"] == 1 and out["moved"] == 0
        assert os.listdir(here) == ["Kaya 009.cbz"]
        assert sc.get_conflict(row["id"])["file_count"] == 1


class TestGroups:
    def _row(self, i, folder, mapped, ignored=False):
        return {"id": i, "folder": folder, "mapped_folder": mapped, "ignored": ignored}

    def test_a_duplicated_parent_forms_a_group(self):
        rows = [self._row(1, "/d/Image/Kaya", "/d/Image Comics/Kaya"),
                self._row(2, "/d/Image/Monstress", "/d/Image Comics/Monstress"),
                self._row(3, "/d/Boom/Lumberjanes", "/d/Boom! Studios/Lumberjanes")]
        groups = merge.group_by_parent(rows)
        assert [(g["parent"], g["ids"]) for g in groups] == [("/d/Image", [1, 2])]

    def test_ignored_and_unmapped_rows_stay_out(self):
        rows = [self._row(1, "/d/Image/Kaya", "/d/Image Comics/Kaya"),
                self._row(2, "/d/Image/Feral", "/d/Image Comics/Feral", ignored=True),
                self._row(3, "/d/Image/Redcoat", None)]
        assert merge.group_by_parent(rows) == []

    def test_siblings_in_one_parent_are_not_a_duplicated_parent(self):
        rows = [self._row(1, "/d/DC/Thunderhawks", "/d/DC/ThunderHawks"),
                self._row(2, "/d/DC/Batman - X", "/d/DC/Batman: X")]
        assert merge.group_by_parent(rows) == []

    def test_punctuation_only_parent_is_likely_the_same(self):
        rows = [self._row(1, "/d/Boom Studios/A", "/d/Boom! Studios/A"),
                self._row(2, "/d/Boom Studios/B", "/d/Boom! Studios/B")]
        assert merge.group_by_parent(rows)[0]["likely_same"] is True

    @pytest.mark.parametrize("a, b, same", [
        ("/d/Thunderhawks", "/d/ThunderHawks", True),
        ("/d/Boom Studios", "/d/Boom! Studios", True),
        ("/d/Image", "/d/Image Comics", False),
        ("/d/Kaya", "/e/Kaya", False),       # identical names are not "likely": they are the same name
        ("/d/Kaya", None, False),
    ])
    def test_likely_same_name(self, a, b, same):
        assert merge.likely_same_name(a, b) is same


class TestRunGroup:
    def test_merges_each_and_skips_a_changed_one(self, env):
        a_here, a_mapped = env["lib"] / "Image" / "Kaya", env["lib"] / "Image Comics" / "Kaya"
        b_here, b_mapped = env["lib"] / "Image" / "Feral", env["lib"] / "Image Comics" / "Feral"
        _comic(a_here, "Kaya 001.cbz")
        a_mapped.mkdir(parents=True)
        _comic(b_here, "Feral 001.cbz")
        b_mapped.mkdir(parents=True)
        a = _conflict(env, a_here, a_mapped, series_id=1)
        sc.record_scan([
            {"folder": str(a_here), "metron_id": 1, "series_name": "Kaya", "conflict_with": str(a_mapped)},
            {"folder": str(b_here), "metron_id": 2, "series_name": "Feral", "conflict_with": str(b_mapped)},
        ], [str(env["lib"])])
        b = next(r for r in sc.list_conflicts() if r["series_id"] == 2)
        items = [(a["id"], merge.build_plan(a)["signature"]),
                 (b["id"], merge.build_plan(b)["signature"])]
        _comic(b_here, "Feral 002.cbz")      # changed after the dry run

        with patch("core.app_state.update_operation"), patch("core.app_state.complete_operation") as done:
            totals = merge.run_group("op", items, rename=False)

        assert totals["series"] == 1 and totals["skipped"] == 1 and totals["moved"] == 1
        assert os.listdir(a_mapped) == ["Kaya 001.cbz"]
        assert sorted(os.listdir(b_here)) == ["Feral 001.cbz", "Feral 002.cbz"]
        done.assert_called_once_with("op", error=False)
