"""core.url_guard: destination checks and custom-header scoping for downloads.

No real network: DNS is answered by a patched ``_resolve`` and HTTP by a fake
transport adapter mounted on a real ``requests.Session``, so the redirect
machinery under test is requests' own.
"""
import ast
import pathlib

import pytest
import requests
from requests.adapters import BaseAdapter
from requests.models import Response

from core import url_guard
from core.url_guard import BlockedURLError, check_url

ROOT = pathlib.Path(__file__).resolve().parents[2]

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


@pytest.fixture
def allow_private(monkeypatch):
    """The owner's DOWNLOAD_ALLOW_PRIVATE_HOSTS, read through core.settings.get_bool."""
    import core.settings as settings

    state = {"on": False}
    real_get_bool = settings.get_bool

    def fake_get_bool(key, *a, **kw):
        if key == "DOWNLOAD_ALLOW_PRIVATE_HOSTS":
            return state["on"]
        return real_get_bool(key, *a, **kw)

    monkeypatch.setattr(settings, "get_bool", fake_get_bool)
    return state


@pytest.fixture
def dns(monkeypatch, allow_private):
    """Map hostname -> list of addresses; anything unmapped fails to resolve."""
    table = {}
    calls = []

    def fake_resolve(host, port):
        calls.append(host)
        if host not in table:
            raise OSError(f"no such host {host}")
        return list(table[host])

    monkeypatch.setattr(url_guard, "_resolve", fake_resolve)
    table["calls"] = calls
    return table


# ---------------------------------------------------------------------------
# Address classes
# ---------------------------------------------------------------------------

class TestBlockedAddresses:
    @pytest.mark.parametrize("address", [
        "127.0.0.1", "127.8.9.10",            # loopback
        "10.0.0.1", "172.16.5.4", "192.168.1.10",  # RFC1918
        "169.254.169.254", "169.254.1.1",     # link-local / cloud metadata
        "100.64.0.1", "100.127.255.254",      # CGNAT
        "0.0.0.0", "0.1.2.3",                 # unspecified / this network
        "224.0.0.1", "239.255.255.250",       # multicast
        "240.0.0.1", "255.255.255.255",       # reserved / broadcast
        "198.18.0.1",                         # benchmarking
        "192.0.0.8",
        "::1", "::",                          # IPv6 loopback / unspecified
        "fe80::1", "fe80::1%eth0",            # IPv6 link-local
        "fc00::1", "fd12:3456::1",            # unique local
        "ff02::1",                            # IPv6 multicast
        "::ffff:127.0.0.1", "::ffff:10.0.0.1",  # IPv4-mapped
        "::ffff:169.254.169.254",
        "::127.0.0.1",                        # IPv4-compatible
        "2002:7f00:1::1",                     # 6to4 around 127.0.0.1
        "2002:a00:1::1",                      # 6to4 around 10.0.0.1
        "64:ff9b::a00:1",                     # NAT64 around 10.0.0.1
    ])
    def test_blocked(self, address):
        assert url_guard.blocked_reason(address) is not None

    @pytest.mark.parametrize("address", [PUBLIC_V4, "8.8.8.8", "1.1.1.1", PUBLIC_V6,
                                         "::ffff:93.184.216.34"])
    def test_public(self, address):
        assert url_guard.blocked_reason(address) is None


