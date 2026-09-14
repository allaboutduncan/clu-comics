"""Structural tests for ``entrypoint.sh``.

The script can be neither imported nor executed on the test host -- it needs
Linux and root -- so this asserts against its text, the same way
``test_folder_thumbnail_orchestrator.py`` asserts against ``app.py``.

It pins three properties, each of which was violated at once and produced the
same symptom: a container that printed nothing for over two minutes after
``docker run`` and read as a broken deploy.

**Audible.** The script's first ``echo`` used to be a hundred lines in, after
every expensive thing it does.

**Bounded.** ``/cache`` holds one thumbnail JPEG per comic. It was added to an
unbounded ``find`` ownership walk, which has to lstat every one of them on every
start -- correct ownership skips the *chown*, never the *walk*.

**Unkillable.** ``set -euo pipefail`` is on, so an unguarded ``chown`` (EPERM on
a CIFS/NFS bind mount, even for root) or ``find | xargs`` pipeline (find exits 1
on a traversal error, xargs 123 on a failed chown) ends the container mid-script
with no message whatsoever.
"""
import os
import re

import pytest

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
ENTRYPOINT = os.path.join(PROJECT_ROOT, "entrypoint.sh")
DOCKERFILE = os.path.join(PROJECT_ROOT, "Dockerfile")


