"""The same-origin decision for state-changing requests (core/request_origin.py).

Pure-function tests: every branch of ``decide`` and the host normalisation.
The Flask wiring is covered by tests/routes/test_same_origin_requests.py.
"""
import pytest

from core.request_origin import (
    TRUSTED_ORIGINS_ENV,
    decide,
    normalize_host,
    parse_trusted_origins,
    refusal_message,
    trusted_origins,
)

HOSTS = ["clu.lan:5577"]


def _decide(headers, method="POST", path="/api/thing", hosts=HOSTS, trusted=frozenset()):
    return decide(method, path, headers, hosts, trusted)


class TestNormalizeHost:
    @pytest.mark.parametrize("value, expected", [
        ("clu.lan:5577", "clu.lan:5577"),
        ("CLU.Lan:5577", "clu.lan:5577"),
        ("clu.lan", "clu.lan"),
        ("clu.lan:80", "clu.lan"),
        ("clu.lan:443", "clu.lan"),
        ("clu.lan.", "clu.lan"),
        ("http://clu.lan:5577", "clu.lan:5577"),
        ("https://comics.example.com", "comics.example.com"),
        ("https://comics.example.com:443/clu/page?x=1", "comics.example.com"),
        ("http://comics.example.com:80", "comics.example.com"),
        ("[::1]:5577", "[::1]:5577"),
        ("http://[::1]:5577", "[::1]:5577"),
        ("192.168.1.10:5577", "192.168.1.10:5577"),
        (" clu.lan:5577 ", "clu.lan:5577"),
    ])
    def test_normalises(self, value, expected):
        assert normalize_host(value) == expected

    @pytest.mark.parametrize("value", ["", None, "   ", "clu.lan:notaport", "http://", ":5577"])
    def test_rejects_garbage(self, value):
        assert normalize_host(value) is None


class TestSafeMethods:
    @pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get"])
    def test_never_blocked(self, method):
        d = _decide({"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"},
                    method=method)
        assert d.allowed
        assert d.reason == "safe-method"

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
    def test_state_changing_methods_are_checked(self, method):
        d = _decide({"Origin": "http://evil.example"}, method=method)
        assert not d.allowed


class TestExemptions:
    def test_extension_download_endpoint(self):
        d = _decide({"Origin": "chrome-extension://abcdef"}, path="/download")
        assert d.allowed and d.reason == "exempt"

    def test_download_queue_endpoints_are_not_exempt(self):
        for path in ("/download_status_all", "/download/", "/downloads", "/clear_downloads"):
            assert not _decide({"Origin": "http://evil.example"}, path=path).allowed

    def test_api_v1_with_bearer_token(self):
        d = _decide({"Origin": "http://evil.example", "Authorization": "Bearer abc123"},
                    path="/api/v1/progress")
        assert d.allowed and d.reason == "exempt"

    def test_api_v1_without_bearer_is_checked(self):
        """Riding the session cookie / single-user mode on /api/v1 is not exempt."""
        d = _decide({"Origin": "http://evil.example"}, path="/api/v1/progress")
        assert not d.allowed

    @pytest.mark.parametrize("auth", ["Bearer ", "Bearer    ", "Basic dXNlcjpwYXNz", "bearer abc", "Token abc"])
    def test_api_v1_with_non_bearer_or_empty_token_is_checked(self, auth):
        d = _decide({"Origin": "http://evil.example", "Authorization": auth},
                    path="/api/v1/progress")
        assert not d.allowed

    def test_bearer_outside_api_v1_is_checked(self):
        """Only /api/v1 authenticates by bearer; elsewhere the cookie still rides."""
        d = _decide({"Origin": "http://evil.example", "Authorization": "Bearer abc"},
                    path="/api/config/page-width")
        assert not d.allowed

    def test_opds_basic_auth_is_not_exempt(self):
        """A browser replays cached Basic credentials cross-site, so they are
        ambient like a cookie (and OPDS has no state-changing routes)."""
        d = _decide({"Origin": "http://evil.example", "Authorization": "Basic dXNlcjpwYXNz"},
                    path="/opds/x")
        assert not d.allowed


class TestFetchMetadata:
    @pytest.mark.parametrize("value", ["same-origin", "none", "Same-Origin"])
    def test_allowed(self, value):
        # Even with a mismatched Origin: the browser's verdict wins.
        d = _decide({"Sec-Fetch-Site": value, "Origin": "http://evil.example"})
        assert d.allowed and d.reason == "fetch-metadata"

    def test_cross_site_refused(self):
        d = _decide({"Sec-Fetch-Site": "cross-site", "Origin": "http://clu.lan:5577"})
        assert not d.allowed and d.reason == "cross-site"

    def test_same_site_needs_matching_origin(self):
        assert not _decide({"Sec-Fetch-Site": "same-site",
                            "Origin": "http://other.lan:5577"}).allowed
        assert _decide({"Sec-Fetch-Site": "same-site",
                        "Origin": "http://clu.lan:5577"}).allowed

    def test_same_site_without_origin_refused(self):
        d = _decide({"Sec-Fetch-Site": "same-site"})
        assert not d.allowed and d.reason == "no-origin"

    def test_unknown_value_treated_like_same_site(self):
        assert not _decide({"Sec-Fetch-Site": "weird"}).allowed
        assert _decide({"Sec-Fetch-Site": "weird", "Origin": "http://clu.lan:5577"}).allowed


