"""Page width: the two save routes and the width as it reaches the page.

/api/config/page-width sets the site default (owner-only by its path);
/api/account/page-width is any user's own override. Both are in routes/auth.py
beside the theme, whose fallback chain they copy: personal override, else
site default, else auto.
"""
import re

import pytest

from core.database import create_user, get_user_setting_override, set_user_setting
from core.settings import get_setting, set_setting


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


def _uid(username):
    from core.database import get_user_by_username
    return get_user_by_username(username)["id"]


def _shell(html):
    return re.search(r'<div class="clu-shell ([^"]*)">', html).group(1)


def _nav_container(html):
    nav = html[html.index("<nav class=\"navbar"):]
    return re.search(r'<div class="([^"]*)">', nav).group(1)


@pytest.fixture(autouse=True)
def _users(db_connection, monkeypatch):
    monkeypatch.delenv("CLU_USERNAME", raising=False)
    monkeypatch.delenv("CLU_PASSWORD", raising=False)
    create_user("owner", password="ownerpass", role="owner")
    create_user("reader", password="readerpass", role="reader")
    create_user("reader2", password="reader2pass", role="reader")
    yield


# -- Site default ------------------------------------------------------------

class TestSiteDefault:
    def test_owner_saves_site_default(self, client):
        _login(client, "owner", "ownerpass")
        resp = client.post("/api/config/page-width", json={"pageWidth": "wide"})

        assert resp.status_code == 200
        assert resp.get_json() == {"success": True, "pageWidth": "wide"}
        assert get_setting("PAGE_WIDTH") == "wide"

    def test_unknown_value_rejected_and_not_stored(self, client):
        _login(client, "owner", "ownerpass")
        resp = client.post("/api/config/page-width", json={"pageWidth": "huge"})

        assert resp.status_code == 400
        assert get_setting("PAGE_WIDTH") == "auto"

    def test_saving_site_default_clears_owners_own_override(self, client):
        set_user_setting("page_width", "constrained", user_id=_uid("owner"))
        _login(client, "owner", "ownerpass")
        client.post("/api/config/page-width", json={"pageWidth": "wide"})

        assert get_user_setting_override("page_width", user_id=_uid("owner")) is None

    def test_reader_cannot_set_site_default(self, client):
        _login(client, "reader", "readerpass")
        resp = client.post("/api/config/page-width", json={"pageWidth": "wide"})

        assert resp.status_code == 403
        assert get_setting("PAGE_WIDTH") == "auto"


# -- Personal override -------------------------------------------------------

class TestAccountOverride:
    def test_reader_saves_own_width(self, client):
        _login(client, "reader", "readerpass")
        resp = client.post("/api/account/page-width", json={"pageWidth": "constrained"})

        assert resp.status_code == 200
        assert resp.get_json()["override"] is True
        assert get_user_setting_override("page_width", user_id=_uid("reader")) == "constrained"
        # Nobody else's width, and not the site default.
        assert get_user_setting_override("page_width", user_id=_uid("reader2")) is None
        assert get_setting("PAGE_WIDTH") == "auto"

    def test_unknown_value_rejected(self, client):
        _login(client, "reader", "readerpass")
        resp = client.post("/api/account/page-width", json={"pageWidth": "huge"})

        assert resp.status_code == 400
        assert get_user_setting_override("page_width", user_id=_uid("reader")) is None

    def test_use_site_default_clears_override(self, client):
        set_setting("PAGE_WIDTH", "wide")
        set_user_setting("page_width", "constrained", user_id=_uid("reader"))
        _login(client, "reader", "readerpass")

        resp = client.post("/api/account/page-width", json={"useSiteDefault": True})

        body = resp.get_json()
        assert body["override"] is False
        assert body["pageWidth"] == "wide"
        assert get_user_setting_override("page_width", user_id=_uid("reader")) is None

    def test_unauthenticated_gets_401(self, client):
        assert client.post(
            "/api/account/page-width", json={"pageWidth": "wide"}
        ).status_code == 401

    def test_account_page_shows_the_picker(self, client):
        set_setting("PAGE_WIDTH", "wide")
        _login(client, "reader", "readerpass")
        html = client.get("/account").get_data(as_text=True)

        assert 'id="pageWidthStateBadge"' in html
        assert re.search(r'id="accountPageWidth-wide" value="wide"\s+checked', html)
        assert "(Wide)" in html


# -- Rendered width ----------------------------------------------------------

class TestRenderedWidth:
    def test_auto_keeps_the_per_page_widths(self, client):
        _login(client, "reader", "readerpass")

        plain = client.get("/reading-lists").get_data(as_text=True)
        grid = client.get("/collection").get_data(as_text=True)

        assert _shell(plain) == "container-lg"
        assert _nav_container(plain) == "container-lg"
        assert _shell(grid) == "container-fluid clu-shell-wide"
        assert _nav_container(grid) == "container-fluid clu-shell-wide-chrome"

    def test_wide_widens_an_ordinary_page(self, client):
        set_setting("PAGE_WIDTH", "wide")
        _login(client, "reader", "readerpass")
        html = client.get("/reading-lists").get_data(as_text=True)

        assert _shell(html) == "container-fluid clu-shell-wide"
        assert _nav_container(html) == "container-fluid clu-shell-wide-chrome"

    def test_constrained_narrows_the_collection(self, client):
        set_setting("PAGE_WIDTH", "constrained")
        _login(client, "reader", "readerpass")
        html = client.get("/collection").get_data(as_text=True)

        assert _shell(html) == "container-lg"
        assert _nav_container(html) == "container-lg"

    def test_override_beats_site_default_for_that_user_only(self, client):
        set_setting("PAGE_WIDTH", "wide")
        set_user_setting("page_width", "constrained", user_id=_uid("reader"))

        _login(client, "reader", "readerpass")
        mine = client.get("/reading-lists").get_data(as_text=True)
        client.get("/logout")
        _login(client, "reader2", "reader2pass")
        theirs = client.get("/reading-lists").get_data(as_text=True)

        assert _shell(mine) == "container-lg"
        assert _shell(theirs) == "container-fluid clu-shell-wide"

    def test_page_shell_block_is_not_printed(self, client):
        """base.html defines page_shell inside {% if false %}; its text must
        only ever reach the page through the shell's class attribute."""
        _login(client, "reader", "readerpass")
        html = client.get("/collection").get_data(as_text=True)

        assert html.count("container-fluid clu-shell-wide\"") == 1
