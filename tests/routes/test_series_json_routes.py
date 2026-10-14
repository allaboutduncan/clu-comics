"""POST /api/series-json/refresh -- explicit create/update of series.json (#618)."""
from unittest.mock import patch

OK = {"success": True, "provider": "comicvine", "description_from": "comicvine", "tried": ["comicvine"]}


def _post(client, **body):
    return client.post("/api/series-json/refresh", json=body)


class TestRefreshSeriesJsonRoute:
    def test_requires_path(self, client):
        assert _post(client).status_code == 400

    def test_rejects_unknown_provider(self, client, tmp_path):
        assert _post(client, path=str(tmp_path), provider="gcd").status_code == 400

    @patch("routes.metadata.is_valid_library_path", return_value=False)
    def test_path_outside_a_library_is_refused(self, _valid, client, tmp_path):
        r = _post(client, path=str(tmp_path))
        assert r.status_code == 400
        assert "library" in r.get_json()["error"]

    @patch("core.auth.file_op_refusal", return_value=("Forbidden", 403))
    def test_folder_grant_is_enforced(self, _refuse, client, tmp_path):
        assert _post(client, path=str(tmp_path)).status_code == 403

    @patch("routes.metadata.is_valid_library_path", return_value=True)
    def test_missing_folder(self, _valid, client, tmp_path):
        assert _post(client, path=str(tmp_path / "nope")).status_code == 404

    @patch("routes.metadata._is_oneshot_folder_safe", return_value=True)
    @patch("routes.metadata.is_valid_library_path", return_value=True)
    def test_oneshot_folder_is_refused(self, _valid, _one, client, tmp_path):
        assert _post(client, path=str(tmp_path)).status_code == 400

    @patch("core.series_json_builder.refresh_series_json", return_value=OK)
    @patch("routes.metadata._is_oneshot_folder_safe", return_value=False)
    @patch("routes.metadata.is_valid_library_path", return_value=True)
    def test_success_passes_provider_and_force(self, _valid, _one, refresh, client, tmp_path):
        r = _post(client, path=str(tmp_path), provider="comicvine", force=True)
        assert r.status_code == 200
        assert r.get_json()["provider"] == "comicvine"
        kwargs = refresh.call_args.kwargs
        assert kwargs["provider"] == "comicvine" and kwargs["force"] is True

    @patch("core.series_json_builder.refresh_series_json",
           return_value={"success": False, "error": "No provider returned data", "tried": []})
    @patch("routes.metadata._is_oneshot_folder_safe", return_value=False)
    @patch("routes.metadata.is_valid_library_path", return_value=True)
    def test_provider_failure_is_422(self, _valid, _one, _refresh, client, tmp_path):
        r = _post(client, path=str(tmp_path))
        assert r.status_code == 422
        assert r.get_json()["success"] is False
