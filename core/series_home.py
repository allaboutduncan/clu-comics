"""Where a series already lives, asked before a second folder is made (#796).

Subscribing creates ``Name (year)`` and maps the series to it. A series already
in the library under a slightly different folder name (``Series Volume 01
(2016)``) got a second, empty folder; the mapping moved there, every issue in
the old folder read as missing and was downloaded again.

Three kinds of evidence, strongest first:

- ``mapped``     this series row is already mapped to another folder;
- ``cv_mapped``  another series row with the same ComicVine volume is mapped;
- ``cvinfo``     an indexed folder's cvinfo names this ComicVine volume or
                 this Metron series.

Identity only: no name matching. Two folders with similar names are not
evidence, and the three name matchers already disagree with each other.
"""
import os

from core.app_logging import app_logger


def same_folder(a, b):
    """Whether two paths name one folder (spelling, case, links)."""
    if not a or not b:
        return False
    if os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b)):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _cvinfo_still_names(folder, cv_id, series_id):
    """Re-read one folder's cvinfo: the folder maps are cached for minutes,
    and a folder deleted or re-tagged since must not block a subscribe."""
    from models.comicvine import find_cvinfo_in_folder, parse_cvinfo_ids

    if not os.path.isdir(folder):
        return False
    cvinfo = find_cvinfo_in_folder(folder)
    if not cvinfo:
        return False
    volume_id, metron_id = parse_cvinfo_ids(cvinfo)
    return bool((cv_id and volume_id == cv_id)
                or (series_id and metron_id == series_id))


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def find_existing_series_home(series, requested_path):
    """The folder ``series`` (a ``get_series_by_id`` row) already has, if it
    is not ``requested_path``.

    Returns ``None`` or ``{"path", "reason", "series_id", "series_name"}``;
    the last two name the *other* series row for ``cv_mapped`` and are None
    otherwise.
    """
    series_id = _as_int(series.get("id"))
    cv_id = _as_int(series.get("cv_id"))

    mapped = series.get("mapped_path")
    if mapped and not same_folder(mapped, requested_path):
        return {"path": mapped, "reason": "mapped",
                "series_id": None, "series_name": None}

    if cv_id:
        from core.database import find_mapped_series_by_cv_id
        for other in find_mapped_series_by_cv_id(cv_id, exclude_id=series_id):
            if not same_folder(other["mapped_path"], requested_path):
                return {"path": other["mapped_path"], "reason": "cv_mapped",
                        "series_id": other["id"], "series_name": other["name"]}

    try:
        from models.cbl import cv_volume_folders, metron_series_folders
        candidates = list(cv_volume_folders().get(cv_id, [])) if cv_id else []
        if series_id:
            candidates += [f for f in metron_series_folders().get(series_id, [])
                           if f not in candidates]
    except Exception as e:
        # The check is a courtesy; a failed folder read must not block
        # subscribing.
        app_logger.warning(f"Could not check cvinfo folders for series {series_id}: {e}")
        candidates = []

    for folder in sorted(candidates):
        if same_folder(folder, requested_path):
            continue
        if _cvinfo_still_names(folder, cv_id, series_id):
            return {"path": folder, "reason": "cvinfo",
                    "series_id": None, "series_name": None}
    return None
