"""
Bulk metadata processing orchestrator.

Fans a set of folders through provider lookup, auto-accepting only on an
exact (series name + year) match. Anything ambiguous is queued in the
bulk_metadata_review table for end-of-run review. Every metadata write is
recorded in bulk_metadata_audit with the prior ComicInfo.xml bytes so a
user-initiated revert can restore the original state.

Entry point: ``start_bulk_job(scope, paths, library_id, overwrite_existing)``.
The function returns immediately with the job id; the actual work runs in a
daemon thread.
"""
from __future__ import annotations

import os
import re
import threading
import time
import traceback
import uuid
import zipfile
from typing import Dict, List, Optional, Tuple

import core.app_state as app_state
from core.app_logging import app_logger
from core.comicinfo import find_comicinfo_in_zip
from core.config import config
from core.database import (
    add_review_item,
    complete_bulk_job,
    create_bulk_job,
    get_files_missing_comicinfo,
    get_library_providers,
    get_provider_credentials,
    log_bulk_audit,
    update_bulk_job_counts,
)
from models.providers import (
    IssueResult,
    ProviderCredentials,
    SearchResult,
    extract_issue_number,
    get_provider_by_name,
    get_provider_class,
)
from routes.metadata import (
    add_comicinfo_to_cbz,
    extract_series_name_from_filename,
    extract_series_name_from_folder,
    extract_year_from_name,
    generate_comicinfo_xml,
)


COMIC_EXTS = (".cbz", ".zip")


def _normalize_series(name: str) -> str:
    """Lowercase + strip punctuation/whitespace so 'The Flash' == 'the flash'.

    Also strips ``(YYYY)`` groups before normalising — Metron sometimes embeds
    the year in the series name (e.g. ``"Avengers Spotlight (1989)"``), and
    without this step the candidate title would normalise to
    ``"avengers spotlight 1989"`` and never match the parsed needle.
    """
    if not name:
        return ""
    s = re.sub(r'\s*\(\d{4}\)\s*', ' ', name)
    s = s.lower()
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return s.strip()


def _read_existing_comicinfo_bytes(cbz_path: str) -> Optional[bytes]:
    """Return the raw ComicInfo.xml bytes from a CBZ if present, else None."""
    try:
        with zipfile.ZipFile(cbz_path, 'r') as z:
            name = find_comicinfo_in_zip(z)
            if not name:
                return None
            return z.read(name)
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        app_logger.debug(f"Could not read existing ComicInfo from {cbz_path}: {e}")
        return None


def _has_existing_comicinfo(cbz_path: str) -> bool:
    try:
        with zipfile.ZipFile(cbz_path, 'r') as z:
            return find_comicinfo_in_zip(z) is not None
    except (zipfile.BadZipFile, OSError):
        return False


def _instantiate_provider(provider_type: str):
    """Build a provider instance using stored credentials, or None if unavailable."""
    try:
        cls = get_provider_class_by_name(provider_type)
        if cls is None:
            return None
        creds_dict = get_provider_credentials(provider_type)
        if cls.requires_auth and not creds_dict:
            app_logger.info(f"Skipping provider {provider_type}: no credentials configured")
            return None
        credentials = ProviderCredentials.from_dict(creds_dict) if creds_dict else None
        return get_provider_by_name(provider_type, credentials)
    except Exception as e:
        app_logger.warning(f"Could not instantiate provider {provider_type}: {e}")
        return None


def get_provider_class_by_name(name: str):
    """Resolve provider class by string name (e.g. 'metron')."""
    from models.providers import ProviderType
    try:
        ptype = ProviderType(name.lower())
    except ValueError:
        return None
    return get_provider_class(ptype)


def _enabled_providers_for_library(library_id: Optional[int]) -> List[str]:
    """Return ordered provider-type names for the library, or empty if none."""
    if not library_id:
        return []
    try:
        rows = get_library_providers(library_id) or []
        return [r["provider_type"] for r in rows if r.get("enabled", True)]
    except Exception as e:
        app_logger.warning(f"Could not load providers for library {library_id}: {e}")
        return []


