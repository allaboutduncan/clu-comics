"""Tests for routes/files.py -- file operations endpoints."""
import os
import pytest
from unittest.mock import patch, MagicMock


class TestMove:

    def test_missing_params(self, client):
        resp = client.post("/move", json={"source": "/a"})
        assert resp.status_code == 400

    def test_source_not_exists(self, client):
        resp = client.post("/move",
                           json={"source": "/nonexistent", "destination": "/dest"})
        assert resp.status_code == 404

    @patch("routes.files.is_critical_path", return_value=True)
    @patch("routes.files.get_critical_path_error_message", return_value="Protected")
    def test_move_critical_source(self, mock_msg, mock_crit, client, tmp_path):
        src = tmp_path / "file.cbz"
        src.write_bytes(b"fake")
        resp = client.post("/move",
                           json={"source": str(src), "destination": "/dest"})
        assert resp.status_code == 403

    def test_move_dir_into_itself(self, client, tmp_path):
        src = tmp_path / "dir"
        src.mkdir()
        dest = str(src / "subdir")
        resp = client.post("/move",
                           json={"source": str(src), "destination": dest})
        assert resp.status_code == 400

    @patch("routes.files.is_critical_path", return_value=False)
    def test_move_file_success(self, mock_crit, client, tmp_path):
        src = tmp_path / "comic.cbz"
        src.write_bytes(b"comic data")
        dest = str(tmp_path / "moved.cbz")

        mock_app = MagicMock()
        mock_app.auto_fetch_metron_metadata.return_value = dest
        mock_app.auto_fetch_comicvine_metadata.return_value = dest
        mock_app.log_file_if_in_data = MagicMock()
        mock_app.update_index_on_move = MagicMock()

        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/move",
                               json={"source": str(src), "destination": dest})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert os.path.exists(dest)
        assert not os.path.exists(str(src))


class TestFolderSize:

    def test_valid_path(self, client, tmp_path):
        d = tmp_path / "comics"
        d.mkdir()
        (d / "a.cbz").write_bytes(b"x" * 100)
        (d / "b.pdf").write_bytes(b"y" * 200)

        resp = client.get(f"/folder-size?path={d}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["size"] == 300
        assert data["comic_count"] == 1
        assert data["magazine_count"] == 1

    def test_invalid_path(self, client):
        resp = client.get("/folder-size?path=/nonexistent")
        assert resp.status_code == 400


class TestRename:

    @patch("routes.files.is_critical_path", return_value=False)
    def test_rename_success(self, mock_crit, client, tmp_path):
        old = tmp_path / "old.cbz"
        old.write_bytes(b"data")
        new = str(tmp_path / "new.cbz")

        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/rename",
                               json={"old": str(old), "new": new})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_rename_missing_params(self, client):
        resp = client.post("/rename", json={"old": "/a"})
        assert resp.status_code == 400

    def test_rename_source_not_exists(self, client):
        resp = client.post("/rename",
                           json={"old": "/nonexistent", "new": "/new"})
        assert resp.status_code == 404

    @patch("routes.files.is_critical_path", return_value=True)
    @patch("routes.files.get_critical_path_error_message", return_value="Nope")
    def test_rename_critical(self, mock_msg, mock_crit, client, tmp_path):
        f = tmp_path / "file.cbz"
        f.write_bytes(b"data")
        resp = client.post("/rename",
                           json={"old": str(f), "new": "/new.cbz"})
        assert resp.status_code == 403


class TestCustomRename:

    @patch("routes.files.is_critical_path", return_value=False)
    def test_custom_rename(self, mock_crit, client, tmp_path):
        old = tmp_path / "Comic (2020) (Digital).cbz"
        old.write_bytes(b"data")
        new = str(tmp_path / "Comic (2020).cbz")

        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/custom-rename",
                               json={"old": str(old), "new": new})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_dest_exists(self, client, tmp_path):
        old = tmp_path / "old.cbz"
        old.write_bytes(b"data")
        new = tmp_path / "existing.cbz"
        new.write_bytes(b"other")

        with patch("routes.files.is_critical_path", return_value=False):
            resp = client.post("/custom-rename",
                               json={"old": str(old), "new": str(new)})
        assert resp.status_code == 400


