"""The reader's full-archive RAR cache: single-flight and safe eviction.

Real threads against ``core.rar_extract_cache.RarExtractCache`` with a fake
extractor that blocks on an event, so every interleaving asserted here is
forced, not hoped for.
"""

import ast
import os
import threading
from pathlib import Path

import pytest

from core.rar_extract_cache import ENTRY_PREFIX, RarExtractCache

WAIT = 10  # seconds; a safety net for a hung test, never a timing assertion


class FakeExtractor:
    """Writes one page into the destination once its gate is opened."""

    def __init__(self, fail=False, block=True):
        self.fail = fail
        self.lock = threading.Lock()
        self.calls = []
        self.started = {}
        self.gates = {}
        self.block = block

    def gate(self, key):
        key = os.path.realpath(key)
        with self.lock:
            return self.gates.setdefault(key, threading.Event())

    def started_event(self, key):
        key = os.path.realpath(key)
        with self.lock:
            return self.started.setdefault(key, threading.Event())

    def __call__(self, archive, dest):
        with self.lock:
            self.calls.append(archive)
        self.started_event(archive).set()
        if self.block:
            assert self.gate(archive).wait(WAIT), "gate never opened"
        with open(os.path.join(dest, "page001.jpg"), "wb") as f:
            f.write(b"page")
        if self.fail:
            return False
        return True


def _wait_for_refs(cache, archive, n):
    """Block until *n* requests have joined *archive*'s entry."""
    key = os.path.realpath(archive)
    tick = threading.Event()
    for _ in range(WAIT * 200):
        entry = cache.snapshot().get(key)
        if entry and entry[1] >= n:
            return
        tick.wait(0.005)
    raise AssertionError(f"only {entry and entry[1]} of {n} requests joined")


def _reader(cache, archive, results, idx, hold=None, entered=None):
    with cache.acquire(archive) as d:
        results[idx] = d
        if entered is not None:
            entered.set()
        if hold is not None:
            assert hold.wait(WAIT)


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "cache"
    r.mkdir()
    return r


def _archive(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"rar")
    return str(p)


def test_concurrent_requests_for_one_archive_extract_once(tmp_path, root):
    ex = FakeExtractor()
    cache = RarExtractCache(ex, root=str(root))
    arc = _archive(tmp_path, "a.cbr")
    n = 8
    results = [None] * n
    threads = [threading.Thread(target=_reader, args=(cache, arc, results, i)) for i in range(n)]
    for t in threads:
        t.start()
    assert ex.started_event(arc).wait(WAIT)
    # Every thread has joined the one entry before the extraction finishes.
    _wait_for_refs(cache, arc, n)
    ex.gate(arc).set()
    for t in threads:
        t.join(WAIT)
    assert len(ex.calls) == 1
    assert len(set(results)) == 1 and results[0]
    assert os.path.isfile(os.path.join(results[0], "page001.jpg"))
    assert os.path.basename(results[0]).startswith(ENTRY_PREFIX)


def test_different_archives_extract_concurrently(tmp_path, root):
    ex = FakeExtractor()
    cache = RarExtractCache(ex, root=str(root))
    a, b = _archive(tmp_path, "a.cbr"), _archive(tmp_path, "b.cbr")
    results = [None, None]
    ta = threading.Thread(target=_reader, args=(cache, a, results, 0))
    tb = threading.Thread(target=_reader, args=(cache, b, results, 1))
    ta.start()
    assert ex.started_event(a).wait(WAIT)
    tb.start()
    # b starts while a is still blocked: no global lock held during extraction.
    assert ex.started_event(b).wait(WAIT)
    ex.gate(b).set()
    tb.join(WAIT)
    assert results[1]
    assert ta.is_alive(), "a should still be extracting"
    ex.gate(a).set()
    ta.join(WAIT)
    assert results[0] and results[0] != results[1]


def test_failure_reaches_every_waiter_leaves_nothing_and_is_retried(tmp_path, root):
    ex = FakeExtractor(fail=True)
    cache = RarExtractCache(ex, root=str(root))
    arc = _archive(tmp_path, "bad.cbr")
    n = 5
    results = ["unset"] * n
    threads = [threading.Thread(target=_reader, args=(cache, arc, results, i)) for i in range(n)]
    for t in threads:
        t.start()
    assert ex.started_event(arc).wait(WAIT)
    _wait_for_refs(cache, arc, n)
    ex.gate(arc).set()
    for t in threads:
        t.join(WAIT)
    assert results == [None] * n
    assert len(ex.calls) == 1
    assert cache.entry_count() == 0
    assert list(root.iterdir()) == []  # the partial staging folder is gone

    ex.fail = False
    with cache.acquire(arc) as d:
        assert d and os.path.isfile(os.path.join(d, "page001.jpg"))
    assert len(ex.calls) == 2


def test_extractor_exception_is_a_failure(tmp_path, root):
    def boom(archive, dest):
        open(os.path.join(dest, "half.jpg"), "wb").close()
        raise RuntimeError("tool crashed")

    cache = RarExtractCache(boom, root=str(root))
    with cache.acquire(_archive(tmp_path, "x.cbr")) as d:
        assert d is None
    assert cache.entry_count() == 0
    assert list(root.iterdir()) == []


