"""
Update ComicInfo.xml fields in CBZ files.
"""
import os
import zipfile
import shutil
import xml.etree.ElementTree as ET
import defusedxml.ElementTree as SafeET
from tempfile import NamedTemporaryFile


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
        temp_path = None

        try:
            with zipfile.ZipFile(cbz_path, "r") as zf:
                if "ComicInfo.xml" not in zf.namelist():
                    result['skipped'] += 1
                    result['details'].append({'file': filename, 'status': 'skipped', 'reason': 'no ComicInfo.xml'})
                    continue

                xml_data = zf.read("ComicInfo.xml")

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

            # Create temp file in same directory
            with NamedTemporaryFile(dir=folder_path, delete=False) as tmp_file:
                temp_path = tmp_file.name

            with zipfile.ZipFile(cbz_path, "r") as zf_in, \
                 zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf_out:

                for item in zf_in.infolist():
                    if item.filename == "ComicInfo.xml":
                        zf_out.writestr(item, new_xml_bytes)
                    else:
                        with zf_in.open(item.filename) as source, \
                             zf_out.open(item, "w") as target:
                            shutil.copyfileobj(source, target)

            os.replace(temp_path, cbz_path)
            result['updated'] += 1
            result['details'].append({'file': filename, 'status': 'updated'})

        except Exception as e:
            result['errors'] += 1
            result['details'].append({'file': filename, 'status': 'error', 'reason': str(e)})
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

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
