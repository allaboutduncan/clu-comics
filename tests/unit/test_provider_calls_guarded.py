"""Every provider call goes through that provider's guarded entry point.

A raw ``api.series(...)`` skips the auth lockout and the quota latch, so a
loop holding a client keeps calling with credentials the provider already
rejected -- which is how a user's IP was banned by Metron's fail2ban. Eight
Metron calls in ``routes/series.py`` and ``models/series_json.py`` did exactly
that (#703).

Asserted over the AST, because app.py cannot be imported in tests.

**Adding a provider (#704):** add a row to ``PROVIDER_RULES``. ``owner`` is
the one module allowed to build the provider's client, ``client_ctor`` the
names that build it, and every call to one of ``methods`` on a receiver in
``receivers`` must sit inside a ``lambda`` -- or a nested ``def`` passed by
name -- given to one of ``guards``. A plain-``requests`` provider gets the
same shape once its ``HttpClient`` exists.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKIP_DIRS = {"tests", ".git", ".claude", "node_modules", ".venv", "venv", "__pycache__"}

# mokkari 5.0 Session's read methods. The write methods (``*_post``,
# ``*_patch``, ``collection_add``...) are not called by CLU.
_MOKKARI_READS = frozenset({
    "arc", "arc_issues_list", "arcs_list", "character", "character_issues_list",
    "characters_list", "collection", "collection_missing_issues",
    "collection_missing_series", "collection_scrobble", "collection_stats",
    "collections_list", "creator", "creators_list", "imprint", "imprints_list",
    "issue", "issues_list", "publisher", "publishers_list", "pull_list",
    "pull_list_issues", "pull_list_series", "reading_list", "reading_list_items",
    "reading_lists_list", "role_list", "series", "series_issues_list",
    "series_list", "series_type_list", "team", "team_issues_list", "teams_list",
    "universe", "universes_list", "wish_list", "wish_list_items",
})

PROVIDER_RULES = {
    "metron": {
        "owner": "models/metron.py",
        "client_ctor": {"MokkariSession"},
        "client_module": "mokkari.session",
        "receivers": {"api"},
        "methods": _MOKKARI_READS,
        "guards": {"_api_call"},
    },
}


def _repo_modules():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                path = os.path.join(dirpath, name)
                yield os.path.relpath(path, ROOT).replace(os.sep, "/"), path


def _parse(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


def _called_name(call):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _parents(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_scope(node, parents):
    cur = parents.get(node)
    while cur is not None and not isinstance(
        cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)
    ):
        cur = parents.get(cur)
    return cur


def _is_guarded(call, parents, guards):
    """True when ``call`` runs inside a callable handed to a guard."""
    cur = call
    while cur in parents:
        parent = parents[cur]
        if isinstance(parent, ast.Lambda):
            holder = parents.get(parent)
            if isinstance(holder, ast.Call) and _called_name(holder) in guards:
                return True
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = _enclosing_scope(parent, parents)
            if scope is not None:
                for node in ast.walk(scope):
                    if (
                        isinstance(node, ast.Call)
                        and _called_name(node) in guards
                        and any(
                            isinstance(arg, ast.Name) and arg.id == parent.name
                            for arg in node.args
                        )
                    ):
                        return True
        cur = parent
    return False


def unguarded_calls(tree, rule):
    """``(lineno, "recv.method")`` for each provider call outside a guard."""
    parents = _parents(tree)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        recv = node.func.value
        if not (isinstance(recv, ast.Name) and recv.id in rule["receivers"]):
            continue
        if node.func.attr not in rule["methods"]:
            continue
        if not _is_guarded(node, parents, rule["guards"]):
            found.append((node.lineno, f"{recv.id}.{node.func.attr}"))
    return found


def client_constructions(tree, rule):
    """Lines that build the provider's client or import its module."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _called_name(node) in rule["client_ctor"]:
            found.append(node.lineno)
        if isinstance(node, ast.ImportFrom) and node.module == rule["client_module"]:
            found.append(node.lineno)
    return found


@pytest.mark.parametrize("provider", sorted(PROVIDER_RULES))
def test_every_provider_call_is_guarded(provider):
    rule = PROVIDER_RULES[provider]
    offenders = []
    for rel, path in _repo_modules():
        for lineno, what in unguarded_calls(_parse(path), rule):
            offenders.append(f"{rel}:{lineno} {what}(...)")
    assert not offenders, (
        f"{provider} calls outside {sorted(rule['guards'])} skip the auth lockout "
        f"and quota latch; wrap each in a lambda passed to the guard:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("provider", sorted(PROVIDER_RULES))
def test_only_the_owner_builds_the_client(provider):
    """One owner means one place that attaches the pacer to every client."""
    rule = PROVIDER_RULES[provider]
    offenders = [
        f"{rel}:{lineno}"
        for rel, path in _repo_modules()
        if rel != rule["owner"]
        for lineno in client_constructions(_parse(path), rule)
    ]
    assert not offenders, (
        f"only {rule['owner']} may build a {provider} client:\n  " + "\n  ".join(offenders)
    )


class TestTheCheckerItself:
    """A ratchet that cannot fail is worse than none."""

    rule = PROVIDER_RULES["metron"]

    def _check(self, source):
        return unguarded_calls(ast.parse(source), self.rule)

    def test_flags_a_raw_call(self):
        assert self._check("def f(api):\n    return api.series(1)\n") == [(2, "api.series")]

    def test_accepts_a_lambda_given_to_the_guard(self):
        assert self._check(
            "def f(api):\n    return metron._api_call(lambda: api.series(1), 'x')\n"
        ) == []

    def test_accepts_a_nested_def_given_to_the_guard_by_name(self):
        assert self._check(
            "def f(api):\n"
            "    def _call():\n"
            "        return api.series_list({})\n"
            "    return _api_call(_call, 'x')\n"
        ) == []

    def test_a_lambda_given_to_something_else_is_still_raw(self):
        assert self._check(
            "def f(api):\n    return retry(lambda: api.issue(1))\n"
        ) == [(2, "api.issue")]

    def test_flags_a_constructor_outside_the_owner(self):
        tree = ast.parse("from mokkari.session import Session\nMokkariSession('t')\n")
        assert client_constructions(tree, self.rule) == [1, 2]
