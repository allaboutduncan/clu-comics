"""Guided Setup API (``routes/setup.py``).

The check logic is covered in ``tests/unit/test_setup_preflight.py``; these
assert the route, its payload shape and that it is owner-only.
"""
import os
from unittest.mock import patch

import pytest

URL = "/api/setup/preflight"
BROWSE_URL = "/api/setup/browse"


@pytest.mark.parametrize("url", [URL, BROWSE_URL])
def test_route_is_owner_only_by_prefix(url):
    from core.auth import _OWNER_PREFIXES

    assert url.startswith(_OWNER_PREFIXES)


class TestPreflight:
    def test_returns_checks(self, client):
        resp = client.get(URL)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert isinstance(data["blocking"], bool)
        ids = [c["id"] for c in data["checks"]]
        for expected in ("database", "config_writable", "cache_writable",
                         "data_mount", "watch_dir", "target_dir",
                         "owner_account", "run_as_user"):
            assert expected in ids
        for c in data["checks"]:
            assert c["status"] in ("ok", "warn", "fail", "skip")

    def test_passes_the_payload_through(self, client):
        payload = {"success": True, "blocking": True, "summary": {}, "checks": []}
        with patch("core.setup_preflight.run_preflight", return_value=payload):
            resp = client.get(URL)
        assert resp.get_json() == payload


class TestAccess:
    @pytest.fixture(autouse=True)
    def _users(self, db_connection, monkeypatch):
        from core.database import create_user

        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("clerk", password="clerkpass", role="clerk")
        create_user("reader", password="readerpass", role="reader")

    def _login(self, client, username, password):
        client.post("/login", data={"username": username, "password": password})

    @pytest.mark.parametrize("url", [URL, BROWSE_URL])
    @pytest.mark.parametrize("user,pw", [("reader", "readerpass"),
                                         ("clerk", "clerkpass")])
    def test_non_owner_is_refused(self, client, user, pw, url):
        self._login(client, user, pw)
        assert client.get(url).status_code == 403

    @pytest.mark.parametrize("url", [URL, BROWSE_URL])
    def test_owner_is_allowed(self, client, url):
        self._login(client, "owner", "ownerpass")
        assert client.get(url).status_code == 200


class TestLogsPageSystemCheck:
    """The Logs page's System Check tab renders the preflight.

    ``/logs`` lives in app.py (a stub in the test app), so the real template is
    rendered directly. Clerks can read the logs, but the tab and the debug
    package are owner-only, so they are not offered to a clerk.
    """

    def _render(self, app, role):
        from flask import g, render_template

        with app.test_request_context("/logs"):
            g.current_user = {"role": role}
            return render_template("logs.html", config=app.config)

    def test_owner_sees_tab_and_debug_package(self, app):
        html = self._render(app, "owner")
        assert 'id="check-tab"' in html
        assert URL in html
        assert "/api/admin/debug-package" in html

    def test_clerk_does_not(self, app):
        html = self._render(app, "clerk")
        assert 'id="check-tab"' not in html
        assert URL not in html
        assert "/api/admin/debug-package" not in html


