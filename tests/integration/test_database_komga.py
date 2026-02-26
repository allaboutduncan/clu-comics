"""Tests for Komga database functions -- config, sync log, library mappings."""
import pytest

# cryptography may not be installed locally (needed for komga credential encryption)
crypto_available = False
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    crypto_available = True
except ImportError:
    pass

skip_no_crypto = pytest.mark.skipif(
    not crypto_available, reason="cryptography package not installed"
)


class TestKomgaConfig:

    def test_get_default_config(self, db_connection):
        from database import get_komga_config

        config = get_komga_config()
        assert config is not None
        assert config["server_url"] == ""

    @skip_no_crypto
    def test_save_and_get_config(self, db_connection):
        from database import save_komga_config, get_komga_config

        ok = save_komga_config(
            server_url="http://localhost:8080",
            username="admin",
            password="secret",
            enabled=True,
            frequency="daily",
            time="05:00",
        )
        assert ok is True

        config = get_komga_config()
        assert config["server_url"] == "http://localhost:8080"

    @skip_no_crypto
    def test_update_last_sync(self, db_connection):
        from database import save_komga_config, update_komga_last_sync, get_komga_config

        save_komga_config(server_url="http://localhost:8080")
        update_komga_last_sync(read_count=5, progress_count=3)

        config = get_komga_config()
        assert config is not None


class TestKomgaSyncLog:

    def test_mark_and_check(self, db_connection):
        from database import mark_komga_book_synced, is_komga_book_synced

        ok = mark_komga_book_synced(
            komga_book_id="book-123",
            komga_path="/komga/books/123",
            clu_path="/data/DC/Batman/001.cbz",
            sync_type="read",
        )
        assert ok is True
        assert is_komga_book_synced("book-123", "read") is True

    def test_not_synced_by_default(self, db_connection):
        from database import is_komga_book_synced

        assert is_komga_book_synced("nonexistent", "read") is False

    def test_sync_stats(self, db_connection):
        from database import mark_komga_book_synced, get_komga_sync_stats

        mark_komga_book_synced("b1", "/k/1", "/c/1", "read")
        mark_komga_book_synced("b2", "/k/2", "/c/2", "read")
        mark_komga_book_synced("b3", "/k/3", "/c/3", "progress")

        stats = get_komga_sync_stats()
        assert stats is not None


class TestKomgaLibraryMappings:

    def test_save_and_get(self, db_connection):
        from database import save_komga_library_mappings, get_komga_library_mappings
        from tests.factories.db_factories import create_library

        lib_id = create_library()

        mappings = [{"library_id": lib_id, "komga_path_prefix": "/komga/data"}]
        ok = save_komga_library_mappings(mappings)
        assert ok is True

        result = get_komga_library_mappings()
        assert len(result) >= 1

    def test_empty_mappings(self, db_connection):
        from database import get_komga_library_mappings

        result = get_komga_library_mappings()
        assert isinstance(result, list)
