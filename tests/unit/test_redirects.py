"""helpers.redirects: which login ``next`` targets are followed."""
import pytest
from flask import Flask, request

from helpers.redirects import login_next_target, safe_redirect_target


ACCEPTED = [
    "/",
    "/config",
    "/collection?path=%2Fdata%2FMarvel&page=2",
    "/files/My%20Series",
    "/a/b/c#frag",
    "/reader/x:y",
]

REJECTED = [
    None,
    "",
    123,
    "config",                      # not rooted
    "http://evil.example/",
    "https://evil.example",
    "HTTP://evil.example",
    "javascript:alert(1)",
    "data:text/html,x",
    "//evil.example",
    "//evil.example/path",
    "///evil.example",
    "/\\evil.example",
    "\\\\evil.example",
    "\\/evil.example",
    "/a\\b",
    " /config",
    "/config ",
    "/\t/evil.example",
    "/\n/evil.example",
    "/\r/evil.example",
    "\t//evil.example",
    "/\x00/evil.example",
    "/\x1b",
    "/\x7f",
    "/\x85evil",
    "/ /evil.example",
    "/　/evil.example",
    "/" + "a" * 5000,
]


@pytest.mark.parametrize("target", ACCEPTED)
def test_same_site_paths_are_accepted(target):
    assert safe_redirect_target(target) == target


@pytest.mark.parametrize("target", REJECTED)
def test_unsafe_targets_are_rejected(target):
    assert safe_redirect_target(target) is None


class TestScriptRoot:
    """Behind ProxyFix(x_prefix=1) a target must stay under the script root."""

    @pytest.mark.parametrize("target", ["/clu", "/clu/", "/clu/config", "/clu/x?y=1", "/clu?x=1"])
    def test_under_script_root_accepted(self, target):
        assert safe_redirect_target(target, "/clu") == target

    @pytest.mark.parametrize("target", ["/config", "/clubhouse", "/other/clu", "//clu/x"])
    def test_outside_script_root_rejected(self, target):
        assert safe_redirect_target(target, "/clu") is None

    def test_trailing_slash_on_script_root(self):
        assert safe_redirect_target("/clu/config", "/clu/") == "/clu/config"


class TestLoginNextTarget:
    def _target(self, path, **kw):
        app = Flask(__name__)
        with app.test_request_context(path, **kw):
            return login_next_target()

    def test_path_only(self):
        assert self._target("/config") == "/config"

    def test_keeps_query_string(self):
        assert self._target("/collection?page=2&x=a%20b") == "/collection?page=2&x=a%20b"

    def test_includes_script_root(self):
        assert self._target("/config", base_url="http://localhost/clu/") == "/clu/config"

    def test_quotes_spaces_and_backslashes(self):
        target = self._target("/files/My%20Series%5Cx")
        assert target == "/files/My%20Series%5Cx"
        assert safe_redirect_target(target) == target

    def test_produced_target_is_always_accepted(self):
        for path, kw in [
            ("/a%20b/c?d=e", {}),
            ("/caf%C3%A9", {}),
            ("/x", {"base_url": "http://localhost/sub/"}),
        ]:
            app = Flask(__name__)
            with app.test_request_context(path, **kw):
                target = login_next_target()
                assert safe_redirect_target(target, request.script_root) == target
