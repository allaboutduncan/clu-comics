"""helpers.responses.error_response: log the exception, return only the fixed message."""

from unittest.mock import patch

from flask import Flask

from helpers.responses import error_response


def _call(*args, **kwargs):
    app = Flask(__name__)
    with app.app_context():
        resp, code = error_response(*args, **kwargs)
        return resp.get_json(), code


def test_exception_is_logged_with_its_trace_and_not_returned():
    try:
        raise ValueError("secret path /config/comic_utils.db")
    except ValueError as e:
        exc = e
    with patch("helpers.responses.app_logger") as log:
        body, code = _call("Could not do the thing", exc)
    assert code == 500
    assert body == {"success": False, "error": "Could not do the thing"}
    log.error.assert_called_once_with("Could not do the thing", exc_info=exc)


def test_no_exception_logs_nothing():
    with patch("helpers.responses.app_logger") as log:
        body, code = _call("Bad input", code=400)
    assert (body, code) == ({"success": False, "error": "Bad input"}, 400)
    log.error.assert_not_called()


def test_extra_flags_are_included():
    body, code = _call("Conflict", code=409, changed=True)
    assert code == 409 and body["changed"] is True
