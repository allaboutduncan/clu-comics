"""The same-origin check is an app-wide hook that runs before anything else.

app.py cannot be imported in tests, so its half is asserted from the source:
auth_bp must be the first blueprint it registers, and neither app.py nor api.py
may add an app-level before_request that would run ahead of it.
"""
import ast
import os

from flask import Flask

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _source(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def test_hook_is_app_wide_and_first():
    from routes.auth import auth_bp, require_login, require_same_origin

    app = Flask(__name__)
    app.register_blueprint(auth_bp)
    # None = runs for every request, whichever blueprint (or none) serves it.
    hooks = app.before_request_funcs.get(None, [])
    assert require_same_origin in hooks
    assert hooks.index(require_same_origin) < hooks.index(require_login)
    assert not any(require_same_origin in v for k, v in app.before_request_funcs.items()
                   if k is not None)


def test_hook_runs_for_routes_outside_any_blueprint():
    from routes.auth import auth_bp

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "x"
    app.register_blueprint(auth_bp)

    @app.route("/plain", methods=["POST"])
    def plain():
        return "ok"

    client = app.test_client()
    assert client.post("/plain", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/plain", headers={"Origin": "http://localhost"}).status_code == 200


def test_auth_bp_is_the_first_blueprint_app_py_registers():
    tree = ast.parse(_source("app.py"))
    registered = sorted(
        (node.lineno, getattr(node.args[0], "id", None))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "register_blueprint"
        and node.args
    )
    assert registered and registered[0][1] == "auth_bp"


def test_no_app_level_before_request_ahead_of_it():
    for name in ("app.py", "api.py"):
        src = _source(name)
        assert "@app.before_request" not in src, name
        assert "app.before_request(" not in src, name
        assert "before_request_funcs" not in src, name


def test_load_flask_config_applies_the_cookie_policy():
    tree = ast.parse(_source(os.path.join("core", "config.py")))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "load_flask_config")
    calls = {n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "apply_session_cookie_policy" in calls
