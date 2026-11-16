"""The reading-list page renders a stored description through the sanitizer.

Rows written by earlier versions went through an older sanitizer, so the page
must sanitize on read: these tests store the description directly, bypassing
the import path, and check what the page actually serves.
"""

import re

import pytest


HOSTILE = (
    '<p onclick="alert(1)">Intro <b>bold</b></p>'
    '<a href="javascript:alert(1)">js</a>'
    '<a href="java&#x09;script:alert(2)">tab</a>'
    '<a href="JaVaScRiPt:alert(3)">case</a>'
    '<a href="data:text/html,x">data</a>'
    '<a href="vbscript:msgbox(1)">vb</a>'
    '<script>alert(4)</script>'
    '<svg onload="alert(5)"><script>alert(6)</script></svg>'
    '<style>body{display:none}</style>'
    '<iframe src="https://evil.example/"></iframe>'
    '<a href="/batman/4005-1699/">Batman</a>'
    '<a href="https://metron.cloud/arc/1/">Metron</a>'
    '<b>unterminated <img src=x onerror="alert(7)"'
)


def _description_block(html):
    m = re.search(r'<div class="mb-0 text-muted reading-list-description">(.*?)</div>',
                  html, re.DOTALL)
    assert m, "description block not rendered"
    return m.group(1)


@pytest.fixture
def hostile_list(app):
    from core.database import create_reading_list, update_reading_list_description
    list_id = create_reading_list("Hostile description")
    assert update_reading_list_description(list_id, HOSTILE)
    return list_id


class TestReadingListDescriptionRender:

    def test_hostile_description_is_neutralised(self, client, hostile_list):
        resp = client.get(f"/reading-lists/{hostile_list}")
        assert resp.status_code == 200
        block = _description_block(resp.get_data(as_text=True))

        lowered = block.lower()
        for needle in ("<script", "<svg", "<style", "<iframe", "<img",
                       "onclick", "onload", "onerror", "javascript:",
                       "vbscript:", "data:"):
            assert needle not in lowered, (needle, block)
        assert "alert(4)" not in block and "alert(6)" not in block

    def test_legitimate_formatting_kept(self, client, hostile_list):
        block = _description_block(
            client.get(f"/reading-lists/{hostile_list}").get_data(as_text=True))
        assert "<p>Intro <b>bold</b></p>" in block
        assert ('<a href="https://comicvine.gamespot.com/batman/4005-1699/" '
                'target="_blank" rel="noopener noreferrer nofollow">Batman</a>') in block
        assert ('<a href="https://metron.cloud/arc/1/" target="_blank" '
                'rel="noopener noreferrer nofollow">Metron</a>') in block
        # Every refused href leaves a bare, inert anchor around its text.
        assert "<a>js</a>" in block and "<a>vb</a>" in block

    def test_unterminated_tag_closed(self, client, hostile_list):
        block = _description_block(
            client.get(f"/reading-lists/{hostile_list}").get_data(as_text=True))
        assert block.count("<b>") == block.count("</b>")
        assert block.count("<a") == block.count("</a>")

    def test_plain_text_description_escaped(self, client, app):
        from core.database import create_reading_list, update_reading_list_description
        list_id = create_reading_list("Plain")
        update_reading_list_description(list_id, "Tom & Jerry <3")
        block = _description_block(
            client.get(f"/reading-lists/{list_id}").get_data(as_text=True))
        assert block == "Tom &amp; Jerry &lt;3"

    def test_filter_is_registered_and_returns_markup(self, app):
        from markupsafe import Markup
        f = app.jinja_env.filters["sanitize_description"]
        out = f('<a href="javascript:x" onclick="y">z</a>')
        assert isinstance(out, Markup)
        assert out == "<a>z</a>"
