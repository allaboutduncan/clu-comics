"""Ratchet: every script and stylesheet under static/ is loaded by something.

``templates/index.html`` and its 60 KB ``static/js/index.js`` outlived the page
they belonged to by several releases, and a vendored ``zephyr.css`` (300 KB)
was linked by nothing at all. Dead assets still ship in the image, still get
linted and still show up in every repo-wide search, so tests kept pinning
rules on files no route rendered.

A file counts as used when its path below ``static/`` (``js/files.js``,
``css/themes/catppuccin.css``) appears in a template, another script or a
Python module. Generated theme palettes are referenced by an f-string in
``core/themes.py``, so those are matched on their directory instead.
"""
import pathlib

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
STATIC = PROJECT_ROOT / "static"

# Directories never searched for references: agent worktrees are whole copies
# of the repo, and tests naming a file must not be what keeps it alive.
SKIP_DIRS = {".claude", ".git", "tests", "__pycache__", "node_modules", ".pytest_cache"}

# Built by name at runtime rather than written out as a literal path.
REFERENCED_BY_PATTERN = {
    # core/themes.py: f"css/themes/catppuccin-{flavour}.css"
    "css/themes/catppuccin-frappe.css",
    "css/themes/catppuccin-latte.css",
    "css/themes/catppuccin-macchiato.css",
    "css/themes/catppuccin-mocha.css",
}


def _assets():
    found = sorted(
        p for ext in ("js", "css") for p in (STATIC / ext).rglob(f"*.{ext}")
    )
    assert len(found) > 20, "static glob matched almost nothing"
    return found


def _haystack():
    chunks = []
    for path in PROJECT_ROOT.rglob("*"):
        if path.suffix not in {".html", ".js", ".py"} or not path.is_file():
            continue
        if SKIP_DIRS.intersection(path.relative_to(PROJECT_ROOT).parts):
            continue
        chunks.append((path, path.read_text(encoding="utf-8", errors="replace")))
    return chunks


def test_every_script_and_stylesheet_is_referenced():
    sources = _haystack()
    orphans = []
    for asset in _assets():
        rel = asset.relative_to(STATIC).as_posix()
        if rel in REFERENCED_BY_PATTERN:
            continue
        if not any(rel in text for path, text in sources if path != asset):
            orphans.append(rel)
    assert orphans == [], (
        "nothing loads these files; delete them or wire them up: "
        + ", ".join(orphans)
    )


def test_pattern_referenced_files_still_exist():
    """An entry that outlives its file would exempt a future one of that name."""
    missing = [rel for rel in sorted(REFERENCED_BY_PATTERN) if not (STATIC / rel).is_file()]
    assert missing == []
