"""The in-memory file index is swapped, never cleared and refilled.

``POST /api/rebuild-file-index`` and the scheduled sync used to
``file_index.clear()`` and then refill it from the database, so a request on
another thread in between (``/api/file-index-status``) counted zero files, and
a failed read (``get_file_index_from_db`` answers ``[]`` on error) left the
index empty until the next rebuild. The cold-start walk appended straight into
the shared list, so a reader saw a half-built index and a walk that raised
left one behind.

The swap is ``core.file_index_memory.replace_index_contents``, tested directly;
app.py cannot be imported, so its call sites are asserted against the AST.
"""
import ast
import pathlib
import threading

import pytest

from core.file_index_memory import replace_index_contents

ROOT = pathlib.Path(__file__).resolve().parents[2]


class TestReplaceIndexContents:
    def test_swaps_in_place(self):
        index = [{"path": "/old"}]
        same = index
        assert replace_index_contents(index, [{"path": "/a"}, {"path": "/b"}], 2)
        assert same is index
        assert [e["path"] for e in index] == ["/a", "/b"]

    def test_failed_read_keeps_the_previous_index(self):
        index = [{"path": "/old"}]
        assert replace_index_contents(index, [], 5) is False
        assert index == [{"path": "/old"}]

    def test_none_is_a_failed_read_too(self):
        index = [{"path": "/old"}]
        assert replace_index_contents(index, None, 1) is False
        assert index == [{"path": "/old"}]

    def test_a_library_that_really_is_empty_empties_the_index(self):
        index = [{"path": "/old"}]
        assert replace_index_contents(index, [], 0) is True
        assert index == []

    def test_a_reader_never_sees_it_empty(self):
        index = [{"path": f"/{i}"} for i in range(5000)]
        fresh = [{"path": f"/n{i}"} for i in range(5000)]
        stop = threading.Event()
        seen_empty = []

        def reader():
            while not stop.is_set():
                if len(index) == 0:
                    seen_empty.append(True)

        t = threading.Thread(target=reader)
        t.start()
        try:
            for _ in range(200):
                replace_index_contents(index, fresh, len(fresh))
        finally:
            stop.set()
            t.join()
        assert not seen_empty


@pytest.fixture(scope="module")
def app_tree():
    return ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not in app.py")


def _calls(node):
    return [ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)]


def test_app_never_clears_or_appends_to_the_shared_index(app_tree):
    bad = [f"app.py:{n.lineno} {ast.unparse(n)}" for n in ast.walk(app_tree)
           if isinstance(n, ast.Call)
           and ast.unparse(n.func) in ("file_index.clear", "file_index.extend",
                                      "file_index.append")]
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("fn", ["api_rebuild_file_index", "scheduled_file_index_rebuild"])
def test_sync_paths_swap_through_the_helper(app_tree, fn):
    node = _function(app_tree, fn)
    call = next(n for n in ast.walk(node) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "replace_index_contents")
    args = [ast.unparse(a) for a in call.args]
    assert args[0] == "file_index"
    assert args[1] == "get_file_index_from_db()"
    assert args[2] == "sync_result['added'] + sync_result['unchanged']"


def test_cold_build_walks_aside_and_swaps_after_the_walk(app_tree):
    node = _function(app_tree, "_build_file_index_locked")
    src = ast.unparse(node)
    assert "new_index = []" in src
    assert src.count("new_index.append(") == 2
    # The swap comes after the walk's try/except, whose failure returns early.
    walk_try = next(n for n in node.body if isinstance(n, ast.Try))
    swap = next(n for n in node.body if ast.unparse(n) == "file_index[:] = new_index")
    assert node.body.index(swap) > node.body.index(walk_try)