def _group_files_by_folder(file_paths: List[str]) -> Dict[str, List[str]]:
    """Group selected file paths by their parent directory."""
    buckets: Dict[str, List[str]] = {}
    for p in file_paths:
        parent = os.path.dirname(p)
        buckets.setdefault(parent, []).append(p)
    return buckets


def _enumerate_folder_recursive(root: str, only_missing: bool) -> Dict[str, List[str]]:
    """Walk root recursively and return {folder_path: [cbz_paths...]}.

    When ``only_missing=True`` we use the SQL-backed file_index helper for
    files flagged as missing ComicInfo.xml. Otherwise we os.walk the tree
    (the index may lag, so for "overwrite existing" runs we want ground truth).
    """
    buckets: Dict[str, List[str]] = {}

    if only_missing:
        try:
            rows = get_files_missing_comicinfo(root) or []
            for r in rows:
                p = r["path"]
                parent = os.path.dirname(p)
                buckets.setdefault(parent, []).append(p)
            if buckets:
                return buckets
        except Exception as e:
            app_logger.warning(f"missing-XML index lookup failed for {root}: {e}; falling back to walk")

    # Fallback / overwrite path: walk the filesystem.
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith(COMIC_EXTS):
                buckets.setdefault(dirpath, []).append(os.path.join(dirpath, f))
    return buckets


# Publication-type keywords that mark a distinct sibling series at most
# providers (e.g. "Avengers West Coast Annual" is its own Metron/ComicVine
# entry, separate from "Avengers West Coast"). When a filename adds one of
# these to the folder-derived name, the filename version is the right lookup.
_PUBLICATION_SUFFIXES = (
    'annual', 'annuals',
    'special', 'specials',
    'giant-size', 'giant size',
    'one-shot', 'oneshot',
    'holiday special',
    'tpb',
    'quarterly',
    'yearbook',
)


def _maybe_promote_filename_series(folder_series: str, filename_series: str) -> str:
    """Prefer ``filename_series`` over ``folder_series`` when the filename adds
    a recognised publication-type keyword.

    Conservative on purpose: only promotes for known suffixes (Annual,
    Special, …). For arbitrary additional tokens (e.g. a stray "v1") we keep
    the folder series, which avoids breaking series searches that providers
    index without the volume suffix.
    """
    if not folder_series:
        return filename_series or ''
    if not filename_series:
        return folder_series
    fname_l = filename_series.lower().strip()
    folder_l = folder_series.lower().strip()
    if fname_l == folder_l:
        return folder_series
    if not fname_l.startswith(folder_l + ' '):
        return folder_series
    suffix = fname_l[len(folder_l) + 1:].strip()
    if suffix in _PUBLICATION_SUFFIXES:
        return filename_series
    return folder_series


def _series_year_for_folder(folder_path: str, files: List[str]) -> Tuple[str, Optional[int]]:
    """Parse a folder name + first filename into (series, year).

    Folder year is preferred over file year because folder names like
    ``v2002`` or ``Captain Marvel (2002)`` mark the series' **start** year,
    while filenames like ``Captain Marvel 015 (2003).cbz`` carry the
    **issue's** publication year. Provider series searches need the start
    year — using the issue year here can rank a completely different series
    (e.g. a 2003 Marvel Masterworks volume) above the right one.
    """
    folder_name = os.path.basename(folder_path.rstrip(os.sep))
    folder_series = extract_series_name_from_folder(folder_name)
    filename_series = ''
    if files:
        filename_series = extract_series_name_from_filename(os.path.basename(files[0]))

    series = _maybe_promote_filename_series(folder_series, filename_series)
    if not series:
        series = filename_series or folder_series

    year = extract_year_from_name(folder_name)
    if not year and files:
        year = extract_year_from_name(os.path.basename(files[0]))
    return series, year


def _is_oneshot_folder(folder_path: str) -> bool:
    """True if the folder's base name is in the user's ONESHOT_FOLDERS list.

    Thin wrapper over ``core.config.is_oneshot_folder`` (the single source of
    truth) kept for existing callers. One-shot folders hold unrelated single
    issues, so files are resolved individually and no cvinfo/series.json sidecar
    is written for the folder.
    """
    from core.config import is_oneshot_folder
    return is_oneshot_folder(folder_path)


