"""Runs static/js/clu-pager.js under node against a minimal fake DOM, so the
pager's behaviour (not just its wiring) is pinned. Skipped where node is not
installed; tests/unit/test_client_pagination_wiring.py covers the wiring."""

import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];

let replaceCalls = 0;
const timers = [];
const sandbox = {
  URLSearchParams,
  Date,
  setTimeout: (fn, ms) => { timers.push(fn); return timers.length; },
  location: { pathname: '/pull-list', search: '?page=3&q=bat', hash: '' },
  history: {
    replaceState(_s, _t, url) {
      replaceCalls++;
      const q = url.indexOf('?');
      sandbox.location.search = q === -1 ? '' : url.slice(q);
    },
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

const items = Array.from({ length: 10 }, (_, i) => ({
  id: i, dataset: { match: i % 2 ? '0' : '1' }, style: { display: '' },
}));
let q = 'bat';
const pager = sandbox.CLU.createClientPager({
  items: () => items, nav: null, defaultPerPage: 2,
  urlState: () => ({ q, tag: [] }),
});
pager.refresh();
const out = {};

if (scenario === 'scope') {
  out.page_shown = items.filter(i => i.style.display === '').map(i => i.id);
  out.matching = pager.matching().map(i => i.id);
  out.on_page = items.filter(i => pager.isOnPage(i)).map(i => i.id);
}
if (scenario === 'writes') {
  out.after_first = replaceCalls;          // URL already says page=3, q=bat
  for (let n = 0; n < 50; n++) pager.refresh();
  out.after_unchanged = replaceCalls;
  for (let n = 0; n < 50; n++) { q = 'bat' + n; pager.refresh(); }
  out.during_burst = replaceCalls;
  timers.splice(0).forEach(fn => fn());
  out.after_flush = replaceCalls;
  out.search = sandbox.location.search;
}
console.log(JSON.stringify(out));
"""


def _run(scenario, tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    res = subprocess.run(
        [NODE, str(harness), os.path.join(ROOT, "static/js/clu-pager.js"), scenario],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_matching_spans_pages_while_display_shows_one(tmp_path):
    out = _run("scope", tmp_path)
    # Even ids match; 2 per page; page 3 of [0,2,4,6,8] is [8]
    assert out["page_shown"] == [8]
    assert out["on_page"] == [8]
    assert out["matching"] == [0, 2, 4, 6, 8]


def test_url_written_only_when_it_changes_and_coalesced(tmp_path):
    out = _run("writes", tmp_path)
    assert out["after_first"] == 0, "the URL already held this state"
    assert out["after_unchanged"] == 0
    # A burst of 50 changes writes at most once now plus one trailing write
    assert out["during_burst"] <= 1
    assert out["after_flush"] <= 2
    assert "q=bat49" in out["search"]
