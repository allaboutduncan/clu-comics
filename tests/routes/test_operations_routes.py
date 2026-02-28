"""Route tests for the /api/operations endpoint."""
import app_state


def _clear_operations():
    with app_state._operations_lock:
        app_state._operations.clear()


class TestOperationsRoute:
    def setup_method(self):
        _clear_operations()

    def teardown_method(self):
        _clear_operations()

    def test_no_operations(self, client):
        resp = client.get("/api/operations")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["operations"] == []

    def test_with_active_operation(self, client):
        op_id = app_state.register_operation("metadata", "Batman", total=10)
        app_state.update_operation(op_id, current=3, detail="Issue #3")

        resp = client.get("/api/operations")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["operations"]) == 1
        op = data["operations"][0]
        assert op["id"] == op_id
        assert op["op_type"] == "metadata"
        assert op["label"] == "Batman"
        assert op["status"] == "running"
        assert op["current"] == 3
        assert op["total"] == 10
        assert op["detail"] == "Issue #3"