def _try_cvinfo(folder_path: str) -> Optional[Tuple[str, str]]:
    """Look for an existing cvinfo file and return (provider, series_id) if usable.

    Prefers Metron series_id when present, otherwise the ComicVine volume id.
    Returns None when cvinfo is missing or unparseable.
    """
    cvinfo_path = os.path.join(folder_path, 'cvinfo')
    if not os.path.exists(cvinfo_path):
        return None
    try:
        from models import comicvine as cv_mod
        from models import metron as metron_mod
        metron_id = metron_mod.parse_cvinfo_for_metron_id(cvinfo_path)
        if metron_id:
            return ("metron", str(metron_id))
        cv_id = cv_mod.parse_cvinfo_volume_id(cvinfo_path)
        if cv_id:
            return ("comicvine", str(cv_id))
    except Exception as e:
        app_logger.debug(f"cvinfo parse failed for {folder_path}: {e}")
    return None


def _write_cvinfo(cvinfo_path: str, provider_name: str, series: SearchResult) -> None:
    """Write a provider-correct cvinfo file for a resolved series.

    Mirrors the layout produced by routes/bulk_metadata.py:apply_cvinfo so
    future bulk runs pick the folder up via _try_cvinfo:
      - Metron: ``series_id: <id>`` (read back as the Metron id) + fields.
      - ComicVine: the ``/4050-<id>/`` URL line (read back as the CV volume id);
        deliberately NO ``series_id:`` line, which _try_cvinfo would misread as
        a Metron id.
    """
    series_id = str(getattr(series, 'id', '') or '')
    publisher = getattr(series, 'publisher', None)
    start_year = getattr(series, 'year', None)
    if provider_name == 'metron':
        from models import metron as metron_mod
        metron_mod.create_cvinfo_file(
            cvinfo_path,
            cv_id=None,
            series_id=series_id,
            publisher_name=publisher,
            start_year=start_year,
        )
    else:  # comicvine
        with open(cvinfo_path, 'w', encoding='utf-8') as f:
            f.write(f"https://comicvine.gamespot.com/volume/4050-{series_id}/")
        from models import comicvine as cv_mod
        cv_mod.write_cvinfo_fields(cvinfo_path, publisher, start_year)


def _series_to_dict(provider_name: str, series: SearchResult) -> Dict:
    """Map a SearchResult to the keys models/series_json.build_metadata reads.

    The provider id is routed to the right field: Metron's id becomes
    ``metron_id``; ComicVine's becomes ``comicid`` (cv_id). For other providers
    we set neither (name/year/publisher still populate) — setting ``id`` for a
    non-Metron match would record a wrong ``metron_id``.
    """
    d = {
        'name': getattr(series, 'title', None),
        'year_began': getattr(series, 'year', None),
        'publisher_name': getattr(series, 'publisher', None),
        'issue_count': getattr(series, 'issue_count', None),
        'description': getattr(series, 'description', None),
        'image': getattr(series, 'cover_url', None),
    }
    series_id = getattr(series, 'id', None)
    if provider_name == 'metron':
        d['id'] = series_id          # -> metron_id
    elif provider_name == 'comicvine':
        d['cv_id'] = series_id       # -> comicid
    return d


def ensure_folder_sidecars(folder_path: str, provider_name: str, series: Optional[SearchResult]) -> None:
    """Create cvinfo and series.json for a resolved series, writing only the
    files that don't already exist (never clobber a user's existing sidecar).

    Best-effort: a sidecar failure is logged but never propagated, so it can't
    fail a bulk job. cvinfo is only written for ComicVine/Metron matches (its
    format is id-based for those providers); series.json is written for any
    provider.
    """
    if not series or not folder_path or not os.path.isdir(folder_path):
        return

    # One-shot folders hold unrelated singles — never drop folder-level sidecars.
    if _is_oneshot_folder(folder_path):
        app_logger.debug(f"Skipping folder sidecars in one-shot folder: {folder_path}")
        return

    try:
        cvinfo_path = os.path.join(folder_path, 'cvinfo')
        if provider_name in ('metron', 'comicvine') and not os.path.exists(cvinfo_path):
            _write_cvinfo(cvinfo_path, provider_name, series)
    except Exception as e:
        app_logger.warning(f"sidecar cvinfo write failed for {folder_path}: {e}")

    try:
        from models.series_json import write_series_json, SERIES_JSON_FILENAME
        if not os.path.exists(os.path.join(folder_path, SERIES_JSON_FILENAME)):
            write_series_json(
                folder_path,
                _series_to_dict(provider_name, series),
                preserve_existing=False,
            )
    except Exception as e:
        app_logger.warning(f"sidecar series.json write failed for {folder_path}: {e}")