def test_eviction_skips_in_use_and_in_progress_and_removes_idle(tmp_path, root):
    ex = FakeExtractor()
    cache = RarExtractCache(ex, root=str(root), max_entries=1)
    a, b, c = (_archive(tmp_path, n) for n in ("a.cbr", "b.cbr", "c.cbr"))

    # a: ready and held by a reader.
    ex.gate(a).set()
    res = [None, None, None]
    hold_a, entered_a = threading.Event(), threading.Event()
    ta = threading.Thread(target=_reader, args=(cache, a, res, 0, hold_a, entered_a))
    ta.start()
    assert entered_a.wait(WAIT)
    dir_a = res[0]

    # b: still extracting.
    tb = threading.Thread(target=_reader, args=(cache, b, res, 1))
    tb.start()
    assert ex.started_event(b).wait(WAIT)

    # c completes and is released. Two ready entries against a bound of one,
    # but a is in use and b is not ready, so c is the only evictable entry.
    ex.gate(c).set()
    with cache.acquire(c) as dir_c:
        assert dir_c
    snap = cache.snapshot()
    assert os.path.isdir(dir_a), "an in-use entry was removed"
    assert os.path.realpath(a) in snap
    assert os.path.realpath(b) in snap, "an in-progress entry was evicted"
    assert os.path.realpath(c) not in snap and not os.path.exists(dir_c)

    # b finishes: a is still held, so b (idle, ready) is the one to go once
    # the bound is exceeded.
    ex.gate(b).set()
    tb.join(WAIT)
    assert os.path.isdir(dir_a)
    assert set(cache.snapshot()) == {os.path.realpath(a)}
    assert not os.path.exists(res[1])

    # Release a: it is now the only entry, within the bound, and kept.
    hold_a.set()
    ta.join(WAIT)
    assert set(cache.snapshot()) == {os.path.realpath(a)}
    assert os.path.isdir(dir_a)


def test_lru_idle_entry_is_evicted_and_table_shrinks(tmp_path, root):
    ex = FakeExtractor(block=False)
    cache = RarExtractCache(ex, root=str(root), max_entries=2)
    paths = [_archive(tmp_path, f"{i}.cbr") for i in range(5)]
    dirs = []
    for p in paths:
        with cache.acquire(p) as d:
            dirs.append(d)
    assert cache.entry_count() == 2
    assert set(cache.snapshot()) == {os.path.realpath(p) for p in paths[-2:]}
    for d in dirs[:-2]:
        assert not os.path.exists(d)
    # Only the two live entries remain on disk.
    assert len(list(root.iterdir())) == 2

    # A cached archive is reused, not re-extracted.
    with cache.acquire(paths[-1]) as d:
        assert d == dirs[-1]
    assert len(ex.calls) == 5


def test_entry_removed_behind_the_cache_is_re_extracted(tmp_path, root):
    import shutil

    ex = FakeExtractor(block=False)
    cache = RarExtractCache(ex, root=str(root))
    arc = _archive(tmp_path, "a.cbr")
    with cache.acquire(arc) as d:
        pass
    shutil.rmtree(d)
    with cache.acquire(arc) as d2:
        assert d2 and os.path.isdir(d2)
    assert len(ex.calls) == 2


def test_refuses_to_remove_anything_outside_its_root(tmp_path, root):
    cache = RarExtractCache(FakeExtractor(block=False), root=str(root))
    outside = tmp_path / (ENTRY_PREFIX + "victim")
    outside.mkdir()
    foreign = root / "not_ours"
    foreign.mkdir()
    assert cache._remove_owned_dir(str(outside)) is False
    assert cache._remove_owned_dir(str(foreign)) is False
    assert outside.is_dir() and foreign.is_dir()


def test_shutdown_removes_its_folders_and_owned_root(tmp_path):
    cache = RarExtractCache(FakeExtractor(block=False))  # private temp root
    with cache.acquire(_archive(tmp_path, "a.cbr")) as d:
        root = os.path.dirname(d)
    cache.shutdown()
    assert not os.path.exists(d)
    assert not os.path.exists(root)
    assert cache.entry_count() == 0


def test_pages_are_read_inside_the_cache_reference():
    """The fallback (core/comic_pages.py, moved out of app.py) must hold the
    cache's reference for as long as it reads from the extracted folder."""
    root = Path(__file__).resolve().parents[2]
    src = (root / "core" / "comic_pages.py").read_text(encoding="utf-8")
    app_src = (root / "app.py").read_text(encoding="utf-8")
    assert "_rar_cache" not in app_src, "one cache, in core/comic_pages.py"
    tree = ast.parse(src)
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_get_full_rar_extraction" not in funcs
    assert "_rar_extract_cache" not in src
    body = funcs["_extract_single_rar_entry"]
    withs = [n for n in ast.walk(body) if isinstance(n, ast.With)]
    acquire = [
        w for w in withs
        if any(
            isinstance(i.context_expr, ast.Call)
            and getattr(i.context_expr.func, "attr", None) == "acquire"
            for i in w.items
        )
    ]
    assert acquire, "the full-extraction fallback must read under _rar_cache.acquire"
    # The file is opened inside the with block, not after it.
    opens = [
        n for n in ast.walk(acquire[0])
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "open"
    ]
    assert opens
    assert "atexit.register(_rar_cache.shutdown)" in src
