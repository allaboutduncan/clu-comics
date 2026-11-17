"""The login exemption covers POST /download and nothing else.

The browser extension posts ``{"link": ...}`` to ``/download`` with no session
cookie, so that one path stays auth-free. The queue's status, cancel, retry,
dismiss and clear endpoints are called by the logged-in Status page and go
through the normal login and role check.

api.py cannot be imported in tests, so the endpoints are stubbed on the test
app under their real paths; what is under test is the before_app_request gate.
"""
import pytest
from flask import jsonify

from core.auth import required_role_for_request
from core.database import create_user


QUEUE_MUTATIONS = [
    "/cancel_download/abc",
    "/retry_download/abc",
    "/dismiss_download/abc",
    "/clear_downloads",
    "/clear_failed_downloads",
]
QUEUE_READS = [
    "/download_status/abc",
    "/download_status_all",
]


@pytest.fixture
def queue_app(app):
    def ok(**_kw):
        return jsonify({"ok": True})

    app.add_url_rule("/download", "stub_download", ok, methods=["GET", "POST", "OPTIONS"])
    app.add_url_rule("/download_status/<download_id>", "stub_status", ok)
    app.add_url_rule("/download_status_all", "stub_status_all", ok)
    app.add_url_rule("/download_summary", "stub_summary", ok)
    app.add_url_rule("/cancel_download/<download_id>", "stub_cancel", ok, methods=["POST"])
    app.add_url_rule("/retry_download/<download_id>", "stub_retry", ok, methods=["POST"])
    app.add_url_rule("/dismiss_download/<download_id>", "stub_dismiss", ok, methods=["POST"])
    app.add_url_rule("/clear_downloads", "stub_clear", ok, methods=["POST"])
    app.add_url_rule("/clear_failed_downloads", "stub_clear_failed", ok, methods=["POST"])
    return app


@pytest.fixture
def queue_client(queue_app):
    return queue_app.test_client()


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


class TestRolePolicy:
    @pytest.mark.parametrize("path", QUEUE_MUTATIONS)
    def test_queue_mutations_need_clerk(self, app, path):
        with app.test_request_context(path, method="POST"):
            assert required_role_for_request() == "clerk"

    @pytest.mark.parametrize("path", QUEUE_READS)
    def test_queue_status_needs_clerk_like_the_status_page(self, app, path):
        with app.test_request_context(path, method="GET"):
            assert required_role_for_request() == "clerk"
        with app.test_request_context("/status", method="GET"):
            assert required_role_for_request() == "clerk"

    def test_download_summary_stays_reader(self, app):
        # The nav badge polls it on every page, for every role.
        with app.test_request_context("/download_summary", method="GET"):
            assert required_role_for_request() == "reader"


class TestLoginRequired:
    @pytest.fixture(autouse=True)
    def _users(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("clerk", password="clerkpass", role="clerk")
        create_user("reader", password="readerpass", role="reader")
        yield

    def test_post_download_is_exempt(self, queue_client):
        r = queue_client.post("/download", json={"link": "https://getcomics.org/x"})
        assert r.status_code == 200

    def test_get_download_help_page_is_exempt(self, queue_client):
        assert queue_client.get("/download").status_code == 200

    @pytest.mark.parametrize("path", QUEUE_MUTATIONS)
    def test_queue_mutations_require_login(self, queue_client, path):
        r = queue_client.post(path, follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    @pytest.mark.parametrize("path", QUEUE_READS + ["/download_summary"])
    def test_queue_reads_require_login(self, queue_client, path):
        r = queue_client.get(path, follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_prefix_lookalike_is_not_exempt(self, queue_client):
        # The old exemption was the prefix "/download"; any path that merely
        # starts with it must now be gated.
        r = queue_client.get("/downloadable-thing", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    @pytest.mark.parametrize("path", QUEUE_MUTATIONS)
    def test_clerk_can_manage_queue(self, queue_client, path):
        _login(queue_client, "clerk", "clerkpass")
        assert queue_client.post(path).status_code == 200

    @pytest.mark.parametrize("path", QUEUE_READS + ["/download_summary"])
    def test_clerk_can_read_status(self, queue_client, path):
        _login(queue_client, "clerk", "clerkpass")
        assert queue_client.get(path).status_code == 200

    @pytest.mark.parametrize("path", QUEUE_MUTATIONS)
    def test_reader_cannot_manage_queue(self, queue_client, path):
        _login(queue_client, "reader", "readerpass")
        r = queue_client.post(path, follow_redirects=False)
        assert r.status_code == 302
        assert "/login" not in r.headers["Location"]

    @pytest.mark.parametrize("path", QUEUE_READS)
    def test_reader_cannot_read_queue_rows(self, queue_client, path):
        _login(queue_client, "reader", "readerpass")
        assert queue_client.get(path, follow_redirects=False).status_code == 302

    def test_reader_can_read_badge_count(self, queue_client):
        _login(queue_client, "reader", "readerpass")
        assert queue_client.get("/download_summary").status_code == 200


class TestImplicitOwnerMode:
    """No accounts and no env gate: nothing changes, every endpoint is open."""

    @pytest.fixture(autouse=True)
    def _no_gate(self, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)

    @pytest.mark.parametrize("path", QUEUE_MUTATIONS)
    def test_mutations_open(self, queue_client, path):
        assert queue_client.post(path).status_code == 200

    @pytest.mark.parametrize("path", QUEUE_READS + ["/download_summary", "/download"])
    def test_reads_open(self, queue_client, path):
        assert queue_client.get(path).status_code == 200
