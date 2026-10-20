"""Mylar3 importer: bring a Mylar user's watchlist and settings into CLU.

Three phases, each its own entry point:

1. **Stage** (``new_import`` + ``load_import``). The files arrive by browser
   upload or from the drop folder ``<cache>/mylar_import/``, and either way are
   copied into a private staging directory. Nothing ever opens Mylar's live
   ``mylar.db``. Staged files hold secrets (client passwords, API keys), so they
   are deleted as soon as they are parsed; the parsed import lives in memory
   until it is applied or ``IMPORT_TTL`` passes. Files the user put in the
   drop folder are theirs and are never deleted.
2. **Preview** (``build_preview``). Makes no Metron calls. It translates every
   Mylar ``ComicLocation`` into a CLU path through an editable root mapping,
   says what would happen to each series, and diffs each setting against what
   CLU already has.
3. **Apply** (``start_apply_job``). A background job: settings first (so an
   imported ComicVine key is available as the fallback), then ComicVine id ->
   Metron series, folders, mapping, issue sync, Mylar's per-issue statuses and
   the Paused flag, then one wanted-cache rebuild.

Things that look arbitrary and are not:

- **Already-mapped series are skipped, never overwritten.** That is what makes a
  re-run safe, and why a run cut short by Metron's daily limit finishes on the
  next attempt.
- **A rate-limited or auth-blocked Metron defers the rest of the run.**
  ``get_series_id_by_comicvine_id`` returns None for "not on Metron",
  "rate-limited" and "blocked" alike. Falling back to a ComicVine-only id in the
  last two cases would map the series under the wrong id for good.
- **A Mylar issue status only fills a blank.** An issue the user has already
  marked in CLU keeps its mark.
- **Settings follow "fill blanks, opt in to replace".** The preview reports each
  one as ``new``, ``same`` or ``differs``; the page pre-ticks only ``new``.
"""

import os
import posixpath
import shutil
import threading
import time
import uuid

from core.app_logging import app_logger
from core.mylar_reader import MylarReadError, read_config, read_mylar_db

DB_NAME = "mylar.db"
INI_NAME = "config.ini"
STAGING_SUBDIR = ".staging"

IMPORT_TTL = 3600       # staged uploads hold secrets: gone after an hour
_JOB_TTL = 900          # finished jobs kept 15 min for the page to fetch
_JOB_LIST_CAP = 500     # per result list, like library_automap

# Mylar issue status -> CLU manual status. Every other status gets no mark:
# Wanted is derived from missing files, and Downloaded is found by the matcher.
ISSUE_STATUS_MAP = {
    "skipped": "skipped",
    "ignored": "skipped",
    "archived": "owned",
}

_imports = {}
_imports_lock = threading.Lock()
_jobs = {}
_jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Seams (tests patch these names)
# ---------------------------------------------------------------------------

def get_cache_dir():
    from core.config import get_cache_dir as _cache_dir

    return _cache_dir()


def get_library_roots():
    from helpers.library import get_library_roots as _roots

    return _roots()


def _refresh_wanted_cache():
    from models.library_automap import _refresh_wanted_cache as _refresh

    _refresh()


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def drop_folder():
    return os.path.join(get_cache_dir(), "mylar_import")


def drop_folder_status():
    folder = drop_folder()
    return {
        "path": folder,
        "mylar_db": os.path.isfile(os.path.join(folder, DB_NAME)),
        "config_ini": os.path.isfile(os.path.join(folder, INI_NAME)),
    }


def _discard_locked(import_id):
    record = _imports.pop(import_id, None)
    if record:
        shutil.rmtree(record["dir"], ignore_errors=True)


def _prune_imports_locked():
    now = time.time()
    for import_id in [
        iid for iid, rec in _imports.items()
        if not rec.get("applying") and now - rec["created"] > IMPORT_TTL
    ]:
        _discard_locked(import_id)


def discard_import(import_id):
    with _imports_lock:
        _discard_locked(import_id)


def new_import():
    """Create an empty staging directory. Returns ``(import_id, dir)``.

    The caller writes ``mylar.db`` and/or ``config.ini`` into it and then calls
    :func:`load_import`.
    """
    import_id = uuid.uuid4().hex
    folder = os.path.join(drop_folder(), STAGING_SUBDIR, import_id)
    os.makedirs(folder, exist_ok=True)
    with _imports_lock:
        _prune_imports_locked()
        _imports[import_id] = {
            "dir": folder, "created": time.time(),
            "watchlist": None, "config": None, "mapping": None,
        }
    return import_id, folder


def stage_from_drop_folder(folder):
    """Copy the drop folder's files into a staging dir. Returns names copied."""
    copied = []
    for name in (DB_NAME, INI_NAME):
        src = os.path.join(drop_folder(), name)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(folder, name))
            copied.append(name)
    return copied


