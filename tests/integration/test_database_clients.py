"""Tests for download-client and indexer DB accessors (encrypted config)."""
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


class TestDownloadClientConfig:

    @skip_no_crypto
    def test_save_and_get(self, db_connection):
        from core.database import (
            save_download_client_config,
            get_download_client_config,
        )
        cfg = {"host": "localhost", "port": 8080, "api_key": "SECRETKEY1234", "category": "comics"}
        assert save_download_client_config("sabnzbd", cfg) is True
        got = get_download_client_config("sabnzbd")
        assert got == cfg

    @skip_no_crypto
    def test_masked(self, db_connection):
        from core.database import (
            save_download_client_config,
            get_download_client_config_masked,
        )
        save_download_client_config("sabnzbd", {"api_key": "abcdefghijklmnop"})
        masked = get_download_client_config_masked("sabnzbd")
        assert "..." in masked["api_key"]
        assert masked["api_key"] != "abcdefghijklmnop"

    @skip_no_crypto
    def test_update_validity(self, db_connection):
        from core.database import (
            save_download_client_config,
            update_download_client_validity,
            get_all_download_clients_status,
        )
        save_download_client_config("sabnzbd", {"api_key": "k"})
        update_download_client_validity("sabnzbd", True)
        status = next(s for s in get_all_download_clients_status()
                      if s["client_type"] == "sabnzbd")
        assert status["is_valid"] == 1

    @skip_no_crypto
    def test_single_active_invariant(self, db_connection):
        from core.database import (
            save_download_client_config,
            set_active_download_client,
            get_all_download_clients_status,
            get_active_download_client,
        )
        save_download_client_config("sabnzbd", {"api_key": "k1"})
        save_download_client_config("nzbget", {"username": "u", "password": "p"})
        set_active_download_client("sabnzbd")
        set_active_download_client("nzbget")
        statuses = get_all_download_clients_status()
        assert sum(s["is_active"] for s in statuses) == 1
        active = get_active_download_client()
        assert active["client_type"] == "nzbget"
        assert active["config"]["username"] == "u"

    @skip_no_crypto
    def test_delete(self, db_connection):
        from core.database import (
            save_download_client_config,
            delete_download_client_config,
            get_download_client_config,
        )
        save_download_client_config("nzbget", {"username": "u"})
        delete_download_client_config("nzbget")
        assert get_download_client_config("nzbget") is None

    def test_get_nonexistent(self, db_connection):
        from core.database import get_download_client_config
        assert get_download_client_config("sabnzbd") is None


class TestIndexers:

    @skip_no_crypto
    def test_add_and_get(self, db_connection):
        from core.database import add_indexer, get_indexer
        iid = add_indexer("NZBgeek", "https://api.nzbgeek.info",
                          {"api_key": "KEYAAAA1111", "categories": "7000"}, priority=0)
        assert isinstance(iid, int)
        got = get_indexer(iid)
        assert got["name"] == "NZBgeek"
        assert got["api_key"] == "KEYAAAA1111"
        assert got["categories"] == "7000"
        assert got["enabled"] is True

    @skip_no_crypto
    def test_masked(self, db_connection):
        from core.database import add_indexer, get_indexer_masked
        iid = add_indexer("X", "https://x", {"api_key": "abcdefghijklmnop"})
        masked = get_indexer_masked(iid)
        assert "..." in masked["api_key"]
        assert masked["name"] == "X"

    @skip_no_crypto
    def test_partial_update_keeps_secret(self, db_connection):
        from core.database import add_indexer, update_indexer, get_indexer
        iid = add_indexer("X", "https://x", {"api_key": "KEEPME1234"})
        # Metadata-only update must not wipe the api_key.
        update_indexer(iid, name="Renamed")
        got = get_indexer(iid)
        assert got["name"] == "Renamed"
        assert got["api_key"] == "KEEPME1234"

    @skip_no_crypto
    def test_reorder(self, db_connection):
        from core.database import add_indexer, set_indexer_order, get_all_indexers
        a = add_indexer("A", "https://a", {}, priority=0)
        b = add_indexer("B", "https://b", {}, priority=1)
        set_indexer_order([b, a])
        order = [(x["id"], x["priority"]) for x in get_all_indexers()]
        assert order[0][0] == b and order[0][1] == 0
        assert order[1][0] == a and order[1][1] == 1

    @skip_no_crypto
    def test_enabled_filter(self, db_connection):
        from core.database import add_indexer, update_indexer, get_enabled_indexers
        a = add_indexer("A", "https://a", {}, enabled=True)
        b = add_indexer("B", "https://b", {}, enabled=True)
        update_indexer(a, enabled=False)
        enabled_ids = [x["id"] for x in get_enabled_indexers()]
        assert b in enabled_ids
        assert a not in enabled_ids

    @skip_no_crypto
    def test_validity_and_delete(self, db_connection):
        from core.database import (
            add_indexer, update_indexer_validity, get_indexer, delete_indexer,
        )
        iid = add_indexer("A", "https://a", {})
        update_indexer_validity(iid, True)
        assert get_indexer(iid)["is_valid"] is True
        delete_indexer(iid)
        assert get_indexer(iid) is None
