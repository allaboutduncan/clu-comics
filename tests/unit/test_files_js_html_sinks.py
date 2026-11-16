"""The File Manager renders file and folder names as text, never as HTML.

`static/js/files.js` lists whatever is on disk, and a name on disk is not
CLU's to trust: a download keeps the name the remote server gave it, and a
library can hold anything another tool wrote. Names used to be dropped into
``innerHTML`` template strings (the file list, the search results, the rename
previews, the metadata modals...), so a name containing markup was parsed as
markup.

files.js now builds those nodes with ``textContent`` / ``setAttribute`` (the
``domEl()`` helper and ``setNameWithSize``). This is the ratchet that keeps it so,
in the style of ``test_feedback.py``: it reads the source, because there is no
browser harness.

* Every HTML sink (``.innerHTML =``, ``.innerHTML +=``, ``.outerHTML =``,
  ``insertAdjacentHTML``) may interpolate only values that cannot carry text
  from the server or the filesystem: counts and sizes. The allow-list below is
  a list of *expression shapes*, not of call sites.
* No inline event-handler attribute (``onclick="..."``) is ever built with an
  interpolated value. A handler attribute is parsed twice (HTML, then JS), and
  no escaping helper in the codebase covers both.
* ``escapeHtml`` is not used inside an attribute value. ``CLU.escapeHtml``
  escapes quotes now (``test_escape_html.py``), but files.js builds every
  attribute with ``setAttribute`` and has no reason to start interpolating.
"""

import pathlib
import re

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
FILES_JS = PROJECT_ROOT / "static" / "js" / "files.js"

SINK_RE = re.compile(r"\.(?:innerHTML|outerHTML)\s*\+?=\s*|\.insertAdjacentHTML\s*\(")

# Interpolations an HTML sink may contain: numbers, never text.
SAFE_EXPRESSIONS = [
    re.compile(r"^[\w.]+\.length$"),                                    # data.items.length
    re.compile(r"^[\w.]+\.length\s*(?:!==|===|>|<)\s*\d+\s*\?\s*'[^'<>]*'\s*:\s*'[^'<>]*'$"),  # plural 's'
    re.compile(r"^[a-z]\w*Count$"),                                     # processedCount
    re.compile(r"^totalFailed$"),
    re.compile(r"^Math\.max\(0,\s*\w+Count\)$"),
    re.compile(r"^(?:CLU\.)?format(?:File)?Size\(\w+\)$"),              # a byte count
]

# A string that builds an inline handler attribute out of a value.
INLINE_HANDLER_RE = re.compile(
    r"""\son[a-z]+\s*=\s*\\?(?:"[^"]*?(?:\$\{|'\s*\+)|'[^']*?(?:\$\{|"\s*\+))""",
    re.IGNORECASE)

ESCAPE_IN_ATTRIBUTE_RE = re.compile(r"""=\s*["']\s*\$\{\s*(?:CLU\.)?escapeHtml\(""")


def _read():
    return FILES_JS.read_text(encoding="utf-8")


def _skip_string(src, i):
    """Index just past the string literal starting at ``src[i]``."""
    quote = src[i]
    i += 1
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if quote == "`" and src.startswith("${", i):
            i = _skip_braces(src, i + 1)
            continue
        if c == quote:
            return i + 1
        i += 1
    return i


