"""A function must not read a name that it later imports locally.

``from datetime import datetime, timedelta`` sat inside a ``try`` in the
per-issue loop of ``scheduled_getcomics_download`` (app.py:1475). That one line
made ``datetime`` a local for the *whole* function, so
``cooldown_now = datetime.now()`` -- 236 lines earlier, and four lines after the
"Starting scheduled GetComics auto-download..." log -- raised "cannot access
local variable 'datetime'". Every scheduled auto-download died there, before it
searched for anything.

The same mistake in ``config_page`` (app.py:7228 shadowing app.py:136) made
``get_provider_credentials`` a local, so saving Metron credentials from the
settings form raised inside a ``try`` ending ``except Exception: pass`` and
silently did nothing -- taking the ComicVine key save on the next lines with it.

Both are invisible to ``py_compile``, to an unused-import check, and to any test
that does not execute the exact branch; and app.py cannot be imported in tests
at all. So the rule is asserted against the parsed AST of every module.

The rule is deliberately "reads a name it *later* imports locally", not the
flatter "no local import may shadow a module-level import". The flat form fires
on 81 sites in this repo, nearly all of them harmless idiomatic re-imports, and
carrying a baseline allow-list for those would go green on the next real one.

Known limitation: this compares line numbers, not execution order. A use that is
textually later but runs earlier -- inside a loop, or a callback -- is not
caught. Nested functions are skipped on purpose: a closure cell is bound by call
time, so counting those would be a false positive.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SKIP_DIRS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "tests",
    "venv",
}

_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPE_NODES = _FUNCTION_NODES + (ast.Lambda, ast.ClassDef)


def _python_files():
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _module_import_names(tree):
    """Names bound by imports in the module body itself."""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    continue
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _own_scope(node):
    """Walk *node*'s body without descending into a nested scope."""
    stack = list(getattr(node, "body", []))
    while stack:
        current = stack.pop()
        yield current
        for child in ast.iter_child_nodes(current):
            if isinstance(child, _SCOPE_NODES):
                continue
            stack.append(child)


def _violations(path):
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:  # pragma: no cover - a broken file is another test's job
        return []

    module_names = _module_import_names(tree)
    if not module_names:
        return []

    found = []
    for func in ast.walk(tree):
        if not isinstance(func, _FUNCTION_NODES):
            continue

        local_imports = {}
        reads = {}
        for node in _own_scope(func):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    bound = alias.asname or alias.name.split(".")[0]
                    if bound in module_names:
                        line = local_imports.get(bound)
                        if line is None or node.lineno < line:
                            local_imports[bound] = node.lineno
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id in module_names:
                    line = reads.get(node.id)
                    if line is None or node.lineno < line:
                        reads[node.id] = node.lineno

        for name, import_line in sorted(local_imports.items()):
            read_line = reads.get(name)
            if read_line is not None and read_line < import_line:
                found.append((path, func.name, name, read_line, import_line))
    return found


@pytest.fixture(scope="module")
def shadowed():
    return [v for path in _python_files() for v in _violations(path)]


def test_no_function_reads_a_name_it_later_imports_locally(shadowed):
    assert not shadowed, "\n".join(
        "{}:{} reads '{}', which {}() imports locally at line {} -- that makes "
        "the name a function-local for the whole body, so this read raises "
        "UnboundLocalError".format(
            os.path.relpath(path, PROJECT_ROOT), read_line, name, func, import_line
        )
        for path, func, name, read_line, import_line in shadowed
    )


def test_the_detector_catches_the_original_bug():
    """The rule has to still fire on the shape that caused this, or it is decor."""
    import tempfile

    source = (
        "from datetime import datetime, timedelta\n"
        "\n"
        "def sweep():\n"
        "    now = datetime.now()\n"
        "    try:\n"
        "        from datetime import datetime, timedelta\n"
        "        other = datetime.now()\n"
        "    except Exception:\n"
        "        pass\n"
        "    return now, other\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sample.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        found = _violations(path)

    assert [(v[1], v[2], v[3], v[4]) for v in found] == [("sweep", "datetime", 4, 6)]


def test_a_local_import_used_only_after_itself_is_not_flagged():
    """The common, harmless re-import must stay silent or the test is noise."""
    import tempfile

    source = (
        "from core.database import get_schedule\n"
        "\n"
        "def configure():\n"
        "    from core.database import get_schedule\n"
        "    return get_schedule('rebuild')\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sample.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        assert _violations(path) == []
