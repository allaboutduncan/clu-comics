"""Custom folder covers from the clu-custom-covers repository.

``allaboutduncan/clu-custom-covers`` holds one directory per publisher
(``Marvel/``, ``DC Comics/``...) with ready-made folder art: a ``folder.*``, a
``<Publisher>.*``, a Codex-style ``.codex-cover.*``, and for a few publishers a
``header.*`` banner. The folder menu's "Choose custom cover…" picker shows the
images for the publisher a folder is named after (or any other, by search) and
stores the one picked through :mod:`core.folder_art` as ``source='custom'``.

Things that look arbitrary and are not:

- **One request builds the whole index.** The repository's recursive git tree
  lists every file, so a picker never lists directories one by one. It is
  cached in memory and in ``CACHE_DIR/custom_covers_index.json`` for
  ``INDEX_TTL_SECONDS``; GitHub's unauthenticated API allows 60 requests an
  hour, and the repository changes rarely.
- **A failed refresh serves the stale index, and no index at all is ``None``,
  never ``{}``.** An empty dict would let the picker say "no covers for this
  publisher" when the truth is "GitHub could not be reached".
- **Only a path that is in the index can be downloaded or previewed.** The
  client names a path; the server builds the URL. That is what keeps the
  preview proxy from becoming a fetcher for arbitrary URLs.
- **Downloads are capped and decoded** before anything is stored, the same as
  an uploaded image.
- **The browser never talks to GitHub.** Previews are proxied, so the page
  works behind a reverse proxy with a strict CSP and the viewer's address is
  not handed to a third party.
"""

import json
import os
import re
import threading
import time
from io import BytesIO
from urllib.parse import quote

import requests

from core.app_logging import app_logger
from core.config import get_cache_dir

REPO = "allaboutduncan/clu-custom-covers"
TREE_URL = f"https://api.github.com/repos/{REPO}/git/trees/HEAD?recursive=1"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/HEAD/"

INDEX_TTL_SECONDS = 24 * 60 * 60
REQUEST_TIMEOUT = 20
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_SEARCH_RESULTS = 30

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

_lock = threading.Lock()
_memory = {"index": None, "fetched_at": 0.0}


#########################
#   Names               #
#########################

def normalize_name(name):
    """A publisher name reduced to what matching should compare.

    Case-, punctuation- and space-blind, so ``Devil_s Due`` (the repository's
    spelling, since ``'`` is awkward in a path) matches a folder called
    ``Devil's Due``, and ``BOOM! Studios`` matches ``Boom Studios``. A leading
    "The" is dropped for the same reason the series matchers drop it.
    """
    text = (name or "").casefold()
    text = re.sub(r"^the\s+", "", text.strip())
    return re.sub(r"[\W_]+", "", text)


#########################
#   Index               #
#########################

def _cover_rank(publisher, filename):
    """Sort key for a publisher's cover candidates: folder.*, <Publisher>.*, .codex-cover.*, rest."""
    stem = os.path.splitext(filename)[0].casefold()
    if stem == "folder":
        return 0
    if normalize_name(stem) == normalize_name(publisher):
        return 1
    if stem == ".codex-cover":
        return 2
    return 3


def build_index(tree):
    """``{publisher: {'name', 'covers': [path...], 'headers': [path...]}}`` from a git tree.

    Only images one level down (``<Publisher>/<file>``) count; the repo root
    holds templates. A file whose name contains "header" is a banner, everything
    else a cover. Byte-identical files (same blob sha -- the repo carries the
    same image as ``Marvel.png`` and ``.codex-cover.png``) are listed once,
    under the better-ranked name.
    """
    staged = {}
    for item in tree or []:
        if item.get("type") != "blob":
            continue
        path = item.get("path") or ""
        parts = path.split("/")
        if len(parts) != 2:
            continue
        publisher, filename = parts
        if not filename.lower().endswith(IMAGE_EXTENSIONS):
            continue
        staged.setdefault(publisher, []).append((filename, path, item.get("sha")))

    index = {}
    for publisher, files in staged.items():
        covers, headers, seen = [], [], set()
        files.sort(key=lambda f: (_cover_rank(publisher, f[0]), f[0].casefold()))
        for filename, path, sha in files:
            if sha and sha in seen:
                continue
            seen.add(sha)
            if "header" in filename.casefold():
                headers.append(path)
            else:
                covers.append(path)
        if covers or headers:
            index[publisher] = {"name": publisher, "covers": covers, "headers": headers}
    return index


def _disk_cache_path():
    return os.path.join(get_cache_dir(), "custom_covers_index.json")


def _read_disk_cache():
    try:
        with open(_disk_cache_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        index = data.get("index")
        if isinstance(index, dict):
            return index, float(data.get("fetched_at") or 0)
    except FileNotFoundError:
        pass
    except Exception as e:
        app_logger.debug(f"Custom covers disk cache unreadable: {e}")
    return None, 0.0


def _write_disk_cache(index, fetched_at):
    path = _disk_cache_path()
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"index": index, "fetched_at": fetched_at}, fh)
        os.replace(tmp, path)
    except Exception as e:
        app_logger.debug(f"Custom covers disk cache not written: {e}")


