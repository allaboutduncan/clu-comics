"""The library is wherever a library row says it is, not a hard-coded ``/data``.

``/data`` is only the default mount. A user who configured their library at
``/comics`` (with no ``/data`` volume at all) found the All Books view rejecting
every path, the stats page reporting no publishers, the file watcher not
running, moves leaving ``file_index`` stale, ComicVine auto-move writing into
the container's own filesystem, and trashing a library's last comic able to
delete the library root. Each was a literal ``'/data'`` or ``app.DATA_DIR``.

A path-less request starts at ``helpers.library.get_default_library_root()``;
"is this in the library?" is ``is_valid_library_path`` / ``get_library_for_path``
/ ``library_root_for``; "may this be deleted?" is ``get_protected_roots``.

This is a ratchet: the table below is every literal that is still allowed, and
it may only shrink. A new one fails here instead of on someone's NAS.

app.py cannot be imported in tests, so the app.py sites are asserted on the AST.
"""

import ast
import glob
import os
import re
from collections import Counter

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PY_GLOBS = ["app.py", "monitor.py", "missing.py", "routes/*.py", "core/*.py",
            "models/**/*.py", "helpers/*.py", "cbz_ops/*.py"]
WEB_GLOBS = ["static/js/*.js", "templates/**/*.html"]

# file -> how many hits it may still have. Shrink it; never grow it.
ALLOWED = {
    # The legacy default mount itself, and the accessor tests patch.
    "core/config.py": 3,
    # Seed a /data library on first start if that folder exists; a TARGET
    # *exclusion* fallback (twice) that never names a library.
    "core/database.py": 3,
    # Diagnostics: reports what is mounted at /data, if anything.
    "core/debug_package.py": 2,
    # Rewrites /data/... to get_data_dir()/... -- an identity today.
    "core/metadata_scanner.py": 2,
    # watch_target_verdict: WATCH/TARGET at /data is a hard 400 (target-scan.md).
    "helpers/library.py": 1,
    # API documentation examples.
    "routes/api_v1_docs.py": 12,
    # templates/index.html is not rendered by any route; index.js is its script.
    "static/js/index.js": 6,
    "templates/index.html": 1,
    # Fallbacks behind an injected defaultLibraryPath / the libraries API.
    "static/js/series.js": 3,
    "templates/series.html": 1,
    "templates/files.html": 2,
    # Input placeholders.
    "templates/publishers.html": 2,
}

_WEB_LITERAL = re.compile(r"""['"`]/data['"`/]""")
_WEB_COMMENT_PREFIXES = ("//", "*", "/*", "{#", "<!--")


def _files(globs):
    seen = []
    for pattern in globs:
        for path in sorted(glob.glob(os.path.join(PROJECT_ROOT, pattern), recursive=True)):
            rel = os.path.relpath(path, PROJECT_ROOT).replace(os.sep, "/")
            if rel not in seen:
                seen.append(rel)
    return seen


def _docstring_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                ids.add(id(first.value))
    return ids


def _python_hits(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    docstrings = _docstring_ids(tree)
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings
                and (node.value == "/data" or node.value.startswith("/data/"))):
            hits.append(f"{rel}:{node.lineno} {node.value!r}")
        elif isinstance(node, ast.Name) and node.id == "DATA_DIR":
            hits.append(f"{rel}:{node.lineno} DATA_DIR")
        elif isinstance(node, ast.alias) and node.name == "DATA_DIR":
            hits.append(f"{rel} imports DATA_DIR")
    return hits


def _web_hits(rel):
    hits = []
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if line.strip().startswith(_WEB_COMMENT_PREFIXES):
                continue
            if _WEB_LITERAL.search(line):
                hits.append(f"{rel}:{lineno} {line.strip()[:80]}")
    return hits


def _all_hits():
    hits = {}
    for rel in _files(PY_GLOBS):
        found = _python_hits(rel)
        if found:
            hits[rel] = found
    for rel in _files(WEB_GLOBS):
        found = _web_hits(rel)
        if found:
            hits[rel] = found
    return hits


class TestNoNewHardcodedDataRoot:

    def test_no_file_exceeds_its_allowance(self):
        over = []
        for rel, found in _all_hits().items():
            if len(found) > ALLOWED.get(rel, 0):
                over.append(f"{rel}: {len(found)} > {ALLOWED.get(rel, 0)}\n  "
                            + "\n  ".join(found))
        assert not over, (
            "Hard-coded /data (or DATA_DIR) found. Use get_default_library_root(), "
            "is_valid_library_path() or get_library_roots() instead:\n"
            + "\n".join(over)
        )

    def test_allowances_are_not_stale(self):
        """Ratchet: when a literal goes away, lower its allowance with it."""
        counts = Counter({rel: len(found) for rel, found in _all_hits().items()})
        stale = {rel: (n, counts[rel]) for rel, n in ALLOWED.items() if counts[rel] < n}
        assert not stale, f"Lower these allowances (allowed, actual): {stale}"


@pytest.fixture(scope="module")
def app_tree():
    with open(os.path.join(PROJECT_ROOT, "app.py"), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    pytest.fail(f"{name} not found in app.py")


def _called_names(node):
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name):
                names.add(sub.func.id)
            elif isinstance(sub.func, ast.Attribute):
                names.add(sub.func.attr)
    return names


class TestAppWiring:

    def test_move_hook_asks_whether_a_path_is_in_any_library(self, app_tree):
        """Comparing against /data made every move inside /comics look like
        outside -> outside, so the index was never updated."""
        assert "is_valid_library_path" in _called_names(
            _func(app_tree, "update_index_on_move"))

    @pytest.mark.parametrize("name", [
        "start_file_watcher_background", "prebuild_browse_cache", "warmup_cache",
    ])
    def test_startup_work_covers_every_library_root(self, app_tree, name):
        assert "get_library_roots" in _called_names(_func(app_tree, name))

    @pytest.mark.parametrize("name", [
        "auto_fetch_metron_metadata",
        "auto_fetch_comicvine_metadata",
        "auto_fetch_comicvine_sqlite_metadata",
    ])
    def test_auto_metadata_measures_depth_from_the_files_library(self, app_tree, name):
        assert "_is_nested_library_folder" in _called_names(_func(app_tree, name))

    def test_home_page_hands_the_grid_its_default_root(self, app_tree):
        """collection.js builds clean URLs relative to the default library."""
        index = _func(app_tree, "index")
        keywords = {kw.arg for call in ast.walk(index) if isinstance(call, ast.Call)
                    for kw in call.keywords}
        assert "default_library_root" in keywords
