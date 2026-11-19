"""Merge a conflicting series folder into the mapped one, or drop its duplicates.

A series points at one folder (``series.mapped_path``), so a second folder for
the same series holds comics that are not counted as owned
(``core/series_conflicts.py``). This module resolves one such conflict:

- **Merge**: comics only in this folder move into the mapped folder; an issue
  in both keeps one copy (the user's choice, or :func:`_default_keeper`) and
  the other goes to the trash. A folder left with no comics is trashed too.
- **Drop**: only the copies in this folder of issues the mapped folder already
  has go to the trash. Nothing moves.

Things that look arbitrary and are not:

- **The plan is computed three times and must agree.** The page shows a plan,
  the request re-plans with the user's choices, and the queued job re-plans
  once more before it touches anything, because a job can wait behind a long
  tagging batch. ``signature`` covers what both folders hold (paths and sizes),
  not the choices, so a different one means the folders changed and the merge
  is refused. One refusal rejects the whole merge before anything moves, as
  the file job queue does.
- **A move never overwrites** (``move_file(replace=False)``, #731). A name
  already taken in the mapped folder by a *different* file is held and
  reported, never ``(1)``-suffixed; an identical file there is a duplicate.
- **Issues are paired by number and by the name in front of it**: the number
  is ``ci_number`` from the index when known, else
  ``models.providers.base.extract_issue_number``, which is quiet
  (``parse_comic_filename`` logs at INFO per file). The number alone paired
  ``Kaya Annual 001`` with ``Kaya 001`` and trashed the real #1, so the words
  before the first number (brackets dropped, case and punctuation ignored)
  must agree too. Two names that do not agree are not a pair: both files are
  kept. A key held by two files on one side (variants) is not paired either:
  those files move, so nothing is trashed on a guess.
- **The keeper is healthy over damaged, then the larger file, and the mapped
  folder wins ties** (plan 15). The healthy-first rule is the one from
  ``core.library_health._suggest_keeper``.
- **A folder is trashed only when nothing but sidecars is left in it**: no
  subfolder of any name (``is_hidden`` treats ``_Specials`` as junk, the trap
  the directory-deletion rule warns about), and no file that is neither a
  comic nor a known sidecar, so a format ``folder_files`` does not list is
  never swept up with the folder.
- **One reconcile per merge**, at the end, as ``_do_move_batch`` does.
- Runs on ``core.job_queue.file_jobs``: a 300-issue folder is a lot of moves on
  a NAS, and a merge must not move a path out from under a queued rename.
"""

import filecmp
import hashlib
import os
import re

from core.app_logging import app_logger
from core.series_conflicts import folder_files

MODE_MERGE = "merge"
MODE_DROP = "drop"
MODES = (MODE_MERGE, MODE_DROP)
SIDE_HERE = "here"
SIDE_MAPPED = "mapped"


