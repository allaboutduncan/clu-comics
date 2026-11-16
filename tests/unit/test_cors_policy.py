"""The CORS policy: only the browser extension's POST to /download is
cross-origin readable.

api.py cannot be imported in tests, so the behaviour is checked by applying
``core.cors.configure_cors`` to a small app with the same route shapes, and a
structural test asserts that api.py applies that function and nothing else.
"""
import ast
import os

import pytest
from flask import Flask, jsonify, request

from core.cors import configure_cors

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API_PY = os.path.join(PROJECT_ROOT, "api.py")

WEB_ORIGIN = "https://evil.example.com"
LAN_ORIGIN = "http://192.168.1.50:8080"
CHROME_ORIGIN = "chrome-extension://cpickljbofjhmhkphgdmiagkdfijlkkg"
FIREFOX_ORIGIN = "moz-extension://0b6c2e2a-6a3f-4b55-9a3e-4f0c2b8f1d2e"
SAFARI_ORIGIN = "safari-web-extension://ABCDEF12-3456-7890-ABCD-EF1234567890"

ACAO = "Access-Control-Allow-Origin"


@pytest.fixture
def client():
    app = Flask(__name__)

    # Mirrors api.py's /download, including its own OPTIONS branch.
    @app.route("/download", methods=["POST", "OPTIONS"])
    def download():
        if request.method == "OPTIONS":
            return jsonify({}), 200
        return jsonify({"message": "Download queued"}), 200

    @app.route("/download_status_all")
    def download_status_all():
        return jsonify({})

    @app.route("/clear_downloads", methods=["POST"])
    def clear_downloads():
        return jsonify({})

    @app.route("/api/v1/library")
    def api_v1():
        return jsonify({})

    @app.route("/api/browse")
    def browse():
        return jsonify({"files": []})

    configure_cors(app)
    return app.test_client()


def _preflight(client, path, origin, method="POST"):
    return client.open(
        path,
        method="OPTIONS",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "content-type",
        },
    )


class TestArbitraryOriginsGetNothing:
    @pytest.mark.parametrize("path", ["/api/browse", "/api/v1/library", "/download_status_all"])
    @pytest.mark.parametrize("origin", [WEB_ORIGIN, LAN_ORIGIN, "null"])
    def test_get_has_no_allow_origin(self, client, path, origin):
        resp = client.get(path, headers={"Origin": origin})
        assert resp.status_code == 200
        assert ACAO not in resp.headers

    @pytest.mark.parametrize("path", ["/api/browse", "/download", "/clear_downloads"])
    def test_preflight_from_web_origin_not_approved(self, client, path):
        resp = _preflight(client, path, WEB_ORIGIN)
        assert ACAO not in resp.headers
        assert "Access-Control-Allow-Methods" not in resp.headers

    def test_post_to_download_from_web_origin_not_readable(self, client):
        resp = client.post("/download", json={"link": "x"}, headers={"Origin": WEB_ORIGIN})
        assert ACAO not in resp.headers

    @pytest.mark.parametrize("origin", [
        "https://chrome-extension.example.com",
        "chrome-extension://abc.evil.com/path",
        "http://chrome-extension://abc",
        "chrome-extension://",
    ])
    def test_lookalike_extension_origins_rejected(self, client, origin):
        resp = client.post("/download", json={"link": "x"}, headers={"Origin": origin})
        assert ACAO not in resp.headers


class TestExtensionStillAllowed:
    @pytest.mark.parametrize("origin", [CHROME_ORIGIN, FIREFOX_ORIGIN, SAFARI_ORIGIN])
    def test_post_download_allowed(self, client, origin):
        resp = client.post("/download", json={"link": "x"}, headers={"Origin": origin})
        assert resp.status_code == 200
        assert resp.headers.get(ACAO) == origin
        # Never the wildcard, never credentials.
        assert "Access-Control-Allow-Credentials" not in resp.headers

    @pytest.mark.parametrize("origin", [CHROME_ORIGIN, FIREFOX_ORIGIN])
    def test_preflight_download_approved_with_custom_headers(self, client, origin):
        resp = client.open(
            "/download",
            method="OPTIONS",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                # The extension lets users add headers, e.g. Cloudflare Access.
                "Access-Control-Request-Headers": "content-type, cf-access-client-id",
            },
        )
        assert resp.headers.get(ACAO) == origin
        assert "POST" in resp.headers.get("Access-Control-Allow-Methods", "")
        allowed = resp.headers.get("Access-Control-Allow-Headers", "").lower()
        assert "content-type" in allowed
        assert "cf-access-client-id" in allowed

    @pytest.mark.parametrize("path", ["/download_status_all", "/clear_downloads", "/api/browse"])
    def test_extension_origin_gets_nothing_elsewhere(self, client, path):
        resp = client.open(path, method="GET" if path != "/clear_downloads" else "POST",
                           headers={"Origin": CHROME_ORIGIN})
        assert ACAO not in resp.headers


class TestSameOriginUnaffected:
    def test_no_origin_header_gets_no_cors_headers(self, client):
        resp = client.get("/api/browse")
        assert resp.status_code == 200
        assert ACAO not in resp.headers


class TestApiPyUsesThePolicy:
    def _tree(self):
        with open(API_PY, encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def test_api_py_calls_configure_cors_on_app(self):
        calls = [
            n for n in ast.walk(self._tree())
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "configure_cors"
        ]
        assert len(calls) == 1
        assert [a.id for a in calls[0].args if isinstance(a, ast.Name)] == ["app"]

    def test_api_py_does_not_call_flask_cors_directly(self):
        tree = self._tree()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "flask_cors":
                pytest.fail("api.py must apply CORS through core.cors.configure_cors")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "CORS"

    def test_policy_has_no_wildcard(self):
        from core.cors import CORS_RESOURCES
        for options in CORS_RESOURCES.values():
            assert "*" not in options["origins"]
