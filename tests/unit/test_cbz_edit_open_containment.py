"""The CBZ editor's /edit must validate its path before renaming or extracting.

Opening an archive in the editor renames ``X.cbz`` to ``X.zip`` and extracts it
into ``X_folder``. These tests drive ``cbz_ops.edit.edit_cbz`` (the view
``app.py`` registers) with real archives, asserting the refusal status *and*
that nothing on disk changed -- plus that a legitimate open still works and
returns the same JSON shape the frontend reads.
"""

import ast
import inspect
import io
import os
import re
import textwrap
import zipfile

import pytest
from flask import Flask
from PIL import Image

from cbz_ops import edit, edit_paths

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _png(i):
    buf = io.BytesIO()
    Image.new("RGB", (40, 60), (i * 60, 90, 160)).save(buf, "PNG")
    return buf.getvalue()


def _make_cbz(path, pages=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(pages):
            zf.writestr(f"page_{i:03d}.png", _png(i))
    return path


def _snapshot(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            out[os.path.join(dirpath, d)] = "dir"
        for f in filenames:
            p = os.path.join(dirpath, f)
            out[p] = os.path.getsize(p)
    return out


@pytest.fixture
def flask_app():
    return Flask(__name__)


@pytest.fixture
def layout(tmp_path, monkeypatch):
    lib = tmp_path / "library"
    watch = tmp_path / "watch"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    for d in (lib, watch, target, outside):
        d.mkdir()
    monkeypatch.setattr(edit_paths, "get_library_roots", lambda: [str(lib)])
    monkeypatch.setattr(edit_paths, "get_watch_dir", lambda: str(watch))
    monkeypatch.setattr(edit_paths, "get_target_dir", lambda: str(target))
    monkeypatch.setattr(edit_paths, "file_op_refusal", lambda *paths: None)
    return {"root": tmp_path, "lib": lib, "watch": watch, "target": target, "outside": outside}


def _call(flask_app, file_path=None, as_form=False, raw=None):
    kwargs = {"method": "POST"}
    if raw is not None:
        kwargs["json"] = raw
    elif as_form:
        kwargs["data"] = {} if file_path is None else {"file_path": file_path}
    else:
        kwargs["json"] = {} if file_path is None else {"file_path": file_path}
    with flask_app.test_request_context("/edit", **kwargs):
        rv = edit.edit_cbz()
    body, status = rv if isinstance(rv, tuple) else (rv, 200)
    return body.get_json(), status


class TestNormalOpen:
    @pytest.mark.parametrize("where", ["lib", "watch", "target"])
    def test_opens_and_returns_the_editor_payload(self, flask_app, layout, where):
        comic = _make_cbz(layout[where] / "Series" / "Comic 001.cbz")

        data, status = _call(flask_app, str(comic))

        assert status == 200, data
        assert set(data) == {"modal_body", "folder_name", "zip_file_path", "original_file_path"}
        assert data["original_file_path"] == str(comic)
        assert data["zip_file_path"] == str(comic.parent / "Comic 001.zip")
        assert data["folder_name"] == str(comic.parent / "Comic 001_folder")
        assert "page_001.png" in data["modal_body"]
        assert not comic.exists()
        assert (comic.parent / "Comic 001.zip").is_file()
        assert sorted(os.listdir(comic.parent / "Comic 001_folder")) == [
            "page_000.png", "page_001.png", "page_002.png",
        ]

    def test_form_body_is_accepted(self, flask_app, layout):
        comic = _make_cbz(layout["lib"] / "Comic 002.cbz")
        data, status = _call(flask_app, str(comic), as_form=True)
        assert status == 200, data
        assert (layout["lib"] / "Comic 002_folder").is_dir()


class TestRefusals:
    def _assert_refused(self, flask_app, layout, file_path, status, **kw):
        before = _snapshot(layout["root"])
        data, got = _call(flask_app, file_path, **kw)
        assert got == status, data
        assert data["error"]
        assert "modal_body" not in data
        assert _snapshot(layout["root"]) == before

    def test_outside_every_root(self, flask_app, layout):
        victim = _make_cbz(layout["outside"] / "Victim.cbz")
        self._assert_refused(flask_app, layout, str(victim), 403)
        assert victim.exists()
        assert not (layout["outside"] / "Victim.zip").exists()
        assert not (layout["outside"] / "Victim_folder").exists()

    def test_dotdot_escape(self, flask_app, layout):
        victim = _make_cbz(layout["outside"] / "Victim.cbz")
        sneaky = os.path.join(str(layout["lib"]), "..", "outside", "Victim.cbz")
        self._assert_refused(flask_app, layout, sneaky, 403)
        assert victim.exists()

    def test_prefix_sibling_of_a_root(self, flask_app, layout):
        victim = _make_cbz(layout["root"] / "library-other" / "Victim.cbz")
        self._assert_refused(flask_app, layout, str(victim), 403)
        assert victim.exists()

    def test_relative_path(self, flask_app, layout):
        self._assert_refused(flask_app, layout, "library/Comic.cbz", 400)

    def test_non_archive(self, flask_app, layout):
        txt = layout["lib"] / "notes.txt"
        txt.write_text("x")
        self._assert_refused(flask_app, layout, str(txt), 400)

    def test_missing_path(self, flask_app, layout):
        self._assert_refused(flask_app, layout, None, 400)

    def test_non_string_path(self, flask_app, layout):
        self._assert_refused(flask_app, layout, None, 400, raw={"file_path": ["/x.cbz"]})

    def test_nonexistent_archive_inside_the_library(self, flask_app, layout):
        self._assert_refused(flask_app, layout, str(layout["lib"] / "Gone.cbz"), 404)
        assert not (layout["lib"] / "Gone_folder").exists()

    def test_grant_refusal_is_honoured(self, flask_app, layout, monkeypatch):
        comic = _make_cbz(layout["lib"] / "Private" / "Comic.cbz")
        monkeypatch.setattr(
            edit_paths, "file_op_refusal",
            lambda *p: ("Access denied - you do not have access to this folder", 403),
        )
        self._assert_refused(flask_app, layout, str(comic), 403)
        assert comic.exists()

    def test_symlink_out_of_the_library(self, flask_app, layout):
        victim = _make_cbz(layout["outside"] / "Victim.cbz")
        link = layout["lib"] / "Link.cbz"
        try:
            os.symlink(str(victim), str(link))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
        self._assert_refused(flask_app, layout, str(link), 403)
        assert victim.exists()


class TestWiring:
    """app.py cannot be imported in tests, so the registration is read as source."""

    def _app_tree(self):
        with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def test_app_registers_edit_as_post_only(self):
        tree = self._app_tree()
        rules = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) in ("add_url_rule", "route")
            and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "/edit"
        ]
        assert len(rules) == 1, "exactly one /edit registration"
        rule = rules[0]
        assert rule.func.attr == "add_url_rule"
        kw = {k.arg: k.value for k in rule.keywords}
        assert isinstance(kw["view_func"], ast.Name) and kw["view_func"].id == "edit_cbz"
        methods = ast.literal_eval(kw["methods"])
        assert list(methods) == ["POST"]

    def test_edit_view_comes_from_cbz_ops(self):
        tree = self._app_tree()
        imports = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module == "cbz_ops.edit"
            and any(a.name == "edit_cbz" for a in n.names)
        ]
        assert imports, "/edit must be cbz_ops.edit.edit_cbz"
        local_defs = [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "edit_cbz"
        ]
        assert not local_defs, "app.py must not shadow cbz_ops.edit.edit_cbz"

    def test_refusal_precedes_opening(self):
        fn = ast.parse(textwrap.dedent(inspect.getsource(edit.edit_cbz))).body[0]
        lines = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                lines.setdefault(name, node.lineno)
        assert "editor_archive_refusal" in lines and "get_edit_modal" in lines
        assert lines["editor_archive_refusal"] < lines["get_edit_modal"]


class TestFrontendCallers:
    """Every caller must POST the path in the body; a GET would now be a 405."""

    def _sources(self):
        for base in ("static/js", "templates"):
            for dirpath, _dirs, files in os.walk(os.path.join(ROOT, base)):
                for f in files:
                    if f.endswith((".js", ".html")):
                        p = os.path.join(dirpath, f)
                        with open(p, encoding="utf-8", errors="replace") as fh:
                            yield p, fh.read()

    def test_no_query_string_edit_requests(self):
        offenders = [p for p, src in self._sources() if re.search(r"""['"`]/edit\?""", src)]
        assert not offenders

    def test_every_edit_fetch_is_a_post(self):
        calls = 0
        for p, src in self._sources():
            for m in re.finditer(r"""fetch\(\s*['"`]/edit['"`]""", src):
                calls += 1
                window = src[m.end():m.end() + 200]
                assert re.match(r"""\s*,\s*\{\s*method:\s*['"]POST['"]""", window), p
                assert "file_path" in window, p
        # 7 until the unrendered static/js/index.js (one caller) was deleted.
        assert calls >= 6
