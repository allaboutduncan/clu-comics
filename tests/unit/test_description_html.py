"""The reading-list description sanitizer (core/description_html.py).

A description is provider/import HTML rendered as markup on the list page, so
the sanitizer is an allow-list re-serialiser. These tests pin the corpus it
must neutralise, the formatting it must keep, and the structural rule that a
reading-list template only renders a description through the filter.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from core.description_html import safe_href, sanitize_description_html as clean
from core.reading_list_sync import _sanitize_html

REPO = Path(__file__).resolve().parents[2]

REL = 'rel="noopener noreferrer nofollow"'

_ALLOWED = {"p", "br", "b", "strong", "i", "em", "u", "ul", "ol", "li", "a",
            "blockquote", "h2", "h3", "h4"}


class _Audit(HTMLParser):
    """Parse sanitizer output the way a browser would and record what is live."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in _ALLOWED:
            self.problems.append(f"tag {tag}")
        for name, value in attrs:
            if tag == "a" and name == "href":
                if not re.match(r"^(https?|mailto):", value or "", re.IGNORECASE):
                    self.problems.append(f"href {value!r}")
            elif tag == "a" and name == "target" and value == "_blank":
                pass
            elif tag == "a" and name == "rel" and value == "noopener noreferrer nofollow":
                pass
            else:
                self.problems.append(f"attr {tag}.{name}={value!r}")
        if tag != "br":
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag != "br":
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag == "br":
            return
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"unbalanced </{tag}>")
        else:
            self.stack.pop()

    def handle_comment(self, data):
        self.problems.append("comment")

    def handle_decl(self, decl):
        self.problems.append("decl")

    def handle_pi(self, data):
        self.problems.append("pi")

    def unknown_decl(self, data):
        self.problems.append("unknown decl")


def _assert_inert(out):
    audit = _Audit()
    audit.feed(out)
    audit.close()
    assert not audit.problems, (audit.problems, out)
    assert not audit.stack, ("unclosed", audit.stack, out)
    # No raw "<" outside a tag the sanitizer emitted itself.
    for m in re.finditer(r"<(/?)([^\s>/]*)", out):
        assert m.group(2) in _ALLOWED, out


HREF_ATTACKS = [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    " javascript:alert(1)",
    "\tjavascript:alert(1)",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    "java\x00script:alert(1)",
    "\x01javascript:alert(1)",
    "java&#x09;script:alert(1)",
    "java&#9;script:alert(1)",
    "&#106;avascript:alert(1)",
    "&#x6A;avascript:alert(1)",
    "javascript&colon;alert(1)",
    "jav&#x0A;ascript:alert(1)",
    "vbscript:msgbox(1)",
    "VBScript:msgbox(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "DATA:text/html,<script>alert(1)</script>",
    " data:text/html,x",
    "//evil.example/x",
    "/\\evil.example/x",
    "/\t/evil.example/x",
    "file:///etc/passwd",
    "ftp://example.com/",
    "relative/path",
    "",
]


class TestHrefAttacks:

    @pytest.mark.parametrize("href", HREF_ATTACKS)
    def test_hostile_href_dropped(self, href):
        out = clean(f'<a href="{href}">x</a>')
        assert out == "<a>x</a>", out

    @pytest.mark.parametrize("href", HREF_ATTACKS)
    def test_hostile_href_single_quoted_and_bare(self, href):
        out = clean(f"<a href='{href}'>x</a>")
        assert out == "<a>x</a>", out
        # Unquoted: whitespace ends the value, so only check nothing is live.
        _assert_inert(clean(f"<a href={href}>y</a>"))

    def test_safe_href_contract(self):
        assert safe_href(None) is None
        assert safe_href(123) is None
        assert safe_href("javascript:x") is None
        assert safe_href("https://a.example/") == "https://a.example/"
        assert safe_href("/x/4005-1/") == "https://comicvine.gamespot.com/x/4005-1/"


