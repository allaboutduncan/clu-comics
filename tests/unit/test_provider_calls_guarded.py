"""Every provider call goes through that provider's guarded entry point.

A raw ``api.series(...)`` skips the auth lockout and the quota latch, so a
loop holding a client keeps calling with credentials the provider already
rejected -- which is how a user's IP was banned by Metron's fail2ban. Eight
Metron calls in ``routes/series.py`` and ``models/series_json.py`` did exactly
that (#703).

Asserted over the AST, because app.py cannot be imported in tests.

**Adding a provider (#704):** add a row to ``PROVIDER_RULES``. ``owner`` is
the one module allowed to build the provider's library object, and
``client_ctor`` / ``client_module`` the names that build it. Then one of:

* ``client_class`` -- a ``core.provider_client.LibraryClient`` subclass whose
  methods carry the library's names and run under the policy. Nothing
  outside the owner may reach its ``_library``, the shim names in ``retired``
  may appear nowhere, and every read the installed library has must be in
  its ``READ_METHODS``. (ComicVine; Metron after #704 PR 4b.)
* ``guards`` -- the older shape: every call to one of ``methods`` on a
  receiver in ``receivers`` must sit inside a ``lambda`` -- or a nested
  ``def`` passed by name -- given to one of ``guards``. (Metron until 4b.)

A plain-``requests`` provider instead goes in ``HTTP_PROVIDER_MODULES``: it
may not touch ``requests`` at all, only ``core.http_client.HttpClient``.
"""
import importlib
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
    "comicvine": {
        "owner": "models/comicvine.py",
        "client_ctor": {"Comicvine"},
        "client_module": "simyan.comicvine",
        "client_class": ("models.comicvine", "ComicVineClient"),
        "library_class": ("simyan.comicvine", "Comicvine"),
        "retired": {"_cv_call_with_retry", "_cv_search"},
    },
}

# Modules that may reach a LibraryClient's ``_library``: the base class and
# each client's owner.
_LIBRARY_OWNERS = {"core/provider_client.py"} | {
    rule["owner"] for rule in PROVIDER_RULES.values() if "client_class" in rule
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


_GUARD_RULES = sorted(p for p, r in PROVIDER_RULES.items() if "guards" in r)
_CLIENT_RULES = sorted(p for p, r in PROVIDER_RULES.items() if "client_class" in r)


def library_access(tree):
    """Lines that read ``._library`` on anything."""
    return [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "_library"
    ]


def name_uses(tree, names):
    """Lines that define, call, import or mention any of ``names``."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in names:
            found.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr in names:
            found.append(node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            found.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and any(a.name in names for a in node.names):
            found.append(node.lineno)
    return found


def test_only_owners_reach_the_library_object():
    """Holding the raw Simyan/mokkari object is how a call skips the policy."""
    offenders = [
        f"{rel}:{lineno}"
        for rel, path in _repo_modules()
        if rel not in _LIBRARY_OWNERS
        for lineno in library_access(_parse(path))
    ]
    assert not offenders, (
        "only a client's owner module may touch LibraryClient._library; call "
        "the client's methods instead:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("provider", _CLIENT_RULES)
def test_retired_shims_are_gone(provider):
    retired = PROVIDER_RULES[provider]["retired"]
    offenders = [
        f"{rel}:{lineno}"
        for rel, path in _repo_modules()
        for lineno in name_uses(_parse(path), retired)
    ]
    assert not offenders, (
        f"{sorted(retired)} were replaced by the {provider} client's methods:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("provider", _CLIENT_RULES)
def test_the_client_lists_every_installed_read(provider):
    """A read the library gains must not be reachable only by going around
    the client -- and there is no __getattr__ to forward it."""
    rule = PROVIDER_RULES[provider]
    module, cls = rule["client_class"]
    client_class = getattr(importlib.import_module(module), cls)
    lib_module, lib_cls = rule["library_class"]
    library = getattr(importlib.import_module(lib_module), lib_cls)
    installed = {
        name for name in dir(library)
        if not name.startswith("_") and callable(getattr(library, name))
    }
    missing = installed - client_class.READ_METHODS
    assert not missing, f"{cls}.READ_METHODS lacks {sorted(missing)}"
    for name in client_class.READ_METHODS:
        assert callable(getattr(client_class, name)), name


@pytest.mark.parametrize("provider", _GUARD_RULES)
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


# Plain-requests providers: their requests may only leave through
# ``core.http_client.HttpClient._send``, which paces each one and puts it
# behind the lockout and quota latch. Each #704 PR adds its modules here.
HTTP_PROVIDER_MODULES = frozenset({
    "models/gcd_api.py",
    "models/providers/gcd_api_provider.py",
    "models/providers/mangadex_provider.py",
    "models/providers/mangaupdates_provider.py",
    "models/providers/bedetheque_provider.py",
    "models/providers/anilist_provider.py",
})


def raw_requests_use(tree):
    """Lines that import ``requests`` (or a submodule) or build a Session."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            a.name == "requests" or a.name.startswith("requests.") for a in node.names
        ):
            found.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module and (
            node.module == "requests" or node.module.startswith("requests.")
        ):
            found.append(node.lineno)
        elif isinstance(node, ast.Call) and _called_name(node) == "Session":
            found.append(node.lineno)
    return found


@pytest.mark.parametrize("module", sorted(HTTP_PROVIDER_MODULES))
def test_http_providers_send_only_through_http_client(module):
    offenders = raw_requests_use(_parse(os.path.join(ROOT, module)))
    assert not offenders, (
        f"{module} talks to requests directly (lines {offenders}); send through "
        f"core.http_client.HttpClient so every request is paced and guarded"
    )


class TestTheHttpCheckerItself:
    def test_flags_requests_imports_and_sessions(self):
        tree = ast.parse(
            "import requests\nfrom requests.auth import HTTPBasicAuth\n"
            "s = requests.Session()\n"
        )
        assert raw_requests_use(tree) == [1, 2, 3]

    def test_accepts_the_http_client(self):
        tree = ast.parse(
            "from core.http_client import HttpClient\nHttpClient('a', 'A', 'u', 30)\n"
        )
        assert raw_requests_use(tree) == []


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

    def test_flags_library_access_and_retired_names(self):
        tree = ast.parse(
            "from models.comicvine import _cv_call_with_retry\n"
            "def f(cv):\n    return cv._library.get_volume(1)\n"
        )
        assert library_access(tree) == [3]
        assert name_uses(tree, {"_cv_call_with_retry"}) == [1]

    def test_accepts_client_methods(self):
        tree = ast.parse("def f(cv):\n    return cv.using(context='x').get_volume(1)\n")
        assert library_access(tree) == []
        assert name_uses(tree, {"_cv_call_with_retry", "_cv_search"}) == []

    def test_flags_a_constructor_outside_the_owner(self):
        tree = ast.parse("from mokkari.session import Session\nMokkariSession('t')\n")
        assert client_constructions(tree, self.rule) == [1, 2]
