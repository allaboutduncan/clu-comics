"""core.outbound_credentials: a stored secret goes only where it was saved for."""
import pytest

from core.outbound_credentials import endpoint_identity, reused_secret_refusal


def _url(cfg):
    return cfg.get("url")


class TestEndpointIdentity:

    @pytest.mark.parametrize("url,expected", [
        ("http://nas.local", ("http", "nas.local", 80)),
        ("http://NAS.local:80/api", ("http", "nas.local", 80)),
        ("https://nas.local", ("https", "nas.local", 443)),
        ("https://nas.local:8443", ("https", "nas.local", 8443)),
        ("http://192.168.1.10:8080", ("http", "192.168.1.10", 8080)),
        ("http://[::1]:8080", ("http", "::1", 8080)),
    ])
    def test_plain_urls(self, url, expected):
        assert endpoint_identity(url) == expected

    @pytest.mark.parametrize("url", [
        "", None, "nas.local", "ftp://nas.local",
        "http://user@nas.local", "http://nas.local\\@other.local",
        "http://nas.local:99999", "http://nas.local:80@other.local",
        "http://nas local",
    ])
    def test_anything_else_matches_nothing(self, url):
        assert endpoint_identity(url) is None


class TestReusedSecretRefusal:

    STORED = {"url": "http://nas.local:8080", "api_key": "stored-test-key",
              "username": "u", "password": "stored-test-pass"}

    def test_no_stored_secret_is_never_refused(self):
        assert reused_secret_refusal({"url": "http://a"}, {"url": "http://b"},
                                     ("api_key",), _url) is None

    def test_supplied_secret_is_never_refused(self):
        assert reused_secret_refusal(self.STORED, {"url": "http://b", "api_key": "x"},
                                     ("api_key",), _url) is None

    def test_same_destination_reuses(self):
        assert reused_secret_refusal(self.STORED, {"url": "http://NAS.local:8080/x"},
                                     ("api_key",), _url) is None

    def test_other_destination_refused(self):
        msg = reused_secret_refusal(self.STORED, {"url": "http://other:8080"},
                                    ("api_key",), _url)
        assert "API key" in msg

    def test_identity_field_change_refused(self):
        msg = reused_secret_refusal(self.STORED, {"username": "v"},
                                    ("password",), _url, identity_fields=("username",))
        assert "password" in msg

    def test_unparsable_stored_destination_refuses_reuse(self):
        stored = {"url": "http://a@b", "api_key": "k"}
        assert reused_secret_refusal(stored, {"x": 1}, ("api_key",), _url)

    def test_destination_that_raises_refuses(self):
        def boom(cfg):
            raise TypeError("bad")
        assert reused_secret_refusal(self.STORED, {"x": 1}, ("api_key",), boom)

    def test_message_never_contains_the_secret(self):
        msg = reused_secret_refusal(self.STORED, {"url": "http://other"},
                                    ("api_key", "password"), _url)
        assert "stored-test-key" not in msg and "stored-test-pass" not in msg