class TestCheckUrl:
    @pytest.mark.parametrize("url", [
        "ftp://example.com/x.cbz", "file:///tmp/x.cbz", "gopher://x/",
        "javascript:alert(1)", "example.com/x.cbz", "", None, 42,
        "http:///nohost", "http://example.com:99999/",
    ])
    def test_bad_scheme_or_shape(self, dns, url):
        with pytest.raises(BlockedURLError):
            check_url(url)

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/", "http://localhost.localdomain/",
        "http://[::1]:8080/", "http://[::ffff:127.0.0.1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[fe80::1%25eth0]/", "https://10.1.2.3/x.cbz",
    ])
    def test_ip_literals_and_names_resolving_internal(self, dns, url):
        dns["localhost.localdomain"] = ["127.0.0.1"]
        with pytest.raises(BlockedURLError):
            check_url(url)

    @pytest.mark.parametrize("host", ["2130706433", "0x7f000001", "0177.0.0.1", "0x7f.1", "127.1"])
    def test_numeric_tricks_judged_by_what_resolves(self, dns, host):
        # These are not ip_address() literals, so they are resolved; the
        # platform resolver turns each into 127.0.0.1, and that is what counts.
        dns[host] = ["127.0.0.1"]
        with pytest.raises(BlockedURLError, match="loopback"):
            check_url(f"http://{host}/x.cbz")
        assert host in dns["calls"]

    def test_mixed_public_and_private_answers_are_refused(self, dns):
        dns["mixed.example"] = [PUBLIC_V4, "10.0.0.5"]
        with pytest.raises(BlockedURLError, match="private"):
            check_url("https://mixed.example/x.cbz")

    def test_mixed_public_and_mapped_v6_answers_are_refused(self, dns):
        dns["mixed6.example"] = [PUBLIC_V6, "::ffff:192.168.0.2"]
        with pytest.raises(BlockedURLError):
            check_url("https://mixed6.example/x.cbz")

    def test_unresolvable_is_refused(self, dns):
        with pytest.raises(BlockedURLError, match="resolve"):
            check_url("https://nowhere.example/x.cbz")

    def test_public_host_passes(self, dns):
        dns["getcomics.org"] = [PUBLIC_V4, PUBLIC_V6]
        assert check_url("https://GetComics.org./dls/abc") == "getcomics.org"

    def test_public_ip_literal_passes_without_dns(self, dns):
        assert check_url(f"http://{PUBLIC_V4}:8080/x") == PUBLIC_V4
        assert dns["calls"] == []


class TestAllowPrivateHostsSetting:
    def test_schema_entry(self):
        from core.settings import SETTINGS_SCHEMA
        s = SETTINGS_SCHEMA["DOWNLOAD_ALLOW_PRIVATE_HOSTS"]
        assert (s.pref, s.type, s.default, s.category) == (
            "download_allow_private_hosts", "bool", False, "downloads")

    def test_off_by_default_refuses_and_names_the_remedy(self, dns):
        dns["nas.lan"] = ["192.168.1.20"]
        with pytest.raises(BlockedURLError) as exc:
            check_url("http://nas.lan/x.cbz")
        assert "Settings" in str(exc.value)

    def test_read_per_call(self, dns, allow_private):
        dns["nas.lan"] = ["192.168.1.20"]
        with pytest.raises(BlockedURLError):
            check_url("http://nas.lan/x.cbz")
        allow_private["on"] = True
        assert check_url("http://nas.lan/x.cbz") == "nas.lan"
        allow_private["on"] = False
        with pytest.raises(BlockedURLError):
            check_url("http://nas.lan/x.cbz")

    def test_on_allows_private_literals_and_unresolvable_names(self, dns, allow_private):
        allow_private["on"] = True
        assert check_url("http://127.0.0.1:8080/x") == "127.0.0.1"
        assert check_url("http://198.18.0.5/x") == "198.18.0.5"
        assert check_url("https://split-dns.example/x") == "split-dns.example"
        assert dns["calls"] == []

    @pytest.mark.parametrize("url", ["ftp://nas.lan/x", "file:///tmp/x.cbz", "nas.lan/x",
                                     "http:///nohost", ""])
    def test_on_still_refuses_bad_schemes_and_shapes(self, dns, allow_private, url):
        allow_private["on"] = True
        with pytest.raises(BlockedURLError):
            check_url(url)

    def test_on_still_strips_custom_headers_for_unknown_hosts(self, dns, allow_private):
        allow_private["on"] = True
        assert url_guard.scope_headers(FULL, "http://192.168.1.20/x", BASIC) == BASIC
        s, adapter = _session({"http://nas.lan/x": (200, {})}, FULL, BASIC)
        s.get("http://nas.lan/x")
        assert "CF-Access-Client-Id" not in adapter.seen[0][1]

    def test_a_failed_settings_read_keeps_the_check_on(self, dns, monkeypatch):
        import core.settings as settings

        def boom(*_a, **_kw):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(settings, "get_bool", boom)
        with pytest.raises(BlockedURLError):
            check_url("http://127.0.0.1/")

    def test_not_frozen_into_a_module_constant(self):
        src = (ROOT / "core" / "url_guard.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                assert "get_bool" not in ast.unparse(node)


class TestDownloadApiSaveWiring:
    """The switch is saved by the owner-only /api/config/download-api route."""

    def test_route_writes_the_setting_only_when_sent(self):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "save_download_api_config")
        src = ast.unparse(fn)
        assert "'downloadAllowPrivateHosts' in data" in src
        assert "set_setting('DOWNLOAD_ALLOW_PRIVATE_HOSTS'" in src

    def test_route_is_owner_only(self, monkeypatch):
        from flask import Flask
        from core.auth import required_role_for_request
        app = Flask(__name__)
        with app.test_request_context("/api/config/download-api", method="POST"):
            assert required_role_for_request() == "owner"

    def test_config_page_has_the_switch_and_sends_it(self):
        html = (ROOT / "templates" / "config.html").read_text(encoding="utf-8")
        assert 'id="downloadAllowPrivateHosts"' in html
        assert "Allow downloads from private network addresses" in html
        assert "downloadAllowPrivateHosts: document.getElementById('downloadAllowPrivateHosts')?.checked" in html


