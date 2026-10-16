"""Source Wall: the Provider column and the "tagged by <provider>" view."""
import pathlib
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


class TestDefaultColumns:

    def test_default_includes_provider(self, client):
        with patch("routes.source_wall.get_user_preference", side_effect=lambda k, d: d):
            data = client.get("/api/source-wall/columns").get_json()
        assert data["columns"] == ["name", "ci_provider", "ci_volume"]

    def test_provider_is_display_only(self, client):
        """It is derived from Notes, so it must never be an editable field."""
        from routes.source_wall import CI_FIELD_TO_XML
        assert "ci_provider" not in CI_FIELD_TO_XML
        with patch("routes.source_wall.is_valid_library_path", return_value=True):
            r = client.post("/api/source-wall/save-pending", json={
                "updates": {"/data/a.cbz": {"ci_provider": "metron"}}})
        assert r.status_code == 400

    def test_listing_selects_the_column(self):
        import inspect
        from core.database import get_source_wall_files
        assert "ci_provider" in inspect.getsource(get_source_wall_files)


class TestProviderCounts:

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_returns_mix(self, _valid, client):
        mix = {"providers": {"metron": 3}, "untagged": 1, "no_xml": 0, "unscanned": 0, "total": 4}
        with patch("routes.source_wall.get_provider_mix", return_value=mix):
            data = client.get("/api/source-wall/providers?path=/data").get_json()
        assert data["success"] and data["mix"]["providers"] == {"metron": 3}
        assert data["mix"]["untagged"] == 1

    def test_invalid_path_403(self, client):
        assert client.get("/api/source-wall/providers?path=/etc").status_code == 403

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_read_failure_is_500_not_empty(self, _valid, client):
        with patch("routes.source_wall.get_provider_mix", return_value=None):
            assert client.get("/api/source-wall/providers?path=/data").status_code == 500

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_folder_scoped_user_only_counts_their_grants(self, _valid, client):
        calls = []

        def fake_mix(scope, recursive=True):
            calls.append(scope)
            return {"providers": {"metron": 1}, "untagged": 0, "no_xml": 0,
                    "unscanned": 0, "total": 1}

        with patch("routes.source_wall.accessible_folder_prefixes",
                   return_value=["/data/DC", "/elsewhere/X"]), \
             patch("routes.source_wall.get_provider_mix", side_effect=fake_mix):
            data = client.get("/api/source-wall/providers?path=/data").get_json()
        assert calls == ["/data/DC"]
        assert data["mix"]["providers"] == {"metron": 1}


class TestByProvider:

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_lists_files_with_paging_info(self, _valid, client):
        files = [{"name": "a.cbz", "path": "/data/X/a.cbz", "parent": "/data/X",
                  "has_comicinfo": 1, "ci_provider": "metron"}]
        with patch("routes.source_wall.get_source_wall_files_by_provider",
                   return_value=(files, 1)) as q:
            data = client.get("/api/source-wall/by-provider?path=/data&provider=metron").get_json()
        assert data["success"] and data["total"] == 1 and data["files"] == files
        assert q.call_args.args[0] == ["/data"] and q.call_args.args[1] == "metron"

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_provider_required(self, _valid, client):
        assert client.get("/api/source-wall/by-provider?path=/data").status_code == 400

    def test_invalid_path_403(self, client):
        assert client.get("/api/source-wall/by-provider?path=/etc&provider=metron").status_code == 403

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_read_failure_is_500_not_empty(self, _valid, client):
        with patch("routes.source_wall.get_source_wall_files_by_provider",
                   return_value=(None, 0)):
            r = client.get("/api/source-wall/by-provider?path=/data&provider=metron")
        assert r.status_code == 500

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_files_outside_a_grant_are_dropped(self, _valid, client):
        files = [{"path": "/data/DC/a.cbz"}, {"path": "/data/Secret/b.cbz"}]
        with patch("routes.source_wall.get_source_wall_files_by_provider",
                   return_value=(files, 2)), \
             patch("routes.source_wall.filter_paths_for_user",
                   side_effect=lambda u, items, key=None, allow_traverse=False:
                   [i for i in items if i[key].startswith("/data/DC")]):
            data = client.get("/api/source-wall/by-provider?path=/data&provider=metron").get_json()
        assert [f["path"] for f in data["files"]] == ["/data/DC/a.cbz"]