class MergeRefused(Exception):
    """The merge cannot run as asked; ``status`` is the HTTP status to answer with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _norm(path):
    return os.path.normpath(path or "").rstrip(os.sep)


def _inside(path, folder):
    path, folder = _norm(path), _norm(folder)
    return path == folder or path.startswith(folder + os.sep)


_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}")
_NUMBER_TOKEN = re.compile(r"^#?-?\d")


def _title_key(name):
    """The words before the first number token, without case or punctuation.

    ``Kaya 001 (2022)`` and ``Kaya #1`` both give ``kaya``; ``Kaya Annual 001``
    gives ``kayaannual``, so it can never pair with issue #1. A leading "The"
    and ``&`` for "and" are spelling, not a different book: both folders are
    already the same series (``The Infernal Hulk`` / ``Infernal Hulk``).
    """
    stem = _BRACKETS.sub(" ", os.path.splitext(name)[0]).replace("&", " and ")
    stem = re.sub(r"^\s*the\s+", "", stem, flags=re.I)
    words = []
    for token in re.split(r"[\s_]+", stem):
        if _NUMBER_TOKEN.match(token):
            break
        words.append(token)
    return re.sub(r"[^0-9a-z]+", "", " ".join(words).lower())


def _issue_key(name, ci_number):
    from core.library_health import _norm_number, name_key
    from models.providers.base import extract_issue_number

    number = _norm_number(str(ci_number)) if ci_number not in (None, "") else ""
    if not number:
        number = _norm_number(extract_issue_number(name) or "")
    if number:
        return "n:" + _title_key(name) + "|" + number, "#" + number
    return "name:" + name_key(name), os.path.splitext(name)[0]


def _index_info(paths):
    """``{path: (ci_number, status)}`` from the index and the health scan."""
    from core.database import db_conn

    out = {}
    if not paths:
        return out
    try:
        with db_conn() as conn:
            if not conn:
                return out
            for i in range(0, len(paths), 500):
                chunk = paths[i:i + 500]
                marks = ",".join("?" * len(chunk))
                for path, number in conn.execute(
                    f"SELECT path, ci_number FROM file_index WHERE path IN ({marks})", chunk
                ).fetchall():
                    out[path] = [number, None]
                for path, status, size in conn.execute(
                    f"SELECT path, status, size FROM file_health WHERE path IN ({marks})", chunk
                ).fetchall():
                    # A verdict about bytes that have since been replaced is not about this file.
                    try:
                        current = os.path.getsize(path) == size
                    except OSError:
                        current = False
                    if current:
                        out.setdefault(path, [None, None])[1] = status
    except Exception as e:
        app_logger.debug(f"series merge: index lookup failed: {e}")
    return out


def _members(folder):
    names = folder_files(folder)
    paths = [os.path.join(folder, n) for n in names]
    info = _index_info(paths)
    members = []
    for name, path in zip(names, paths):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        number, status = info.get(path, (None, None))
        key, label = _issue_key(name, number)
        members.append({
            "name": name, "path": path, "size": size,
            "damaged": status == "damaged", "key": key, "label": label,
        })
    return members


# Two copies of one issue usually differ by a re-tag (a few KB of ComicInfo),
# not by quality. Within this fraction the sizes are a tie, and the mapped copy
# stays: picking the "larger" one there moved 80 MB files for nothing.
SIZE_TIE_FRACTION = 0.02


def _default_keeper(here, mapped):
    """Healthy over damaged, then clearly larger; the mapped copy wins ties."""
    if here["damaged"] != mapped["damaged"]:
        return SIDE_MAPPED if here["damaged"] else SIDE_HERE
    bigger = max(here["size"], mapped["size"]) or 1
    if abs(here["size"] - mapped["size"]) / bigger <= SIZE_TIE_FRACTION:
        return SIDE_MAPPED
    return SIDE_HERE if here["size"] > mapped["size"] else SIDE_MAPPED


def _identical(a, b):
    try:
        return os.path.getsize(a) == os.path.getsize(b) and filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def _signature(members):
    """What both folders hold (paths and sizes) -- not what the user chose to do.

    The keeper choices travel separately and are re-applied; this only proves
    the folders are the ones the user looked at.
    """
    parts = sorted(f"{m['path']}|{m['size']}" for m in members)
    return hashlib.sha1("\n".join(parts).encode("utf-8", "surrogateescape"), usedforsecurity=False).hexdigest()


def validate(row):
    """Refuse a merge that must not run. Returns ``(folder, mapped_folder)``.

    Path checks only; the grant check needs the request user and lives in the
    route (``grant_refusal``).
    """
    from helpers.library import is_critical_path, is_valid_library_path

    if not row:
        raise MergeRefused("That conflict is no longer listed", 404)
    folder, mapped = row.get("folder"), row.get("mapped_folder")
    if not mapped:
        raise MergeRefused("This series is not mapped to a folder any more; use this folder instead")
    for path in (folder, mapped):
        if not is_valid_library_path(path):
            raise MergeRefused("Both folders must be inside a library")
        if is_critical_path(path):
            raise MergeRefused("A protected folder cannot be merged")
    if not os.path.isdir(folder):
        raise MergeRefused("This folder no longer exists", 409)
    if not os.path.isdir(mapped):
        raise MergeRefused("The mapped folder no longer exists", 409)
    if _inside(folder, mapped) or _inside(mapped, folder):
        raise MergeRefused("One folder is inside the other; move it in the File Manager instead")
    return folder, mapped


def build_plan(row, mode=MODE_MERGE, keep=None):
    """What a merge or drop would do, without doing any of it.

    ``keep`` maps an issue key to ``"here"`` or ``"mapped"`` for issues in both
    folders; anything not named takes the default keeper. Returns::

        {mode, folder, mapped_folder, pairs, only_here, only_mapped,
         held, actions, folder_emptied, signature}
    """
    if mode not in MODES:
        raise MergeRefused("Unknown mode")
    folder, mapped = validate(row)
    keep = keep or {}
    here, there = _members(folder), _members(mapped)

    by_key_here, by_key_there = {}, {}
    for m in here:
        by_key_here.setdefault(m["key"], []).append(m)
    for m in there:
        by_key_there.setdefault(m["key"], []).append(m)
    there_names = {m["name"]: m for m in there}

    pairs, only_here, held, actions = [], [], [], []
    paired_there = set()
    for key, members in by_key_here.items():
        others = by_key_there.get(key, [])
        if len(members) == 1 and len(others) == 1:
            h, t = members[0], others[0]
            default = _default_keeper(h, t)
            side = keep.get(key) if keep.get(key) in (SIDE_HERE, SIDE_MAPPED) else default
            pairs.append({"key": key, "label": h["label"], "here": h, "mapped": t,
                          "keep": side, "default": default})
            paired_there.add(t["path"])
        else:
            only_here.extend(members)

    for p in pairs:
        h, t = p["here"], p["mapped"]
        if mode == MODE_DROP or p["keep"] == SIDE_MAPPED:
            actions.append({"op": "trash", "trash": h["path"], "label": p["label"]})
            continue
        dst = os.path.join(mapped, h["name"])
        clash = there_names.get(h["name"])
        if clash and clash["path"] != t["path"]:
            held.append({"path": h["path"], "name": h["name"],
                         "reason": "A different file with this name is already in the mapped folder"})
            continue
        actions.append({"op": "replace", "src": h["path"], "dst": dst,
                        "trash": t["path"], "label": p["label"]})

    moving = []
    for m in only_here:
        clash = there_names.get(m["name"])
        if clash and clash["path"] not in paired_there and _identical(m["path"], clash["path"]):
            # The same bytes under the same name: a duplicate, whatever the number says.
            actions.append({"op": "trash", "trash": m["path"], "label": m["label"]})
        elif mode == MODE_DROP:
            continue
        elif clash:
            held.append({"path": m["path"], "name": m["name"],
                         "reason": "A different file with this name is already in the mapped folder"})
        else:
            moving.append(m)
            actions.append({"op": "move", "src": m["path"],
                            "dst": os.path.join(mapped, m["name"]), "label": m["label"]})

    # Every comic in this folder leaves it (moved, or trashed as a duplicate).
    leaving = {a["src"] if a["op"] != "trash" else a["trash"] for a in actions}
    plan = {
        "mode": mode,
        "folder": folder,
        "mapped_folder": mapped,
        "pairs": sorted(pairs, key=lambda p: p["key"]),
        "only_here": sorted(moving, key=lambda m: m["name"].lower()),
        "only_mapped": sorted((m for m in there if m["path"] not in paired_there),
                              key=lambda m: m["name"].lower()),
        "held": held,
        "actions": actions,
        "folder_emptied": bool(here) and len(leaving) == len(here) and not _has_other_entries(folder),
    }
    plan["signature"] = _signature(here + there)
    return plan


def summary(plan):
    acts = plan["actions"]
    return {
        "move": sum(1 for a in acts if a["op"] in ("move", "replace")),
        "trash": sum(1 for a in acts if a["op"] in ("trash", "replace")),
        "held": len(plan["held"]),
    }


def _forget_trashed(paths, replaced=()):
    """The bookkeeping ``library_health.resolve_group`` does after a trash.

    ``replaced`` are trashed paths that the kept copy then moved onto. Their
    index row and path references now belong to the kept file (the move
    carried them over), so only the verdicts about the old bytes are cleared:
    forgetting the path would wipe the kept file's row and its reading-list
    matches.
    """
    from core.database import db_conn, forget_deleted_paths
    from core.problem_files import clear_problem

    paths, replaced = list(paths or []), list(replaced or [])
    if not paths and not replaced:
        return
    try:
        if paths:
            forget_deleted_paths(paths)
        for path in paths + replaced:
            clear_problem(path)
        with db_conn() as conn:
            if conn:
                conn.executemany("DELETE FROM file_health WHERE path = ?",
                                 [(p,) for p in paths + replaced])
                conn.commit()
    except Exception as e:
        app_logger.error(f"series merge: bookkeeping after trash failed: {e}")


def _trash(path, trashed=None):
    from helpers.trash import move_to_trash

    move_to_trash(path)
    if trashed is not None:
        trashed.append(path)


def _move(src, dst, rename):
    """Move one comic without overwriting; returns where it ended up."""
    from core.database import move_indexed_path
    from helpers import match_parent_permissions, move_file

    move_file(src, dst, replace=False)
    match_parent_permissions(dst)
    move_indexed_path(src, dst, is_dir=False)
    if not rename:
        return dst
    try:
        from cbz_ops.rename import get_renamed_filename
        from helpers.no_clobber import rename_no_clobber

        new_name = get_renamed_filename(os.path.basename(dst), file_path=dst)
        if new_name and new_name != os.path.basename(dst):
            renamed = os.path.join(os.path.dirname(dst), new_name)
            rename_no_clobber(dst, renamed)
            move_indexed_path(dst, renamed, is_dir=False)
            return renamed
    except FileExistsError:
        app_logger.info(f"series merge: kept the name of {dst}; the renamed one is taken")
    except Exception as e:
        app_logger.warning(f"series merge: could not rename {dst}: {e}")
    return dst


# What may be left in a folder that is trashed once its comics have gone: the
# series sidecars, folder art and OS litter. Anything else keeps the folder.
_SIDECAR_NAMES = {"cvinfo", "cvinfo.txt", "series.json", "comicinfo.xml",
                  ".ds_store", "thumbs.db", "desktop.ini"}
_SIDECAR_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".nfo", ".txt", ".json", ".xml")


def _has_other_entries(folder):
    """True when ``folder`` holds a subfolder or a file that is neither a comic nor a sidecar.

    Any subfolder counts, hidden or not: ``is_hidden`` is true for
    ``_Specials``, which can hold comics.
    """
    from core.series_conflicts import COMIC_EXTENSIONS

    try:
        for entry in os.scandir(folder):
            if entry.is_dir(follow_symlinks=False) or entry.is_symlink():
                return True
            name = entry.name.lower()
            if name.endswith(COMIC_EXTENSIONS) or name in _SIDECAR_NAMES:
                continue
            if not name.endswith(_SIDECAR_EXTENSIONS):
                return True
    except OSError:
        return True
    return False


def _folder_is_empty_of_comics(folder):
    """True when ``folder`` holds no comics, no subfolders and nothing but sidecars."""
    return not _has_other_entries(folder) and not folder_files(folder)


def _trash_folder(folder):
    from core.database import forget_deleted_paths
    from helpers.library import get_protected_roots, is_critical_path
    from helpers.trash import move_to_trash

    real = os.path.realpath(folder)
    if real in get_protected_roots() or is_critical_path(folder):
        return False
    move_to_trash(folder)
    try:
        forget_deleted_paths([], dir_paths=[folder])
    except Exception as e:
        app_logger.error(f"series merge: could not forget {folder}: {e}")
    return True


def _detail(result):
    parts = []
    if result["moved"]:
        parts.append(f"{result['moved']} moved")
    if result["trashed"]:
        parts.append(f"{result['trashed']} trashed")
    if result["held"]:
        parts.append(f"{len(result['held'])} kept (name taken)")
    if result["failed"]:
        names = result["failed"][:3]
        more = len(result["failed"]) - len(names)
        parts.append(f"{len(result['failed'])} failed: " + "; ".join(names)
                     + (f" and {more} more" if more > 0 else ""))
    if result["folder_removed"]:
        parts.append("folder removed")
    return ", ".join(parts) or "Nothing to do"


def _execute(conflict_id, mode, keep, rename, signature, progress=None):
    """Re-plan one conflict, refuse a changed plan, then act. Raises :class:`MergeRefused`.

    ``progress(name)`` is called before each action; it is the op heartbeat.
    """
    from core import series_conflicts as sc
    from helpers.collection import reconcile_wanted_for_series

    row = sc.get_conflict(conflict_id)
    plan = build_plan(row, mode=mode, keep=keep)
    if plan["signature"] != signature:
        raise MergeRefused("the folders changed since you reviewed them", 409)

    trashed, replaced, moved, failed = [], [], 0, []
    held = list(plan["held"])
    for action in plan["actions"]:
        name = os.path.basename(action.get("src") or action["trash"])
        if progress:
            progress(name)
        other_trashed = False
        try:
            if action["op"] == "trash":
                _trash(action["trash"], trashed)
            elif action["op"] == "replace":
                # Trash first: the move must not overwrite, and a failed move
                # then leaves this folder's copy where it was.
                _trash(action["trash"])
                other_trashed = True
                landed = _move(action["src"], action["dst"], rename)
                moved += 1
                # Landed on the trashed copy's path: that path is the kept file
                # now. Renamed elsewhere: the old path is simply gone.
                (replaced if landed == action["dst"] else trashed).append(action["trash"])
            else:
                _move(action["src"], action["dst"], rename)
                moved += 1
        except FileExistsError:
            if other_trashed:
                trashed.append(action["trash"])
            held.append({"path": action["src"], "name": name,
                         "reason": "A file with this name appeared in the mapped folder"})
        except Exception as e:
            app_logger.error(f"series merge: {action['op']} failed for {name}: {e}")
            if other_trashed:
                trashed.append(action["trash"])
                failed.append(f"{name} (still in this folder; the mapped folder's copy is in the trash)")
            else:
                failed.append(name)

    _forget_trashed(trashed, replaced)

    folder = plan["folder"]
    folder_gone = False
    if not held and not failed and _folder_is_empty_of_comics(folder):
        try:
            folder_gone = _trash_folder(folder)
        except Exception as e:
            app_logger.error(f"series merge: could not trash {folder}: {e}")
    if folder_gone or not folder_files(folder):
        sc.remove(conflict_id)
    else:
        sc.refresh(conflict_id)

    try:
        reconcile_wanted_for_series(row["series_id"])
    except Exception as e:
        app_logger.error(f"series merge: reconcile failed for series {row['series_id']}: {e}")

    result = {"moved": moved, "trashed": len(trashed) + len(replaced), "held": held, "failed": failed,
              "folder_removed": folder_gone}
    app_logger.info(f"series merge ({mode}) {folder} -> {plan['mapped_folder']}: {_detail(result)}")
    return result


def run(op_id, conflict_id, mode, keep, rename, signature):
    """The ``file_jobs`` body for one conflict."""
    from core import app_state

    done = [0]

    def progress(name):
        app_state.update_operation(op_id, current=done[0], detail=name)
        done[0] += 1

    app_state.update_operation(op_id, detail="Checking the folders")
    try:
        result = _execute(conflict_id, mode, keep, rename, signature, progress)
    except MergeRefused as e:
        app_state.update_operation(op_id, detail=f"Not merged: {e}")
        app_state.complete_operation(op_id, error=True)
        return None
    app_state.update_operation(op_id, current=done[0], detail=_detail(result))
    app_state.complete_operation(
        op_id, error=bool(result["failed"]) and not (result["moved"] or result["trashed"]))
    return result


def run_group(op_id, items, rename):
    """The ``file_jobs`` body for a bulk merge: ``items`` is ``[(conflict_id, signature)]``.

    Each conflict is merged with its default keepers, one after another. A
    conflict whose folders changed after the dry run is skipped and counted,
    not merged on a plan nobody reviewed; the others still run.
    """
    from core import app_state

    totals = {"series": 0, "moved": 0, "trashed": 0, "held": 0, "failed": 0, "skipped": 0}
    done = [0]

    def progress(name):
        app_state.update_operation(op_id, current=done[0], detail=name)
        done[0] += 1

    for conflict_id, signature in items:
        try:
            result = _execute(conflict_id, MODE_MERGE, {}, rename, signature, progress)
        except MergeRefused as e:
            app_logger.info(f"series merge: skipped conflict {conflict_id} in a bulk merge: {e}")
            totals["skipped"] += 1
            continue
        except Exception as e:
            app_logger.error(f"series merge: conflict {conflict_id} failed in a bulk merge: {e}")
            totals["failed"] += 1
            continue
        totals["series"] += 1
        totals["moved"] += result["moved"]
        totals["trashed"] += result["trashed"]
        totals["held"] += len(result["held"])
        totals["failed"] += len(result["failed"])

    parts = [f"{totals['series']} series merged"]
    for key, label in (("moved", "moved"), ("trashed", "trashed"),
                       ("held", "kept (name taken)"), ("skipped", "skipped (changed)"),
                       ("failed", "failed")):
        if totals[key]:
            parts.append(f"{totals[key]} {label}")
    detail = ", ".join(parts)
    app_logger.info(f"series merge: bulk merge done: {detail}")
    app_state.update_operation(op_id, current=done[0], detail=detail)
    app_state.complete_operation(op_id, error=not totals["series"] and bool(items))
    return totals


def _folder_key(name):
    """A folder name with case and punctuation removed: ``Boom! Studios`` == ``boom studios``."""
    import re
    return re.sub(r"[^0-9a-z]+", "", (name or "").lower())


def likely_same_name(a, b):
    """Two folder names that differ only by case or punctuation (``Thunderhawks``/``ThunderHawks``)."""
    a, b = os.path.basename(_norm(a)), os.path.basename(_norm(b))
    return bool(a) and a != b and _folder_key(a) == _folder_key(b)


def group_by_parent(rows, minimum=2):
    """Open conflicts grouped by ``(parent of folder, parent of mapped folder)``.

    The common shape is one parent duplicated (``/data/Image`` beside
    ``/data/Image Comics``), and resolving those one row at a time is the
    wrong granularity. Only pairs with at least ``minimum`` conflicts form a
    group; ignored rows and rows with no mapped folder are left out.
    """
    groups = {}
    for row in rows or []:
        if row.get("ignored") or not row.get("mapped_folder"):
            continue
        key = (os.path.dirname(_norm(row["folder"])), os.path.dirname(_norm(row["mapped_folder"])))
        if key[0] == key[1]:
            # Siblings in one parent (case-only variants): not a duplicated parent.
            continue
        groups.setdefault(key, []).append(row)
    out = []
    for (parent, mapped_parent), members in groups.items():
        if len(members) < minimum:
            continue
        out.append({
            "parent": parent,
            "mapped_parent": mapped_parent,
            "likely_same": likely_same_name(parent, mapped_parent),
            "ids": [r["id"] for r in members],
            "count": len(members),
        })
    out.sort(key=lambda g: (-g["count"], g["parent"].lower()))
    return out