def _candidates_to_json(results: List[SearchResult], max_n: int = 8) -> List[Dict]:
    """Serialise a few top SearchResults for the review queue UI."""
    out = []
    for r in results[:max_n]:
        out.append({
            "provider": r.provider.value if hasattr(r.provider, 'value') else str(r.provider),
            "id": r.id,
            "title": r.title,
            "year": r.year,
            "publisher": r.publisher,
            "issue_count": r.issue_count,
            "cover_url": r.cover_url,
        })
    return out


def _years_match(a, b) -> bool:
    """Tolerant year equality.

    ComicVine's ``start_year`` comes through Simyan as a string (e.g. "1989"),
    whereas the parsed year from a filename is always int — a plain ``==``
    silently returns False. This helper coerces both sides to int before
    comparing and treats either side as missing if it can't be parsed.
    """
    if a is None or b is None:
        return False
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return False


def _resolve_series_auto(
    providers: List[str],
    series_name: str,
    year: Optional[int],
) -> Tuple[Optional[str], Optional[SearchResult], List[SearchResult]]:
    """Try each provider in priority order looking for an exact name+year match.

    Returns ``(provider_name, accepted_result, all_candidates_seen)``.
    ``accepted_result`` is None when nothing satisfied the auto-accept gate;
    ``all_candidates_seen`` aggregates top hits from each provider for the
    review queue UI.
    """
    if not series_name:
        return None, None, []
    needle = _normalize_series(series_name)

    aggregated: List[SearchResult] = []
    for pname in providers:
        provider = _instantiate_provider(pname)
        if provider is None:
            continue
        try:
            results = provider.search_series(series_name, year) or []
        except Exception as e:
            app_logger.warning(f"{pname}.search_series('{series_name}', {year}) failed: {e}")
            continue

        if not results:
            continue

        aggregated.extend(results)

        # Year required for auto-accept — without it we cannot disambiguate.
        if year is None:
            continue

        exact = [
            r for r in results
            if _normalize_series(r.title) == needle and _years_match(r.year, year)
        ]
        if len(exact) == 1:
            return pname, exact[0], aggregated

    return None, None, aggregated


def _resolve_issue(
    provider_name: str,
    series_id: str,
    issue_number_text: str,
) -> Tuple[Optional[IssueResult], List[IssueResult]]:
    """Find the matching issue in a series. Returns (chosen, all_issues)."""
    provider = _instantiate_provider(provider_name)
    if provider is None:
        return None, []
    try:
        issues = provider.get_issues(str(series_id)) or []
    except Exception as e:
        app_logger.warning(f"{provider_name}.get_issues({series_id}) failed: {e}")
        return None, []

    if not issue_number_text:
        return None, issues

    # Normalise — Metron returns '1' for '001' etc.
    target = issue_number_text.lstrip('0') or '0'
    matches = []
    for i in issues:
        candidate = (i.issue_number or '').lstrip('0') or '0'
        if candidate == target:
            matches.append(i)
    if len(matches) == 1:
        return matches[0], issues
    return None, issues


