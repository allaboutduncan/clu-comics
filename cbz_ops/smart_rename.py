"""
Smart Rename — metadata-driven bulk rename.

Unlike `cbz_ops.rename.rename_files`, which reverse-engineers series/volume/year
from each filename via regex, Smart Rename pulls those values from `series.json`
(created from `cvinfo` if missing, using the library's configured providers)
and extracts only the issue number from each filename. Values are then fed
through the existing custom-pattern + filename-cleanup pipeline.
"""

import os
import re
from typing import Dict, List, Optional

from core.app_logging import app_logger
from cbz_ops.rename import (
    apply_custom_pattern,
    apply_filename_cleanup,
    clean_final_filename,
    extract_comic_values,
    load_custom_rename_config,
    load_filename_cleanup_config,
    _format_issue_month,
    _pad_issue_number,
)
from models.series_json import build_metadata, read_series_json, write_series_json
from models import comicvine as cv_mod

COMIC_EXTS = (".cbz", ".cbr", ".zip")


def _load_exclude_terms() -> List[str]:
    """Read `smart_rename_exclude_terms` and return a normalized list.

    Returns lowercased, whitespace-stripped, non-empty terms. The pref is
    a comma-separated string; default is "Annual,Special" so out of the box
    we don't merge Annuals/Specials into the main series namespace.
    """
    from core.database import get_user_preference  # lazy: avoid import cycles
    raw = get_user_preference("smart_rename_exclude_terms", default="Annual,Special") or ""
    return [t.strip().lower() for t in raw.split(",") if t.strip()]


def _get_metron_mod():
    """Lazy import of models.metron so unit tests don't need mokkari."""
    from models import metron as metron_mod  # noqa: WPS433
    return metron_mod


def _iter_dirs(root_dir: str, recursive: bool):
    """Yield directories to process, root first."""
    if not recursive:
        yield root_dir
        return
    for dirpath, _dirnames, _filenames in os.walk(root_dir):
        yield dirpath


def _has_comic_files(directory: str) -> bool:
    try:
        for name in os.listdir(directory):
            if os.path.isfile(os.path.join(directory, name)) and name.lower().endswith(COMIC_EXTS):
                return True
    except OSError:
        return False
    return False


def _resolve_series_via_providers(cvinfo_path: str, library_id: Optional[int]):
    """
    Walk the library's configured providers in order and return the first
    provider-built series object suitable for `write_series_json`.

    Returns a tuple (series_obj, metron_api) where metron_api may be None.
    Returns (None, None) when nothing usable is found.
    """
    cv_id = cv_mod.parse_cvinfo_volume_id(cvinfo_path)
    if not cv_id:
        app_logger.warning(f"smart_rename: no CV volume id in {cvinfo_path}")
        return None, None

    provider_types: List[str] = []
    if library_id:
        try:
            from core.database import get_library_providers
            for p in get_library_providers(library_id) or []:
                if p.get("enabled", True):
                    provider_types.append(p["provider_type"])
        except Exception as e:
            app_logger.warning(f"smart_rename: failed to load library providers: {e}")
    if not provider_types:
        provider_types = ["metron", "comicvine"]

    metron_api = None
    for ptype in provider_types:
        if ptype == "metron":
            try:
                metron_mod = _get_metron_mod()
                if not metron_mod.is_metron_configured():
                    continue
                api = metron_mod.get_flask_api()
                if not api:
                    continue
                metron_api = api
                metron_id = metron_mod.get_series_id(cvinfo_path, api)
                if not metron_id:
                    continue
                series = api.series(metron_id)
                if series:
                    return series, metron_api
            except Exception as e:
                app_logger.warning(f"smart_rename: metron lookup failed for cv_id={cv_id}: {e}")
                continue

        elif ptype == "comicvine":
            try:
                from flask import current_app
                api_key = current_app.config.get("COMICVINE_API_KEY", "")
                if not api_key:
                    continue
                series_dict = _build_comicvine_series_dict(api_key, cv_id)
                if series_dict:
                    return series_dict, metron_api
            except Exception as e:
                app_logger.warning(f"smart_rename: comicvine lookup failed for cv_id={cv_id}: {e}")
                continue
        # Other providers (gcd, anilist, manga*) don't populate Mylar-style
        # series.json today; skip them silently.
    return None, metron_api


