"""The legacy global API token is hashed at rest: startup wiring and the page.

app.py cannot be imported in tests, so the startup call is asserted on its
source; the Settings page is asserted on the template.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _module_level_calls(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Name):
                names.append(func.id)
    return names


def test_app_migrates_the_legacy_token_at_startup_after_seeding_the_owner():
    calls = _module_level_calls(ROOT / "app.py")
    assert "migrate_legacy_api_token_to_digest" in calls
    # seed_owner_if_needed copies the legacy token into the owner's api_tokens
    # row; it reads the digest either way, but the order keeps the two steps
    # readable as "seed, then retire the plaintext".
    assert calls.index("seed_owner_if_needed") < calls.index(
        "migrate_legacy_api_token_to_digest")


def _token_script():
    html = (ROOT / "templates" / "config.html").read_text(encoding="utf-8")
    start = html.index("// Client API Token")
    end = html.index("function setApiBrowseModeUi")
    return html, html[start:end]


def test_page_never_expects_the_stored_token_back():
    _html, script = _token_script()
    # The status fetch hands over only "configured"; a token reaches the field
    # only from the rotate response.
    assert "setApiTokenUi(data.configured, null)" in script
    assert "setApiTokenUi(data.token)" not in script
    assert "setApiTokenUi(true, data.token)" in script


def test_page_uses_a_modal_not_native_dialogs():
    html, script = _token_script()
    assert not re.search(r"\b(confirm|prompt|alert)\(", script.replace("CLU.toast(", ""))
    assert 'id="apiTokenRotateModal"' in html
    assert 'id="apiTokenRotateConfirmBtn"' in html


def test_page_says_the_token_is_shown_once():
    html, _script = _token_script()
    assert 'id="apiTokenShownOnceNote"' in html
    assert "will not be shown again" in html