# ---------------------------------------------------------------------------
# Header scoping
# ---------------------------------------------------------------------------

BASIC = {"User-Agent": "UA-basic"}
FULL = {"User-Agent": "UA-basic", "CF-Access-Client-Id": "id", "CF-Access-Client-Secret": "s"}


class TestScopeHeaders:
    def test_custom_items(self):
        assert url_guard.custom_header_items(FULL, BASIC) == {
            "CF-Access-Client-Id": "id", "CF-Access-Client-Secret": "s"}
        assert url_guard.custom_header_items(BASIC, BASIC) == {}

    @pytest.mark.parametrize("url", [
        "https://getcomics.org/dls/x", "https://pixeldrain.com/u/abc",
        "https://box01.comicbookplus.com/dload/?f=1", "https://mega.nz/file/x",
        "https://mega.co.nz/#!x", "https://dl.comicfiles.ru/x.cbz",
    ])
    def test_known_hosts_keep_custom_headers(self, url):
        assert url_guard.scope_headers(FULL, url, BASIC) == FULL

    @pytest.mark.parametrize("url", [
        "https://evil.example/x.cbz",
        "https://pixeldrain.com.evil.example/x",
        "https://evil.example/?u=pixeldrain.com",
        "https://notgetcomics.org/x",
    ])
    def test_other_hosts_get_basic_headers(self, url):
        assert url_guard.scope_headers(FULL, url, BASIC) == BASIC

    def test_custom_user_agent_falls_back_to_basic(self):
        full = {"User-Agent": "custom-ua", "X-Token": "t"}
        assert url_guard.scope_headers(full, "https://evil.example/", BASIC) == BASIC

    def test_cross_host_hop_drops_custom_headers_even_between_known_hosts(self):
        out = url_guard.scope_headers(
            FULL, "https://pixeldrain.com/api/file/x", BASIC,
            origin="https://getcomics.org/dls/x")
        assert out == BASIC

    def test_same_host_hop_keeps_them(self):
        out = url_guard.scope_headers(
            FULL, "https://getcomics.org/dlds/y", BASIC,
            origin="https://getcomics.org/dls/x")
        assert out == FULL

    def test_internal_headers_unchanged(self):
        assert url_guard.scope_headers(BASIC, "https://evil.example/", BASIC) == BASIC


# ---------------------------------------------------------------------------
# guard_session: every request and every redirect hop
# ---------------------------------------------------------------------------

class FakeAdapter(BaseAdapter):
    """Answers from a {url: (status, headers)} table and records what it saw."""

    def __init__(self, routes):
        super().__init__()
        self.routes = routes
        self.seen = []

    def send(self, request, **kwargs):
        self.seen.append((request.url, dict(request.headers)))
        status, headers = self.routes.get(request.url, (404, {}))
        resp = Response()
        resp.status_code = status
        resp.headers.update(headers)
        resp.url = request.url
        resp.request = request
        resp._content = b"payload"
        return resp

    def close(self):
        pass


def _session(routes, full=None, basic=None):
    s = requests.Session()
    adapter = FakeAdapter(routes)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.clear()
    s.headers.update(full or {})
    url_guard.guard_session(s, full, basic)
    return s, adapter


