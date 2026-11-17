"""Ratchet: templates reference JS/CSS only through asset().

108 tags once carried ``?v={{ range(...) | random }}``, so ``files.js``,
``collection.js`` and the rest were re-downloaded in full on every render,
while the shared ``base.html`` assets carried no version at all and were
revalidated on every page. ``asset()`` (routes/auth.py, core/static_assets.py)
gives each file a content-hash token, and only that token earns the year-long
immutable max-age.
"""
import ast
import pathlib
import re

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = sorted(
    p for p in (PROJECT_ROOT / "templates").glob("**/*.html")
    if ".omc" not in p.parts
)


def _texts():
    assert len(TEMPLATES) > 10, "template glob matched almost nothing"
    return {p: p.read_text(encoding="utf-8") for p in TEMPLATES}


def _offenders(pattern):
    rx = re.compile(pattern)
    return [
        f"{p.relative_to(PROJECT_ROOT)}:{text.count(chr(10), 0, m.start()) + 1}"
        for p, text in _texts().items()
        for m in rx.finditer(text)
    ]


def test_no_random_cache_buster():
    assert _offenders(r"\|\s*random\b") == []


def test_js_and_css_go_through_asset():
    assert _offenders(r"url_for\(\s*['\"]static['\"]\s*,\s*filename\s*=\s*['\"](?:js|css)/") == []


def test_no_hand_made_date_token():
    assert _offenders(r"[?&]t=20\d{2}") == []


def test_asset_is_actually_used():
    total = sum(text.count("asset(") for text in _texts().values())
    assert total >= 100, f"only {total} asset( calls -- did a rewrite match nothing?"


def test_app_registers_no_second_static_route():
    """Flask's own ``static`` endpoint serves /static/; a later duplicate rule
    never matched and only hid where the cache headers come from."""
    tree = ast.parse((PROJECT_ROOT / "app.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and dec.args
                        and isinstance(dec.args[0], ast.Constant)
                        and isinstance(dec.args[0].value, str)):
                    assert not dec.args[0].value.startswith("/static/"), node.name
    assert any(
        isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "STATIC_DIR" for t in n.targets)
        for n in tree.body
    ), "STATIC_DIR is still used by serve_manifest and the missing.txt mover"
