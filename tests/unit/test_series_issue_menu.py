"""Structural checks for the series page's per-issue "More options" menu.

The menu is rendered twice: server-side in templates/series.html and rebuilt
by refreshCollectionStatus() in static/js/series.js. Both copies must agree.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
COPIES = ("templates/series.html", "static/js/series.js")


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_every_issue_row_dropdown_escapes_the_table_overflow():
    # The issue table sits in .table-responsive (overflow: auto), which clipped
    # an absolutely positioned dropdown on a short list: both the found row's
    # "More options" and the wanted row's "Mark issue" split toggle. A fixed
    # Popper strategy positions the menu against the viewport instead.
    for rel in COPIES:
        src = _read(rel)
        toggles = [
            m.group(0)
            for m in re.finditer(r"<button\b[^>]*>", src, re.S)
            if 'data-bs-toggle="dropdown"' in m.group(0)
        ]
        assert len(toggles) >= 2, rel
        for toggle in toggles:
            assert "data-bs-popper-config='{\"strategy\":\"fixed\"}'" in toggle, (rel, toggle)


def test_menu_offers_fetch_metadata():
    for rel in COPIES:
        src = _read(rel)
        assert "fetchIssueMetadata(this.closest('[data-issue-path]').dataset.issuePath)" in src, rel
        assert "Fetch Metadata" in src, rel


def test_fetch_metadata_uses_the_shared_cascade():
    js = _read("static/js/series.js")
    body = js[js.index("function fetchIssueMetadata"):]
    body = body[:body.index("\n}\n")]
    assert "window._cluMetadata" in body
    assert "CLU.searchMetadata(" in body
    assert "CLU.maybeRenameAfterMetadata(" in body


def test_page_loads_the_shared_metadata_modals_once():
    html = _read("templates/series.html")
    assert "partials/modal_metadata_select.html" in html
    assert "clu-metadata.js" in html
    # The partial owns these ids; an inline copy would duplicate them.
    assert 'id="gcdSeriesModal"' not in html
    assert 'id="comicVineVolumeModal"' not in html