class TestDelete:

    @patch("routes.files.is_critical_path", return_value=False)
    def test_delete_file(self, mock_crit, client, tmp_path):
        f = tmp_path / "delete_me.cbz"
        f.write_bytes(b"data")

        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/delete", json={"target": str(f)})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert not os.path.exists(str(f))

    def test_delete_missing_target(self, client):
        resp = client.post("/delete", json={})
        assert resp.status_code == 400

    def test_delete_nonexistent(self, client):
        resp = client.post("/delete", json={"target": "/nonexistent"})
        assert resp.status_code == 404

    @patch("routes.files.is_critical_path", return_value=True)
    @patch("routes.files.get_critical_path_error_message", return_value="No")
    def test_delete_critical(self, mock_msg, mock_crit, client, tmp_path):
        f = tmp_path / "critical.cbz"
        f.write_bytes(b"data")
        resp = client.post("/delete", json={"target": str(f)})
        assert resp.status_code == 403


class TestDeleteMultiple:

    @patch("routes.files.is_critical_path", return_value=False)
    def test_bulk_delete(self, mock_crit, client, tmp_path):
        f1 = tmp_path / "a.cbz"
        f2 = tmp_path / "b.cbz"
        f1.write_bytes(b"data")
        f2.write_bytes(b"data")

        with patch("database.delete_file_index_entries"):
            resp = client.post("/api/delete-multiple",
                               json={"targets": [str(f1), str(f2)]})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert all(r["success"] for r in data["results"])

    def test_empty_targets(self, client):
        resp = client.post("/api/delete-multiple", json={"targets": []})
        assert resp.status_code == 400


class TestCreateFolder:

    @patch("routes.files.is_critical_path", return_value=False)
    def test_create_folder(self, mock_crit, client, tmp_path):
        new_dir = str(tmp_path / "new_folder")
        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/create-folder", json={"path": new_dir})
        assert resp.status_code == 200
        assert os.path.isdir(new_dir)

    def test_no_path(self, client):
        resp = client.post("/create-folder", json={})
        assert resp.status_code == 400


class TestCombineCbz:

    def test_too_few_files(self, client):
        resp = client.post("/api/combine-cbz",
                           json={"files": ["/a.cbz"], "directory": "/tmp"})
        assert resp.status_code == 400

    def test_no_directory(self, client):
        resp = client.post("/api/combine-cbz",
                           json={"files": ["/a.cbz", "/b.cbz"]})
        assert resp.status_code == 400

    @patch("routes.files.is_valid_library_path", return_value=True)
    @patch("routes.files.config")
    def test_combine_success(self, mock_config, mock_valid, client, create_cbz, tmp_path):
        mock_config.get.return_value = str(tmp_path)
        cbz1 = create_cbz("part1.cbz", num_images=2)
        cbz2 = create_cbz("part2.cbz", num_images=2)

        resp = client.post("/api/combine-cbz", json={
            "files": [cbz1, cbz2],
            "output_name": "Combined",
            "directory": str(tmp_path),
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["total_images"] == 4


class TestCrop:

    def test_missing_params(self, client):
        resp = client.post("/crop", json={})
        assert resp.status_code == 400

    def test_invalid_crop_type(self, client):
        resp = client.post("/crop",
                           json={"target": "/img.jpg", "cropType": "invalid"})
        assert resp.status_code == 400


class TestGetImageData:

    def test_missing_path(self, client):
        resp = client.post("/get-image-data", json={})
        assert resp.status_code == 400

    def test_file_not_found(self, client):
        resp = client.post("/get-image-data", json={"target": "/nonexistent.jpg"})
        assert resp.status_code == 404

    def test_valid_image(self, client, tmp_path):
        from PIL import Image
        img_path = str(tmp_path / "test.jpg")
        Image.new("RGB", (10, 10), "blue").save(img_path)

        resp = client.post("/get-image-data", json={"target": img_path})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["imageData"].startswith("data:image/jpeg")
