"""Guided Setup lifecycle: the ``guided_setup_*`` preferences.

``guided_setup_state`` is one of:

``none``
    A fresh install that has not seen the wizard. The first owner ``GET /``
    redirects to ``/setup`` and moves it to ``in_progress`` (redirect *once*).
``in_progress`` / ``skipped`` / ``completed``
    Set by the wizard. None of them redirect.
``upgraded``
    An install whose database predates the Guided Setup. Never redirected.

Who decides between ``none`` and ``upgraded``? ``init_db`` does, once, the first
time it runs without the pref -- from whether the database had any tables
before it started. A fresh install cannot be recognised from ``libraries``,
because ``init_db`` seeds a ``/data`` library on every install where ``/data``
exists. The schema default (``""``) means "not decided" and never redirects, so
an unreadable store sends the owner to the dashboard, not into a loop.
"""

import re

from core.settings import get_setting, set_setting

STATE_NONE = "none"
STATE_IN_PROGRESS = "in_progress"
STATE_SKIPPED = "skipped"
STATE_COMPLETED = "completed"
STATE_UPGRADED = "upgraded"

# States the wizard may write. ``none`` and ``upgraded`` are init_db's alone.
WRITABLE_STATES = (STATE_IN_PROGRESS, STATE_SKIPPED, STATE_COMPLETED)

# Step and use-case ids are wizard-defined tokens, not free text.
_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def is_valid_token(value):
    return isinstance(value, str) and bool(_TOKEN_RE.match(value))


def get_setup_state():
    """``{state, last_step, path}``; ``path`` is a list of use-case ids."""
    path = str(get_setting("GUIDED_SETUP_PATH") or "")
    return {
        "state": get_setting("GUIDED_SETUP_STATE"),
        "last_step": get_setting("GUIDED_SETUP_LAST_STEP"),
        "path": [p for p in path.split(",") if p],
    }


def set_setup_state(state=None, last_step=None, path=None):
    """Write whichever fields are given. Validation is the caller's job."""
    ok = True
    if state is not None:
        ok = set_setting("GUIDED_SETUP_STATE", state) and ok
    if last_step is not None:
        ok = set_setting("GUIDED_SETUP_LAST_STEP", last_step) and ok
    if path is not None:
        ok = set_setting("GUIDED_SETUP_PATH", ",".join(path)) and ok
    return ok


def claim_first_run():
    """True once, for the request that should redirect to ``/setup``.

    Moves ``none`` to ``in_progress`` so the next ``GET /`` shows the
    dashboard. If the write fails we still return False: a redirect that
    cannot be recorded would repeat on every visit.
    """
    if get_setting("GUIDED_SETUP_STATE") != STATE_NONE:
        return False
    return bool(set_setting("GUIDED_SETUP_STATE", STATE_IN_PROGRESS))
