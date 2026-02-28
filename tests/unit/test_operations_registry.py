"""Unit tests for the operations registry in app_state.py."""
import sys
import types
import time
import threading
from unittest.mock import patch, MagicMock

# Ensure apscheduler is importable before app_state
if "apscheduler" not in sys.modules:
    sys.modules["apscheduler"] = types.ModuleType("apscheduler")
    sys.modules["apscheduler.schedulers"] = types.ModuleType("apscheduler.schedulers")
    _bg = types.ModuleType("apscheduler.schedulers.background")
    _bg.BackgroundScheduler = MagicMock
    sys.modules["apscheduler.schedulers.background"] = _bg

import app_state


def _clear_operations():
    """Helper to reset the registry between tests."""
    with app_state._operations_lock:
        app_state._operations.clear()


class TestRegisterOperation:
    def setup_method(self):
        _clear_operations()

    def teardown_method(self):
        _clear_operations()

    def test_register_operation(self):
        op_id = app_state.register_operation("metadata", "Batman (2020)", total=10)
        assert op_id is not None
        ops = app_state.get_active_operations()
        assert len(ops) == 1
        op = ops[0]
        assert op["id"] == op_id
        assert op["op_type"] == "metadata"
        assert op["label"] == "Batman (2020)"
        assert op["status"] == "running"
        assert op["current"] == 0
        assert op["total"] == 10
        assert op["detail"] == "Starting..."
        assert op["started_at"] > 0
        assert op["completed_at"] is None

    def test_update_operation(self):
        op_id = app_state.register_operation("move", "file.cbz", total=100)
        app_state.update_operation(op_id, current=50, detail="Copying...")
        ops = app_state.get_active_operations()
        op = ops[0]
        assert op["current"] == 50
        assert op["detail"] == "Copying..."

    def test_complete_operation(self):
        op_id = app_state.register_operation("convert", "folder", total=5)
        app_state.update_operation(op_id, current=3)
        app_state.complete_operation(op_id)
        ops = app_state.get_active_operations()
        op = ops[0]
        assert op["status"] == "completed"
        assert op["current"] == 5  # snapped to total
        assert op["completed_at"] is not None

    def test_complete_with_error(self):
        op_id = app_state.register_operation("metadata", "X-Men", total=10)
        app_state.complete_operation(op_id, error=True)
        ops = app_state.get_active_operations()
        op = ops[0]
        assert op["status"] == "error"
        assert op["completed_at"] is not None
        # current should NOT snap to total on error
        assert op["current"] == 0

    def test_auto_cleanup_expired(self):
        op_id = app_state.register_operation("move", "old-op", total=1)
        app_state.complete_operation(op_id)
        # Backdate completed_at to force expiry
        with app_state._operations_lock:
            app_state._operations[op_id]["completed_at"] = time.time() - app_state.COMPLETED_TTL - 1
        ops = app_state.get_active_operations()
        assert len(ops) == 0

    def test_stale_running_op_marked_error(self):
        op_id = app_state.register_operation("metadata", "stale-op", total=5)
        app_state.update_operation(op_id, current=2, detail="file2.cbz")
        # Backdate updated_at to exceed STALE_TIMEOUT
        with app_state._operations_lock:
            app_state._operations[op_id]["updated_at"] = time.time() - app_state.STALE_TIMEOUT - 1
        ops = app_state.get_active_operations()
        op = ops[0]
        assert op["status"] == "error"
        assert op["detail"] == "Operation stalled"
        assert op["completed_at"] is not None

    def test_update_nonexistent_op(self):
        # Should not raise
        app_state.update_operation("nonexistent-id", current=5, detail="test")

    def test_complete_nonexistent_op(self):
        # Should not raise
        app_state.complete_operation("nonexistent-id")

    def test_thread_safety(self):
        results = []

        def register_many(n):
            ids = []
            for _ in range(n):
                ids.append(app_state.register_operation("metadata", "test"))
            results.extend(ids)

        threads = [threading.Thread(target=register_many, args=(50,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ops = app_state.get_active_operations()
        assert len(ops) == 200
        # All IDs should be unique
        assert len(set(results)) == 200
