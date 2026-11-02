"""CLU must not create the *default* WATCH/TARGET folders on its own (#699).

WATCH and TARGET are settings (``user_preferences``). A user who configured
both elsewhere still got ``temp`` and ``processed`` folders on their
``/downloads`` share, because three places created the defaults regardless:

- ``entrypoint.sh`` ran ``mkdir -p /downloads/{temp,processed}`` on every start,
  and ``/downloads`` is a bind mount, so they landed on the host;
- ``api.py`` ``os.makedirs``'d its import-time WATCH snapshot. It is imported
  before ``init_db()``, so a fresh install or a failed settings read resolves to
  the default there;
- the ``Dockerfile`` baked them into the image.

The configured folders are created on first use instead (``monitor.main``,
``api._download_dir``). None of these files can be executed in a test, so this
asserts against their text and AST.
"""
import ast
import os
import re

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
DEFAULT_DIRS = ("/downloads/temp", "/downloads/processed")


def _read(name):
    with open(os.path.join(PROJECT_ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def _code_lines(text):
    return [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


class TestEntrypoint:
    def test_does_not_mkdir_default_watch_or_target(self):
        for ln in _code_lines(_read("entrypoint.sh")):
            if re.search(r"\bmkdir\b", ln):
                assert "temp" not in ln and "processed" not in ln, ln

    def test_no_loop_builds_the_default_subdirs(self):
        code = "\n".join(_code_lines(_read("entrypoint.sh")))
        assert not re.search(r"for\s+\w+\s+in\s+temp\s+processed", code)

    def test_write_probes_skip_missing_folders(self):
        # A probe that touches a folder that was never created would report a
        # healthy configured-elsewhere install as unwritable.
        code = "\n".join(_code_lines(_read("entrypoint.sh")))
        for d in DEFAULT_DIRS:
            assert f"touch {d}/" not in code


class TestDockerfile:
    def test_image_does_not_create_default_subdirs(self):
        for ln in _code_lines(_read("Dockerfile")):
            if "mkdir" in ln:
                for d in DEFAULT_DIRS:
                    assert d not in ln, ln


class TestApiImport:
    def test_no_module_level_makedirs(self):
        tree = ast.parse(_read("api.py"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in ("makedirs", "mkdir")
                ):
                    raise AssertionError(
                        f"api.py line {sub.lineno}: module-level {sub.func.attr}() "
                        "creates a folder at import, before settings are loaded"
                    )

    def test_download_dir_still_creates_on_demand(self):
        tree = ast.parse(_read("api.py"))
        fn = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_download_dir"
        )
        calls = {
            s.func.attr for s in ast.walk(fn)
            if isinstance(s, ast.Call) and isinstance(s.func, ast.Attribute)
        }
        assert "makedirs" in calls
