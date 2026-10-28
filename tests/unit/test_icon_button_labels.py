"""Every icon-only button has a name a screen reader can say.

An audit found 107 buttons and links in templates and page scripts whose only
content was a Bootstrap icon, with no aria-label: a screen reader announced
"button" and nothing else (a title alone is not read reliably, and is never
shown on touch). They are labelled now; this keeps a new one from shipping
bare. The scan reads markup in templates and in JS template strings.

A button whose text a script fills in (an empty <span id=...>) counts as
labelled, and so does one built with createElement, which this scan does not
see: those set aria-label next to their title.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
TAG = re.compile(r"<(button|a)\b([^>]*)>(.*?)</\1>", re.S | re.I)


def _visible_text(inner):
    s = re.sub(r"<span[^>]*\b(visually-hidden|id=)[^>]*>.*?</span>", "TEXT", inner, flags=re.S)
    s = re.sub(r"\{#.*?#\}|\{%.*?%\}", "", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"&[a-z]+;|&#\d+;", " ", s)
    return s.strip()


def _unlabelled():
    files = sorted(list((ROOT / "templates").rglob("*.html")) + list((ROOT / "static" / "js").glob("*.js")))
    for f in files:
        text = f.read_text(encoding="utf-8")
        for m in TAG.finditer(text):
            tag, attrs, inner = m.groups()
            if tag.lower() == "a" and "btn" not in attrs:
                continue
            if "<i" not in inner:
                continue
            if re.search(r"aria-label(ledby)?\s*=", attrs):
                continue
            if _visible_text(inner):  # has text (static or ${...}/{{ ... }})
                continue
            line = text.count("\n", 0, m.start()) + 1
            yield f"{f.relative_to(ROOT)}:{line}"


def test_icon_only_buttons_have_a_label():
    offenders = list(_unlabelled())
    assert offenders == [], (
        "icon-only button without aria-label (add aria-label and a matching title):\n  "
        + "\n  ".join(offenders))


def test_password_toggles_say_what_they_will_do():
    config = (ROOT / "templates" / "config.html").read_text(encoding="utf-8")
    assert config.count('class="btn btn-outline-secondary toggle-password"') == config.count(
        'aria-label="Show password" title="Show password" class="btn btn-outline-secondary toggle-password"')
    assert config.count("'Show password' : 'Hide password'") == 2


def test_status_row_actions_are_buttons():
    status = (ROOT / "templates" / "status.html").read_text(encoding="utf-8")
    # Clickable <i> icons cannot be reached from the keyboard.
    assert "action-icon" not in status
    assert "function actionButton(" in status and "btn.setAttribute('aria-label', label)" in status
    # The table is rebuilt every second; focus must survive it.
    assert "dataset.focusKey" in status and "again.focus()" in status
    # Narrow screens drop provider and byte counts rather than scroll sideways.
    assert status.count("d-none d-md-table-cell") >= 4


def test_library_toolbar_is_one_row_on_desktop():
    html = (ROOT / "templates" / "metadata_browser.html").read_text(encoding="utf-8")
    toolbar = html[html.index('role="toolbar" aria-label="Library view"'):html.index('id="mbLetterFilter"')]
    # View, sort, page size, refresh and search share one row; no divider row.
    for needle in ('name="mbAxis"', 'id="mbSort"', 'id="mbPerPage"', 'id="mbRefreshBtn"', 'id="mbSearch"'):
        assert needle in toolbar
    assert "border-top" not in html[html.index("mb-toolbar"):html.index('id="mbGrid"')]
    assert '<label class="visually-hidden" for="mbSearch">' in html