class TestAllowedLinks:

    @pytest.mark.parametrize("href", [
        "https://metron.cloud/arc/1/",
        "http://example.com/a?b=1&c=2",
        "HTTPS://Example.com/",
        "mailto:someone@example.com",
    ])
    def test_allowed_scheme_kept(self, href):
        out = clean(f'<a href="{href}">link</a>')
        _assert_inert(out)
        assert out.startswith('<a href="')
        assert REL in out
        assert 'target="_blank"' in out
        assert out.endswith(">link</a>")

    def test_query_ampersand_escaped_once(self):
        out = clean('<a href="https://x.example/?a=1&amp;b=2">q</a>')
        assert 'href="https://x.example/?a=1&amp;b=2"' in out

    def test_comicvine_relative_path_made_absolute(self):
        out = clean('<a href="/batman/4005-1699/" data-ref-id="4005-1699">Batman</a>')
        assert out == ('<a href="https://comicvine.gamespot.com/batman/4005-1699/" '
                       f'target="_blank" {REL}>Batman</a>')

    def test_quote_in_href_cannot_break_out(self):
        out = clean('<a href="https://x.example/&quot; onmouseover=&quot;alert(1)">q</a>')
        _assert_inert(out)
        assert "&quot;" in out


class TestAttributes:

    @pytest.mark.parametrize("markup", [
        '<p onclick="alert(1)">t</p>',
        '<b onmouseover=alert(1)>t</b>',
        '<a href="https://ok.example/" onclick="alert(1)">t</a>',
        '<p style="background:url(javascript:alert(1))">t</p>',
        '<i class="x" id="y" title="z">t</i>',
        '<p ONLOAD="alert(1)">t</p>',
    ])
    def test_attributes_dropped(self, markup):
        out = clean(markup)
        _assert_inert(out)
        assert "=" not in out.replace(f'href="https://ok.example/" target="_blank" {REL}', "")


class TestStructure:

    @pytest.mark.parametrize("markup", [
        '<b>unterminated <a href="javascript:x" onclick="y"',
        '<img src=x onerror=alert(1)',
        '<a href="https://ok.example/"',
        '<p>open <b>bold <i>italic',
        '<p><b>a<i>b</p>c</i></b>',
        '</p></a></b>stray closers',
        '<scr<script>ipt>alert(1)</script>',
        '<<script>script>alert(1)<</script>/script>',
        '<a<a href="javascript:x">>y</a>',
        '<p <script>alert(1)</script>>t</p>',
        '<a href="https://ok.example/"><a href="javascript:x">nested</a></a>',
        '<ul><li>one<li>two</ul>',
        '<b>' * 2000 + 'deep',
        '<!--><script>alert(1)</script>-->',
        '<![CDATA[<script>alert(1)</script>]]>',
        '<?xml version="1.0"?><p>t</p>',
        '<!DOCTYPE html><p>t</p>',
    ])
    # pytest resets the module-level filter in core/description_html.py.
    @pytest.mark.filterwarnings("ignore::bs4.XMLParsedAsHTMLWarning")
    def test_malformed_is_inert_and_balanced(self, markup):
        out = clean(markup)
        _assert_inert(out)
        assert clean(out) == out

    def test_unterminated_tag_never_live(self):
        out = clean('text <a href="javascript:alert(1)" onclick="alert(2)"')
        assert "<a" not in out
        assert out.startswith("text")

    def test_unclosed_allowed_tags_are_closed(self):
        assert clean("<p>open <b>bold") == "<p>open <b>bold</b></p>"

    def test_stray_closers_dropped(self):
        assert clean("</p></a>text</b>") == "text"


class TestDroppedContent:

    @pytest.mark.parametrize("markup", [
        "<script>alert(1)</script>",
        "<SCRIPT type='text/javascript'>alert(1)</SCRIPT>",
        "<style>body{background:url(javascript:alert(1))}</style>",
        "<iframe src='https://evil.example/'>inner</iframe>",
        "<object data='x.swf'>inner</object>",
        "<embed src='x.swf'>",
        "<svg onload=alert(1)><circle/></svg>",
        "<svg><script>alert(1)</script></svg>",
        "<math><mtext><script>alert(1)</script></mtext></math>",
        "<template><script>alert(1)</script></template>",
        "<noscript><img src=x onerror=alert(1)></noscript>",
        "<textarea><script>alert(1)</script></textarea>",
        "<form action='javascript:x'><input value='x'></form>",
    ])
    def test_content_removed(self, markup):
        out = clean(markup + "after")
        assert out == "after", out

    def test_unknown_tags_unwrapped(self):
        assert clean("<div><span>kept</span> <font>text</font></div>") == "kept text"

    def test_comments_dropped(self):
        assert clean("a<!-- secret -->b") == "ab"


