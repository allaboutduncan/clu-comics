"""Allow-list sanitizer for reading-list descriptions.

A reading list's description comes from remote sources (Metron, ComicVine,
imported files) and is rendered as HTML on the list page, so it must only ever
reach the page through :func:`sanitize_description_html`.

The input is parsed into a tree by BeautifulSoup (already a dependency, using
the standard library ``html.parser`` builder) and the tree is re-serialised
from scratch. Nothing from the input is ever copied through as markup:

- only the tags in ``ALLOWED_TAGS`` are emitted, each with no attributes
  except ``href`` on ``<a>``;
- ``href`` must begin with ``http:``, ``https:`` or ``mailto:`` (or be a
  site-relative ComicVine path, which is made absolute), checked after the
  parser has decoded entities and after control characters are removed;
- every text node is escaped;
- the elements in ``DROP_CONTENT_TAGS`` are removed together with their
  content, any other tag is unwrapped (its children are kept);
- comments, CDATA, doctypes and processing instructions are dropped;
- every emitted open tag gets its close tag from the serialiser's own stack,
  so the output is balanced whatever the input looked like.

The function is idempotent, so it is safe to run on write and again on read
(descriptions already stored went through an older sanitizer).
"""

from html import escape
import re
import warnings

from bs4 import BeautifulSoup, NavigableString, Tag, XMLParsedAsHTMLWarning
from bs4.element import PreformattedString

from core.app_logging import app_logger

# A description that opens with "<?xml" is still parsed as HTML on purpose.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning,
                        module=r"core\.description_html")

# The formatting set descriptions are allowed to keep.
ALLOWED_TAGS = frozenset({
    "p", "br", "b", "strong", "i", "em", "u",
    "ul", "ol", "li", "a", "blockquote", "h2", "h3", "h4",
})

VOID_TAGS = frozenset({"br"})

# Removed together with everything inside them.
DROP_CONTENT_TAGS = frozenset({
    "script", "style", "iframe", "frame", "frameset", "object", "embed",
    "applet", "svg", "math", "template", "noscript", "noembed", "noframes",
    "textarea", "title", "xmp", "plaintext", "select", "option", "head",
    "link", "meta", "base", "canvas", "audio", "video", "picture", "source",
    "track", "img", "map", "area", "form", "input", "button",
})

LINK_REL = "noopener noreferrer nofollow"

# Relative ComicVine paths ("/batman/4005-1699/") are made absolute.
COMICVINE_BASE = "https://comicvine.gamespot.com"

_ALLOWED_SCHEME_RE = re.compile(r"^(?:https?|mailto):", re.IGNORECASE)
# C0 controls, space, DEL and C1 controls trimmed from the ends.
_EDGE_JUNK = "".join(chr(c) for c in range(0x00, 0x21)) + "\x7f"
# Control characters removed anywhere (browsers drop tab/CR/LF inside a URL;
# the rest are never legitimate).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe_href(value):
    """Return a safe absolute URL for ``value``, or None to drop the href.

    ``value`` is the attribute value as the parser decoded it, which is what a
    browser would resolve. Only a URL that literally starts with an allowed
    scheme after cleaning is kept, so obfuscated schemes cannot slip through.
    """
    if not isinstance(value, str):
        return None
    url = _CONTROL_RE.sub("", value.strip(_EDGE_JUNK))
    if not url:
        return None
    if url.startswith("/") and not url.startswith("//") and not url.startswith("/\\"):
        return COMICVINE_BASE + url
    if _ALLOWED_SCHEME_RE.match(url):
        return url
    return None


class _Close:
    __slots__ = ("markup",)

    def __init__(self, markup):
        self.markup = markup


def _open_tag(name, node):
    if name == "a":
        href = safe_href(node.get("href"))
        if href is None:
            return "<a>"
        return (f'<a href="{escape(href, quote=True)}" target="_blank" '
                f'rel="{LINK_REL}">')
    return f"<{name}>"


def _serialise(soup):
    out = []
    stack = list(reversed(list(soup.children)))
    while stack:
        item = stack.pop()
        if isinstance(item, _Close):
            out.append(item.markup)
            continue
        if isinstance(item, PreformattedString):
            # Comment, CData, Doctype, Declaration, ProcessingInstruction.
            continue
        if isinstance(item, NavigableString):
            out.append(escape(str(item), quote=False))
            continue
        if not isinstance(item, Tag):
            continue
        name = (item.name or "").lower()
        if name in DROP_CONTENT_TAGS:
            continue
        children = list(item.children)
        if name in ALLOWED_TAGS:
            if name in VOID_TAGS:
                out.append(f"<{name}>")
            else:
                out.append(_open_tag(name, item))
                stack.append(_Close(f"</{name}>"))
        stack.extend(reversed(children))
    return "".join(out)


def sanitize_description_html(value):
    """Reduce ``value`` to allow-listed, balanced, escaped HTML.

    Returns ``""`` for None/empty input. On any parser failure the whole value
    is returned as escaped text, never as markup.
    """
    if not value:
        return ""
    text = str(value)
    try:
        return _serialise(BeautifulSoup(text, "html.parser"))
    except Exception as e:  # pragma: no cover - defensive
        app_logger.warning(f"Description sanitizer fell back to plain text: {e}")
        return escape(text, quote=True)
