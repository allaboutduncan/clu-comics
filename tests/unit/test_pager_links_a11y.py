"""Runs CLU.renderPageLinks (static/js/clu-pager.js) under node against a tiny
DOM stub, so what a screen reader and a keyboard get from the shared page bar
on /library, /pull-list and /reading-lists is pinned: the current page is
marked, the arrows and numbers are named, disabled links leave the tab order,
the gap is not a control, and a rebuild keeps focus on the bar.
Skipped where node is not installed."""

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

class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parent = null;
    this.attrs = {}; this.dataset = {}; this.className = ''; this.textContent = '';
    this.tabIndex = 0; this.href = null;
  }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  set innerHTML(_v) { this.children.forEach(c => { c.parent = null; }); this.children = []; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  contains(n) { for (; n; n = n.parent) if (n === this) return true; return false; }
  all() { return this.children.flatMap(c => [c, ...c.all()]); }
  // Enough of querySelector for the two selectors clu-pager.js uses.
  querySelector(sel) {
    const live = sel.endsWith(':not([aria-disabled])');
    const key = /data-pager-key="([^"]+)"/.exec(sel);
    return this.all().find(e =>
      (!live || e.getAttribute('aria-disabled') === null) &&
      (key ? e.dataset.pagerKey === key[1]
           : sel === '[aria-current="page"]' ? e.getAttribute('aria-current') === 'page' : false)
    ) || null;
  }
  focus() { document.activeElement = this; }
}

const document = { activeElement: null, createElement: t => new El(t), cookie: '' };
const sandbox = { document, window: {}, console };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);
const CLU = sandbox.CLU;

const scenario = process.argv[3];
const ul = new El('ul');
const went = [];
const describe = () => ul.children.map(li => {
  const a = li.children[0];
  return {
    tag: a.tagName, text: a.textContent, li: li.className,
    label: a.getAttribute('aria-label'), current: a.getAttribute('aria-current'),
    disabled: a.getAttribute('aria-disabled'), hidden: a.getAttribute('aria-hidden'),
    tabIndex: a.tabIndex, key: a.dataset.pagerKey || null,
  };
});

if (scenario === 'middle') {
  CLU.renderPageLinks(ul, 5, 20, p => went.push(p));
  console.log(JSON.stringify({ items: describe() }));
} else if (scenario === 'first') {
  CLU.renderPageLinks(ul, 1, 3, p => went.push(p));
  const items = describe();
  // Clicking a disabled or current link must not navigate.
  ul.children[0].children[0].onclick({ preventDefault() {} });
  ul.children[1].children[0].onclick({ preventDefault() {} });
  ul.children[2].children[0].onclick({ preventDefault() {} });
  console.log(JSON.stringify({ items, went }));
} else if (scenario === 'focus-next-to-last') {
  CLU.renderPageLinks(ul, 2, 3, p => went.push(p));
  ul.all().find(e => e.dataset.pagerKey === 'next').focus();
  CLU.renderPageLinks(ul, 3, 3, p => went.push(p));
  const f = document.activeElement;
  console.log(JSON.stringify({ inBar: ul.contains(f), key: f.dataset.pagerKey }));
} else if (scenario === 'focus-kept') {
  CLU.renderPageLinks(ul, 2, 9, p => went.push(p));
  ul.all().find(e => e.dataset.pagerKey === 'next').focus();
  CLU.renderPageLinks(ul, 3, 9, p => went.push(p));
  const f = document.activeElement;
  console.log(JSON.stringify({ inBar: ul.contains(f), key: f.dataset.pagerKey }));
} else if (scenario === 'focus-elsewhere') {
  const outside = new El('input');
  outside.focus();
  CLU.renderPageLinks(ul, 2, 9, p => went.push(p));
  console.log(JSON.stringify({ untouched: document.activeElement === outside }));
}
"""


def _run(tmp_path, scenario):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    out = subprocess.run(
        [NODE, str(harness), os.path.join(ROOT, "static/js/clu-pager.js"), scenario],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_current_page_is_marked_and_links_are_named(tmp_path):
    items = _run(tmp_path, "middle")["items"]
    current = [i for i in items if i["current"] == "page"]
    assert [i["text"] for i in current] == ["5"]
    assert current[0]["label"] == "Page 5"
    assert items[0]["label"] == "Previous page"
    assert items[-1]["label"] == "Next page"
    numbers = [i for i in items if i["text"].isdigit()]
    assert all(i["label"] == f"Page {i['text']}" for i in numbers)


def test_gaps_are_not_links(tmp_path):
    items = _run(tmp_path, "middle")["items"]
    gaps = [i for i in items if i["text"] == "…"]
    assert len(gaps) == 2          # 1 … 3 4 5 6 7 … 20
    for g in gaps:
        assert g["tag"] == "SPAN"
        assert g["hidden"] == "true"
        assert "disabled" in g["li"]


def test_disabled_arrows_leave_the_tab_order_and_do_nothing(tmp_path):
    r = _run(tmp_path, "first")
    prev = r["items"][0]
    assert prev["disabled"] == "true"
    assert prev["tabIndex"] == -1
    assert "disabled" in prev["li"]
    nxt = r["items"][-1]
    assert nxt["disabled"] is None and nxt["tabIndex"] == 0
    assert r["went"] == [2], "only the live Next link may navigate"


def test_focus_stays_on_the_control_after_a_rebuild(tmp_path):
    assert _run(tmp_path, "focus-kept") == {"inBar": True, "key": "next"}


def test_focus_falls_back_to_the_current_page_when_the_control_is_disabled(tmp_path):
    assert _run(tmp_path, "focus-next-to-last") == {"inBar": True, "key": "p3"}


def test_a_rebuild_never_steals_focus_from_outside_the_bar(tmp_path):
    assert _run(tmp_path, "focus-elsewhere") == {"untouched": True}