class TestFolderScope:
    """The "This folder" narrowing: intersected with grants, never trusted."""

    def test_scopes_unrestricted(self, app):
        from routes.source_wall import _provider_scopes
        with app.test_request_context(), \
             patch("routes.source_wall.accessible_folder_prefixes", return_value=None):
            assert _provider_scopes("/data") == ["/data"]
            assert _provider_scopes("/data", "/data/DC") == ["/data/DC"]

    def test_scopes_restricted_intersect_grants(self, app):
        from routes.source_wall import _provider_scopes
        grants = ["/data/DC", "/data/Marvel/X-Men"]
        with app.test_request_context(), \
             patch("routes.source_wall.accessible_folder_prefixes", return_value=grants):
            # Folder above a grant -> the grant; inside a grant -> the folder;
            # unrelated, or a sibling sharing a name prefix -> nothing.
            assert _provider_scopes("/data", "/data/Marvel") == ["/data/Marvel/X-Men"]
            assert _provider_scopes("/data", "/data/DC/Batman") == ["/data/DC/Batman"]
            assert _provider_scopes("/data", "/data/Image") == []
            assert _provider_scopes("/data", "/data/DCX") == []

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_by_provider_passes_folder_through(self, _valid, client):
        with patch("routes.source_wall.get_source_wall_files_by_provider",
                   return_value=([], 0)) as q:
            r = client.get("/api/source-wall/by-provider"
                           "?path=/data&provider=metron&folder=/data/DC")
        assert r.status_code == 200 and q.call_args.args[0] == ["/data/DC"]

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_providers_passes_folder_through(self, _valid, client):
        seen = []
        mix = {"providers": {}, "untagged": 0, "no_xml": 0, "unscanned": 0, "total": 0}
        with patch("routes.source_wall.get_provider_mix",
                   side_effect=lambda scope, recursive=True: seen.append(scope) or mix):
            r = client.get("/api/source-wall/providers?path=/data&folder=/data/DC")
        assert r.status_code == 200 and seen == ["/data/DC"]

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_folder_outside_the_library_is_refused(self, _valid, client):
        for url in ("/api/source-wall/by-provider?path=/data&provider=metron&folder=/other",
                    "/api/source-wall/providers?path=/data&folder=/data/../other"):
            assert client.get(url).status_code == 403

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_folder_equal_to_library_means_whole_library(self, _valid, client):
        with patch("routes.source_wall.get_source_wall_files_by_provider",
                   return_value=([], 0)) as q:
            client.get("/api/source-wall/by-provider?path=/data&provider=metron&folder=/data/")
        assert q.call_args.args[0] == ["/data"]

    def test_page_has_the_scope_toggle(self):
        html = _read("templates/source_wall.html")
        assert 'id="swScopeFolder"' in html and 'id="swScopeLibrary"' in html
        js = _read("static/js/source_wall.js")
        assert "setProviderScope" in js and "swScopeQuery()" in js
        # Both provider fetches carry the scope, or the toggle does nothing.
        assert js.count("swScopeQuery()") >= 3


class TestProviderQuery:

    def _add(self, path, provider, ci=1):
        from core.database import add_file_index_entry
        add_file_index_entry(name=path.rsplit("/", 1)[-1], path=path, entry_type="file",
                             size=1, parent=path.rsplit("/", 1)[0])
        self.db.execute("UPDATE file_index SET ci_provider=?, has_comicinfo=? WHERE path=?",
                        (provider, ci, path))
        self.db.commit()

    def test_by_provider_across_folders_with_paging(self, db_connection):
        from core.database import get_source_wall_files_by_provider
        self.db = db_connection
        for p, prov in [("/data/A/1.cbz", "metron"), ("/data/B/2.cbz", "metron"),
                        ("/data/B/3.cbz", "comicvine"), ("/data/C/4.cbz", ""),
                        ("/data/Z/5.cbz", "metron")]:
            self._add(p, prov)
        # /data/AB must not match a /data/A scope.
        self._add("/data/AB/6.cbz", "metron")

        files, total = get_source_wall_files_by_provider(["/data/A", "/data/B"], "metron")
        assert total == 2 and [f["path"] for f in files] == ["/data/A/1.cbz", "/data/B/2.cbz"]
        assert files[0]["parent"] == "/data/A" and files[0]["ci_provider"] == "metron"

        page, total = get_source_wall_files_by_provider(["/data"], "metron", limit=2, offset=2)
        assert total == 4 and len(page) == 2

        untagged, _ = get_source_wall_files_by_provider(["/data"], "untagged")
        assert [f["path"] for f in untagged] == ["/data/C/4.cbz"]

    def test_no_scopes_is_empty_not_error(self, db_connection):
        from core.database import get_source_wall_files_by_provider
        assert get_source_wall_files_by_provider([], "metron") == ([], 0)


class TestSourceWallWiring:

    def test_page_loads_retag_machinery(self):
        html = _read("templates/source_wall.html")
        for needle in ("modal_metadata_tools.html", "modal_bulk_metadata_review.html",
                       "clu-metadata-tools.js", "bulk-metadata-review.js",
                       'id="swViewSelect"', 'id="swLoadMore"', "retagSelectedSW"):
            assert needle in html, needle

    def test_row_dropdown_and_column(self):
        js = _read("static/js/source_wall.js")
        assert 'data-action="retag"' in js
        assert "CLU.openRetagFilesModal" in js
        assert "ci_provider:" in js
        assert "/api/source-wall/by-provider" in js and "/api/source-wall/providers" in js

    def test_reloads_keep_the_provider_view(self):
        js = _read("static/js/source_wall.js")
        body = js.split("function reloadCurrentView()")[1].split("function retagFilesSW")[0]
        assert "loadProviderView" in body and "loadPath" in body
        # Every other post-action reload goes through it, or a delete in the
        # provider view would silently drop back to the folder tree.
        outside = js.replace(body, "")
        assert "loadPath(swCurrentPath)" not in outside.split("// ── CLU Module Contracts ──")[1]

    def test_open_retag_files_modal_exists(self):
        assert "CLU.openRetagFilesModal" in _read("static/js/clu-metadata-tools.js")
