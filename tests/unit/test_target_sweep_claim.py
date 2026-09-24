"""One wanted-scan at a time, and nothing dropped on the floor.

``app.process_incoming_wanted_issues`` has two callers and no schedule: api.py
runs it on a bare daemon thread per completed download, and routes/series.py
runs it inline for /api/scan-downloads. Nothing serialised them, so a batch of
finishing downloads produced a pass each -- one reported log has ~9 running at
once, every line duplicated, and an ENOENT storm as the losers tried to move
files a winner had already moved.

Unlike the GetComics sweep this coalesces rather than simply dropping: nothing
re-triggers the pass on a timer, so a caller that stands down with no trace can
strand a file in TARGET until the next download finishes.
"""
import threading

import pytest

import core.app_state as app_state


@pytest.fixture(autouse=True)
def _clean_claim():
    app_state._target_sweep["started_at"] = None
    app_state._target_sweep["rerun"] = False
    yield
    app_state._target_sweep["started_at"] = None
    app_state._target_sweep["rerun"] = False


class TestClaiming:

    def test_the_second_claim_stands_down(self):
        assert app_state.claim_target_sweep() is True
        assert app_state.claim_target_sweep() is False

    def test_releasing_lets_the_next_run_through(self):
        app_state.claim_target_sweep()
        app_state.release_target_sweep()
        assert app_state.claim_target_sweep() is True

    def test_releasing_an_unheld_claim_is_harmless(self):
        assert app_state.release_target_sweep() is False
        assert app_state.claim_target_sweep() is True

    def test_running_is_reported(self):
        assert app_state.target_sweep_running() is False
        app_state.claim_target_sweep()
        assert app_state.target_sweep_running() is True
        app_state.release_target_sweep()
        assert app_state.target_sweep_running() is False

    def test_age_is_none_when_unheld(self):
        assert app_state.target_sweep_age() is None
        app_state.claim_target_sweep()
        assert app_state.target_sweep_age() >= 0

    def test_a_refused_claim_asks_for_a_rerun(self):
        app_state.claim_target_sweep()
        app_state.claim_target_sweep()  # refused
        assert app_state.release_target_sweep() is True

    def test_a_clean_run_owes_nothing(self):
        app_state.claim_target_sweep()
        assert app_state.release_target_sweep() is False


class TestDecorator:
    """``single_flight_target_sweep`` is what app.py actually wears."""

    def test_a_lone_call_runs_once(self):
        calls = []

        @app_state.single_flight_target_sweep
        def pass_():
            calls.append(1)

        pass_()
        assert len(calls) == 1

    def test_a_caller_arriving_mid_pass_earns_one_more_pass(self):
        """The coalescing guarantee: the second caller's work is not lost."""
        calls = []

        @app_state.single_flight_target_sweep
        def pass_():
            calls.append(1)
            if len(calls) == 1:
                # A second caller arrives while the first is still working.
                pass_()

        pass_()
        assert len(calls) == 2

    def test_the_reentrant_caller_does_not_recurse(self):
        """The nested call must return immediately, not run the body inline."""
        depth = {"max": 0, "now": 0}

        @app_state.single_flight_target_sweep
        def pass_():
            depth["now"] += 1
            depth["max"] = max(depth["max"], depth["now"])
            if depth["max"] == 1 and depth["now"] == 1:
                pass_()
            depth["now"] -= 1

        pass_()
        assert depth["max"] == 1

    def test_reruns_are_bounded(self):
        """A caller that re-triggers forever must not spin the pass forever."""
        calls = []

        @app_state.single_flight_target_sweep
        def pass_():
            calls.append(1)
            pass_()  # always asks for another

        pass_()
        assert len(calls) == app_state.MAX_TARGET_SWEEP_PASSES

    def test_the_claim_is_released_after_a_failure(self):
        """One failed pass must not lock the scan out for the process's life."""

        @app_state.single_flight_target_sweep
        def boom():
            raise RuntimeError("nope")

        with pytest.raises(RuntimeError):
            boom()
        assert app_state.target_sweep_running() is False
        assert app_state.claim_target_sweep() is True

    def test_the_result_is_passed_through(self):
        @app_state.single_flight_target_sweep
        def pass_():
            return "done"

        assert pass_() == "done"

    def test_the_wrapped_name_survives(self):
        """api.py imports this by name; functools.wraps keeps it findable."""

        @app_state.single_flight_target_sweep
        def process_incoming_wanted_issues():
            return None

        assert process_incoming_wanted_issues.__name__ == (
            "process_incoming_wanted_issues"
        )

    def test_only_one_thread_runs_the_body(self):
        inside = []
        overlapped = []
        gate = threading.Event()

        @app_state.single_flight_target_sweep
        def pass_():
            inside.append(1)
            if len(inside) > 1:
                overlapped.append(1)
            gate.wait(0.5)
            inside.pop()

        first = threading.Thread(target=pass_)
        first.start()
        # Give the first thread time to take the claim before the second tries.
        threading.Event().wait(0.05)
        pass_()  # stands down immediately
        gate.set()
        first.join(2)
        assert overlapped == []