def _write_metadata(
    job_id: str,
    folder_path: str,
    file_path: str,
    provider_name: str,
    series: Optional[SearchResult],
    issue: IssueResult,
    matched_via: str,
    parsed_year: Optional[int],
) -> bool:
    """Generate ComicInfo, write to CBZ, log the audit row.

    Returns True on success. On any exception the audit row is skipped and
    the caller should count an error.
    """
    provider = _instantiate_provider(provider_name)
    if provider is None:
        return False

    try:
        comicinfo_dict = provider.to_comicinfo(issue, series)
        xml_bytes = generate_comicinfo_xml(comicinfo_dict)
        if not xml_bytes:
            return False

        prior = _read_existing_comicinfo_bytes(file_path)
        add_comicinfo_to_cbz(file_path, xml_bytes)

        # Apply the user's Custom Naming pattern (configured in /config) so
        # files get renamed to match the freshly-applied metadata. Matches
        # the legacy single-file flow at routes/metadata.py:1627. No-op when
        # the user hasn't enabled custom rename or the pattern doesn't change
        # the name. We do this BEFORE the file_index sync so the updated row
        # carries the final path.
        final_path = file_path
        try:
            from cbz_ops.rename import rename_comic_from_metadata
            new_path, was_renamed = rename_comic_from_metadata(file_path, comicinfo_dict)
            if was_renamed:
                app_logger.info(
                    f"[bulk-meta] Renamed {os.path.basename(file_path)} -> "
                    f"{os.path.basename(new_path)}"
                )
                try:
                    from core.database import update_file_index_entry
                    update_file_index_entry(
                        file_path,
                        name=os.path.basename(new_path),
                        new_path=new_path,
                        parent=os.path.dirname(new_path),
                    )
                except Exception as e:
                    app_logger.warning(
                        f"file_index entry update after rename failed for {file_path}: {e}"
                    )
                final_path = new_path
        except Exception as e:
            app_logger.warning(f"Custom rename failed for {file_path}: {e}")

        # Keep file_index in sync: flip has_comicinfo to 1 and populate the
        # ci_* columns so the file drops out of the Missing XML view and shows
        # up in metadata-browser filters immediately. No-op if there's no
        # matching row (e.g., file hasn't been indexed yet).
        try:
            from core.database import update_file_index_from_comicinfo
            update_file_index_from_comicinfo(final_path, comicinfo_dict)
        except Exception as e:
            app_logger.warning(
                f"file_index sync failed for {final_path}: {e}"
            )

        log_bulk_audit(
            job_id=job_id,
            file_path=final_path,
            folder_path=folder_path,
            provider=provider_name,
            series_id=(series.id if series else issue.series_id),
            issue_id=issue.id,
            series_name=(series.title if series else None),
            issue_number=issue.issue_number,
            year=parsed_year,
            matched_via=matched_via,
            prior_xml=prior,
            new_xml=xml_bytes,
        )
        return True
    except Exception as e:
        app_logger.error(f"Failed to write metadata for {file_path}: {e}")
        app_logger.debug(traceback.format_exc())
        return False


# ----------------------------------------------------------------------------
# Folder worker
# ----------------------------------------------------------------------------


