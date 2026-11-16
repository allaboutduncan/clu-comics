"""Which preferences the generic ``/api/preferences/<key>`` route may read and write.

The route is deny-by-default both ways: ``core.settings.PREFERENCE_READ_ROLES``
lists the keys GET serves and ``PREFERENCE_WRITE_ROLES`` the keys POST stores,
each with a minimum role, and everything else is a 404. These tests keep both
lists in step with what the frontend actually fetches and saves, and keep
credentials off them.

app.py cannot be imported in tests (importing it starts the scheduler and spawns
monitor.py), so the route's wiring is asserted against the parsed AST.
"""

import ast
import json
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


# ---------------------------------------------------------------------------
# The POST side: PREFERENCE_WRITE_ROLES / preference_write_status / save_preference
# ---------------------------------------------------------------------------

class TestWriteStatus:
    @pytest.mark.parametrize("key", sorted(cs.SECRET_PREFERENCES))
    @pytest.mark.parametrize("user", [OWNER, CLERK, READER], ids=["owner", "clerk", "reader"])
    def test_secrets_are_never_writable(self, key, user):
        assert cs.preference_write_status(key, user) == 404

    @pytest.mark.parametrize("key", [
        "not_a_pref", "monitor_enabled", "bootstrap_theme", "library_health_scan_enabled",
        "comicvine_sqlite_auto_update", "smart_rename_preview_enabled", "",
    ])
    def test_unlisted_keys_are_404_even_for_owner(self, key):
        assert cs.preference_write_status(key, OWNER) == 404

    def test_owner_writes_every_listed_key(self):
        for key in cs.PREFERENCE_WRITE_ROLES:
            assert cs.preference_write_status(key, OWNER) == 200, key

    @pytest.mark.parametrize("user", [CLERK, READER], ids=["clerk", "reader"])
    def test_lower_roles_get_only_keys_at_their_level(self, user):
        from core.auth import role_at_least
        for key, minimum in cs.PREFERENCE_WRITE_ROLES.items():
            expected = 200 if role_at_least(user, minimum) else 403
            assert cs.preference_write_status(key, user) == expected, key

    def test_anonymous_is_refused(self):
        for key in cs.PREFERENCE_WRITE_ROLES:
            assert cs.preference_write_status(key, None) == 403

    def test_unknown_role_is_refused(self):
        for key in cs.PREFERENCE_WRITE_ROLES:
            assert cs.preference_write_status(key, {"role": "guest"}) == 403


class TestWriteAllowListShape:
    def test_roles_are_real_roles(self):
        from core.auth import ROLE_LEVELS
        assert set(cs.PREFERENCE_WRITE_ROLES.values()) <= set(ROLE_LEVELS)

    def test_no_secret_is_writable(self):
        assert not (set(cs.PREFERENCE_WRITE_ROLES) & cs.SECRET_PREFERENCES)

    def test_nothing_writable_looks_like_a_credential(self):
        for key in cs.PREFERENCE_WRITE_ROLES:
            assert not _SECRET_LOOKING.search(key), key

    def test_every_key_has_a_type(self):
        """A writable key is either a schema pref or has its own write spec."""
        schema_prefs = {s.pref for s in cs.SETTINGS_SCHEMA.values()}
        for key in cs.PREFERENCE_WRITE_ROLES:
            assert (key in schema_prefs) != (key in cs._WRITE_SPECS), key
        assert set(cs._WRITE_SPECS) <= set(cs.PREFERENCE_WRITE_ROLES)


