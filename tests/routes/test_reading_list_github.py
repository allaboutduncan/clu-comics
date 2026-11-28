"""Tests for reading list GitHub tree browser, batch import, and sync endpoints."""
import pytest
from unittest.mock import patch, MagicMock
import hashlib


DEFAULT = "DieselTech/CBL-ReadingLists"


@pytest.fixture
def repos(monkeypatch, tmp_path):
    """Pin the configured repo list and isolate the tree caches."""
    import core.cbl_repos as cbl_repos
    import routes.reading_lists as rl
    state = {"value": DEFAULT}
    monkeypatch.setattr(cbl_repos, "get_setting", lambda key: state["value"])

    def _set(key, value):
        state["value"] = value
        return True
    monkeypatch.setattr(cbl_repos, "set_setting", _set)
    monkeypatch.setattr(rl, "_github_tree_cache", {})
    monkeypatch.setattr(rl, "_get_github_tree_cache_path",
                        lambda spec: str(tmp_path / f"{spec.replace('/', '_')}.json"))
    return state


def _tree_response(paths, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.ok = status < 400
    resp.json.return_value = {"tree": [{"path": p, "type": "blob"} for p in paths]}
    return resp


class TestGithubTree:

    @patch("routes.reading_lists.requests.get")
    def test_github_tree_returns_tree(self, mock_get, client, repos):
        mock_get.return_value = _tree_response(["DC/Events/Crisis.cbl"])

        resp = client.get("/api/reading-lists/github-tree")
        data = resp.get_json()
        assert data["success"] is True
        assert data["repo"] == DEFAULT
        assert data["fetched_at"]
        assert len(data["tree"]) > 0
        assert "/repos/DieselTech/CBL-ReadingLists/git/trees/HEAD" in mock_get.call_args.args[0]

    @patch("routes.reading_lists.requests.get")
    def test_github_tree_filters_cbl_files(self, mock_get, client, repos):
        mock_get.return_value = _tree_response(
            ["DC/Events/Crisis.cbl", "DC/Events/README.md", "Marvel/X-Men.cbl"])

        data = client.get("/api/reading-lists/github-tree").get_json()
        assert data["success"] is True
        blob_items = [i for i in data["tree"] if i["type"] == "blob"]
        assert [i["path"] for i in blob_items] == ["DC/Events/Crisis.cbl", "Marvel/X-Men.cbl"]

    @patch("routes.reading_lists.requests.get")
    def test_github_tree_returns_cached_tree(self, mock_get, client, repos):
        import routes.reading_lists as rl
        import time
        rl._github_tree_cache[DEFAULT] = {
            "tree": [{"path": "cached.cbl", "type": "blob"}],
            "fetched_at": time.time(),
        }

        data = client.get("/api/reading-lists/github-tree").get_json()
        assert data["success"] is True
        assert data["tree"][0]["path"] == "cached.cbl"
        mock_get.assert_not_called()

    @patch("routes.reading_lists.requests.get")
    def test_refresh_bypasses_the_cache(self, mock_get, client, repos):
        import routes.reading_lists as rl
        import time
        rl._github_tree_cache[DEFAULT] = {
            "tree": [{"path": "cached.cbl", "type": "blob"}],
            "fetched_at": time.time(),
        }
        mock_get.return_value = _tree_response(["fresh.cbl"])

        data = client.get("/api/reading-lists/github-tree?refresh=1").get_json()
        assert [i["path"] for i in data["tree"]] == ["fresh.cbl"]
        assert rl._github_tree_cache[DEFAULT]["tree"] == data["tree"]

    @patch("routes.reading_lists.requests.get")
    def test_selects_a_configured_repo_and_its_branch(self, mock_get, client, repos):
        repos["value"] = f"{DEFAULT},someone/lists@dev"
        mock_get.return_value = _tree_response(["a.cbl"])

        data = client.get("/api/reading-lists/github-tree?repo=someone/lists@dev").get_json()
        assert data["repo"] == "someone/lists@dev"
        assert "/repos/someone/lists/git/trees/dev?" in mock_get.call_args.args[0]

    @patch("routes.reading_lists.requests.get")
    def test_unconfigured_repo_is_refused(self, mock_get, client, repos):
        resp = client.get("/api/reading-lists/github-tree?repo=evil/repo")
        assert resp.status_code == 404
        mock_get.assert_not_called()

    def test_no_repos_configured(self, client, repos):
        repos["value"] = ""
        resp = client.get("/api/reading-lists/github-tree")
        assert resp.status_code == 404
        assert "No GitHub repositories" in resp.get_json()["message"]

    @patch("routes.reading_lists.requests.get")
    def test_missing_repo_reports_a_readable_error(self, mock_get, client, repos):
        mock_get.return_value = _tree_response([], status=404)
        resp = client.get("/api/reading-lists/github-tree")
        assert resp.status_code == 502
        assert "not found" in resp.get_json()["message"]


class TestGithubRepos:

    def test_lists_configured_repos(self, client, repos):
        repos["value"] = f"{DEFAULT},someone/lists@dev"
        data = client.get("/api/reading-lists/github-repos").get_json()
        assert data["success"] is True
        assert data["default"] == DEFAULT
        assert [r["spec"] for r in data["repos"]] == [DEFAULT, "someone/lists@dev"]
        assert data["repos"][1]["branch"] == "dev"
        assert data["repos"][1]["url"] == "https://github.com/someone/lists/tree/dev"
        assert data["repos"][0]["branch"] is None

    def test_save_normalizes_urls_and_dedupes(self, client, repos):
        resp = client.post("/api/reading-lists/github-repos", json={"repos": [
            "https://github.com/someone/lists/tree/dev",
            "someone/lists@dev",
            DEFAULT,
        ]})
        data = resp.get_json()
        assert data["success"] is True
        assert repos["value"] == f"someone/lists@dev,{DEFAULT}"

    def test_save_empty_list_removes_all(self, client, repos):
        resp = client.post("/api/reading-lists/github-repos", json={"repos": []})
        assert resp.get_json()["success"] is True
        assert repos["value"] == ""
        assert client.get("/api/reading-lists/github-repos").get_json()["repos"] == []

    def test_save_rejects_garbage(self, client, repos):
        resp = client.post("/api/reading-lists/github-repos", json={"repos": ["not a repo"]})
        assert resp.status_code == 400
        assert repos["value"] == DEFAULT

    def test_removing_a_repo_drops_its_cached_tree(self, client, repos):
        import routes.reading_lists as rl
        import time
        repos["value"] = f"{DEFAULT},someone/lists"
        rl._github_tree_cache["someone/lists"] = {"tree": [], "fetched_at": time.time()}
        client.post("/api/reading-lists/github-repos", json={"repos": [DEFAULT]})
        assert "someone/lists" not in rl._github_tree_cache


class TestImportBatch:

    @patch("routes.reading_lists.threading.Thread")
    def test_import_batch_creates_tasks(self, mock_thread, client, repos):
        resp = client.post(
            "/api/reading-lists/import-batch",
            json={"files": ["DC/Crisis.cbl", "Marvel/X-Men.cbl"]},
        )
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["tasks"]) == 2
        assert data["tasks"][0]["filename"] == "Crisis.cbl"
        assert data["tasks"][1]["filename"] == "X-Men.cbl"

    @patch("routes.reading_lists.threading.Thread")
    def test_import_batch_quotes_hash_in_path(self, mock_thread, client, repos):
        """'#' in a CBL name is part of the name, not a URL fragment."""
        path = "DC/CBRO/[DC Comics] Master Reading Order Part #01 (WEB-CBRO).cbl"
        resp = client.post("/api/reading-lists/import-batch", json={"files": [path]})
        assert resp.get_json()["success"] is True
        url = mock_thread.call_args.kwargs["args"][1]
        assert url == (
            "https://raw.githubusercontent.com/DieselTech/CBL-ReadingLists/HEAD/"
            "DC/CBRO/%5BDC%20Comics%5D%20Master%20Reading%20Order%20Part%20"
            "%2301%20%28WEB-CBRO%29.cbl"
        )
        assert mock_thread.call_args.kwargs["args"][2] == path.split("/")[-1]

    @patch("routes.reading_lists.threading.Thread")
    def test_import_batch_uses_the_chosen_repo(self, mock_thread, client, repos):
        repos["value"] = f"{DEFAULT},someone/lists@dev"
        client.post("/api/reading-lists/import-batch",
                    json={"repo": "someone/lists@dev", "files": ["a.cbl"]})
        assert mock_thread.call_args.kwargs["args"][1] == \
            "https://raw.githubusercontent.com/someone/lists/dev/a.cbl"

    @patch("routes.reading_lists.threading.Thread")
    def test_import_batch_refuses_unconfigured_repo(self, mock_thread, client, repos):
        resp = client.post("/api/reading-lists/import-batch",
                           json={"repo": "evil/repo", "files": ["a.cbl"]})
        assert resp.status_code == 400
        mock_thread.assert_not_called()

    def test_import_batch_no_files(self, client, repos):
        resp = client.post("/api/reading-lists/import-batch", json={"files": []})
        data = resp.get_json()
        assert data["success"] is False


