"""
Split a multi-issue CBZ into several single-issue CBZ files.

The source archive collects several issues whose page images are named with a
recognizable pattern, e.g.:

    Cult of Dracula 003 - 0001.jpg   (series, issue 003, page 0001)
    Cult of Dracula 004 - 0001.jpg

The middle number is the issue; the trailing number is the page. This module
auto-detects issue boundaries from those page filenames, and writes one
image-only CBZ per issue.

Unpacking is NON-destructive: the source .cbz is read in place and extracted to
a unique temp folder, so the original is never renamed or modified (unlike the
Edit flow's ``process_cbz_file``, which renames .cbz -> .zip).

CBZ writes go through ``helpers.open_zip_for_write`` (assemble-local-then-move
for FUSE/ESPIPE mounts, plus parent-permission matching) — never write a
``zipfile.ZipFile`` directly onto the data mount.
"""

import os
import re
import gc
import uuid
import base64
import shutil
import zipfile
from collections import Counter

from core.app_logging import app_logger
from helpers import create_thumbnail_streaming, open_zip_for_write, sanitize_path_segment
from cbz_ops.edit import _safe_join, deletedFiles, skippedFiles

IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')

# Page-name parser: "<series> <issue> - <page>". Targets the names of the image
# files *inside* the archive (not the archive filename, which cbz_ops/rename.py
# handles). Issue may carry a decimal suffix (e.g. 003.1); page is trailing.
PAGE_PATTERN = re.compile(
    r'^(?P<series>.*?)\s+(?P<issue>\d{1,4}(?:\.\d+)?)\s*-\s*(?P<page>\d{1,4})$'
)


