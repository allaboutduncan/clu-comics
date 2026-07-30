"""Tests for the /api/insights endpoint and its helpers.

/api/insights is a public (login-gate-exempt) JSON endpoint for the
gethomepage.dev `customapi` widget. It now supports an optional per-user API
token: with a `Authorization: Bearer <token>` header the reading counters
(issues_read / pages_read / time_reading*) are scoped to that user; without a
token they reflect the Store Owner (backwards compatible).

The route itself lives in app.py (not a blueprint); conftest registers an
equivalent route against the same real helpers, so these exercise the wiring
end-to-end. The helpers are also unit-tested directly.
"""
import pytest

from core.auth import resolve_optional_bearer_user
from core.database import (
    create_api_token,
    create_user,
    get_owner_user,
    mark_issue_read,
    set_user_preference,
)
from models.stats import get_insights_stats


READING_KEYS = {
    "issues_read",
    "pages_read",
    "time_reading",
    "time_reading_hours",
    "time_reading_minutes",
}
ALL_KEYS = READING_KEYS | {"total_files", "total_size", "root_folders"}


@pytest.fixture
def two_readers(db_connection):
    """Two reader accounts, each with a token and distinct reading history.

    Alice: 2 issues, 30 pages, 3900s (1h 05m).
    Bob:   1 issue, 5 pages, 120s.
    """
    alice = create_user("alice", role="reader")
    bob = create_user("bob", role="reader")
    token_a = create_api_token(alice)
    token_b = create_api_token(bob)

    mark_issue_read("/data/a1.cbz", page_count=20, time_spent=3600, user_id=alice)
    mark_issue_read("/data/a2.cbz", page_count=10, time_spent=300, user_id=alice)
    mark_issue_read("/data/b1.cbz", page_count=5, time_spent=120, user_id=bob)

    return {
        "alice": alice, "bob": bob,
        "token_a": token_a, "token_b": token_b,
    }


# =============================================================================
# resolve_optional_bearer_user
# =============================================================================


class TestResolveOptionalBearerUser:
    def test_no_header_is_anonymous(self, db_connection):
        assert resolve_optional_bearer_user("") == ("anonymous", None)

    def test_non_bearer_header_is_anonymous(self, db_connection):
        assert resolve_optional_bearer_user("Basic abc") == ("anonymous", None)

    def test_empty_bearer_is_anonymous(self, db_connection):
        assert resolve_optional_bearer_user("Bearer   ") == ("anonymous", None)

    def test_valid_per_user_token_resolves_user(self, two_readers):
        status, user = resolve_optional_bearer_user(
            f"Bearer {two_readers['token_a']}"
        )
        assert status == "ok"
        assert user["id"] == two_readers["alice"]

    def test_unknown_token_is_invalid(self, two_readers):
        assert resolve_optional_bearer_user("Bearer nope") == ("invalid", None)

    def test_legacy_global_token_maps_to_owner(self, db_connection):
        # Seed an owner so the legacy token has someone to map to.
        create_user("owner", role="owner", display_name="Store Owner")
        set_user_preference("api_token", "legacy-xyz", category="security")

        status, user = resolve_optional_bearer_user("Bearer legacy-xyz")
        assert status == "ok"
        owner = get_owner_user()
        assert user["id"] == owner["id"]


# =============================================================================
# get_insights_stats
# =============================================================================


class TestGetInsightsStats:
    def test_shape_and_keys(self, two_readers):
        payload = get_insights_stats(user_id=two_readers["alice"])
        assert ALL_KEYS.issubset(payload.keys())

    def test_scoped_reading_counters(self, two_readers):
        alice = get_insights_stats(user_id=two_readers["alice"])
        bob = get_insights_stats(user_id=two_readers["bob"])

        assert alice["issues_read"] == 2
        assert alice["pages_read"] == 30
        assert alice["time_reading"] == 3900
        assert alice["time_reading_hours"] == 1
        assert alice["time_reading_minutes"] == 5

        assert bob["issues_read"] == 1
        assert bob["pages_read"] == 5
        assert bob["time_reading"] == 120
        assert bob["time_reading_hours"] == 0
        assert bob["time_reading_minutes"] == 2


# =============================================================================
# /api/insights route
# =============================================================================


class TestInsightsRoute:
    def test_no_token_returns_200(self, db_connection, client):
        resp = client.get("/api/insights")
        assert resp.status_code == 200
        body = resp.get_json()
        assert ALL_KEYS.issubset(body.keys())

    def test_invalid_token_returns_401(self, two_readers, client):
        resp = client.get(
            "/api/insights",
            headers={"Authorization": "Bearer bogus-token"},
        )
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "unauthorized"

    def test_per_user_token_scopes_stats(self, two_readers, client):
        resp_a = client.get(
            "/api/insights",
            headers={"Authorization": f"Bearer {two_readers['token_a']}"},
        )
        resp_b = client.get(
            "/api/insights",
            headers={"Authorization": f"Bearer {two_readers['token_b']}"},
        )
        assert resp_a.status_code == 200 and resp_b.status_code == 200

        a = resp_a.get_json()
        b = resp_b.get_json()
        assert a["issues_read"] == 2
        assert a["pages_read"] == 30
        assert a["time_reading"] == 3900
        assert b["issues_read"] == 1
        assert b["pages_read"] == 5
        assert b["time_reading"] == 120

    def test_legacy_token_maps_to_owner(self, db_connection, client):
        owner_id = create_user("owner", role="owner", display_name="Store Owner")
        set_user_preference("api_token", "legacy-abc", category="security")
        mark_issue_read("/data/o.cbz", page_count=7, time_spent=60, user_id=owner_id)

        resp = client.get(
            "/api/insights",
            headers={"Authorization": "Bearer legacy-abc"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["issues_read"] == 1
        assert body["pages_read"] == 7