def load_import(import_id):
    """Parse the staged files. Raises ``MylarReadError`` (and discards) on bad input."""
    with _imports_lock:
        record = _imports.get(import_id)
    if not record:
        raise MylarReadError("This import has expired; load the files again.")

    db_path = os.path.join(record["dir"], DB_NAME)
    ini_path = os.path.join(record["dir"], INI_NAME)
    try:
        watchlist = read_mylar_db(db_path) if os.path.isfile(db_path) else None
        config = read_config(ini_path) if os.path.isfile(ini_path) else None
    except MylarReadError:
        discard_import(import_id)
        raise
    if watchlist is None and config is None:
        discard_import(import_id)
        raise MylarReadError("No mylar.db or config.ini was provided.")

    # The database copy is no longer needed once read; the ini holds secrets
    # the apply step reads back from memory, so drop both files now.
    for path in (db_path, ini_path):
        try:
            os.remove(path)
        except OSError:
            pass

    with _imports_lock:
        record["watchlist"] = watchlist
        record["config"] = config
        record["mapping"] = detect_path_mapping(
            (watchlist or {}).get("series", []),
            (config or {}).get("destination_dir"),
        )
    return record


def get_import(import_id):
    with _imports_lock:
        _prune_imports_locked()
        return _imports.get(import_id)


# ---------------------------------------------------------------------------
# Path mapping
# ---------------------------------------------------------------------------

def _slash(path):
    """Mylar's paths are its container's; compare them with '/' separators."""
    return (path or "").replace("\\", "/").rstrip("/")


def _is_under(path, root):
    if not root:
        return False
    if path == root:
        return True
    # A Windows-hosted Mylar ("D:/Comics") compares case-insensitively.
    if len(root) > 1 and root[1] == ":":
        path, root = path.lower(), root.lower()
    return path.startswith(root + "/")


def _rel_parts(path, root):
    return [p for p in path[len(root):].split("/") if p]


def detect_path_mapping(series, destination_dir, roots=None):
    """Guess which CLU library root each Mylar root corresponds to.

    Mylar's ``destination_dir`` is its library root. For each CLU root, count
    how many series folders exist at the same relative path, and pick the one
    with the most. Locations outside ``destination_dir`` (Mylar's
    ``multiple_dest_dirs``) are grouped by their parent folder and matched by
    folder name the same way.

    Returns a list of ``{mylar_root, clu_root, found, total}``. With no hits,
    the first library root is proposed; ``found == 0`` tells the page to say so.
    """
    roots = list(roots if roots is not None else get_library_roots())
    locations = [_slash(s.get("location")) for s in series if s.get("location")]
    dest = _slash(destination_dir)

    groups = {}
    for loc in locations:
        if dest and _is_under(loc, dest) and loc != dest:
            key = dest
        else:
            key = posixpath.dirname(loc) or "/"
        groups.setdefault(key, []).append(loc)
    if dest and dest not in groups and not locations:
        groups[dest] = []

    mapping = []
    for mylar_root, locs in sorted(groups.items()):
        best_root, best_hits = (roots[0] if roots else ""), 0
        for root in roots:
            hits = sum(
                1 for loc in locs
                if os.path.isdir(os.path.join(root, *_rel_parts(loc, mylar_root)))
            )
            if hits > best_hits:
                best_root, best_hits = root, hits
        mapping.append({
            "mylar_root": mylar_root,
            "clu_root": best_root,
            "found": best_hits,
            "total": len(locs),
        })
    return mapping


def _within_roots(path, roots):
    normalized = os.path.normpath(path)
    for root in roots:
        root_n = os.path.normpath(root)
        if normalized == root_n or normalized.startswith(root_n.rstrip(os.sep) + os.sep):
            return True
    return False


def translate_location(location, mapping):
    """Mylar ``ComicLocation`` -> the CLU folder for it, or None if unmapped.

    The longest matching Mylar root wins. When the folder already exists under
    its literal name, that name is used; otherwise each segment goes through
    the one filename sanitizer, as Subscribe does, because it will be created.
    """
    loc = _slash(location)
    if not loc:
        return None
    best = None
    for row in mapping or []:
        mylar_root = _slash(row.get("mylar_root"))
        clu_root = (row.get("clu_root") or "").strip()
        if not mylar_root or not clu_root or not _is_under(loc, mylar_root):
            continue
        if best is None or len(mylar_root) > len(_slash(best["mylar_root"])):
            best = row
    if best is None:
        return None

    parts = _rel_parts(loc, _slash(best["mylar_root"]))
    if not parts:
        return None
    clu_root = best["clu_root"].strip()
    literal = os.path.join(clu_root, *parts)
    if os.path.isdir(literal):
        return literal
    from helpers import sanitize_path_segment

    clean = [sanitize_path_segment(p) for p in parts]
    clean = [p for p in clean if p]
    return os.path.join(clu_root, *clean) if clean else None


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def _norm_path(path):
    return os.path.normcase(os.path.normpath(path)) if path else ""


