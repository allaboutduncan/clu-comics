"""Tests for routes/collection.py -- collection browse and search endpoints."""
import os
import pytest
from unittest.mock import patch, MagicMock


class TestFilesPage:

    @patch("routes.collection.config")
    def test_files_page(self, mock_config, client):
        mock_config.get.side_effect = lambda s, k, fallback="": fallback
        resp = client.get("/files")
        assert resp.status_code == 200


class TestCollectionPage:

    @patch("routes.collection.get_dashboard_sections", return_value=[])
    @patch("routes.collection.config")
    def test_collection_root(self, mock_config, mock_sections, client):
        mock_config.get.return_value = "True"
        resp = client.get("/collection")
        assert resp.status_code == 200

    @patch("routes.collection.get_dashboard_sections", return_value=[])
    @patch("routes.collection.config")
    def test_collection_with_subpath(self, mock_config, mock_sections, client):
        mock_config.get.return_value = "True"
        resp = client.get("/collection/DC%20Comics/Batman")
        assert resp.status_code == 200


class TestToReadPage:

    def test_to_read_page(self, client):
        resp = client.get("/to-read")
        assert resp.status_code == 200


class TestApiBrowse:

    @patch("routes.collection.get_directory_children")
    def test_browse_root(self, mock_children, client, app, tmp_path):
        data_dir = str(tmp_path / "data")
        mock_children.return_value = ([], [])

        with patch.dict("sys.modules", {"app": MagicMock(DATA_DIR=data_dir)}):
            resp = client.get("/api/browse")

        assert resp.status_code == 200
        data = resp.get_json()
        assert "directories" in data
        assert "files" in data

    @patch("routes.collection.get_directory_children")
    def test_browse_with_path(self, mock_children, client, tmp_path):
        path = str(tmp_path / "data")
        os.makedirs(path, exist_ok=True)
        mock_children.return_value = (
            [{"name": "DC Comics", "path": os.path.join(path, "DC Comics"),
              "has_thumbnail": False}],
            [{"name": "comic.cbz", "path": os.path.join(path, "comic.cbz"),
              "size": 1000, "has_comicinfo": True}],
        )

        with patch.dict("sys.modules", {"app": MagicMock(DATA_DIR=path)}):
            resp = client.get(f"/api/browse?path={path}")

        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["directories"]) == 1
        assert len(data["files"]) == 1

    @patch("routes.collection.get_directory_children",
           side_effect=Exception("DB error"))
    def test_browse_error(self, mock_children, client, tmp_path):
        with patch.dict("sys.modules", {"app": MagicMock(DATA_DIR=str(tmp_path))}):
            resp = client.get("/api/browse")
        assert resp.status_code == 500


class TestApiMissingXml:

    @patch("core.database.get_files_missing_comicinfo",
           return_value=[{"name": "x.cbz", "path": "/data/x.cbz",
                          "size": 100, "has_comicinfo": False,
                          "has_thumbnail": False}])
    def test_missing_xml(self, mock_fn, client):
        resp = client.get("/api/missing-xml")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] >= 0


class TestApiIssuesReadPaths:

    @patch("core.database.get_issues_read", return_value=[
        {"issue_path": "/data/Batman.cbz"},
    ])
    def test_issues_read_paths(self, mock_read, client):
        resp = client.get("/api/issues-read-paths")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "paths" in data


class TestSearchFiles:

    @patch("routes.collection.search_file_index", return_value=[
        {"name": "Batman 001.cbz", "path": "/data/Batman 001.cbz", "type": "file"},
    ])
    def test_search_files(self, mock_search, client):
        with patch.dict("sys.modules", {"app": MagicMock(index_built=True)}):
            resp = client.get("/search-files?query=batman")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["results"]) == 1

    def test_search_empty_query(self, client):
        with patch.dict("sys.modules", {"app": MagicMock(index_built=True)}):
            resp = client.get("/search-files?query=")
        assert resp.status_code == 400

    def test_search_too_short(self, client):
        with patch.dict("sys.modules", {"app": MagicMock(index_built=True)}):
            resp = client.get("/search-files?query=a")
        assert resp.status_code == 400


