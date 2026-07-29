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


def _resolve_uid(user_id):
    """Best-effort id of the user this op/notification belongs to.

    An explicit ``user_id`` wins; otherwise resolve the request's current user
    (or the Store Owner in a background thread). Kept resilient so the registry
    never raises just because identity can't be determined.
    """
    if user_id is not None:
        return user_id
    try:
        from core.database import current_user_id
        return current_user_id()
    except Exception:
        return None


def _visible(op_or_note, viewer_id, is_owner):
    """Whether a viewer may see an op/notification (owner sees all)."""
    if is_owner or viewer_id is None:
        return True
    return op_or_note.get("user_id") == viewer_id


def register_operation(op_type, label, total=0, op_id=None, user_id=None):
    """Register a new long-running operation. Returns the operation ID.

    Pass ``op_id`` to use a caller-chosen identifier (e.g. a client-generated
    token for synchronous endpoints that want polled progress). Defaults to a
    fresh uuid when omitted. The op is attributed to ``user_id`` (or the current
    request user / owner) so progress doesn't leak across accounts.
    """
    if op_id is None:
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
            "user_id": _resolve_uid(user_id),
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


def get_active_operations(viewer_id=None, is_owner=False):
    """Return operations visible to the viewer, auto-pruning completed/stale ops.

    With no viewer (``viewer_id=None``) or for an owner, every op is returned
    (backward-compatible default). A non-owner viewer sees only their own ops.
    """
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
        return [op for op in _operations.values() if _visible(op, viewer_id, is_owner)]


# ── Background Notifications ──
_notifications = []
_notifications_lock = threading.Lock()
NOTIFICATION_TTL = 300  # seconds before notifications expire


def add_notification(message, level="warning", user_id=None):
    """Add a notification for the UI to display (e.g., partial extraction warnings).

    Attributed to ``user_id`` (or the current request user / owner) so toasts
    don't surface on another account's screen.
    """
    with _notifications_lock:
        _notifications.append({
            "message": message,
            "level": level,
            "created_at": time.time(),
            "user_id": _resolve_uid(user_id),
        })


def get_and_clear_notifications(viewer_id=None, is_owner=False):
    """Return the viewer's pending notifications and clear (only) those.

    With no viewer or for an owner, all notifications are returned and cleared
    (backward-compatible default). A non-owner viewer sees and clears only their
    own; others' notifications are retained. Expired ones are always pruned.
    """
    now = time.time()
    with _notifications_lock:
        fresh = [n for n in _notifications if (now - n["created_at"]) < NOTIFICATION_TTL]
        mine = [n for n in fresh if _visible(n, viewer_id, is_owner)]
        # Retain fresh notifications that belong to other users.
        retained = [n for n in fresh if not _visible(n, viewer_id, is_owner)]
        _notifications.clear()
        _notifications.extend(retained)
        return mine


# ── Database Integrity State ──
# Result of the last DB integrity check. Unlike notifications this is NOT
# TTL-pruned, so the UI can read it any time after the startup check.
_db_integrity = {"ok": True, "error": None, "checked_at": 0}
_db_integrity_lock = threading.Lock()


def set_db_integrity(ok, error=None):
    """Record the outcome of a database integrity check."""
    with _db_integrity_lock:
        _db_integrity["ok"] = bool(ok)
        _db_integrity["error"] = error
        _db_integrity["checked_at"] = time.time()


def get_db_integrity():
    """Return a copy of the last recorded database integrity state."""
    with _db_integrity_lock:
        return dict(_db_integrity)
