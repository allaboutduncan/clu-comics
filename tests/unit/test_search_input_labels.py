"""Every search or filter box has an accessible name.

A placeholder is not a label: it disappears as soon as you type, and screen
readers announce it inconsistently or not at all. An audit found about twenty
search boxes named only by their placeholder (Pull List, Publishers, Problem
Files, Timeline, the source-search modals, the boxes /collection and /files
inject from JS...). This is the ratchet: a text or search <input> that looks
like a search or filter box must carry aria-label, aria-labelledby, or a
<label for> in the same file.

"Looks like a search box" is type="search", or "search", "filter" or "query"
in its id, class or placeholder. Other text fields (folder names, tags,
tokens) are not covered here.
"""

import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

INPUT = re.compile(r"<input\b[^>]*>", re.S | re.I)
SEARCHY = re.compile(r"search|filter|query", re.I)
NON_TEXT = re.compile(
    r'type=["\'](hidden|checkbox|radio|number|date|file|password|email|url|range|color|submit|button)["\']',
    re.I)


def _attr(tag, name):
    m = re.search(r'(?<![\w-])' + name + r'\s*=\s*"([^"]*)"', tag) or \
        re.search(r"(?<![\w-])" + name + r"\s*=\s*'([^']*)'", tag)
    return m.group(1) if m else ""


def _unlabelled(path):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    label_for = set(re.findall(r'<label\b[^>]*\bfor\s*=\s*["\']([^"\']+)["\']', src))
    found = []
    for m in INPUT.finditer(src):
        tag = m.group(0)
        if NON_TEXT.search(tag):
            continue
        is_search = re.search(r'type=["\']search["\']', tag, re.I) or any(
            SEARCHY.search(_attr(tag, a)) for a in ("id", "class", "placeholder"))
        if not is_search:
            continue
        if re.search(r"(?<![\w-])aria-label(ledby)?\s*=", tag):
            continue
        if _attr(tag, "id") and _attr(tag, "id") in label_for:
            continue
        line = src[:m.start()].count("\n") + 1
        found.append(f"{os.path.relpath(path, ROOT)}:{line}  {tag[:90]}")
    return found


def _files():
    pats = ["templates/**/*.html", "static/js/*.js"]
    for p in pats:
        yield from glob.glob(os.path.join(ROOT, p), recursive=True)


def test_search_and_filter_inputs_have_an_accessible_name():
    missing = [hit for f in _files() for hit in _unlabelled(f)]
    assert not missing, (
        "search/filter input named only by its placeholder "
        "(add aria-label, or a <label for>):\n  " + "\n  ".join(missing))


def test_the_check_sees_a_placeholder_only_box(tmp_path):
    f = tmp_path / "page.html"
    f.write_text('<input type="text" id="q" placeholder="Search series...">\n'
                 '<input type="search" id="ok" aria-label="Search">\n'
                 '<label for="lab">Find</label><input type="text" id="lab" placeholder="Filter">\n'
                 '<input type="text" id="folderName" placeholder="Folder name">\n')
    hits = _unlabelled(str(f))
    assert len(hits) == 1 and 'id="q"' in hits[0]
