"""Page stylesheets must not redefine a @keyframes name that base.html owns.

@keyframes names are global and the last definition wins, so collection.css
once declared its own `pulse` and silently replaced the navbar count-badge
animation from base.html on every page that loaded it. No error, no warning;
the badge just animated differently.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KEYFRAMES = re.compile(r"@keyframes\s+([A-Za-z0-9_-]+)")
COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _names(path):
    with open(path, encoding="utf-8") as fh:
        # Comments out: collection.css explains the old clash in prose.
        return set(KEYFRAMES.findall(COMMENT.sub("", fh.read())))


def test_base_html_keyframes_are_found():
    # Guards the regex: an empty set would make the real test vacuous.
    assert "pulse" in _names(os.path.join(ROOT, "templates", "base.html"))


def test_static_css_does_not_shadow_base_keyframes():
    base = _names(os.path.join(ROOT, "templates", "base.html"))
    css_dir = os.path.join(ROOT, "static", "css")
    clashes = []
    for dirpath, _dirs, files in os.walk(css_dir):
        for name in files:
            if name.endswith(".css"):
                path = os.path.join(dirpath, name)
                for kf in sorted(_names(path) & base):
                    clashes.append(f"{os.path.relpath(path, ROOT)}: @keyframes {kf}")
    assert not clashes, "redefines a base.html @keyframes name:\n" + "\n".join(clashes)
