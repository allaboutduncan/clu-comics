"""
PR6: per-user API tokens (/api/v1) and OPDS Basic-Auth identity.
"""
import base64

import pytest

from core.database import (
    add_to_read,
    create_api_token,
    create_user,
    get_user_by_username,
    set_user_libraries,
)
from tests.factories.db_factories import create_library


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _basic(username, password):
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


# ---------------------------------------------------------------------------
# /api/v1 per-user tokens
# ---------------------------------------------------------------------------
class TestApiV1PerUserTokens:
    @pytest.fixture(autouse=True)
    def _users(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("alice", password="pw", role="reader")
        create_user("bob", password="pw", role="reader")
        self.alice = get_user_by_username("alice")["id"]
        self.bob = get_user_by_username("bob")["id"]
        self.token_a = create_api_token(self.alice, name="phone")
        self.token_b = create_api_token(self.bob, name="tablet")
        yield

    def test_unknown_token_401(self, client):
        r = client.get("/api/v1/auth/ping", headers=_bearer("nope"))
        assert r.status_code == 401

    def test_valid_token_200(self, client):
        r = client.get("/api/v1/auth/ping", headers=_bearer(self.token_a))
        assert r.status_code == 200

    def test_progress_is_per_token_user(self, client):
        client.put("/api/v1/progress", json={"path": "/data/x.cbz", "page_number": 5,
                                             "total_pages": 20}, headers=_bearer(self.token_a))
        client.put("/api/v1/progress", json={"path": "/data/x.cbz", "page_number": 15,
                                             "total_pages": 20}, headers=_bearer(self.token_b))

        ra = client.get("/api/v1/progress?path=/data/x.cbz", headers=_bearer(self.token_a))
        rb = client.get("/api/v1/progress?path=/data/x.cbz", headers=_bearer(self.token_b))
        assert ra.get_json()["page_number"] == 5
        assert rb.get_json()["page_number"] == 15

    def test_token_library_scope(self, client, db_connection):
        from tests.factories.db_factories import create_file_index_entry

        lib_a = create_library(name="A", path="/data/LibA")
        create_library(name="B", path="/data/LibB")
        set_user_libraries(self.alice, [lib_a])  # alice: only Library A

        create_file_index_entry(name="b.cbz", path="/data/LibB/b.cbz", parent="/data/LibB")
        fid = db_connection.execute(
            "SELECT id FROM file_index WHERE path = ?", ("/data/LibB/b.cbz",)
        ).fetchone()[0]

        # Alice (granted only A) can't see a file in B → 404.
        r = client.get(f"/api/v1/issue/{fid}", headers=_bearer(self.token_a))
        assert r.status_code == 404


class TestApiV1Disabled:
    def test_503_when_no_tokens(self, db_connection, client):
        # No users, no tokens, no legacy global token → API disabled.
        r = client.get("/api/v1/auth/ping")
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# OPDS identity
# ---------------------------------------------------------------------------
class TestOpdsImplicitOwner:
    def test_no_auth_required(self, db_connection, client, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        assert client.get("/opds/").status_code == 200


class TestOpdsMultiUser:
    @pytest.fixture(autouse=True)
    def _users(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("reader", password="readerpass", role="reader")
        yield

    def test_requires_auth(self, client):
        r = client.get("/opds/")
        assert r.status_code == 401
        assert "Basic" in r.headers.get("WWW-Authenticate", "")

    def test_wrong_credentials(self, client):
        assert client.get("/opds/", headers=_basic("reader", "nope")).status_code == 401

    def test_valid_credentials(self, client):
        assert client.get("/opds/", headers=_basic("reader", "readerpass")).status_code == 200

    def test_to_read_is_per_user(self, client):
        reader_id = get_user_by_username("reader")["id"]
        owner_id = get_user_by_username("owner")["id"]
        add_to_read("/data/reader-book.cbz", user_id=reader_id)
        add_to_read("/data/owner-book.cbz", user_id=owner_id)

        reader_feed = client.get("/opds/to-read",
                                 headers=_basic("reader", "readerpass")).get_data(as_text=True)
        owner_feed = client.get("/opds/to-read",
                                headers=_basic("owner", "ownerpass")).get_data(as_text=True)
        assert "reader-book.cbz" in reader_feed
        assert "reader-book.cbz" not in owner_feed
        assert "owner-book.cbz" in owner_feed