@pytest.fixture(scope="module")
def script():
    with open(ENTRYPOINT, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def lines(script):
    """Logical lines: continuations joined, blanks and comments dropped.

    A comment mentioning ``/cache`` must never be able to satisfy an assertion
    about code -- the block being tested is heavily commented precisely because
    the reasoning is not obvious.
    """
    joined = re.sub(r"\\\n\s*", " ", script)
    return [
        ln
        for ln in joined.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def _index_of(lines, predicate, what):
    for i, ln in enumerate(lines):
        if predicate(ln):
            return i
    pytest.fail(f"{what} not found in entrypoint.sh")


def _enclosing_for_list(lines, i):
    """The ``for ... in ...`` header governing line ``i``.

    Membership assertions resolve through this rather than searching the whole
    file, so they survive reordering and cannot be satisfied by an unrelated
    mention of the path.
    """
    for j in range(i, -1, -1):
        if lines[j].lstrip().startswith("for "):
            return lines[j]
    pytest.fail("no enclosing for-loop")


def _is_per_dir_walk(line):
    return "xargs" in line and "chown" in line and '"$d"' in line


def _walk_line(lines):
    """The ownership walk inside the per-directory loop."""
    return lines[_index_of(lines, _is_per_dir_walk, "the per-directory chown pipeline")]


def _depth(script, name):
    m = re.search(rf"^{name}=(\d+)$", script, re.M)
    assert m, f"{name} is no longer defined as a bare integer"
    return int(m.group(1))


class TestTheOwnershipWalkIsBounded:
    """An unbounded walk lstats one inode per comic on every single start."""

    def test_the_nested_walk_is_depth_capped(self, lines):
        assert "-maxdepth" in _walk_line(lines)

    def test_the_cache_cap_is_two(self, script):
        assert _depth(script, "CACHE_WALK_DEPTH") == 2

    def test_the_cap_matches_the_real_cache_layout(self, script):
        """Ties the number to the path formula, not to a comment.

        Change the shard width or add a level in ``core/thumbnail_cache.py`` and
        this fires: a JPEG must sit exactly one level below the deepest thing
        the walk chowns.
        """
        from core.thumbnail_cache import thumbnail_cache_path

        rel = os.path.relpath(
            thumbnail_cache_path("/data/Series/Issue 001.cbz", cache_dir="/cache"),
            "/cache",
        ).replace("\\", "/")
        jpeg_depth = len(rel.split("/"))  # thumbnails/b9/<md5>.jpg -> 3
        assert _depth(script, "CACHE_WALK_DEPTH") == jpeg_depth - 1

    def test_the_cap_still_reaches_the_shard_directories(self, script):
        """Depth 1 would leave a root-owned ``/cache/thumbnails/<shard>/``, and
        ``mkstemp(dir=shard_dir)`` inside ``write_cached_thumbnail`` would fail
        with EACCES -- #548 all over again.
        """
        assert _depth(script, "CACHE_WALK_DEPTH") >= 2

    def test_the_cap_is_selected_per_directory(self, script):
        assert re.search(
            r"/cache\)\s*walk_depth=", script
        ), "the depth cap is no longer chosen per-directory"

    def test_config_is_not_capped_shallow(self, script):
        """``/config/.cache/<provider>/cache.sqlite`` is at depth 3 and IS
        opened in place, as are the DB's WAL/SHM sidecars and the log files.
        """
        assert _depth(script, "FULL_WALK_DEPTH") >= 8

    def test_the_trash_is_walked_in_full(self, lines):
        """``move_to_trash`` moves whole directories in, so trash contents go
        below the depth-2 bound, and evicting one needs write+execute on each
        directory inside it. It is size-capped, so a full walk stays cheap.
        """
        trash = lines[
            _index_of(
                lines,
                lambda l: "xargs" in l and "chown" in l and "/cache/trash" in l,
                "the trash chown pipeline",
            )
        ]
        assert "-maxdepth" not in trash

    def test_special_characters_are_still_handled(self, lines):
        walk = _walk_line(lines)
        assert "-print0" in walk and "xargs -0" in walk


class TestCacheStaysInEveryOwnershipPass:
    """#548: ``/cache`` appeared in none of these, so root-fallback starts left
    root-owned thumbnails behind. Bounding the walk is not licence to drop it.
    """

    def test_cache_is_in_the_chown_loop(self, lines):
        i = _index_of(lines, _is_per_dir_walk, "the per-directory chown pipeline")
        assert "/cache" in _enclosing_for_list(lines, i)

    def test_cache_is_in_the_setgid_list(self, lines):
        i = _index_of(lines, lambda l: "chmod g+s" in l, "the setgid pass")
        assert "/cache" in _enclosing_for_list(lines, i)

    def test_cache_is_in_the_writability_probe(self, lines):
        i = _index_of(
            lines,
            lambda l: "can_write" in l and l.lstrip().startswith("if"),
            "the can_write probe",
        )
        assert "/cache" in _enclosing_for_list(lines, i)

    def test_the_image_creates_cache(self):
        with open(DOCKERFILE, encoding="utf-8") as fh:
            body = re.sub(r"\\\n\s*", " ", fh.read())
        mkdir = [l for l in body.splitlines() if "mkdir -p" in l and "/app/logs" in l]
        assert mkdir and "/cache" in mkdir[0]


class TestNothingCanKillTheContainerSilently:

    def test_pipefail_is_still_on(self, script):
        """The premise of everything below. If this goes, they guard nothing."""
        assert "set -euo pipefail" in script

    def test_every_chown_is_guarded(self, lines):
        for ln in lines:
            if "chown" in ln:
                assert (
                    "||" in ln
                ), f"unguarded chown would abort the script: {ln.strip()}"

    def test_the_setgid_pass_is_still_guarded(self, lines):
        for ln in lines:
            if "chmod g+s" in ln:
                assert "||" in ln


class TestStartupIsAudible:

    SLOW = ("groupadd", "useradd", "getent", "chown", "find ", "gosu", "stat -c")

    def test_something_is_printed_before_anything_slow(self, lines):
        first_echo = _index_of(
            lines, lambda l: l.lstrip().startswith("echo "), "any echo"
        )
        first_slow = min(
            i for i, l in enumerate(lines) if any(tok in l for tok in self.SLOW)
        )
        assert first_echo < first_slow, (
            "entrypoint.sh does work before it prints anything; a slow ownership "
            "pass then looks like a container that never started"
        )

    def test_the_first_line_of_output_cannot_itself_fail(self, lines):
        """Under ``set -u`` an unset variable in the banner aborts the script --
        and the whole point of the banner is that it always prints.
        """
        first_echo = _index_of(
            lines, lambda l: l.lstrip().startswith("echo "), "any echo"
        )
        assert "$" not in lines[first_echo]

    def test_the_ownership_pass_announces_itself(self, lines):
        walk = _index_of(
            lines, lambda l: "xargs" in l and "chown" in l, "the chown pipeline"
        )
        assert any(
            l.lstrip().startswith("echo") and "ownership" in l.lower()
            for l in lines[:walk]
        )

    def test_the_ownership_pass_reports_how_long_it_took(self, lines):
        start = _index_of(
            lines,
            lambda l: l.lstrip().startswith("own_start="),
            "the ownership-pass start stamp",
        )
        assert any(
            l.lstrip().startswith("echo") and "own_start" in l for l in lines[start:]
        ), "nothing reports the elapsed time, so a slow walk is invisible again"

    def test_the_script_has_unix_line_endings(self):
        """A CRLF entrypoint.sh fails with a bare 'not found' from exec -- the
        same class of unexplained startup failure. .gitattributes pins eol=lf;
        this catches a checkout or editor that defeated it.
        """
        with open(ENTRYPOINT, "rb") as fh:
            assert b"\r\n" not in fh.read()