def _process_folder(
    job_id: str,
    op_id: str,
    folder_path: str,
    files: List[str],
    providers: List[str],
    overwrite_existing: bool,
    progress: Dict[str, int],
) -> None:
    """Process a single folder bucket. Updates job counts and progress in place."""
    folder_name = os.path.basename(folder_path.rstrip(os.sep)) or folder_path

    # Oneshots folders hold unrelated single issues — resolve each file on its
    # own from its filename, and never write a folder-level series sidecar.
    if _is_oneshot_folder(folder_path):
        _process_oneshot_folder(
            job_id=job_id,
            op_id=op_id,
            folder_path=folder_path,
            files=files,
            providers=providers,
            overwrite_existing=overwrite_existing,
            progress=progress,
        )
        return

    # Resolve series — cvinfo first, then provider search with the auto-accept gate.
    cvinfo = _try_cvinfo(folder_path)
    parsed_series, parsed_year = _series_year_for_folder(folder_path, files)
    matched_via = None
    chosen_provider: Optional[str] = None
    chosen_series: Optional[SearchResult] = None

    if cvinfo:
        chosen_provider, series_id = cvinfo
        matched_via = 'cvinfo'
        try:
            provider = _instantiate_provider(chosen_provider)
            chosen_series = provider.get_series(series_id) if provider else None
        except Exception as e:
            app_logger.warning(f"cvinfo get_series failed for {folder_path}: {e}")
            chosen_series = None
        if chosen_series is None:
            # cvinfo points at a series the provider can't resolve. Queue it.
            add_review_item(
                job_id=job_id,
                folder_path=folder_path,
                file_path=None,
                parsed_series=parsed_series,
                parsed_issue=None,
                parsed_year=parsed_year,
                reason='series_no_match',
                candidates=[],
            )
            update_bulk_job_counts(job_id, needs_review=1)
            for fp in files:
                progress["done"] += 1
                app_state.update_operation(op_id, current=progress["done"], detail=os.path.basename(fp))
            return
    else:
        chosen_provider, chosen_series, all_candidates = _resolve_series_auto(
            providers, parsed_series, parsed_year
        )
        if chosen_series is None:
            reason = 'series_ambiguous' if all_candidates else 'series_no_match'
            add_review_item(
                job_id=job_id,
                folder_path=folder_path,
                file_path=None,
                parsed_series=parsed_series,
                parsed_issue=None,
                parsed_year=parsed_year,
                reason=reason,
                candidates=_candidates_to_json(all_candidates),
            )
            update_bulk_job_counts(job_id, needs_review=1)
            # Files still count toward progress so the bar reaches 100%.
            for fp in files:
                progress["done"] += 1
                app_state.update_operation(op_id, current=progress["done"], detail=os.path.basename(fp))
            return
        matched_via = 'exact_name_year'

    # Record the resolved series as folder sidecars (cvinfo + series.json) when
    # they're missing, so future runs and external tools can reuse the match.
    ensure_folder_sidecars(folder_path, chosen_provider, chosen_series)

    # Cache the issue list once per folder (one network round-trip per provider call).
    provider = _instantiate_provider(chosen_provider)
    try:
        all_issues = provider.get_issues(str(chosen_series.id)) if provider else []
    except Exception as e:
        app_logger.warning(f"get_issues failed for {folder_path}: {e}")
        all_issues = []

    issues_by_norm: Dict[str, List[IssueResult]] = {}
    for i in all_issues:
        key = (i.issue_number or '').lstrip('0') or '0'
        issues_by_norm.setdefault(key, []).append(i)

    for file_path in files:
        progress["done"] += 1
        app_state.update_operation(op_id, current=progress["done"], detail=os.path.basename(file_path))

        # Skip files that already have metadata (unless caller asked to overwrite).
        if not overwrite_existing and _has_existing_comicinfo(file_path):
            update_bulk_job_counts(job_id, skipped=1)
            continue

        issue_text = extract_issue_number(os.path.basename(file_path))
        if not issue_text:
            add_review_item(
                job_id=job_id,
                folder_path=folder_path,
                file_path=file_path,
                parsed_series=parsed_series,
                parsed_issue=None,
                parsed_year=parsed_year,
                reason='issue_no_match',
                candidates=_candidates_to_json([], 0),
            )
            update_bulk_job_counts(job_id, needs_review=1)
            continue

        norm = issue_text.lstrip('0') or '0'
        matches = issues_by_norm.get(norm, [])
        if len(matches) == 1:
            ok = _write_metadata(
                job_id=job_id,
                folder_path=folder_path,
                file_path=file_path,
                provider_name=chosen_provider,
                series=chosen_series,
                issue=matches[0],
                matched_via=matched_via,
                parsed_year=parsed_year,
            )
            if ok:
                update_bulk_job_counts(job_id, auto_accepted=1)
            else:
                update_bulk_job_counts(job_id, errors=1)
        elif len(matches) > 1:
            add_review_item(
                job_id=job_id,
                folder_path=folder_path,
                file_path=file_path,
                parsed_series=parsed_series,
                parsed_issue=issue_text,
                parsed_year=parsed_year,
                reason='issue_ambiguous',
                candidates=[
                    {
                        "provider": chosen_provider,
                        "id": m.id,
                        "issue_number": m.issue_number,
                        "title": m.title,
                        "cover_date": m.cover_date,
                        "cover_url": m.cover_url,
                        "series_id": m.series_id,
                    }
                    for m in matches[:8]
                ],
            )
            update_bulk_job_counts(job_id, needs_review=1)
        else:
            add_review_item(
                job_id=job_id,
                folder_path=folder_path,
                file_path=file_path,
                parsed_series=parsed_series,
                parsed_issue=issue_text,
                parsed_year=parsed_year,
                reason='issue_no_match',
                candidates=[],
            )
            update_bulk_job_counts(job_id, needs_review=1)


