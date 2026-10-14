"""GET /api/folder-sidecars -- read-only view of a folder's cvinfo and series.json."""
from unittest.mock import patch


def _get(client, path=None):
    url = "/api/folder-sidecars"
    if path is not None:
        url += "?path=" + str(path)
    return client.get(url)


class TestFolderSidecarsRoute:
    def test_requires_path(self, client):
        assert _get(client).status_code == 400

    @patch("routes.metadata.is_valid_library_path", return_value=False)
    def test_path_outside_a_library_is_refused(self, _valid, client, tmp_path):
        assert _get(client, tmp_path).status_code == 400

    @patch("core.auth.file_op_refusal", return_value=("Forbidden", 403))
    def test_folder_grant_is_enforced(self, _refuse, client, tmp_path):
        assert _get(client, tmp_path).status_code == 403

    @patch("routes.metadata.is_valid_library_path", return_value=True)
    def test_missing_folder(self, _valid, client, tmp_path):
        assert _get(client, tmp_path / "nope").status_code == 404

    @patch("routes.metadata.is_valid_library_path", return_value=True)
    def test_returns_both_files(self, _valid, client, tmp_path):
        (tmp_path / "cvinfo").write_text("https://comicvine.gamespot.com/x/4050-1/\n")
        (tmp_path / "series.json").write_text('{"metadata": {"name": "X"}}')
        data = _get(client, tmp_path).get_json()
        assert data["success"] is True
        assert "4050-1" in data["files"]["cvinfo"]["content"]
        assert '"name": "X"' in data["files"]["series.json"]["content"]

    @patch("routes.metadata.is_valid_library_path", return_value=True)
    def test_absent_files_are_not_an_error(self, _valid, client, tmp_path):
        (tmp_path / "cvinfo").write_text("x")
        data = _get(client, tmp_path).get_json()
        assert data["files"]["cvinfo"]["exists"] is True
        assert data["files"]["series.json"] == {"exists": False, "content": ""}

    @patch("routes.metadata.is_valid_library_path", return_value=True)
    def test_oversized_file_is_truncated(self, _valid, client, tmp_path):
        from routes.metadata import SIDECAR_VIEW_MAX_BYTES
        (tmp_path / "series.json").write_text("a" * (SIDECAR_VIEW_MAX_BYTES + 10))
        info = _get(client, tmp_path).get_json()["files"]["series.json"]
        assert info["truncated"] is True
        assert len(info["content"]) == SIDECAR_VIEW_MAX_BYTES


class TestListingShowsSeriesJson:
    def test_series_json_is_allowlisted_in_directory_listing(self):
        # app.py cannot be imported in tests, so assert the allowlist structurally.
        import pathlib
        src = pathlib.Path(__file__).resolve().parents[2].joinpath("app.py").read_text(encoding="utf-8")
        assert 'allowed_files = {"missing.txt", "cvinfo", "series.json"}' in src
