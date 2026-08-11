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


class TestClientGroups:
    """Single-active is scoped per group so DC++ and Usenet can coexist."""

    @skip_no_crypto
    def test_activating_dcpp_keeps_usenet_active(self, db_connection):
        from core.database import (
            save_download_client_config,
            set_active_download_client,
            get_active_download_client,
            get_all_download_clients_status,
        )
        save_download_client_config("sabnzbd", {"api_key": "k1"}, client_group="usenet")
        save_download_client_config("airdcpp", {"username": "u", "password": "p"},
                                    client_group="dcpp")

        set_active_download_client("sabnzbd")
        set_active_download_client("airdcpp")

        # Both stay active — they are not competing for one slot.
        assert sum(s["is_active"] for s in get_all_download_clients_status()) == 2
        assert get_active_download_client("usenet")["client_type"] == "sabnzbd"
        assert get_active_download_client("dcpp")["client_type"] == "airdcpp"

    @skip_no_crypto
    def test_activating_usenet_keeps_dcpp_active(self, db_connection):
        from core.database import (
            save_download_client_config,
            set_active_download_client,
            get_active_download_client,
        )
        save_download_client_config("airdcpp", {"username": "u"}, client_group="dcpp")
        save_download_client_config("sabnzbd", {"api_key": "k"}, client_group="usenet")

        set_active_download_client("airdcpp")
        set_active_download_client("sabnzbd")

        assert get_active_download_client("dcpp")["client_type"] == "airdcpp"
        assert get_active_download_client("usenet")["client_type"] == "sabnzbd"

    @skip_no_crypto
    def test_within_a_group_still_single_active(self, db_connection):
        from core.database import (
            save_download_client_config,
            set_active_download_client,
            get_active_download_client,
        )
        save_download_client_config("sabnzbd", {"api_key": "k"}, client_group="usenet")
        save_download_client_config("nzbget", {"username": "u"}, client_group="usenet")

        set_active_download_client("sabnzbd")
        set_active_download_client("nzbget")

        assert get_active_download_client("usenet")["client_type"] == "nzbget"

    @skip_no_crypto
    def test_default_group_is_usenet(self, db_connection):
        # Existing callers that don't pass a group must keep working.
        from core.database import (
            save_download_client_config,
            set_active_download_client,
            get_active_download_client,
            get_all_download_clients_status,
        )
        save_download_client_config("sabnzbd", {"api_key": "k"})
        set_active_download_client("sabnzbd")

        assert get_active_download_client()["client_type"] == "sabnzbd"
        status = next(s for s in get_all_download_clients_status()
                      if s["client_type"] == "sabnzbd")
        assert status["client_group"] == "usenet"

    @skip_no_crypto
    def test_no_active_client_in_group(self, db_connection):
        from core.database import (
            save_download_client_config,
            set_active_download_client,
            get_active_download_client,
        )
        save_download_client_config("sabnzbd", {"api_key": "k"}, client_group="usenet")
        set_active_download_client("sabnzbd")
        assert get_active_download_client("dcpp") is None

    def test_activate_unconfigured_client_fails(self, db_connection):
        from core.database import set_active_download_client
        assert set_active_download_client("airdcpp") is False

    @skip_no_crypto
    def test_saving_again_preserves_group(self, db_connection):
        from core.database import (
            save_download_client_config,
            get_all_download_clients_status,
        )
        save_download_client_config("airdcpp", {"username": "u"}, client_group="dcpp")
        save_download_client_config("airdcpp", {"username": "u2"}, client_group="dcpp")
        status = next(s for s in get_all_download_clients_status()
                      if s["client_type"] == "airdcpp")
        assert status["client_group"] == "dcpp"


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


class TestDcppJobLedger:
    """The dcpp_jobs crash-recovery ledger: no encryption, plain CRUD."""

    JOB = {
        "client_type": "airdcpp", "client_id": "b1", "filename": "Batman 1.cbz",
        "series": "Batman", "issue": "1", "status": "downloading", "error": None,
        "percent": 0, "stage": "Queued", "bytes_total": 100,
        "bytes_downloaded": None, "target": None,
    }

    def test_save_and_read_back(self, db_connection):
        from core.database import save_dcpp_job, get_active_dcpp_jobs

        assert save_dcpp_job("d1", dict(self.JOB)) is True
        rows = get_active_dcpp_jobs()
        assert len(rows) == 1
        assert rows[0]["download_id"] == "d1"
        assert rows[0]["client_id"] == "b1"
        assert rows[0]["series"] == "Batman"

    def test_save_is_idempotent(self, db_connection):
        # Recovery may re-save a row it just hydrated; that must not duplicate.
        from core.database import save_dcpp_job, get_active_dcpp_jobs

        save_dcpp_job("d1", dict(self.JOB))
        save_dcpp_job("d1", dict(self.JOB, percent=50))
        rows = get_active_dcpp_jobs()
        assert len(rows) == 1
        assert rows[0]["percent"] == 50

    def test_extra_keys_are_ignored(self, db_connection):
        # models.dcpp hands its in-memory job dict straight over, and that dict
        # carries display-only fields the table has no column for.
        from core.database import save_dcpp_job, get_active_dcpp_jobs

        assert save_dcpp_job("d1", dict(self.JOB, untracked=False)) is True
        assert get_active_dcpp_jobs()[0]["download_id"] == "d1"

    def test_partial_update(self, db_connection):
        from core.database import (
            save_dcpp_job, update_dcpp_job, get_active_dcpp_jobs,
        )
        save_dcpp_job("d1", dict(self.JOB))
        assert update_dcpp_job(
            "d1", percent=75, target=r"F:\downloads\temp\Batman 1.cbz") is True

        row = get_active_dcpp_jobs()[0]
        assert row["percent"] == 75
        assert row["target"] == r"F:\downloads\temp\Batman 1.cbz"
        assert row["filename"] == "Batman 1.cbz"  # untouched

    def test_update_with_no_known_fields_is_a_no_op(self, db_connection):
        from core.database import save_dcpp_job, update_dcpp_job

        save_dcpp_job("d1", dict(self.JOB))
        assert update_dcpp_job("d1", nonsense="x") is False

    def test_delete(self, db_connection):
        from core.database import (
            save_dcpp_job, delete_dcpp_job, get_active_dcpp_jobs,
        )
        save_dcpp_job("d1", dict(self.JOB))
        assert delete_dcpp_job("d1") is True
        assert get_active_dcpp_jobs() == []
        # A second delete reports the miss so the route can 404.
        assert delete_dcpp_job("d1") is False

    def test_rows_come_back_oldest_first(self, db_connection):
        from core.database import save_dcpp_job, get_active_dcpp_jobs

        save_dcpp_job("d1", dict(self.JOB, client_id="b1"))
        save_dcpp_job("d2", dict(self.JOB, client_id="b2"))
        assert [r["download_id"] for r in get_active_dcpp_jobs()] == ["d1", "d2"]