def _build_comicvine_series_dict(api_key: str, cv_id: int) -> Optional[Dict]:
    """Fetch a CV volume and shape it for `write_series_json`."""
    try:
        from simyan.comicvine import Comicvine  # type: ignore
    except ImportError:
        app_logger.warning("smart_rename: simyan not installed; cannot use ComicVine fallback")
        return None
    try:
        cv = Comicvine(api_key=api_key, cache=None)
        volume = cv.get_volume(cv_id)
        if not volume:
            return None
        publisher = getattr(volume, "publisher", None)
        publisher_name = getattr(publisher, "name", None) if publisher else None
        image = getattr(volume, "image", None)
        image_url = getattr(image, "original_url", None) if image else None
        return {
            "id": None,
            "cv_id": cv_id,
            "name": getattr(volume, "name", None),
            "year_began": getattr(volume, "start_year", None),
            "year_end": None,
            "publisher": publisher_name,
            "imprint": None,
            "issue_count": getattr(volume, "issue_count", 0) or 0,
            "desc": getattr(volume, "description", None),
            "volume": 1,
            "status": None,
            "image": image_url,
        }
    except Exception as e:
        app_logger.error(f"smart_rename: ComicVine volume fetch failed for {cv_id}: {e}")
        return None


def _ensure_series_json(directory: str, library_id: Optional[int]) -> Dict:
    """
    Ensure `series.json` exists in `directory`. Returns:
      {"status": "ok", "metadata": {...}} on success
      {"status": "needs_cvinfo"} if no cvinfo
      {"status": "series_json_failed", "reason": "..."} if creation failed
    """
    cvinfo_path = cv_mod.find_cvinfo_in_folder(directory)
    if not cvinfo_path:
        return {"status": "needs_cvinfo"}

    existing = read_series_json(directory)
    if existing and isinstance(existing.get("metadata"), dict) and existing["metadata"].get("name"):
        return {"status": "ok", "metadata": existing["metadata"]}

    series_obj, metron_api = _resolve_series_via_providers(cvinfo_path, library_id)
    if not series_obj:
        return {"status": "series_json_failed", "reason": "no provider returned series data"}

    # Build the metadata we need for the rename in memory first. This is what
    # the plan actually consumes, so a failure to persist the series.json
    # sidecar (e.g. read-only mount) should not block the rename.
    try:
        metadata = build_metadata(series_obj, api=metron_api)
    except Exception as e:
        return {"status": "series_json_failed", "reason": f"could not build metadata: {e}"}
    if not metadata.get("name"):
        return {"status": "series_json_failed", "reason": "provider series has no name"}

    ok, reason = write_series_json(directory, series_obj, api=metron_api, return_reason=True)
    if not ok:
        app_logger.warning(
            f"smart_rename: series.json not persisted in {directory}: {reason}"
        )
        return {
            "status": "ok",
            "metadata": metadata,
            "warning": f"series.json not saved: {reason}",
        }

    # Persisted — re-read so preserved/merged fields win, but fall back to the
    # in-memory metadata if the re-read comes back empty.
    refreshed = read_series_json(directory) or {}
    md = refreshed.get("metadata") if isinstance(refreshed, dict) else None
    if not md or not md.get("name"):
        md = metadata
    return {"status": "ok", "metadata": md}


def _format_volume(volume) -> str:
    """Format the series.json `volume` field as the {volume_number} token."""
    if volume in (None, "", 0):
        return ""
    text = str(volume).strip()
    if not text:
        return ""
    if text.lower().startswith("v"):
        return text
    if text.isdigit():
        return f"v{int(text)}"
    return text


def _format_year(year) -> str:
    if year in (None, "", 0):
        return ""
    return str(year).strip()


