"""Every streamed response in api.py is closed on every path.

A ``stream=True`` response holds its pooled connection until its body is read
to the end or it is closed. ``resolve_final_url``'s fallback GET never read the
body of a redirect hop or a non-HTML answer, and ``download_getcomics`` left
the response open on a 403/404, an HTML answer and a failed read, so each of
those kept a connection checked out until the garbage collector found it (and
longer while a stored exception's traceback kept the frame alive).

api.py starts worker threads and a cloudscraper session at import time and
cannot be imported by the suite, so this is asserted against its AST: every
streamed call must be a ``with`` item, or be bound to a name that a
``finally`` enclosing the call closes.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _tree():
    return ast.parse((ROOT / "api.py").read_text(encoding="utf-8"))


def _parents(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _is_streamed(call):
    return isinstance(call, ast.Call) and any(
        kw.arg == "stream" and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for kw in call.keywords
    )


def _closes(stmts, name):
    for stmt in stmts:
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Call):
                continue
            src = ast.unparse(node)
            if src in (f"close_quietly({name})", f"{name}.close()"):
                return True
    return False


def _verdict(call, parents):
    parent = parents[call]
    if isinstance(parent, ast.withitem):
        return True
    if not (isinstance(parent, ast.Assign) and len(parent.targets) == 1
            and isinstance(parent.targets[0], ast.Name)):
        return False
    name = parent.targets[0].id
    node = parent
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.Try) and _closes(node.finalbody, name):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
    return False


def test_every_streamed_response_is_closed():
    tree = _tree()
    parents = _parents(tree)
    streamed = [n for n in ast.walk(tree) if _is_streamed(n)]
    assert streamed, "fixture check: api.py has streamed requests"
    unclosed = [f"api.py:{c.lineno} {ast.unparse(c)}"
                for c in streamed if not _verdict(c, parents)]
    assert not unclosed, (
        "streamed responses not closed on every path (use `with`, or bind the "
        "response and close it in an enclosing finally):\n" + "\n".join(unclosed)
    )


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not in api.py")


def test_resolve_final_url_closes_every_hop_response():
    """The non-streamed scraper responses go through the same finally, which
    covers the redirect `continue` and every `return` after the request."""
    fn = _function(_tree(), "resolve_final_url")
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)
             and _closes(n.finalbody, "r")]
    assert len(tries) == 1
    body = ast.unparse(ast.Module(body=tries[0].body, type_ignores=[]))
    assert "r.headers['location']" in body


def test_download_getcomics_resets_the_response_each_attempt():
    """The finally must never close the previous attempt's response as if it
    were this one's, nor raise NameError before the first request."""
    fn = _function(_tree(), "download_getcomics")
    loop = next(n for n in ast.walk(fn) if isinstance(n, ast.For)
                and ast.unparse(n.iter) == "range(retries)")
    first = loop.body[0]
    assert ast.unparse(first) == "response = None"
    assert isinstance(loop.body[1], ast.Try)
    assert _closes(loop.body[1].finalbody, "response")
