"""GitHub repositories the reading-list "Import from GitHub" browser offers.

Stored in the ``CBL_GITHUB_REPOS`` setting as a comma-separated list of repo
specs, ``owner/repo`` or ``owner/repo@branch``. A spec without a branch reads
the repository's default branch through the ref ``HEAD``, which both the git
trees API and raw.githubusercontent.com resolve, so no extra request is spent
finding out what the default branch is called.
"""

import re
from urllib.parse import quote

from core.settings import get_setting, set_setting

DEFAULT_REPO = "DieselTech/CBL-ReadingLists"

# GitHub's own limits: a login is alphanumerics and hyphens, a repo name adds
# '.' and '_'. A branch may hold '/', but never ',' (the list separator) or
# whitespace, and never '..' (it must not climb out of the URL path).
_REPO_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9-]{1,39})/(?P<repo>[A-Za-z0-9._-]{1,100})"
    r"(?:@(?P<ref>[A-Za-z0-9._/-]{1,200}))?$"
)
_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)"
    r"(?:\.git)?(?:/tree/(?P<ref>[^\s?#]+?))?/?(?:[?#].*)?$",
    re.IGNORECASE,
)


def normalize_repo(text):
    """Return the canonical spec for a repo name or GitHub URL, or None.

    Accepts ``owner/repo``, ``owner/repo@branch``, ``https://github.com/owner/repo``
    and ``https://github.com/owner/repo/tree/branch``.
    """
    text = (text or "").strip()
    if not text:
        return None
    m = _URL_RE.match(text)
    if m:
        spec = f"{m['owner']}/{m['repo']}"
        if m["ref"]:
            spec += f"@{m['ref']}"
    else:
        spec = text
    m = _REPO_RE.match(spec)
    if not m or m["repo"] in (".", "..") or (m["ref"] and ".." in m["ref"]):
        return None
    return spec


def split_repo(spec):
    """``(owner/repo, ref)`` for a canonical spec; ref is ``HEAD`` when unset."""
    name, _, ref = spec.partition("@")
    return name, ref or "HEAD"


def get_repos():
    """The configured specs, canonical and de-duplicated, in the user's order."""
    out = []
    for item in str(get_setting("CBL_GITHUB_REPOS") or "").split(","):
        spec = normalize_repo(item)
        if spec and spec.lower() not in (s.lower() for s in out):
            out.append(spec)
    return out


def set_repos(specs):
    """Store a list of already-normalized specs. Returns set_setting's result."""
    return set_setting("CBL_GITHUB_REPOS", ",".join(specs))


def tree_api_url(spec):
    name, ref = split_repo(spec)
    return f"https://api.github.com/repos/{name}/git/trees/{quote(ref, safe='/')}?recursive=1"


def raw_file_url(spec, path):
    """Raw URL of ``path`` in ``spec``. The path is quoted: CBL names carry '#'."""
    name, ref = split_repo(spec)
    return (f"https://raw.githubusercontent.com/{name}/{quote(ref, safe='/')}/"
            + quote(path, safe="/"))


def html_url(spec):
    name, ref = split_repo(spec)
    base = f"https://github.com/{name}"
    return base if ref == "HEAD" else f"{base}/tree/{quote(ref, safe='/')}"