class TestSyncList:

    @patch("routes.reading_lists.requests.get")
    @patch("routes.reading_lists.get_reading_list")
    def test_sync_unchanged_list(self, mock_get_list, mock_requests_get, client):
        content = "<ReadingList><Name>Test</Name><Books></Books></ReadingList>"
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        mock_get_list.return_value = {
            "id": 1,
            "name": "Test",
            "source": "https://raw.githubusercontent.com/DieselTech/CBL-ReadingLists/main/test.cbl",
            "source_hash": content_hash,
            "entries": [],
        }

        mock_response = MagicMock()
        mock_response.text = content
        mock_response.raise_for_status = MagicMock()
        mock_requests_get.return_value = mock_response

        resp = client.post("/api/reading-lists/1/sync")
        data = resp.get_json()
        assert data["success"] is True
        assert data["changed"] is False

    @patch("routes.reading_lists.threading.Thread")
    @patch("routes.reading_lists.requests.get")
    @patch("routes.reading_lists.get_reading_list")
    def test_sync_changed_list_is_backgrounded(self, mock_get_list, mock_requests_get,
                                               mock_thread, client):
        """A changed list comes back as a task, not a result.

        The probe is one request and answers in-band, but the rebuild behind it
        is not: a ComicVine arc resolves one request per issue, so every apply
        goes to a thread rather than only the ones that would time out.
        """
        mock_get_list.return_value = {
            "id": 1,
            "name": "Test",
            "source": "https://raw.githubusercontent.com/DieselTech/CBL-ReadingLists/main/test.cbl",
            "source_hash": "oldhash",
            "entries": [],
        }

        new_content = """<?xml version="1.0"?>
        <ReadingList><Name>Test</Name><Books>
            <Book Series="Batman" Number="1" Volume="2016" Year="2016"/>
        </Books></ReadingList>"""

        mock_response = MagicMock()
        mock_response.text = new_content
        mock_response.raise_for_status = MagicMock()
        mock_requests_get.return_value = mock_response

        resp = client.post("/api/reading-lists/1/sync")
        data = resp.get_json()
        assert data["success"] is True
        assert data["changed"] is True
        assert data["background"] is True
        assert data["task_id"]
        assert mock_thread.called

    @patch("core.reading_list_sync.requests.get")
    @patch("routes.reading_lists.get_reading_list")
    def test_sync_falls_back_to_source_hash(self, mock_get_list, mock_requests_get, client):
        """A list imported before source_version existed must not report changed.

        Every GitHub list in an upgraded database has a source_hash and a NULL
        source_version; reading the old column when the new one is empty is
        what keeps the first sweep after an upgrade from rebuilding all of them.
        """
        content = "<ReadingList><Name>Test</Name><Books></Books></ReadingList>"
        mock_get_list.return_value = {
            "id": 1,
            "name": "Test",
            "source": "https://raw.githubusercontent.com/DieselTech/CBL-ReadingLists/main/test.cbl",
            "source_hash": hashlib.sha256(content.encode()).hexdigest(),
            "source_version": None,
            "entries": [],
        }

        mock_response = MagicMock()
        mock_response.text = content
        mock_response.raise_for_status = MagicMock()
        mock_requests_get.return_value = mock_response

        resp = client.post("/api/reading-lists/1/sync")
        data = resp.get_json()
        assert data["success"] is True
        assert data["changed"] is False

    @patch("routes.reading_lists.threading.Thread")
    @patch("core.reading_list_sync.requests.get")
    @patch("routes.reading_lists.get_reading_list")
    def test_sync_force_reruns_an_unchanged_list(self, mock_get_list, mock_requests_get,
                                                 mock_thread, client):
        content = "<ReadingList><Name>Test</Name><Books></Books></ReadingList>"
        mock_get_list.return_value = {
            "id": 1,
            "name": "Test",
            "source": "https://raw.githubusercontent.com/DieselTech/CBL-ReadingLists/main/test.cbl",
            "source_version": hashlib.sha256(content.encode()).hexdigest(),
            "entries": [],
        }

        mock_response = MagicMock()
        mock_response.text = content
        mock_response.raise_for_status = MagicMock()
        mock_requests_get.return_value = mock_response

        resp = client.post("/api/reading-lists/1/sync", json={"force": True})
        data = resp.get_json()
        assert data["changed"] is True
        assert mock_thread.called

    @patch("routes.reading_lists.get_reading_list")
    def test_sync_returns_error_for_unsyncable_source(self, mock_get_list, client):
        """An uploaded .cbl has nowhere to go back to."""
        mock_get_list.return_value = {
            "id": 1,
            "name": "Test",
            "source": "uploaded_file.cbl",
            "source_hash": None,
            "entries": [],
        }

        resp = client.post("/api/reading-lists/1/sync")
        data = resp.get_json()
        assert data["success"] is False
        assert resp.status_code == 400

    @patch("routes.reading_lists.get_reading_list", return_value=None)
    def test_sync_not_found(self, mock_get_list, client):
        resp = client.post("/api/reading-lists/999/sync")
        assert resp.status_code == 404
