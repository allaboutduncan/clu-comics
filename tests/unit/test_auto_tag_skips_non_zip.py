"""A .cbr must be skipped by the auto-tag loops, not raise inside them.

Every ComicInfo.xml reader and writer in core.comicinfo raises
``ValueError("Only .zip or .cbz files are supported by this function.")`` on a
container it cannot open, and all three auto-tag entry points walk a folder
that may hold ``.cbr`` files. Two of them run the whole folder inside a single
``try``, so one RAR abandoned every remaining file in that folder -- eleven of
those errors in one reported log.

CLU converts CBRs in the WATCH pipeline, so a .cbr sitting in the library is
one the user chose to keep. It is a skip, not a failure.
"""
import ast
import os

import pytest

from core.comicinfo import is_zip_container

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")
CV_PATH = os.path.join(PROJECT_ROOT, "models", "comicvine.py")


class TestPredicate:

    @pytest.mark.parametrize("name", [
        "Batman 001.cbz", "Batman 001.CBZ", "Batman 001.zip", "Batman 001.ZIP",
    ])
    def test_zip_containers(self, name):
        assert is_zip_container(name) is True

    @pytest.mark.parametrize("name", [
        "Batman 001.cbr", "Batman 001.CBR", "Batman 001.rar",
        "Batman 001.pdf", "Batman 001", "",
    ])
    def test_everything_else(self, name):
        assert is_zip_container(name) is False

    def test_none_is_not_a_container(self):
        assert is_zip_container(None) is False

    def test_it_agrees_with_the_reader_it_guards(self, tmp_path):
        """If these two ever disagree the guard stops guarding."""
        from core.comicinfo import read_comicinfo_from_zip

        cbr = tmp_path / "Batman 001.cbr"
        cbr.write_bytes(b"Rar!\x1a\x07\x00")
        assert is_zip_container(str(cbr)) is False
        with pytest.raises(ValueError):
            read_comicinfo_from_zip(str(cbr))


def _func(path, name):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name} not found in {os.path.basename(path)}")


AUTO_TAG_LOOPS = [
    (APP_PATH, "auto_fetch_metron_metadata"),
    (APP_PATH, "auto_fetch_comicvine_sqlite_metadata"),
    (CV_PATH, "auto_fetch_metadata_for_folder"),
]


class TestEveryAutoTagLoopGuards:
    """Three entry points with no shared choke point, so the check is
    necessarily repeated -- but the predicate is not."""

    @pytest.mark.parametrize("path,name", AUTO_TAG_LOOPS,
                             ids=[n for _p, n in AUTO_TAG_LOOPS])
    def test_it_asks_before_reading(self, path, name):
        node = _func(path, name)
        guards = [
            c for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "is_zip_container"
        ]
        assert guards, f"{name} still hands a .cbr to a zip-only reader"

    @pytest.mark.parametrize("path,name", AUTO_TAG_LOOPS,
                             ids=[n for _p, n in AUTO_TAG_LOOPS])
    def test_the_guard_precedes_the_read(self, path, name):
        """Order matters: after the read is too late, the read is the raise."""
        node = _func(path, name)
        guard_lines = [
            c.lineno for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "is_zip_container"
        ]
        read_lines = [
            c.lineno for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "read_comicinfo_from_zip"
        ]
        assert guard_lines and read_lines
        assert min(guard_lines) < min(read_lines), \
            f"{name} reads before it checks"

    @pytest.mark.parametrize("path,name", AUTO_TAG_LOOPS,
                             ids=[n for _p, n in AUTO_TAG_LOOPS])
    def test_it_continues_rather_than_returning(self, path, name):
        """The point is that the *rest of the folder* still gets tagged."""
        node = _func(path, name)
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.If):
                continue
            if not any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                       and c.func.id == "is_zip_container"
                       for c in ast.walk(stmt.test)):
                continue
            assert any(isinstance(n, ast.Continue) for n in ast.walk(stmt)), \
                f"{name} must skip the file, not abandon the folder"
            return
        pytest.fail(f"no is_zip_container guard found in {name}")
