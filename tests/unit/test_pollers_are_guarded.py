"""Ratchet: a recurring fetch goes through CLU.startPoll, never a bare setInterval.

A bare ``setInterval(() => fetch(...))`` fires whether or not the last request
came back. Behind a slow reverse proxy the requests pile up until the browser's
per-origin connection pool is full, and the tab freezes. ``CLU.startPoll``
(templates/base.html) skips a tick while one is in flight, aborts a request
that hangs, and pauses in a background tab.

Five pollers bypassed it: two with no guard at all (the bulk-metadata progress
poll, at one request a second, and the Library Health replacement poll) and
three with a hand-rolled ``busy`` flag that skipped overlap but never aborted
a hung request, so one stall stopped the poll for good.

So ``setInterval`` is allow-listed. A timer that fetches nothing is fine and
gets an entry here with its reason; a poller does not.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]

# file -> why each setInterval in it is not a poller.
ALLOWED = {
    "templates/base.html": "CLU.startPoll's own timer",
    "static/js/files.js": "drag-and-drop auto-scroll; touches only scrollTop",
    "templates/schedules.html": "the server clock, ticked locally from one instant (scheduled-times.md)",
}

_SET_INTERVAL = re.compile(r"(?<![\w.$])setInterval\(")


def _sources():
    for folder, pattern in (("static/js", "*.js"), ("templates", "*.html")):
        for path in sorted((ROOT / folder).rglob(pattern)):
            yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


def _counts():
    found = {}
    scanned = 0
    for rel, text in _sources():
        scanned += 1
        n = len(_SET_INTERVAL.findall(text))
        if n:
            found[rel] = n
    assert scanned > 50, "the walk found almost no sources"
    return found


def test_setinterval_only_where_allow_listed():
    extra = {rel: n for rel, n in _counts().items() if rel not in ALLOWED}
    assert not extra, (
        f"setInterval outside the allow-list: {extra}. A recurring fetch must use "
        "CLU.startPoll(fn, intervalMs); a timer that fetches nothing needs an entry "
        "in ALLOWED with its reason."
    )


def test_each_allowed_file_has_exactly_one():
    """An allow-listed file must not become a place to hide a second timer."""
    counts = _counts()
    assert {rel: counts.get(rel, 0) for rel in ALLOWED} == {rel: 1 for rel in ALLOWED}


def test_the_allow_listed_timers_do_not_fetch():
    for rel in ("static/js/files.js", "templates/schedules.html"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for m in _SET_INTERVAL.finditer(text):
            body = text[m.end():m.end() + 400]
            assert "fetch(" not in body, f"{rel}: this timer fetches; use CLU.startPoll"


def test_start_poll_keeps_its_three_guards():
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    body = base[base.index("CLU.startPoll = function"):]
    body = body[:body.index("return setInterval(tick, intervalMs);")]
    assert "if (inFlight || document.hidden) return;" in body   # overlap + hidden tab
    assert "new AbortController()" in body and "ctrl.abort()" in body  # hung request
    assert "fn(ctrl.signal)" in body                             # callers can pass it on


def test_converted_pollers_pass_the_abort_signal_to_fetch():
    """A guard that never aborts leaves the hung request holding a connection."""
    checks = {
        "static/js/bulk-metadata-review.js": "progress/${jobId}`, { signal }",
        "static/js/problem_files.js": "replacements/apply', null, signal)",
        "static/js/files.js": "encodeURIComponent(opId), { signal }",
        "templates/pull_list.html": "encodeURIComponent(opId), { signal }",
        "templates/config.html": "'Cache-Control': 'no-cache' }, signal }",
    }
    for rel, needle in checks.items():
        assert needle in (ROOT / rel).read_text(encoding="utf-8"), rel
