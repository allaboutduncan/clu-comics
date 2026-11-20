"""Ratchet: every route is named by a test.

CLAUDE.md: "Every new route in routes/ must have a corresponding test in
tests/routes/." Nothing checked it, and a scan found about thirty routes no
test mentioned. The ones in blueprints have tests now
(tests/routes/test_route_coverage_gaps.py); the ones still in app.py cannot be
imported in tests and sit on a list that may only shrink, as each one moves to
a blueprint.

"Named" is deliberately cheap: the longest literal stretch of the rule (the
part between ``<converters>``) has to appear somewhere under tests/. That
proves a test author looked at the route, not that the route is covered well,
and it costs nothing to keep true.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TESTS = os.path.join(ROOT, "tests")
THIS_FILE = os.path.abspath(__file__)

_RULE = re.compile(r"""@\w+\.route\(\s*["']([^"']+)["']""")

# Rules whose literal part is too short to search for ("/", "/<path>").
MIN_KEY = 4

# app.py routes with no test. app.py cannot be imported in tests, so each of
# these needs moving to a blueprint first. Remove an entry when its route gets
# a test; never add one for a new route -- put the route in routes/.
APP_PY_UNTESTED = {
    "/api/config/system-perf",
    "/api/metadata-scan-status",
    "/api/metadata-scan-trigger",
    "/api/recommendations",
    "/api/run-sync-now",
    "/api/scrape-index-count",
    "/api/version-check",
    "/clear-cache",
    "/gcd-db-status",
    "/gcd-import",
    "/gcd-status",
    "/manifest.json",
    "/restart",
    "/warmup-cache",
    "/watch-count",
}


def _rules(path):
    with open(path, encoding="utf-8") as fh:
        return _RULE.findall(fh.read())


def _key(rule):
    """The longest literal stretch of a rule, without a trailing slash."""
    return max(re.split(r"<[^>]+>", rule), key=len).rstrip("/")


def _test_text():
    chunks = []
    for base, dirs, files in os.walk(TESTS):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            path = os.path.join(base, name)
            if name.endswith(".py") and os.path.abspath(path) != THIS_FILE:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    chunks.append(fh.read())
    return "\n".join(chunks)


def _unnamed(rules, text):
    return sorted({r for r in rules if len(_key(r)) >= MIN_KEY and _key(r) not in text})


def _blueprint_rules():
    folder = os.path.join(ROOT, "routes")
    found = {}
    for name in sorted(os.listdir(folder)):
        if name.endswith(".py"):
            found["routes/" + name] = _rules(os.path.join(folder, name))
    return found


def test_every_blueprint_route_is_named_by_a_test():
    text = _test_text()
    by_file = _blueprint_rules()
    assert sum(len(r) for r in by_file.values()) > 300, "the route scan found almost nothing"
    missing = {f: _unnamed(rules, text) for f, rules in by_file.items()}
    missing = {f: r for f, r in missing.items() if r}
    assert not missing, (
        f"routes with no test: {missing}. Add one under tests/routes/ "
        "(CLAUDE.md: every new route in routes/ has a corresponding test)."
    )


def test_no_new_untested_route_in_app_py():
    unnamed = set(_unnamed(_rules(os.path.join(ROOT, "app.py")), _test_text()))
    new = sorted(unnamed - APP_PY_UNTESTED)
    assert not new, (
        f"app.py gained routes no test names: {new}. app.py cannot be imported in "
        "tests; put the route in a blueprint under routes/ and test it there."
    )


def test_the_app_py_list_is_tight():
    """An entry that got a test, or whose route moved or was deleted, comes off."""
    unnamed = set(_unnamed(_rules(os.path.join(ROOT, "app.py")), _test_text()))
    stale = sorted(APP_PY_UNTESTED - unnamed)
    assert not stale, f"remove from APP_PY_UNTESTED: {stale}"


def test_the_scan_can_fail():
    """Guard the guard: a made-up rule is reported, a tested one is not."""
    text = _test_text()
    assert _unnamed(["/api/zz-no-such-route/<int:x>/frobnicate"], text) == [
        "/api/zz-no-such-route/<int:x>/frobnicate"]
    assert _unnamed(["/api/reading-position"], text) == []
    assert _key("/api/read/<path:comic_path>/page/<int:page_num>/info") == "/api/read"