class TestOriginComparison:
    def test_origin_equal_to_host(self):
        d = _decide({"Origin": "http://clu.lan:5577"})
        assert d.allowed and d.reason == "same-host"

    def test_scheme_is_not_compared(self):
        assert _decide({"Origin": "https://clu.lan:5577"}).allowed

    def test_case_insensitive(self):
        assert _decide({"Origin": "http://CLU.LAN:5577"}).allowed

    def test_different_host_refused(self):
        d = _decide({"Origin": "http://evil.example"})
        assert not d.allowed and d.reason == "host-mismatch"
        assert d.origin == "evil.example"

    def test_different_port_refused(self):
        assert not _decide({"Origin": "http://clu.lan:8080"}).allowed

    def test_default_port_normalised(self):
        hosts = ["comics.example.com"]
        assert _decide({"Origin": "https://comics.example.com"}, hosts=hosts).allowed
        assert _decide({"Origin": "https://comics.example.com:443"}, hosts=hosts).allowed
        assert _decide({"Origin": "http://comics.example.com:80"}, hosts=hosts).allowed
        assert _decide({"Origin": "https://comics.example.com"},
                       hosts=["comics.example.com:443"]).allowed

    def test_any_own_host_value_matches(self):
        """Host header, ProxyFix's host, or an X-Forwarded-Host value."""
        hosts = ["clu:5577", "comics.example.com", "proxy.internal"]
        assert _decide({"Origin": "https://comics.example.com"}, hosts=hosts).allowed
        assert _decide({"Origin": "http://clu:5577"}, hosts=hosts).allowed
        assert _decide({"Origin": "http://proxy.internal"}, hosts=hosts).allowed

    def test_null_origin_refused(self):
        d = _decide({"Origin": "null"})
        assert not d.allowed and d.reason == "null-origin"

    def test_null_origin_refused_even_with_good_referer(self):
        assert not _decide({"Origin": "null", "Referer": "http://clu.lan:5577/"}).allowed

    def test_unparseable_origin_refused(self):
        d = _decide({"Origin": "not a url"})
        assert not d.allowed and d.reason == "unparseable"

    def test_extension_origin_refused_off_the_exempt_path(self):
        assert not _decide({"Origin": "chrome-extension://abcdef"}).allowed

    def test_origin_wins_over_referer(self):
        assert not _decide({"Origin": "http://evil.example",
                            "Referer": "http://clu.lan:5577/files"}).allowed


class TestRefererOnly:
    def test_matching_referer(self):
        d = _decide({"Referer": "http://clu.lan:5577/collection?page=2"})
        assert d.allowed and d.source == "Referer"

    def test_mismatched_referer(self):
        d = _decide({"Referer": "http://evil.example/attack.html"})
        assert not d.allowed and d.source == "Referer"

    def test_relative_referer_refused(self):
        assert not _decide({"Referer": "/collection"}).allowed


class TestNoBrowserHeaders:
    def test_allowed(self):
        """curl, scripts, the monitor, OPDS readers: CSRF needs a browser."""
        d = _decide({})
        assert d.allowed and d.reason == "no-browser-headers"

    def test_empty_values_count_as_absent(self):
        assert _decide({"Origin": "", "Referer": "", "Sec-Fetch-Site": ""}).allowed


class TestTrustedOrigins:
    def test_parse(self):
        hosts = parse_trusted_origins(
            "https://comics.example.com, clu.home:8443 ,, HTTP://Other.Example:80")
        assert hosts == frozenset({"comics.example.com", "clu.home:8443", "other.example"})

    def test_parse_empty(self):
        assert parse_trusted_origins("") == frozenset()

    def test_trusted_entry_accepted(self):
        trusted = parse_trusted_origins("https://comics.example.com")
        d = _decide({"Origin": "https://comics.example.com"}, trusted=trusted)
        assert d.allowed and d.reason == "trusted"

    def test_untrusted_still_refused(self):
        trusted = parse_trusted_origins("https://comics.example.com")
        assert not _decide({"Origin": "http://evil.example"}, trusted=trusted).allowed

    def test_does_not_override_cross_site_fetch_metadata(self):
        trusted = parse_trusted_origins("https://comics.example.com")
        assert not _decide({"Sec-Fetch-Site": "cross-site",
                            "Origin": "https://comics.example.com"}, trusted=trusted).allowed

    def test_read_from_environment(self, monkeypatch):
        monkeypatch.setenv(TRUSTED_ORIGINS_ENV, "https://comics.example.com")
        assert trusted_origins() == frozenset({"comics.example.com"})
        monkeypatch.delenv(TRUSTED_ORIGINS_ENV)
        assert trusted_origins() == frozenset()


class TestRefusalMessage:
    @pytest.mark.parametrize("headers", [
        {"Origin": "http://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "null"},
        {"Origin": "garbage"},
    ])
    def test_names_cause_and_remedy(self, headers):
        msg = refusal_message(_decide(headers), "clu.lan:5577")
        assert "Cross-site request refused" in msg
        assert TRUSTED_ORIGINS_ENV in msg

    def test_mismatch_names_both_hosts(self):
        msg = refusal_message(_decide({"Origin": "http://evil.example"}), "clu.lan:5577")
        assert "evil.example" in msg and "clu.lan:5577" in msg

    def test_control_characters_are_not_echoed(self):
        msg = refusal_message(_decide({"Origin": "bad\nvalue"}), "clu.lan")
        assert "\n" not in msg