def _mapped_index():
    """``(by_cv_id, by_path)`` over every series already on the Pull List."""
    from core.database import get_all_mapped_series
    from helpers.comicvine_ids import cv_id_from_series_id, is_comicvine_series_id

    by_cv, by_path = {}, {}
    for row in get_all_mapped_series():
        cv = row.get("cv_id")
        if not cv and is_comicvine_series_id(row.get("id")):
            cv = cv_id_from_series_id(row["id"])
        try:
            cv = int(cv) if cv else None
        except (TypeError, ValueError):
            cv = None
        if cv:
            by_cv.setdefault(cv, row)
        by_path[_norm_path(row.get("mapped_path"))] = row
    return by_cv, by_path


def _status_counts(issues):
    counts = {"skipped": 0, "owned": 0}
    for issue in issues:
        mark = ISSUE_STATUS_MAP.get((issue.get("status") or "").lower())
        if mark:
            counts[mark] += 1
    return counts


def series_rows(record, mapping=None):
    """One row per Mylar series: where it goes in CLU, and what will happen.

    ``state`` is one of ``exists`` / ``will_create`` (importable),
    ``already_mapped`` / ``path_taken`` / ``duplicate`` (left alone) or
    ``invalid`` (no usable folder).
    """
    mapping = mapping if mapping is not None else record.get("mapping") or []
    series = (record.get("watchlist") or {}).get("series", [])
    roots = get_library_roots()
    by_cv, by_path = _mapped_index()

    rows = []
    claimed = set()
    for s in series:
        path = translate_location(s.get("location"), mapping)
        counts = _status_counts(s.get("issues") or [])
        row = {
            "cv_id": s["cv_id"],
            "name": s["name"],
            "year": s.get("year"),
            "publisher": s.get("publisher"),
            "mylar_location": s.get("location"),
            "path": path,
            "paused": s.get("status") == "Paused",
            "issue_count": len(s.get("issues") or []),
            "skipped_count": counts["skipped"],
            "owned_count": counts["owned"],
            "state": None,
            "reason": None,
        }
        existing = by_cv.get(s["cv_id"])
        if existing:
            row["state"] = "already_mapped"
            row["reason"] = f"Already on the Pull List at {existing.get('mapped_path')}"
        elif not s.get("location"):
            row["state"], row["reason"] = "invalid", "Mylar has no folder for this series"
        elif not path:
            row["state"], row["reason"] = "invalid", "No path mapping covers this folder"
        elif not _within_roots(path, roots):
            row["state"], row["reason"] = "invalid", "Not inside a CLU library"
        elif _norm_path(path) in by_path:
            other = by_path[_norm_path(path)]
            row["state"] = "path_taken"
            row["reason"] = f"Folder is already mapped to {other.get('name') or 'another series'}"
        elif _norm_path(path) in claimed:
            row["state"], row["reason"] = "duplicate", "Another Mylar series uses this folder"
        else:
            row["state"] = "exists" if os.path.isdir(path) else "will_create"
        if path and row["state"] in ("exists", "will_create"):
            claimed.add(_norm_path(path))
        rows.append(row)
    return rows


def _norm_url(url):
    text = (url or "").strip().lower().rstrip("/")
    if text.endswith("/api"):
        text = text[:-4]
    for scheme in ("https://", "http://"):
        if text.startswith(scheme):
            text = text[len(scheme):]
    return text


_CLIENT_FIELDS = ("host", "port", "use_ssl", "url_base", "api_key", "username",
                  "password", "category", "priority")


def _client_payload(cfg):
    """The CLU config fields to write for a Mylar client (None values dropped)."""
    return {k: cfg.get(k) for k in _CLIENT_FIELDS if cfg.get(k) is not None}


def _diff_state(new, existing, keys):
    if not existing:
        return "new"
    for key in keys:
        if new.get(key) is not None and str(new.get(key)) != str(existing.get(key) or ""):
            if not (key in ("use_ssl",) and bool(new.get(key)) == bool(existing.get(key))):
                return "differs"
    return "same"