class TestBrowse:
    """``/api/setup/browse``: directories only, under /data and /downloads."""

    @pytest.fixture
    def mounts(self, tmp_path, monkeypatch):
        data = tmp_path / "data"
        downloads = tmp_path / "downloads"
        (data / "Comics" / "Marvel").mkdir(parents=True)
        (data / "manga").mkdir()
        (data / ".hidden").mkdir()
        (data / "_staging").mkdir()
        (data / "loose.cbz").write_bytes(b"x")
        (downloads / "temp").mkdir(parents=True)
        (tmp_path / "outside").mkdir()
        monkeypatch.setattr("core.config.get_data_dir", lambda: str(data))
        monkeypatch.setattr("core.config.get_downloads_dir",
                            lambda: str(downloads))
        # Linux CI has a real mount table; extra volumes are opted into per test.
        monkeypatch.setattr("core.db_health._read_mountinfo",
                            lambda *a, **k: [])
        return {"tmp": tmp_path,
                "data": os.path.realpath(data),
                "downloads": os.path.realpath(downloads)}

    def _get(self, client, path=None):
        query = {} if path is None else {"path": path}
        return client.get(BROWSE_URL, query_string=query)

    def test_no_path_lists_the_roots(self, client, mounts):
        data = self._get(client).get_json()
        assert data["success"] is True
        assert data["current_path"] is None
        assert [d["path"] for d in data["directories"]] == [
            mounts["data"], mounts["downloads"]]
        assert all(r["exists"] for r in data["roots"])
        assert all(r["mounted"] is None for r in data["roots"])

    def test_missing_root_is_reported_not_listed(self, client, mounts):
        import shutil
        shutil.rmtree(mounts["downloads"])
        data = self._get(client).get_json()
        assert [d["path"] for d in data["directories"]] == [mounts["data"]]
        assert {r["path"]: r["exists"] for r in data["roots"]} == {
            mounts["data"]: True, mounts["downloads"]: False}

    def test_lists_visible_directories_only(self, client, mounts):
        resp = self._get(client, mounts["data"])
        assert resp.status_code == 200
        data = resp.get_json()
        assert [d["name"] for d in data["directories"]] == ["Comics", "manga"]
        assert data["directories"][0]["path"] == os.path.join(
            mounts["data"], "Comics")
        assert data["current_path"] == mounts["data"]
        assert data["parent"] is None
        assert data["writable"] is True
        assert data["truncated"] is False

    def test_subfolder_has_a_parent(self, client, mounts):
        sub = os.path.join(mounts["data"], "Comics")
        data = self._get(client, sub).get_json()
        assert data["parent"] == mounts["data"]
        assert [d["name"] for d in data["directories"]] == ["Marvel"]

    def test_downloads_root_is_browsable(self, client, mounts):
        data = self._get(client, mounts["downloads"]).get_json()
        assert [d["name"] for d in data["directories"]] == ["temp"]

    def test_outside_the_mounts_is_refused(self, client, mounts):
        outside = str(mounts["tmp"] / "outside")
        assert self._get(client, outside).status_code == 403

    def test_dotdot_cannot_escape(self, client, mounts):
        escape = os.path.join(mounts["data"], "..", "outside")
        assert self._get(client, escape).status_code == 403

    def test_sibling_with_a_shared_prefix_is_refused(self, client, mounts):
        sibling = mounts["tmp"] / "data-other"
        sibling.mkdir()
        assert self._get(client, str(sibling)).status_code == 403

    def test_symlink_out_of_a_mount_is_refused(self, client, mounts):
        link = os.path.join(mounts["data"], "escape")
        try:
            os.symlink(str(mounts["tmp"] / "outside"), link,
                       target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not available")
        assert self._get(client, link).status_code == 403

    def test_refusal_is_decided_before_existence(self, client, mounts):
        missing_outside = str(mounts["tmp"] / "nope")
        assert self._get(client, missing_outside).status_code == 403

    def test_missing_folder_under_a_mount_is_404(self, client, mounts):
        missing = os.path.join(mounts["data"], "nope")
        assert self._get(client, missing).status_code == 404

    def test_a_file_is_not_a_directory(self, client, mounts):
        f = os.path.join(mounts["data"], "loose.cbz")
        assert self._get(client, f).status_code == 404

    def test_hung_mount_times_out(self, client, mounts, monkeypatch):
        monkeypatch.setattr("core.setup_browse._bounded",
                            lambda fn, timeout: (False, None, None))
        resp = self._get(client, mounts["data"])
        assert resp.status_code == 504
        assert resp.get_json()["success"] is False

    def test_listing_is_capped(self, client, mounts, monkeypatch):
        monkeypatch.setattr("core.setup_browse.MAX_LISTED_DIRECTORIES", 1)
        data = self._get(client, mounts["data"]).get_json()
        assert len(data["directories"]) == 1
        assert data["truncated"] is True


class TestBrowseMounts:
    """Volumes mapped outside /data and /downloads (``/comics``) are roots too."""

    @pytest.fixture
    def mounts(self, tmp_path, monkeypatch):
        for name in ("data", "downloads", "comics/Marvel", "data/nested",
                     "config", "cache"):
            (tmp_path / name).mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("core.config.get_data_dir",
                            lambda: str(tmp_path / "data"))
        monkeypatch.setattr("core.config.get_downloads_dir",
                            lambda: str(tmp_path / "downloads"))
        monkeypatch.setattr("core.config.get_config_dir",
                            lambda: str(tmp_path / "config"))
        monkeypatch.setattr("core.config.get_cache_dir",
                            lambda: str(tmp_path / "cache"))
        table = [
            ("/", "overlay", "overlay"),
            (str(tmp_path / "comics"), "cifs", "//nas/comics"),
            (str(tmp_path / "data" / "nested"), "ext4", "/dev/sdb1"),
            (str(tmp_path / "config"), "ext4", "/dev/sda1"),
            (str(tmp_path / "cache"), "ext4", "/dev/sda1"),
        ]
        monkeypatch.setattr("core.db_health._read_mountinfo",
                            lambda *a, **k: table)
        return {k: os.path.realpath(tmp_path / k)
                for k in ("data", "downloads", "comics", "config")}

    def test_extra_volume_is_a_root(self, client, mounts):
        data = client.get(BROWSE_URL).get_json()
        assert [d["path"] for d in data["directories"]] == [
            mounts["data"], mounts["downloads"], mounts["comics"]]
        mounted = {r["path"]: r["mounted"] for r in data["roots"]}
        assert mounted == {mounts["data"]: True, mounts["downloads"]: False,
                           mounts["comics"]: True}

    def test_extra_volume_is_browsable(self, client, mounts):
        data = client.get(BROWSE_URL,
                          query_string={"path": mounts["comics"]}).get_json()
        assert data["parent"] is None
        assert [d["name"] for d in data["directories"]] == ["Marvel"]

    def test_clu_own_volumes_are_not_roots(self, client, mounts):
        resp = client.get(BROWSE_URL, query_string={"path": mounts["config"]})
        assert resp.status_code == 403


class TestUserMounts:
    """``core.setup_browse.user_mounts`` over a real container's mount table."""

    TABLE = [
        ("/", "overlay", "overlay"),
        ("/proc", "proc", "proc"),
        ("/dev", "tmpfs", "tmpfs"),
        ("/dev/shm", "tmpfs", "shm"),
        ("/sys/fs/cgroup", "cgroup2", "cgroup"),
        ("/etc/hosts", "ext4", "/dev/sda1"),
        ("/tmp/docker.sock", "tmpfs", "tmpfs"),
        ("/app/logs", "ext4", "/dev/sda1"),
        ("/config", "ext4", "/dev/sda1"),
        ("/cache", "ext4", "/dev/sda1"),
        ("/data", "fuse.mergerfs", "mergerfs"),
        ("/downloads", "ext4", "/dev/sdb1"),
        ("/comics", "cifs", "//nas/comics"),
        ("/mnt/manga", "nfs4", "nas:/manga"),
        ("/srv", "binfmt_misc", "binfmt_misc"),
    ]

    def test_keeps_only_user_volumes(self, monkeypatch):
        from core.setup_browse import user_mounts

        monkeypatch.setattr("core.config.get_config_dir", lambda: "/config")
        monkeypatch.setattr("core.config.get_cache_dir", lambda: "/cache")
        assert user_mounts(self.TABLE) == [
            "/comics", "/data", "/downloads", "/mnt/manga"]

    def test_no_mount_table(self):
        from core.setup_browse import user_mounts

        assert user_mounts([]) == []
