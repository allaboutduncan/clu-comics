"""Tests for provider credentials -- encrypt/decrypt/mask, library_providers."""
import pytest

# cryptography may not be installed locally
crypto_available = False
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    crypto_available = True
except ImportError:
    pass

skip_no_crypto = pytest.mark.skipif(
    not crypto_available, reason="cryptography package not installed"
)


class TestProviderCredentials:

    @skip_no_crypto
    def test_save_and_get(self, db_connection):
        from database import save_provider_credentials, get_provider_credentials

        creds = {"api_key": "test-key-12345", "username": "testuser"}
        ok = save_provider_credentials("metron", creds)
        assert ok is True

        retrieved = get_provider_credentials("metron")
        assert retrieved is not None
        assert retrieved["api_key"] == "test-key-12345"
        assert retrieved["username"] == "testuser"

    @skip_no_crypto
    def test_get_masked(self, db_connection):
        from database import save_provider_credentials, get_provider_credentials_masked

        save_provider_credentials("comicvine", {"api_key": "abcdefghijklmnop"})

        masked = get_provider_credentials_masked("comicvine")
        assert masked is not None
        # Masked values should have "..." in them
        assert "..." in masked["api_key"]
        assert masked["api_key"] != "abcdefghijklmnop"

    @skip_no_crypto
    def test_update_validity(self, db_connection):
        from database import (
            save_provider_credentials,
            update_provider_validity,
            get_all_provider_credentials_status,
        )

        save_provider_credentials("metron", {"api_key": "key"})
        update_provider_validity("metron", is_valid=True)

        statuses = get_all_provider_credentials_status()
        metron_status = next(
            (s for s in statuses if s["provider_type"] == "metron"), None
        )
        assert metron_status is not None
        assert metron_status["is_valid"] == 1

    @skip_no_crypto
    def test_delete_credentials(self, db_connection):
        from database import (
            save_provider_credentials,
            delete_provider_credentials,
            get_provider_credentials,
        )

        save_provider_credentials("gcd", {"host": "localhost"})
        delete_provider_credentials("gcd")
        assert get_provider_credentials("gcd") is None

    def test_get_nonexistent(self, db_connection):
        from database import get_provider_credentials

        assert get_provider_credentials("nonexistent") is None


class TestLibraryProviders:

    def test_add_and_get(self, db_connection):
        from database import add_library_provider, get_library_providers
        from tests.factories.db_factories import create_library

        lib_id = create_library()
        ok = add_library_provider(lib_id, "metron", priority=1, enabled=True)
        assert ok is True

        providers = get_library_providers(lib_id)
        assert len(providers) == 1
        assert providers[0]["provider_type"] == "metron"
        assert providers[0]["priority"] == 1

    def test_set_replaces_existing(self, db_connection):
        from database import add_library_provider, set_library_providers, get_library_providers
        from tests.factories.db_factories import create_library

        lib_id = create_library()
        add_library_provider(lib_id, "metron")

        new_providers = [
            {"provider_type": "comicvine", "priority": 1, "enabled": True},
            {"provider_type": "gcd", "priority": 2, "enabled": True},
        ]
        set_library_providers(lib_id, new_providers)

        providers = get_library_providers(lib_id)
        types = {p["provider_type"] for p in providers}
        assert "comicvine" in types
        assert "gcd" in types
        assert "metron" not in types

    def test_remove_provider(self, db_connection):
        from database import add_library_provider, remove_library_provider, get_library_providers
        from tests.factories.db_factories import create_library

        lib_id = create_library()
        add_library_provider(lib_id, "metron")
        add_library_provider(lib_id, "comicvine")

        remove_library_provider(lib_id, "metron")

        providers = get_library_providers(lib_id)
        types = [p["provider_type"] for p in providers]
        assert "metron" not in types
        assert "comicvine" in types

    def test_empty_library(self, db_connection):
        from database import get_library_providers
        from tests.factories.db_factories import create_library

        lib_id = create_library()
        assert get_library_providers(lib_id) == []
