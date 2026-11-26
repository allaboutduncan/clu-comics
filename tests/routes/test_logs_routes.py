"""The Logs page and its incremental tail (routes/logs.py), moved out of app.py."""
import pytest

import routes.logs as logs_routes


@pytest.fixture
def log_files(tmp_path, monkeypatch):
    app_log = tmp_path / "app.log"
    mon_log = tmp_path / "monitor.log"
    app_log.write_bytes(b"one\ntwo\nthree\n")
    mon_log.write_bytes(b"watching\n")
    monkeypatch.setattr(logs_routes, "_LOG_FILES", {"app": str(app_log), "mon": str(mon_log)})
    return app_log, mon_log


class TestPage:
    def test_the_logs_page_renders(self, client):
        resp = client.get("/logs")
        assert resp.status_code == 200
        assert "<html" in resp.get_data(as_text=True)

    @pytest.mark.parametrize("old", ["/app-logs", "/mon-logs"])
    def test_the_two_old_pages_redirect_to_it(self, client, old):
        resp = client.get(old)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/logs")

    def test_the_header_links_to_it(self, client):
        assert 'href="/logs"' in client.get("/logs").get_data(as_text=True)


class TestTail:
    def test_first_load_returns_the_lines_and_the_offset(self, client, log_files):
        body = client.get("/logs/tail?type=app").get_json()
        assert body == {"lines": ["one", "two", "three"], "pos": 14, "reset": False}

    def test_the_type_defaults_to_the_app_log(self, client, log_files):
        assert client.get("/logs/tail").get_json()["lines"] == ["one", "two", "three"]

    def test_the_monitor_log_is_separate(self, client, log_files):
        assert client.get("/logs/tail?type=mon").get_json()["lines"] == ["watching"]

    def test_a_poll_returns_only_what_was_added(self, client, log_files):
        app_log, _ = log_files
        pos = client.get("/logs/tail?type=app").get_json()["pos"]
        with open(app_log, "ab") as fh:
            fh.write(b"four\n")
        body = client.get(f"/logs/tail?type=app&pos={pos}").get_json()
        assert body == {"lines": ["four"], "pos": pos + 5, "reset": False}

    def test_nothing_new_returns_nothing(self, client, log_files):
        body = client.get("/logs/tail?type=app&pos=14").get_json()
        assert body == {"lines": [], "pos": 14, "reset": False}

    def test_a_half_written_line_waits_for_its_newline(self, client, log_files):
        app_log, _ = log_files
        with open(app_log, "ab") as fh:
            fh.write(b"partial")
        body = client.get("/logs/tail?type=app&pos=14").get_json()
        assert body["lines"] == [] and body["pos"] == 14

    def test_a_cleared_log_tells_the_page_to_start_over(self, client, log_files):
        """entrypoint.sh truncates the logs on start; the page's offset is then past the end."""
        app_log, _ = log_files
        app_log.write_bytes(b"fresh\n")
        body = client.get("/logs/tail?type=app&pos=14").get_json()
        assert body == {"lines": ["fresh"], "pos": 6, "reset": True}

    def test_an_unknown_type_is_400_and_reads_no_file(self, client, log_files):
        resp = client.get("/logs/tail?type=../../config/comic_utils.db")
        assert resp.status_code == 400
        assert resp.get_json() == {"error": "Invalid log type"}

    def test_a_missing_log_is_empty_not_an_error(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(logs_routes, "_LOG_FILES", {"app": str(tmp_path / "none.log")})
        body = client.get("/logs/tail?type=app").get_json()
        assert body == {"lines": [], "pos": 0, "reset": False}