def settings_preview(config):
    """What each importable setting would do, without exposing any secret."""
    from core.database import (
        get_all_indexers,
        get_download_client_config,
        get_indexer,
        get_provider_credentials,
    )

    out = {"comicvine": None, "client": None, "indexers": []}
    if not config:
        return out

    cv_key = config.get("comicvine_api")
    if cv_key:
        current = (get_provider_credentials("comicvine") or {}).get("api_key")
        out["comicvine"] = {
            "state": "new" if not current else ("same" if current == cv_key else "differs"),
        }

    client_type = config.get("nzb_downloader")
    cfg = config.get(client_type) if client_type in ("sabnzbd", "nzbget") else None
    if cfg:
        payload = _client_payload(cfg)
        existing = get_download_client_config(client_type)
        out["client"] = {
            "type": client_type,
            "state": _diff_state(payload, existing, _CLIENT_FIELDS),
            "host": cfg.get("host"),
            "port": cfg.get("port"),
            "use_ssl": bool(cfg.get("use_ssl")),
            "url_base": cfg.get("url_base"),
            "category": cfg.get("category"),
            "has_api_key": bool(cfg.get("api_key")),
            "has_password": bool(cfg.get("password")),
        }

    existing_by_url = {}
    for row in get_all_indexers():
        existing_by_url.setdefault(_norm_url(row.get("url")), row)
    for index, provider in enumerate(config.get("newznabs") or []):
        match = existing_by_url.get(_norm_url(provider["url"]))
        state = "new"
        if match:
            full = get_indexer(match["id"]) or {}
            same = (
                (full.get("api_key") or None) == provider.get("api_key")
                and (full.get("categories") or "") == provider.get("categories")
            )
            state = "same" if same else "differs"
        out["indexers"].append({
            "index": index,
            "name": provider["name"],
            "url": provider["url"],
            "categories": provider["categories"],
            "enabled": provider["enabled"],
            "has_api_key": bool(provider.get("api_key")),
            "state": state,
            "existing_id": match["id"] if match else None,
        })
    return out


def build_preview(import_id, mapping=None):
    """The whole preview for the page. Saves ``mapping`` when one is given."""
    record = get_import(import_id)
    if not record:
        raise MylarReadError("This import has expired; load the files again.")
    if mapping is not None:
        mapping = [
            {"mylar_root": (m.get("mylar_root") or "").strip(),
             "clu_root": (m.get("clu_root") or "").strip()}
            for m in mapping if isinstance(m, dict)
        ]
        with _imports_lock:
            record["mapping"] = mapping

    watchlist = record.get("watchlist") or {}
    config = record.get("config") or {}
    rows = series_rows(record) if record.get("watchlist") else []
    counts = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1

    not_imported = []
    if config.get("torznab_count"):
        not_imported.append(f"{config['torznab_count']} Torznab indexer(s): CLU has no torrent support")
    if config.get("torrents_enabled"):
        not_imported.append("Torrent client settings: CLU has no torrent support")
    if config.get("nzb_downloader") == "blackhole":
        not_imported.append("Blackhole NZB folder: CLU sends NZBs to SABnzbd or NZBGet")
    if config.get("newznab_error"):
        not_imported.append(config["newznab_error"])
    if watchlist.get("annual_count"):
        not_imported.append(
            f"{watchlist['annual_count']} annual issue(s): annuals are not imported as separate series"
        )
    for item in watchlist.get("excluded") or []:
        not_imported.append(f"{item['name']}: {item['reason']}")

    return {
        "import_id": import_id,
        "has_db": bool(record.get("watchlist")),
        "has_config": bool(record.get("config")),
        "mapping": record.get("mapping") or [],
        "library_roots": get_library_roots(),
        "series": rows,
        "series_counts": counts,
        "settings": settings_preview(record.get("config")),
        "folder_format": config.get("folder_format"),
        "not_imported": not_imported,
    }


# ---------------------------------------------------------------------------
# Apply job
# ---------------------------------------------------------------------------

def _prune_jobs_locked():
    now = time.time()
    for op_id in [
        oid for oid, job in _jobs.items()
        if job["status"] in ("done", "error")
        and now - job.get("finished_at", now) > _JOB_TTL
    ]:
        del _jobs[op_id]


def _update_job(op_id, **fields):
    with _jobs_lock:
        job = _jobs.get(op_id)
        if job:
            job.update(fields)


def get_job(op_id):
    with _jobs_lock:
        _prune_jobs_locked()
        job = _jobs.get(op_id)
        return dict(job) if job else None


def latest_job():
    """The running import, else the most recent one still kept; or None.

    The page asks for this on load, so leaving it and coming back picks the
    import up where it is instead of offering to start another.
    """
    with _jobs_lock:
        _prune_jobs_locked()
        if not _jobs:
            return None
        running = [j for j in _jobs.values() if j["status"] == "running"]
        job = running[0] if running else max(_jobs.values(), key=lambda j: j["started_at"])
        return dict(job)


def _cap(items):
    return items[:_JOB_LIST_CAP], len(items), len(items) > _JOB_LIST_CAP


