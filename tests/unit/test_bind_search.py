"""Runs CLU.bindSearch (static/js/clu-filters.js) under node against a tiny
input stub. It skips a value it already ran, and the Clear buttons on
Library, Library Health, Pull List, Reading Lists, Metadata History, Releases
and the volume modal empty the box with input.value = '', which fires no
event: the same query typed again was skipped, so the box said "bat" over an
unfiltered list. Skipped where node is not installed."""

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

const sandbox = { document: { addEventListener() {} }, window: {}, console, setTimeout, clearTimeout };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);
const CLU = sandbox.CLU;
const wait = ms => new Promise(r => setTimeout(r, ms));

// A user edit: beforeinput sees the old value, input the new one.
function box() {
  const on = {};
  return {
    value: '',
    addEventListener(t, f) { (on[t] = on[t] || []).push(f); },
    type(v) {
      (on.beforeinput || []).forEach(f => f({ isTrusted: true }));
      this.value = v;
      (on.input || []).forEach(f => f({ isTrusted: true }));
    },
    // What a page does to reset the box: value set, then a dispatched event.
    dispatch() { (on.input || []).forEach(f => f({ isTrusted: false })); },
    enter() { (on.keydown || []).forEach(f => f({ key: 'Enter', preventDefault() {} })); },
  };
}

(async () => {
  const scenario = process.argv[3];
  const ran = [];
  const b = box();
  if (scenario === 'debounced') {
    CLU.bindSearch(b, v => ran.push(v), { delay: 20 });
    b.type('b'); b.type('ba'); b.type('bat'); await wait(60);
    b.value = '';                                  // the page's Clear button
    b.type('b'); b.type('ba'); b.type('bat'); await wait(60);
  } else if (scenario === 'paste') {
    CLU.bindSearch(b, v => ran.push(v), { delay: 0 });
    b.type('bat');
    b.value = '';
    b.type('bat');
  } else if (scenario === 'unchanged') {
    CLU.bindSearch(b, v => ran.push(v), { delay: 20 });
    b.type('bat'); await wait(60);
    b.type('bat '); await wait(60);                // trailing space
    b.enter();                                     // Enter on the same query
  } else if (scenario === 'reopen') {
    // A modal reopened inside the debounce: the box is cleared and an input
    // event dispatched while the last query that ran is still ''.
    CLU.bindSearch(b, v => ran.push(v), { delay: 20 });
    b.type('bat');
    b.value = ''; b.dispatch(); await wait(60);
  } else if (scenario === 'page-reset') {
    // The page reset its own query state under a box that still reads the
    // old query, then cleared the box and dispatched input. Retyping the
    // same query must search again.
    CLU.bindSearch(b, v => ran.push(v), { delay: 20 });
    b.type('bat'); await wait(60);
    b.value = ''; b.dispatch();
    b.type('ba'); b.type('bat'); await wait(60);
  } else if (scenario === 'set-by-code') {
    // Code wrote a different query into the box without searching: the
    // next edit's query runs even if it matches what last ran.
    CLU.bindSearch(b, v => ran.push(v), { delay: 20 });
    b.type('bat'); await wait(60);
    b.value = 'batman';
    b.type('bat'); await wait(60);
  }
  console.log(JSON.stringify(ran));
})();
"""


def _run(tmp_path, scenario):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    out = subprocess.run(
        [NODE, str(harness), os.path.join(ROOT, "static/js/clu-filters.js"), scenario],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_retyping_a_query_after_a_clear_runs_it_again(tmp_path):
    assert _run(tmp_path, "debounced") == ["bat", "bat"]


def test_pasting_a_query_after_a_clear_runs_it_again(tmp_path):
    assert _run(tmp_path, "paste") == ["bat", "bat"]


def test_an_unchanged_query_is_still_skipped(tmp_path):
    assert _run(tmp_path, "unchanged") == ["bat"]


def test_a_dispatched_input_event_runs_inside_the_debounce(tmp_path):
    # The pending 'bat' is superseded; the dispatched '' runs even though ''
    # is also the value bindSearch started from.
    assert _run(tmp_path, "reopen") == [""]


def test_a_query_retyped_after_a_page_reset_runs_again(tmp_path):
    assert _run(tmp_path, "page-reset") == ["bat", "", "bat"]


def test_a_box_changed_by_code_runs_the_next_query(tmp_path):
    assert _run(tmp_path, "set-by-code") == ["bat", "bat"]
