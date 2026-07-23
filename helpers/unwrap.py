"""Recursive unwrapping of Hybrid/Multipart release folders.

Usenet releases (e.g. BitBook) arrive as a folder of obfuscated, multi-part
archives rather than a ready comic — a set of ``.zip`` parts that extract to a
``.RAR`` that extracts to the real PDF/CBR/CBZ. This module reassembles and
recursively extracts such a folder until the comic file(s) emerge, so the normal
monitor pipeline can take over.

Nothing here mutates the source folder — extraction happens in an isolated work
dir under WATCH — so a failed or partial unwrap leaves the originals intact for
recovery. The monitor (monitor.py) owns the source-side cleanup and hand-off.

Reuses ``extract_rar_with_unar`` (unrar -> 7z -> unar, multi-volume aware) for
RAR layers; zip layers are extracted with zipfile and a 7z/unar fallback.
"""
import os
import re
import shutil
import subprocess
import uuid
import zipfile
from collections import defaultdict, namedtuple

from core.app_logging import app_logger
from helpers import is_hidden, match_parent_permissions, extract_rar_with_unar
from helpers.library import is_allowed_path

# Comic containers that end the recursion — hand these back to the pipeline.
COMIC_EXTS = {".pdf", ".cbr", ".cbz", ".cbt"}
# Release cruft that is safe to delete once a comic has emerged.
CRUFT_EXTS = {".nfo", ".diz", ".sfv", ".txt"}

# Archive-part patterns. RAR: plain .rar, part-volumes (.part01.rar) and old-style
# .r00/.r01 volumes. ZIP: plain .zip and spanned .z01/.z02 volumes. Numeric split
# volumes (.001/.002) round out the common Usenet packaging styles.
RAR_PART_RE = re.compile(r"\.part(\d+)\.rar$", re.IGNORECASE)
RAR_VOL_RE = re.compile(r"\.r\d{2}$", re.IGNORECASE)
RAR_MAIN_RE = re.compile(r"\.rar$", re.IGNORECASE)
ZIP_VOL_RE = re.compile(r"\.z\d{2}$", re.IGNORECASE)
ZIP_MAIN_RE = re.compile(r"\.zip$", re.IGNORECASE)
SPLIT_RE = re.compile(r"\.(\d{3})$")

# Classification results for a WATCH subfolder.
MULTIPART_ARCHIVE = "MULTIPART_ARCHIVE"
NORMAL = "NORMAL"

UnwrapResult = namedtuple(
    "UnwrapResult", ["comics", "ok", "reason", "partial", "work_dir"]
)


def _is_rar_family(name):
    return bool(RAR_MAIN_RE.search(name) or RAR_VOL_RE.search(name))


def _is_zip_family(name):
    return bool(ZIP_MAIN_RE.search(name) or ZIP_VOL_RE.search(name))


def is_archive_part(name):
    """True if the filename looks like any archive volume we know how to open."""
    return bool(
        _is_rar_family(name) or _is_zip_family(name) or SPLIT_RE.search(name)
    )


def _rar_stem(name):
    m = RAR_PART_RE.search(name)
    if m:
        return name[: m.start()].lower()
    if RAR_VOL_RE.search(name) or RAR_MAIN_RE.search(name):
        return name[:-4].lower()
    return name.lower()


def _zip_stem(name):
    if ZIP_VOL_RE.search(name) or ZIP_MAIN_RE.search(name):
        return name[:-4].lower()
    return name.lower()


def pick_primary_volume(parts):
    """Given the volumes of a single archive set, return the one to hand the
    extractor (which then follows the remaining volumes natively).

    Order of preference: lowest ``.partNN.rar``; the plain ``.rar`` of a
    ``.rar``+``.rNN`` set; the ``.zip`` of a ``.zip``+``.zNN`` spanned set; the
    lowest numeric ``.NNN`` split. Falls back to the lexicographically first
    name so the choice is deterministic for obfuscated same-extension sets.
    """
    if not parts:
        return None
    names = sorted(parts, key=lambda s: s.lower())

    part_rars = []
    for n in names:
        m = RAR_PART_RE.search(n)
        if m:
            part_rars.append((int(m.group(1)), n))
    if part_rars:
        return min(part_rars)[1]

    plain_rars = [n for n in names if RAR_MAIN_RE.search(n) and not RAR_PART_RE.search(n)]
    if plain_rars and any(RAR_VOL_RE.search(n) for n in names):
        return plain_rars[0]

    zips = [n for n in names if ZIP_MAIN_RE.search(n)]
    if zips and any(ZIP_VOL_RE.search(n) for n in names):
        return zips[0]

    splits = []
    for n in names:
        m = SPLIT_RE.search(n)
        if m:
            splits.append((int(m.group(1)), n))
    if splits:
        return min(splits)[1]

    if plain_rars:
        return plain_rars[0]
    return names[0]


