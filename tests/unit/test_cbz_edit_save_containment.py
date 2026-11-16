"""The CBZ editor's /save must only act on the paths /edit produced.

``save_cbz`` renames, rezips, deletes and rmtree's paths that come back from
the browser. These tests open real archives with ``get_edit_modal`` and then
post tampered forms, asserting the refusal status *and* that nothing on disk
changed -- plus that an untampered save still works.
"""

import io
import json
import os
import zipfile
from unittest.mock import patch

import pytest
from flask import Flask
from PIL import Image

from cbz_ops import edit, edit_paths


def _png(i):
    buf = io.BytesIO()
    Image.new("RGB", (40, 60), (i * 60, 90, 160)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def flask_app():
    return Flask(__name__)


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A library, WATCH, TARGET and an unrelated folder, all real directories."""
    lib = tmp_path / "library"
    watch = tmp_path / "watch"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    for d in (lib, watch, target, outside):
        d.mkdir()

    monkeypatch.setattr(edit_paths, "get_library_roots", lambda: [str(lib)])
    monkeypatch.setattr(edit_paths, "get_watch_dir", lambda: str(watch))
    monkeypatch.setattr(edit_paths, "get_target_dir", lambda: str(target))
    monkeypatch.setattr(
        edit_paths, "get_protected_roots",
        lambda: {os.path.realpath(str(p)) for p in (lib, watch, target)},
    )
    monkeypatch.setattr(
        edit_paths, "is_critical_path",
        lambda p: os.path.normpath(p) in {os.path.normpath(str(watch)), os.path.normpath(str(target))},
    )
    # Single-user by default; grant tests override this.
    monkeypatch.setattr(edit_paths, "file_op_refusal", lambda *paths: None)
    return {"lib": lib, "watch": watch, "target": target, "outside": outside}


def _make_cbz(path, pages=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(pages):
            zf.writestr(f"page_{i:03d}.png", _png(i))
    return path


def _open(flask_app, path):
    with flask_app.app_context():
        return edit.get_edit_modal(str(path))


def _post(flask_app, form):
    form = dict(form)
    form.setdefault("pending_deletes", "[]")
    form.setdefault("pending_order", "[]")
    with flask_app.test_request_context("/save", method="POST", data=form):
        with patch.object(edit, "regenerate_thumbnail"), \
             patch.object(edit, "_recheck_listed_problems"):
            rv = edit.save_cbz()
    if isinstance(rv, tuple):
        body, status = rv
    else:
        body, status = rv, 200
    data = body.get_json() if hasattr(body, "get_json") else body
    return data, status


def _snapshot(root):
    """Every path under ``root`` with its size, so 'nothing changed' is checkable."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            out[os.path.join(dirpath, d)] = "dir"
        for f in filenames:
            p = os.path.join(dirpath, f)
            out[p] = os.path.getsize(p)
    return out


# ---------------------------------------------------------------------------
# The legitimate flow still works
# ---------------------------------------------------------------------------

class TestNormalSave:
    def test_library_comic_round_trips(self, flask_app, layout):
        comic = _make_cbz(layout["lib"] / "Series" / "Comic 001.cbz")
        opened = _open(flask_app, comic)

        data, status = _post(flask_app, {
            "folder_name": opened["folder_name"],
            "zip_file_path": opened["zip_file_path"],
            "original_file_path": opened["original_file_path"],
            "pending_deletes": json.dumps(["page_001.png"]),
        })

        assert status == 200 and data["success"] is True
        with zipfile.ZipFile(comic) as zf:
            assert zf.namelist() == ["page_000.png", "page_002.png"]
        assert not (comic.parent / "Comic 001.zip").exists()
        assert not (comic.parent / "Comic 001_folder").exists()
        assert not (comic.parent / "Comic 001.zip.bak").exists()

    def test_nested_extraction_folder_is_accepted(self, flask_app, layout):
        comic = layout["lib"] / "Nested 001.cbz"
        comic.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(comic, "w") as zf:
            zf.writestr("Inner/page_000.png", _png(0))
            zf.writestr("Inner/page_001.png", _png(1))
        opened = _open(flask_app, comic)
        assert opened["folder_name"].endswith(os.path.join("Nested 001_folder", "Inner"))

        data, status = _post(flask_app, {
            "folder_name": opened["folder_name"],
            "zip_file_path": opened["zip_file_path"],
            "original_file_path": opened["original_file_path"],
        })

        assert status == 200 and data["success"] is True
        assert not (layout["lib"] / "Nested 001_folder").exists()
        with zipfile.ZipFile(comic) as zf:
            assert sorted(zf.namelist()) == ["page_000.png", "page_001.png"]

    @pytest.mark.parametrize("staging", ["watch", "target"])
    def test_staging_comic_round_trips(self, flask_app, layout, staging):
        comic = _make_cbz(layout[staging] / "Download 001.cbz")
        opened = _open(flask_app, comic)
        data, status = _post(flask_app, {
            "folder_name": opened["folder_name"],
            "zip_file_path": opened["zip_file_path"],
            "original_file_path": opened["original_file_path"],
        })
        assert status == 200 and data["success"] is True
        assert comic.exists()
        assert not (layout[staging] / "Download 001_folder").exists()


# ---------------------------------------------------------------------------
# Tampered forms are refused and touch nothing
# ---------------------------------------------------------------------------

class TestRefusals:
    @pytest.fixture
    def opened(self, flask_app, layout):
        comic = _make_cbz(layout["lib"] / "Series" / "Comic 001.cbz")
        return _open(flask_app, comic)

    def _assert_refused(self, flask_app, layout, form, status):
        before = _snapshot(layout["lib"].parent)
        data, got = _post(flask_app, form)
        assert got == status, data
        assert data["success"] is False and data["error"]
        assert _snapshot(layout["lib"].parent) == before

    def test_folder_outside_the_extraction_root(self, flask_app, layout, opened):
        victim = layout["outside"] / "keep"
        victim.mkdir()
        (victim / "precious.txt").write_text("x")
        self._assert_refused(flask_app, layout, {**opened, "folder_name": str(victim)}, 403)
        assert (victim / "precious.txt").exists()

    def test_folder_is_a_library_root(self, flask_app, layout, opened):
        self._assert_refused(flask_app, layout, {**opened, "folder_name": str(layout["lib"])}, 403)

    def test_folder_is_a_sibling_with_a_shared_prefix(self, flask_app, layout, opened):
        """``Comic 001_folder-other`` is not inside ``Comic 001_folder``."""
        sibling = layout["lib"] / "Series" / "Comic 001_folder-other"
        sibling.mkdir()
        (sibling / "a.txt").write_text("x")
        self._assert_refused(flask_app, layout, {**opened, "folder_name": str(sibling)}, 403)

    def test_folder_traversal_escape(self, flask_app, layout, opened):
        escape = os.path.join(opened["folder_name"], "..", "..")
        self._assert_refused(flask_app, layout, {**opened, "folder_name": escape}, 403)

    def test_relative_folder(self, flask_app, layout, opened):
        self._assert_refused(flask_app, layout, {**opened, "folder_name": "Comic 001_folder"}, 400)

    def test_original_outside_every_root(self, flask_app, layout, opened):
        target = layout["outside"] / "Other.cbz"
        _make_cbz(target)
        self._assert_refused(flask_app, layout, {**opened, "original_file_path": str(target)}, 403)

    def test_zip_outside_every_root(self, flask_app, layout, opened):
        victim = _make_cbz(layout["outside"] / "Victim.zip")
        self._assert_refused(flask_app, layout, {**opened, "zip_file_path": str(victim)}, 403)
        assert victim.exists()

    def test_zip_not_derived_from_the_original(self, flask_app, layout, opened):
        other = _make_cbz(layout["lib"] / "Series" / "Other.zip")
        self._assert_refused(flask_app, layout, {**opened, "zip_file_path": str(other)}, 400)
        assert other.exists()

    def test_original_with_another_extension(self, flask_app, layout, opened):
        txt = layout["lib"] / "Series" / "notes.txt"
        txt.write_text("x")
        self._assert_refused(flask_app, layout, {**opened, "original_file_path": str(txt)}, 400)

    def test_original_with_no_extraction_folder(self, flask_app, layout, opened):
        """A real library archive that was never opened in the editor."""
        other = _make_cbz(layout["lib"] / "Series" / "Never Opened.cbz")
        (layout["lib"] / "Series" / "Never Opened.zip").write_bytes(other.read_bytes())
        self._assert_refused(flask_app, layout, {
            **opened,
            "original_file_path": str(other),
            "zip_file_path": str(layout["lib"] / "Series" / "Never Opened.zip"),
        }, 404)

    def test_extraction_root_that_is_a_protected_root(self, flask_app, layout, monkeypatch):
        """A library configured at ``<stem>_folder`` beside the archive must survive."""
        comic = _make_cbz(layout["lib"] / "Comic 001.cbz")
        opened = _open(flask_app, comic)
        extract_root = layout["lib"] / "Comic 001_folder"
        monkeypatch.setattr(
            edit_paths, "get_protected_roots",
            lambda: {os.path.realpath(str(layout["lib"])), os.path.realpath(str(extract_root))},
        )
        self._assert_refused(flask_app, layout, opened, 403)

    def test_grant_refusal_is_honoured(self, flask_app, layout, opened, monkeypatch):
        seen = []

        def refuse(*paths):
            seen.extend(paths)
            return "Access denied - you do not have access to this folder", 403

        monkeypatch.setattr(edit_paths, "file_op_refusal", refuse)
        self._assert_refused(flask_app, layout, opened, 403)
        assert opened["original_file_path"] in seen


class TestWiring:
    """app.py cannot be imported in tests, so the call sites are read as source."""

    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def test_app_registers_the_guarded_save(self):
        import ast
        with open(os.path.join(self.ROOT, "app.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        rules = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "add_url_rule"
            and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "/save"
        ]
        assert len(rules) == 1
        view = {k.arg: k.value for k in rules[0].keywords}["view_func"]
        assert isinstance(view, ast.Name) and view.id == "save_cbz"
        imports = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module == "cbz_ops.edit"
            and any(a.name == "save_cbz" for a in n.names)
        ]
        assert imports, "/save must be cbz_ops.edit.save_cbz"

    def test_validation_precedes_every_disk_operation(self):
        import ast
        import inspect
        import textwrap
        fn = ast.parse(textwrap.dedent(inspect.getsource(edit.save_cbz))).body[0]
        lines = {"validate": None, "disk": []}
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "validate_save_paths":
                lines["validate"] = node.lineno
            elif name in {"remove", "rename", "rmtree", "open_zip_for_write", "walk"}:
                lines["disk"].append(node.lineno)
        assert lines["validate"] is not None
        assert lines["disk"] and min(lines["disk"]) > lines["validate"]


class TestArchiveRefusal:
    """The shared single-path check /edit can reuse."""

    def test_accepts_library_and_staging(self, layout):
        for key in ("lib", "watch", "target"):
            p = _make_cbz(layout[key] / "A.cbz")
            assert edit_paths.editor_archive_refusal(str(p)) is None

    def test_refuses_outside(self, layout):
        p = _make_cbz(layout["outside"] / "A.cbz")
        assert edit_paths.editor_archive_refusal(str(p))[1] == 403

    def test_refuses_prefix_sibling_of_a_root(self, layout):
        sibling = layout["lib"].parent / "library-other"
        p = _make_cbz(sibling / "A.cbz")
        assert edit_paths.editor_archive_refusal(str(p))[1] == 403

    def test_refuses_dotdot_escape(self, layout):
        p = _make_cbz(layout["outside"] / "A.cbz")
        sneaky = os.path.join(str(layout["lib"]), "..", "outside", "A.cbz")
        assert os.path.exists(sneaky)
        assert edit_paths.editor_archive_refusal(sneaky)[1] == 403

    @pytest.mark.parametrize("bad", ["", None, "relative/A.cbz", "/x/A.cbz\0.cbz"])
    def test_refuses_malformed(self, layout, bad):
        assert edit_paths.editor_archive_refusal(bad)[1] == 400

    def test_refuses_non_archive(self, layout):
        p = layout["lib"] / "A.txt"
        p.write_text("x")
        assert edit_paths.editor_archive_refusal(str(p))[1] == 400

    def test_symlink_out_of_the_library_is_refused(self, layout):
        target = _make_cbz(layout["outside"] / "A.cbz")
        link = layout["lib"] / "Link.cbz"
        try:
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
        assert edit_paths.editor_archive_refusal(str(link))[1] == 403

    def test_grant_check_sees_the_resolved_path(self, layout, monkeypatch):
        """A symlink inside a granted folder into an ungranted one is judged by
        where it really points."""
        granted = layout["lib"] / "Granted"
        other = layout["lib"] / "Other"
        real = _make_cbz(other / "A.cbz")
        granted.mkdir()
        link = granted / "A.cbz"
        try:
            os.symlink(str(real), str(link))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
        seen = []
        monkeypatch.setattr(edit_paths, "file_op_refusal", lambda *p: seen.extend(p))
        edit_paths.editor_archive_refusal(str(link))
        assert any(os.path.normcase(s) == os.path.normcase(str(real)) for s in seen)
