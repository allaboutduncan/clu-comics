"""
PR2: RBAC enforcement + admin user management.

Covers the centralized route policy (required_role_for_request), the per-role
403 matrix through the real before_app_request gate, the /api/admin/users CRUD
surface, and first-run owner setup.
"""
import pytest

from core.auth import required_role_for_request
from core.database import (
    count_users,
    create_user,
    get_owner_user,
    get_user_by_username,
    seed_owner_if_needed,
    verify_password,
)


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


# ---------------------------------------------------------------------------
# Policy classification (unit-level, via a request context)
# ---------------------------------------------------------------------------
class TestRolePolicy:
    @pytest.mark.parametrize("method,path,expected", [
        ("GET", "/", "reader"),
        ("GET", "/collection", "reader"),
        ("GET", "/api/libraries", "reader"),          # browsing libraries
        ("POST", "/api/favorites/toggle", "reader"),  # personal data write
        ("POST", "/api/mark-comic-read", "reader"),    # reader records own reads
        ("POST", "/api/browse-thumbnails", "reader"),  # read-only batch (POST body)
        ("POST", "/api/browse-metadata", "reader"),    # read-only batch (POST body)
        ("GET", "/api/continue-reading", "reader"),
        # Bookmarking a reading list is personal data. These live under
        # /api/favorites precisely so _READER_WRITE_PREFIXES classifies them as
        # reader-writable; moving them under /api/reading-lists/* would fall
        # through to the "mutation → clerk" default and 403 every Reader.
        ("GET", "/api/favorites/to-read/reading-lists", "reader"),
        ("POST", "/api/favorites/to-read/reading-lists", "reader"),
        ("DELETE", "/api/favorites/to-read/reading-lists", "reader"),
        ("POST", "/rename", "clerk"),                 # default: mutation → clerk
        ("DELETE", "/api/delete-file", "clerk"),
        ("GET", "/api/getcomics/search", "clerk"),    # clerk-only read area
        # Backs the clerk-only /releases page; must not be reader-visible.
        ("POST", "/api/releases/publishers", "clerk"),
        ("POST", "/api/bulk-metadata/start", "clerk"),
        # Clerk-only browser pages (hidden from Readers in the nav + blocked by
        # direct URL). Enumerated so a Reader can't reach them via GET.
        ("GET", "/files", "clerk"),
        ("GET", "/pull-list", "clerk"),
        ("GET", "/releases", "clerk"),
        ("GET", "/wanted", "clerk"),
        ("GET", "/status", "clerk"),
        ("GET", "/weekly-packs", "clerk"),
        ("GET", "/series-search", "clerk"),
        ("GET", "/publishers", "clerk"),
        ("GET", "/metadata/history", "clerk"),
        ("GET", "/source-wall", "clerk"),
        # Logs viewer + raw log data are clerk-only (can expose system info).
        ("GET", "/logs", "clerk"),
        ("GET", "/logs/tail", "clerk"),
        ("GET", "/app-logs", "clerk"),
        ("GET", "/mon-logs", "clerk"),
        # Self-service API tokens: a user manages their OWN tokens at any role.
        ("GET", "/account", "reader"),
        ("GET", "/api/account/tokens", "reader"),
        ("POST", "/api/account/tokens", "reader"),
        ("DELETE", "/api/account/tokens/5", "reader"),
        # Per-user personalization. These live under /api/account/ precisely so
        # _READER_WRITE_PREFIXES classifies them as reader-writable; a rename to
        # e.g. /api/settings/* would silently 403 every Reader.
        ("POST", "/api/account/appearance", "reader"),
        ("POST", "/api/account/dashboard", "reader"),
        ("GET", "/config", "owner"),
        ("POST", "/api/config/file-processing", "owner"),
        ("GET", "/api/database/stats", "owner"),
        ("GET", "/api/admin/users", "owner"),
        ("POST", "/api/libraries", "owner"),          # library CRUD → owner
        ("DELETE", "/api/publishers/10", "owner"),
        ("GET", "/users", "owner"),
    ])
    def test_required_role(self, app, method, path, expected):
        with app.test_request_context(path, method=method):
            assert required_role_for_request() == expected


