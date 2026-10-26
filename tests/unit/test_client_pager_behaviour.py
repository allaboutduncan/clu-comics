"""Runs CLU.createServerPager (static/js/clu-pager.js) under node with a stub
fetch, so the pager's behaviour (not just its wiring) is pinned. Skipped
where node is not installed; tests/unit/test_client_pagination_wiring.py
covers the wiring."""

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
const requests = [];   // {url, resolve, signal}
const sandbox = {
  URLSearchParams,
  AbortController,
  Date,
  console,
  setTimeout: (fn, ms) => { timers.push(fn); return timers.length; },
  location: { pathname: '/pull-list', search: '?page=3&q=bat', hash: '' },
  history: {
    replaceState(_s, _t, url) {
      replaceCalls++;
      const q = url.indexOf('?');
      sandbox.location.search = q === -1 ? '' : url.slice(q);
    },
  },
  fetch(url, opts) {
    return new Promise((resolve, reject) => {
      const signal = opts && opts.signal;
      if (signal) signal.addEventListener('abort', () => {
        const e = new Error('aborted'); e.name = 'AbortError'; reject(e);
      });
      requests.push({ url, signal, resolve: body => resolve({ ok: true, json: () => Promise.resolve(body) }) });
    });
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

let q = 'bat';
const seen = [];
const errors = [];
const pager = sandbox.CLU.createServerPager({
  endpoint: '/api/pull-list/rows', nav: null, defaultPerPage: 50,
  initial: { page: 3, perPage: 50, totalPages: 9 },
  state: () => ({ q, tag: [] }),
  onData: data => seen.push(data.html),
  onError: err => errors.push(err.message),
});
const answer = (page, html) => ({ success: true, html, total: 500, page, per_page: 50, total_pages: 9 });
const tick = () => new Promise(r => setImmediate(r));
const out = {};

(async () => {
  if (scenario === 'writes') {
    // The server-rendered page already matches the URL: nothing to write.
    for (let n = 0; n < 50; n++) {
      pager.load();
      requests[requests.length - 1].resolve(answer(3, 'p3'));
      await tick();
    }
    out.after_unchanged = replaceCalls;
    for (let n = 0; n < 50; n++) {
      q = 'bat' + n;
      pager.load({ resetPage: true });
      requests[requests.length - 1].resolve(answer(1, 'p1'));
      await tick();
    }
    out.during_burst = replaceCalls;
    timers.splice(0).forEach(fn => fn());
    out.after_flush = replaceCalls;
    out.search = sandbox.location.search;
  }
  if (scenario === 'abort') {
    // A slow answer to an older load must not land after a newer one.
    q = 'b';  pager.load({ resetPage: true });
    q = 'ba'; pager.load({ resetPage: true });
    out.first_aborted = requests[0].signal.aborted;
    requests[1].resolve(answer(1, 'new'));
    await tick();
    requests[0].resolve(answer(1, 'old'));
    await tick();
    out.seen = seen;
    out.urls = requests.map(r => r.url);
  }
  if (scenario === 'foreign') {
    sandbox.location.search = '?q=bat&page=3&automap_op=x';
    pager.load({ resetPage: true });
    requests[requests.length - 1].resolve(answer(1, 'p1'));
    await tick(); await tick();
    timers.splice(0).forEach(fn => fn());
    out.search = sandbox.location.search;
  }
  if (scenario === 'failed') {
    pager.goTo(5);
    requests[0].resolve({ success: false, error: 'boom' });
    await tick(); await tick();
    out.page = pager.page();
    out.seen = seen;
  }
  if (scenario === 'failed_reset') {
    // A filter change resets to page 1; when it fails, the rows on screen
    // are still page 3's, so the page number and URL must say so.
    q = 'x';
    pager.load({ resetPage: true });
    requests[0].resolve({ success: false, error: 'boom' });
    await tick(); await tick();
    timers.splice(0).forEach(fn => fn());
    out.page = pager.page();
    out.search = sandbox.location.search;
    out.errors = errors.slice();
    out.seen = seen.slice();
    // The next load that lands re-syncs everything to the current filter.
    pager.load({ resetPage: true });
    requests[1].resolve(answer(1, 'x1'));
    await tick(); await tick();
    timers.splice(0).forEach(fn => fn());
    out.page_after = pager.page();
    out.search_after = sandbox.location.search;
  }
  if (scenario === 'clamp') {
    pager.goTo(9);
    requests[0].resolve(answer(4, 'last'));   // fewer pages than asked for
    await tick(); await tick();
    timers.splice(0).forEach(fn => fn());
    out.page = pager.page();
    out.search = sandbox.location.search;
    out.query = pager.query();
  }
  console.log(JSON.stringify(out));
})();
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


def test_url_written_only_when_it_changes_and_coalesced(tmp_path):
    out = _run("writes", tmp_path)
    assert out["after_unchanged"] == 0, "the URL already held this state"
    # A burst of 50 changes writes at most once now plus one trailing write
    assert out["during_burst"] <= 1
    assert out["after_flush"] <= 2
    assert out["search"] == "?q=bat49", "page 1 and the default per_page stay out of the URL"


def test_a_newer_load_aborts_an_older_one(tmp_path):
    out = _run("abort", tmp_path)
    assert out["first_aborted"] is True
    assert out["seen"] == ["new"]
    assert out["urls"] == ["/api/pull-list/rows?q=b", "/api/pull-list/rows?q=ba"]


def test_parameters_the_pager_does_not_own_are_kept(tmp_path):
    out = _run("foreign", tmp_path)
    assert out["search"] == "?automap_op=x&q=bat"


def test_a_failed_page_change_keeps_the_page_on_screen(tmp_path):
    out = _run("failed", tmp_path)
    assert out["page"] == 3 and out["seen"] == []


def test_follows_the_page_the_server_clamped_to(tmp_path):
    out = _run("clamp", tmp_path)
    assert out["page"] == 4
    assert out["search"] == "?q=bat&page=4"
    assert out["query"] == "q=bat"


def test_a_failed_filter_change_rolls_back_to_the_rows_on_screen(tmp_path):
    out = _run("failed_reset", tmp_path)
    assert out["page"] == 3 and out["search"] == "?page=3&q=bat"
    assert out["errors"] == ["boom"] and out["seen"] == []
    assert out["page_after"] == 1 and out["search_after"] == "?q=x"