def start_apply_job(app, import_id, selections):
    """Start the import in the background. Returns the op id to poll.

    ``selections``: ``{"series": [cv_id, ...], "comicvine": bool,
    "client": bool, "indexers": [index, ...]}``. Raises ``MylarReadError`` for
    an unknown import and ``RuntimeError`` when an import is already running.
    """
    from core.app_state import register_operation

    record = get_import(import_id)
    if not record:
        raise MylarReadError("This import has expired; load the files again.")
    with _jobs_lock:
        if any(job["status"] == "running" for job in _jobs.values()):
            raise RuntimeError("A Mylar3 import is already running")
        _prune_jobs_locked()
        op_id = uuid.uuid4().hex
        _jobs[op_id] = {
            "id": op_id, "status": "running", "current": 0, "total": 0,
            "detail": "Starting...", "result": None, "error": None,
            "started_at": time.time(),
        }
    with _imports_lock:
        record["applying"] = True
    register_operation("mylar_import", "Importing from Mylar3", op_id=op_id)
    threading.Thread(
        target=_run_apply_job, args=(op_id, app, import_id, selections), daemon=True
    ).start()
    return op_id


def _run_apply_job(op_id, app, import_id, selections):
    from core.app_state import complete_operation

    error = False
    with app.app_context():
        try:
            _apply(op_id, app, import_id, selections or {})
        except Exception as e:
            error = True
            app_logger.error(f"Mylar3 import {op_id} failed: {e}")
            _update_job(op_id, status="error", error=str(e), finished_at=time.time())
        finally:
            discard_import(import_id)
            complete_operation(op_id, error=error)


def _progress(op_id, current, total, detail):
    from core.app_state import update_operation

    _update_job(op_id, current=current, total=total, detail=detail)
    update_operation(op_id, current=current, total=total, detail=detail)


def _apply(op_id, app, import_id, selections):
    record = get_import(import_id)
    if not record:
        raise MylarReadError("This import has expired; load the files again.")
    config = record.get("config") or {}

    result = {"settings": apply_settings(app, config, selections, op_id=op_id)}

    wanted = {int(c) for c in selections.get("series") or [] if str(c).isdigit()}
    rows = [
        r for r in (series_rows(record) if record.get("watchlist") else [])
        if r["cv_id"] in wanted and r["state"] in ("exists", "will_create")
    ]
    mylar_by_cv = {s["cv_id"]: s for s in record["watchlist"]["series"]} if rows else {}
    result.update(import_series(app, rows, mylar_by_cv, op_id=op_id))

    # Counted before the lists are capped.
    result["source_counts"] = {}
    for item in result.get("mapped") or []:
        source = item.get("source")
        result["source_counts"][source] = result["source_counts"].get(source, 0) + 1

    for key in ("mapped", "deferred", "unresolved", "failed", "skipped"):
        items, total, truncated = _cap(result.get(key) or [])
        result[key] = items
        result[f"{key}_total"] = total
        result[f"{key}_truncated"] = truncated
    _update_job(op_id, status="done", result=result, finished_at=time.time(),
                detail="Done")
    app_logger.info(
        f"Mylar3 import: {result['mapped_total']} series mapped, "
        f"{result['deferred_total']} deferred, {result['unresolved_total']} unresolved, "
        f"{result['failed_total']} failed"
    )


def apply_settings(app, config, selections, op_id=None):
    """Write the ticked settings. Returns a per-setting report."""
    report = {"comicvine": None, "client": None, "indexers": [], "source_priority": None}
    if not config:
        return report
    preview = settings_preview(config)

    if selections.get("comicvine") and config.get("comicvine_api"):
        if op_id:
            _progress(op_id, 0, 0, "Saving the ComicVine API key...")
        report["comicvine"] = _apply_comicvine_key(app, config["comicvine_api"])

    if selections.get("client") and preview["client"]:
        if op_id:
            _progress(op_id, 0, 0, "Saving and testing the download client...")
        client_type = preview["client"]["type"]
        report["client"] = _apply_client(client_type, config[client_type])

    chosen = {int(i) for i in selections.get("indexers") or [] if str(i).isdigit()}
    for item in preview["indexers"]:
        if item["index"] not in chosen or item["state"] == "same":
            continue
        if op_id:
            _progress(op_id, 0, 0, f"Saving and testing indexer {item['name']}...")
        provider = config["newznabs"][item["index"]]
        report["indexers"].append(_apply_indexer(provider, item.get("existing_id")))

    report["source_priority"] = _enable_usenet_source()
    return report


