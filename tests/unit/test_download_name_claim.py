"""Atomic reservation of a download's destination name (core.download_utils).

api.py runs three download workers against one WATCH directory. The destination
name used to be chosen with ``while os.path.exists(final): bump the counter`` --
a test that only ever looks at the *finished* ``.cbz``, which does not exist
while a download is running. So two workers picked the same name one second
apart, wrote into the same ``..._4.cbz.0.crdownload``, and the loser died with
"Temp file not found" when the winner renamed it away:

    22:05:15,551  Temp file path: ..._4.cbz.0.crdownload
    22:05:16,543  Temp file path: ..._4.cbz.0.crdownload
    22:05:19,343  Attempt 1 failed with error: Temp file not found: ..._4.cbz.0.crdownload

The claim lives in core.download_utils rather than api.py so it can be tested
here -- api.py starts worker threads and a cloudscraper session at import time
and cannot be imported by the suite at all. The api.py wiring is asserted by AST
below, the same way the auto-retry policy is.
"""

import ast
import os
import threading

import pytest

from core.download_utils import (
    CLAIM_SUFFIX,
    claim_download_path,
    release_download_claim,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API_PATH = os.path.join(PROJECT_ROOT, "api.py")


@pytest.fixture(scope="module")
def download_getcomics_node():
    with open(API_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "download_getcomics":
            return node
    pytest.fail("download_getcomics not found in api.py")


class TestClaimDownloadPath:

    def test_first_claim_takes_the_plain_name(self, tmp_path):
        final, claim = claim_download_path(str(tmp_path), "Batman 1.cbz")

        assert os.path.basename(final) == "Batman 1.cbz"
        assert claim == final + CLAIM_SUFFIX
        assert os.path.exists(claim), "the claim must exist to block a second thread"

    def test_a_held_name_is_not_handed_out_twice(self, tmp_path):
        """The regression: nothing is on disk under the final name yet."""
        first, _ = claim_download_path(str(tmp_path), "Batman 1.cbz")
        second, _ = claim_download_path(str(tmp_path), "Batman 1.cbz")

        assert first != second
        assert os.path.basename(second) == "Batman 1_1.cbz"
        assert not os.path.exists(first), (
            "the old code's only test -- the finished file -- is exactly what "
            "does not exist during a download"
        )

    def test_a_finished_file_of_that_name_is_skipped(self, tmp_path):
        (tmp_path / "Batman 1.cbz").write_bytes(b"already here")

        final, _ = claim_download_path(str(tmp_path), "Batman 1.cbz")

        assert os.path.basename(final) == "Batman 1_1.cbz"

    def test_claims_step_past_every_taken_name(self, tmp_path):
        names = [claim_download_path(str(tmp_path), "Batman 1.cbz")[0] for _ in range(5)]

        assert len(set(names)) == 5
        assert [os.path.basename(n) for n in names] == [
            "Batman 1.cbz", "Batman 1_1.cbz", "Batman 1_2.cbz",
            "Batman 1_3.cbz", "Batman 1_4.cbz",
        ]

    def test_releasing_frees_the_name_again(self, tmp_path):
        """A retry must not leak the name it was holding."""
        final, claim = claim_download_path(str(tmp_path), "Batman 1.cbz")
        release_download_claim(claim)

        again, _ = claim_download_path(str(tmp_path), "Batman 1.cbz")

        assert again == final

    def test_release_is_safe_to_call_twice_and_on_none(self, tmp_path):
        _, claim = claim_download_path(str(tmp_path), "Batman 1.cbz")
        release_download_claim(claim)
        release_download_claim(claim)   # already gone
        release_download_claim(None)    # never taken

    def test_the_marker_is_invisible_to_the_monitor(self, tmp_path):
        """WATCH is the download dir, so the marker must read as a temp file.

        monitor.py matches '.crdownload' anywhere in the name. A plain '.claim'
        would be treated as a comic and moved to TARGET.
        """
        import monitor

        _, claim = claim_download_path(str(tmp_path), "Batman 1.cbz")
        handler = monitor.DownloadCompleteHandler.__new__(monitor.DownloadCompleteHandler)

        assert ".crdownload" in CLAIM_SUFFIX
        assert monitor.DownloadCompleteHandler._is_temporary_download_file(
            handler, claim, os.path.splitext(claim)[1]
        )

    def test_extension_is_preserved_when_stepping(self, tmp_path):
        claim_download_path(str(tmp_path), "Pack.zip")
        final, _ = claim_download_path(str(tmp_path), "Pack.zip")

        assert os.path.basename(final) == "Pack_1.zip"

    def test_concurrent_claims_are_all_distinct(self, tmp_path):
        """The reported bug, reproduced: N threads, one name, no duplicates."""
        results = []
        lock = threading.Lock()
        start = threading.Barrier(12)

        def worker():
            start.wait()
            final, _ = claim_download_path(str(tmp_path), "Star Wars 23.cbz")
            with lock:
                results.append(final)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 12
        assert len(set(results)) == 12, (
            f"two threads claimed the same destination: "
            f"{sorted(n for n in results if results.count(n) > 1)}"
        )


class TestApiWiring:
    """api.py cannot be imported, so its use of the claim is checked by AST."""

    def test_the_exists_scan_is_gone(self, download_getcomics_node):
        loops = [
            n for n in ast.walk(download_getcomics_node)
            if isinstance(n, ast.While)
            and any(isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "exists"
                    for c in ast.walk(n.test))
        ]
        assert not loops, (
            "download_getcomics still picks its destination with a "
            "`while os.path.exists(...)` scan, which is not atomic"
        )

    def test_it_claims_the_path(self, download_getcomics_node):
        calls = [
            c for c in ast.walk(download_getcomics_node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "claim_download_path"
        ]
        assert calls, "download_getcomics never reserves its destination name"

    def test_the_claim_is_taken_before_the_retry_loop(self, download_getcomics_node):
        """Held across attempts, not re-taken per attempt.

        The temp file carries the attempt number, so a per-attempt claim would
        release and re-take the reservation on every retry and reopen the very
        window this closes.
        """
        retry_loop = next(
            (n for n in download_getcomics_node.body
             if isinstance(n, ast.For)
             and isinstance(n.target, ast.Name) and n.target.id == "attempt"),
            None,
        )
        assert retry_loop is not None, "the `for attempt in range(retries)` loop moved"

        initialised_before = [
            stmt for stmt in download_getcomics_node.body
            if isinstance(stmt, ast.Assign)
            and stmt.lineno < retry_loop.lineno
            and any(isinstance(t, ast.Name) and t.id == "claimed_claim"
                    for t in ast.walk(stmt))
        ]
        assert initialised_before, (
            "the claim variables are not initialised before the retry loop, so "
            "the reservation cannot be held across attempts"
        )

    def test_the_claim_is_released_on_success_and_on_failure(self, download_getcomics_node):
        releases = [
            c for c in ast.walk(download_getcomics_node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "release_download_claim"
        ]
        assert len(releases) >= 4, (
            "the claim must be released on every way out -- success, "
            "all-retries-failed, and both cancel returns -- or a name is held "
            "for the life of the process and every later download steps past it"
        )

    def test_no_return_leaves_the_claim_held(self, download_getcomics_node):
        releases = {
            c.lineno for c in ast.walk(download_getcomics_node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "release_download_claim"
        }
        leaked = [
            r.lineno for r in ast.walk(download_getcomics_node)
            if isinstance(r, ast.Return)
            and not any(abs(r.lineno - rel) <= 12 for rel in releases)
        ]
        assert not leaked, (
            f"return(s) at line(s) {leaked} exit download_getcomics without "
            f"releasing the destination claim"
        )

    def test_rename_is_portable(self, download_getcomics_node):
        """os.rename refuses an existing destination on Windows."""
        renames = [
            c for c in ast.walk(download_getcomics_node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr == "rename"
        ]
        assert not renames, "use os.replace, not os.rename, for the final move"


# ---------------------------------------------------------------------------
# The other three downloaders were left on the racy loop (#multiFile report)
# ---------------------------------------------------------------------------

OTHER_DOWNLOADERS = (
    "download_pixeldrain",
    "download_comicbookplus",
    "download_mega",
)


@pytest.fixture(scope="module")
def api_tree():
    with open(API_PATH, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name} not found in api.py")


class TestEveryDownloaderReservesItsName:
    """#578 moved download_getcomics onto the claim and left the rest behind.

    A reported log shows the consequence: PixelDrain picked
    "..._1.cbr.part", the hourly orphan sweep unlinked it mid-transfer, and the
    final os.replace failed with ENOENT -- a retryable failure, so the download
    was re-queued under a fresh "_N" name and the cycle repeated.
    """

    @pytest.mark.parametrize("name", OTHER_DOWNLOADERS)
    def test_the_exists_scan_is_gone(self, api_tree, name):
        node = _func(api_tree, name)
        loops = [
            n for n in ast.walk(node)
            if isinstance(n, ast.While)
            and any(isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "exists"
                    for c in ast.walk(n.test))
        ]
        assert not loops, (
            f"{name} still picks its destination with a "
            f"`while os.path.exists(...)` scan, which is not atomic"
        )

    @pytest.mark.parametrize("name", OTHER_DOWNLOADERS)
    def test_it_claims_the_path(self, api_tree, name):
        node = _func(api_tree, name)
        calls = [
            c for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "claim_download_path"
        ]
        assert calls, f"{name} never reserves its destination name"

    @pytest.mark.parametrize("name", OTHER_DOWNLOADERS)
    def test_the_claim_is_released_in_a_finally(self, api_tree, name):
        """A marker left behind pushes every later download to "_1"."""
        node = _func(api_tree, name)
        tries = [n for n in ast.walk(node) if isinstance(n, ast.Try) and n.finalbody]
        released = [
            c
            for t in tries for stmt in t.finalbody
            for c in ast.walk(stmt)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "release_download_claim"
        ]
        assert released, f"{name} must release its claim on every exit path"

    @pytest.mark.parametrize("name", ("download_pixeldrain", "download_comicbookplus"))
    def test_the_part_file_derives_from_the_claimed_name(self, api_tree, name):
        """An unreserved ".part" is as racy as an unreserved destination."""
        node = _func(api_tree, name)
        assignments = [
            stmt for stmt in ast.walk(node)
            if isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "tmp_path" for t in stmt.targets)
        ]
        assert assignments, f"{name} has no tmp_path"
        for stmt in assignments:
            assert any(
                isinstance(n, ast.Name) and n.id == "out_path"
                for n in ast.walk(stmt.value)
            ), f"{name} builds tmp_path from something other than the claimed out_path"


class TestPixeldrainHoldsTheNameWhileAPartialRemains:
    """PixelDrain resumes from its ".part" and the retry recomputes the
    destination from scratch, so releasing the name while a partial is still
    there would let another worker resume *our* bytes into its own file."""

    def test_the_release_is_guarded_on_the_partial(self, api_tree):
        node = _func(api_tree, "download_pixeldrain")
        tries = [n for n in ast.walk(node) if isinstance(n, ast.Try) and n.finalbody]
        guarded = False
        for t in tries:
            for stmt in t.finalbody:
                if not isinstance(stmt, ast.If):
                    continue
                mentions_tmp = any(
                    isinstance(n, ast.Name) and n.id == "tmp_path"
                    for n in ast.walk(stmt.test)
                )
                releases = any(
                    isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id == "release_download_claim"
                    for c in ast.walk(stmt)
                )
                if mentions_tmp and releases:
                    guarded = True
        assert guarded, (
            "the PixelDrain release must be conditional on no partial remaining"
        )


class TestComicBookPlusDropsItsPartial:
    """It always opens tmp_path 'wb', so a failed attempt leaves nothing worth
    reserving the name for -- and a kept partial accumulates under a name no
    retry will pick."""

    def test_the_partial_is_removed_in_the_finally(self, api_tree):
        node = _func(api_tree, "download_comicbookplus")
        tries = [n for n in ast.walk(node) if isinstance(n, ast.Try) and n.finalbody]
        removes = [
            c
            for t in tries for stmt in t.finalbody
            for c in ast.walk(stmt)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute) and c.func.attr == "remove"
            and any(isinstance(a, ast.Name) and a.id == "tmp_path" for a in c.args)
        ]
        assert removes, "a failed ComicBookPlus attempt must not strand its .part"


class TestMegaClaimIsBoundBeforeTheTry:
    """get_metadata() is where MEGA's rate limiter answers, so the common
    failure happens before a name is ever reserved -- and an unbound name in the
    finally would raise there, replacing the real error."""

    def test_claim_path_is_initialised_first(self, api_tree):
        node = _func(api_tree, "download_mega")
        # The try that takes the claim, not the import guard above it.
        claiming_try = next(
            (i for i, stmt in enumerate(node.body)
             if isinstance(stmt, ast.Try)
             and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                     and c.func.id == "claim_download_path"
                     for c in ast.walk(stmt))),
            None,
        )
        assert claiming_try is not None, "download_mega never claims a name"
        assigned_before = [
            stmt for stmt in node.body[:claiming_try]
            if isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "claim_path"
                    for t in stmt.targets)
        ]
        assert assigned_before, "claim_path must be bound before the try block"
