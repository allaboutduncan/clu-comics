"""Which preferences the generic ``GET /api/preferences/<key>`` route may return.

The route is deny-by-default: ``core.settings.PREFERENCE_READ_ROLES`` lists the
keys it serves and the minimum role for each, and everything else is a 404.
These tests keep that list in step with what the frontend actually fetches, and
keep credentials off it.

app.py cannot be imported in tests (importing it starts the scheduler and spawns
monitor.py), so the route's wiring is asserted against the parsed AST.
"""

import ast
import os
import re

import pytest

import core.settings as cs

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OWNER = {"id": 1, "role": "owner"}
CLERK = {"id": 2, "role": "clerk"}
READER = {"id": 3, "role": "reader"}

_SECRET_LOOKING = re.compile(r"(secret|token|password|passwd|api_key|apikey|header|cookie|credential)")


class TestReadStatus:
    @pytest.mark.parametrize("key", sorted(cs.SECRET_PREFERENCES))
    @pytest.mark.parametrize("user", [OWNER, CLERK, READER], ids=["owner", "clerk", "reader"])
    def test_secrets_are_never_served(self, key, user):
        assert cs.preference_read_status(key, user) == 404

    @pytest.mark.parametrize("key", ["not_a_pref", "monitor_enabled", "bootstrap_theme", ""])
    def test_unlisted_keys_are_404_even_for_owner(self, key):
        assert cs.preference_read_status(key, OWNER) == 404

    def test_owner_reads_every_listed_key(self):
        for key in cs.PREFERENCE_READ_ROLES:
            assert cs.preference_read_status(key, OWNER) == 200

    def test_reader_gets_reader_keys_only(self):
        for key, minimum in cs.PREFERENCE_READ_ROLES.items():
            expected = 200 if minimum == "reader" else 403
            assert cs.preference_read_status(key, READER) == expected, key

    def test_owner_keys_are_refused_to_clerk(self):
        for key, minimum in cs.PREFERENCE_READ_ROLES.items():
            if minimum == "owner":
                assert cs.preference_read_status(key, CLERK) == 403, key

    def test_anonymous_is_refused(self):
        for key in cs.PREFERENCE_READ_ROLES:
            assert cs.preference_read_status(key, None) == 403

    def test_unknown_role_is_refused(self):
        assert cs.preference_read_status(
            "smart_rename_preview_enabled", {"role": "guest"}) == 403


class TestAllowListShape:
    def test_roles_are_real_roles(self):
        from core.auth import ROLE_LEVELS
        assert set(cs.PREFERENCE_READ_ROLES.values()) <= set(ROLE_LEVELS)

    def test_no_secret_is_readable(self):
        assert not (set(cs.PREFERENCE_READ_ROLES) & cs.SECRET_PREFERENCES)

    def test_nothing_readable_looks_like_a_credential(self):
        for key in cs.PREFERENCE_READ_ROLES:
            assert not _SECRET_LOOKING.search(key), key

    def test_schema_credentials_are_listed_as_secret(self):
        """A credential added to SETTINGS_SCHEMA must be named a secret too."""
        for spec in cs.SETTINGS_SCHEMA.values():
            if _SECRET_LOOKING.search(spec.pref):
                assert spec.pref in cs.SECRET_PREFERENCES, spec.pref

    def test_known_credentials_are_secret(self):
        assert {"secret_key", "api_token", "rec_api_key",
                "pixeldrain_api_key", "custom_headers"} <= cs.SECRET_PREFERENCES


# fetch('/api/preferences/<key>') with no options object is a GET.
_GET_FETCH = re.compile(r"""fetch\(\s*['"`]/api/preferences/([A-Za-z0-9_]+)['"`]\s*\)""")
_ANY_REF = re.compile(r"""/api/preferences/([^'"`\s)]*)""")


def _frontend_files():
    for sub in ("static/js", "templates"):
        base = os.path.join(PROJECT_ROOT, sub)
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                if name.endswith((".js", ".html")):
                    yield os.path.join(dirpath, name)


class TestFrontendCallers:
    def _read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_every_frontend_get_is_allow_listed(self):
        found = set()
        for path in _frontend_files():
            found.update(_GET_FETCH.findall(self._read(path)))
        assert found, "expected at least one frontend preference read"
        missing = found - set(cs.PREFERENCE_READ_ROLES)
        assert not missing, f"frontend reads preferences not on the allow-list: {missing}"

    def test_no_dynamic_preference_keys(self):
        """A key built at runtime cannot be checked against the allow-list."""
        for path in _frontend_files():
            for ref in _ANY_REF.findall(self._read(path)):
                assert re.fullmatch(r"[A-Za-z0-9_]+", ref), (path, ref)

    def test_allow_list_has_no_stale_entries(self):
        found = set()
        for path in _frontend_files():
            found.update(_GET_FETCH.findall(self._read(path)))
        assert set(cs.PREFERENCE_READ_ROLES) <= found


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name} not found")


def _call_lines(node, callee):
    lines = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if name == callee:
                lines.append(sub.lineno)
    return lines


class TestAppRouteWiring:
    @pytest.fixture(scope="class")
    def handler(self):
        with open(os.path.join(PROJECT_ROOT, "app.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        return _func(tree, "api_get_preference")

    def test_checks_the_allow_list_before_reading(self, handler):
        checks = _call_lines(handler, "preference_read_status")
        reads = _call_lines(handler, "get_user_preference")
        assert checks and reads
        assert max(checks) < min(reads)

    def test_checks_against_the_request_user(self, handler):
        assert _call_lines(handler, "current_user")
