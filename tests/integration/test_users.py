"""
Integration tests for the multi-user data layer (PR1):
user CRUD, password hashing, roles, per-user API tokens, library grants,
and the owner-seeding / backfill startup routine.
"""
import os

import pytest

from core.auth import ROLE_LEVELS, role_at_least
from core.database import (
    _hash_token,
    adopt_env_credentials_for_placeholder_owner,
    count_users,
    create_api_token,
    create_user,
    get_api_token,
    get_owner_user,
    get_user_by_id,
    get_user_by_token_hash,
    get_user_by_username,
    get_user_folder_paths,
    get_user_library_ids,
    init_db,
    seed_owner_if_needed,
    set_user_folders,
    set_user_libraries,
    set_user_password,
    user_has_library,
    verify_password,
)
from tests.factories.db_factories import create_library


# ---------------------------------------------------------------------------
# User CRUD & password hashing
# ---------------------------------------------------------------------------
class TestUserCrud:
    def test_create_and_fetch_user(self, db_connection):
        uid = create_user("alice", password="pw", role="clerk",
                          display_name="Alice", email="a@x.com")
        assert uid
        user = get_user_by_id(uid)
        assert user["username"] == "alice"
        assert user["role"] == "clerk"
        assert user["display_name"] == "Alice"
        # Password must be hashed, never stored plaintext.
        assert user["password_hash"] and user["password_hash"] != "pw"

    def test_username_is_unique_case_insensitive(self, db_connection):
        assert create_user("Bob", password="pw")
        assert create_user("bob", password="pw2") is None  # UNIQUE COLLATE NOCASE

    def test_get_user_by_username_case_insensitive(self, db_connection):
        create_user("Carol", password="pw")
        assert get_user_by_username("carol")["username"] == "Carol"

    def test_invalid_role_rejected(self, db_connection):
        assert create_user("dave", password="pw", role="superadmin") is None

    def test_count_users(self, db_connection):
        assert count_users() == 0
        create_user("u1", password="pw")
        create_user("u2", password="pw")
        assert count_users() == 2


# ---------------------------------------------------------------------------
# Password verification
# ---------------------------------------------------------------------------
class TestPasswordVerification:
    def test_correct_password(self, db_connection):
        create_user("eve", password="s3cret", role="reader")
        user = verify_password("eve", "s3cret")
        assert user and user["username"] == "eve"

    def test_wrong_password(self, db_connection):
        create_user("frank", password="s3cret")
        assert verify_password("frank", "nope") is None

    def test_unknown_user(self, db_connection):
        assert verify_password("ghost", "whatever") is None

    def test_inactive_user_cannot_authenticate(self, db_connection):
        uid = create_user("grace", password="pw")
        conn = db_connection
        conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (uid,))
        conn.commit()
        assert verify_password("grace", "pw") is None

    def test_placeholder_without_password_cannot_authenticate(self, db_connection):
        create_user("owner", password=None, role="owner", needs_setup=1)
        assert verify_password("owner", "") is None

    def test_set_user_password_enables_login_and_clears_needs_setup(self, db_connection):
        uid = create_user("owner", password=None, role="owner", needs_setup=1)
        assert set_user_password(uid, "newpw")
        assert verify_password("owner", "newpw")
        assert get_user_by_id(uid)["needs_setup"] == 0


# ---------------------------------------------------------------------------
# Role hierarchy
# ---------------------------------------------------------------------------
class TestRoleHierarchy:
    def test_levels_ordered(self):
        assert ROLE_LEVELS["reader"] < ROLE_LEVELS["clerk"] < ROLE_LEVELS["owner"]

    @pytest.mark.parametrize("role,minimum,expected", [
        ("owner", "reader", True),
        ("owner", "clerk", True),
        ("owner", "owner", True),
        ("clerk", "reader", True),
        ("clerk", "clerk", True),
        ("clerk", "owner", False),
        ("reader", "reader", True),
        ("reader", "clerk", False),
    ])
    def test_role_at_least(self, role, minimum, expected):
        assert role_at_least({"role": role}, minimum) is expected

    def test_none_user_never_satisfies(self):
        assert role_at_least(None, "reader") is False


