"""GHSA-4v7f-qm46-8fw4: /stream/<script_type> deleted or rewrote any path.

app.py cannot be imported in tests, so the guards in ``stream_logs`` are
asserted structurally; the role classification is exercised directly.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")


@pytest.fixture(scope="module")
def source():
    with open(APP_PATH, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "stream_logs":
            return ast.get_source_segment(src, node)
    pytest.fail("stream_logs not found")


def test_cross_site_requests_refused(source):
    assert "Sec-Fetch-Site" in source
    assert "Origin" in source


def test_paths_confined_before_any_existence_check(source):
    confine = source.index("is_allowed_path(target)")
    assert confine < source.index("os.path.isfile(file_path)")
    assert confine < source.index("os.path.isdir(directory)")
    assert "file_op_refusal(" in source


def test_delete_refuses_critical_paths(source):
    assert 'script_type == "delete" and is_critical_path(target)' in source


def test_stream_requires_clerk():
    from flask import Flask
    from core import auth

    with Flask(__name__).test_request_context("/stream/delete", method="GET"):
        assert auth.required_role_for_request() == "clerk"
