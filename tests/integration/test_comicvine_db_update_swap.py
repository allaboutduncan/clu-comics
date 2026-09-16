"""The install half of the local-ComicVine-DB update, against real files.

The unit tests cover *whether* it runs. These cover what it does to the disk,
because every one of these was a way to lose a working database:

* staging must be a sibling of the destination, or the final step stops being
  an atomic rename within one directory;
* the 541 MB archive must go before the swap, or peak disk is old + new + zip
  on the one volume that has to hold all three;
* permissions must be normalised on the staged file, or a root-fallback start
  writes it root:0600 and the provider -- which opens it read-only on every
  single lookup -- silently stops working;
* a stale ``-wal`` from the old database beside a new one is corruption;
* and a failure has to be a no-op, leaving the original byte-for-byte.
"""

import hashlib
import io
import os
import sqlite3
import zipfile

import pytest

from core import comicvine_db_update as mod


@pytest.fixture(autouse=True)
def isolated_prefs(monkeypatch):
    store = {}
    monkeypatch.setattr(mod, "_get_pref", lambda k, default=None: store.get(k, default))
    monkeypatch.setattr(mod, "_set_pref", lambda k, v: store.__setitem__(k, v))
    return store


def _db_bytes(tmp_path, name="seed.db"):
    path = tmp_path / name
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE cv_volume (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE cv_issue (id INTEGER PRIMARY KEY, volume_id INTEGER)")
    conn.execute("INSERT INTO cv_volume (id, name) VALUES (1, 'Batman')")
    conn.execute("INSERT INTO cv_issue (id, volume_id) VALUES (1, 1)")
    conn.commit()
    conn.close()
    data = path.read_bytes()
    path.unlink()
    return data


def _zip_of(db_bytes, name="localcv.db"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, db_bytes)
    return buf.getvalue()


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A configured destination holding an existing database."""
    dest_dir = tmp_path / "config"
    dest_dir.mkdir()
    db = dest_dir / "comicvine.db"
    db.write_bytes(b"the database the user already had")
    monkeypatch.setattr(mod, "get_database_path", lambda: str(db))
    return {"dir": dest_dir, "db": db}


def _install_fake_download(monkeypatch, payload, recorder=None):
    def _fake(dest, say):
        with open(dest, "wb") as fh:
            fh.write(payload)
        if recorder is not None:
            recorder.append(("downloaded", dest))
        return len(payload)

    monkeypatch.setattr(mod, "_download_zip", _fake)


class TestSwap:
    def test_installs_the_new_database(self, library, tmp_path, monkeypatch):
        fresh = _db_bytes(tmp_path)
        digest = hashlib.md5(fresh, usedforsecurity=False).hexdigest()
        monkeypatch.setattr(mod, "probe", lambda: (digest, None))
        _install_fake_download(monkeypatch, _zip_of(fresh))

        result = mod.run_update()

        assert result["success"] is True, result.get("error")
        assert library["db"].read_bytes() == fresh

    def test_stages_beside_the_destination(self, library, tmp_path, monkeypatch):
        """A staged file on another filesystem turns the atomic rename into a
        copy, which is neither atomic nor guaranteed to fit."""
        fresh = _db_bytes(tmp_path)
        digest = hashlib.md5(fresh, usedforsecurity=False).hexdigest()
        monkeypatch.setattr(mod, "probe", lambda: (digest, None))

        seen = {}

        def _fake_extract(zip_path, dest, say):
            seen["zip_dir"] = os.path.dirname(zip_path)
            seen["db_dir"] = os.path.dirname(dest)
            with open(dest, "wb") as fh:
                fh.write(fresh)
            return digest

        _install_fake_download(monkeypatch, _zip_of(fresh))
        monkeypatch.setattr(mod, "_extract_db", _fake_extract)

        mod.run_update()

        assert seen["db_dir"] == str(library["dir"])
        assert seen["zip_dir"] == str(library["dir"])

    def test_archive_is_deleted_before_the_swap(self, library, tmp_path, monkeypatch):
        """Peak disk has to be old + new, not old + new + a 541 MB archive."""
        fresh = _db_bytes(tmp_path)
        digest = hashlib.md5(fresh, usedforsecurity=False).hexdigest()
        monkeypatch.setattr(mod, "probe", lambda: (digest, None))
        _install_fake_download(monkeypatch, _zip_of(fresh))

        zip_present_at_swap = {}
        real_replace = mod.os.replace

        def _watch_replace(src, dst):
            zip_present_at_swap["value"] = os.path.exists(
                os.path.join(str(library["dir"]), ".localcv.zip.clu_incoming")
            )
            return real_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", _watch_replace)

        mod.run_update()

        assert zip_present_at_swap["value"] is False

    def test_permissions_are_normalised_before_the_swap(
        self, library, tmp_path, monkeypatch
    ):
        """Otherwise a root-fallback start leaves the new file root:0600 and the
        provider, which only ever opens it read-only, stops working silently."""
        fresh = _db_bytes(tmp_path)
        digest = hashlib.md5(fresh, usedforsecurity=False).hexdigest()
        monkeypatch.setattr(mod, "probe", lambda: (digest, None))
        _install_fake_download(monkeypatch, _zip_of(fresh))

        order = []
        import helpers

        real_match = helpers.match_parent_permissions
        real_replace = mod.os.replace

        def _watched_match(path):
            order.append(("perms", path))
            return real_match(path)

        def _watched_replace(src, dst):
            order.append(("replace", src))
            return real_replace(src, dst)

        monkeypatch.setattr(helpers, "match_parent_permissions", _watched_match)
        monkeypatch.setattr(mod.os, "replace", _watched_replace)

        mod.run_update()

        steps = [step for step, _ in order]
        assert "perms" in steps, "the staged database never had its permissions fixed"
        assert steps.index("perms") < steps.index("replace")
        # And it was the staged file, not the destination.
        staged = [p for step, p in order if step == "perms"][0]
        assert os.path.basename(staged).startswith(".")

    def test_stale_sidecars_are_removed(self, library, tmp_path, monkeypatch):
        """A -wal belonging to the old database beside a new one is corruption."""
        wal = library["dir"] / "comicvine.db-wal"
        shm = library["dir"] / "comicvine.db-shm"
        wal.write_bytes(b"stale wal")
        shm.write_bytes(b"stale shm")

        fresh = _db_bytes(tmp_path)
        digest = hashlib.md5(fresh, usedforsecurity=False).hexdigest()
        monkeypatch.setattr(mod, "probe", lambda: (digest, None))
        _install_fake_download(monkeypatch, _zip_of(fresh))

        mod.run_update()

        assert not wal.exists()
        assert not shm.exists()

    def test_no_staged_files_are_left_behind(self, library, tmp_path, monkeypatch):
        fresh = _db_bytes(tmp_path)
        digest = hashlib.md5(fresh, usedforsecurity=False).hexdigest()
        monkeypatch.setattr(mod, "probe", lambda: (digest, None))
        _install_fake_download(monkeypatch, _zip_of(fresh))

        mod.run_update()

        leftovers = [p.name for p in library["dir"].iterdir()
                     if "clu_incoming" in p.name]
        assert leftovers == []


class TestFailureIsANoOp:
    def test_a_bad_checksum_leaves_the_original_and_cleans_up(
        self, library, tmp_path, monkeypatch, isolated_prefs
    ):
        original = library["db"].read_bytes()
        fresh = _db_bytes(tmp_path)
        monkeypatch.setattr(mod, "probe", lambda: ("f" * 32, None))
        _install_fake_download(monkeypatch, _zip_of(fresh))

        result = mod.run_update()

        assert result["success"] is False
        assert library["db"].read_bytes() == original
        leftovers = [p.name for p in library["dir"].iterdir()
                     if "clu_incoming" in p.name]
        assert leftovers == []
        assert mod.PREF_DB_TOKEN not in isolated_prefs
        assert mod.PREF_LAST_UPDATE not in isolated_prefs

    def test_a_failed_replace_leaves_the_original(
        self, library, tmp_path, monkeypatch, isolated_prefs
    ):
        original = library["db"].read_bytes()
        fresh = _db_bytes(tmp_path)
        digest = hashlib.md5(fresh, usedforsecurity=False).hexdigest()
        monkeypatch.setattr(mod, "probe", lambda: (digest, None))
        _install_fake_download(monkeypatch, _zip_of(fresh))

        def _boom(src, dst):
            raise OSError("device is busy")

        monkeypatch.setattr(mod.os, "replace", _boom)

        result = mod.run_update()

        assert result["success"] is False
        assert library["db"].read_bytes() == original
        leftovers = [p.name for p in library["dir"].iterdir()
                     if "clu_incoming" in p.name]
        assert leftovers == []
        assert mod.PREF_DB_TOKEN not in isolated_prefs

    def test_a_download_failure_leaves_the_original(
        self, library, monkeypatch, isolated_prefs
    ):
        original = library["db"].read_bytes()
        monkeypatch.setattr(mod, "probe", lambda: ("f" * 32, None))

        def _boom(dest, say):
            with open(dest, "wb") as fh:
                fh.write(b"partial")
            raise mod.UpdateError("The download stopped early.")

        monkeypatch.setattr(mod, "_download_zip", _boom)

        result = mod.run_update()

        assert result["success"] is False
        assert library["db"].read_bytes() == original
        leftovers = [p.name for p in library["dir"].iterdir()
                     if "clu_incoming" in p.name]
        assert leftovers == [], "a partial download was left on the volume"


class TestVerifyDatabase:
    def test_accepts_a_real_dump(self, tmp_path):
        path = tmp_path / "ok.db"
        path.write_bytes(_db_bytes(tmp_path))
        ok, why = mod.verify_database(str(path))
        assert ok, why

    def test_rejects_a_non_database(self, tmp_path):
        path = tmp_path / "junk.db"
        path.write_bytes(b"not a database at all")
        ok, _ = mod.verify_database(str(path))
        assert ok is False

    def test_rejects_a_database_without_the_comicvine_tables(self, tmp_path):
        path = tmp_path / "wrong.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE something_else (id INTEGER)")
        conn.commit()
        conn.close()
        ok, why = mod.verify_database(str(path))
        assert ok is False
        assert "cv_volume" in why