def _natural_key(s):
    """Natural sort key so 'x 2' sorts before 'x 10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


def _parse_page(basename):
    """Parse a page image basename into {series, issue, page} or None."""
    stem = os.path.splitext(basename)[0].strip()
    m = PAGE_PATTERN.match(stem)
    if not m:
        return None
    return {
        'series': m.group('series').strip(),
        'issue': m.group('issue'),
        'page': m.group('page'),
    }


def parse_issue_key(basename):
    """Return the normalized issue token for a page basename, or None."""
    parsed = _parse_page(basename)
    return parsed['issue'] if parsed else None


def _detect_series(rel_paths):
    """Most common series prefix across parseable page names ('' if none)."""
    names = Counter()
    for rel in rel_paths:
        parsed = _parse_page(os.path.basename(rel))
        if parsed and parsed['series']:
            names[parsed['series']] += 1
    return names.most_common(1)[0][0] if names else ''


def detect_groups(rel_paths):
    """Group page rel_paths into consecutive issues by their issue key.

    - Pages are naturally sorted by basename first.
    - A new group starts each time the issue key changes.
    - Pages with no parseable key attach to the current group. Leading
      unmatched pages are absorbed into the first keyed group.
    - When NO page has a key, a single group is returned (manual mode).

    Returns an ordered list of {issue_key, page_rel_paths:[...]}.
    """
    ordered = sorted(rel_paths, key=lambda p: _natural_key(os.path.basename(p)))
    groups = []
    current = None
    current_key = None
    for rel in ordered:
        key = parse_issue_key(os.path.basename(rel))
        if current is None:
            current = {'issue_key': key, 'page_rel_paths': [rel]}
            current_key = key
            continue
        if key is not None and current_key is not None and key != current_key:
            groups.append(current)
            current = {'issue_key': key, 'page_rel_paths': [rel]}
            current_key = key
        else:
            current['page_rel_paths'].append(rel)
            if current_key is None and key is not None:
                current_key = key
                current['issue_key'] = key
    if current is not None:
        groups.append(current)
    return groups


def extract_for_split(file_path):
    """Non-destructively extract a .cbz to a unique temp folder.

    The original file is left in place (opened read-only). Junk files are
    dropped and a single nested wrapper directory is collapsed, mirroring
    cbz_ops/edit.py:process_cbz_file steps 4-5.

    Returns {'folder_name': <where pages live>, 'root_folder': <temp dir to
    remove>}. On any error the temp dir is removed and the error re-raised.
    """
    if not file_path.lower().endswith(('.cbz', '.zip')):
        raise ValueError("Provided file is not a CBZ file.")

    base_dir = os.path.dirname(os.path.abspath(file_path))
    root_folder = os.path.join(base_dir, f".tmp_split_{os.getpid()}_{uuid.uuid4().hex[:8]}")
    os.makedirs(root_folder, exist_ok=True)

    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            for name in zf.namelist():
                try:
                    zf.extract(name, root_folder)
                except Exception as e:
                    app_logger.warning(f"Failed to extract {name}: {e}")
                    continue

        # Drop configured junk extensions before collapsing nested folders.
        for root, _, files in os.walk(root_folder):
            for f in files:
                if os.path.splitext(f)[1].lower() in deletedFiles:
                    try:
                        os.remove(os.path.join(root, f))
                    except Exception as e:
                        app_logger.error(f"Error deleting file {f}: {e}")

        # Collapse single nested wrapper directories.
        folder_name = root_folder
        while True:
            inner = [d for d in os.listdir(folder_name)
                     if os.path.isdir(os.path.join(folder_name, d))]
            loose = [f for f in os.listdir(folder_name)
                     if os.path.isfile(os.path.join(folder_name, f))]
            if len(inner) == 1 and not loose:
                folder_name = os.path.join(folder_name, inner[0])
            else:
                break

        return {'folder_name': folder_name, 'root_folder': root_folder}
    except Exception:
        shutil.rmtree(root_folder, ignore_errors=True)
        raise


def _suggest_name(series, issue_key, file_path, n):
    base = series or os.path.splitext(os.path.basename(file_path))[0]
    name = f"{base} {issue_key}" if issue_key else f"{base} part {n}"
    return sanitize_path_segment(name)


def get_split_modal(file_path):
    """Extract, build per-page thumbnails, and auto-detect issue groups.

    Returns {groups, folder_name, root_folder, original_file_path,
    suggested_folder}, where each group is
    {issue_key, suggested_name, pages:[{filename, rel_path, img_data, issue_key}]}.
    Cleans up the temp extraction dir if anything after extraction fails.
    """
    result = extract_for_split(file_path)
    folder_name = result['folder_name']
    root_folder = result['root_folder']

    try:
        rel_paths = []
        for root, _, files in os.walk(folder_name):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in skippedFiles:
                    continue
                if f.lower() == 'comicinfo.xml':
                    continue
                if ext not in IMAGE_EXTS:
                    continue
                rel_paths.append(os.path.relpath(os.path.join(root, f), folder_name))

        groups = detect_groups(rel_paths)
        series = _detect_series(rel_paths)

        out_groups = []
        count = 0
        for g in groups:
            pages = []
            for rel in g['page_rel_paths']:
                full = os.path.join(folder_name, rel)
                img_data = None
                try:
                    thumb = create_thumbnail_streaming(full, max_size=(400, 600), quality=85)
                    if thumb:
                        img_data = "data:image/jpeg;base64," + base64.b64encode(thumb).decode('utf-8')
                except Exception as e:
                    app_logger.info(f"Thumbnail generation failed for '{rel}': {e}")
                pages.append({
                    'filename': os.path.basename(rel),
                    'rel_path': rel,
                    'img_data': img_data,
                    'issue_key': g['issue_key'],
                })
                count += 1
                if count % 10 == 0:
                    gc.collect()

            out_groups.append({
                'issue_key': g['issue_key'],
                'suggested_name': _suggest_name(series, g['issue_key'], file_path, len(out_groups) + 1),
                'pages': pages,
            })

        suggested_folder = sanitize_path_segment(
            series or os.path.splitext(os.path.basename(file_path))[0]
        )

        return {
            'groups': out_groups,
            'folder_name': folder_name,
            'root_folder': root_folder,
            'original_file_path': file_path,
            'suggested_folder': suggested_folder,
        }
    except Exception:
        shutil.rmtree(root_folder, ignore_errors=True)
        raise


def commit_split(folder_name, output_directory, groups):
    """Write one image-only CBZ per group into output_directory.

    Each group is {output_name, rel_paths:[...]}. Output names are sanitized and
    collision-suffixed with " (1)", " (2)". Duplicate page basenames within a
    group get an a/b/c suffix. No ComicInfo.xml is written.

    Returns [{name, path, total_images}, ...]. Does not touch the DB or the temp
    folder — the caller indexes outputs and cleans up.
    """
    os.makedirs(output_directory, exist_ok=True)
    outputs = []

    for g in groups:
        name = sanitize_path_segment(g.get('output_name') or '') or 'Split'
        output_path = os.path.join(output_directory, name + '.cbz')
        counter = 1
        while os.path.exists(output_path):
            output_path = os.path.join(output_directory, f"{name} ({counter}).cbz")
            counter += 1

        file_counter = {}
        total = 0
        with open_zip_for_write(output_path) as zf:
            ordered = sorted(g.get('rel_paths', []),
                             key=lambda p: _natural_key(os.path.basename(p)))
            for rel in ordered:
                abs_path = _safe_join(folder_name, rel)
                if not os.path.isfile(abs_path):
                    continue
                base = os.path.basename(rel)
                name_part, ext = os.path.splitext(base)
                if base in file_counter:
                    suffix = chr(ord('a') + file_counter[base])
                    arcname = f"{name_part}{suffix}{ext}"
                    file_counter[base] += 1
                else:
                    arcname = base
                    file_counter[base] = 1
                zf.write(abs_path, arcname)
                total += 1

        outputs.append({
            'name': os.path.basename(output_path),
            'path': output_path,
            'total_images': total,
        })

    return outputs
