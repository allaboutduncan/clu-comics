"""
One place that decides which provider fills a folder's series.json (#618).

Four callers used to write series.json and each picked a provider its own way:
whichever provider won a file match (bulk metadata), Metron only (the series
page), or the library order but only when a cvinfo existed (Smart Rename). A
Metron record with a null description therefore beat a ComicVine one that had
text, and there was no way to ask for the other provider.

`resolve_series_for_folder` is the shared answer: a preferred provider first,
then the library's enabled providers in their configured order, with a
description fallback to the next provider when the chosen one has none.
`refresh_series_json` wraps it with the write, for the explicit menu action.

Nothing here raises for a provider that is down or unconfigured; a provider that
cannot answer is recorded in ``tried`` and the next one is asked.
"""

import os
from typing import Dict, List, Optional, Tuple

from core.app_logging import app_logger

PROVIDER_CHOICES = ("auto", "metron", "comicvine")
DEFAULT_ORDER = ["metron", "comicvine"]
# The two providers that can populate a Mylar-style series.json today. GCD and
# the manga providers have no series-level description shaped for it.
SUPPORTED_PROVIDERS = ("metron", "comicvine")
PREFERENCE_KEY = "series_json_provider"


def get_default_provider() -> str:
    """The saved ``series_json_provider`` preference; anything unknown is auto."""
    try:
        from core.database import get_user_preference
        value = (get_user_preference(PREFERENCE_KEY, "auto") or "auto").strip().lower()
    except Exception:
        return "auto"
    return value if value in PROVIDER_CHOICES else "auto"


def _library_order(library_id: Optional[int]) -> List[str]:
    """Enabled library providers in configured order, narrowed to the two that
    can fill series.json. An unconfigured library gets the default order."""
    order: List[str] = []
    if library_id:
        try:
            from core.database import get_library_providers
            for p in get_library_providers(library_id) or []:
                if p.get("enabled", True) and p["provider_type"] in SUPPORTED_PROVIDERS:
                    order.append(p["provider_type"])
        except Exception as e:
            app_logger.warning(f"series.json: failed to load library providers: {e}")
    return order or list(DEFAULT_ORDER)


def provider_order(library_id: Optional[int], preferred: str = "auto") -> List[str]:
    """Order in which providers are asked. ``preferred`` leads when it is a real
    provider; the rest follow in library order so a failure still falls through."""
    base = _library_order(library_id)
    preferred = (preferred or "auto").lower()
    if preferred in SUPPORTED_PROVIDERS:
        return [preferred] + [p for p in base if p != preferred] + \
               [p for p in SUPPORTED_PROVIDERS if p != preferred and p not in base]
    return base


def folder_ids(folder: str) -> Dict[str, Optional[int]]:
    """Provider ids already recorded in the folder: cvinfo first, then any
    existing series.json. Never asks the user for an id."""
    from models import comicvine as cv_mod
    from models import metron
    from models.series_json import read_series_json

    cv_id = metron_id = None
    cvinfo = cv_mod.find_cvinfo_in_folder(folder)
    if cvinfo:
        cv_id = cv_mod.parse_cvinfo_volume_id(cvinfo)
        metron_id = metron.parse_cvinfo_for_metron_id(cvinfo)

    existing = (read_series_json(folder) or {}).get("metadata")
    if isinstance(existing, dict):
        if not cv_id:
            try:
                cv_id = int(existing.get("comicid")) if existing.get("comicid") else None
            except (TypeError, ValueError):
                cv_id = None
        if not metron_id:
            try:
                metron_id = int(existing.get("metron_id")) if existing.get("metron_id") else None
            except (TypeError, ValueError):
                metron_id = None
    return {"cv_id": cv_id, "metron_id": metron_id}


def _fetch_metron(ids: Dict) -> Optional[Dict]:
    from models import metron

    if not metron.is_metron_configured():
        return None
    api = metron.get_flask_api()
    if not api:
        return None
    metron_id = ids.get("metron_id")
    if not metron_id and ids.get("cv_id"):
        metron_id = metron.get_series_id_by_comicvine_id(api, ids["cv_id"])
    if not metron_id:
        return None
    series = metron._api_call(lambda: api.series(metron_id), f"fetching series {metron_id} for series.json")
    if not series:
        return None
    if hasattr(series, "model_dump"):
        data = series.model_dump(mode="json")
    elif isinstance(series, dict):
        data = dict(series)
    else:
        data = {"id": metron_id, "name": getattr(series, "name", None)}
    data.setdefault("id", metron_id)
    return data


def _fetch_comicvine(ids: Dict) -> Optional[Dict]:
    cv_id = ids.get("cv_id")
    if not cv_id:
        return None
    from flask import current_app
    api_key = (current_app.config.get("COMICVINE_API_KEY", "") or "").strip()
    if not api_key:
        return None
    # Smart Rename owns the ComicVine volume -> series dict shaping (shared
    # client, rate-limit budget, error handling); reuse it rather than copy it.
    from cbz_ops.smart_rename import _build_comicvine_series_dict
    return _build_comicvine_series_dict(api_key, cv_id)


_FETCHERS = {"metron": _fetch_metron, "comicvine": _fetch_comicvine}


def _description(series: Optional[Dict]) -> Optional[str]:
    from models.comicvine import description_to_text
    if not series:
        return None
    return description_to_text(series.get("desc") or series.get("description")) or None