class TestText:

    def test_text_escaped(self):
        assert clean("1 < 2 & 3 > 2") == "1 &lt; 2 &amp; 3 &gt; 2"

    def test_entities_round_trip(self):
        assert clean("&lt;script&gt;alert(1)&lt;/script&gt;") == \
            "&lt;script&gt;alert(1)&lt;/script&gt;"

    def test_quotes_in_text_kept(self):
        assert clean('He said "hi" & left') == 'He said "hi" &amp; left'

    def test_empty(self):
        assert clean(None) == ""
        assert clean("") == ""


class TestLegitimateFormatting:

    def test_comicvine_style_description_unchanged(self):
        src = ("<h2>Overview</h2><p>The <b>Crisis</b> begins in "
               "<em>Issue #1</em>.</p><ul><li>Part one</li><li><i>Part two</i></li></ul>"
               "<blockquote>A quote</blockquote><p>Line<br>break<br/>again</p>"
               "<h3>Notes</h3><ol><li><strong>Strong</strong> <u>under</u></li></ol>")
        expected = ("<h2>Overview</h2><p>The <b>Crisis</b> begins in "
                    "<em>Issue #1</em>.</p><ul><li>Part one</li><li><i>Part two</i></li></ul>"
                    "<blockquote>A quote</blockquote><p>Line<br>break<br>again</p>"
                    "<h3>Notes</h3><ol><li><strong>Strong</strong> <u>under</u></li></ol>")
        assert clean(src) == expected

    def test_plain_text_unchanged(self):
        assert clean("Just a plain description.") == "Just a plain description."

    def test_idempotent(self):
        src = '<p>See <a href="/x/4005-1/">X</a> and <a href="https://m.example/">M</a></p>'
        once = clean(src)
        assert clean(once) == once

    def test_import_alias_uses_new_sanitizer(self):
        assert _sanitize_html('<a href="javascript:x">y</a>') == "<a>y</a>"
        assert _sanitize_html(None) is None
        assert _sanitize_html("") == ""


class TestTemplatesUseTheFilter:
    """A reading-list description reaches a template only through the
    ``sanitize_description`` filter, which returns the sanitizer's output as
    Markup. ``|safe`` on a description is never needed and is refused."""

    def _templates(self):
        return sorted((REPO / "templates").rglob("*.html"))

    def test_no_description_rendered_with_safe(self):
        offenders = []
        for path in self._templates():
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r"\{\{[^}]*?description[^}]*?\}\}", text):
                expr = m.group(0)
                if re.search(r"\|\s*safe\b", expr):
                    offenders.append(f"{path.name}: {expr}")
        assert not offenders, offenders

    def test_reading_list_view_uses_filter(self):
        text = (REPO / "templates" / "reading_list_view.html").read_text(encoding="utf-8")
        exprs = re.findall(r"\{\{[^}]*reading_list\.description[^}]*\}\}", text)
        assert exprs, "reading_list_view.html no longer renders the description"
        for expr in exprs:
            assert "|sanitize_description" in expr.replace(" ", ""), expr

    def test_no_markup_or_autoescape_bypass_for_description(self):
        for path in self._templates():
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r"\{%-?\s*autoescape\s+false", text):
                block_end = text.find("endautoescape", m.end())
                assert "description" not in text[m.end():block_end], path.name

    def test_arc_pickers_escape_provider_descriptions(self):
        js = (REPO / "static" / "js" / "reading_list.js").read_text(encoding="utf-8")
        assert "function arcDescriptionSnippet" in js
        assert "${snippet}" not in js
        assert "<strong>${name}</strong>" not in js