class TestCountFiles:

    def test_count_files(self, client, tmp_path):
        # Create some files in the tmp dir
        d = tmp_path / "comics"
        d.mkdir()
        (d / "a.cbz").write_bytes(b"fake")
        (d / "b.cbz").write_bytes(b"fake")

        resp = client.get(f"/count-files?path={d}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["file_count"] == 2

    def test_count_invalid_path(self, client):
        resp = client.get("/count-files?path=/nonexistent/path")
        assert resp.status_code == 400


class TestApiBrowseMetadata:

    @patch("routes.collection.get_path_counts_batch", return_value={
        "/data/DC": (5, 20),
    })
    def test_browse_metadata(self, mock_counts, client):
        resp = client.post("/api/browse-metadata",
                           json={"paths": ["/data/DC"]})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "/data/DC" in data["metadata"]
        assert data["metadata"]["/data/DC"]["folder_count"] == 5

    def test_no_paths(self, client):
        resp = client.post("/api/browse-metadata", json={"paths": []})
        assert resp.status_code == 400

    def test_too_many_paths(self, client):
        resp = client.post("/api/browse-metadata",
                           json={"paths": [f"/data/{i}" for i in range(101)]})
        assert resp.status_code == 400

    @patch("routes.collection.get_path_counts_batch", return_value={})
    def test_returns_zero_counts_for_missing_paths(self, mock_counts, client):
        """Every requested path must come back keyed in the response, even
        when the DB layer returns nothing — otherwise the frontend has no
        way to distinguish 'still loading' from 'no data'."""
        paths = ["/data/Marvel", "/data/DC", "/data/Image"]
        resp = client.post("/api/browse-metadata", json={"paths": paths})
        assert resp.status_code == 200
        data = resp.get_json()
        for p in paths:
            assert p in data["metadata"]
            assert data["metadata"][p] == {
                "folder_count": 0,
                "file_count": 0,
                "has_files": False,
            }

    @patch("routes.collection.get_path_counts_batch",
           side_effect=RuntimeError("db blew up"))
    def test_logs_input_paths_on_error(self, mock_counts, client, caplog):
        """A 500 response must log the failing input paths so on-call can
        correlate browser console errors with server logs."""
        import logging
        paths = ["/data/Marvel", "/data/DC"]
        with caplog.at_level(logging.ERROR, logger="app_logger"):
            resp = client.post("/api/browse-metadata", json={"paths": paths})
        assert resp.status_code == 500
        assert any("/data/Marvel" in rec.message for rec in caplog.records), \
            f"Expected error log to mention input path; got: {[r.message for r in caplog.records]}"


class TestApiClearBrowseCache:

    @patch("routes.collection.invalidate_browse_cache")
    def test_clear_specific_path(self, mock_inv, client):
        with patch.dict("sys.modules", {"app": MagicMock()}):
            resp = client.post("/api/clear-browse-cache",
                               json={"path": "/data/DC"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_clear_all(self, client):
        mock_clear = MagicMock()
        with patch.dict("sys.modules", {"app": MagicMock(clear_browse_cache=mock_clear)}):
            resp = client.post("/api/clear-browse-cache", json={})
        assert resp.status_code == 200


class TestListRecentFiles:

    @patch("routes.collection.get_recent_files", return_value=[
        {"name": "Batman.cbz", "path": "/data/Batman.cbz", "added_at": "2024-01-01"},
    ])
    def test_list_recent(self, mock_recent, client):
        resp = client.get("/list-recent-files")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["total_count"] == 1

    @patch("routes.collection.get_recent_files", return_value=[])
    def test_list_recent_empty(self, mock_recent, client):
        resp = client.get("/list-recent-files")
        data = resp.get_json()
        assert data["total_count"] == 0
        assert data["date_range"] is None


class TestFolderThumbnail:

    def test_missing_path(self, client):
        resp = client.get("/api/folder-thumbnail")
        assert resp.status_code == 200  # Returns error.svg

    def test_nonexistent_path(self, client):
        resp = client.get("/api/folder-thumbnail?path=/nonexistent/image.png")
        assert resp.status_code == 200  # Returns error.svg

    def test_valid_image(self, client, tmp_path):
        from PIL import Image
        img_path = tmp_path / "folder.png"
        Image.new("RGB", (10, 10), "red").save(str(img_path))

        resp = client.get(f"/api/folder-thumbnail?path={img_path}")
        assert resp.status_code == 200
        assert resp.content_type == "image/png"


class TestCbzPreview:

    def test_invalid_path(self, client):
        resp = client.get("/cbz-preview?path=/nonexistent.cbz")
        assert resp.status_code == 400

    def test_non_cbz_file(self, client, tmp_path):
        txt = tmp_path / "test.txt"
        txt.write_text("hello")
        resp = client.get(f"/cbz-preview?path={txt}")
        assert resp.status_code == 400

    def test_valid_cbz(self, client, create_cbz):
        cbz_path = create_cbz("preview.cbz", num_images=2)
        resp = client.get(f"/cbz-preview?path={cbz_path}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["total_images"] == 2
        assert "preview" in data


class TestApiOnTheStack:

    @patch("core.database.get_on_the_stack_items", return_value=[])
    def test_empty_response(self, mock_items, client):
        resp = client.get("/api/on-the-stack")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["items"] == []
        assert data["total_count"] == 0

    @patch("core.database.get_on_the_stack_items")
    def test_with_items(self, mock_items, client):
        mock_items.return_value = [{
            "series_id": 100,
            "series_name": "Absolute Batman",
            "issue_number": "4",
            "file_path": "/data/DC/Absolute Batman/Absolute Batman 004.cbz",
            "file_name": "Absolute Batman 004.cbz",
            "cover_image": "https://example.com/cover.jpg",
            "last_read_at": "2024-12-01 10:00:00",
            "series_status": "Ongoing",
        }]
        resp = client.get("/api/on-the-stack")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["items"]) == 1
        assert data["items"][0]["series_name"] == "Absolute Batman"

    @patch("core.database.get_on_the_stack_items")
    def test_limit_param(self, mock_items, client):
        mock_items.return_value = []
        resp = client.get("/api/on-the-stack?limit=5")
        assert resp.status_code == 200
        mock_items.assert_called_once_with(limit=5)


# =============================================================================
# /api/browse-recursive (All Books) — server-side pagination
# =============================================================================

def _seed_file_index(data_dir, files):
    """
    Seed file_index with a list of (filename, ci_series, ci_year[, ci_number])
    tuples under ``data_dir``.  Returns the list of full paths inserted.

    Uses a fresh connection for each ci_* UPDATE so we don't hold a write
    transaction across multiple add_file_index_entry calls (which would
    otherwise produce SQLite "database is locked" errors).
    """
    from core.database import add_file_index_entry, get_db_connection

    paths = []
    for entry in files:
        if len(entry) == 3:
            name, ci_series, ci_year = entry
            ci_number = None
        else:
            name, ci_series, ci_year, ci_number = entry
        full_path = os.path.join(data_dir, name)
        ok = add_file_index_entry(
            name=name,
            path=full_path,
            entry_type="file",
            size=1234,
            parent=data_dir,
            modified_at=1_700_000_000.0,
        )
        assert ok, f"failed to seed {full_path}"
        if ci_series is not None or ci_year is not None or ci_number is not None:
            conn = get_db_connection()
            try:
                conn.execute(
                    "UPDATE file_index SET ci_series=?, ci_year=?, ci_number=? WHERE path=?",
                    (ci_series, ci_year, ci_number, full_path),
                )
                conn.commit()
            finally:
                conn.close()
        paths.append(full_path)
    return paths


class TestBrowseRecursivePagination:
    """Server-side paginated /api/browse-recursive."""

    def test_default_response_shape(self, client, app, db_connection):
        data_dir = app.config["DATA_DIR"]
        _seed_file_index(data_dir, [
            ("Avengers 001 (2018).cbz", "Avengers", "2018", "1"),
        ])

        resp = client.get(f"/api/browse-recursive?path={data_dir}")
        assert resp.status_code == 200
        data = resp.get_json()
        # Required envelope keys
        for key in ("current_path", "files", "total", "offset", "limit", "letters"):
            assert key in data, f"missing {key}"
        assert data["total"] == 1
        assert data["offset"] == 0
        assert data["limit"] == 21
        assert len(data["files"]) == 1

        f = data["files"][0]
        assert f["name"] == "Avengers 001 (2018).cbz"
        assert f["size"] == 1234
        assert f["has_thumbnail"] is True
        assert "thumbnail_url" in f
        assert "has_comicinfo" in f

    def test_offset_and_limit(self, client, app, db_connection):
        data_dir = app.config["DATA_DIR"]
        rows = [(f"Series{i:02d} 001 (2020).cbz", f"Series {i:02d}", "2020", "1")
                for i in range(25)]
        _seed_file_index(data_dir, rows)

        resp1 = client.get(
            f"/api/browse-recursive?path={data_dir}&offset=0&limit=10"
        )
        resp2 = client.get(
            f"/api/browse-recursive?path={data_dir}&offset=10&limit=10"
        )
        resp3 = client.get(
            f"/api/browse-recursive?path={data_dir}&offset=20&limit=10"
        )

        for r in (resp1, resp2, resp3):
            assert r.status_code == 200
            assert r.get_json()["total"] == 25

        names1 = [f["name"] for f in resp1.get_json()["files"]]
        names2 = [f["name"] for f in resp2.get_json()["files"]]
        names3 = [f["name"] for f in resp3.get_json()["files"]]

        assert len(names1) == 10
        assert len(names2) == 10
        assert len(names3) == 5  # last partial page

        # No overlap, full coverage when concatenated
        assert set(names1).isdisjoint(set(names2))
        assert set(names2).isdisjoint(set(names3))
        assert len(set(names1 + names2 + names3)) == 25

    def test_sort_is_stable_across_pages(self, client, app, db_connection):
        """Concat of paged results must equal the full sorted list."""
        data_dir = app.config["DATA_DIR"]
        rows = [
            ("Zatanna 001.cbz", "Zatanna", "2010", "1"),
            ("Avengers 010.cbz", "Avengers", "2018", "10"),
            ("Avengers 002.cbz", "Avengers", "2018", "2"),
            ("Avengers 001.cbz", "Avengers", "2018", "1"),
            ("Batman 003.cbz", "Batman", "2016", "3"),
            ("Batman 001.cbz", "Batman", "2016", "1"),
        ]
        _seed_file_index(data_dir, rows)

        # Full unpaginated query for the canonical sorted order
        full = client.get(
            f"/api/browse-recursive?path={data_dir}&offset=0&limit=100"
        ).get_json()
        full_names = [f["name"] for f in full["files"]]

        page1 = client.get(
            f"/api/browse-recursive?path={data_dir}&offset=0&limit=2"
        ).get_json()["files"]
        page2 = client.get(
            f"/api/browse-recursive?path={data_dir}&offset=2&limit=2"
        ).get_json()["files"]
        page3 = client.get(
            f"/api/browse-recursive?path={data_dir}&offset=4&limit=2"
        ).get_json()["files"]

        concat = [f["name"] for f in (page1 + page2 + page3)]
        assert concat == full_names
        # Sanity: Avengers 001 < 002 < 010 (CAST AS REAL beats lexicographic)
        avengers_idx = [i for i, n in enumerate(full_names) if n.startswith("Avengers")]
        assert full_names[avengers_idx[0]] == "Avengers 001.cbz"
        assert full_names[avengers_idx[1]] == "Avengers 002.cbz"
        assert full_names[avengers_idx[2]] == "Avengers 010.cbz"

    def test_letter_filter_alpha(self, client, app, db_connection):
        data_dir = app.config["DATA_DIR"]
        _seed_file_index(data_dir, [
            ("Avengers 001.cbz", "Avengers", "2018", "1"),
            ("Batman 001.cbz", "Batman", "2016", "1"),
            ("Batman 002.cbz", "Batman", "2016", "2"),
            ("Catwoman 001.cbz", "Catwoman", "2018", "1"),
        ])

        resp = client.get(f"/api/browse-recursive?path={data_dir}&letter=B")
        data = resp.get_json()
        assert data["total"] == 2
        names = [f["name"] for f in data["files"]]
        assert all("Batman" in n for n in names)
        # Letters list reflects available buckets BEFORE the letter filter
        assert "A" in data["letters"]
        assert "B" in data["letters"]
        assert "C" in data["letters"]

    def test_letter_filter_hash_for_non_alpha(self, client, app, db_connection):
        data_dir = app.config["DATA_DIR"]
        _seed_file_index(data_dir, [
            ("Avengers 001.cbz", "Avengers", "2018", "1"),
            # Non-alpha first char in ci_series → '#'
            ("52 #001.cbz", "52", "2006", "1"),
            ("100 Bullets 001.cbz", "100 Bullets", "1999", "1"),
        ])

        resp = client.get(f"/api/browse-recursive?path={data_dir}&letter=%23")
        data = resp.get_json()
        names = [f["name"] for f in data["files"]]
        assert "52 #001.cbz" in names
        assert "100 Bullets 001.cbz" in names
        assert "Avengers 001.cbz" not in names
        assert "#" in data["letters"]

    def test_search_filter(self, client, app, db_connection):
        data_dir = app.config["DATA_DIR"]
        _seed_file_index(data_dir, [
            ("Batman 001.cbz", "Batman", "2016", "1"),
            ("Detective Comics 001.cbz", "Detective Comics", "2016", "1"),
            ("Superman 001.cbz", "Superman", "2018", "1"),
        ])

        # Match via name
        resp = client.get(f"/api/browse-recursive?path={data_dir}&search=batman")
        data = resp.get_json()
        assert data["total"] == 1
        assert data["files"][0]["name"] == "Batman 001.cbz"

        # Match via ci_series (file name doesn't contain 'detective' but ci_series does)
        resp2 = client.get(
            f"/api/browse-recursive?path={data_dir}&search=detective"
        )
        assert resp2.get_json()["total"] == 1

    def test_search_escapes_like_wildcards(self, client, app, db_connection):
        """A user typing '%' should NOT match everything."""
        data_dir = app.config["DATA_DIR"]
        _seed_file_index(data_dir, [
            ("Batman 001.cbz", "Batman", "2016", "1"),
            ("Superman 001.cbz", "Superman", "2018", "1"),
            ("50_off 001.cbz", "50% Off", "2020", "1"),
        ])

        resp = client.get(f"/api/browse-recursive?path={data_dir}&search=%25")
        data = resp.get_json()
        # Only files containing literal '%' in name or ci_series should match.
        # ci_series='50% Off' contains '%', so that file matches; the others don't.
        assert data["total"] == 1
        assert data["files"][0]["name"] == "50_off 001.cbz"

    def test_trailing_separator_collision_guard(self, client, app, db_connection):
        """path=/data/Marvel must NOT match files under /data/MarvelOther."""
        data_dir = app.config["DATA_DIR"]
        marvel_dir = os.path.join(data_dir, "Marvel")
        marvel_other_dir = os.path.join(data_dir, "MarvelOther")
        os.makedirs(marvel_dir, exist_ok=True)
        os.makedirs(marvel_other_dir, exist_ok=True)

        from core.database import add_file_index_entry
        add_file_index_entry(
            name="X-Men 001.cbz",
            path=os.path.join(marvel_dir, "X-Men 001.cbz"),
            entry_type="file", size=100, parent=marvel_dir,
            modified_at=1_700_000_000.0,
        )
        add_file_index_entry(
            name="Knockoff 001.cbz",
            path=os.path.join(marvel_other_dir, "Knockoff 001.cbz"),
            entry_type="file", size=100, parent=marvel_other_dir,
            modified_at=1_700_000_000.0,
        )

        resp = client.get(f"/api/browse-recursive?path={marvel_dir}")
        data = resp.get_json()
        assert data["total"] == 1
        assert data["files"][0]["name"] == "X-Men 001.cbz"

    def test_path_traversal_rejected(self, client, app, tmp_path):
        """A path outside DATA_DIR returns 400."""
        outside = str(tmp_path)  # parent of data_dir, not within
        resp = client.get(f"/api/browse-recursive?path={outside}")
        assert resp.status_code == 400

    def test_excludes_dot_dash_underscore_and_extensions(self, client, app, db_connection):
        data_dir = app.config["DATA_DIR"]
        _seed_file_index(data_dir, [
            ("Batman 001.cbz", "Batman", "2016", "1"),
            (".hidden.cbz", None, None),
            ("-leading-dash.cbz", None, None),
            ("_leading-underscore.cbz", None, None),
            ("cvinfo", None, None),
            ("folder.jpg", None, None),
            ("ComicInfo.xml", None, None),
        ])

        resp = client.get(f"/api/browse-recursive?path={data_dir}")
        names = [f["name"] for f in resp.get_json()["files"]]
        # Only the legit cbz survives the route's exclusion filter
        assert names == ["Batman 001.cbz"]

    def test_empty_index_returns_empty(self, client, app, db_connection):
        data_dir = app.config["DATA_DIR"]
        resp = client.get(f"/api/browse-recursive?path={data_dir}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 0
        assert data["files"] == []
        assert data["letters"] == []

    def test_does_not_call_os_walk(self, client, app, db_connection):
        """Performance regression guard: route must not fall back to os.walk."""
        data_dir = app.config["DATA_DIR"]
        _seed_file_index(data_dir, [
            ("Batman 001.cbz", "Batman", "2016", "1"),
        ])

        with patch("routes.collection.os.walk",
                   side_effect=RuntimeError("os.walk must not be called")):
            resp = client.get(f"/api/browse-recursive?path={data_dir}")

        assert resp.status_code == 200
        assert resp.get_json()["total"] == 1

    def test_invalid_path_returns_400(self, client, app):
        data_dir = app.config["DATA_DIR"]
        bogus = os.path.join(data_dir, "DoesNotExist")
        resp = client.get(f"/api/browse-recursive?path={bogus}")
        assert resp.status_code == 400
