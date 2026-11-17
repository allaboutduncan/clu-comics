"""Every external tool CLU runs from these packages runs with a timeout.

``unrar``, ``7z``, ``unar`` and ``lsar`` were run with ``subprocess.run(...)``
and no timeout in ``helpers`` and ``helpers.unwrap``. A tool that hangs -- a
damaged archive, a password prompt waiting on stdin, a NAS read that stalls --
then blocked its caller forever: for ``monitor.py``, which handles one file at
a time, that was the whole WATCH pipeline, and for a conversion a worker thread
that never returned.

Archive tools now go through ``helpers.run_archive_tool`` (timeout required,
``stdin`` is ``/dev/null``, own session), and the behaviour of a timed-out
attempt is pinned in ``test_rar_extract_strict.py``, ``test_unwrap.py``,
``test_monitor.py`` and ``tests/mocked/test_convert_to_cbz_contract.py``. This
file is the structural half, over the parsed source so a new call cannot slip
in without one:

- every ``subprocess.run`` / ``call`` / ``check_call`` / ``check_output`` in
  the scanned packages passes ``timeout=``;
- ``subprocess.Popen`` (which has no timeout of its own) appears only in the
  modules that start long-lived services on purpose, listed below with why;
- ``run_archive_tool`` itself passes ``timeout``, ``stdin=DEVNULL`` and
  ``start_new_session=True``, and its ``timeout`` has no default.

``app.py`` and ``api.py`` are not scanned here (their archive-tool calls
already carry timeouts and are owned elsewhere).
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCANNED_PACKAGES = ("helpers", "cbz_ops", "core", "models")
SCANNED_FILES = ("monitor.py",)

BLOCKING_CALLS = {"run", "call", "check_call", "check_output"}

# Popen is only for processes meant to outlive the call that starts them.
POPEN_ALLOWED = {
    # The bundled FlareSolverr service, started on first challenge and
    # stopped when idle.
    os.path.join("core", "flaresolverr_supervisor.py"),
    # Re-exec wrapper that execs FlareSolverr with PR_SET_PDEATHSIG.
    os.path.join("core", "flaresolverr_launch.py"),
    # The monitor.py child process, supervised for the worker's lifetime.
    os.path.join("core", "monitor_supervisor.py"),
}


def _scanned_files():
    for pkg in SCANNED_PACKAGES:
        root = os.path.join(PROJECT_ROOT, pkg)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in sorted(filenames):
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)
    for name in SCANNED_FILES:
        yield os.path.join(PROJECT_ROOT, name)


def _rel(path):
    return os.path.relpath(path, PROJECT_ROOT)


def _parse(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


def _subprocess_names(tree):
    """Local names bound by ``from subprocess import x [as y]``."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                names[alias.asname or alias.name] = alias.name
    return names


def _subprocess_attr(node, imported):
    """The subprocess function *node* refers to, or None."""
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "subprocess"):
        return node.attr
    if isinstance(node, ast.Name) and node.id in imported:
        return imported[node.id]
    return None


def _has_timeout(call):
    for kw in call.keywords:
        if kw.arg == "timeout":
            return not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
        if kw.arg is None:          # **kwargs: cannot tell, so refuse
            return False
    return False


def _violations():
    missing_timeout, stray_popen = [], []
    for path in _scanned_files():
        tree = _parse(path)
        imported = _subprocess_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = _subprocess_attr(node.func, imported)
                if fn in BLOCKING_CALLS and not _has_timeout(node):
                    missing_timeout.append(f"{_rel(path)}:{node.lineno} subprocess.{fn}")
            # Any reference, not just a call: ``popen=subprocess.Popen`` defaults
            # are how the supervisors take theirs.
            if (_subprocess_attr(node, imported) == "Popen"
                    and _rel(path) not in POPEN_ALLOWED):
                stray_popen.append(f"{_rel(path)}:{node.lineno}")
    return missing_timeout, stray_popen


def test_the_scan_sees_the_archive_tool_runner():
    """Guard against a scan that silently finds nothing."""
    paths = {_rel(p) for p in _scanned_files()}
    assert os.path.join("helpers", "__init__.py") in paths
    assert os.path.join("helpers", "unwrap.py") in paths
    assert "monitor.py" in paths


def test_every_blocking_subprocess_call_has_a_timeout():
    missing_timeout, _ = _violations()
    assert not missing_timeout, (
        "subprocess call without a timeout; an archive tool that hangs blocks "
        "its caller forever. Use helpers.run_archive_tool for archive tools, "
        "or pass timeout=:\n  " + "\n  ".join(missing_timeout)
    )


def test_popen_only_where_a_long_lived_process_is_intended():
    _, stray_popen = _violations()
    assert not stray_popen, (
        "subprocess.Popen has no timeout; use subprocess.run(timeout=...) or "
        "helpers.run_archive_tool:\n  " + "\n  ".join(stray_popen)
    )


def _run_archive_tool_def():
    tree = _parse(os.path.join(PROJECT_ROOT, "helpers", "__init__.py"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run_archive_tool":
            return node
    pytest.fail("helpers.run_archive_tool is gone")


def test_run_archive_tool_requires_a_timeout():
    fn = _run_archive_tool_def()
    args = fn.args
    names = [a.arg for a in args.args]
    assert "timeout" in names
    # Positional defaults align with the tail of args.args.
    defaulted = names[len(names) - len(args.defaults):] if args.defaults else []
    assert "timeout" not in defaulted, "a default timeout would let callers forget it"


def test_run_archive_tool_closes_stdin_and_detaches():
    fn = _run_archive_tool_def()
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and _subprocess_attr(n.func, {}) == "run"]
    assert len(calls) == 1
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(kw.get("timeout"), ast.Name) and kw["timeout"].id == "timeout"
    stdin = kw.get("stdin")
    assert isinstance(stdin, ast.Attribute) and stdin.attr == "DEVNULL", \
        "a password prompt must read EOF, not wait on an inherited stdin"
    session = kw.get("start_new_session")
    assert isinstance(session, ast.Constant) and session.value is True, \
        "without a controlling terminal a tool cannot prompt on /dev/tty either"


def test_archive_tools_in_helpers_go_through_the_runner():
    """No direct subprocess call to an archive tool outside the runner."""
    offenders = []
    for rel in (os.path.join("helpers", "__init__.py"),
                os.path.join("helpers", "unwrap.py")):
        tree = _parse(os.path.join(PROJECT_ROOT, rel))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or fn.name == "run_archive_tool":
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and _subprocess_attr(node.func, {}) in BLOCKING_CALLS | {"Popen"}):
                    offenders.append(f"{rel}:{node.lineno} in {fn.name}")
    assert not offenders, "\n".join(offenders)