def _process_oneshot_folder(
    job_id: str,
    op_id: str,
    folder_path: str,
    files: List[str],
    providers: List[str],
    overwrite_existing: bool,
    progress: Dict[str, int],
) -> None:
    """Process a folder of unrelated single issues (a "oneshots" folder).

    Unlike _process_folder, there is no single folder series: each file is
    resolved independently from its own filename (series name + year), matched
    to an issue, and written. No cvinfo/series.json sidecar is written. A file
    with no parseable issue number is treated as issue ``#1`` (one-shots are a
    single issue). Anything that can't be auto-resolved is queued for file-level
    review.
    """
    # Small per-folder cache so repeated series in the same folder don't refetch.
    series_cache: Dict[Tuple[str, Optional[int]], Tuple[Optional[str], Optional[SearchResult], Dict[str, List[IssueResult]]]] = {}

    for file_path in files:
        progress["done"] += 1
        app_state.update_operation(op_id, current=progress["done"], detail=os.path.basename(file_path))

        # Skip files that already have metadata (unless caller asked to overwrite).
        if not overwrite_existing and _has_existing_comicinfo(file_path):
            update_bulk_job_counts(job_id, skipped=1)
            continue

        base = os.path.basename(file_path)
        file_series = extract_series_name_from_filename(base)
        file_year = extract_year_from_name(base)
        # One-shots are a single issue; default a missing issue number to #1.
        issue_text = extract_issue_number(base) or "1"

        cache_key = (file_series, file_year)
        if cache_key in series_cache:
            prov_name, series, issues_by_norm = series_cache[cache_key]
        else:
            prov_name, series, all_candidates = _resolve_series_auto(
                providers, file_series, file_year
            )
            if series is None:
                reason = 'series_ambiguous' if all_candidates else 'series_no_match'
                add_review_item(
                    job_id=job_id,
                    folder_path=folder_path,
                    file_path=file_path,
                    parsed_series=file_series,
                    parsed_issue=issue_text,
                    parsed_year=file_year,
                    reason=reason,
                    candidates=_candidates_to_json(all_candidates),
                )
                update_bulk_job_counts(job_id, needs_review=1)
                series_cache[cache_key] = (None, None, {})
                continue

            provider = _instantiate_provider(prov_name)
            try:
                all_issues = provider.get_issues(str(series.id)) if provider else []
            except Exception as e:
                app_logger.warning(f"get_issues failed for {file_path}: {e}")
                all_issues = []
            issues_by_norm = {}
            for i in all_issues:
                key = (i.issue_number or '').lstrip('0') or '0'
                issues_by_norm.setdefault(key, []).append(i)
            series_cache[cache_key] = (prov_name, series, issues_by_norm)

        # A cached miss (unresolved series) still queues this file for review.
        if series is None:
            add_review_item(
                job_id=job_id,
                folder_path=folder_path,
                file_path=file_path,
                parsed_series=file_series,
                parsed_issue=issue_text,
                parsed_year=file_year,
                reason='series_no_match',
                candidates=[],
            )
            update_bulk_job_counts(job_id, needs_review=1)
            continue

        norm = issue_text.lstrip('0') or '0'
        matches = issues_by_norm.get(norm, [])
        if len(matches) == 1:
            ok = _write_metadata(
                job_id=job_id,
                folder_path=folder_path,
                file_path=file_path,
                provider_name=prov_name,
                series=series,
                issue=matches[0],
                matched_via='oneshot_filename',
                parsed_year=file_year,
            )
            if ok:
                update_bulk_job_counts(job_id, auto_accepted=1)
            else:
                update_bulk_job_counts(job_id, errors=1)
        elif len(matches) > 1:
            add_review_item(
                job_id=job_id,
                folder_path=folder_path,
                file_path=file_path,
                parsed_series=file_series,
                parsed_issue=issue_text,
                parsed_year=file_year,
                reason='issue_ambiguous',
                candidates=[
                    {
                        "provider": prov_name,
                        "id": m.id,
                        "issue_number": m.issue_number,
                        "title": m.title,
                        "cover_date": m.cover_date,
                        "cover_url": m.cover_url,
                        "series_id": m.series_id,
                    }
                    for m in matches[:8]
                ],
            )
            update_bulk_job_counts(job_id, needs_review=1)
        else:
            add_review_item(
                job_id=job_id,
                folder_path=folder_path,
                file_path=file_path,
                parsed_series=file_series,
                parsed_issue=issue_text,
                parsed_year=file_year,
                reason='issue_no_match',
                candidates=[],
            )
            update_bulk_job_counts(job_id, needs_review=1)


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------