def _skip_braces(src, i):
    """``src[i]`` is ``{``; index just past its matching ``}``."""
    depth = 0
    while i < len(src):
        c = src[i]
        if c in "'\"`":
            i = _skip_string(src, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def _statement_end(src, i):
    """End of the expression assigned at ``src[i]``: the first ``;`` (or the
    closing paren of an insertAdjacentHTML call) outside strings and brackets."""
    depth = 0
    while i < len(src):
        c = src[i]
        if c in "'\"`":
            i = _skip_string(src, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return i
            depth -= 1
        elif c == ";" and depth == 0:
            return i
        i += 1
    return i


def _interpolations(expr):
    """Every ``${...}`` in the template literals of ``expr``, and every bare
    operand concatenated onto a string literal with ``+``."""
    found = []
    i = 0
    while i < len(expr):
        c = expr[i]
        if c == "`":
            j = i + 1
            while j < len(expr) and expr[j] != "`":
                if expr[j] == "\\":
                    j += 2
                    continue
                if expr.startswith("${", j):
                    end = _skip_braces(expr, j + 1)
                    found.append(expr[j + 2:end - 1].strip())
                    j = end
                    continue
                j += 1
            i = j + 1
            continue
        if c in "'\"":
            i = _skip_string(expr, i)
            continue
        i += 1
    # Concatenation: strip literals, then whatever is left between `+` signs.
    stripped = re.sub(r"`(?:[^`\\]|\\.)*`|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", "''", expr)
    if "+" in stripped:
        for part in stripped.split("+"):
            part = part.strip()
            if part and part != "''":
                found.append(part)
    return found


def _sinks(src):
    for m in SINK_RE.finditer(src):
        start = m.end()
        end = _statement_end(src, start)
        line = src.count("\n", 0, m.start()) + 1
        yield line, src[start:end]


def _is_safe(expr):
    return any(p.match(expr) for p in SAFE_EXPRESSIONS)


def test_the_scanner_finds_the_sinks():
    """Guard the guard: a parser that sees nothing would pass silently."""
    sinks = list(_sinks(_read()))
    assert len(sinks) > 50
    assert any("${" in expr for _, expr in sinks), \
        "expected at least one count interpolated into a static template"


def test_html_sinks_interpolate_only_numbers():
    offenders = []
    for line, expr in _sinks(_read()):
        for value in _interpolations(expr):
            if not _is_safe(value):
                offenders.append(f"files.js:{line}: {value}")
    assert not offenders, (
        "files.js puts a value into HTML. A name, path, message or provider "
        "field must go through textContent / setAttribute (domEl(), "
        "setNameWithSize) instead:\n  " + "\n  ".join(offenders))


def test_no_inline_handler_is_built_from_a_value():
    src = _read()
    offenders = [
        f"files.js:{src.count(chr(10), 0, m.start()) + 1}: {m.group(0).strip()}"
        for m in INLINE_HANDLER_RE.finditer(src)
    ]
    assert not offenders, (
        "Bind handlers with addEventListener and pass values through closures "
        "or dataset, never through an onclick=\"...\" string:\n  "
        + "\n  ".join(offenders))


def test_escape_html_is_not_used_inside_an_attribute():
    src = _read()
    offenders = [
        f"files.js:{src.count(chr(10), 0, m.start()) + 1}"
        for m in ESCAPE_IN_ATTRIBUTE_RE.finditer(src)
    ]
    assert not offenders, (
        "escapeHtml does not cover attribute values; use setAttribute:\n  "
        + "\n  ".join(offenders))


@pytest.mark.parametrize("snippet, bad", [
    ("el.innerHTML = `<b>${fileData.name}</b>`;", ["fileData.name"]),
    ("el.innerHTML = '<b>' + item.path + '</b>';", ["item.path"]),
    ("el.innerHTML = `<span>${CLU.escapeHtml(dir)}</span>`;", ["CLU.escapeHtml(dir)"]),
    ("el.innerHTML = `Progress ${processedCount} / ${files.length}`;", []),
    ("el.innerHTML = `<i class=\"x\"></i>${error.message}`;", ["error.message"]),
    ("el.insertAdjacentHTML('beforeend', `<li>${name}</li>`);", ["name"]),
])
def test_the_checker_itself(snippet, bad):
    """The scanner flags names and passes counts (escapeHtml is not an
    exemption here: files.js has no reason to build name markup at all)."""
    found = [v for _, expr in _sinks(snippet) for v in _interpolations(expr)
             if not _is_safe(v)]
    assert found == bad


def test_inline_handler_pattern_itself():
    assert INLINE_HANDLER_RE.search("html = `<a onclick=\"go('${path}')\">`;")
    assert INLINE_HANDLER_RE.search("html = '<a onclick=\"go(\\'' + path + '\\')\">';")
    assert not INLINE_HANDLER_RE.search("'<button onclick=\"emptyTrash()\">Empty</button>'")


def test_names_with_punctuation_are_set_as_text():
    """The list row and the rename previews hand names to setNameWithSize /
    domEl(), which use textContent; nothing parses them."""
    src = _read()
    body = src[src.index("function setNameWithSize("):]
    body = body[:body.index("\n}\n")]
    assert "el.textContent = name" in body
    assert "innerHTML" not in body
    h_body = src[src.index("function domEl(tag, props, ...children)"):]
    h_body = h_body[:h_body.index("\n}\n")]
    assert "innerHTML" not in h_body
    assert "document.createTextNode" in h_body
