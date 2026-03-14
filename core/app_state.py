import time
import uuid
import threading
from apscheduler.schedulers.background import BackgroundScheduler

# ── Unified Scheduler ──
scheduler = BackgroundScheduler(daemon=True)

# ── Wanted Issues Refresh ──
wanted_refresh_in_progress = False
wanted_refresh_lock = threading.Lock()
wanted_last_refresh_time = 0  # timestamp of last completed refresh

# ── Data Directory Stats Cache ──
data_dir_stats_cache = {}
data_dir_stats_last_update = 0
DATA_DIR_STATS_CACHE_DURATION = 300  # 5 minutes

# ── Operations Registry ──
_operations = {}
_operations_lock = threading.Lock()
COMPLETED_TTL = 15  # seconds before completed ops are purged
STALE_TIMEOUT = 300  # seconds with no update before a running op is marked stale/error


def register_operation(op_type, label, total=0):
    """Register a new long-running operation. Returns the operation ID."""
    op_id = uuid.uuid4().hex
    now = time.time()
    with _operations_lock:
        _operations[op_id] = {
            "id": op_id,
            "op_type": op_type,
            "label": label,
            "status": "running",
            "current": 0,
            "total": total,
            "detail": "Starting...",
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
        }
    return op_id


def update_operation(op_id, current=None, total=None, detail=None):
    """Update progress on an existing operation. No-op if op_id not found."""
    with _operations_lock:
        op = _operations.get(op_id)
        if op is None:
            return
        if current is not None:
            op["current"] = current
        if total is not None:
            op["total"] = total
        if detail is not None:
            op["detail"] = detail
        op["updated_at"] = time.time()


def complete_operation(op_id, error=False):
    """Mark an operation as completed or errored."""
    with _operations_lock:
        op = _operations.get(op_id)
        if op is None:
            return
        op["status"] = "error" if error else "completed"
        op["completed_at"] = time.time()
        if not error:
            op["current"] = op["total"]


def get_active_operations():
    """Return all operations, auto-pruning completed/stale ops."""
    now = time.time()
    with _operations_lock:
        # Mark stale running ops as error (generator abandoned / connection lost)
        for op in _operations.values():
            if op["status"] == "running" and (now - op["updated_at"]) > STALE_TIMEOUT:
                op["status"] = "error"
                op["completed_at"] = now
                op["detail"] = "Operation stalled"

        # Prune expired completed operations
        expired = [
            oid for oid, op in _operations.items()
            if op["status"] in ("completed", "error")
            and op["completed_at"] is not None
            and (now - op["completed_at"]) > COMPLETED_TTL
        ]
        for oid in expired:
            del _operations[oid]
        return list(_operations.values())
