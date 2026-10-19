"""JSON error responses that never carry exception text to the client.

An exception's message (and certainly its traceback) can hold paths, SQL and
library internals, so it goes to the log and the client gets the fixed ``msg``.
"""

from flask import jsonify

from core.app_logging import app_logger


def error_response(msg, exc=None, code=500, **extra):
    """``({"success": False, "error": msg, **extra}, code)``; logs ``exc`` with its trace.

    ``extra`` is for fixed, non-sensitive flags the page branches on (e.g.
    ``changed=True``) -- never for anything derived from ``exc``.
    """
    if exc is not None:
        app_logger.error(msg, exc_info=exc)   # full trace goes to logs
    return jsonify({"success": False, "error": msg, **extra}), code