def _looks_obfuscated(name):
    """Heuristic for scene/Usenet obfuscated names (e.g. ``--bbyvt3ga.zip``)."""
    if name.startswith("--"):
        return True
    stem = os.path.splitext(name)[0].lstrip("-_")
    return bool(re.fullmatch(r"[a-z0-9]{6,}", stem))


def classify_release_folder(folder_path):
    """Classify a WATCH subfolder as ``MULTIPART_ARCHIVE`` or ``NORMAL``.

    Conservative by design (a false positive would try to unwrap a normal
    folder): fires only when the folder has NO ready comic, has at least one
    archive part, and shows a multipart signal — several parts, release cruft
    (.nfo/file_id.diz), or an obfuscated archive name.
    """
    try:
        entries = os.listdir(folder_path)
    except OSError:
        return NORMAL

    files = []
    for e in entries:
        p = os.path.join(folder_path, e)
        if os.path.isfile(p) and not is_hidden(p):
            files.append(e)

    if any(os.path.splitext(f)[1].lower() in COMIC_EXTS for f in files):
        # A ready comic is present — this is a normal pipeline job, never unwrap.
        return NORMAL

    archives = [f for f in files if is_archive_part(f)]
    if not archives:
        return NORMAL

    cruft = [
        f for f in files
        if os.path.splitext(f)[1].lower() in CRUFT_EXTS or f.lower() == "file_id.diz"
    ]

    if len(archives) >= 2 or cruft or any(_looks_obfuscated(f) for f in archives):
        return MULTIPART_ARCHIVE
    return NORMAL


def _find_comics(dirpath):
    """Return full paths of comic files anywhere under ``dirpath`` (sorted)."""
    found = []
    for root, dirs, files in os.walk(dirpath):
        dirs[:] = [d for d in dirs if not is_hidden(os.path.join(root, d))]
        for f in files:
            if is_hidden(os.path.join(root, f)):
                continue
            if os.path.splitext(f)[1].lower() in COMIC_EXTS:
                found.append(os.path.join(root, f))
    return sorted(found)


def _plan_layer_extractions(dirpath):
    """Return the list of primary-volume paths to extract from one directory.

    RAR/spanned-zip/split sets collapse to a single primary (the extractor
    follows the rest); independent zips each become their own primary.
    """
    try:
        entries = os.listdir(dirpath)
    except OSError:
        return []
    archives = [
        e for e in entries
        if os.path.isfile(os.path.join(dirpath, e))
        and not is_hidden(os.path.join(dirpath, e))
        and is_archive_part(e)
    ]

    primaries = []

    rar_family = [f for f in archives if _is_rar_family(f)]
    rar_groups = defaultdict(list)
    for f in rar_family:
        rar_groups[_rar_stem(f)].append(f)
    for group in rar_groups.values():
        primaries.append(os.path.join(dirpath, pick_primary_volume(group)))

    # Zip: skip spanned volumes (.zNN) — their .zip primary drives them. Every
    # remaining .zip (spanned primary or independent) is extracted.
    for f in archives:
        if ZIP_MAIN_RE.search(f) and not ZIP_VOL_RE.search(f):
            primaries.append(os.path.join(dirpath, f))

    split_groups = defaultdict(list)
    for f in archives:
        if SPLIT_RE.search(f):
            split_groups[f[:-4].lower()].append(f)
    for group in split_groups.values():
        primaries.append(os.path.join(dirpath, pick_primary_volume(group)))

    return primaries