def _apply_comicvine_key(app, key):
    from core.config import load_flask_config
    from core.database import get_provider_credentials, save_provider_credentials

    existing = get_provider_credentials("comicvine") or {}
    if not save_provider_credentials("comicvine", {**existing, "api_key": key}):
        return {"saved": False, "error": "Could not save the ComicVine key"}
    try:
        load_flask_config(app)
    except Exception as e:
        app_logger.warning(f"Mylar3 import: reloading config after the ComicVine key failed: {e}")
    return {"saved": True}


def _apply_client(client_type, cfg):
    from core.database import (
        get_active_download_client,
        get_download_client_config,
        save_download_client_config,
        set_active_download_client,
        update_download_client_validity,
    )
    from models.download_clients import (
        DownloadClientConfig,
        get_client_group,
        get_download_client_by_name,
    )

    merged = {**(get_download_client_config(client_type) or {}), **_client_payload(cfg)}
    group = get_client_group(client_type)
    if not save_download_client_config(client_type, merged, client_group=group):
        return {"type": client_type, "saved": False, "error": "Could not save the client"}

    report = {"type": client_type, "saved": True, "valid": None, "error": None,
              "activated": False}
    try:
        client = get_download_client_by_name(client_type, DownloadClientConfig.from_dict(merged))
        report["valid"] = bool(client.test_connection())
        update_download_client_validity(client_type, report["valid"])
        if not report["valid"]:
            report["error"] = getattr(client, "last_error", None) or "Connection failed"
    except Exception as e:
        report["valid"], report["error"] = False, str(e)

    if not get_active_download_client(group):
        report["activated"] = bool(set_active_download_client(client_type))
    return report


def _apply_indexer(provider, existing_id):
    from core.database import (
        add_indexer,
        get_all_indexers,
        get_indexer,
        update_indexer,
        update_indexer_validity,
    )
    from models.indexers import IndexerConfig, IndexerType, get_indexer_impl

    fields = {"api_key": provider.get("api_key"), "categories": provider["categories"]}
    report = {"name": provider["name"], "saved": False, "valid": None, "error": None,
              "updated": bool(existing_id)}
    if existing_id:
        current = get_indexer(existing_id) or {}
        config = {"api_key": current.get("api_key"), "categories": current.get("categories")}
        config.update({k: v for k, v in fields.items() if v is not None})
        if not update_indexer(existing_id, config=config):
            report["error"] = "Could not update the indexer"
            return report
        indexer_id = existing_id
    else:
        indexer_id = add_indexer(
            name=provider["name"], url=provider["url"], config=fields,
            priority=len(get_all_indexers()), enabled=provider["enabled"],
        )
        if indexer_id is None:
            report["error"] = "Could not add the indexer"
            return report
    report["saved"] = True

    saved = get_indexer(indexer_id) or {}
    try:
        impl = get_indexer_impl(IndexerType(saved.get("indexer_type") or "newznab"), IndexerConfig(
            name=saved.get("name", provider["name"]),
            url=saved.get("url", provider["url"]),
            api_key=saved.get("api_key"),
            categories=saved.get("categories"),
            enabled=saved.get("enabled", True),
        ))
        report["valid"] = bool(impl.test_connection())
        update_indexer_validity(indexer_id, report["valid"])
        if not report["valid"]:
            report["error"] = getattr(impl, "last_error", None) or "Connection failed"
    except Exception as e:
        report["valid"], report["error"] = False, str(e)
    return report


def _enable_usenet_source():
    """Add Usenet to the download source priority once it can actually work.

    Usenet is live only with an active client and an enabled indexer
    (``models/usenet.py``); listing it without them changes nothing, and
    listing it is the step a user is least likely to know about.
    """
    from core.database import get_active_download_client, get_enabled_indexers, set_user_preference
    from models.download_sources import get_source_priority

    if not get_active_download_client("usenet") or not get_enabled_indexers():
        return None
    order = get_source_priority()
    if "usenet" in order:
        return None
    order.append("usenet")
    if set_user_preference("download_source_priority", order, category="downloads"):
        return order
    return None


# A ComicVine-only series costs a volume lookup plus at least one page of its
# issue list; wait until that many requests fit before starting one.
_CV_CALLS_PER_SERIES = 3
_WAIT_POLL_SECONDS = 10
_sleep = time.sleep  # seam: tests replace it