def _fetch_tree():
    resp = requests.get(
        TREE_URL,
        timeout=REQUEST_TIMEOUT,
        headers={"Accept": "application/vnd.github+json"},
    )
    resp.raise_for_status()
    return resp.json().get("tree", [])


def reset_state():
    """Forget the in-memory index. Test-support only."""
    with _lock:
        _memory["index"] = None
        _memory["fetched_at"] = 0.0


def get_index(force=False):
    """The publisher index, or None when it has never been fetched and cannot be.

    Fresh memory, then a fresh disk cache, then GitHub. If GitHub fails, the
    newest stale copy is returned rather than nothing.
    """
    now = time.time()
    with _lock:
        if not force and _memory["index"] is not None and \
                now - _memory["fetched_at"] < INDEX_TTL_SECONDS:
            return _memory["index"]

        disk_index, disk_at = _read_disk_cache()
        if not force and disk_index is not None and now - disk_at < INDEX_TTL_SECONDS:
            _memory["index"], _memory["fetched_at"] = disk_index, disk_at
            return disk_index

        try:
            index = build_index(_fetch_tree())
        except Exception as e:
            stale = _memory["index"] if _memory["index"] is not None else disk_index
            app_logger.warning(
                f"Could not fetch the custom covers index from GitHub: {e}"
                + (" (using a cached copy)" if stale is not None else "")
            )
            return stale

        _memory["index"], _memory["fetched_at"] = index, now
        _write_disk_cache(index, now)
        app_logger.info(f"Custom covers index refreshed: {len(index)} publishers")
        return index


def match(folder_name, index=None):
    """The index entry for a folder named after a publisher, or None."""
    if index is None:
        index = get_index()
    if not index:
        return None
    wanted = normalize_name(folder_name)
    if not wanted:
        return None
    for publisher, entry in index.items():
        if normalize_name(publisher) == wanted:
            return entry
    return None


def search_page(query, index=None, offset=0, limit=MAX_SEARCH_RESULTS):
    """One page of the index entries whose name contains the query, best first.

    Returns ``(entries, total)``: the picker loads further pages as the user
    scrolls, so ``total`` is every hit, not the page. An empty query lists
    every publisher alphabetically.
    """
    if index is None:
        index = get_index()
    if not index:
        return [], 0
    q = normalize_name(query)
    hits = []
    for publisher, entry in index.items():
        norm = normalize_name(publisher)
        if not q or q in norm:
            # Prefix matches first, then by name.
            hits.append((0 if norm.startswith(q) else 1, publisher.casefold(), entry))
    hits.sort(key=lambda h: (h[0], h[1]))
    offset = max(0, offset)
    return [h[2] for h in hits[offset:offset + max(0, limit)]], len(hits)


def search(query, index=None, limit=MAX_SEARCH_RESULTS):
    """The first ``limit`` entries of :func:`search_page`."""
    return search_page(query, index, 0, limit)[0]


def is_known_path(path, index=None):
    """True when ``path`` is an image the index lists. The SSRF guard."""
    if not path or not isinstance(path, str):
        return False
    if index is None:
        index = get_index()
    if not index:
        return False
    publisher = path.split("/", 1)[0]
    entry = index.get(publisher)
    return bool(entry) and (path in entry["covers"] or path in entry["headers"])


def kind_of(path, index=None):
    """``'header'`` or ``'folder'`` for an indexed path, else None."""
    if index is None:
        index = get_index()
    if not is_known_path(path, index):
        return None
    entry = index[path.split("/", 1)[0]]
    return "header" if path in entry["headers"] else "folder"


#########################
#   Download            #
#########################

class CustomCoverError(Exception):
    """A cover could not be fetched or is not usable. The message is for the user."""


def raw_url(path):
    return RAW_URL + "/".join(quote(part) for part in path.split("/"))


def download(path, index=None):
    """``(bytes, ext)`` for an indexed image. Raises :class:`CustomCoverError`."""
    from PIL import Image

    if not is_known_path(path, index):
        raise CustomCoverError("That image is not in the custom covers repository")
    ext = os.path.splitext(path)[1].lower()

    try:
        with requests.get(raw_url(path), timeout=REQUEST_TIMEOUT, stream=True) as resp:
            resp.raise_for_status()
            buf = BytesIO()
            for chunk in resp.iter_content(64 * 1024):
                buf.write(chunk)
                if buf.tell() > MAX_IMAGE_BYTES:
                    raise CustomCoverError("The image is too large")
            data = buf.getvalue()
    except CustomCoverError:
        raise
    except Exception as e:
        app_logger.warning(f"Could not download custom cover {path}: {e}")
        raise CustomCoverError("Could not download the image from GitHub")

    try:
        with Image.open(BytesIO(data)) as img:
            img.verify()
    except Exception:
        raise CustomCoverError("The downloaded file is not a readable image")
    return data, ext