# ---------------------------------------------------------------------------
# Per-user API tokens
# ---------------------------------------------------------------------------
class TestApiTokens:
    def test_create_token_resolves_to_user(self, db_connection):
        uid = create_user("holly", password="pw", role="reader")
        token = create_api_token(uid, name="phone")
        assert token
        user = get_user_by_token_hash(_hash_token(token))
        assert user and user["id"] == uid

    def test_token_stored_hashed_not_plaintext(self, db_connection):
        uid = create_user("ivan", password="pw")
        token = create_api_token(uid)
        row = db_connection.execute(
            "SELECT token_hash FROM api_tokens WHERE user_id = ?", (uid,)
        ).fetchone()
        assert row["token_hash"] == _hash_token(token)
        assert row["token_hash"] != token

    def test_unknown_token_returns_none(self, db_connection):
        assert get_user_by_token_hash(_hash_token("bogus")) is None

    def test_token_for_inactive_user_rejected(self, db_connection):
        uid = create_user("jane", password="pw")
        token = create_api_token(uid)
        db_connection.execute("UPDATE users SET is_active = 0 WHERE id = ?", (uid,))
        db_connection.commit()
        assert get_user_by_token_hash(_hash_token(token)) is None


# ---------------------------------------------------------------------------
# Library grants
# ---------------------------------------------------------------------------
class TestLibraryGrants:
    def test_grant_and_check(self, db_connection):
        uid = create_user("ken", password="pw", role="reader")
        lib_a = create_library(name="A", path="/data/a")
        lib_b = create_library(name="B", path="/data/b")

        set_user_libraries(uid, [lib_a])
        assert user_has_library(uid, lib_a) is True
        assert user_has_library(uid, lib_b) is False
        assert get_user_library_ids(uid) == {lib_a}

    def test_set_libraries_replaces_prior_grants(self, db_connection):
        uid = create_user("leo", password="pw")
        lib_a = create_library(name="A", path="/data/a")
        lib_b = create_library(name="B", path="/data/b")
        set_user_libraries(uid, [lib_a])
        set_user_libraries(uid, [lib_b])
        assert get_user_library_ids(uid) == {lib_b}

    def test_new_user_has_no_libraries_by_default(self, db_connection):
        uid = create_user("mia", password="pw")
        assert get_user_library_ids(uid) == set()


# ---------------------------------------------------------------------------
# Folder grants (refine library grants; see core/auth.folder_access_level)
# ---------------------------------------------------------------------------
class TestFolderGrants:
    def test_set_and_get_roundtrip(self, db_connection):
        uid = create_user("nadia", password="pw", role="reader")
        set_user_folders(uid, ["/data/a/X", "/data/a/Y"])
        assert get_user_folder_paths(uid) == [
            os.path.normpath("/data/a/X"), os.path.normpath("/data/a/Y")]

    def test_set_normalizes_and_dedups(self, db_connection):
        uid = create_user("omar", password="pw")
        set_user_folders(uid, ["/data/a/X/", "/data/a/X", "/data/a//Y"])
        assert get_user_folder_paths(uid) == [
            os.path.normpath("/data/a/X"), os.path.normpath("/data/a/Y")]

    def test_set_replaces_prior(self, db_connection):
        uid = create_user("peg", password="pw")
        set_user_folders(uid, ["/data/a/X"])
        set_user_folders(uid, ["/data/a/Y"])
        assert get_user_folder_paths(uid) == [os.path.normpath("/data/a/Y")]

    def test_new_user_has_no_folders(self, db_connection):
        uid = create_user("quinn", password="pw")
        assert get_user_folder_paths(uid) == []

    def test_backfill_seeds_root_grants_on_upgrade(self, db_connection):
        # Simulate a pre-feature DB: existing library grant, folder table absent.
        # init_db() must recreate the table and seed a whole-library root grant.
        uid = create_user("rick", password="pw", role="reader")
        lib = create_library(name="A", path="/data/a")
        set_user_libraries(uid, [lib])
        db_connection.execute("DROP TABLE user_folder_permissions")
        db_connection.commit()
        init_db()
        # The seeded grant equals the library's stored root path (normalized).
        assert get_user_folder_paths(uid) == [os.path.normpath("/data/a")]


