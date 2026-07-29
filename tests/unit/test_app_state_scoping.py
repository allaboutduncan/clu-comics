"""
PR7: per-user scoping of the in-memory operations registry and notifications
(core/app_state.py). A user sees only their own progress/toasts; owners (and
the backward-compatible no-viewer default) see everything.
"""
import pytest

import core.app_state as app_state


@pytest.fixture(autouse=True)
def _clean_state():
    with app_state._operations_lock:
        app_state._operations.clear()
    with app_state._notifications_lock:
        app_state._notifications.clear()
    yield
    with app_state._operations_lock:
        app_state._operations.clear()
    with app_state._notifications_lock:
        app_state._notifications.clear()


A, B = 101, 102


class TestOperationScoping:
    def test_operation_stamped_with_user(self):
        op_id = app_state.register_operation("scan", "A scan", user_id=A)
        op = next(o for o in app_state.get_active_operations() if o["id"] == op_id)
        assert op["user_id"] == A

    def test_viewer_sees_only_own_ops(self):
        a = app_state.register_operation("scan", "A scan", user_id=A)
        b = app_state.register_operation("scan", "B scan", user_id=B)
        assert {o["id"] for o in app_state.get_active_operations(viewer_id=A)} == {a}
        assert {o["id"] for o in app_state.get_active_operations(viewer_id=B)} == {b}

    def test_owner_sees_all_ops(self):
        app_state.register_operation("scan", "A scan", user_id=A)
        app_state.register_operation("scan", "B scan", user_id=B)
        assert len(app_state.get_active_operations(is_owner=True)) == 2

    def test_no_viewer_returns_all(self):
        app_state.register_operation("scan", "A scan", user_id=A)
        app_state.register_operation("scan", "B scan", user_id=B)
        # Backward-compatible default (implicit-owner mode / existing callers).
        assert len(app_state.get_active_operations()) == 2


class TestNotificationScoping:
    def test_viewer_gets_and_clears_only_own(self):
        app_state.add_notification("for A", user_id=A)
        app_state.add_notification("for B", user_id=B)

        mine = app_state.get_and_clear_notifications(viewer_id=A)
        assert [n["message"] for n in mine] == ["for A"]

        # B's notification is retained, not cleared by A's fetch.
        remaining = app_state.get_and_clear_notifications(viewer_id=B)
        assert [n["message"] for n in remaining] == ["for B"]

    def test_owner_gets_all(self):
        app_state.add_notification("for A", user_id=A)
        app_state.add_notification("for B", user_id=B)
        got = app_state.get_and_clear_notifications(is_owner=True)
        assert {n["message"] for n in got} == {"for A", "for B"}

    def test_no_viewer_returns_all(self):
        app_state.add_notification("x", user_id=A)
        app_state.add_notification("y", user_id=B)
        assert len(app_state.get_and_clear_notifications()) == 2
