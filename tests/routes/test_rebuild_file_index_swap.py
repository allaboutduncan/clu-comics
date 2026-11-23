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


def test_manual_rebuild_runs_off_the_request(app_tree):
    """The walk reads every library folder. In the request it outlasted
    gunicorn's 120s timeout on a large or networked library: the worker was
    killed mid-sync and the page reported a network error."""
    node = _function(app_tree, "api_rebuild_file_index")
    inner = next(n for n in node.body if isinstance(n, ast.FunctionDef))
    assert inner.name == "_run"
    # Nothing slow at the route's own level: it is all inside the thread body.
    outer_calls = [
        ast.unparse(n.func)
        for stmt in node.body if stmt is not inner
        for n in ast.walk(stmt) if isinstance(n, ast.Call)
    ]
    for slow in ("scan_filesystem_for_sync", "sync_file_index_incremental",
                 "prune_vanished_index_dirs", "get_library_stats"):
        assert slow not in outer_calls
        assert slow in _calls(inner)
    assert "threading.Thread" in outer_calls
    assert "register_operation" in outer_calls
    # One at a time, and the claim is given back however the thread ends.
    assert "_rebuild_index_lock.acquire" in outer_calls
    final = next(n for n in ast.walk(inner) if isinstance(n, ast.Try) and n.finalbody)
    released = [ast.unparse(n.func) for stmt in final.finalbody
                for n in ast.walk(stmt) if isinstance(n, ast.Call)]
    assert "_rebuild_index_lock.release" in released
    assert "complete_operation" in released
    # The thread reads current_app-backed helpers.
    assert "app.app_context" in _calls(inner)


@pytest.mark.parametrize("page", ["static/js/problem_files.js", "templates/schedules.html"])
def test_both_pages_follow_the_operation(page):
    src = (ROOT / page).read_text(encoding="utf-8")
    start = src.index("function rebuildFileIndex")
    body = src[start:src.index("\n    }\n", src.index("CLU.startPoll", start))]
    assert "/api/operation/" in body and "op_id" in body
    assert "interrupted" in body
