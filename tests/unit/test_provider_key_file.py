"""The provider encryption key is created once and never overwritten.

Every stored provider credential is encrypted with this key, so replacing it
makes them all permanently undecryptable. It used to be regenerated (and the
file truncated) on any failed or short read.
"""
import builtins
import errno
import os
import threading

import pytest

pytest.importorskip("cryptography", reason="cryptography package not installed")

from models.providers import crypto  # noqa: E402


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    path = str(tmp_path / "cfg" / ".provider_key")
    monkeypatch.setattr(crypto, "KEY_FILE", path)
    crypto._reset_key_cache()
    yield path
    crypto._reset_key_cache()


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


class TestExistingFileIsNeverOverwritten:
    @pytest.mark.parametrize("content", [b"", b"short", b"x" * 31, b"y" * 33])
    def test_wrong_length_raises_and_leaves_file(self, key_file, content):
        _write(key_file, content)
        with pytest.raises(crypto.ProviderKeyError):
            crypto._get_or_create_key()
        assert _read(key_file) == content

    def test_wrong_length_is_not_cached(self, key_file):
        _write(key_file, b"short")
        with pytest.raises(crypto.ProviderKeyError):
            crypto._get_or_create_key()
        good = os.urandom(32)
        _write(key_file, good)
        assert crypto._get_or_create_key() == good

    def test_read_error_raises_and_leaves_file(self, key_file, monkeypatch):
        original = os.urandom(32)
        _write(key_file, original)
        real_open = builtins.open

        def flaky_open(file, *args, **kwargs):
            if os.fspath(file) == key_file:
                raise OSError(errno.EIO, "I/O error")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", flaky_open)
        with pytest.raises(crypto.ProviderKeyError):
            crypto._get_or_create_key()
        monkeypatch.setattr(builtins, "open", real_open)
        assert _read(key_file) == original
        # And once readable again, the original key is used.
        assert crypto._get_or_create_key() == original

    def test_unreadable_path_raises_and_is_left_alone(self, key_file):
        # A directory at the key path exists but cannot be read as a file.
        os.makedirs(key_file)
        with pytest.raises(crypto.ProviderKeyError):
            crypto._get_or_create_key()
        assert os.path.isdir(key_file)

    def test_decrypt_with_damaged_key_raises_clearly(self, key_file):
        ciphertext, nonce = crypto.encrypt_credentials({"api_key": "secret"})
        stored = _read(key_file)
        crypto._reset_key_cache()
        _write(key_file, stored[:10])
        with pytest.raises(crypto.ProviderKeyError):
            crypto.decrypt_credentials(ciphertext, nonce)
        assert _read(key_file) == stored[:10]


class TestCreation:
    def test_missing_file_creates_a_32_byte_key(self, key_file):
        assert not os.path.exists(key_file)
        key = crypto._get_or_create_key()
        assert len(key) == 32
        assert _read(key_file) == key
        leftovers = [
            n for n in os.listdir(os.path.dirname(key_file)) if n != ".provider_key"
        ]
        assert leftovers == []

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_key_file_is_owner_only(self, key_file):
        crypto._get_or_create_key()
        assert os.stat(key_file).st_mode & 0o777 == 0o600

    def test_existing_values_still_decrypt(self, key_file):
        creds = {"username": "u", "password": "p", "api_key": "k"}
        ciphertext, nonce = crypto.encrypt_credentials(creds)
        crypto._reset_key_cache()  # as a fresh process would
        assert crypto.decrypt_credentials(ciphertext, nonce) == creds

    def test_cached_key_survives_a_later_read_failure(self, key_file, monkeypatch):
        key = crypto._get_or_create_key()

        def boom(*a, **k):
            raise AssertionError("key file must not be re-read")

        monkeypatch.setattr(crypto, "_read_existing_key", boom)
        assert crypto._get_or_create_key() == key

    def test_concurrent_first_run_agrees_on_one_key(self, key_file):
        results, errors = [], []
        barrier = threading.Barrier(12)

        def worker():
            try:
                barrier.wait()
                results.append(crypto._get_or_create_key())
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(set(results)) == 1
        assert _read(key_file) == results[0]

    @pytest.mark.parametrize("hard_links", [True, False])
    def test_racing_creators_without_shared_lock_agree(
        self, key_file, monkeypatch, hard_links
    ):
        # _create_key bypasses the in-process lock, as two processes would.
        if not hard_links:
            def no_link(src, dst):
                raise OSError(errno.EPERM, "hard links not supported")

            monkeypatch.setattr(crypto.os, "link", no_link)

        results, errors = [], []
        barrier = threading.Barrier(10)

        def worker():
            try:
                barrier.wait()
                results.append(crypto._create_key(key_file))
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(set(results)) == 1
        assert _read(key_file) == results[0]

    def test_create_never_clobbers_an_existing_file(self, key_file):
        winner = os.urandom(32)
        _write(key_file, winner)
        assert crypto._create_key(key_file) == winner
        assert _read(key_file) == winner


class TestRotate:
    def test_rotate_replaces_key_and_cache(self, key_file):
        old = crypto._get_or_create_key()
        assert crypto.rotate_key() is True
        new = crypto._get_or_create_key()
        assert new != old
        assert _read(key_file) == new