class TestGuardSession:
    def test_first_request_to_internal_host_never_sent(self, dns):
        s, adapter = _session({})
        with pytest.raises(BlockedURLError):
            s.get("http://169.254.169.254/latest/meta-data/")
        assert adapter.seen == []

    def test_public_redirect_to_internal_is_refused_before_the_hop(self, dns):
        dns["files.example"] = [PUBLIC_V4]
        s, adapter = _session({
            "https://files.example/x.cbz": (302, {"Location": "http://127.0.0.1:5577/config"}),
        })
        with pytest.raises(BlockedURLError):
            s.get("https://files.example/x.cbz")
        assert [u for u, _ in adapter.seen] == ["https://files.example/x.cbz"]

    def test_redirect_to_name_resolving_internal_is_refused(self, dns):
        dns["files.example"] = [PUBLIC_V4]
        dns["internal.example"] = ["192.168.1.20"]
        s, adapter = _session({
            "https://files.example/a": (301, {"Location": "https://files.example/b"}),
            "https://files.example/b": (302, {"Location": "https://internal.example/c"}),
        })
        with pytest.raises(BlockedURLError):
            s.get("https://files.example/a")
        assert [u for u, _ in adapter.seen] == ["https://files.example/a",
                                               "https://files.example/b"]

    def test_public_redirect_chain_is_followed(self, dns):
        dns["getcomics.org"] = [PUBLIC_V4]
        dns["pixeldrain.com"] = [PUBLIC_V4]
        s, adapter = _session({
            "https://getcomics.org/dls/x": (302, {"Location": "https://pixeldrain.com/u/abc"}),
            "https://pixeldrain.com/u/abc": (200, {}),
        })
        r = s.get("https://getcomics.org/dls/x")
        assert r.status_code == 200
        assert len(adapter.seen) == 2

    def test_custom_headers_only_to_known_hosts(self, dns):
        dns["evil.example"] = [PUBLIC_V4]
        dns["pixeldrain.com"] = [PUBLIC_V4]
        s, adapter = _session({"https://evil.example/x": (200, {}),
                               "https://pixeldrain.com/x": (200, {})}, FULL, BASIC)
        s.get("https://evil.example/x")
        s.get("https://pixeldrain.com/x")
        to_evil, to_pd = adapter.seen[0][1], adapter.seen[1][1]
        assert "CF-Access-Client-Id" not in to_evil
        assert to_evil["User-Agent"] == "UA-basic"
        assert to_pd["CF-Access-Client-Id"] == "id"

    def test_custom_headers_dropped_on_cross_host_redirect(self, dns):
        dns["comicbookplus.com"] = [PUBLIC_V4]
        dns["box01.comicbookplus.com"] = [PUBLIC_V4]
        s, adapter = _session({
            "https://comicbookplus.com/x": (302, {"Location": "https://box01.comicbookplus.com/y"}),
            "https://box01.comicbookplus.com/y": (200, {}),
        }, FULL, BASIC)
        s.get("https://comicbookplus.com/x")
        assert adapter.seen[0][1]["CF-Access-Client-Secret"] == "s"
        assert "CF-Access-Client-Secret" not in adapter.seen[1][1]

    def test_custom_headers_dropped_on_redirect_to_unknown_host(self, dns):
        dns["pixeldrain.com"] = [PUBLIC_V4]
        dns["cdn.example"] = [PUBLIC_V4]
        s, adapter = _session({
            "https://pixeldrain.com/x": (302, {"Location": "https://cdn.example/y"}),
            "https://cdn.example/y": (200, {}),
        }, FULL, BASIC)
        s.get("https://pixeldrain.com/x")
        assert "CF-Access-Client-Id" not in adapter.seen[1][1]

    def test_caller_value_for_same_name_is_kept(self, dns):
        dns["evil.example"] = [PUBLIC_V4]
        full = {"User-Agent": "custom-ua", "X-Token": "t"}
        s, adapter = _session({"https://evil.example/x": (200, {})}, full, BASIC)
        s.headers["User-Agent"] = "scraper-ua"
        s.get("https://evil.example/x")
        sent = adapter.seen[0][1]
        assert sent["User-Agent"] == "scraper-ua"
        assert "X-Token" not in sent


# ---------------------------------------------------------------------------
# download_headers: the user's custom_headers, read per download
# ---------------------------------------------------------------------------

