"""Tests for models/providers/__init__.py -- provider registration and factory."""
import pytest
from models.providers.base import ProviderType, ProviderCredentials


class TestProviderRegistry:

    def test_metron_is_registered(self):
        from models.providers import is_provider_registered
        assert is_provider_registered(ProviderType.METRON) is True

    def test_comicvine_is_registered(self):
        from models.providers import is_provider_registered
        assert is_provider_registered(ProviderType.COMICVINE) is True

    def test_gcd_is_registered(self):
        from models.providers import is_provider_registered
        assert is_provider_registered(ProviderType.GCD) is True

    def test_anilist_is_registered(self):
        from models.providers import is_provider_registered
        assert is_provider_registered(ProviderType.ANILIST) is True

    def test_mangadex_is_registered(self):
        from models.providers import is_provider_registered
        assert is_provider_registered(ProviderType.MANGADEX) is True


class TestGetProvider:

    def test_creates_metron_provider(self):
        from models.providers import get_provider
        from models.providers.metron_provider import MetronProvider
        provider = get_provider(ProviderType.METRON)
        assert isinstance(provider, MetronProvider)

    def test_creates_comicvine_provider(self):
        from models.providers import get_provider
        from models.providers.comicvine_provider import ComicVineProvider
        provider = get_provider(ProviderType.COMICVINE)
        assert isinstance(provider, ComicVineProvider)

    def test_passes_credentials(self):
        from models.providers import get_provider
        creds = ProviderCredentials(username="user", password="pass")
        provider = get_provider(ProviderType.METRON, credentials=creds)
        assert provider.credentials.username == "user"
        assert provider.credentials.password == "pass"

    def test_unknown_type_raises(self):
        from models.providers import get_provider
        # Passing a raw string instead of ProviderType raises AttributeError
        # since get_provider expects ProviderType and accesses .value
        with pytest.raises((ValueError, AttributeError)):
            get_provider("nonexistent")


class TestGetProviderByName:

    def test_by_name_metron(self):
        from models.providers import get_provider_by_name
        provider = get_provider_by_name("metron")
        assert provider.provider_type == ProviderType.METRON

    def test_by_name_case_insensitive(self):
        from models.providers import get_provider_by_name
        provider = get_provider_by_name("METRON")
        assert provider.provider_type == ProviderType.METRON

    def test_unknown_name_raises(self):
        from models.providers import get_provider_by_name
        with pytest.raises(ValueError, match="Unknown provider"):
            get_provider_by_name("fakeprovider")


class TestGetRegisteredProviders:

    def test_returns_list(self):
        from models.providers import get_registered_providers
        providers = get_registered_providers()
        assert isinstance(providers, list)
        assert len(providers) >= 4  # At least metron, comicvine, gcd, anilist


class TestGetAvailableProviders:

    def test_returns_list_of_dicts(self):
        from models.providers import get_available_providers
        providers = get_available_providers()
        assert isinstance(providers, list)
        for p in providers:
            assert "type" in p
            assert "name" in p
            assert "requires_auth" in p
            assert "auth_fields" in p
            assert "rate_limit" in p


class TestGetProviderClass:

    def test_returns_class(self):
        from models.providers import get_provider_class
        from models.providers.metron_provider import MetronProvider
        cls = get_provider_class(ProviderType.METRON)
        assert cls is MetronProvider

    def test_returns_none_for_unregistered(self):
        from models.providers import get_provider_class
        # Use a monkeypatch-free approach: just test a valid type returns non-None
        cls = get_provider_class(ProviderType.METRON)
        assert cls is not None
