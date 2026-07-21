"""
Update ComicInfo.xml fields in CBZ files.
"""
import os
import zipfile
import shutil
import xml.etree.ElementTree as ET
import defusedxml.ElementTree as SafeET
from core.comicinfo import find_comicinfo_in_zip
from helpers import open_zip_for_write


def update_field_in_cbz_files(folder_path: str, field: str, value: str) -> dict:
    """
    Update a field in ComicInfo.xml for all CBZ files in a folder.

    Args:
        folder_path: Path to the folder containing CBZ files
        field: XML field name to update (e.g., 'Volume')
        value: New value for the field

    Returns:
        dict with 'updated', 'skipped', 'errors' counts
    """
    result = {'updated': 0, 'skipped': 0, 'errors': 0, 'details': []}

    if not os.path.isdir(folder_path):
        return {'error': f'{folder_path} is not a valid directory'}

    for filename in os.listdir(folder_path):
        if not filename.lower().endswith(".cbz"):
            continue

        cbz_path = os.path.join(folder_path, filename)

        try:
            with zipfile.ZipFile(cbz_path, "r") as zf:
                comicinfo_path = find_comicinfo_in_zip(zf)
                if comicinfo_path is None:
                    result['skipped'] += 1
                    result['details'].append({'file': filename, 'status': 'skipped', 'reason': 'no ComicInfo.xml'})
                    continue

                xml_data = zf.read(comicinfo_path)

            root = SafeET.fromstring(xml_data)
            elem = root.find(field)

            if elem is None:
                elem = ET.SubElement(root, field)

            if elem.text == str(value):
                result['skipped'] += 1
                result['details'].append({'file': filename, 'status': 'skipped', 'reason': 'already set'})
                continue

            elem.text = str(value)
            new_xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)

            # open_zip_for_write assembles the archive on a local volume and
            # moves it into place (never seeking the data mount, which can raise
            # "OSError: [Errno 29] Illegal seek" on mergerfs/network/FUSE), then
            # matches parent-folder permissions. The source is read and closed
            # inside — before the move.
            with open_zip_for_write(cbz_path) as zf_out:
                with zipfile.ZipFile(cbz_path, "r") as zf_in:
                    for item in zf_in.infolist():
                        if item.filename == comicinfo_path:
                            zf_out.writestr(item, new_xml_bytes)
                        else:
                            with zf_in.open(item.filename) as source, \
                                 zf_out.open(item, "w") as target:
                                shutil.copyfileobj(source, target)

            result['updated'] += 1
            result['details'].append({'file': filename, 'status': 'updated'})

        except Exception as e:
            result['errors'] += 1
            result['details'].append({'file': filename, 'status': 'error', 'reason': str(e)})

    return result


def update_volume_in_cbz(folder_path: str, volume_value: str):
    """Legacy function - updates Volume field in all CBZ files."""
    result = update_field_in_cbz_files(folder_path, 'Volume', volume_value)

    if 'error' in result:
        print(f"Error: {result['error']}")
        return

    for detail in result.get('details', []):
        filename = detail['file']
        status = detail['status']
        if status == 'updated':
            print(f"Processing: {filename}...")
            print("  [Updated] Volume set.")
        elif status == 'skipped':
            reason = detail.get('reason', '')
            if reason == 'no ComicInfo.xml':
                print(f"Processing: {filename}...")
                print(f"  [Skipped] ComicInfo.xml not found.")
            elif reason == 'already set':
                print(f"Processing: {filename}...")
                print(f"  [Skipped] Volume is already {volume_value}.")
        elif status == 'error':
            print(f"Processing: {filename}...")
            print(f"  [Error] Failed to process {filename}: {detail.get('reason', 'Unknown error')}")