def _wait_for_comicvine(op_id, current, total, label):
    """Wait out ComicVine's hourly budget visibly, before a ComicVine-only series.

    ``_cv_call_with_retry`` blocks a background thread until a slot frees, up to
    the full hour, with no log line and no progress update -- so a large import
    looked frozen, page and log alike, and the operations registry marked it
    stalled. Waiting here instead keeps the job heartbeating and says why.
    """
    from models import comicvine, comicvine_source

    if comicvine_source._local_available():
        return  # the local dump answers without touching the API
    limiter = comicvine._comicvine_rate_limiter
    wait = limiter.wait_time(_CV_CALLS_PER_SERIES)
    if wait <= 0:
        return
    app_logger.info(
        f"Mylar3 import: ComicVine's hourly request limit is reached; waiting "
        f"about {max(1, round(wait / 60))} min before {label}"
    )
    started = time.monotonic()
    while wait > 0:
        if op_id:
            _progress(
                op_id, current, total,
                f"Waiting for ComicVine's hourly request limit; resuming in about "
                f"{max(1, round(wait / 60))} min (next: {label})",
            )
        _sleep(min(wait, _WAIT_POLL_SECONDS))
        wait = limiter.wait_time(_CV_CALLS_PER_SERIES)
    app_logger.info(
        f"Mylar3 import: ComicVine limit cleared after "
        f"{max(1, round((time.monotonic() - started) / 60))} min; resuming"
    )


def _metron_unavailable():
    """Why Metron cannot answer right now, or None."""
    from models import metron

    if metron.auth_blocked():
        return "Metron rejected the saved credentials"
    if metron._metron_pacer.daily_limit_reached():
        return "Metron's daily request limit was reached"
    return None


def import_series(app, rows, mylar_by_cv, op_id=None):
    """Resolve, create, map and sync each row. Returns the result lists."""
    from core.database import get_series_mapping
    from helpers.comicvine_ids import make_comicvine_series_id
    from models import comicvine_source, library_automap, metron

    out = {"mapped": [], "deferred": [], "unresolved": [], "failed": [], "skipped": [],
           "folders_created": 0}
    if not rows:
        return out

    api = metron.get_flask_api(app)
    cv_available = comicvine_source.is_available(app)
    total = len(rows)
    defer_reason = None

    for i, row in enumerate(rows):
        label = f"{row['name']} ({row['year']})" if row.get("year") else row["name"]
        if op_id:
            _progress(op_id, i, total, label)
        entry = {"cv_id": row["cv_id"], "name": label, "path": row["path"]}

        if api and defer_reason is None:
            defer_reason = _metron_unavailable()
        if defer_reason:
            out["deferred"].append({**entry, "reason": defer_reason})
            continue

        series_id = metron.get_series_id_by_comicvine_id(api, row["cv_id"]) if api else None
        if series_id is None and api:
            defer_reason = _metron_unavailable()
            if defer_reason:
                out["deferred"].append({**entry, "reason": defer_reason})
                continue
        source = "metron"
        if series_id is None:
            if not cv_available:
                out["unresolved"].append({
                    **entry,
                    "reason": "Not on Metron, and ComicVine is not configured"
                    if api else "Neither Metron nor ComicVine is configured",
                })
                continue
            series_id = make_comicvine_series_id(row["cv_id"])
            source = "comicvine"

        current = get_series_mapping(series_id)
        if current and _norm_path(current) != _norm_path(row["path"]):
            out["skipped"].append({**entry, "reason": f"Already on the Pull List at {current}"})
            continue

        created = False
        if not os.path.isdir(row["path"]):
            if not _within_roots(row["path"], get_library_roots()):
                out["failed"].append({**entry, "reason": "Not inside a CLU library"})
                continue
            try:
                os.makedirs(row["path"], exist_ok=True)
                created = True
            except OSError as e:
                out["failed"].append({**entry, "reason": f"Could not create the folder: {e}"})
                continue

        mylar = mylar_by_cv.get(row["cv_id"]) or {}
        seeded = source == "comicvine" and bool(mylar_issue_records(mylar))
        if seeded:
            # Everything ComicVine would tell us, Mylar already copied: no API call.
            source = "mylar"
            error = seed_from_mylar(series_id, row, mylar)
            if error:
                out["failed"].append({**entry, "reason": error})
                continue
        else:
            if source == "comicvine":
                _wait_for_comicvine(op_id, i, total, label)
                if op_id:
                    _progress(op_id, i, total, label)
            applied = library_automap.apply_automap([{
                "folder": row["path"],
                "metron_id": series_id,
                "series_name": row["name"],
                "publisher_name": row.get("publisher"),
                "year": row.get("year"),
                "cv_id": row["cv_id"],
            }], api=api)
            if not applied["applied"]:
                reason = (applied["failed"][0]["error"] if applied["failed"] else "Not mapped")
                out["failed"].append({**entry, "reason": reason})
                continue
        if created:
            out["folders_created"] += 1

        if op_id and not seeded:
            _progress(op_id, i, total, f"{label}: syncing issues")
        # With seeded issues this only matches files; it fetches nothing.
        library_automap._sync_and_match(api, series_id)
        marks = apply_issue_statuses(series_id, mylar.get("issues") or [])
        if mylar.get("status") == "Paused":
            from core.database import set_series_monitored

            set_series_monitored(series_id, False)
        out["mapped"].append({
            **entry, "series_id": series_id, "source": source, "created": created,
            "paused": mylar.get("status") == "Paused", **marks,
        })

    if op_id:
        _progress(op_id, total, total, "Rebuilding the Wanted list...")
    if out["mapped"]:
        try:
            _refresh_wanted_cache()
        except Exception as e:
            app_logger.error(f"Mylar3 import: wanted rebuild failed: {e}")
    return out


