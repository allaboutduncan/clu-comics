"""Path prefixes that GRANT match on a ``/`` segment boundary.

``routes/auth.py``'s login exemptions and ``core/auth.py``'s Reader tables lower
the bar for every path they match, so a bare ``startswith`` would also exempt
or down-grade a future route whose name merely begins the same way
(``/login-foo``, ``/opdsanything``, ``/api/favorites-admin``). Every route that
exists today matches exactly as before; the Owner and Clerk tables, which raise
the bar, keep plain prefix matching.
"""
import pytest
from flask import Flask

from core import auth as core_auth
from core.auth import path_matches_prefix, required_role_for_request
from core.database import create_user


class TestPathMatchesPrefix:

    @pytest.mark.parametrize("path,expected", [
        ("/login", True),
        ("/login/", True),
        ("/login/sub", True),
        ("/login-foo", False),
        ("/loginx", False),
        ("/opds", True),
        ("/opds/browse", True),
        ("/opdsanything", False),
        ("/static/js/x.js", True),
        ("/staticx", False),
        ("/api/v1/auth/ping", True),
        ("/api/v1x", False),
        ("/api/insights", True),
        ("/api/insights-admin", False),
    ])
    def test_login_exemptions(self, path, expected):
        from routes.auth import _EXEMPT_PREFIXES
        assert path_matches_prefix(path, _EXEMPT_PREFIXES) is expected

    def test_trailing_slash_prefix_matches_below_it(self):
        assert path_matches_prefix("/api/read/x/page/1", ("/api/read/",))
        assert not path_matches_prefix("/api/reader", ("/api/read/",))


def _role(method, path):
    app = Flask(__name__)
    with app.test_request_context(path, method=method):
        return required_role_for_request()


class TestReaderTablesAreSegmentBounded:

    @pytest.mark.parametrize("path", [
        "/api/favorites", "/api/favorites/publishers", "/api/mark-comic-read",
        "/api/reading-position", "/api/to-read", "/api/continue-reading",
        "/api/on-the-stack", "/api/account/tokens", "/api/browse-thumbnails",
        "/api/browse-metadata",
    ])
    def test_existing_reader_posts_stay_reader(self, path):
        assert _role("POST", path) == "reader"

    @pytest.mark.parametrize("path", [
        "/api/favorites-admin", "/api/mark-comic-read-all",
        "/api/reading-positions-purge", "/api/to-read-import",
        "/api/browse-thumbnails-rebuild",
    ])
    def test_lookalike_post_is_not_down_graded_to_reader(self, path):
        assert _role("POST", path) == "clerk"

    def test_owner_and_clerk_tables_keep_prefix_matching(self):
        # These raise the bar, so matching past a boundary only over-restricts.
        assert _role("POST", "/api/save-cvinfo") == "owner"
        assert _role("GET", "/download_status_all") == "clerk"
        assert _role("GET", "/setup-owner") == "owner"

    def test_every_reader_prefix_still_matches_itself(self):
        for prefix in core_auth._READER_WRITE_PREFIXES + core_auth._READER_READ_POST_PREFIXES:
            assert path_matches_prefix(prefix, (prefix,))


@pytest.fixture
def login_required(db_connection, monkeypatch):
    """Two accounts, so the login gate is on; nobody is logged in."""
    monkeypatch.delenv("CLU_USERNAME", raising=False)
    monkeypatch.delenv("CLU_PASSWORD", raising=False)
    for name, role in (("pb_owner", "owner"), ("pb_reader", "reader")):
        create_user(name, password="fake-" + name, role=role)


class TestLoginGateOnLookalikes:

    def test_exempt_route_still_exempt(self, client, login_required):
        assert client.get("/login").status_code == 200

    def test_lookalike_of_login_is_gated(self, client, login_required):
        resp = client.get("/login-foo")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_lookalike_of_opds_is_gated(self, client, login_required):
        # Not exempt, so the anonymous request is refused before routing
        # (a JSON 401, as for /opds itself without credentials) rather than
        # reaching a 404.
        assert client.get("/opdsanything").status_code == 401

    def test_lookalike_of_api_v1_is_gated(self, client, login_required):
        assert client.get("/api/v1x").status_code == 401
