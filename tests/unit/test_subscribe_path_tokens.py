"""#616: the Subscribe modal builds the folder path in JavaScript.

``String.prototype.replace`` with a *string* argument replaces only the first
occurrence, so ``{publisher}/{series_name}/{series_name} ...`` created a folder
literally named ``{series_name} v7 (2021)`` while the Settings preview (which
uses ``/g`` regexes) looked right. The template is JavaScript, so this is
asserted structurally; the behaviour is also run through ``node`` when it is
available.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "..", "templates", "series.html"
)


def _build_subscribe_path_source():
    with open(TEMPLATE, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index("function buildSubscribePath()")
    end = src.index("function openSubscribeModal()", start)
    return src[start:end]


def test_no_first_occurrence_only_token_replace():
    body = _build_subscribe_path_source()
    assert not re.search(r"""\.replace\(\s*['"`]\{""", body), (
        "a string-argument .replace('{token}', ...) replaces only the first occurrence"
    )


@pytest.mark.parametrize(
    "token", ["publisher", "series_name", "start_year", "volume_number"]
)
def test_each_token_replaced_globally(token):
    body = _build_subscribe_path_source()
    assert f"/\\{{{token}\\}}/g" in body


def test_leftover_tokens_are_stripped():
    body = _build_subscribe_path_source()
    assert r"/\{[A-Za-z_][^}]*\}/g" in body


def test_pattern_is_emitted_as_json():
    with open(TEMPLATE, encoding="utf-8") as fh:
        src = fh.read()
    line = next(l for l in src.splitlines() if "const subscribePattern" in l)
    assert "tojson" in line


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_repeated_token_resolves_in_node():
    body = _build_subscribe_path_source()
    script = (
        "const CLU = {applyCharMap: (v) => v};\n"
        "const charMap = {};\n"
        "const seriesData = {publisher: {name: 'DC Comics'}, name: 'Green Lantern',"
        " year_began: 2021, volume: 7};\n"
        "const subscribePattern = "
        + json.dumps(
            "{publisher}/{series_name}/{series_name} {volume_number} ({start_year}) {issue_number}"
        )
        + ";\n"
        "function getSelectedLibraryPath() { return '/data'; }\n"
        + body
        + "\nprocess.stdout.write(buildSubscribePath());\n"
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    ).stdout
    assert out == "/data/DC Comics/Green Lantern/Green Lantern v7 (2021)"
