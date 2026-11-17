"""Ratchet: no bare ``path.startswith(<root>)`` containment checks.

``"/data2/x".startswith("/data")`` is True, so a containment decision written
as ``p.startswith(root)`` puts a sibling folder inside the root, and one built
on ``normpath``/``abspath`` alone is fooled by a symlink. Containment goes
through ``helpers.library.is_within_roots`` / ``is_valid_library_path`` /
``is_allowed_path`` or ``core.path_access.resolve_served_path``, which compare
realpaths on a separator boundary.

The rule is deliberately narrow so it stays precise: it flags
``x.startswith(arg)`` where ``arg`` is a direct ``os.path.normpath/abspath/
realpath(...)`` call, or a bare name/attribute whose name ends in ``dir``,
``root`` or ``folder``. ``startswith(root + os.sep)`` (a BinOp) and tuples of
prefixes built with a separator are not flagged. Sites that are not a
containment decision are listed in ``ALLOWED`` with the reason.
"""
import ast
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCANNED_DIRS = ("routes", "core", "helpers", "models", "cbz_ops")
SCANNED_FILES = ("app.py", "monitor.py")

_PATH_CALLS = {"normpath", "abspath", "realpath"}
_ROOTISH_SUFFIXES = ("dir", "root", "folder")

# (relative file, enclosing function) -> why it is not a containment decision.
ALLOWED = {
    # TARGET exclusion for the file index, only reached when commonpath/
    # samefile raised; deciding "do not index" is not an access decision.
    ("app.py", "is_in_target_dir"): "index exclusion fallback, not access control",
    # Path *translation* to the legacy /data form; the separator is checked
    # on the very next line.
    ("core/metadata_scanner.py", "queue_file_for_scan"): "separator checked after",
    # Rewrites a path the AirDC++ host reported (another machine's namespace,
    # where realpath means nothing); the separator is checked right after.
    ("models/dcpp.py", "translate_remote_path"): "remote path translation, separator checked after",
}


def _iter_files():
    for d in SCANNED_DIRS:
        for root, _dirs, files in os.walk(os.path.join(REPO, d)):
            for name in files:
                if name.endswith(".py"):
                    yield os.path.join(root, name)
    for name in SCANNED_FILES:
        path = os.path.join(REPO, name)
        if os.path.exists(path):
            yield path


def _name_of(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_flagged_arg(arg):
    if isinstance(arg, ast.Call):
        func = arg.func
        return (isinstance(func, ast.Attribute) and func.attr in _PATH_CALLS
                and isinstance(func.value, ast.Attribute) and func.value.attr == "path")
    name = _name_of(arg)
    return bool(name) and name.lower().endswith(_ROOTISH_SUFFIXES)


def _violations():
    found = []
    for path in _iter_files():
        rel = os.path.relpath(path, REPO).replace(os.sep, "/")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=rel)
        stack = []

        def visit(node):
            is_func = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_func:
                stack.append(node.name)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "startswith" and len(node.args) == 1
                    and _is_flagged_arg(node.args[0])):
                func = stack[-1] if stack else "<module>"
                if (rel, func) not in ALLOWED:
                    found.append(f"{rel}:{node.lineno} in {func}()")
            for child in ast.iter_child_nodes(node):
                visit(child)
            if is_func:
                stack.pop()

        visit(tree)
    return found


def test_no_bare_startswith_containment():
    found = _violations()
    assert not found, (
        "Bare startswith(<root>) containment (use helpers.library.is_within_roots "
        "or core.path_access.resolve_served_path):\n  " + "\n  ".join(found)
    )


@pytest.mark.parametrize("key", sorted(ALLOWED))
def test_allowed_entries_still_exist(key):
    """An allow-list entry whose site is gone must be removed, not kept."""
    rel, func = key
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert func in names
