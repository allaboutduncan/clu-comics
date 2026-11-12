"""The collection grid loads a cover with one cacheable request.

It used to do GET + HEAD (``_check=<now>``) + GET (``_t=<now>``) for every
card, so a 100-card folder cost ~300 requests on every visit and no cover was
ever reused from the browser cache. An errored/skipped card (every CBR, every
PDF) was treated as "not ready" and polled every 2s for as long as the page was
open. ``loadThumbnail`` probes once with a plain fetch, polls only while the
server answers loading.svg, and stops on error.svg.

Asserted on the file as text, in the style of tests/unit/test_issue_badge.py.
"""
import pathlib
import re

import pytest

JS = (pathlib.Path(__file__).resolve().parents[2] / "static" / "js" / "collection.js").read_text(
    encoding="utf-8")


def _body(name):
    start = JS.find(f"function {name}(")
    assert start != -1, f"{name} not found"
    nxt = re.compile(r"^function ", re.M).search(JS, start + 1)
    return JS[start: nxt.start() if nxt else len(JS)]


def test_loader_exists_and_the_old_poll_is_gone():
    assert "function loadThumbnail(" in JS
    assert "pollThumbnail" not in JS


@pytest.mark.parametrize("buster", ["_check=", "_t=", "_refresh="])
def test_no_timestamp_cache_busters(buster):
    assert buster not in JS


def test_no_head_probe():
    assert "method: 'HEAD'" not in JS and 'method: "HEAD"' not in JS


def test_polls_only_on_loading_svg():
    body = _body("loadThumbnail")
    branch = body[body.index("_redirectedTo(response, 'loading.svg')"): body.index("'error.svg'")]
    assert "retry(2000)" in branch
    assert "setTimeout(" in body


def test_error_svg_is_terminal():
    body = _body("loadThumbnail")
    start = body.index("_redirectedTo(response, 'error.svg')")
    branch = body[start: body.index("return;", start)]
    assert "classList.remove('polling'" in branch
    assert "setTimeout" not in branch and "retry(" not in branch


def test_reload_bypasses_the_cache_and_frees_the_object_url():
    body = _body("loadThumbnail")
    assert "cache: reload ? 'reload' : 'default'" in body
    assert "URL.revokeObjectURL" in body


def test_grid_items_have_no_onload_poll():
    start = JS.index("img.classList.add('polling');")
    block = JS[start: JS.index("} else {", start)]
    assert "onload" not in block


@pytest.mark.parametrize("name", ["initLazyLoading", "initSwiperThumbnails"])
def test_call_sites_use_the_loader(name):
    assert "loadThumbnail(" in _body(name)


def test_refresh_forces_a_reload():
    assert "reload: true" in _body("refreshThumbnail")


def test_redirects_are_detected_on_the_final_path_not_a_substring():
    """The request URL carries ?path=<comic>, so a substring test would
    misread a comic whose path contains 'loading.svg'."""
    helper = _body("_redirectedTo")
    assert "response.redirected" in helper and "pathname" in helper
    assert "response.url.includes(" not in _body("loadThumbnail")


def test_a_reload_supersedes_a_probe_in_flight():
    body = _body("loadThumbnail")
    assert "loadGen" in body
    assert "_refreshedCovers" in body


def test_removed_cards_stop_polling():
    assert "isConnected" in _body("loadThumbnail")


def test_server_errors_retry_before_they_are_final():
    """A transient 5xx ("database is locked", a proxy 502/503 while the
    worker restarts) must not leave the cover broken for the page view, so it
    goes through retry() -- and is checked before the !response.ok branch
    that makes everything else final."""
    body = _body("loadThumbnail")
    start = body.index("response.status >= 500")
    assert start < body.index("!response.ok")
    branch = body[start: body.index("return;", start)]
    assert "retry(5000)" in branch


def test_server_error_retries_are_bounded():
    """A cover whose request always 500s must stop polling eventually."""
    body = _body("loadThumbnail")
    branch = body[body.index("response.status >= 500"):]
    branch = branch[: branch.index("retry(5000)")]
    assert "THUMBNAIL_5XX_RETRIES" in branch
    m = re.search(r"const THUMBNAIL_5XX_RETRIES = (\d+);", JS)
    assert m and 0 < int(m.group(1)) <= 30
