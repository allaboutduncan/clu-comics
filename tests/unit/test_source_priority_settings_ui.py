"""Settings -> Download Clients: the Source Priority list and the client cards.

Being in ``download_source_priority`` is what turns a source on for
auto-download. The list used to start from a hard-coded
``['getcomics', 'usenet', 'dcpp']`` and append any source missing from the
stored value, so the first reorder saved all three and silently switched
Usenet and DC++ on. The page's JS cannot run in tests, so this reads it.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _config_html():
    with open(os.path.join(ROOT, "templates", "config.html"), encoding="utf-8") as f:
        return f.read()


def _function(src, name):
    start = src.index(f"function {name}(")
    nxt = re.search(r"\n    (async )?function \w+\(", src[start + 1:])
    return src[start:start + 1 + nxt.start()] if nxt else src[start:]


def test_order_comes_from_the_server_default():
    body = _function(_config_html(), "loadSourcePriority")
    assert "/api/download-clients/sources" in body
    assert "['getcomics', 'usenet', 'dcpp']" not in body
    # Nothing is appended to the enabled list: off sources are rendered apart.
    assert "order.push" not in body


def test_each_source_has_an_on_switch():
    src = _config_html()
    assert "toggleSource(" in _function(src, "loadSourcePriority")
    toggle = _function(src, "toggleSource")
    assert "saveSourcePriority(order)" in toggle
    assert "!order.length" in toggle  # the last source cannot be switched off


def test_client_cards_show_a_saved_secret_as_a_hint():
    """GET /api/download-clients no longer returns api_key/password."""
    body = _function(_config_html(), "generateClientFields")
    assert "secrets_set" in body