def _extract_zip(zip_path, out_dir):
    """Extract a zip into ``out_dir``. Returns (ok, failed_count).

    Standard/independent zips go through zipfile; a spanned or otherwise
    unreadable zip falls back to 7z then unar (which assemble volume sets).
    """
    os.makedirs(out_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            failed = 0
            for member in zf.namelist():
                try:
                    zf.extract(member, out_dir)
                except Exception as e:
                    failed += 1
                    app_logger.warning(f"unwrap: failed to extract {member} from {zip_path}: {e}")
        return True, failed
    except zipfile.BadZipFile:
        pass  # likely a spanned volume — fall back to external tools
    except Exception as e:
        app_logger.warning(f"unwrap: zipfile error on {zip_path}: {e}")

    for cmd in (
        ["7z", "x", f"-o{out_dir}", "-y", zip_path],
        ["unar", "-f", "-o", out_dir, zip_path],
    ):
        try:
            subprocess.run(cmd, capture_output=True)
        except FileNotFoundError:
            continue
        except Exception as e:
            app_logger.warning(f"unwrap: {cmd[0]} error on {zip_path}: {e}")
            continue
        if os.path.isdir(out_dir) and any(os.listdir(out_dir)):
            return True, 0
    return False, 0


def _extract_archive(primary_path, out_dir):
    """Dispatch to the RAR or ZIP extractor. Returns (ok, failed_count)."""
    name = os.path.basename(primary_path)
    if _is_rar_family(name):
        try:
            return extract_rar_with_unar(primary_path, out_dir)
        except Exception as e:
            app_logger.warning(f"unwrap: RAR extraction failed for {primary_path}: {e}")
            return False, 0
    return _extract_zip(primary_path, out_dir)


def _dir_size(dirpath):
    total = 0
    for root, _dirs, files in os.walk(dirpath):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def unwrap_release(src_folder, work_root, *, max_depth=8, max_expansion_ratio=200):
    """Recursively unwrap a multipart release folder.

    Stages the archive parts into an isolated work dir under ``work_root`` and
    extracts layer by layer (breadth-first) until comic files emerge. The source
    folder is never modified. The caller is responsible for removing the
    returned ``work_dir`` once it has moved the comics out.

    Guards against archive bombs via ``max_depth``, a cumulative-size ceiling
    (``max_expansion_ratio`` * staged size), and a visited-name set.

    Returns an ``UnwrapResult(comics, ok, reason, partial, work_dir)`` where
    ``comics`` are paths inside ``work_dir``.
    """
    try:
        src_folder = os.path.realpath(src_folder)
        if not is_allowed_path(src_folder):
            return UnwrapResult([], False, "path_not_allowed", False, None)

        parts = [
            os.path.join(src_folder, e)
            for e in os.listdir(src_folder)
            if os.path.isfile(os.path.join(src_folder, e))
            and not is_hidden(os.path.join(src_folder, e))
            and is_archive_part(e)
        ]
        if not parts:
            return UnwrapResult([], False, "no_archives", False, None)

        os.makedirs(work_root, exist_ok=True)
        work_dir = os.path.join(
            work_root, f"{os.path.basename(src_folder)}-{uuid.uuid4().hex[:8]}"
        )
        layer0 = os.path.join(work_dir, "L0")
        os.makedirs(layer0)

        staged_size = 0
        for p in parts:
            shutil.copy2(p, os.path.join(layer0, os.path.basename(p)))
            try:
                staged_size += os.path.getsize(p)
            except OSError:
                pass

        comics = []
        partial = False
        total_extracted = 0
        visited = set()
        queue = [(layer0, 0)]
        layer_no = 0

        while queue:
            cur_dir, depth = queue.pop(0)

            found = _find_comics(cur_dir)
            if found:
                comics.extend(found)
                continue

            if depth >= max_depth:
                app_logger.warning(
                    f"unwrap: max depth {max_depth} reached for {src_folder}"
                )
                return UnwrapResult(comics, False, "max_depth", partial, work_dir)

            primaries = _plan_layer_extractions(cur_dir)
            primaries = [p for p in primaries if os.path.basename(p).lower() not in visited]
            if not primaries:
                continue

            layer_no += 1
            out_dir = os.path.join(work_dir, f"L{depth + 1}_{layer_no}")
            os.makedirs(out_dir, exist_ok=True)

            for primary in primaries:
                visited.add(os.path.basename(primary).lower())
                ok, failed = _extract_archive(primary, out_dir)
                if failed:
                    partial = True

            total_extracted += _dir_size(out_dir)
            if staged_size and total_extracted > staged_size * max_expansion_ratio:
                app_logger.warning(
                    f"unwrap: expansion ceiling exceeded for {src_folder}"
                )
                return UnwrapResult(comics, False, "expansion_limit", partial, work_dir)

            queue.append((out_dir, depth + 1))

        if comics:
            return UnwrapResult(comics, True, None, partial, work_dir)
        return UnwrapResult([], False, "no_comics", partial, work_dir)

    except Exception as e:
        app_logger.error(f"unwrap: unexpected error unwrapping {src_folder}: {e}")
        wd = locals().get("work_dir")
        return UnwrapResult([], False, "error", False, wd)