class TestSavePreference:
    @pytest.fixture
    def stored(self, monkeypatch):
        calls = []

        def fake_set(key, value, category="general"):
            calls.append((key, value, category))
            return True

        monkeypatch.setattr("core.database.set_user_preference", fake_set)
        monkeypatch.setattr(cs, "invalidate", lambda key=None: None)
        return calls

    def test_unlisted_key_writes_nothing(self, stored):
        assert cs.save_preference("api_token", "x")[0] == 404
        assert cs.save_preference("monitor_enabled", True)[0] == 404
        assert stored == []

    def test_schema_key_goes_through_the_schema(self, stored):
        assert cs.save_preference("gcd_metadata_languages", "en,fr") == (200, None)
        assert stored == [("gcd_metadata_languages", "en,fr", "metadata")]

    @pytest.mark.parametrize("value", [5, True, None, ["en"], {"a": 1}])
    def test_schema_key_refuses_a_wrong_type(self, stored, value):
        status, error = cs.save_preference("gcd_metadata_languages", value)
        assert status == 400 and error
        assert stored == []

    def test_bool_key(self, stored):
        assert cs.save_preference("credit_backfill_enabled", False) == (200, None)
        assert stored == [("credit_backfill_enabled", False, "metadata")]
        assert cs.save_preference("credit_backfill_enabled", "false")[0] == 400
        assert cs.save_preference("credit_backfill_enabled", 0)[0] == 400
        assert len(stored) == 1

    def test_series_json_provider_choices(self, stored):
        assert cs.save_preference("series_json_provider", "metron")[0] == 200
        assert cs.save_preference("series_json_provider", "nonsense")[0] == 400
        assert cs.save_preference("series_json_provider", 1)[0] == 400
        assert stored == [("series_json_provider", "metron", "metadata")]

    def test_inducks_countries(self, stored):
        assert cs.save_preference("inducks_countries", "us,it")[0] == 200
        assert cs.save_preference("inducks_countries", ["us"])[0] == 400
        assert stored == [("inducks_countries", "us,it", "metadata")]

    def test_source_priority_keeps_the_json_string_format(self, stored):
        value = json.dumps(["usenet", "getcomics"])
        assert cs.save_preference("download_source_priority", value)[0] == 200
        assert cs.save_preference("download_source_priority", "[]")[0] == 200
        for bad in (["getcomics"], "getcomics", "{}", json.dumps([1, 2])):
            assert cs.save_preference("download_source_priority", bad)[0] == 400, bad
        assert stored == [
            ("download_source_priority", value, "downloads"),
            ("download_source_priority", "[]", "downloads"),
        ]

    def test_category_is_the_servers_not_the_clients(self, stored):
        # save_preference takes no category: the stored one is fixed per key.
        cs.save_preference("inducks_countries", "us")
        assert stored[-1][2] == "metadata"

    def test_store_failure_is_500(self, monkeypatch):
        monkeypatch.setattr("core.database.set_user_preference",
                            lambda *a, **k: False)
        monkeypatch.setattr(cs, "invalidate", lambda key=None: None)
        assert cs.save_preference("inducks_countries", "us")[0] == 500


# A reference to /api/preferences/<key> that is not a bare GET fetch is a write:
# a fetch with an options object, or the config page's postSettings helper.
_BARE_GET_AT = re.compile(r"""['"`]\s*\)""")
_POST_HINT = re.compile(r"""method\s*:\s*['"]POST['"]""")


def _write_callers():
    """{key: [(file, offset)]} for every frontend POST to /api/preferences/<key>."""
    found = {}
    for path in _frontend_files():
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for m in re.finditer(r"""/api/preferences/([A-Za-z0-9_]+)""", text):
            if _BARE_GET_AT.match(text, m.end()):
                continue  # fetch('/api/preferences/x') -- a read
            found.setdefault(m.group(1), []).append((path, m.start(), text))
    return found


class TestFrontendWriteCallers:
    def test_every_non_get_reference_is_a_post(self):
        callers = _write_callers()
        assert callers, "expected at least one frontend preference write"
        for key, sites in callers.items():
            for path, pos, text in sites:
                before = text[max(0, pos - 40):pos]
                after = text[pos:pos + 300]
                assert "postSettings(" in before or _POST_HINT.search(after), (path, key)

    def test_every_frontend_post_is_allow_listed(self):
        missing = set(_write_callers()) - set(cs.PREFERENCE_WRITE_ROLES)
        assert not missing, f"frontend writes preferences not on the allow-list: {missing}"

    def test_allow_list_has_no_stale_entries(self):
        assert set(cs.PREFERENCE_WRITE_ROLES) <= set(_write_callers())

    def test_known_post_callers_are_found(self):
        callers = _write_callers()
        assert "download_source_priority" in callers
        assert "series_json_provider" in callers  # via postSettings
        assert "credit_backfill_enabled" in callers


class TestAppWriteRouteWiring:
    @pytest.fixture(scope="class")
    def handler(self):
        with open(os.path.join(PROJECT_ROOT, "app.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        return _func(tree, "api_save_preference")

    def test_checks_the_allow_list_before_writing(self, handler):
        checks = _call_lines(handler, "preference_write_status")
        writes = _call_lines(handler, "save_preference")
        assert checks and writes
        assert max(checks) < min(writes)

    def test_does_not_write_around_the_validator(self, handler):
        for callee in ("set_user_preference", "set_setting"):
            assert not _call_lines(handler, callee), callee

    def test_checks_against_the_request_user(self, handler):
        assert _call_lines(handler, "current_user")

    def test_client_category_is_ignored(self, handler):
        """The stored category comes from the schema / write spec, not the body."""
        for node in ast.walk(handler):
            if isinstance(node, ast.Constant) and node.value == "category":
                pytest.fail("handler reads a client-supplied category")