# ---------------------------------------------------------------------------
# End-to-end 403 matrix through the auth gate
# ---------------------------------------------------------------------------
class TestRbacMatrix:
    @pytest.fixture(autouse=True)
    def _users(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("clerk", password="clerkpass", role="clerk")
        create_user("reader", password="readerpass", role="reader")
        yield

    # Reader
    def test_reader_can_browse(self, client):
        _login(client, "reader", "readerpass")
        assert client.get("/").status_code == 200

    def test_reader_denied_owner_page(self, client):
        _login(client, "reader", "readerpass")
        # HTML owner page → redirect home with a flash (not a 403 body).
        assert client.get("/config").status_code == 302

    def test_reader_denied_admin_api(self, client):
        _login(client, "reader", "readerpass")
        assert client.get("/api/admin/users").status_code == 403

    def test_reader_denied_clerk_mutation(self, client):
        _login(client, "reader", "readerpass")
        assert client.delete("/api/delete-file").status_code == 403

    def test_reader_can_batch_fetch_folder_thumbnails(self, client):
        # Regression for #446: folder thumbnails load lazily via this read-only
        # POST endpoint. RBAC must not treat it as a clerk mutation, or the
        # thumbnails silently fail to render for non-admin users.
        _login(client, "reader", "readerpass")
        resp = client.post("/api/browse-thumbnails", json={"paths": ["/data"]})
        assert resp.status_code != 403

    def test_reader_can_batch_fetch_folder_metadata(self, client):
        _login(client, "reader", "readerpass")
        resp = client.post("/api/browse-metadata", json={"paths": ["/data"]})
        assert resp.status_code != 403

    def test_reader_can_mark_comic_read(self, client):
        # Regression for #448: a Reader must be able to record their own reads,
        # or "On the Stack" (next-unread-per-series) can never populate for them.
        _login(client, "reader", "readerpass")
        resp = client.post("/api/mark-comic-read", json={"path": "/data/x.cbz"})
        assert resp.status_code != 403

    @pytest.mark.parametrize("path", [
        "/files", "/pull-list", "/releases", "/wanted", "/status",
        "/weekly-packs", "/series-search", "/publishers", "/metadata/history",
        "/source-wall", "/logs", "/logs/tail", "/app-logs", "/mon-logs",
    ])
    def test_reader_denied_clerk_pages(self, client, path):
        # Clerk-only pages: a Reader hitting them by URL is redirected home
        # (HTML deny → 302), never allowed to render the page. Logs are here
        # because raw app/monitor logs can expose system info.
        _login(client, "reader", "readerpass")
        assert client.get(path).status_code == 302

    def test_clerk_allowed_logs(self, client):
        _login(client, "clerk", "clerkpass")
        # Clerk reaches the logs page and the raw log-tail data (never denied).
        assert client.get("/logs").status_code == 200
        assert client.get("/logs/tail").status_code != 302

    # Clerk
    def test_clerk_allowed_clerk_page(self, client):
        # A Clerk reaches the handler (here a simple stub → 200), never 403/redirect.
        _login(client, "clerk", "clerkpass")
        assert client.get("/status").status_code == 200

    def test_clerk_can_browse(self, client):
        _login(client, "clerk", "clerkpass")
        assert client.get("/").status_code == 200

    def test_clerk_allowed_clerk_mutation(self, client):
        _login(client, "clerk", "clerkpass")
        # Allowed by RBAC → reaches the handler (which may 400/500), never 403.
        assert client.delete("/api/delete-file").status_code != 403

    def test_clerk_denied_owner_api(self, client):
        _login(client, "clerk", "clerkpass")
        assert client.get("/api/admin/users").status_code == 403

    def test_clerk_denied_config(self, client):
        _login(client, "clerk", "clerkpass")
        assert client.get("/config").status_code == 302

    # Owner
    def test_owner_allowed_admin_api(self, client):
        _login(client, "owner", "ownerpass")
        assert client.get("/api/admin/users").status_code == 200

    def test_owner_allowed_config(self, client):
        _login(client, "owner", "ownerpass")
        assert client.get("/config").status_code == 200

    def test_unauthenticated_redirected(self, client):
        r = client.get("/api/admin/users")
        # JSON namespace → 401, not an HTML redirect.
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Admin user-management API
# ---------------------------------------------------------------------------
class TestAdminUserApi:
    @pytest.fixture(autouse=True)
    def _owner_and_login(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("second", password="secondpass", role="reader")
        yield

    def test_list_users(self, client):
        _login(client, "owner", "ownerpass")
        data = client.get("/api/admin/users").get_json()
        assert data["success"] is True
        assert {u["username"] for u in data["users"]} == {"owner", "second"}
        assert "libraries" in data

    def test_create_user_with_library_grant(self, client, db_connection):
        from tests.factories.db_factories import create_library
        lib = create_library(name="A", path="/data/a")
        _login(client, "owner", "ownerpass")
        resp = client.post("/api/admin/users", json={
            "username": "newclerk", "password": "pw", "role": "clerk",
            "library_ids": [lib],
        })
        assert resp.status_code == 201
        user = resp.get_json()["user"]
        assert user["role"] == "clerk"
        assert user["library_ids"] == [lib]

    def test_create_and_update_folder_grants(self, client, db_connection):
        import os
        from tests.factories.db_factories import create_library
        lib = create_library(name="A", path="/data/a")
        _login(client, "owner", "ownerpass")
        resp = client.post("/api/admin/users", json={
            "username": "folderuser", "password": "pw", "role": "reader",
            "library_ids": [lib], "folder_paths": ["/data/a/X"],
        })
        assert resp.status_code == 201
        assert resp.get_json()["user"]["folder_paths"] == [os.path.normpath("/data/a/X")]

        uid = get_user_by_username("folderuser")["id"]
        resp = client.put(f"/api/admin/users/{uid}",
                          json={"folder_paths": ["/data/a/Y"]})
        assert resp.status_code == 200
        assert resp.get_json()["user"]["folder_paths"] == [os.path.normpath("/data/a/Y")]

    def test_create_duplicate_username_conflicts(self, client):
        _login(client, "owner", "ownerpass")
        resp = client.post("/api/admin/users", json={
            "username": "owner", "password": "pw", "role": "reader",
        })
        assert resp.status_code == 409

    def test_create_requires_password(self, client):
        _login(client, "owner", "ownerpass")
        resp = client.post("/api/admin/users", json={"username": "x", "role": "reader"})
        assert resp.status_code == 400

    def test_update_role_and_password(self, client):
        _login(client, "owner", "ownerpass")
        uid = get_user_by_username("second")["id"]
        resp = client.put(f"/api/admin/users/{uid}", json={
            "role": "clerk", "password": "changed",
        })
        assert resp.status_code == 200
        assert resp.get_json()["user"]["role"] == "clerk"
        assert verify_password("second", "changed")

    def test_update_username(self, client):
        _login(client, "owner", "ownerpass")
        uid = get_user_by_username("second")["id"]
        resp = client.put(f"/api/admin/users/{uid}", json={"username": "renamed"})
        assert resp.status_code == 200
        assert resp.get_json()["user"]["username"] == "renamed"
        assert get_user_by_username("renamed")["id"] == uid
        assert get_user_by_username("second") is None

    def test_update_username_conflict(self, client):
        _login(client, "owner", "ownerpass")
        uid = get_user_by_username("second")["id"]
        # "owner" already exists → renaming "second" to it must 409 and not change.
        resp = client.put(f"/api/admin/users/{uid}", json={"username": "owner"})
        assert resp.status_code == 409
        assert get_user_by_username("second")["id"] == uid

    def test_delete_user(self, client):
        _login(client, "owner", "ownerpass")
        uid = get_user_by_username("second")["id"]
        assert client.delete(f"/api/admin/users/{uid}").status_code == 200
        assert get_user_by_username("second") is None

    def test_cannot_delete_last_owner(self, client):
        _login(client, "owner", "ownerpass")
        uid = get_user_by_username("owner")["id"]
        resp = client.delete(f"/api/admin/users/{uid}")
        assert resp.status_code == 400
        assert get_user_by_username("owner") is not None

    def test_cannot_demote_last_owner(self, client):
        _login(client, "owner", "ownerpass")
        uid = get_user_by_username("owner")["id"]
        resp = client.put(f"/api/admin/users/{uid}", json={"role": "reader"})
        assert resp.status_code == 400
        assert get_user_by_username("owner")["role"] == "owner"

    def test_reader_forbidden_from_admin(self, client):
        _login(client, "second", "secondpass")  # reader
        assert client.get("/api/admin/users").status_code == 403
        assert client.post("/api/admin/users", json={
            "username": "z", "password": "pw",
        }).status_code == 403


# ---------------------------------------------------------------------------
# Reading data resolves to the logged-in user (request-context scoping)
# ---------------------------------------------------------------------------
class TestReadingDataScopedByRequest:
    @pytest.fixture(autouse=True)
    def _users(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("alice", password="alicepass", role="reader")
        create_user("bob", password="bobpass", role="reader")
        yield

    def test_mark_read_scopes_to_logged_in_user(self, app):
        from core.database import is_issue_read

        alice = app.test_client()
        _login(alice, "alice", "alicepass")
        bob = app.test_client()
        _login(bob, "bob", "bobpass")

        assert alice.post("/api/favorites/issues",
                          json={"path": "/data/alice.cbz"}).status_code == 200
        assert bob.post("/api/favorites/issues",
                        json={"path": "/data/bob.cbz"}).status_code == 200

        aid = get_user_by_username("alice")["id"]
        bid = get_user_by_username("bob")["id"]
        # Each user's read history contains only their own issue.
        assert is_issue_read("/data/alice.cbz", user_id=aid) is True
        assert is_issue_read("/data/alice.cbz", user_id=bid) is False
        assert is_issue_read("/data/bob.cbz", user_id=bid) is True
        assert is_issue_read("/data/bob.cbz", user_id=aid) is False


# ---------------------------------------------------------------------------
# Self-service API tokens: any logged-in user manages their OWN tokens
# ---------------------------------------------------------------------------
class TestSelfServiceApiTokens:
    @pytest.fixture(autouse=True)
    def _users(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("reader", password="readerpass", role="reader")
        yield

    def test_reader_can_open_account_page(self, client):
        # The self-service page renders for a reader (also exercises the nav's
        # url_for('auth.account') link resolving).
        _login(client, "reader", "readerpass")
        resp = client.get("/account")
        assert resp.status_code == 200
        assert "API Tokens" in resp.get_data(as_text=True)

    def test_reader_full_token_lifecycle(self, client):
        _login(client, "reader", "readerpass")

        # Create — reader may mint their own token (personal-data write).
        resp = client.post("/api/account/tokens", json={"name": "Phone"})
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["success"] is True
        assert body["token"]  # plaintext returned exactly once

        # List — shows only the reader's own token, metadata only (no plaintext).
        resp = client.get("/api/account/tokens")
        assert resp.status_code == 200
        tokens = resp.get_json()["tokens"]
        assert [t["name"] for t in tokens] == ["Phone"]
        assert "token" not in tokens[0] and "token_hash" not in tokens[0]

        # Revoke — the reader can delete their own token.
        tid = tokens[0]["id"]
        assert client.delete(f"/api/account/tokens/{tid}").status_code == 200
        assert client.get("/api/account/tokens").get_json()["tokens"] == []

    def test_tokens_are_scoped_per_user(self, client):
        # A token minted for the owner must not be listable or revocable by a
        # reader through the self-service surface.
        from core.database import create_api_token, get_user_by_username
        owner_id = get_user_by_username("owner")["id"]
        create_api_token(owner_id, name="owner-token")

        _login(client, "reader", "readerpass")
        assert client.get("/api/account/tokens").get_json()["tokens"] == []

        # Discover the owner's token id (as owner) then try to revoke it as reader.
        owner_client = client.application.test_client()
        _login(owner_client, "owner", "ownerpass")
        owner_tokens = owner_client.get(
            f"/api/admin/users/{owner_id}/tokens").get_json()["tokens"]
        owner_tid = owner_tokens[0]["id"]

        resp = client.delete(f"/api/account/tokens/{owner_tid}")
        assert resp.status_code == 404  # scoped delete: not this reader's token
        # Owner's token survives.
        assert len(owner_client.get(
            f"/api/admin/users/{owner_id}/tokens").get_json()["tokens"]) == 1


# ---------------------------------------------------------------------------
# First-run owner setup (implicit-owner mode)
# ---------------------------------------------------------------------------
class TestSetupOwner:
    def test_setup_placeholder_owner(self, client, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        seed_owner_if_needed()  # creates a needs_setup placeholder owner
        assert get_owner_user()["needs_setup"] == 1

        # Reachable without login in implicit-owner mode.
        resp = client.post("/api/admin/setup-owner", json={
            "username": "boss", "password": "hunter2",
        })
        assert resp.status_code == 200
        owner = get_owner_user()
        assert owner["username"] == "boss"
        assert owner["needs_setup"] == 0
        assert verify_password("boss", "hunter2")

    def test_setup_rejected_once_configured(self, client, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="pw", role="owner")  # already configured
        resp = client.post("/api/admin/setup-owner", json={
            "username": "x", "password": "y",
        })
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Leaving implicit-owner mode: the first *additional* account turns login on
# ---------------------------------------------------------------------------
class TestFirstAdditionalUser:
    """Adding the second account flips is_login_required() from False to True.

    Before the flip nobody has ever logged in, so the owner holds no session.
    These cover the two ways that used to go wrong: the owner being logged out
    the instant the account was created, and a placeholder owner turning login
    on for an install where no password exists to log in with.
    """

    @pytest.fixture(autouse=True)
    def _no_env_gate(self, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)

    def _setup_owner(self, client):
        seed_owner_if_needed()
        resp = client.post("/api/admin/setup-owner", json={
            "username": "boss", "password": "hunter2",
        })
        assert resp.status_code == 200

    def test_owner_keeps_access_after_creating_the_first_user(
            self, client, db_connection):
        self._setup_owner(client)

        resp = client.post("/api/admin/users", json={
            "username": "kid", "password": "pw", "role": "reader",
        })
        assert resp.status_code == 201

        # Login is now required; the owner's browser must still be the owner.
        resp = client.get("/api/admin/users")
        assert resp.status_code == 200
        assert {u["username"] for u in resp.get_json()["users"]} == {"boss", "kid"}

    def test_owner_keeps_access_to_browser_pages_too(self, client, db_connection):
        self._setup_owner(client)
        client.post("/api/admin/users", json={
            "username": "kid", "password": "pw", "role": "reader",
        })
        # Owner-only browser page: a redirect here is the logout users reported.
        assert client.get("/config").status_code == 200

    def test_creating_a_user_does_not_authenticate_a_bystander(
            self, client, db_connection, app):
        """Only the browser that made the call is signed in, not every client."""
        self._setup_owner(client)
        client.post("/api/admin/users", json={
            "username": "kid", "password": "pw", "role": "reader",
        })
        other = app.test_client()
        assert other.get("/api/admin/users").status_code == 401

    def test_placeholder_owner_cannot_turn_login_on(self, client, db_connection):
        """An owner with no password must not be able to lock the install."""
        seed_owner_if_needed()  # placeholder: needs_setup, no password
        assert get_owner_user()["needs_setup"] == 1

        resp = client.post("/api/admin/users", json={
            "username": "kid", "password": "pw", "role": "reader",
        })
        assert resp.status_code == 409
        assert count_users() == 1  # nothing created

        # The install is still reachable, and first-run setup still works.
        assert client.get("/api/admin/users").status_code == 200
        assert client.post("/api/admin/setup-owner", json={
            "username": "boss", "password": "hunter2",
        }).status_code == 200
        assert client.post("/api/admin/users", json={
            "username": "kid", "password": "pw", "role": "reader",
        }).status_code == 201

    def test_owner_can_still_log_in_normally_afterwards(self, client, db_connection):
        self._setup_owner(client)
        client.post("/api/admin/users", json={
            "username": "kid", "password": "pw", "role": "reader",
        })
        client.get("/logout")
        assert client.get("/api/admin/users").status_code == 401
        _login(client, "boss", "hunter2")
        assert client.get("/api/admin/users").status_code == 200