def mylar_issue_records(mylar_series):
    """CLU ``issues`` rows built from Mylar's own issue list.

    Shaped exactly as ``models.comicvine.get_all_issues_for_volume`` returns
    them, ids offset into the ComicVine range, so a later ComicVine refresh of
    the series replaces these rows one for one. Issues without a ComicVine id
    are left out: the id is the row's primary key.
    """
    from helpers.comicvine_ids import COMICVINE_SERIES_ID_OFFSET

    records = []
    seen = set()
    for issue in mylar_series.get("issues") or []:
        cv_issue_id = issue.get("issue_id")
        if not cv_issue_id or cv_issue_id in seen:
            continue
        seen.add(cv_issue_id)
        records.append({
            "id": COMICVINE_SERIES_ID_OFFSET + int(cv_issue_id),
            "cv_id": cv_issue_id,
            "number": issue.get("number"),
            "name": issue.get("name"),
            "cover_date": issue.get("cover_date"),
            "store_date": issue.get("store_date"),
            "image": issue.get("image"),
            "resource_url": f"https://comicvine.gamespot.com/issue/4000-{cv_issue_id}/",
        })
    return records


def seed_from_mylar(series_id, row, mylar_series):
    """Map a ComicVine-only series from Mylar's data alone. Returns an error or None.

    The API path costs a volume lookup plus a page of issues per series; at
    CLU's 180-an-hour ComicVine budget that capped an import at about sixty
    series an hour. Mylar refreshes from ComicVine itself, so its copy is as
    current as Mylar was; the series page's Sync replaces it from ComicVine.
    (The scheduled series sync is Metron-only and skips ComicVine-only series.)
    """
    from core.database import (
        delete_issues_for_series,
        save_issues_bulk,
        save_series_mapping,
        update_series_sync_time,
    )
    from models import comicvine, library_automap

    cv_id = row["cv_id"]
    series_dict = {
        "id": series_id,
        "name": mylar_series.get("name") or row["name"],
        "publisher_name": mylar_series.get("publisher") or row.get("publisher"),
        "year_began": mylar_series.get("year") or row.get("year"),
        "cv_id": cv_id,
        "desc": comicvine.description_to_text(mylar_series.get("description")),
        "resource_url": f"https://comicvine.gamespot.com/volume/4050-{cv_id}/",
    }
    library_automap._prepare_series_dict_for_save(series_dict, {
        "publisher_name": series_dict["publisher_name"],
    })
    if not save_series_mapping(
        series_dict, row["path"], cover_image=mylar_series.get("image_url")
    ):
        return "Failed to save mapping"

    issues = mylar_issue_records(mylar_series)
    delete_issues_for_series(series_id)
    if save_issues_bulk(issues, series_id) < 0:
        return "Mapped, but saving its issues failed"
    update_series_sync_time(series_id, len(issues))
    return None


def apply_issue_statuses(series_id, mylar_issues):
    """Copy Mylar's Skipped/Ignored/Archived marks onto issues with no CLU mark.

    Issue numbers are compared through ``normalize_issue_number`` (leading
    zeros, sign) and case-folded (Mylar may write ``1.mu`` for Metron's
    ``1.MU``); the mark is stored under CLU's own spelling of the number.
    """
    from core.database import (
        bulk_set_manual_status,
        get_issues_for_series,
        get_manual_status_for_series,
    )
    from helpers.collection import normalize_issue_number

    def key(number):
        return normalize_issue_number(number).lower()

    wanted = {}
    for issue in mylar_issues:
        mark = ISSUE_STATUS_MAP.get((issue.get("status") or "").lower())
        if mark:
            wanted[key(issue["number"])] = mark
    if not wanted:
        return {"marked_skipped": 0, "marked_owned": 0}

    already = {key(n) for n in (get_manual_status_for_series(series_id) or {})}
    by_mark = {"skipped": [], "owned": []}
    for issue in get_issues_for_series(series_id):
        k = key(issue.get("number"))
        if k in wanted and k not in already:
            by_mark[wanted[k]].append(str(issue.get("number")))

    for mark, numbers in by_mark.items():
        if numbers:
            bulk_set_manual_status(series_id, numbers, mark, notes="Imported from Mylar3")
    return {"marked_skipped": len(by_mark["skipped"]), "marked_owned": len(by_mark["owned"])}