# ---------------------------------------------------------------------------
# Owner seeding / backfill
# ---------------------------------------------------------------------------
class TestSeedOwner:
    def test_seed_creates_placeholder_owner_without_env(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        seed_owner_if_needed()
        owner = get_owner_user()
        assert owner and owner["role"] == "owner"
        assert owner["needs_setup"] == 1
        assert owner["password_hash"] is None

    def test_seed_from_env_creates_hashed_owner(self, db_connection, monkeypatch):
        monkeypatch.setenv("CLU_USERNAME", "boss")
        monkeypatch.setenv("CLU_PASSWORD", "hunter2")
        seed_owner_if_needed()
        owner = get_owner_user()
        assert owner["username"] == "boss"
        assert owner["needs_setup"] == 0
        assert verify_password("boss", "hunter2")

    def test_seed_is_idempotent(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        seed_owner_if_needed()
        seed_owner_if_needed()
        assert count_users() == 1

    def test_seed_skips_when_users_exist(self, db_connection, monkeypatch):
        create_user("existing", password="pw", role="reader")
        monkeypatch.setenv("CLU_USERNAME", "boss")
        monkeypatch.setenv("CLU_PASSWORD", "hunter2")
        seed_owner_if_needed()
        assert count_users() == 1
        assert get_owner_user() is None  # no owner was seeded

    def test_legacy_global_token_migrated_to_owner(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        from core.database import set_user_preference
        set_user_preference("api_token", "legacy-token-123", category="security")
        assert get_api_token() == "legacy-token-123"

        seed_owner_if_needed()
        owner = get_owner_user()
        # The old global token now resolves to the owner via api_tokens.
        user = get_user_by_token_hash(_hash_token("legacy-token-123"))
        assert user and user["id"] == owner["id"]


# ---------------------------------------------------------------------------
# Recovering an install whose owner never got a password
# ---------------------------------------------------------------------------
# The CLU_USERNAME/CLU_PASSWORD pair an operator would put in the compose file.
ENV_OWNER, ENV_OWNER_KEY = "envboss", "envpw"


def _set_env_gate(monkeypatch):
    """Set the env credential gate the way the compose file documents it."""
    monkeypatch.setenv("CLU_USERNAME", ENV_OWNER)
    monkeypatch.setenv("CLU_PASSWORD", ENV_OWNER_KEY)


class TestPlaceholderOwnerAdoptsEnvCredentials:
    """CLU_USERNAME/CLU_PASSWORD are the way back into a locked-out install.

    A placeholder owner has no password, so once login is required — the env
    gate itself, or a second account — nothing can authenticate and even
    /setup-owner is behind the gate. seed_owner_if_needed() reads the env vars
    only on an empty database, so these cover the non-empty case.
    """

    def test_placeholder_owner_takes_the_env_credentials(
            self, db_connection, monkeypatch):
        seed_owner_if_needed()  # placeholder owner, no password
        create_user("kid", password="pw", role="reader")  # login now required
        assert get_owner_user()["needs_setup"] == 1

        _set_env_gate(monkeypatch)
        seed_owner_if_needed()

        owner = get_owner_user()
        assert owner["username"] == ENV_OWNER
        assert owner["needs_setup"] == 0
        assert verify_password(ENV_OWNER, ENV_OWNER_KEY)["role"] == "owner"

    def test_configured_owner_keeps_its_own_password(
            self, db_connection, monkeypatch):
        """Never override credentials somebody has already set."""
        create_user("boss", password="pw", role="owner")
        _set_env_gate(monkeypatch)

        adopt_env_credentials_for_placeholder_owner()

        assert get_owner_user()["username"] == "boss"
        assert verify_password("boss", "pw")
        assert verify_password(ENV_OWNER, ENV_OWNER_KEY) is None

    def test_username_taken_keeps_the_owner_name(self, db_connection, monkeypatch):
        seed_owner_if_needed()
        create_user(ENV_OWNER, password="pw", role="reader")  # CLU_USERNAME clashes
        _set_env_gate(monkeypatch)

        adopt_env_credentials_for_placeholder_owner()

        owner = get_owner_user()
        assert owner["username"] == "owner"  # not renamed onto the clash
        assert verify_password("owner", ENV_OWNER_KEY)["role"] == "owner"
        assert verify_password(ENV_OWNER, "pw")["role"] == "reader"  # untouched

    def test_no_env_credentials_changes_nothing(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        seed_owner_if_needed()
        create_user("kid", password="pw", role="reader")

        adopt_env_credentials_for_placeholder_owner()

        assert get_owner_user()["needs_setup"] == 1

    def test_is_idempotent(self, db_connection, monkeypatch):
        seed_owner_if_needed()
        create_user("kid", password="pw", role="reader")
        _set_env_gate(monkeypatch)

        adopt_env_credentials_for_placeholder_owner()
        adopt_env_credentials_for_placeholder_owner()

        assert count_users() == 2
        assert verify_password(ENV_OWNER, ENV_OWNER_KEY)