@pytest.fixture
def settings_db(tmp_path):
    """A real settings store, written the way the settings page writes it."""
    from unittest.mock import patch

    import core.settings as cs
    db_path = str(tmp_path / "headers.db")
    with patch("core.database.get_db_path", return_value=db_path):
        from core.database import init_db
        init_db()
        cs.invalidate()
        yield cs
        cs.invalidate()


class TestDownloadHeaders:
    def test_unset_is_basic_only(self, settings_db):
        assert url_guard.download_headers(BASIC) == BASIC

    def test_does_not_mutate_basic(self, settings_db):
        settings_db.set_setting("HEADERS", '{"X-Token": "a"}')
        basic = dict(BASIC)
        url_guard.download_headers(basic)
        assert basic == BASIC

    def test_an_edit_reaches_the_next_download_without_reimport(self, settings_db):
        settings_db.set_setting("HEADERS", '{"CF-Access-Client-Id": "id-1"}')
        assert url_guard.download_headers(BASIC)["CF-Access-Client-Id"] == "id-1"
        settings_db.set_setting("HEADERS", '{"CF-Access-Client-Id": "id-2"}')
        assert url_guard.download_headers(BASIC)["CF-Access-Client-Id"] == "id-2"

    def test_removing_a_header_removes_it(self, settings_db):
        settings_db.set_setting("HEADERS", '{"X-Keep": "k", "X-Drop": "d"}')
        assert "X-Drop" in url_guard.download_headers(BASIC)
        settings_db.set_setting("HEADERS", '{"X-Keep": "k"}')
        out = url_guard.download_headers(BASIC)
        assert "X-Drop" not in out and out["X-Keep"] == "k"
        settings_db.set_setting("HEADERS", "")
        assert url_guard.download_headers(BASIC) == BASIC

    def test_values_are_stripped(self, settings_db):
        settings_db.set_setting("HEADERS", '{" X-Token ": " t "}')
        assert url_guard.download_headers(BASIC)["X-Token"] == "t"

    def test_a_custom_user_agent_overrides_basic(self, settings_db):
        settings_db.set_setting("HEADERS", '{"User-Agent": "custom-ua"}')
        assert url_guard.download_headers(BASIC) == {"User-Agent": "custom-ua"}

    @pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"a string"', '{"X": 1}'])
    def test_bad_values_are_ignored(self, settings_db, raw):
        settings_db.set_setting("HEADERS", raw)
        assert url_guard.download_headers(BASIC) == BASIC

    def test_a_bad_value_warns_once_not_per_download(self, settings_db, monkeypatch):
        import logging
        warnings = []
        monkeypatch.setattr(logging.getLogger("monitor_logger"), "warning",
                            lambda msg, *a, **k: warnings.append(msg))
        monkeypatch.setattr(url_guard, "_last_rejected_custom_headers", None)
        settings_db.set_setting("HEADERS", "{broken")
        for _ in range(5):
            url_guard.download_headers(BASIC)
        assert len(warnings) == 1
        settings_db.set_setting("HEADERS", "[]")
        url_guard.download_headers(BASIC)
        assert len(warnings) == 2

    def test_a_failed_settings_read_sends_basic_only(self, monkeypatch):
        import core.settings as settings

        def boom(*_a, **_kw):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(settings, "get_setting", boom)
        assert url_guard.download_headers(BASIC) == BASIC

    def test_edits_and_removals_reach_the_wire_with_scoping_intact(self, settings_db, dns):
        """Two downloads either side of a settings edit, through guard_session."""
        dns["pixeldrain.com"] = [PUBLIC_V4]
        dns["evil.example"] = [PUBLIC_V4]
        routes = {"https://pixeldrain.com/x": (200, {}), "https://evil.example/x": (200, {})}

        settings_db.set_setting("HEADERS", '{"X-Keep": "k", "X-Drop": "d"}')
        first = url_guard.download_headers(BASIC)
        s1, a1 = _session(routes, first, BASIC)
        s1.get("https://pixeldrain.com/x")
        s1.get("https://evil.example/x")
        assert a1.seen[0][1]["X-Drop"] == "d"
        # #744 scoping: custom headers never reach an unknown host.
        assert "X-Keep" not in a1.seen[1][1] and "X-Drop" not in a1.seen[1][1]

        settings_db.set_setting("HEADERS", '{"X-Keep": "k2"}')
        second = url_guard.download_headers(BASIC)
        s2, a2 = _session(routes, second, BASIC)
        s2.get("https://pixeldrain.com/x")
        s2.get("https://evil.example/x")
        assert a2.seen[0][1]["X-Keep"] == "k2"
        assert "X-Drop" not in a2.seen[0][1]
        assert "X-Keep" not in a2.seen[1][1]
        assert url_guard.scope_headers(second, "https://evil.example/x", BASIC) == BASIC


