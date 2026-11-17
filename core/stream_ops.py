"""Operations-registry tracking for the single-file actions run by /stream.

Crop Cover, Remove 1st Image, Rebuild, Enhance, Add Blank and Delete each run
as a subprocess whose output is streamed to the page that started it. Only
Rebuild used to be registered, so the others showed nowhere but in that page's
own progress card, and nothing followed them once the page went away.

This lives outside app.py so it can be tested: app.py cannot be imported in
tests.
"""
import os
import re
import threading

import core.app_state as app_state

# script_type -> (registry op_type, label shown in the header indicator).
# "single_file" keeps the "convert" type it has always been registered under.
SINGLE_FILE_OPS = {
    "single_file": ("convert", "Rebuild"),
    "crop": ("crop", "Crop Cover"),
    "remove": ("remove", "Remove 1st Image"),
    "enhance_single": ("enhance", "Enhance"),
    "add": ("add", "Add Blank to End"),
    "delete": ("delete", "Delete"),
}

_PROGRESS_RE = re.compile(r"Compression progress: ([\d.]+)% \((\d+)/(\d+)")
_STEP_RE = re.compile(r"Step (\d+)/(\d+): (.+)")


def register_single_file_op(script_type, file_path, user_id=None):
    """Register the op for one single-file action and return its id.

    ``user_id`` is resolved by the caller inside the request: the stream's
    generator runs after the request context is gone, where identity would
    resolve to nobody and the op would be hidden from the user who started it.
    """
    op_type, label = SINGLE_FILE_OPS.get(script_type, (script_type, script_type))
    op_id = app_state.register_operation(
        op_type, f"{label}: {os.path.basename(file_path)}", user_id=user_id,
        path=file_path,
    )
    app_state.update_operation(op_id, detail="Working...")
    return op_id


def track_line(op_id, line):
    """Turn one line of the subprocess's output into registry progress."""
    m = _PROGRESS_RE.search(line)
    if m:
        app_state.update_operation(
            op_id,
            current=int(float(m.group(1))),
            total=100,
            detail=f"Compressing ({m.group(2)}/{m.group(3)} files)",
        )
        return
    m = _STEP_RE.search(line)
    if m:
        app_state.update_operation(op_id, detail=m.group(3).strip(".\n"))


def finish(op_id, returncode):
    """Mark the op done; a non-zero exit is an error."""
    failed = returncode != 0
    if failed:
        app_state.update_operation(op_id, detail="Failed")
    else:
        app_state.update_operation(op_id, detail="Done")
    app_state.complete_operation(op_id, error=failed)


def follow_in_background(process, op_id):
    """Keep following *process* after the page that started it disconnected.

    The pipes must still be read to the end: a subprocess whose stdout nobody
    drains blocks as soon as the pipe buffer fills, and would never finish.
    Returns the thread, or None when the process had already exited (the op is
    completed here in that case).
    """
    if process.poll() is not None:
        finish(op_id, process.returncode)
        return None

    def _drain():
        try:
            for line in process.stdout:
                track_line(op_id, line)
            for _ in process.stderr:
                pass
            process.wait()
            finish(op_id, process.returncode)
        except Exception:
            app_state.complete_operation(op_id, error=True)

    thread = threading.Thread(target=_drain, daemon=True)
    thread.start()
    return thread
