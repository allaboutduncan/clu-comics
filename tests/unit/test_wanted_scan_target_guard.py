"""What app.process_incoming_wanted_issues is allowed to reach, and how often.

A user with ``TARGET = /library/media`` -- which was also their library root --
had 26 comics named "Batman 0NN (YYYY).cbz" moved out of "(2016) Batman v3" and
into "(2003) Superman - Batman v1". Three defects stacked:

* the ComicInfo tier accepted a shorter series name by raw substring, so
  "Batman" satisfied a wanted issue of "Superman-Batman ... Special Edition"
  (fixed in helpers/collection.py, tested for real elsewhere);
* the pass walked TARGET recursively with no exclusions, so a comic already
  filed in a series folder was offered up as an incoming download;
* nothing serialised the pass, so ~9 ran at once and raced each other into an
  ENOENT storm.

Asserted against the parsed AST because app.py cannot be imported in tests: it
starts the scheduler and spawns monitor.py at import. The behaviour itself lives
in helpers.collection.collect_target_candidates and core.app_state, which have
their own real tests -- what app.py owes is calling them.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")

FUNC = "process_incoming_wanted_issues"


@pytest.fixture(scope="module")
def app_source():
    with open(APP_PATH, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def tree(app_source):
    return ast.parse(app_source)


@pytest.fixture(scope="module")
def func_node(tree):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == FUNC:
            return node
    pytest.fail(f"{FUNC} not found in app.py")


@pytest.fixture(scope="module")
def func_src(app_source, func_node):
    return ast.get_source_segment(app_source, func_node)


def _calls(node, name):
    """Calls to a bare name, e.g. foo(...)."""
    return [
        child for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == name
    ]


def _attr_calls(node, attr):
    """Calls to an attribute, e.g. os.walk(...)."""
    return [
        child for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == attr
    ]


class TestSingleFlight:

    def test_the_pass_is_decorated(self, func_node):
        names = set()
        for dec in func_node.decorator_list:
            if isinstance(dec, ast.Name):
                names.add(dec.id)
            elif isinstance(dec, ast.Attribute):
                names.add(dec.attr)
        assert "single_flight_target_sweep" in names, (
            "api.py runs this on a bare daemon thread per completed download "
            "and routes/series.py runs it on the request thread; nothing else "
            "serialises them"
        )

    def test_the_decorator_owns_the_imported_name(self, func_node):
        """api.py imports this name and must not be edited.

        A wrapper function under a different name, with the body renamed to
        _process_incoming_wanted_issues, would leave api.py calling the
        unguarded body.
        """
        assert func_node.name == FUNC


class TestTheWalkIsGone:

    def test_the_pass_does_not_enumerate_target_itself(self, func_node):
        assert not _attr_calls(func_node, "walk"), (
            "the recursive walk is what reached into a series folder; "
            "collect_target_candidates owns that decision now"
        )

    def test_it_asks_the_collector_exactly_once(self, func_node):
        calls = _calls(func_node, "collect_target_candidates")
        assert len(calls) == 1, (
            f"expected one collect_target_candidates call, found {len(calls)}"
        )

    def test_the_collector_is_told_which_folders_are_series_folders(self, func_node):
        call = _calls(func_node, "collect_target_candidates")[0]
        kwargs = {kw.arg for kw in call.keywords}
        assert "mapped_dirs" in kwargs, (
            "the pass already loads get_all_mapped_series(); passing the list "
            "avoids a second query and keeps the protected set consistent with "
            "the wanted list built from it"
        )


class TestNoHardcodedDataLiteral:
    """Path safety is asked of the configured libraries, not of a literal.

    The old guard compared against os.path.realpath("/data"), which said
    nothing about a library configured anywhere else -- and /library/media was
    exactly that. The positive behaviour is covered in
    tests/unit/test_watch_target_verdict.py; app.py can only be pinned
    negatively.
    """

    def _constants(self, node):
        return {
            child.value for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }

    def test_the_scan_carries_no_data_literal(self, func_node):
        assert "/data" not in self._constants(func_node)

    @pytest.mark.parametrize("handler", [
        "save_file_processing_config",
        "config_page",
    ])
    def test_the_settings_handlers_carry_no_data_literal(self, tree, handler):
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == handler:
                assert "/data" not in self._constants(node), (
                    f"{handler} must ask helpers.library.watch_target_verdict"
                )
                return
        pytest.fail(f"{handler} not found in app.py")

    @pytest.mark.parametrize("handler", [
        "save_file_processing_config",
        "config_page",
    ])
    def test_the_settings_handlers_use_the_shared_validator(self, tree, handler):
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == handler:
                assert _calls(node, "watch_target_verdict"), (
                    f"{handler} must use the one predicate, or the three "
                    f"copies drift apart again"
                )
                return
        pytest.fail(f"{handler} not found in app.py")


class TestTheLastGuardBeforeTheMove:
    """A filed comic is never moved, whatever the matcher concluded.

    collect_target_candidates should mean this never fires, but it is the one
    sentence the incident violated and it guards any future caller of the
    matcher -- so it sits at the point of no return, not only at collection.
    """

    def _match_loop(self, func_node):
        for child in ast.walk(func_node):
            if (
                isinstance(child, ast.For)
                and isinstance(child.target, ast.Name)
                and child.target.id == "match"
            ):
                return child
        pytest.fail("the `for match in matches:` loop was not found")

    def test_the_loop_refuses_a_source_in_a_series_folder(self, func_node):
        loop = self._match_loop(func_node)
        guards = [
            node for node in ast.walk(loop)
            if isinstance(node, ast.If)
            and any(
                isinstance(n, ast.Name) and n.id == "protected_dirs"
                for n in ast.walk(node.test)
            )
            and any(isinstance(n, ast.Continue) for n in node.body)
        ]
        assert guards, (
            "expected an `if ... protected_dirs ...: continue` guard inside "
            "the match loop"
        )

    def test_the_guard_precedes_the_move(self, func_node):
        loop = self._match_loop(func_node)
        guard_line = min(
            node.lineno for node in ast.walk(loop)
            if isinstance(node, ast.If)
            and any(
                isinstance(n, ast.Name) and n.id == "protected_dirs"
                for n in ast.walk(node.test)
            )
            and any(isinstance(n, ast.Continue) for n in node.body)
        )
        move_lines = [c.lineno for c in _calls(loop, "move_file")]
        assert move_lines, "move_file call not found in the match loop"
        assert guard_line < min(move_lines), (
            "the guard is worthless after the file has already moved"
        )


class TestProtectedDirsIsBuiltFromEverySeries:

    def test_it_is_assigned_before_the_series_loop(self, app_source, func_node):
        """A series with nothing missing still owns its folder.

        The per-series loop `continue`s for a series with no cached issues, so
        collecting inside it would leave those folders unprotected.
        """
        assigns = [
            node for node in ast.walk(func_node)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "protected_dirs"
                for t in node.targets
            )
        ]
        assert assigns, "protected_dirs is never assigned"

        series_loops = [
            node for node in ast.walk(func_node)
            if isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "series"
            and any(
                isinstance(n, ast.Continue) for n in ast.walk(node)
            )
        ]
        assert series_loops, "the per-series loop was not found"
        assert min(a.lineno for a in assigns) < min(
            loop.lineno for loop in series_loops
        )