def _image(series: Optional[Dict]) -> Optional[str]:
    """The cover URL, however the provider spelled it; None when absent/blank."""
    if not series:
        return None
    value = series.get("cover_image") or series.get("image")
    return value.strip() or None if isinstance(value, str) else None


def _fill_gaps(primary: Dict, other: Optional[Dict]) -> List[str]:
    """Copy a blank description and cover from ``other`` into ``primary``.

    Returns the names of the fields taken. Only blanks are filled: the primary
    provider's own values always win.
    """
    taken: List[str] = []
    if other:
        if not _description(primary):
            text = _description(other)
            if text:
                primary["description"] = text
                primary["desc"] = text
                taken.append("description")
        if not _image(primary):
            url = _image(other)
            if url:
                primary["image"] = url
                primary.pop("cover_image", None)
                taken.append("image")
    return taken


def resolve_series_for_folder(folder: str, library_id: Optional[int] = None,
                              preferred: str = "auto") -> Tuple[Optional[Dict], Optional[str], Optional[str], List[str]]:
    """Return ``(series, provider_used, description_from, tried)``.

    The first provider that returns a series supplies every field. If its
    description is empty, the next provider is asked and only its description is
    taken (``description_from`` names it); the preferred provider's other fields
    stay. ``series`` is None when no provider could answer.
    """
    ids = folder_ids(folder)
    tried: List[str] = []
    primary = primary_name = None
    description_from = None

    for name in provider_order(library_id, preferred):
        tried.append(name)
        try:
            series = _FETCHERS[name](ids)
        except Exception as e:
            app_logger.warning(f"series.json: {name} lookup failed for {folder}: {e}")
            continue
        if not series or not series.get("name"):
            continue
        # Learn the other provider's id from this one so the next lookup is direct.
        if name == "metron" and series.get("cv_id") and not ids.get("cv_id"):
            ids["cv_id"] = series["cv_id"]
        if primary is None:
            primary, primary_name = series, name
            if _description(primary):
                description_from = name
        else:
            taken = _fill_gaps(primary, series)
            if "description" in taken:
                description_from = name
            if taken:
                app_logger.info(
                    f"series.json {','.join(taken)} for '{primary.get('name')}' "
                    f"taken from {name}"
                )
        # Stop as soon as nothing is left to ask another provider for. An empty
        # string is a valid description/cover, but a provider that has real
        # values is worth one more request when the first had none.
        if _description(primary) and _image(primary):
            break

    return primary, primary_name, description_from, tried


def with_description_fallback(series, folder: str, library_id: Optional[int] = None):
    """Return ``series`` with an empty description or cover filled from the other provider.

    For the writers that already hold a series from one provider (subscribe,
    series-page refresh, bulk-metadata sidecars) and so cannot go through
    `resolve_series_for_folder`. A Metron series is topped up from ComicVine and
    a ComicVine one from Metron, using the ids the series and folder carry.
    Never raises; on any failure the series comes back unchanged.
    """
    if not isinstance(series, dict) or (_description(series) and _image(series)):
        return series
    try:
        ids = folder_ids(folder) if folder and os.path.isdir(folder) else {"cv_id": None, "metron_id": None}
        if series.get("cv_id"):
            ids["cv_id"] = series["cv_id"]
        origin = "metron" if series.get("id") else "comicvine"
        if origin == "metron":
            ids["metron_id"] = series["id"]
        filled = None
        for name in provider_order(library_id):
            if name == origin:
                continue
            filled = filled if filled is not None else dict(series)
            taken = _fill_gaps(filled, _FETCHERS[name](ids))
            if taken:
                app_logger.info(
                    f"series.json {','.join(taken)} for '{series.get('name')}' "
                    f"taken from {name}"
                )
            if _description(filled) and _image(filled):
                break
        if filled is not None:
            return filled
    except Exception as e:
        app_logger.warning(f"series.json description fallback failed for {folder}: {e}")
    return series


def refresh_series_json(folder: str, library_id: Optional[int] = None,
                        provider: str = "auto", force: bool = False) -> Dict:
    """Create or update ``folder``'s series.json. Returns a result dict:
    ``{success, provider, description_from, tried, error?}``.

    ``force`` rebuilds from the provider and discards hand edits; otherwise
    fields the user edited are carried forward (``preserve_existing``).
    """
    from models.series_json import write_series_json

    if not folder or not os.path.isdir(folder):
        return {"success": False, "error": "Folder not found", "tried": []}

    provider = (provider or "auto").lower()
    if provider not in PROVIDER_CHOICES:
        provider = "auto"
    if provider == "auto":
        provider = get_default_provider()

    series, used, desc_from, tried = resolve_series_for_folder(folder, library_id, provider)
    if not series:
        return {
            "success": False, "tried": tried,
            "error": "No provider returned data for this folder. Add a cvinfo "
                     "(ComicVine or Metron id) or check the provider settings.",
        }

    api = None
    if used == "metron" or series.get("id"):
        try:
            from models import metron
            api = metron.get_flask_api() if metron.is_metron_configured() else None
        except Exception:
            api = None

    ok, reason = write_series_json(folder, series, api=api,
                                   preserve_existing=not force, return_reason=True)
    if not ok:
        return {"success": False, "error": reason or "Could not write series.json",
                "provider": used, "tried": tried}

    app_logger.info(
        f"series.json written for {folder} (provider={used}, "
        f"description from {desc_from or 'none'}, tried={','.join(tried)}"
        f"{', rebuilt' if force else ''})"
    )
    return {"success": True, "provider": used, "description_from": desc_from, "tried": tried}