# ---------------------------------------------------------------------------
# api.py call sites (api.py cannot be imported in tests)
# ---------------------------------------------------------------------------

def _api_tree():
    return ast.parse((ROOT / "api.py").read_text(encoding="utf-8"))


def _function(name):
    for node in ast.walk(_api_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"api.{name} not found")


def _guard_calls(node, attr):
    return [
        c for c in ast.walk(node)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
        and c.func.attr == attr
        and isinstance(c.func.value, ast.Name) and c.func.value.id == "url_guard"
    ]


class TestApiWiring:
    def test_download_route_checks_the_link_before_queueing(self):
        fn = _function("download")
        checks = _guard_calls(fn, "check_url")
        assert checks, "POST /download must check the link"
        puts = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
                and ast.unparse(c.func) == "download_queue.put"]
        assert puts and checks[0].lineno < puts[0].lineno

    def test_every_attempt_is_checked_before_it_is_resolved_or_routed(self):
        fn = _function("process_download")
        checks = _guard_calls(fn, "check_url")
        assert checks
        resolve = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
                   and ast.unparse(c.func) == "resolve_final_url"]
        assert resolve and checks[0].lineno < resolve[0].lineno
        assert "try_url" in ast.unparse(checks[0])

    def test_resolver_checks_each_hop_outside_its_try(self):
        fn = _function("resolve_final_url")
        hop_loop = next(n for n in fn.body if isinstance(n, ast.For))
        first = hop_loop.body[0]
        assert isinstance(first, ast.Expr)
        assert ast.unparse(first.value) == "url_guard.check_url(current)"

    def test_resolver_sends_scoped_headers_only(self):
        fn = _function("resolve_final_url")
        for call in ast.walk(fn):
            if isinstance(call, ast.Call) and ast.unparse(call.func) in ("requests.head", "requests.get"):
                kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
                assert kw.get("headers") == "hop_hdrs"
                assert kw.get("allow_redirects") == "False"
        assert _guard_calls(fn, "scope_headers")

    @pytest.mark.parametrize("name", ["download_getcomics", "_requests_session"])
    def test_download_sessions_are_guarded(self, name):
        assert _guard_calls(_function(name), "guard_session")


class TestApiReadsCustomHeadersPerDownload:
    """custom_headers used to be read once at import, before init_db()."""

    def test_no_module_level_header_state(self):
        for node in _api_tree().body:
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.If, ast.Try, ast.Expr)):
                src = ast.unparse(node)
                assert "custom_headers" not in src, src
                assert "HEADERS" not in src, src
            if isinstance(node, ast.Assign):
                names = {t.id for t in node.targets if isinstance(t, ast.Name)}
                assert not names & {"headers", "default_headers", "custom_headers_str"}

    def test_full_headers_reads_through_url_guard(self):
        src = ast.unparse(_function("_full_headers"))
        assert "url_guard.download_headers(basic_headers)" in src

    def test_process_download_reads_them_per_task(self):
        src = ast.unparse(_function("process_download"))
        assert "use_headers = basic_headers if internal else _full_headers()" in src

    @pytest.mark.parametrize("name", [
        "resolve_final_url", "download_getcomics", "download_pixeldrain",
        "download_comicbookplus",
    ])
    def test_fallbacks_read_now(self, name):
        fn = _function(name)
        for default in fn.args.defaults + fn.args.kw_defaults:
            if default is not None:
                assert ast.unparse(default) != "headers"
        assert "hdrs = _full_headers()" in ast.unparse(fn)

    @pytest.mark.parametrize("name", ["download_getcomics", "_requests_session"])
    def test_sessions_are_guarded_for_this_downloads_headers(self, name):
        calls = _guard_calls(_function(name), "guard_session")
        assert calls
        for call in calls:
            assert [ast.unparse(a) for a in call.args[1:]] == ["hdrs", "basic_headers"]
