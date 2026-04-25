"""Tests for the public /api/v1/docs reference page."""

from routes.api_v1_docs import ENDPOINTS


class TestApiV1Docs:

    def test_docs_page_is_public(self, db_connection, client):
        """No bearer token required -- docs are reference material."""
        resp = client.get("/api/v1/docs")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/html")

    def test_docs_page_lists_every_endpoint(self, db_connection, client):
        body = client.get("/api/v1/docs").get_data(as_text=True)
        for ep in ENDPOINTS:
            # Angle brackets in paths (e.g. <file_id>) are HTML-escaped on render.
            expected = ep["path"].replace("<", "&lt;").replace(">", "&gt;")
            assert expected in body, f"docs page missing endpoint {ep['path']}"

    def test_docs_page_describes_pagination_envelope(
        self, db_connection, client
    ):
        body = client.get("/api/v1/docs").get_data(as_text=True)
        for key in ("total_pages", "has_more", "page_size", "Bearer"):
            assert key in body
