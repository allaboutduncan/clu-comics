"""Static assets must be clean text.

A single stray NUL byte inside `static/js/problem_files.js` — in a string
literal used as a lookup key — made every button on the Problem Files page do
nothing, silently. The HTML parser rewrites a NUL inside an attribute value to
U+FFFD, so the value written into the DOM was not the value read back out, the
row lookup missed, and each handler returned early with no error anywhere.

Nothing caught it: the file parsed as valid JavaScript, the Python tests all
passed because they exercise the API, and `grep` merely reported "Binary file
matches" rather than failing. This test is the cheap guard for that whole class
of corruption, across every asset the browser has to parse.
"""

import pathlib

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Everything a browser or Jinja has to parse as text.
ASSET_GLOBS = ("static/js/*.js", "static/css/*.css", "templates/**/*.html")

# Tab, LF and CR are the only control characters legitimately in source.
ALLOWED_CONTROL = {0x09, 0x0A, 0x0D}


def _assets():
    for pattern in ASSET_GLOBS:
        for path in sorted(PROJECT_ROOT.glob(pattern)):
            if path.is_file():
                yield path


def _ids(paths):
    return [str(p.relative_to(PROJECT_ROOT)) for p in paths]


ASSETS = list(_assets())


def test_there_are_assets_to_check():
    """Guard the guard: a glob that matches nothing would pass silently."""
    assert len(ASSETS) > 10


@pytest.mark.parametrize("path", ASSETS, ids=_ids(ASSETS))
def test_no_control_bytes(path):
    """No NULs or other stray control bytes.

    A NUL is the dangerous one — it survives a syntax check and is then
    rewritten by the HTML parser — but any unexpected control byte here means
    the file was mangled in transit rather than authored.
    """
    data = path.read_bytes()
    bad = sorted({b for b in data if b < 0x20 and b not in ALLOWED_CONTROL})
    assert not bad, (
        f"{path.relative_to(PROJECT_ROOT)} contains control bytes "
        f"{[hex(b) for b in bad]} — the file has been corrupted"
    )


@pytest.mark.parametrize("path", ASSETS, ids=_ids(ASSETS))
def test_decodes_as_utf8(path):
    """Served with a UTF-8 charset, so it has to actually be UTF-8."""
    try:
        path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as e:
        pytest.fail(f"{path.relative_to(PROJECT_ROOT)} is not valid UTF-8: {e}")
