"""POST /api/config/download-api reports a ComicVine key it could not save.

The key save used to sit in ``try: ... except Exception: pass`` and ignored
``save_provider_credentials``' False return, so an unreadable encryption key
or a locked database was a silent no-op and the Settings page said "saved".

app.py cannot be imported in tests, so the handler (and the
``sanitize_config_value`` helper it calls) are lifted out of app.py's AST and
run against a throwaway Flask app with the settings writers stubbed.
"""
import ast
import logging
import pathlib

import pytest
from flask import Flask, jsonify, request

ROOT = pathlib.Path(__file__).resolve().parents[2]

_WANTED = {"save_download_api_config", "sanitize_config_value"}


def _load_handler(calls):
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    funcs = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in _WANTED]
    assert {f.name for f in funcs} == _WANTED
    for f in funcs:
        f.decorator_list = []
    module = ast.Module(body=funcs, type_ignores=[])
    flask_app = Flask(__name__)
    ns = {
        "app": flask_app,
        "request": request,
        "jsonify": jsonify,
        "app_logger": logging.getLogger("test-download-api"),
        "set_user_preference": lambda *a, **k: calls.append(("pref", a)),
        "load_flask_config": lambda app: calls.append(("load", None)),
    }
    exec(compile(module, "app.py", "exec"), ns)
    return flask_app, ns["save_download_api_config"]


@pytest.fixture
def run(monkeypatch):
    calls = []
    monkeypatch.setattr("core.settings.set_setting",
                        lambda *a, **k: calls.append(("setting", a)))

    def _run(save_result, payload):
        def fake_save(provider, creds):
            calls.append(("creds", provider, creds))
            if isinstance(save_result, Exception):
                raise save_result
            return save_result

        monkeypatch.setattr("core.database.save_provider_credentials", fake_save)
        flask_app, handler = _load_handler(calls)
        with flask_app.test_request_context(
                "/api/config/download-api", method="POST", json=payload):
            rv = handler()
        resp, status = rv if isinstance(rv, tuple) else (rv, 200)
        return status, resp.get_json(), calls

    return _run


def test_saved_key_reports_success(run):
    status, body, calls = run(True, {"comicvineApiKey": "abc123"})
    assert status == 200 and body["success"] is True
    assert ("creds", "comicvine", {"api_key": "abc123"}) in calls


def test_refused_save_is_reported(run, caplog):
    with caplog.at_level(logging.ERROR, logger="test-download-api"):
        status, body, calls = run(False, {"comicvineApiKey": "abc123"})
    assert status == 500
    assert body["success"] is False
    assert "ComicVine API key could not be saved" in body["error"]
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    # Everything else on the tab was still written and applied.
    assert ("load", None) in calls
    assert any(c[0] == "setting" for c in calls)


def test_raising_save_is_reported(run):
    status, body, _ = run(RuntimeError("key file unreadable"),
                          {"comicvineApiKey": "abc123"})
    assert status == 500 and body["success"] is False


def test_blank_key_is_not_saved_and_not_an_error(run):
    status, body, calls = run(False, {"comicvineApiKey": ""})
    assert status == 200 and body["success"] is True
    assert not any(c[0] == "creds" for c in calls)