def _sanitize_series_name(name: str) -> str:
    """Strip Windows-illegal chars from a series name.

    Matches the convention used in `rename_comic_from_metadata`: colon is
    replaced with " -" (not removed) so subtitles like "Batman: Year One"
    become "Batman - Year One"; the rest of the Windows-reserved set is
    stripped outright.
    """
    if not name:
        return ""
    name = name.replace(":", " -")
    name = re.sub(r'[<>"/\\|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _same_file(a: str, b: str) -> bool:
    """True if both paths resolve to the same file. On case-insensitive
    filesystems this catches a case-only rename (e.g. "Avx" -> "AVX") where the
    target path points back at the source file rather than a real collision."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _read_issue_meta(file_path: str) -> Dict[str, str]:
    """Read issue-level tokens from a file's embedded ComicInfo.xml.

    Returns a dict with issue_year/issue_month_M/issue_month_m (from ComicInfo
    Year/Month, the cover date written to the XML) and issue_title (from
    Title); any may be empty. Only .cbz/.zip are supported (ComicInfo in a
    .cbr lives in a RAR); anything else, or a read failure, yields empty values.
    """
    empty = {
        "issue_year": "",
        "issue_month_M": "",
        "issue_month_m": "",
        "issue_title": "",
    }
    if not file_path.lower().endswith((".cbz", ".zip")):
        return empty
    try:
        from core.comicinfo import read_comicinfo_from_zip

        info = read_comicinfo_from_zip(file_path)
    except Exception:
        return empty
    if not info:
        return empty

    out = dict(empty)
    if info.get("Year"):
        out["issue_year"] = str(info["Year"])
    if info.get("Month"):
        out["issue_month_M"], out["issue_month_m"] = _format_issue_month(info["Month"])
    if info.get("Title"):
        out["issue_title"] = info["Title"]
    return out


def _plan_file(
    file_path: str,
    metadata: Dict,
    pattern: str,
    cleanup_cfg: Dict,
    used_targets: set,
    exclude_terms: Optional[List[str]] = None,
    needs_issue_meta: bool = False,
) -> Dict:
    """Build the rename plan entry for a single file."""
    old_name = os.path.basename(file_path)
    stem, ext = os.path.splitext(old_name)

    if exclude_terms:
        lowered_stem = stem.lower()
        for term in exclude_terms:
            if term and term in lowered_stem:
                return {
                    "old_path": file_path,
                    "old_name": old_name,
                    "status": "excluded_term",
                    "matched_term": term,
                }

    extracted = extract_comic_values(stem)
    raw_issue = (extracted.get("issue_number") or "").strip()
    if not raw_issue:
        return {"old_path": file_path, "old_name": old_name, "status": "no_issue"}

    issue = _pad_issue_number(raw_issue, width=3)

    # Issue-level tokens ({issue_year}, {issue_title}) come from each file's
    # embedded ComicInfo.xml, not series.json. The series/volume year feeds
    # {volume_year}; {issue_year} is the issue's own year from the XML.
    issue_meta = _read_issue_meta(file_path) if needs_issue_meta else {}

    values = {
        "series_name": _sanitize_series_name((metadata.get("name") or "").strip()),
        "volume_number": _format_volume(metadata.get("volume")),
        # series.json "year" is the series/volume year -> feeds {volume_year}
        "volume_year": _format_year(metadata.get("year")),
        # "year" is the {volume_year} fallback only; the issue's own year (from
        # ComicInfo) feeds {issue_year}, never the series start year.
        "year": issue_meta.get("issue_year", ""),
        "issue_number": issue,
        "issue_title": issue_meta.get("issue_title", ""),
        "issue_year": issue_meta.get("issue_year", ""),
        "issue_month_M": issue_meta.get("issue_month_M", ""),
        "issue_month_m": issue_meta.get("issue_month_m", ""),
    }

    new_stem = apply_custom_pattern(values, pattern)
    if not new_stem:
        return {"old_path": file_path, "old_name": old_name, "status": "pattern_empty"}

    new_stem = apply_filename_cleanup(new_stem, cleanup_cfg)
    new_name = clean_final_filename(new_stem + ext.lower())

    if new_name == old_name:
        return {"old_path": file_path, "old_name": old_name, "status": "unchanged"}

    directory = os.path.dirname(file_path)
    candidate = os.path.join(directory, new_name)
    if not _same_file(candidate, file_path) and (candidate in used_targets or os.path.exists(candidate)):
        base, ext_final = os.path.splitext(new_name)
        suffix = 2
        while True:
            unique = f"{base} ({suffix}){ext_final}"
            candidate = os.path.join(directory, unique)
            if candidate not in used_targets and not os.path.exists(candidate):
                new_name = unique
                break
            suffix += 1

    used_targets.add(candidate)
    return {
        "old_path": file_path,
        "old_name": old_name,
        "new_path": candidate,
        "new_name": new_name,
        "status": "ok",
    }


def plan_smart_rename(
    root_dir: str,
    recursive: bool = True,
    library_id: Optional[int] = None,
) -> Dict:
    """
    Build a rename plan for `root_dir` (optionally recursive).

    Returns:
        {
          "root": root_dir,
          "recursive": bool,
          "directories": [
             {"dir": str, "status": "ok"|"needs_cvinfo"|"series_json_failed"|"empty",
              "metadata": {...} | None, "files": [<file plan entries>]}
          ]
        }
    """
    custom_enabled, pattern = load_custom_rename_config()
    cleanup_cfg = load_filename_cleanup_config()
    exclude_terms = _load_exclude_terms()

    # Only crack open each archive for ComicInfo.xml when the pattern actually
    # references an issue-level token (title / year / month).
    needs_issue_meta = bool(pattern) and any(
        tok in pattern
        for tok in (
            "{issue_title}",
            "{issue_year}",
            "{issue_month_M}",
            "{issue_month_m}",
        )
    )

    result = {
        "root": root_dir,
        "recursive": recursive,
        "pattern": pattern,
        "directories": [],
    }

    if not custom_enabled or not pattern:
        result["error"] = (
            "Custom rename pattern is not enabled. Configure 'Enable Custom Rename' "
            "and 'Custom Rename Pattern' in Settings before running Smart Rename."
        )
        return result

    if not os.path.isdir(root_dir):
        result["error"] = f"Directory does not exist: {root_dir}"
        return result

    for directory in _iter_dirs(root_dir, recursive):
        if not _has_comic_files(directory):
            continue

        dir_entry: Dict = {"dir": directory, "files": []}
        sj = _ensure_series_json(directory, library_id)
        if sj["status"] != "ok":
            dir_entry["status"] = sj["status"]
            if "reason" in sj:
                dir_entry["reason"] = sj["reason"]
            dir_entry["metadata"] = None
            result["directories"].append(dir_entry)
            continue

        metadata = sj["metadata"]
        dir_entry["status"] = "ok"
        if sj.get("warning"):
            dir_entry["warning"] = sj["warning"]
        dir_entry["metadata"] = {
            "name": metadata.get("name"),
            "volume": metadata.get("volume"),
            "year": metadata.get("year"),
        }

        used_targets: set = set()
        try:
            entries = sorted(os.listdir(directory))
        except OSError as e:
            dir_entry["status"] = "list_failed"
            dir_entry["reason"] = str(e)
            result["directories"].append(dir_entry)
            continue

        for name in entries:
            file_path = os.path.join(directory, name)
            if not os.path.isfile(file_path):
                continue
            if not name.lower().endswith(COMIC_EXTS):
                continue
            dir_entry["files"].append(
                _plan_file(
                    file_path,
                    metadata,
                    pattern,
                    cleanup_cfg,
                    used_targets,
                    exclude_terms=exclude_terms,
                    needs_issue_meta=needs_issue_meta,
                )
            )

        result["directories"].append(dir_entry)

    return result


def apply_smart_rename(plan: Dict) -> Dict:
    """
    Apply a plan produced by `plan_smart_rename`.

    Returns a summary dict with counts and per-file errors.
    """
    summary = {"renamed": 0, "skipped": 0, "failed": 0, "errors": [], "directories": []}

    try:
        from app import update_index_on_move
    except Exception:
        update_index_on_move = None

    for dir_entry in plan.get("directories", []):
        if dir_entry.get("status") != "ok":
            summary["skipped"] += 1
            summary["directories"].append(
                {"dir": dir_entry.get("dir"), "status": dir_entry.get("status")}
            )
            continue

        for f in dir_entry.get("files", []):
            if f.get("status") != "ok":
                summary["skipped"] += 1
                continue
            old_path = f["old_path"]
            new_path = f["new_path"]
            try:
                os.rename(old_path, new_path)
                summary["renamed"] += 1
                if update_index_on_move:
                    try:
                        update_index_on_move(old_path, new_path)
                    except Exception as e:
                        app_logger.warning(
                            f"smart_rename: index update failed for {old_path} -> {new_path}: {e}"
                        )
            except Exception as e:
                summary["failed"] += 1
                summary["errors"].append(
                    {"old_path": old_path, "new_path": new_path, "error": str(e)}
                )
                app_logger.error(
                    f"smart_rename: rename failed {old_path} -> {new_path}: {e}"
                )

        summary["directories"].append(
            {"dir": dir_entry.get("dir"), "status": "ok"}
        )

    return summary