def _expand_scope(scope: str, paths: List[str], overwrite_existing: bool) -> Dict[str, List[str]]:
    if scope == 'files':
        return _group_files_by_folder(paths)
    if scope == 'folder':
        merged: Dict[str, List[str]] = {}
        for root in paths:
            for folder, files in _enumerate_folder_recursive(root, only_missing=not overwrite_existing).items():
                merged.setdefault(folder, []).extend(files)
        # Dedupe.
        for k, v in merged.items():
            merged[k] = sorted(set(v))
        return merged
    raise ValueError(f"Unknown scope: {scope}")


def start_bulk_job(
    scope: str,
    paths: List[str],
    library_id: Optional[int] = None,
    overwrite_existing: bool = False,
) -> str:
    """Kick off a bulk metadata job in the background.

    Returns the job_id immediately. Progress is exposed via app_state
    operations (id == job_id) and via the bulk_metadata_job table.
    """
    job_id = uuid.uuid4().hex

    buckets = _expand_scope(scope, paths, overwrite_existing)
    total_folders = len(buckets)
    total_files = sum(len(v) for v in buckets.values())

    create_bulk_job(
        job_id=job_id,
        scope_type=('folder_recursive' if scope == 'folder' else 'files'),
        scope_payload={'paths': paths},
        library_id=library_id,
        overwrite_existing=overwrite_existing,
    )
    update_bulk_job_counts(job_id, total_folders=total_folders, total_files=total_files)

    op_id = app_state.register_operation(
        op_type='bulk_metadata',
        label=f"Bulk metadata: {total_files} files across {total_folders} folder(s)",
        total=total_files,
    )
    # Pin op_id == job_id so the UI only has to track one identifier.
    # app_state generates its own UUID, so we store the mapping in the job table
    # by writing it into scope_payload — see get_bulk_job.
    _OP_ID_MAP[job_id] = op_id

    providers = _enabled_providers_for_library(library_id)
    if not providers:
        # No library_id, or library has no providers configured. Fall back to a
        # sensible default order — Metron first since it's the most reliable.
        providers = ['metron', 'comicvine', 'gcd', 'gcd_api']

    def _runner():
        progress = {"done": 0}
        try:
            for folder_path, files in buckets.items():
                _process_folder(
                    job_id=job_id,
                    op_id=op_id,
                    folder_path=folder_path,
                    files=sorted(files),
                    providers=providers,
                    overwrite_existing=overwrite_existing,
                    progress=progress,
                )
            complete_bulk_job(job_id, 'completed')
            app_state.update_operation(op_id, current=total_files, detail='Done')
            app_state.complete_operation(op_id, error=False)
        except Exception as e:
            app_logger.error(f"Bulk metadata job {job_id} failed: {e}")
            app_logger.debug(traceback.format_exc())
            complete_bulk_job(job_id, 'error')
            app_state.complete_operation(op_id, error=True)

    threading.Thread(target=_runner, name=f"bulk-meta-{job_id[:8]}", daemon=True).start()
    return job_id


# Maps bulk_metadata_job.id → app_state operation id. Both are uuid-like
# strings; this layer exists so we can keep the public job_id stable across
# app_state's TTL-driven pruning.
_OP_ID_MAP: Dict[str, str] = {}


def get_op_id_for_job(job_id: str) -> Optional[str]:
    return _OP_ID_MAP.get(job_id)
