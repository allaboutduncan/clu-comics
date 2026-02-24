"""
Metadata Blueprint

Provides routes for:
- CBZ metadata extraction and ComicInfo.xml management
- GCD (Grand Comics Database) metadata search
- ComicVine metadata search
- Batch metadata processing
- Provider management (credentials, testing, library config)
- XML field updates
"""

import os
import re
import io
import json
import time
import shutil
import zipfile
import traceback
import xml.etree.ElementTree as ET
import mysql.connector
from datetime import datetime
from flask import (Blueprint, request, jsonify, Response,
                   stream_with_context, current_app)
from app_logging import app_logger
from config import config
from helpers.library import is_valid_library_path
from models import gcd, metron, comicvine
from models.gcd import STOPWORDS

metadata_bp = Blueprint('metadata', __name__)


# =============================================================================
# Helper Functions (used by multiple routes)
# =============================================================================

def _as_text(val):
    if val is None:
        return None
    if isinstance(val, (list, tuple, set)):
        # ComicInfo expects comma-separated for multi-credits
        return ", ".join(str(x) for x in val if x is not None and str(x).strip())
    return str(val)


def generate_comicinfo_xml(issue_data, series_data=None):
    """
    Generate a ComicInfo.xml that ComicRack will actually read.
    - No XML namespaces
    - UTF-8 bytes with XML declaration
    - Only write elements when we have non-empty values
    - Ensure numeric fields are integers-as-text
    """
    root = ET.Element("ComicInfo")  # IMPORTANT: no xmlns/xsi attributes

    def add(tag, value):
        val = _as_text(value)
        if val:
            ET.SubElement(root, tag).text = val

    # Basic
    add("Title",   issue_data.get("Title"))
    add("Series",  issue_data.get("Series"))
    # Number/Count/Volume should be simple numerics-as-text
    if issue_data.get("Number") not in (None, ""):
        add("Number", str(int(float(issue_data["Number"]))) if str(issue_data["Number"]).replace(".","",1).isdigit() else str(issue_data["Number"]))
    if issue_data.get("Count") not in (None, ""):
        add("Count", str(int(issue_data["Count"])) )
    if issue_data.get("Volume") not in (None, ""):
        add("Volume", str(int(issue_data["Volume"])) )

    add("Summary", issue_data.get("Summary"))

    # Dates
    if issue_data.get("Year") not in (None, ""):
        add("Year", str(int(issue_data["Year"])))
    if issue_data.get("Month") not in (None, ""):
        m = int(issue_data["Month"])
        if 1 <= m <= 12:
            add("Month", str(m))

    # Credits
    add("Writer",      issue_data.get("Writer"))
    add("Penciller",   issue_data.get("Penciller"))
    add("Inker",       issue_data.get("Inker"))
    add("Colorist",    issue_data.get("Colorist"))
    add("Letterer",    issue_data.get("Letterer"))
    add("CoverArtist", issue_data.get("CoverArtist"))

    # Publisher/Imprint
    add("Publisher", issue_data.get("Publisher"))

    # Genre/Characters
    add("Genre",      issue_data.get("Genre"))
    add("Characters", issue_data.get("Characters"))

    # Language (ComicRack likes LanguageISO, e.g., 'en')
    add("LanguageISO", issue_data.get("LanguageISO") or "en")

    # Page count (integer)
    if issue_data.get("PageCount") not in (None, ""):
        add("PageCount", str(int(issue_data["PageCount"])))

    # Manga flag: ComicRack expects "Yes" or "No"
    add("Manga", "No")

    # Metron ID (for scrobble support)
    add("MetronId", issue_data.get("MetronId"))

    # Notes - use provided Notes if available (e.g., from ComicVine), otherwise generate GCD notes
    if issue_data.get("Notes"):
        add("Notes", issue_data.get("Notes"))
    else:
        # Default to GCD format for backward compatibility
        notes = f"Metadata from Grand Comic Database (GCD). Issue ID: {issue_data.get('id', 'Unknown')} — retrieved {datetime.now():%Y-%m-%d}."
        add("Notes", notes)

    # Pretty-print and serialize as UTF-8 BYTES (not a Python str)
    ET.indent(root)  # Python 3.9+
    tree = ET.ElementTree(root)
    buf = io.BytesIO()
    tree.write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue()  # BYTES


def add_comicinfo_to_cbz(file_path, comicinfo_xml_bytes):
    """
    Writes ComicInfo.xml at the ROOT of the CBZ.
    - Removes any existing ComicInfo.xml (case-insensitive)
    - Uses UTF-8 bytes for content
    - Rebuilds the entire ZIP by extracting and recompressing (matches single_file.py approach)
    - Handles RAR files incorrectly named as CBZ
    """
    from cbz_ops.single_file import convert_single_rar_file

    # Safety: ensure bytes
    if isinstance(comicinfo_xml_bytes, str):
        comicinfo_xml_bytes = comicinfo_xml_bytes.encode("utf-8")

    # Create temp directory and file in the same directory as the source file
    file_dir = os.path.dirname(file_path) or '.'
    base_name = os.path.splitext(os.path.basename(file_path))[0]

    # Create temporary extraction directory
    temp_extract_dir = os.path.join(file_dir, f".tmp_extract_{base_name}_{os.getpid()}")
    temp_zip_path = os.path.join(file_dir, f".tmp_{base_name}_{os.getpid()}.cbz")

    try:
        # Step 1: Extract all files to temporary directory
        os.makedirs(temp_extract_dir, exist_ok=True)
        corrupted_files = []

        with zipfile.ZipFile(file_path, 'r') as src:
            for filename in src.namelist():
                # Skip any existing ComicInfo.xml
                if os.path.basename(filename).lower() == "comicinfo.xml":
                    continue
                try:
                    src.extract(filename, temp_extract_dir)
                except zipfile.BadZipFile as crc_error:
                    # Handle corrupted files with bad CRC
                    app_logger.warning(f"Corrupted file in archive (bad CRC): {filename} - attempting raw copy")
                    corrupted_files.append(filename)
                    try:
                        # Try to copy the file data without CRC verification
                        # Get the ZipInfo object
                        info = src.getinfo(filename)
                        # Create the target path
                        target_path = os.path.join(temp_extract_dir, filename)
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        # Read raw data (may be corrupted but we'll preserve what we can)
                        with src.open(filename) as zf:
                            # Read in chunks to handle large files
                            with open(target_path, 'wb') as out:
                                while True:
                                    try:
                                        chunk = zf.read(8192)
                                        if not chunk:
                                            break
                                        out.write(chunk)
                                    except zipfile.BadZipFile:
                                        # Write what we have and stop
                                        app_logger.warning(f"Partial extraction for corrupted file: {filename}")
                                        break
                    except Exception as copy_error:
                        app_logger.error(f"Failed to copy corrupted file {filename}: {copy_error}")
                        # Skip this file entirely
                        continue

        if corrupted_files:
            app_logger.warning(f"Archive had {len(corrupted_files)} corrupted file(s), processed with best effort")

        # Step 2: Write ComicInfo.xml to temp directory
        comicinfo_path = os.path.join(temp_extract_dir, "ComicInfo.xml")
        with open(comicinfo_path, 'wb') as f:
            f.write(comicinfo_xml_bytes)

        # Step 3: Recompress everything into new CBZ (sorted for consistency)
        with zipfile.ZipFile(temp_zip_path, 'w', zipfile.ZIP_DEFLATED) as dst:
            # Get all files and sort them
            all_files = []
            for root_dir, dirs, files in os.walk(temp_extract_dir):
                for file in files:
                    file_path_full = os.path.join(root_dir, file)
                    arcname = os.path.relpath(file_path_full, temp_extract_dir)
                    all_files.append((file_path_full, arcname))

            # Sort by arcname for consistent ordering
            all_files.sort(key=lambda x: x[1])

            # Write all files
            for file_path_full, arcname in all_files:
                dst.write(file_path_full, arcname)

        # Step 4: Replace original file
        os.replace(temp_zip_path, file_path)

    except zipfile.BadZipFile as e:
        # Handle the case where a .cbz file is actually a RAR file
        if "File is not a zip file" in str(e) or "BadZipFile" in str(e):
            app_logger.warning(f"Detected that {os.path.basename(file_path)} is not a valid ZIP file. Attempting to convert from RAR...")

            # Clean up any partial extraction
            if os.path.exists(temp_extract_dir):
                shutil.rmtree(temp_extract_dir, ignore_errors=True)
            if os.path.exists(temp_zip_path):
                try:
                    os.unlink(temp_zip_path)
                except:
                    pass

            # Rename to .rar for conversion
            rar_file = os.path.join(file_dir, base_name + ".rar")
            shutil.move(file_path, rar_file)

            # Convert RAR to CBZ
            app_logger.info(f"Converting {base_name}.rar to CBZ format...")
            temp_conversion_dir = os.path.join(file_dir, f"temp_{base_name}")
            success = convert_single_rar_file(rar_file, file_path, temp_conversion_dir)

            if success:
                # Delete the RAR file
                if os.path.exists(rar_file):
                    os.remove(rar_file)
                # Clean up temp directory
                if os.path.exists(temp_conversion_dir):
                    shutil.rmtree(temp_conversion_dir, ignore_errors=True)

                app_logger.info(f"Successfully converted RAR to CBZ. Now adding ComicInfo.xml...")

                # Now recursively call this function to add ComicInfo.xml to the newly converted CBZ
                add_comicinfo_to_cbz(file_path, comicinfo_xml_bytes)
            else:
                app_logger.error(f"Failed to convert {base_name}.rar to CBZ")
                # Move the RAR file back to original CBZ name
                if os.path.exists(rar_file):
                    shutil.move(rar_file, file_path)
                raise Exception(f"File is actually a RAR archive and conversion failed")
        else:
            raise

    finally:
        # Clean up temp directory
        if os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir, ignore_errors=True)
        # Clean up temp zip if it still exists
        if os.path.exists(temp_zip_path):
            try:
                os.unlink(temp_zip_path)
            except:
                pass


# =============================================================================
# CBZ Metadata
# =============================================================================

@metadata_bp.route('/cbz-metadata', methods=['GET'])
def cbz_metadata():
    """Extract metadata from a CBZ file"""
    file_path = request.args.get('path')
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "Invalid file path"}), 400

    if not file_path.lower().endswith(('.cbz', '.zip')):
        return jsonify({"error": "File is not a CBZ"}), 400

    try:
        from comicinfo import read_comicinfo_xml

        metadata = {
            "file_size": os.path.getsize(file_path),
            "total_files": 0,
            "image_files": 0,
            "comicinfo": None,
            "file_list": []
        }

        # Open the CBZ file
        with zipfile.ZipFile(file_path, 'r') as zf:
            file_list = zf.namelist()
            metadata["total_files"] = len(file_list)

            # Count image files
            image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
            image_files = []

            for file_name in file_list:
                ext = os.path.splitext(file_name.lower())[1]
                if ext in image_extensions:
                    image_files.append(file_name)

            metadata["image_files"] = len(image_files)

            # Look for ComicInfo.xml
            comicinfo_files = [f for f in file_list if f.lower().endswith('comicinfo.xml')]

            if comicinfo_files:
                try:
                    with zf.open(comicinfo_files[0]) as xml_file:
                        xml_data = xml_file.read()
                        app_logger.info(f"Found ComicInfo.xml in {file_path}, size: {len(xml_data)} bytes")
                        comicinfo = read_comicinfo_xml(xml_data)
                        if comicinfo:
                            app_logger.info(f"Successfully parsed ComicInfo.xml with {len(comicinfo)} fields")
                            metadata["comicinfo"] = comicinfo
                        else:
                            app_logger.warning(f"ComicInfo.xml parsed but returned empty data")
                except Exception as e:
                    app_logger.warning(f"Error reading ComicInfo.xml: {e}")
            else:
                app_logger.info(f"No ComicInfo.xml found in {file_path}")

            # Get first few files for preview
            metadata["file_list"] = sorted(file_list)[:10]  # First 10 files

        return jsonify(metadata)

    except Exception as e:
        app_logger.error(f"Error reading CBZ metadata {file_path}: {e}")
        return jsonify({"error": str(e)}), 500


@metadata_bp.route('/cbz-clear-comicinfo', methods=['POST'])
def cbz_clear_comicinfo():
    """Delete ComicInfo.xml from a CBZ file"""
    data = request.get_json()
    file_path = data.get('path')

    if not file_path or not os.path.exists(file_path):
        return jsonify({"success": False, "error": "Invalid file path"}), 400

    if not file_path.lower().endswith('.cbz'):
        return jsonify({"success": False, "error": "File is not a CBZ"}), 400

    try:
        # Create a temporary file for the new CBZ
        temp_zip_path = file_path + ".tmpzip"
        comicinfo_found = False

        # Open the original CBZ and create a new one without ComicInfo.xml
        with zipfile.ZipFile(file_path, 'r') as old_zip, \
             zipfile.ZipFile(temp_zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as new_zip:

            for item in old_zip.infolist():
                if item.filename.lower() == "comicinfo.xml":
                    comicinfo_found = True
                    app_logger.info(f"Removing ComicInfo.xml from {file_path}")
                    # Skip this file (don't write it to new zip)
                    continue
                else:
                    # Copy all other files as-is
                    new_zip.writestr(item, old_zip.read(item.filename))

        if not comicinfo_found:
            # Clean up temp file if ComicInfo.xml wasn't found
            os.remove(temp_zip_path)
            return jsonify({"success": False, "error": "ComicInfo.xml not found in CBZ"}), 404

        # Replace the original CBZ with the updated one
        os.replace(temp_zip_path, file_path)

        app_logger.info(f"Successfully removed ComicInfo.xml from {file_path}")
        return jsonify({"success": True})

    except Exception as e:
        app_logger.error(f"Error removing ComicInfo.xml from {file_path}: {e}")
        # Clean up temp file if it exists
        if os.path.exists(file_path + ".tmpzip"):
            os.remove(file_path + ".tmpzip")
        return jsonify({"success": False, "error": str(e)}), 500


# =============================================================================
# Save CVInfo
# =============================================================================

@metadata_bp.route('/api/save-cvinfo', methods=['POST'])
def save_cvinfo():
    """Save a cvinfo file in the specified directory."""
    from app import TARGET_DIR

    data = request.get_json()
    directory = data.get('directory')
    content = data.get('content') or data.get('url')  # Support both content and legacy url

    if not directory or not content:
        return jsonify({"error": "Missing directory or content parameter"}), 400

    # Security: Ensure the directory path is within allowed directories
    normalized_path = os.path.normpath(directory)
    if not (is_valid_library_path(normalized_path) or
            normalized_path.startswith(os.path.normpath(TARGET_DIR))):
        return jsonify({"error": "Access denied"}), 403

    if not os.path.exists(directory) or not os.path.isdir(directory):
        return jsonify({"error": "Directory not found"}), 404

    try:
        cvinfo_path = os.path.join(directory, 'cvinfo')
        with open(cvinfo_path, 'w', encoding='utf-8') as f:
            f.write(content.strip())

        app_logger.info(f"Saved cvinfo to {cvinfo_path}")
        return jsonify({"success": True, "path": cvinfo_path})
    except Exception as e:
        app_logger.error(f"Error saving cvinfo to {directory}: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Provider Management API
# =============================================================================

@metadata_bp.route('/api/providers', methods=['GET'])
def list_providers():
    """List all available metadata providers with their configuration."""
    try:
        from models.providers import get_available_providers
        from database import get_all_provider_credentials_status, get_provider_credentials_masked

        providers = get_available_providers()
        credentials_status = {s['provider_type']: s for s in get_all_provider_credentials_status()}

        # Enrich providers with credential status and masked credentials
        for p in providers:
            status = credentials_status.get(p['type'], {})
            p['has_credentials'] = p['type'] in credentials_status
            p['is_valid'] = status.get('is_valid', 0) == 1
            p['last_tested'] = status.get('last_tested')
            # Include masked credentials if available
            if p['has_credentials']:
                p['credentials_masked'] = get_provider_credentials_masked(p['type'])
            else:
                p['credentials_masked'] = None

        return jsonify({"success": True, "providers": providers})
    except Exception as e:
        app_logger.error(f"Error listing providers: {e}")
        return jsonify({"error": str(e)}), 500


@metadata_bp.route('/api/providers/<provider_type>/credentials', methods=['GET'])
def get_provider_creds(provider_type):
    """Get masked credentials for a provider (safe for display)."""
    try:
        from database import get_provider_credentials_masked

        masked = get_provider_credentials_masked(provider_type)
        if not masked:
            return jsonify({"success": True, "has_credentials": False, "credentials": {}})

        return jsonify({
            "success": True,
            "has_credentials": True,
            "credentials": masked
        })
    except Exception as e:
        app_logger.error(f"Error getting provider credentials: {e}")
        return jsonify({"error": str(e)}), 500


@metadata_bp.route('/api/providers/<provider_type>/credentials', methods=['POST'])
def save_provider_creds(provider_type):
    """Save credentials for a provider."""
    try:
        from database import save_provider_credentials
        from models.providers import ProviderType

        # Validate provider type
        try:
            ProviderType(provider_type)
        except ValueError:
            return jsonify({"error": f"Unknown provider type: {provider_type}"}), 400

        data = request.get_json()
        if not data:
            return jsonify({"error": "No credentials provided"}), 400

        # Save credentials
        success = save_provider_credentials(provider_type, data)
        if success:
            # Refresh Flask app.config with new DB credentials
            try:
                from config import load_flask_config
                load_flask_config(current_app)
            except Exception:
                pass
            return jsonify({"success": True, "message": f"Credentials saved for {provider_type}"})
        else:
            return jsonify({"error": "Failed to save credentials"}), 500
    except Exception as e:
        app_logger.error(f"Error saving provider credentials: {e}")
        return jsonify({"error": str(e)}), 500


@metadata_bp.route('/api/providers/<provider_type>/credentials', methods=['DELETE'])
def delete_provider_creds(provider_type):
    """Delete credentials for a provider."""
    try:
        from database import delete_provider_credentials

        success = delete_provider_credentials(provider_type)
        if success:
            return jsonify({"success": True, "message": f"Credentials deleted for {provider_type}"})
        else:
            return jsonify({"error": "Failed to delete credentials"}), 500
    except Exception as e:
        app_logger.error(f"Error deleting provider credentials: {e}")
        return jsonify({"error": str(e)}), 500


@metadata_bp.route('/api/providers/<provider_type>/test', methods=['POST'])
def test_provider_connection(provider_type):
    """Test connection to a provider using saved credentials."""
    try:
        from database import get_provider_credentials, update_provider_validity, register_provider_configured
        from models.providers import get_provider_by_name, get_provider_class, ProviderCredentials

        # Validate provider type
        from models.providers import ProviderType
        try:
            ptype = ProviderType(provider_type)
        except ValueError:
            return jsonify({"error": f"Unknown provider type: {provider_type}"}), 400

        # Check if provider requires authentication
        provider_class = get_provider_class(ptype)
        requires_auth = provider_class.requires_auth if provider_class else True

        # Get saved credentials
        creds_dict = get_provider_credentials(provider_type)
        if not creds_dict and requires_auth:
            return jsonify({"success": False, "error": "No credentials configured"}), 400

        # Create provider instance with credentials (or None for public APIs)
        credentials = ProviderCredentials.from_dict(creds_dict) if creds_dict else None
        provider = get_provider_by_name(provider_type, credentials)

        # Test connection
        is_valid = provider.test_connection()

        # Update validity in database
        # For auth-free providers, register them as configured when test succeeds
        if not requires_auth:
            register_provider_configured(provider_type, is_valid)
        else:
            update_provider_validity(provider_type, is_valid)

        if is_valid:
            return jsonify({"success": True, "valid": True, "message": f"Connection to {provider_type} successful"})
        else:
            return jsonify({"success": True, "valid": False, "error": f"Connection to {provider_type} failed"})
    except Exception as e:
        app_logger.error(f"Error testing provider connection: {e}")
        return jsonify({"error": str(e)}), 500


@metadata_bp.route('/api/libraries/<int:library_id>/providers', methods=['GET'])
def get_library_provider_config(library_id):
    """Get provider configuration for a library."""
    try:
        from database import get_library_providers, get_library_by_id

        # Verify library exists
        library = get_library_by_id(library_id)
        if not library:
            return jsonify({"error": "Library not found"}), 404

        providers = get_library_providers(library_id)

        return jsonify({
            "success": True,
            "library_id": library_id,
            "library_name": library.get('name'),
            "providers": providers
        })
    except Exception as e:
        app_logger.error(f"Error getting library providers: {e}")
        return jsonify({"error": str(e)}), 500


@metadata_bp.route('/api/libraries/<int:library_id>/providers', methods=['PUT'])
def set_library_provider_config(library_id):
    """Set provider configuration for a library."""
    try:
        from database import set_library_providers, get_library_by_id

        # Verify library exists
        library = get_library_by_id(library_id)
        if not library:
            return jsonify({"error": "Library not found"}), 404

        data = request.get_json()
        if not data or 'providers' not in data:
            return jsonify({"error": "Missing providers list"}), 400

        providers = data['providers']

        # Validate provider types
        from models.providers import ProviderType
        for p in providers:
            try:
                ProviderType(p.get('provider_type', ''))
            except ValueError:
                return jsonify({"error": f"Unknown provider type: {p.get('provider_type')}"}), 400

        success = set_library_providers(library_id, providers)
        if success:
            return jsonify({"success": True, "message": f"Provider configuration saved for library {library_id}"})
        else:
            return jsonify({"error": "Failed to save provider configuration"}), 500
    except Exception as e:
        app_logger.error(f"Error setting library providers: {e}")
        return jsonify({"error": str(e)}), 500


@metadata_bp.route('/api/libraries/<int:library_id>/providers/<provider_type>', methods=['POST'])
def add_library_provider(library_id, provider_type):
    """Add a provider to a library."""
    try:
        from database import add_library_provider as db_add_library_provider, get_library_by_id

        # Verify library exists
        library = get_library_by_id(library_id)
        if not library:
            return jsonify({"error": "Library not found"}), 404

        # Validate provider type
        from models.providers import ProviderType
        try:
            ProviderType(provider_type)
        except ValueError:
            return jsonify({"error": f"Unknown provider type: {provider_type}"}), 400

        data = request.get_json() or {}
        priority = data.get('priority', 0)
        enabled = data.get('enabled', True)

        success = db_add_library_provider(library_id, provider_type, priority, enabled)
        if success:
            return jsonify({"success": True, "message": f"Added {provider_type} to library {library_id}"})
        else:
            return jsonify({"error": "Failed to add provider to library"}), 500
    except Exception as e:
        app_logger.error(f"Error adding library provider: {e}")
        return jsonify({"error": str(e)}), 500


@metadata_bp.route('/api/libraries/<int:library_id>/providers/<provider_type>', methods=['DELETE'])
def remove_library_provider(library_id, provider_type):
    """Remove a provider from a library."""
    try:
        from database import remove_library_provider as db_remove_library_provider, get_library_by_id

        # Verify library exists
        library = get_library_by_id(library_id)
        if not library:
            return jsonify({"error": "Library not found"}), 404

        success = db_remove_library_provider(library_id, provider_type)
        if success:
            return jsonify({"success": True, "message": f"Removed {provider_type} from library {library_id}"})
        else:
            return jsonify({"error": "Failed to remove provider from library"}), 500
    except Exception as e:
        app_logger.error(f"Error removing library provider: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Update XML
# =============================================================================

@metadata_bp.route('/api/update-xml', methods=['POST'])
def update_xml():
    """Update a field in ComicInfo.xml for all CBZ files in a directory."""
    from models.update_xml import update_field_in_cbz_files
    from app import TARGET_DIR

    try:
        data = request.get_json()
        directory = data.get('directory')
        field = data.get('field')
        value = data.get('value')

        if not directory or not field or not value:
            return jsonify({"error": "Missing required parameters"}), 400

        # Security check - ensure path is within allowed directories
        normalized_path = os.path.normpath(directory)
        if not (is_valid_library_path(normalized_path) or
                normalized_path.startswith(os.path.normpath(TARGET_DIR))):
            return jsonify({"error": "Access denied"}), 403

        if not os.path.exists(directory) or not os.path.isdir(directory):
            return jsonify({"error": "Directory not found"}), 404

        result = update_field_in_cbz_files(directory, field, value)
        return jsonify(result)

    except Exception as e:
        app_logger.error(f"Error in update_xml: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Validate GCD Issue
# =============================================================================

@metadata_bp.route('/validate-gcd-issue', methods=['POST'])
def validate_gcd_issue():
    """Validate that a specific issue number exists in the given series"""
    data = request.get_json()
    series_id = data.get('series_id')
    issue_number = data.get('issue_number')

    app_logger.debug(f"DEBUG: validate_gcd_issue called - series_id={series_id}, issue={issue_number}")

    # Note: issue_number can be 0, so check for None explicitly
    if series_id is None or issue_number is None:
        app_logger.error(f"ERROR: Missing parameters in validate_gcd_issue - series_id={series_id}, issue_number={issue_number}")
        return jsonify({
            "success": False,
            "error": "Missing required parameters"
        }), 400

    result = gcd.validate_issue(series_id, str(issue_number))

    # Transform response to match expected format
    if result.get('success') and result.get('valid'):
        issue_data = result.get('issue', {})
        return jsonify({
            "success": True,
            "issue_id": issue_data.get('id'),
            "issue_number": issue_data.get('number'),
            "issue_title": issue_data.get('title')
        })
    elif result.get('success') and not result.get('valid'):
        return jsonify({
            "success": False,
            "error": f"Issue #{issue_number} not found in series"
        })
    else:
        return jsonify({
            "success": False,
            "error": result.get('error', 'Validation error')
        }), 500


# =============================================================================
# Batch Metadata
# =============================================================================

@metadata_bp.route('/api/batch-metadata', methods=['POST'])
def batch_metadata():
    """
    Batch fetch metadata for all comics in a folder.
    Returns Server-Sent Events (SSE) for real-time progress updates.

    Process order:
    1. Check for cvinfo in folder
    2. If no cvinfo, create via ComicVine search (using folder name as series)
    3. Add Metron series ID to cvinfo if not present
    4. Read/fetch start_year for Volume field from cvinfo
    5. For each CBZ/CBR without ComicInfo.xml:
       - Try Metron first, then ComicVine, then GCD
    """
    from comicinfo import read_comicinfo_from_zip
    from app import TARGET_DIR

    try:
        from database import get_library_providers

        data = request.get_json()
        directory = data.get('directory')
        selected_volume_id = data.get('volume_id')  # Optional: pre-selected ComicVine volume ID
        library_id = data.get('library_id')  # Optional: library ID for provider lookup

        if not directory:
            return jsonify({"error": "Missing directory parameter"}), 400

        # Security: Ensure the directory path is within allowed directories
        normalized_path = os.path.normpath(directory)
        if not (is_valid_library_path(normalized_path) or
                normalized_path.startswith(os.path.normpath(TARGET_DIR))):
            return jsonify({"error": "Access denied"}), 403

        if not os.path.exists(directory) or not os.path.isdir(directory):
            return jsonify({"error": "Directory not found"}), 404

        # Always load API credentials (needed for provider initialization)
        comicvine_api_key = current_app.config.get('COMICVINE_API_KEY', '')
        metron_username = current_app.config.get('METRON_USERNAME', '')
        metron_password = current_app.config.get('METRON_PASSWORD', '')

        # Determine provider availability
        # If library_id is provided, use library-specific providers
        # Otherwise fall back to global configuration
        if library_id:
            library_providers = get_library_providers(library_id)
            enabled_providers = [p['provider_type'] for p in library_providers if p.get('enabled', True)]
            comicvine_available = 'comicvine' in enabled_providers
            metron_available = 'metron' in enabled_providers
            gcd_available = 'gcd' in enabled_providers
            anilist_available = 'anilist' in enabled_providers
            bedetheque_available = 'bedetheque' in enabled_providers
            mangadex_available = 'mangadex' in enabled_providers
            app_logger.info(f"Library {library_id} providers: {enabled_providers}")
        else:
            # Fallback to global API credential availability checks
            comicvine_available = bool(comicvine_api_key and comicvine_api_key.strip())
            metron_available = bool(metron_password and metron_password.strip())
            gcd_available = gcd.is_mysql_available() and gcd.check_mysql_status().get('gcd_mysql_available', False)
            anilist_available = False
            bedetheque_available = False
            mangadex_available = False

        app_logger.info(f"Batch metadata: CV={comicvine_available}, Metron={metron_available}, GCD={gcd_available}, AniList={anilist_available}, MangaDex={mangadex_available}")

        # Initialize Metron API early (needed for cvinfo creation)
        metron_api = None
        if metron_available:
            metron_api = metron.get_api(metron_username, metron_password)

        # Step 1: Get list of comic files (needed for year extraction)
        comic_files = []
        for item in os.listdir(directory):
            item_path = os.path.join(directory, item)
            if os.path.isfile(item_path) and item.lower().endswith(('.cbz', '.cbr')):
                comic_files.append(item_path)

        app_logger.info(f"Found {len(comic_files)} comic files to process")

        # Helper function to extract year from filename or folder name
        def extract_year_from_name(name: str):
            """Extract year from name in (YYYY) or vYYYY format."""
            # Try (YYYY) format
            match = re.search(r'\((\d{4})\)', name)
            if match:
                return int(match.group(1))
            # Try vYYYY format
            match = re.search(r'v(\d{4})', name)
            if match:
                return int(match.group(1))
            return None

        # Extract year - try first filename, then folder name
        extracted_year = None
        if comic_files:
            extracted_year = extract_year_from_name(os.path.basename(comic_files[0]))
        if not extracted_year:
            extracted_year = extract_year_from_name(os.path.basename(directory))

        app_logger.info(f"Extracted year from filename/folder: {extracted_year}")

        # Step 2: Check for cvinfo
        cvinfo_path = os.path.join(directory, 'cvinfo')
        cv_volume_id = None
        series_id = None
        cvinfo_created = False
        metron_id_added = False
        cvinfo_start_year = None
        cv_id_missing_warning = False  # Track if CV ID is missing from Metron

        if not os.path.exists(cvinfo_path):
            # Extract series name from folder first
            series_name = os.path.basename(directory)
            series_name = re.sub(r'\s*\(\d{4}\).*$', '', series_name)  # Remove (1994) and everything after
            series_name = re.sub(r'\s*v\d+.*$', '', series_name)  # Remove v1, v2 etc
            series_name = re.sub(r'\s*-\s*complete.*$', '', series_name, flags=re.IGNORECASE)
            series_name = series_name.strip()

            # If folder name didn't yield a series name, try extracting from first filename
            if not series_name and comic_files:
                filename = os.path.basename(comic_files[0])
                # Remove extension
                series_name = os.path.splitext(filename)[0]
                # Remove year in parentheses: "(2005)"
                series_name = re.sub(r'\s*\(\d{4}\)', '', series_name)
                # Remove issue number patterns: "003", "#3", "Issue 3"
                series_name = re.sub(r'\s*#?\d{1,4}\s*$', '', series_name)  # Trailing numbers
                series_name = re.sub(r'\s*-\s*\d{1,4}\s*$', '', series_name)  # "- 003"
                series_name = re.sub(r'\s+Issue\s+\d+', '', series_name, flags=re.IGNORECASE)
                series_name = series_name.strip()
                app_logger.info(f"Extracted series name from filename: '{series_name}'")

            app_logger.info(f"No cvinfo found, searching for series: '{series_name}' (year: {extracted_year})")

            # Try Metron first if available
            if metron_api:
                app_logger.info("Trying Metron first for cvinfo creation...")
                try:
                    metron_series = metron.search_series_by_name(metron_api, series_name, extracted_year)
                    if metron_series:
                        # Create cvinfo with all Metron data
                        metron.create_cvinfo_file(
                            cvinfo_path,
                            cv_id=metron_series.get('cv_id'),
                            series_id=metron_series['id'],
                            publisher_name=metron_series.get('publisher_name'),
                            start_year=metron_series.get('year_began')
                        )
                        cv_volume_id = metron_series.get('cv_id')
                        series_id = metron_series['id']
                        cvinfo_start_year = metron_series.get('year_began')
                        cvinfo_created = True
                        metron_id_added = True
                        app_logger.info(f"Created cvinfo via Metron: series_id={series_id}, cv_id={cv_volume_id}")
                except Exception as e:
                    if metron.is_connection_error(e):
                        app_logger.warning(f"Metron unavailable during series search: {e}")
                    else:
                        app_logger.error(f"Error searching Metron for series: {e}")

            # Fallback to ComicVine if Metron didn't find it
            if not cvinfo_created and comicvine_available:
                app_logger.info("Trying ComicVine for cvinfo creation...")
                try:
                    # If user already selected a volume, use it directly
                    if selected_volume_id:
                        cv_volume_id = selected_volume_id
                        app_logger.info(f"Using pre-selected volume ID: {cv_volume_id}")
                    else:
                        # Search for volumes
                        volumes = comicvine.search_volumes(comicvine_api_key, series_name, extracted_year)
                        if volumes:
                            # If multiple volumes found, return them for user selection
                            if len(volumes) > 1:
                                app_logger.info(f"Found {len(volumes)} volumes - returning for user selection")
                                return jsonify({
                                    "requires_selection": True,
                                    "directory": directory,
                                    "parsed_filename": {
                                        "series_name": series_name,
                                        "issue_number": str(len(comic_files)),
                                        "year": extracted_year
                                    },
                                    "possible_matches": volumes
                                })
                            cv_volume_id = volumes[0]['id']

                    # Create cvinfo with the selected/found volume
                    if cv_volume_id:
                        url = f"https://comicvine.gamespot.com/volume/4050-{cv_volume_id}/"
                        with open(cvinfo_path, 'w', encoding='utf-8') as f:
                            f.write(url)
                        cvinfo_created = True
                        app_logger.info(f"Created cvinfo with ComicVine volume ID: {cv_volume_id}")

                        # Fetch and save volume details
                        volume_details = comicvine.get_volume_details(comicvine_api_key, cv_volume_id)
                        if volume_details:
                            comicvine.write_cvinfo_fields(cvinfo_path,
                                volume_details.get('publisher_name'),
                                volume_details.get('start_year'))
                            cvinfo_start_year = volume_details.get('start_year')
                except Exception as e:
                    app_logger.error(f"Error searching ComicVine: {e}")
        else:
            # Parse existing cvinfo
            cv_volume_id = comicvine.parse_cvinfo_volume_id(cvinfo_path)
            series_id = metron.parse_cvinfo_for_metron_id(cvinfo_path)
            app_logger.info(f"Found existing cvinfo with volume ID: {cv_volume_id}, series_id: {series_id}")

            # If cvinfo has series_id but no CV URL, look up cv_id from Metron and add it
            if not cv_volume_id and series_id and metron_api:
                cv_id_from_metron = metron.get_series_cv_id(metron_api, series_id)
                if cv_id_from_metron:
                    metron.add_cvinfo_url(cvinfo_path, cv_id_from_metron)
                    cv_volume_id = cv_id_from_metron
                    app_logger.info(f"Added CV URL to existing cvinfo: cv_id={cv_id_from_metron}")
                else:
                    # Metron doesn't have a CV ID for this series
                    # cvinfo already exists with series_id, just set warning flag
                    cv_id_missing_warning = True
                    app_logger.warning(f"Series in Metron but no ComicVine ID available for series_id={series_id}")

        # Step 3: Add Metron series ID and details if not present in existing cvinfo
        if metron_api and os.path.exists(cvinfo_path) and not series_id:
            cv_id = metron.parse_cvinfo_for_comicvine_id(cvinfo_path)
            if cv_id:
                series_id = metron.get_series_id_by_comicvine_id(metron_api, cv_id)
                if series_id:
                    # Get full series details from Metron
                    series_details = metron.get_series_details(metron_api, series_id)
                    if series_details:
                        # Update cvinfo with series_id
                        metron.update_cvinfo_with_metron_id(cvinfo_path, series_id)
                        # Also add publisher_name and start_year if available
                        if series_details.get('publisher_name') or series_details.get('year_began'):
                            metron.write_cvinfo_fields(cvinfo_path,
                                series_details.get('publisher_name'),
                                series_details.get('year_began'))
                            cvinfo_start_year = series_details.get('year_began')
                        metron_id_added = True
                        app_logger.info(f"Added Metron data to cvinfo: series_id={series_id}, publisher={series_details.get('publisher_name')}, year={series_details.get('year_began')}")

        # Step 4: Read start_year from cvinfo for ComicVine calls (for Volume field)
        if not cvinfo_start_year and os.path.exists(cvinfo_path):
            cvinfo_fields = comicvine.read_cvinfo_fields(cvinfo_path)
            cvinfo_start_year = cvinfo_fields.get('start_year')
            # If not in cvinfo but we have a volume_id, fetch and save
            if not cvinfo_start_year and cv_volume_id and comicvine_available:
                volume_details = comicvine.get_volume_details(comicvine_api_key, cv_volume_id)
                if volume_details.get('start_year') or volume_details.get('publisher_name'):
                    cvinfo_start_year = volume_details.get('start_year')
                    comicvine.write_cvinfo_fields(cvinfo_path, volume_details.get('publisher_name'), cvinfo_start_year)

        # Store year for GCD lookups
        gcd_year = extracted_year or cvinfo_start_year

        def generate():
            """Generator for SSE streaming."""
            result = {
                'cvinfo_created': cvinfo_created,
                'metron_id_added': metron_id_added,
                'cv_id_missing_warning': cv_id_missing_warning,
                'processed': 0,
                'skipped': 0,
                'errors': 0,
                'details': []
            }

            total_files = len(comic_files)

            # Emit initial progress
            yield f"data: {json.dumps({'type': 'progress', 'current': 0, 'total': total_files, 'file': 'Starting...'})}\n\n"

            # Step 4: Process each comic file
            for i, file_path in enumerate(comic_files):
                filename = os.path.basename(file_path)

                # Emit progress event
                yield f"data: {json.dumps({'type': 'progress', 'current': i + 1, 'total': total_files, 'file': filename})}\n\n"

                try:
                    # Check if already has ComicInfo.xml
                    if file_path.lower().endswith('.cbz'):
                        existing = read_comicinfo_from_zip(file_path)
                        existing_notes = existing.get('Notes', '').strip() if existing else ''

                        # Skip if has metadata, unless it's just Amazon scraped data
                        if existing_notes and 'Scraped metadata from Amazon' not in existing_notes:
                            app_logger.debug(f"Skipping {filename} - already has metadata")
                            result['skipped'] += 1
                            result['details'].append({'file': filename, 'status': 'skipped', 'reason': 'has metadata'})
                            continue
                    elif file_path.lower().endswith('.cbr'):
                        # Skip CBR files - we can't check or modify them without conversion
                        app_logger.debug(f"Skipping {filename} - CBR format not supported for metadata")
                        result['skipped'] += 1
                        result['details'].append({'file': filename, 'status': 'skipped', 'reason': 'CBR format'})
                        continue

                    # Extract issue/volume number from filename
                    issue_number = comicvine.extract_issue_number(filename)

                    # For manga, also try to extract volume number (v01, v02, etc.)
                    volume_number = None
                    volume_match = re.search(r'\bv(\d+)', filename, re.IGNORECASE)
                    if volume_match:
                        volume_number = volume_match.group(1).lstrip('0') or '1'

                    # Use volume number for manga providers (AniList, MangaDex), issue number for comics
                    if (anilist_available or mangadex_available) and volume_number:
                        issue_number = volume_number
                        app_logger.info(f"Using volume number {volume_number} for manga: {filename}")
                    elif not issue_number:
                        app_logger.warning(f"Could not extract issue number from {filename}")
                        result['errors'] += 1
                        result['details'].append({'file': filename, 'status': 'error', 'reason': 'no issue number'})
                        continue

                    app_logger.info(f"Processing {filename} (issue/vol #{issue_number})")

                    # Try sources based on volume year
                    metadata = None
                    source = None

                    # Helper function for GCD lookup
                    def try_gcd():
                        nonlocal metadata, source
                        if not gcd_available:
                            return False
                        try:
                            # Get series name from directory
                            gcd_series_name = os.path.basename(directory)
                            # Clean up series name
                            gcd_series_name = re.sub(r'\s*\(\d{4}\).*$', '', gcd_series_name)
                            gcd_series_name = re.sub(r'\s*v\d+.*$', '', gcd_series_name)

                            # Use gcd_year (from filename/folder or cvinfo)
                            gcd_series = gcd.search_series(gcd_series_name, gcd_year)
                            if gcd_series:
                                metadata = gcd.get_issue_metadata(gcd_series['id'], issue_number)
                                if metadata:
                                    source = 'GCD'
                                    app_logger.info(f"Found metadata from GCD for {filename}")
                                    return True
                        except Exception as e:
                            app_logger.warning(f"GCD lookup failed for {filename}: {e}")
                        return False

                    # Helper function for ComicVine lookup
                    def try_comicvine():
                        nonlocal metadata, source
                        if not (comicvine_available and cv_volume_id):
                            return False
                        try:
                            metadata = comicvine.get_metadata_by_volume_id(comicvine_api_key, cv_volume_id, issue_number, start_year=cvinfo_start_year)
                            if metadata:
                                source = 'ComicVine'
                                app_logger.info(f"Found metadata from ComicVine for {filename}")
                                return True
                        except Exception as e:
                            app_logger.warning(f"ComicVine lookup failed for {filename}: {e}")
                        return False

                    # Helper function for Metron lookup
                    def try_metron():
                        nonlocal metadata, source
                        if not (metron_available and metron_api and series_id):
                            return False
                        try:
                            issue_data = metron.get_issue_metadata(metron_api, series_id, issue_number)
                            if issue_data:
                                metadata = metron.map_to_comicinfo(issue_data)
                                source = 'Metron'
                                app_logger.info(f"Found metadata from Metron for {filename}")
                                return True
                        except Exception as e:
                            app_logger.warning(f"Metron lookup failed for {filename}: {e}")
                        return False

                    # Helper function for AniList lookup (manga)
                    def try_anilist():
                        nonlocal metadata, source
                        if not anilist_available:
                            return False
                        try:
                            from models.providers.anilist_provider import AniListProvider
                            anilist = AniListProvider()

                            # Get series name from directory
                            series_name = os.path.basename(directory)
                            series_name = re.sub(r'\s*\(\d{4}\).*$', '', series_name)
                            series_name = re.sub(r'\s*v\d+.*$', '', series_name)

                            # Search for the manga
                            results = anilist.search_series(series_name, gcd_year)
                            if results:
                                series = results[0]  # Take first/best match
                                metadata = anilist.get_issue_metadata(series.id, issue_number)
                                if metadata:
                                    source = 'AniList'
                                    app_logger.info(f"Found metadata from AniList for {filename}")
                                    return True
                        except Exception as e:
                            app_logger.warning(f"AniList lookup failed for {filename}: {e}")
                        return False

                    # Helper function for MangaDex lookup (manga)
                    def try_mangadex():
                        nonlocal metadata, source
                        if not mangadex_available:
                            return False
                        try:
                            from models.providers.mangadex_provider import MangaDexProvider
                            mangadex = MangaDexProvider()

                            # Get series name from directory
                            series_name = os.path.basename(directory)
                            series_name = re.sub(r'\s*\(\d{4}\).*$', '', series_name)
                            series_name = re.sub(r'\s*v\d+.*$', '', series_name)

                            # Search for the manga
                            results = mangadex.search_series(series_name, gcd_year)
                            if results:
                                series = results[0]  # Take first/best match
                                metadata = mangadex.get_issue_metadata(series.id, issue_number)
                                if metadata:
                                    source = 'MangaDex'
                                    app_logger.info(f"Found metadata from MangaDex for {filename}")
                                    return True
                        except Exception as e:
                            app_logger.warning(f"MangaDex lookup failed for {filename}: {e}")
                        return False

                    # Use providers in library-configured priority order
                    provider_try_fns = {
                        'metron': try_metron,
                        'comicvine': try_comicvine,
                        'gcd': try_gcd,
                        'anilist': try_anilist,
                        'mangadex': try_mangadex,
                    }

                    if library_id and library_providers:
                        # Use library-configured priority order
                        for provider_config in library_providers:
                            if provider_config.get('enabled', True):
                                try_fn = provider_try_fns.get(provider_config['provider_type'])
                                if try_fn and try_fn():
                                    break
                    else:
                        # Fallback for no library_id: try all available providers
                        for name, try_fn in provider_try_fns.items():
                            if try_fn():
                                break

                    if metadata:
                        # Generate and add ComicInfo.xml
                        xml_bytes = comicvine.generate_comicinfo_xml(metadata)
                        add_comicinfo_to_cbz(file_path, xml_bytes)
                        from database import set_has_comicinfo
                        set_has_comicinfo(file_path)
                        result['processed'] += 1
                        result['details'].append({'file': filename, 'status': 'success', 'source': source})
                        app_logger.info(f"Added metadata to {filename} from {source}")
                    else:
                        result['errors'] += 1
                        result['details'].append({'file': filename, 'status': 'error', 'reason': 'not found'})
                        app_logger.warning(f"No metadata found for {filename}")

                    # Rate limiting - wait between API calls
                    time.sleep(0.5)

                except Exception as e:
                    app_logger.error(f"Error processing {filename}: {e}")
                    result['errors'] += 1
                    result['details'].append({'file': filename, 'status': 'error', 'reason': str(e)})

            # Emit final complete event
            yield f"data: {json.dumps({'type': 'complete', 'result': result})}\n\n"

        return Response(stream_with_context(generate()), mimetype='text/event-stream')

    except Exception as e:
        if metron.is_connection_error(e):
            app_logger.warning(f"Metron unavailable during batch metadata: {e}")
            return jsonify({"error": "Metron is currently unavailable. Please try again later."}), 503
        app_logger.error(f"Error in batch_metadata: {e}")
        app_logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# GCD Metadata Search
# =============================================================================

@metadata_bp.route('/search-gcd-metadata', methods=['POST'])
def search_gcd_metadata():
    """Search GCD database for comic metadata and add to CBZ file"""
    try:

        app_logger.info(f"🔍 GCD search started")
        data = request.get_json()
        app_logger.info(f"GCD Request data: {data}")
        file_path = data.get('file_path')
        file_name = data.get('file_name')
        is_directory_search = data.get('is_directory_search', False)
        directory_path = data.get('directory_path')
        directory_name = data.get('directory_name')
        total_files = data.get('total_files', 1)
        parent_series_name = data.get('parent_series_name')  # For nested volume processing
        volume_year = data.get('volume_year')  # For volume year parsing
        app_logger.debug(f"DEBUG: file_path={file_path}, file_name={file_name}, is_directory_search={is_directory_search}")
        app_logger.debug(f"DEBUG: directory_path={directory_path}, directory_name={directory_name}")
        app_logger.debug(f"DEBUG: parent_series_name={parent_series_name}, volume_year={volume_year}")

        if not file_path or not file_name:
            return jsonify({
                "success": False,
                "error": "Missing file_path or file_name"
            }), 400

        # For directory search, prefer directory name parsing, fallback to file name
        if is_directory_search and directory_name:
            name_without_ext = directory_name
            app_logger.debug(f"DEBUG: Using directory name for parsing: {name_without_ext}")
        else:
            # Parse series name and issue from filename
            name_without_ext = file_name
            for ext in ('.cbz', '.cbr', '.zip'):
                name_without_ext = name_without_ext.replace(ext, '')

            app_logger.debug(f"DEBUG: Using file name for parsing: {name_without_ext}")

        # Try to parse series and issue from common formats
        series_name = None
        issue_number = None
        year = None
        issue_number_was_defaulted = False  # Track if we defaulted the issue number

        if is_directory_search:
            # Check if this is a volume directory (e.g., v2015) that needs parent series name
            volume_directory_match = re.match(r'^v(\d{4})$', name_without_ext, re.IGNORECASE)

            if volume_directory_match and parent_series_name:
                # Approach 2: Volume directory getting series name from parent
                series_name = parent_series_name
                year = int(volume_directory_match.group(1))
                app_logger.debug(f"DEBUG: Volume directory detected - using parent series '{series_name}' with year {year}")
            elif parent_series_name and volume_year:
                # Approach 1: Nested volume processing with explicit parent name and year
                series_name = parent_series_name
                year = int(volume_year)
                app_logger.debug(f"DEBUG: Nested volume processing - series='{series_name}', year={year}")
            else:
                # Standard directory processing
                directory_patterns = [
                    r'^(.+?)\s+\((\d{4})\)',  # "Series Name (2020)"
                    r'^(.+?)\s+(\d{4})',      # "Series Name 2020"
                    r'^(.+?)\s+v\d+\s+\((\d{4})\)', # "Series v1 (2020)"
                ]

                for pattern in directory_patterns:
                    match = re.match(pattern, name_without_ext, re.IGNORECASE)
                    if match:
                        series_name = match.group(1).strip()
                        year = int(match.group(2)) if len(match.groups()) >= 2 else None
                        app_logger.debug(f"DEBUG: Directory parsed - series_name={series_name}, year={year}")
                        break

                # If no year pattern matched, just use the whole directory name as series
                if not series_name:
                    series_name = name_without_ext.strip()
                    app_logger.debug(f"DEBUG: Directory fallback - series_name={series_name}")

            # For directory search, parse issue number from the first file name
            file_name_without_ext = file_name
            for ext in ('.cbz', '.cbr', '.zip'):
                file_name_without_ext = file_name_without_ext.replace(ext, '')
            app_logger.debug(f"DEBUG: Parsing issue number from first file: {file_name_without_ext}")

            # Try multiple patterns to extract issue number from the first file
            issue_patterns = [
                r'(?:^|\s)(\d{1,4})(?:\s*\(|\s*$|\s*\.)',     # Standard: "Series 123 (year)" or "Series 123.cbz"
                r'(?:^|\s)#(\d{1,4})(?:\s|$)',                 # Hash prefix: "Series #123"
                r'(?:issue\s*)(\d{1,4})',                      # Issue prefix: "Series Issue 123"
                r'(?:no\.?\s*)(\d{1,4})',                      # No. prefix: "Series No. 123"
                r'(?:vol\.\s*\d+\s+)(\d{1,4})',                # Volume and issue: "Series Vol. 1 123"
            ]

            for pattern in issue_patterns:
                match = re.search(pattern, file_name_without_ext, re.IGNORECASE)
                if match:
                    issue_number = int(match.group(1))  # Handles '0', '00', '000' -> 0
                    if issue_number == 0:
                        app_logger.debug(f"DEBUG: Extracted issue number {issue_number} (zero/variant issue) from filename using pattern: {pattern}")
                    else:
                        app_logger.debug(f"DEBUG: Extracted issue number {issue_number} from filename using pattern: {pattern}")
                    break

            if issue_number is None:
                issue_number = 1  # Ultimate fallback
                app_logger.debug(f"DEBUG: Could not parse issue number from filename, defaulting to 1")
        else:
            # Pattern matching for common comic filename formats
            patterns = [
                r'^(.+?)\s+(\d{3,4})\s+\((\d{4})\)',  # "Series 001 (2020)"
                r'^(.+?)\s+#?(\d{1,4})\s*\((\d{4})\)', # "Series #1 (2020)" or "Series 1 (2020)"
                r'^(.+?)\s+v\d+\s+(\d{1,4})\s*\((\d{4})\)', # "Series v1 001 (2020)"
                r'^(.+?)\s+(\d{1,4})\s+\(of\s+\d+\)\s+\((\d{4})\)', # "Series 05 (of 12) (2020)"
                r'^(.+?)\s+#?(\d{1,4})$',  # "Series 169" or "Series #169" (no year)
            ]

            for pattern in patterns:
                match = re.match(pattern, name_without_ext, re.IGNORECASE)
                if match:
                    series_name = match.group(1).strip()
                    issue_number = int(match.group(2))  # Handles '0', '00', '000' -> 0
                    year = int(match.group(3)) if len(match.groups()) >= 3 else None
                    if issue_number == 0:
                        app_logger.debug(f"DEBUG: File parsed - series_name={series_name}, issue_number={issue_number} (zero/variant issue), year={year}")
                    else:
                        app_logger.debug(f"DEBUG: File parsed - series_name={series_name}, issue_number={issue_number}, year={year}")
                    break

            # If no pattern matched, try to parse as single-issue/graphic novel with just year
            if not series_name:
                # Pattern for single-issue series: "Series Name (2020)" or "Series Name: Subtitle (2020)"
                single_issue_pattern = r'^(.+?)\s*\((\d{4})\)$'
                match = re.match(single_issue_pattern, name_without_ext, re.IGNORECASE)
                if match:
                    series_name = match.group(1).strip()
                    year = int(match.group(2))
                    issue_number = 1  # Default to issue 1 for single-issue series/graphic novels
                    issue_number_was_defaulted = True  # Mark that we defaulted this
                    app_logger.debug(f"DEBUG: Single-issue/graphic novel parsed - series_name={series_name}, year={year}, issue_number={issue_number} (defaulted)")

            # Ultimate fallback: if still no series_name, use the entire filename as series name
            if not series_name:
                series_name = name_without_ext.strip()
                issue_number = 1  # Default to issue 1
                issue_number_was_defaulted = True
                app_logger.debug(f"DEBUG: Fallback parsing - using entire filename as series_name={series_name}, issue_number={issue_number} (defaulted)")

        if not series_name or (not is_directory_search and issue_number is None):
            app_logger.debug(f"DEBUG: Failed to parse: {name_without_ext}")
            return jsonify({
                "success": False,
                "error": f"Could not parse series name from: {name_without_ext}"
            }), 400

        app_logger.debug(f"DEBUG: About to connect to database...")
        # Connect to GCD MySQL database
        try:
            # Get database connection details (checks saved credentials first, then env vars)
            from models.gcd import get_connection_params
            params = get_connection_params()
            if not params:
                return jsonify({
                    "success": False,
                    "error": "GCD MySQL not configured. Set credentials in Config or use environment variables."
                }), 500

            connection = mysql.connector.connect(
                host=params['host'],
                port=params['port'],
                database=params['database'],
                user=params['username'],
                password=params['password'],
                charset='utf8mb4',
                connection_timeout=30,  # 30 second connection timeout
                autocommit=True
            )
            app_logger.debug(f"DEBUG: Database connection successful!")
            cursor = connection.cursor(dictionary=True)
            # Set query timeout to 30 seconds
            cursor.execute("SET SESSION MAX_EXECUTION_TIME=30000")  # 30000 milliseconds = 30 seconds

            # Helper: build safe IN (...) placeholder list + params
            def build_in_clause(codes):
                codes = list(codes or [])
                if not codes:
                    return 'NULL', []            # produces "IN (NULL)" -> matches nothing
                return ','.join(['%s'] * len(codes)), codes

            # Progressive search strategy for GCD database
            app_logger.debug(f"DEBUG: Starting progressive search for series: '{series_name}' with year: {year}")

            # Generate search variations
            search_variations = gcd.generate_search_variations(series_name, year)
            app_logger.debug(f"DEBUG: Generated {len(search_variations)} search variations")
            app_logger.debug(f"DEBUG: Checkpoint 1 - About to initialize variables")

            series_results = []
            search_success_method = None
            app_logger.debug(f"DEBUG: Checkpoint 2 - Variables initialized")

            # Language filter
            from database import get_user_preference
            gcd_langs = get_user_preference('gcd_metadata_languages', default='en')
            languages = [language.strip().lower() for language in gcd_langs.split(",")]
            app_logger.debug(f"DEBUG: Checkpoint 3 - languages set")
            app_logger.debug(f"DEBUG: Building IN clause for language filter with codes: {languages}")
            in_clause, in_params = build_in_clause(languages)
            app_logger.debug(f"DEBUG: IN clause built: {in_clause}, params: {in_params}")

            # Base queries for LIKE and REGEXP matching
            # in_clause contains only %s placeholders from build_in_clause()
            base_select = (
                'SELECT s.id, s.name, s.year_began, s.year_ended, s.publisher_id,'
                ' l.code AS language, p.name AS publisher_name,'
                ' (SELECT COUNT(*) FROM gcd_issue i WHERE i.series_id = s.id) AS issue_count'
                ' FROM gcd_series s'
                ' JOIN stddata_language l ON s.language_id = l.id'
                ' LEFT JOIN gcd_publisher p ON s.publisher_id = p.id'
            )
            lang_filter = ' AND l.code IN (' + in_clause + ')'
            order_suffix = ' ORDER BY s.year_began DESC'

            like_query = (base_select
                          + ' WHERE s.name LIKE %s'
                          + lang_filter + order_suffix)

            like_query_with_year = (base_select
                                    + ' WHERE s.name LIKE %s'
                                    + ' AND s.year_began <= %s'
                                    + ' AND (s.year_ended IS NULL OR s.year_ended >= %s)'
                                    + lang_filter + order_suffix)

            regexp_query = (base_select
                            + ' WHERE LOWER(s.name) REGEXP %s'
                            + lang_filter + order_suffix)

            # Try each search variation progressively
            app_logger.debug(f"DEBUG: Starting search loop with {len(search_variations)} variations")
            for search_type, search_pattern in search_variations:
                app_logger.debug(f"DEBUG: Trying {search_type} search with pattern: {search_pattern}")

                try:
                    if search_type == "tokenized":
                        # Use REGEXP for tokenized search (pattern should be lowercase for LOWER(s.name))
                        cursor.execute(regexp_query, (search_pattern.lower(), *in_params))

                    elif year and search_type in ["exact", "no_issue", "no_year", "no_dash"]:
                        # Year-constrained search when year is available
                        cursor.execute(like_query_with_year, (search_pattern, year, year, *in_params))

                    else:
                        # Regular LIKE search
                        cursor.execute(like_query, (search_pattern, *in_params))

                    current_results = cursor.fetchall()
                    app_logger.debug(f"DEBUG: {search_type} search found {len(current_results)} results")

                    if current_results:
                        series_results = current_results
                        search_success_method = search_type
                        app_logger.debug(f"DEBUG: Success with {search_type} search method!")
                        break

                except Exception as e:
                    app_logger.debug(f"DEBUG: Error in {search_type} search: {str(e)}")
                    continue

            # If we still have no results, collect all partial matches for user selection
            if not series_results:
                app_logger.debug(f"DEBUG: No matches found with any search method, collecting partial matches...")
                alternative_matches = []

                # Try broader word-based search as final fallback
                words = series_name.split()
                for word in words:
                    if len(word) > 3 and word.lower() not in STOPWORDS:
                        try:
                            alt_search = f"%{word}%"
                            app_logger.debug(f"DEBUG: Trying fallback word search: {alt_search}")
                            cursor.execute(like_query, (alt_search, *in_params))
                            alt_results = cursor.fetchall()
                            if alt_results:
                                alternative_matches.extend(alt_results)
                        except Exception as e:
                            app_logger.debug(f"DEBUG: Error in fallback search for '{word}': {str(e)}")

                # Remove duplicates and sort
                seen_ids = set()
                unique_matches = []
                for match in alternative_matches:
                    if match['id'] not in seen_ids:
                        unique_matches.append(match)
                        seen_ids.add(match['id'])

                unique_matches.sort(key=lambda x: x['year_began'] or 0, reverse=True)

                if unique_matches:
                    app_logger.debug(f"DEBUG: Found {len(unique_matches)} fallback matches")
                    response_data = {
                        "success": False,
                        "requires_selection": True,
                        "parsed_filename": {
                            "series_name": series_name,
                            "issue_number": issue_number,
                            "year": year
                        },
                        "possible_matches": unique_matches,
                        "message": "Multiple series found. Please select the correct one."
                    }

                    if is_directory_search:
                        response_data["is_directory_search"] = True
                        response_data["directory_path"] = directory_path
                        response_data["directory_name"] = directory_name
                        response_data["total_files"] = total_files

                    return jsonify(response_data), 200

                return jsonify({
                    "success": False,
                    "error": f"No series found matching '{series_name}' in GCD database"
                }), 404

            # Analyze the search results and decide whether to auto-select or prompt user
            app_logger.debug(f"DEBUG: Analyzing {len(series_results)} series results for matching...")
            app_logger.debug(f"DEBUG: Search successful using method: {search_success_method}")

            if len(series_results) == 1:
                # Only one series found - auto-select it
                best_series = series_results[0]
                app_logger.debug(f"DEBUG: Single series match found: {best_series['name']} (ID: {best_series['id']}) using {search_success_method} search")
            elif len(series_results) > 1:
                # Multiple series found - always prompt user to select
                app_logger.debug(f"DEBUG: Multiple series found, showing options for user selection")
                response_data = {
                    "success": False,
                    "requires_selection": True,
                    "parsed_filename": {
                        "series_name": series_name,
                        "issue_number": issue_number,
                        "year": year
                    },
                    "possible_matches": series_results,
                    "search_method": search_success_method,
                    "message": f"Multiple series found for '{series_name}' using {search_success_method} search. Please select the correct one."
                }

                # Add directory info for directory searches
                if is_directory_search:
                    response_data["is_directory_search"] = True
                    response_data["directory_path"] = directory_path
                    response_data["directory_name"] = directory_name
                    response_data["total_files"] = total_files

                return jsonify(response_data), 200
            else:
                # This shouldn't happen since we already checked for no results above
                app_logger.debug(f"DEBUG: No series results found (unexpected)")
                return jsonify({
                    "success": False,
                    "error": f"No series found matching '{series_name}' in GCD database"
                }), 404

            # OPTIMIZED: Split into 3 smaller queries for better performance
            app_logger.debug(f"DEBUG: Searching for issue #{issue_number} in series ID {best_series['id']}...")

            # Query 1: Basic issue information (fast, no subqueries)
            # When issue_number_was_defaulted, also check for [nn] which GCD uses for one-shot comics
            # Note: issue_number can be 0, which is valid and used for variants/special editions
            if issue_number_was_defaulted:
                app_logger.debug(f"DEBUG: Issue number was defaulted, also searching for [nn] (one-shot comics)")
                basic_issue_query = """
                    SELECT
                        i.id,
                        i.title,
                        i.number,
                        i.volume,
                        i.rating AS AgeRating,
                        i.page_count,
                        i.page_count_uncertain,
                        i.key_date,
                        i.on_sale_date,
                        sr.id AS series_id,
                        sr.name AS Series,
                        l.code AS language,
                        COALESCE(ip.name, p.name) AS Publisher,
                        (SELECT COUNT(*) FROM gcd_issue i2 WHERE i2.series_id = i.series_id AND i2.deleted = 0) AS Count
                    FROM gcd_issue i
                    JOIN gcd_series sr ON sr.id = i.series_id
                    JOIN stddata_language l ON l.id = sr.language_id
                    LEFT JOIN gcd_publisher p ON p.id = sr.publisher_id
                    LEFT JOIN gcd_indicia_publisher ip ON ip.id = i.indicia_publisher_id
                    WHERE i.series_id = %s AND (i.number = %s OR i.number = CONCAT('[', %s, ']') OR i.number LIKE CONCAT(%s, ' (%') OR i.number = '[nn]')
                    LIMIT 1
                """
            else:
                basic_issue_query = """
                    SELECT
                        i.id,
                        i.title,
                        i.number,
                        i.volume,
                        i.rating AS AgeRating,
                        i.page_count,
                        i.page_count_uncertain,
                        i.key_date,
                        i.on_sale_date,
                        sr.id AS series_id,
                        sr.name AS Series,
                        l.code AS language,
                        COALESCE(ip.name, p.name) AS Publisher,
                        (SELECT COUNT(*) FROM gcd_issue i2 WHERE i2.series_id = i.series_id AND i2.deleted = 0) AS Count
                    FROM gcd_issue i
                    JOIN gcd_series sr ON sr.id = i.series_id
                    JOIN stddata_language l ON l.id = sr.language_id
                    LEFT JOIN gcd_publisher p ON p.id = sr.publisher_id
                    LEFT JOIN gcd_indicia_publisher ip ON ip.id = i.indicia_publisher_id
                    WHERE i.series_id = %s AND (i.number = %s OR i.number = CONCAT('[', %s, ']') OR i.number LIKE CONCAT(%s, ' (%'))
                    LIMIT 1
                """

            # Convert issue_number to string for SQL query (handles 0 correctly)
            issue_number_str = str(issue_number)
            app_logger.debug(f"DEBUG: Querying for issue_number_str='{issue_number_str}' (includes checks for '{issue_number_str}', '[{issue_number_str}]', '{issue_number_str} (%')")
            cursor.execute(basic_issue_query, (best_series['id'], issue_number_str, issue_number_str, issue_number_str))
            issue_basic = cursor.fetchone()

            if not issue_basic:
                app_logger.debug(f"DEBUG: Issue #{issue_number} not found in series")

                # If the issue number was defaulted and we have exactly one series match,
                # check if this is a single-issue series and get the only issue
                if issue_number_was_defaulted and len(series_results) == 1:
                    app_logger.debug(f"DEBUG: Checking if this is a single-issue series...")

                    # Count total issues in this series
                    count_query = "SELECT COUNT(*) as total FROM gcd_issue WHERE series_id = %s AND deleted = 0"
                    cursor.execute(count_query, (best_series['id'],))
                    count_result = cursor.fetchone()
                    total_issues = count_result['total'] if count_result else 0

                    app_logger.debug(f"DEBUG: Series has {total_issues} total issue(s)")

                    if total_issues == 1:
                        # This is a single-issue series, get the only issue regardless of its number
                        app_logger.debug(f"DEBUG: Single-issue series detected, fetching the only issue...")

                        single_issue_query = """
                            SELECT
                                i.id,
                                i.title,
                                i.number,
                                i.volume,
                                i.rating AS AgeRating,
                                i.page_count,
                                i.page_count_uncertain,
                                i.key_date,
                                i.on_sale_date,
                                sr.id AS series_id,
                                sr.name AS Series,
                                l.code AS language,
                                COALESCE(ip.name, p.name) AS Publisher,
                                (SELECT COUNT(*) FROM gcd_issue i2 WHERE i2.series_id = i.series_id AND i2.deleted = 0) AS Count
                            FROM gcd_issue i
                            JOIN gcd_series sr ON sr.id = i.series_id
                            JOIN stddata_language l ON l.id = sr.language_id
                            LEFT JOIN gcd_publisher p ON p.id = sr.publisher_id
                            LEFT JOIN gcd_indicia_publisher ip ON ip.id = i.indicia_publisher_id
                            WHERE i.series_id = %s AND i.deleted = 0
                            LIMIT 1
                        """

                        cursor.execute(single_issue_query, (best_series['id'],))
                        issue_basic = cursor.fetchone()

                        if issue_basic:
                            app_logger.debug(f"DEBUG: Found single issue with number: {issue_basic['number']}")
                            # Continue with normal processing using this issue
                        else:
                            app_logger.debug(f"DEBUG: Failed to fetch the single issue")
                            issue_result = None
                    else:
                        issue_result = None
                # For directory searches, if the specific issue isn't found, return series info
                # so that other files in the directory can be processed
                elif is_directory_search:
                    app_logger.debug(f"DEBUG: Directory search - issue #{issue_number} not found, but returning series info for continued processing")
                    return jsonify({
                        "success": True,
                        "issue_not_found": True,
                        "series_found": True,
                        "series_id": best_series['id'],
                        "series_name": best_series['name'],
                        "is_directory_search": True,
                        "directory_path": directory_path,
                        "directory_name": directory_name,
                        "total_files": total_files,
                        "message": f"Issue #{issue_number} not found, but series '{best_series['name']}' found. Continuing with other files."
                    }), 200
                else:
                    issue_result = None

            # Process the issue if we found it (either by exact match or single-issue fallback)
            if issue_basic:
                app_logger.debug(f"DEBUG: Basic issue info retrieved for issue #{issue_number}")
                issue_id = issue_basic['id']

                # Query 2: Get all credits in a single query (much faster than multiple subqueries)
                credits_query = """
                    SELECT
                        ct.name AS credit_type,
                        TRIM(COALESCE(NULLIF(sc.credited_as,''), NULLIF(sc.credit_name,''), c.gcd_official_name)) AS creator_name,
                        s.sequence_number
                    FROM gcd_story s
                    JOIN gcd_story_credit sc ON sc.story_id = s.id
                    JOIN gcd_credit_type ct ON ct.id = sc.credit_type_id
                    LEFT JOIN gcd_creator c ON c.id = sc.creator_id
                    WHERE s.issue_id = %s
                        AND (sc.deleted = 0 OR sc.deleted IS NULL)
                        AND NULLIF(TRIM(COALESCE(NULLIF(sc.credited_as,''), NULLIF(sc.credit_name,''), c.gcd_official_name)), '') IS NOT NULL
                    UNION
                    SELECT
                        ct.name AS credit_type,
                        TRIM(COALESCE(NULLIF(ic.credited_as,''), NULLIF(ic.credit_name,''), c.gcd_official_name)) AS creator_name,
                        NULL AS sequence_number
                    FROM gcd_issue_credit ic
                    JOIN gcd_credit_type ct ON ct.id = ic.credit_type_id
                    LEFT JOIN gcd_creator c ON c.id = ic.creator_id
                    WHERE ic.issue_id = %s
                        AND (ic.deleted = 0 OR ic.deleted IS NULL)
                        AND NULLIF(TRIM(COALESCE(NULLIF(ic.credited_as,''), NULLIF(ic.credit_name,''), c.gcd_official_name)), '') IS NOT NULL
                """

                cursor.execute(credits_query, (issue_id, issue_id))
                credits = cursor.fetchall()

                # Query 3: Story details (title, summary, genre, characters, page count)
                story_query = """
                    SELECT
                        NULLIF(TRIM(s.title), '') AS title,
                        NULLIF(TRIM(s.synopsis), '') AS synopsis,
                        NULLIF(TRIM(s.notes), '') AS notes,
                        NULLIF(TRIM(s.genre), '') AS genre,
                        NULLIF(TRIM(s.characters), '') AS characters,
                        s.page_count,
                        s.sequence_number,
                        st.name AS story_type
                    FROM gcd_story s
                    LEFT JOIN gcd_story_type st ON st.id = s.type_id
                    WHERE s.issue_id = %s
                    ORDER BY
                        CASE WHEN s.sequence_number = 0 THEN 1 ELSE 0 END,
                        CASE
                            WHEN LOWER(st.name) IN ('comic story','story') THEN 0
                            WHEN LOWER(st.name) IN ('text story','text') THEN 1
                            ELSE 3
                        END,
                        s.sequence_number
                """

                cursor.execute(story_query, (issue_id,))
                stories = cursor.fetchall()

                # Query 4: Character names from character table
                characters_query = """
                    SELECT DISTINCT c.name
                    FROM gcd_story s
                    LEFT JOIN gcd_story_character sc ON sc.story_id = s.id
                    LEFT JOIN gcd_character c ON c.id = sc.character_id
                    WHERE s.issue_id = %s AND c.name IS NOT NULL
                """

                cursor.execute(characters_query, (issue_id,))
                character_results = cursor.fetchall()

                # Process credits in Python (faster than 6 separate subqueries)
                credits_dict = {
                    'Writer': set(),
                    'Penciller': set(),
                    'Inker': set(),
                    'Colorist': set(),
                    'Letterer': set(),
                    'CoverArtist': set()
                }

                for credit in credits:
                    ct_lower = credit['credit_type'].lower()
                    seq_num = credit['sequence_number']
                    name = credit['creator_name']

                    # Writer
                    if any(x in ct_lower for x in ['script', 'writer', 'plot']):
                        if seq_num is None or seq_num != 0:
                            credits_dict['Writer'].add(name)
                    # Penciller
                    elif 'pencil' in ct_lower or 'penc' in ct_lower:
                        if seq_num is None or seq_num != 0:
                            credits_dict['Penciller'].add(name)
                    # Inker
                    elif 'ink' in ct_lower:
                        if seq_num is None or seq_num != 0:
                            credits_dict['Inker'].add(name)
                    # Colorist
                    elif 'color' in ct_lower or 'colour' in ct_lower:
                        if seq_num is None or seq_num != 0:
                            credits_dict['Colorist'].add(name)
                    # Letterer
                    elif 'letter' in ct_lower:
                        if seq_num is None or seq_num != 0:
                            credits_dict['Letterer'].add(name)
                    # Cover Artist
                    elif 'cover' in ct_lower or (seq_num == 0 and any(x in ct_lower for x in ['pencil', 'penc', 'ink', 'art'])):
                        credits_dict['CoverArtist'].add(name)

                # Convert sets to sorted comma-separated strings
                for key in credits_dict:
                    credits_dict[key] = ', '.join(sorted(credits_dict[key])) if credits_dict[key] else None

                # Process story details
                title = issue_basic['title']
                summary = None
                genres = set()
                characters_text = set()
                page_count_sum = 0

                for story in stories:
                    # Get title from first non-zero sequence story if issue title is empty
                    if not title and story['title'] and (story['sequence_number'] is None or story['sequence_number'] != 0):
                        title = story['title']

                    # Get summary (prefer synopsis > notes > title)
                    if not summary and (story['sequence_number'] is None or story['sequence_number'] != 0):
                        summary = story['synopsis'] or story['notes'] or story['title']

                    # Collect genres
                    if story['genre']:
                        for g in story['genre'].replace(';', ',').split(','):
                            g = g.strip()
                            if g:
                                genres.add(g)

                    # Collect characters
                    if story['characters']:
                        for ch in story['characters'].replace(';', ',').split(','):
                            ch = ch.strip()
                            if ch:
                                characters_text.add(ch)

                    # Sum page counts
                    if story['page_count']:
                        page_count_sum += float(story['page_count'])

                # Add character names from character table
                for char_row in character_results:
                    if char_row['name']:
                        characters_text.add(char_row['name'])

                # Calculate dates
                date_str = issue_basic['key_date'] or issue_basic['on_sale_date']
                year = None
                month = None
                if date_str and len(date_str) >= 4:
                    year = int(date_str[0:4])
                    if len(date_str) >= 7:
                        month = int(date_str[5:7])

                # Calculate page count
                page_count = None
                if issue_basic['page_count'] and issue_basic['page_count'] > 0 and not issue_basic['page_count_uncertain']:
                    page_count = issue_basic['page_count']
                elif page_count_sum > 0:
                    page_count = round(page_count_sum)

                # Build final result dictionary matching the original structure
                issue_result = {
                    'id': issue_id,
                    'Title': title,
                    'Series': issue_basic['Series'],
                    'Number': issue_basic['number'],
                    'Count': issue_basic['Count'],
                    'Volume': issue_basic['volume'],
                    'Summary': summary,
                    'Year': year,
                    'Month': month,
                    'Writer': credits_dict['Writer'],
                    'Penciller': credits_dict['Penciller'],
                    'Inker': credits_dict['Inker'],
                    'Colorist': credits_dict['Colorist'],
                    'Letterer': credits_dict['Letterer'],
                    'CoverArtist': credits_dict['CoverArtist'],
                    'Publisher': issue_basic['Publisher'],
                    'Genre': ', '.join(sorted(genres)) if genres else None,
                    'Characters': ', '.join(sorted(characters_text)) if characters_text else None,
                    'AgeRating': issue_basic['AgeRating'],
                    'LanguageISO': issue_basic['language'],
                    'PageCount': page_count
                }
            else:
                # If we still don't have issue_basic after all attempts, set issue_result to None
                issue_result = None

            app_logger.debug(f"DEBUG: Issue search result: {'Found' if issue_result else 'Not found'}")
            if issue_result:
                #print(f"DEBUG: Issue result keys: {list(issue_result.keys())}")
                #print(f"DEBUG: Issue result values: {dict(issue_result)}")
                #print(f"DEBUG: Writer value: '{issue_result.get('Writer')}'")
                app_logger.debug(f"DEBUG: Summary value: '{issue_result.get('Summary')}'")
                #print(f"DEBUG: Characters value: '{issue_result.get('Characters')}'")

            matches_found = len(series_results)

            if issue_result:
                app_logger.debug(f"DEBUG: Issue found! Title: {issue_result.get('title', 'N/A')}")

                # Check if ComicInfo.xml already exists and has Notes data
                try:
                    from comicinfo import read_comicinfo_from_zip
                    existing_comicinfo = read_comicinfo_from_zip(file_path)
                    existing_notes = existing_comicinfo.get('Notes', '').strip()

                    if existing_notes:
                        app_logger.info(f"Skipping ComicInfo.xml generation - file already has Notes data: {existing_notes[:50]}...")

                        # For directory searches, return series_id so processing can continue with other files
                        if is_directory_search:
                            response_data = {
                                "success": True,
                                "skipped": True,
                                "message": "ComicInfo.xml already exists with Notes data",
                                "existing_notes": existing_notes,
                                "series_id": best_series['id'],
                                "is_directory_search": True,
                                "directory_path": directory_path,
                                "directory_name": directory_name,
                                "total_files": total_files
                            }
                            return jsonify(response_data), 200
                        else:
                            return jsonify({
                                "success": True,
                                "skipped": True,
                                "message": "ComicInfo.xml already exists with Notes data",
                                "existing_notes": existing_notes
                            }), 200
                except Exception as check_error:
                    app_logger.debug(f"DEBUG: Error checking existing ComicInfo.xml (will proceed with generation): {str(check_error)}")

                # Generate ComicInfo.xml content
                app_logger.debug(f"DEBUG: Generating ComicInfo.xml...")
                try:
                    comicinfo_xml = generate_comicinfo_xml(issue_result, best_series)
                    app_logger.debug(f"DEBUG: ComicInfo.xml generated successfully (length: {len(comicinfo_xml)} chars)")
                except Exception as xml_error:
                    app_logger.debug(f"DEBUG: Error generating ComicInfo.xml: {str(xml_error)}")
                    app_logger.debug(f"DEBUG: XML Error Traceback: {traceback.format_exc()}")
                    return jsonify({
                        "success": False,
                        "error": f"Failed to generate metadata: {str(xml_error)}"
                    }), 500

                # Add ComicInfo.xml to the CBZ file
                app_logger.debug(f"DEBUG: Adding ComicInfo.xml to CBZ file: {file_path}")
                try:
                    add_comicinfo_to_cbz(file_path, comicinfo_xml)
                    from database import set_has_comicinfo
                    set_has_comicinfo(file_path)
                    app_logger.debug(f"DEBUG: Successfully added ComicInfo.xml!")
                except Exception as cbz_error:
                    app_logger.debug(f"DEBUG: Error adding ComicInfo.xml: {str(cbz_error)}")
                    app_logger.debug(f"DEBUG: CBZ Error Traceback: {traceback.format_exc()}")
                    return jsonify({
                        "success": False,
                        "error": f"Failed to add metadata to CBZ file: {str(cbz_error)}"
                    }), 500

                app_logger.debug(f"DEBUG: Returning success response...")
                response_data = {
                    "success": True,
                    "metadata": {
                        "series": issue_result['Series'],
                        "issue": issue_result['Number'],
                        "title": issue_result['Title'],
                        "publisher": issue_result['Publisher'],
                        "year": issue_result['Year'],
                        "month": issue_result['Month'],
                        "page_count": issue_result['PageCount'],
                        "writer": issue_result.get('Writer'),
                        "artist": issue_result.get('Penciller'),
                        "genre": issue_result.get('Genre'),
                        "characters": issue_result.get('Characters')
                    },
                    "matches_found": matches_found
                }

                # Add series_id for directory searches to enable bulk processing
                if is_directory_search:
                    response_data["series_id"] = best_series['id']
                    response_data["is_directory_search"] = True
                    response_data["directory_path"] = directory_path
                    response_data["directory_name"] = directory_name
                    response_data["total_files"] = total_files

                return jsonify(response_data)
            else:
                app_logger.debug(f"DEBUG: Issue #{issue_number} not found for series '{best_series['name']}'")
                app_logger.debug(f"DEBUG: Returning 404 response...")
                return jsonify({
                    "success": False,
                    "error": f"Issue #{issue_number} not found for series '{best_series['name']}' in GCD database",
                    "series_found": best_series['name'],
                    "matches_found": matches_found
                }), 404

        except mysql.connector.Error as db_error:
            app_logger.debug(f"MySQL Error: {str(db_error)}")
            app_logger.debug(f"MySQL Error Traceback: {traceback.format_exc()}")
            return jsonify({
                "success": False,
                "error": f"Database connection error: {str(db_error)}"
            }), 500
        finally:
            if 'connection' in locals() and connection.is_connected():
                cursor.close()
                connection.close()

    except Exception as e:
        error_msg = str(e)
        error_traceback = traceback.format_exc()
        app_logger.error(f"ERROR in search_gcd_metadata: {error_msg}")
        app_logger.debug(f"Full Traceback:\n{error_traceback}")
        return jsonify({
            "success": False,
            "error": f"Server error: {error_msg}"
        }), 500



# =============================================================================
# GCD Metadata With Selection
# =============================================================================

@metadata_bp.route('/search-gcd-metadata-with-selection', methods=['POST'])
def search_gcd_metadata_with_selection():
    """Search GCD database for comic metadata using user-selected series"""
    try:

        data = request.get_json()
        file_path = data.get('file_path')
        file_name = data.get('file_name')
        series_id = data.get('series_id')
        issue_number = data.get('issue_number')

        app_logger.debug(f"DEBUG: search_gcd_metadata_with_selection called - file={file_name}, series_id={series_id}, issue={issue_number}")

        # Note: issue_number can be 0, so check for None explicitly
        if not file_path or not file_name or series_id is None or issue_number is None:
            app_logger.error(f"ERROR: Missing required parameters - file_path={file_path}, file_name={file_name}, series_id={series_id}, issue_number={issue_number}")
            return jsonify({
                "success": False,
                "error": "Missing required parameters"
            }), 400

        # Connect to GCD MySQL database
        try:
            # Get database connection details (checks saved credentials first, then env vars)
            from models.gcd import get_connection_params
            params = get_connection_params()
            if not params:
                return jsonify({
                    "success": False,
                    "error": "GCD MySQL not configured"
                }), 500

            connection = mysql.connector.connect(
                host=params['host'],
                port=params['port'],
                database=params['database'],
                user=params['username'],
                password=params['password'],
                charset='utf8mb4'
            )
            cursor = connection.cursor(dictionary=True)

            # Get series information
            series_query = """
                SELECT s.id, s.name, s.year_began, s.year_ended, s.publisher_id,
                       p.name as publisher_name
                FROM gcd_series s
                LEFT JOIN gcd_publisher p ON s.publisher_id = p.id
                WHERE s.id = %s
            """
            cursor.execute(series_query, (series_id,))
            series_result = cursor.fetchone()

            if not series_result:
                return jsonify({
                    "success": False,
                    "error": f"Series with ID {series_id} not found"
                }), 404

            # Search for the specific issue using comprehensive query
            issue_query = """
                SELECT
                  i.id,
                  COALESCE(
                    NULLIF(TRIM(i.title), ''),
                    (
                      SELECT NULLIF(TRIM(s.title), '')
                      FROM gcd_story s
                      WHERE s.issue_id = i.id AND (s.sequence_number IS NULL OR s.sequence_number <> 0)
                      ORDER BY s.sequence_number
                      LIMIT 1
                    )
                  )                                                   AS Title,
                  sr.name                                             AS Series,
                  i.number                                            AS Number,
                  (
                    SELECT COUNT(*)
                    FROM gcd_issue i2
                    WHERE i2.series_id = i.series_id AND i2.deleted = 0
                  )                                                   AS `Count`,
                  i.volume                                            AS Volume,
                  (
                    SELECT COALESCE(
                      NULLIF(TRIM(s.synopsis), ''),
                      NULLIF(TRIM(s.notes), ''),
                      NULLIF(TRIM(s.title), '')
                    )
                    FROM gcd_story s
                    WHERE s.issue_id = i.id
                      AND COALESCE(
                        NULLIF(TRIM(s.synopsis), ''),
                        NULLIF(TRIM(s.notes), ''),
                        NULLIF(TRIM(s.title), '')
                      ) IS NOT NULL
                    ORDER BY
                      CASE WHEN s.sequence_number = 0 THEN 1 ELSE 0 END,
                      CASE WHEN NULLIF(TRIM(s.synopsis), '') IS NOT NULL THEN 0 ELSE 1 END,
                      CASE WHEN NULLIF(TRIM(s.notes), '') IS NOT NULL THEN 0 ELSE 1 END,
                      s.sequence_number
                    LIMIT 1
                  )                                                   AS Summary,
                  CASE
                    WHEN COALESCE(i.key_date, i.on_sale_date) IS NOT NULL
                         AND LENGTH(COALESCE(i.key_date, i.on_sale_date)) >= 4
                      THEN CAST(SUBSTRING(COALESCE(i.key_date, i.on_sale_date), 1, 4) AS UNSIGNED)
                  END AS `Year`,
                  CASE
                    WHEN COALESCE(i.key_date, i.on_sale_date) IS NOT NULL
                         AND LENGTH(COALESCE(i.key_date, i.on_sale_date)) >= 7
                      THEN CAST(SUBSTRING(COALESCE(i.key_date, i.on_sale_date), 6, 2) AS UNSIGNED)
                  END AS `Month`,
                  (
                    SELECT GROUP_CONCAT(DISTINCT name ORDER BY name SEPARATOR ', ')
                    FROM (
                      SELECT TRIM(COALESCE(NULLIF(sc.credited_as,''), NULLIF(sc.credit_name,''), c.gcd_official_name)) AS name
                      FROM gcd_story s
                      JOIN gcd_story_credit sc ON sc.story_id = s.id
                      JOIN gcd_credit_type ct   ON ct.id = sc.credit_type_id
                      LEFT JOIN gcd_creator c   ON c.id = sc.creator_id
                      WHERE s.issue_id = i.id
                        AND (s.sequence_number IS NULL OR s.sequence_number <> 0)
                        AND (ct.name LIKE 'script%' OR ct.name LIKE 'writer%' OR ct.name LIKE 'plot%')
                        AND (sc.deleted = 0 OR sc.deleted IS NULL)
                      UNION
                      SELECT TRIM(COALESCE(NULLIF(ic.credited_as,''), NULLIF(ic.credit_name,''), c.gcd_official_name)) AS name
                      FROM gcd_issue_credit ic
                      JOIN gcd_credit_type ct ON ct.id = ic.credit_type_id
                      LEFT JOIN gcd_creator c ON c.id = ic.creator_id
                      WHERE ic.issue_id = i.id
                        AND (ct.name LIKE 'script%' OR ct.name LIKE 'writer%' OR ct.name LIKE 'plot%')
                        AND (ic.deleted = 0 OR ic.deleted IS NULL)
                    ) x
                    WHERE NULLIF(name,'') IS NOT NULL
                  )                                                   AS Writer,
                  (
                    SELECT GROUP_CONCAT(DISTINCT name ORDER BY name SEPARATOR ', ')
                    FROM (
                      SELECT TRIM(COALESCE(NULLIF(sc.credited_as,''), NULLIF(sc.credit_name,''), c.gcd_official_name)) AS name
                      FROM gcd_story s
                      JOIN gcd_story_credit sc ON sc.story_id = s.id
                      JOIN gcd_credit_type ct   ON ct.id = sc.credit_type_id
                      LEFT JOIN gcd_creator c   ON c.id = sc.creator_id
                      WHERE s.issue_id = i.id
                        AND (s.sequence_number IS NULL OR s.sequence_number <> 0)
                        AND (ct.name LIKE 'pencil%' OR ct.name LIKE 'penc%')
                        AND (sc.deleted = 0 OR sc.deleted IS NULL)
                      UNION
                      SELECT TRIM(COALESCE(NULLIF(ic.credited_as,''), NULLIF(ic.credit_name,''), c.gcd_official_name)) AS name
                      FROM gcd_issue_credit ic
                      JOIN gcd_credit_type ct ON ct.id = ic.credit_type_id
                      LEFT JOIN gcd_creator c ON c.id = ic.creator_id
                      WHERE ic.issue_id = i.id
                        AND (ct.name LIKE 'pencil%' OR ct.name LIKE 'penc%')
                        AND (ic.deleted = 0 OR ic.deleted IS NULL)
                    ) x
                    WHERE NULLIF(name,'') IS NOT NULL
                  )                                                   AS Penciller,
                  (
                    SELECT GROUP_CONCAT(DISTINCT name ORDER BY name SEPARATOR ', ')
                    FROM (
                      SELECT TRIM(COALESCE(NULLIF(sc.credited_as,''), NULLIF(sc.credit_name,''), c.gcd_official_name)) AS name
                      FROM gcd_story s
                      JOIN gcd_story_credit sc ON sc.story_id = s.id
                      JOIN gcd_credit_type ct   ON ct.id = sc.credit_type_id
                      LEFT JOIN gcd_creator c   ON c.id = sc.creator_id
                      WHERE s.issue_id = i.id
                        AND (s.sequence_number IS NULL OR s.sequence_number <> 0)
                        AND (ct.name LIKE 'ink%')
                        AND (sc.deleted = 0 OR sc.deleted IS NULL)
                      UNION
                      SELECT TRIM(COALESCE(NULLIF(ic.credited_as,''), NULLIF(ic.credit_name,''), c.gcd_official_name)) AS name
                      FROM gcd_issue_credit ic
                      JOIN gcd_credit_type ct ON ct.id = ic.credit_type_id
                      LEFT JOIN gcd_creator c ON c.id = ic.creator_id
                      WHERE ic.issue_id = i.id
                        AND (ct.name LIKE 'ink%')
                        AND (ic.deleted = 0 OR ic.deleted IS NULL)
                    ) x
                    WHERE NULLIF(name,'') IS NOT NULL
                  )                                                   AS Inker,
                  (
                    SELECT GROUP_CONCAT(DISTINCT name ORDER BY name SEPARATOR ', ')
                    FROM (
                      SELECT TRIM(COALESCE(NULLIF(sc.credited_as,''), NULLIF(sc.credit_name,''), c.gcd_official_name)) AS name
                      FROM gcd_story s
                      JOIN gcd_story_credit sc ON sc.story_id = s.id
                      JOIN gcd_credit_type ct   ON ct.id = sc.credit_type_id
                      LEFT JOIN gcd_creator c   ON c.id = sc.creator_id
                      WHERE s.issue_id = i.id
                        AND (s.sequence_number IS NULL OR s.sequence_number <> 0)
                        AND (ct.name LIKE 'color%' OR ct.name LIKE 'colour%')
                        AND (sc.deleted = 0 OR sc.deleted IS NULL)
                      UNION
                      SELECT TRIM(COALESCE(NULLIF(ic.credited_as,''), NULLIF(ic.credit_name,''), c.gcd_official_name)) AS name
                      FROM gcd_issue_credit ic
                      JOIN gcd_credit_type ct ON ct.id = ic.credit_type_id
                      LEFT JOIN gcd_creator c ON c.id = ic.creator_id
                      WHERE ic.issue_id = i.id
                        AND (ct.name LIKE 'color%' OR ct.name LIKE 'colour%')
                        AND (ic.deleted = 0 OR ic.deleted IS NULL)
                    ) x
                    WHERE NULLIF(name,'') IS NOT NULL
                  )                                                   AS Colorist,
                  (
                    SELECT GROUP_CONCAT(DISTINCT name ORDER BY name SEPARATOR ', ')
                    FROM (
                      SELECT TRIM(COALESCE(NULLIF(sc.credited_as,''), NULLIF(sc.credit_name,''), c.gcd_official_name)) AS name
                      FROM gcd_story s
                      JOIN gcd_story_credit sc ON sc.story_id = s.id
                      JOIN gcd_credit_type ct   ON ct.id = sc.credit_type_id
                      LEFT JOIN gcd_creator c   ON c.id = sc.creator_id
                      WHERE s.issue_id = i.id
                        AND (s.sequence_number IS NULL OR s.sequence_number <> 0)
                        AND (ct.name LIKE 'letter%')
                        AND (sc.deleted = 0 OR sc.deleted IS NULL)
                      UNION
                      SELECT TRIM(COALESCE(NULLIF(ic.credited_as,''), NULLIF(ic.credit_name,''), c.gcd_official_name)) AS name
                      FROM gcd_issue_credit ic
                      JOIN gcd_credit_type ct ON ct.id = ic.credit_type_id
                      LEFT JOIN gcd_creator c ON c.id = ic.creator_id
                      WHERE ic.issue_id = i.id
                        AND (ct.name LIKE 'letter%')
                        AND (ic.deleted = 0 OR ic.deleted IS NULL)
                    ) x
                    WHERE NULLIF(name,'') IS NOT NULL
                  )                                                   AS Letterer,
                  (
                    SELECT GROUP_CONCAT(DISTINCT name ORDER BY name SEPARATOR ', ')
                    FROM (
                      SELECT TRIM(COALESCE(NULLIF(sc.credited_as,''), NULLIF(sc.credit_name,''), c.gcd_official_name)) AS name
                      FROM gcd_story s
                      JOIN gcd_story_credit sc ON sc.story_id = s.id
                      JOIN gcd_credit_type ct   ON ct.id = sc.credit_type_id
                      LEFT JOIN gcd_creator c   ON c.id = sc.creator_id
                      WHERE s.issue_id = i.id
                        AND (s.sequence_number = 0 OR ct.name LIKE 'cover%')
                        AND (ct.name LIKE 'pencil%' OR ct.name LIKE 'penc%' OR ct.name LIKE 'ink%' OR ct.name LIKE 'art%' OR ct.name LIKE 'cover%')
                        AND (sc.deleted = 0 OR sc.deleted IS NULL)
                      UNION
                      SELECT TRIM(COALESCE(NULLIF(ic.credited_as,''), NULLIF(ic.credit_name,''), c.gcd_official_name)) AS name
                      FROM gcd_issue_credit ic
                      JOIN gcd_credit_type ct ON ct.id = ic.credit_type_id
                      LEFT JOIN gcd_creator c ON c.id = ic.creator_id
                      WHERE ic.issue_id = i.id
                        AND (ct.name LIKE 'cover%')
                        AND (ic.deleted = 0 OR ic.deleted IS NULL)
                    ) z
                    WHERE NULLIF(name,'') IS NOT NULL
                  )                                                   AS CoverArtist,
                  COALESCE(ip.name, p.name)                           AS Publisher,
                  (
                    SELECT TRIM(BOTH ', ' FROM
                           REPLACE(
                             GROUP_CONCAT(DISTINCT NULLIF(TRIM(s.genre), '') SEPARATOR ', '),
                             ';', ','
                           ))
                    FROM gcd_story s
                    WHERE s.issue_id = i.id
                  )                                                   AS Genre,
                  COALESCE(
                    (
                      SELECT NULLIF(GROUP_CONCAT(DISTINCT c.name SEPARATOR ', '), '')
                      FROM gcd_story s
                      LEFT JOIN gcd_story_character sc ON sc.story_id = s.id
                      LEFT JOIN gcd_character c ON c.id = sc.character_id
                      WHERE s.issue_id = i.id
                    ),
                    (
                      SELECT TRIM(BOTH ', ' FROM
                             REPLACE(
                               GROUP_CONCAT(DISTINCT NULLIF(TRIM(s.characters), '') SEPARATOR ', '),
                               ';', ','
                             ))
                      FROM gcd_story s
                      WHERE s.issue_id = i.id
                    )
                  )                                                   AS Characters,
                  i.rating                                            AS AgeRating,
                  l.code                                              AS LanguageISO,
                  i.page_count                                        AS PageCount
                FROM gcd_issue i
                JOIN gcd_series sr                 ON sr.id = i.series_id
                JOIN stddata_language l            ON sr.language_id = l.id
                LEFT JOIN gcd_publisher p          ON p.id = sr.publisher_id
                LEFT JOIN gcd_indicia_publisher ip ON ip.id = i.indicia_publisher_id
                WHERE i.series_id = %s AND (i.number = %s OR i.number = CONCAT('[', %s, ']') OR i.number LIKE CONCAT(%s, ' (%'))
                LIMIT 1
            """

            app_logger.debug(f"DEBUG: Executing issue query for series {series_id}, issue {issue_number}")
            cursor.execute(issue_query, (series_id, str(issue_number), str(issue_number), str(issue_number)))
            issue_result = cursor.fetchone()

            app_logger.debug(f"DEBUG: Issue search result for series {series_id}, issue {issue_number}: {'Found' if issue_result else 'Not found'}")
            if issue_result:
                app_logger.debug(f"DEBUG: Issue result keys: {list(issue_result.keys())}")
                app_logger.debug(f"DEBUG: Issue title: {issue_result.get('Title', 'N/A')}")

            if issue_result:
                # Check if ComicInfo.xml already exists and has Notes data
                try:
                    from comicinfo import read_comicinfo_from_zip
                    existing_comicinfo = read_comicinfo_from_zip(file_path)
                    existing_notes = existing_comicinfo.get('Notes', '').strip()

                    if existing_notes:
                        app_logger.info(f"Skipping ComicInfo.xml generation - file already has Notes data: {existing_notes[:50]}...")
                        return jsonify({
                            "success": True,
                            "skipped": True,
                            "message": "ComicInfo.xml already exists with Notes data",
                            "existing_notes": existing_notes,
                            "metadata": {
                                "issue": issue_result['Number']
                            }
                        }), 200
                except Exception as check_error:
                    app_logger.debug(f"DEBUG: Error checking existing ComicInfo.xml (will proceed with generation): {str(check_error)}")

                # Generate ComicInfo.xml content
                comicinfo_xml = generate_comicinfo_xml(issue_result, series_result)

                # Add ComicInfo.xml to the CBZ file
                add_comicinfo_to_cbz(file_path, comicinfo_xml)
                from database import set_has_comicinfo
                set_has_comicinfo(file_path)

                return jsonify({
                    "success": True,
                    "metadata": {
                        "series": issue_result['Series'],
                        "issue": issue_result['Number'],
                        "title": issue_result['Title'],
                        "publisher": issue_result['Publisher'],
                        "year": issue_result['Year'],
                        "writer": issue_result['Writer'],
                        "penciller": issue_result['Penciller'],
                        "inker": issue_result['Inker'],
                        "colorist": issue_result['Colorist'],
                        "letterer": issue_result['Letterer'],
                        "cover_artist": issue_result['CoverArtist'],
                        "genre": issue_result['Genre'],
                        "characters": issue_result['Characters'],
                        "summary": issue_result['Summary'],
                        "age_rating": issue_result['AgeRating']
                    }
                })
            else:
                return jsonify({
                    "success": False,
                    "error": f"Issue #{issue_number} not found for series '{series_result['name']}'"
                }), 404

        except mysql.connector.Error as db_error:
            app_logger.error(f"MySQL Error in search_gcd_metadata_with_selection: {str(db_error)}")
            app_logger.debug(f"MySQL Error Traceback:\n{traceback.format_exc()}")
            return jsonify({
                "success": False,
                "error": f"Database connection error: {str(db_error)}"
            }), 500
        finally:
            if 'connection' in locals() and connection.is_connected():
                cursor.close()
                connection.close()

    except Exception as e:
        app_logger.error(f"ERROR in search_gcd_metadata_with_selection: {str(e)}")
        app_logger.debug(f"Full Traceback:\n{traceback.format_exc()}")
        return jsonify({
            "success": False,
            "error": f"Server error: {str(e)}"
        }), 500


# =============================================================================
# Unified Metadata Search (Provider Priority Cascade)
# =============================================================================

def _try_metron_single(cvinfo_path, issue_number):
    """Try Metron provider for a single file. Returns (metadata_dict, image_url) or (None, None)."""
    try:
        series_id = metron.parse_cvinfo_for_metron_id(cvinfo_path)
        metron_username = current_app.config.get("METRON_USERNAME", "").strip()
        metron_password = current_app.config.get("METRON_PASSWORD", "").strip()

        if not (series_id and metron_username and metron_password and metron.MOKKARI_AVAILABLE):
            return None, None

        metron_api = metron.get_api(metron_username, metron_password)
        if not metron_api:
            return None, None

        issue_data = metron.get_issue_metadata(metron_api, series_id, issue_number)
        if not issue_data:
            return None, None

        metadata = metron.map_to_comicinfo(issue_data)

        # Extract image URL
        img_url = None
        if isinstance(issue_data, dict):
            image = issue_data.get('image')
            if image:
                img_url = str(image) if not isinstance(image, str) else image

        return metadata, img_url
    except Exception as e:
        app_logger.warning(f"[search-metadata] Metron lookup failed: {e}")
        return None, None


def _try_comicvine_single(cvinfo_path, series_name, issue_number, year):
    """Try ComicVine provider for a single file.
    Returns (metadata_dict, image_url, volume_data, None) on success,
    or (None, None, None, selection_data) when user selection is needed,
    or (None, None, None, None) when nothing found.
    """
    try:
        api_key = current_app.config.get("COMICVINE_API_KEY", "").strip()
        if not api_key or not comicvine.is_simyan_available():
            return None, None, None, None

        # If cvinfo exists with volume_id, use it directly
        if cvinfo_path:
            cv_volume_id = comicvine.parse_cvinfo_volume_id(cvinfo_path)
            if cv_volume_id:
                issue_data = comicvine.get_issue_by_number(api_key, cv_volume_id, issue_number, year)
                if issue_data:
                    volume_data = {
                        'id': cv_volume_id,
                        'name': issue_data.get('volume_name', ''),
                        'start_year': issue_data.get('year'),
                        'publisher_name': issue_data.get('publisher_name', '')
                    }
                    # Read start_year from cvinfo for Volume field
                    cvinfo_fields = comicvine.read_cvinfo_fields(cvinfo_path)
                    start_year = cvinfo_fields.get('start_year')
                    metadata = comicvine.map_to_comicinfo(issue_data, volume_data, start_year=start_year)
                    img_url = issue_data.get('image_url')
                    if img_url and not isinstance(img_url, str):
                        img_url = str(img_url)
                    return metadata, img_url, volume_data, None

        # No cvinfo or no volume_id - search by series name
        if not series_name:
            return None, None, None, None

        # Normalize series name for searching
        normalized_series = re.sub(r'[:\-\u2013\u2014\'\"\.\,\!\?]', ' ', series_name)
        normalized_series = re.sub(r'\s+', ' ', normalized_series).strip()

        volumes = comicvine.search_volumes(api_key, normalized_series, year)
        if not volumes:
            return None, None, None, None

        # Check for confident match
        search_words = set(normalized_series.lower().split())
        confident_match = None
        if len(volumes) > 1:
            for volume in volumes:
                volume_name_lower = volume['name'].lower()
                if all(word in volume_name_lower for word in search_words):
                    confident_match = volume
                    break

        if confident_match:
            selected_volume = confident_match
        elif len(volumes) > 1:
            # Multiple volumes, no confident match - need user selection
            return None, None, None, {
                "requires_selection": True,
                "provider": "comicvine",
                "possible_matches": volumes
            }
        else:
            selected_volume = volumes[0]

        # Get the issue from selected volume
        issue_data = comicvine.get_issue_by_number(api_key, selected_volume['id'], issue_number, year)
        if not issue_data:
            return None, None, None, None

        metadata = comicvine.map_to_comicinfo(issue_data, selected_volume)
        img_url = issue_data.get('image_url')
        if img_url and not isinstance(img_url, str):
            img_url = str(img_url)
        return metadata, img_url, selected_volume, None

    except Exception as e:
        app_logger.warning(f"[search-metadata] ComicVine lookup failed: {e}")
        return None, None, None, None


def _try_gcd_single(series_name, issue_number, year):
    """Try GCD provider for a single file.
    Returns (metadata_dict, None, None) on success,
    or (None, None, selection_data) when user selection is needed,
    or (None, None, None) when nothing found.
    """
    try:
        if not (gcd.is_mysql_available() and gcd.check_mysql_status().get('gcd_mysql_available', False)):
            return None, None, None

        if not series_name:
            return None, None, None

        gcd_series = gcd.search_series(series_name, year)
        if not gcd_series:
            return None, None, None

        metadata = gcd.get_issue_metadata(gcd_series['id'], issue_number)
        if metadata:
            return metadata, None, None

        return None, None, None
    except Exception as e:
        app_logger.warning(f"[search-metadata] GCD lookup failed: {e}")
        return None, None, None


@metadata_bp.route('/api/search-metadata', methods=['POST'])
def search_metadata():
    """
    Unified metadata search endpoint that respects library provider priorities.

    Input: {file_path, file_name, library_id}
    Or for selection follow-up: {file_path, file_name, library_id, selected_match: {provider, volume_id, ...}}
    """
    from app import log_file_if_in_data, invalidate_cache_for_path, update_index_on_move
    from database import get_library_providers, set_has_comicinfo

    try:
        data = request.get_json()
        file_path = data.get('file_path')
        file_name = data.get('file_name')
        library_id = data.get('library_id')
        selected_match = data.get('selected_match')

        if not file_path or not file_name:
            return jsonify({"success": False, "error": "Missing file_path or file_name"}), 400

        app_logger.info(f"[search-metadata] Starting search for {file_name}")

        # Parse filename - extract series name, issue number, year
        name_without_ext = file_name
        for ext in ('.cbz', '.cbr', '.zip'):
            name_without_ext = name_without_ext.replace(ext, '')

        series_name = None
        issue_number = None
        issue_from_pattern = False  # Track if issue number came from a regex match
        year = None

        patterns = [
            r'^(.+?)\s+(\d{3,4})\s+\((\d{4})\)',
            r'^(.+?)\s+#?(\d{1,4})\s*\((\d{4})\)',
            r'^(.+?)\s+v\d+\s+(\d{1,4})\s*\((\d{4})\)',
            r'^(.+?)\s+(\d{1,4})\s+\(of\s+\d+\)\s+\((\d{4})\)',
            r'^(.+?)\s+#?(\d{1,4})$',
        ]

        for pattern in patterns:
            match = re.match(pattern, name_without_ext, re.IGNORECASE)
            if match:
                series_name = match.group(1).strip()
                issue_number = str(int(match.group(2)))
                issue_from_pattern = True
                year = int(match.group(3)) if len(match.groups()) >= 3 else None
                break

        if not series_name:
            single_issue_pattern = r'^(.+?)\s*\((\d{4})\)$'
            match = re.match(single_issue_pattern, name_without_ext, re.IGNORECASE)
            if match:
                series_name = match.group(1).strip()
                year = int(match.group(2))
                issue_number = "1"

        if not series_name:
            series_name = name_without_ext.strip()
            issue_number = "1"

        # Also extract issue number via the provider base utility
        # Only use fallback when no pattern matched an issue number (avoid
        # overriding a valid match, e.g. "Spider-Man 2099 001" where 001 is correct)
        if not issue_number or (issue_number == "1" and not issue_from_pattern):
            extracted = comicvine.extract_issue_number(file_name)
            if extracted:
                issue_number = extracted

        app_logger.info(f"[search-metadata] Parsed: series='{series_name}', issue=#{issue_number}, year={year}")

        # Check for cvinfo file in parent folder
        folder_path = os.path.dirname(file_path)
        cvinfo_path = comicvine.find_cvinfo_in_folder(folder_path)

        # Handle selection follow-up (user picked from a selection modal)
        if selected_match:
            provider = selected_match.get('provider')
            app_logger.info(f"[search-metadata] Selection follow-up for provider: {provider}")

            metadata = None
            img_url = None
            volume_data = None

            if provider == 'comicvine':
                volume_id = selected_match.get('volume_id')
                api_key = current_app.config.get("COMICVINE_API_KEY", "").strip()
                if volume_id and api_key:
                    issue_data = comicvine.get_issue_by_number(api_key, volume_id, issue_number, year)
                    if issue_data:
                        volume_data = {
                            'id': volume_id,
                            'name': issue_data.get('volume_name', ''),
                            'start_year': issue_data.get('year'),
                            'publisher_name': selected_match.get('publisher_name', '')
                        }
                        metadata = comicvine.map_to_comicinfo(issue_data, volume_data)
                        img_url = issue_data.get('image_url')
                        if img_url and not isinstance(img_url, str):
                            img_url = str(img_url)

            elif provider == 'gcd':
                series_id = selected_match.get('series_id')
                if series_id:
                    metadata = gcd.get_issue_metadata(series_id, issue_number)

            if not metadata:
                return jsonify({"success": False, "error": "No metadata found for selection"}), 404

            # Apply metadata
            comicinfo_xml = generate_comicinfo_xml(metadata)
            add_comicinfo_to_cbz(file_path, comicinfo_xml)
            set_has_comicinfo(file_path)

            # Auto-move if enabled and we have volume data
            new_file_path = None
            if volume_data:
                try:
                    new_file_path = comicvine.auto_move_file(file_path, volume_data, current_app.config)
                except Exception as move_error:
                    app_logger.error(f"[search-metadata] Auto-move failed: {move_error}")

            response_data = {
                "success": True,
                "source": provider,
                "metadata": metadata,
                "image_url": img_url,
                "rename_config": {
                    "enabled": current_app.config.get("ENABLE_CUSTOM_RENAME", False),
                    "pattern": current_app.config.get("CUSTOM_RENAME_PATTERN", ""),
                    "auto_rename": current_app.config.get("ENABLE_AUTO_RENAME", False)
                }
            }

            if new_file_path:
                response_data["moved"] = True
                response_data["new_file_path"] = new_file_path
                log_file_if_in_data(new_file_path)
                invalidate_cache_for_path(os.path.dirname(file_path))
                invalidate_cache_for_path(os.path.dirname(new_file_path))
                update_index_on_move(file_path, new_file_path)

            app_logger.info(f"[search-metadata] {provider} returned metadata for {file_name} (via selection)")
            return jsonify(response_data)

        # Look up library provider priorities
        if library_id:
            library_providers = get_library_providers(library_id)
            provider_order = [p['provider_type'] for p in library_providers if p.get('enabled', True)]
        else:
            # Fallback: try all available providers in default order
            provider_order = []
            if current_app.config.get("METRON_PASSWORD", "").strip():
                provider_order.append('metron')
            if current_app.config.get("COMICVINE_API_KEY", "").strip():
                provider_order.append('comicvine')
            if gcd.is_mysql_available() and gcd.check_mysql_status().get('gcd_mysql_available', False):
                provider_order.append('gcd')

        app_logger.info(f"[search-metadata] Provider order: {provider_order}")

        # Try each provider in priority order
        for provider_type in provider_order:
            app_logger.info(f"[search-metadata] Trying provider: {provider_type} for {file_name}")

            metadata = None
            img_url = None
            volume_data = None
            selection_data = None

            if provider_type == 'metron':
                if cvinfo_path:
                    metadata, img_url = _try_metron_single(cvinfo_path, issue_number)

            elif provider_type == 'comicvine':
                metadata, img_url, volume_data, selection_data = _try_comicvine_single(
                    cvinfo_path, series_name, issue_number, year
                )
                if selection_data:
                    # Pause cascade - need user selection
                    selection_data["parsed_filename"] = {
                        "series_name": series_name,
                        "issue_number": issue_number,
                        "year": year
                    }
                    app_logger.info(f"[search-metadata] {provider_type} requires selection for {file_name}")
                    return jsonify(selection_data)

            elif provider_type == 'gcd':
                metadata, _, selection_data = _try_gcd_single(series_name, issue_number, year)
                if selection_data:
                    selection_data["parsed_filename"] = {
                        "series_name": series_name,
                        "issue_number": issue_number,
                        "year": year
                    }
                    app_logger.info(f"[search-metadata] {provider_type} requires selection for {file_name}")
                    return jsonify(selection_data)

            elif provider_type in ('anilist', 'mangadex'):
                # Not yet implemented for single-file cascade
                app_logger.info(f"[search-metadata] {provider_type} not yet implemented for single-file search")
                continue

            if metadata:
                app_logger.info(f"[search-metadata] {provider_type} returned metadata for {file_name}")

                # Apply metadata to file
                comicinfo_xml = generate_comicinfo_xml(metadata)
                add_comicinfo_to_cbz(file_path, comicinfo_xml)
                set_has_comicinfo(file_path)

                # Auto-move if enabled and we have volume data
                new_file_path = None
                if volume_data:
                    try:
                        new_file_path = comicvine.auto_move_file(file_path, volume_data, current_app.config)
                    except Exception as move_error:
                        app_logger.error(f"[search-metadata] Auto-move failed: {move_error}")

                response_data = {
                    "success": True,
                    "source": provider_type,
                    "metadata": metadata,
                    "image_url": img_url,
                    "rename_config": {
                        "enabled": current_app.config.get("ENABLE_CUSTOM_RENAME", False),
                        "pattern": current_app.config.get("CUSTOM_RENAME_PATTERN", ""),
                        "auto_rename": current_app.config.get("ENABLE_AUTO_RENAME", False)
                    }
                }

                if new_file_path:
                    response_data["moved"] = True
                    response_data["new_file_path"] = new_file_path
                    log_file_if_in_data(new_file_path)
                    invalidate_cache_for_path(os.path.dirname(file_path))
                    invalidate_cache_for_path(os.path.dirname(new_file_path))
                    update_index_on_move(file_path, new_file_path)

                return jsonify(response_data)

            app_logger.info(f"[search-metadata] {provider_type} found no results, trying next provider")

        # All providers exhausted
        app_logger.info(f"[search-metadata] No metadata found from any provider for {file_name}")
        return jsonify({"success": False, "error": "No metadata found from any provider"}), 404

    except Exception as e:
        if metron.is_connection_error(e):
            app_logger.warning(f"[search-metadata] Metron unavailable: {e}")
            return jsonify({"success": False, "error": "Metron is currently unavailable. Please try again later."}), 503
        app_logger.error(f"[search-metadata] Error: {e}")
        app_logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500


# =============================================================================
# ComicVine Metadata Search
# =============================================================================

@metadata_bp.route('/search-comicvine-metadata', methods=['POST'])
def search_comicvine_metadata():
    """Search ComicVine API for comic metadata and add to CBZ file"""
    from app import log_file_if_in_data, invalidate_cache_for_path, update_index_on_move
    try:
        app_logger.info(f"🔍 ComicVine search started")

        try:
            app_logger.debug("DEBUG: comicvine module imported successfully")
        except ImportError as import_err:
            app_logger.error(f"Failed to import models.comicvine module: {str(import_err)}")
            return jsonify({
                "success": False,
                "error": f"ComicVine module import error: {str(import_err)}"
            }), 500

        data = request.get_json()
        app_logger.info(f"ComicVine Request data: {data}")

        file_path = data.get('file_path')
        file_name = data.get('file_name')

        if not file_path or not file_name:
            return jsonify({
                "success": False,
                "error": "Missing file_path or file_name"
            }), 400

        # Check if ComicVine API key is configured
        api_key = current_app.config.get("COMICVINE_API_KEY", "").strip()
        app_logger.debug(f"DEBUG: ComicVine API key configured: {bool(api_key)}")
        app_logger.debug(f"DEBUG: API key value (first 10 chars): {api_key[:10] if api_key else 'EMPTY'}")
        app_logger.debug(f"DEBUG: All COMICVINE config keys in current_app.config: {[k for k in current_app.config.keys() if 'COMIC' in k.upper()]}")

        # Also check the raw config file
        from config import config as raw_config
        raw_key = raw_config.get("SETTINGS", "COMICVINE_API_KEY", fallback="")
        app_logger.debug(f"DEBUG: Raw config.ini value (first 10 chars): {raw_key[:10] if raw_key else 'EMPTY'}")

        if not api_key:
            app_logger.error("ComicVine API key not configured")
            return jsonify({
                "success": False,
                "error": "ComicVine API key not configured. Please add your API key in Settings."
            }), 400

        # Check if Simyan library is available
        app_logger.debug(f"DEBUG: Checking if Simyan is available...")
        if not comicvine.is_simyan_available():
            app_logger.error("Simyan library not available")
            return jsonify({
                "success": False,
                "error": "Simyan library not installed. Please install it with: pip install simyan"
            }), 500
        app_logger.debug(f"DEBUG: Simyan library is available")

        # Check for cvinfo file in parent folder - can skip volume search if found
        folder_path = os.path.dirname(file_path)
        cvinfo_path = comicvine.find_cvinfo_in_folder(folder_path)

        if cvinfo_path:
            app_logger.info(f"Found cvinfo file at {cvinfo_path}")

            # Extract issue number from filename (handles extension removal internally)
            issue_number = comicvine.extract_issue_number(file_name)
            name_without_ext = os.path.splitext(file_name)[0]
            if not issue_number:
                issue_number = "1"  # Default for graphic novels/one-shots

            # Extract year from filename if present
            year_match = re.search(r'\((\d{4})\)', name_without_ext)
            year = int(year_match.group(1)) if year_match else None

            # Try ComicVine with volume ID from cvinfo
            cv_volume_id = comicvine.parse_cvinfo_volume_id(cvinfo_path)

            if cv_volume_id:
                app_logger.info(f"Using ComicVine volume ID {cv_volume_id} from cvinfo")
                issue_data = comicvine.get_issue_by_number(api_key, cv_volume_id, issue_number, year)

                if issue_data:
                    app_logger.info(f"Found issue #{issue_number} using cvinfo volume ID")

                    # Create minimal volume_data for mapping
                    volume_data = {
                        'id': cv_volume_id,
                        'name': issue_data.get('volume_name', ''),
                        'start_year': issue_data.get('year'),
                        'publisher_name': issue_data.get('publisher_name', '')
                    }

                    # Map to ComicInfo format
                    comicinfo_data = comicvine.map_to_comicinfo(issue_data, volume_data)

                    # Generate ComicInfo.xml
                    comicinfo_xml = generate_comicinfo_xml(comicinfo_data)

                    # Add ComicInfo.xml to the CBZ file
                    add_comicinfo_to_cbz(file_path, comicinfo_xml)
                    from database import set_has_comicinfo
                    set_has_comicinfo(file_path)

                    # Auto-move file if enabled
                    new_file_path = None
                    try:
                        new_file_path = comicvine.auto_move_file(file_path, volume_data, app.config)
                    except Exception as move_error:
                        app_logger.error(f"Auto-move failed but metadata was added successfully: {str(move_error)}")

                    # Get image URL
                    img_url = issue_data.get('image_url')
                    if img_url and not isinstance(img_url, str):
                        img_url = str(img_url)

                    response_data = {
                        "success": True,
                        "metadata": comicinfo_data,
                        "image_url": img_url,
                        "source": "comicvine_cvinfo",
                        "volume_info": {
                            "id": cv_volume_id,
                            "name": volume_data.get('name', ''),
                            "start_year": volume_data.get('start_year')
                        },
                        "rename_config": {
                            "enabled": current_app.config.get("ENABLE_CUSTOM_RENAME", False),
                            "pattern": current_app.config.get("CUSTOM_RENAME_PATTERN", ""),
                            "auto_rename": current_app.config.get("ENABLE_AUTO_RENAME", False)
                        }
                    }

                    if new_file_path:
                        response_data["moved"] = True
                        response_data["new_file_path"] = new_file_path
                        log_file_if_in_data(new_file_path)
                        invalidate_cache_for_path(os.path.dirname(file_path))
                        invalidate_cache_for_path(os.path.dirname(new_file_path))
                        update_index_on_move(file_path, new_file_path)

                    return jsonify(response_data)
                else:
                    app_logger.info(f"Issue #{issue_number} not found using cvinfo, falling back to volume search")

        # Parse series name and issue from filename (reuse GCD parsing logic)
        name_without_ext = file_name
        for ext in ('.cbz', '.cbr', '.zip'):
            name_without_ext = name_without_ext.replace(ext, '')

        # Try to parse series and issue from common formats
        series_name = None
        issue_number = None
        year = None

        patterns = [
            r'^(.+?)\s+(\d{3,4})\s+\((\d{4})\)',  # "Series 001 (2020)"
            r'^(.+?)\s+#?(\d{1,4})\s*\((\d{4})\)', # "Series #1 (2020)" or "Series 1 (2020)"
            r'^(.+?)\s+v\d+\s+(\d{1,4})\s*\((\d{4})\)', # "Series v1 001 (2020)"
            r'^(.+?)\s+(\d{1,4})\s+\(of\s+\d+\)\s+\((\d{4})\)', # "Series 05 (of 12) (2020)"
            r'^(.+?)\s+#?(\d{1,4})$',  # "Series 169" or "Series #169" (no year)
        ]

        for pattern in patterns:
            match = re.match(pattern, name_without_ext, re.IGNORECASE)
            if match:
                series_name = match.group(1).strip()
                issue_number = str(int(match.group(2)))  # Convert to int then back to string to remove leading zeros
                year = int(match.group(3)) if len(match.groups()) >= 3 else None
                app_logger.debug(f"DEBUG: File parsed - series_name={series_name}, issue_number={issue_number}, year={year}")
                break

        # If no pattern matched, try to parse as single-issue/graphic novel with just year
        if not series_name:
            single_issue_pattern = r'^(.+?)\s*\((\d{4})\)$'
            match = re.match(single_issue_pattern, name_without_ext, re.IGNORECASE)
            if match:
                series_name = match.group(1).strip()
                year = int(match.group(2))
                issue_number = "1"
                app_logger.debug(f"DEBUG: Single-issue/graphic novel parsed - series_name={series_name}, year={year}, issue_number={issue_number}")

        # Ultimate fallback: use entire filename as series name
        if not series_name:
            series_name = name_without_ext.strip()
            issue_number = "1"
            app_logger.debug(f"DEBUG: Fallback parsing - using entire filename as series_name={series_name}, issue_number={issue_number}")

        if not series_name or not issue_number:
            return jsonify({
                "success": False,
                "error": f"Could not parse series name from: {name_without_ext}"
            }), 400

        # Normalize series name for searching - remove special characters
        normalized_series = re.sub(r'[:\-–—\'\"\.\,\!\?]', ' ', series_name)
        normalized_series = re.sub(r'\s+', ' ', normalized_series).strip()

        # Search ComicVine for volumes using normalized name
        app_logger.info(f"Searching ComicVine for '{normalized_series}' (original: '{series_name}') issue #{issue_number}")
        volumes = comicvine.search_volumes(api_key, normalized_series, year)

        if not volumes:
            return jsonify({
                "success": False,
                "error": f"No volumes found matching '{series_name}' in ComicVine"
            }), 404

        # Check if we have a confident match (all search words present in a single result)
        search_words = set(normalized_series.lower().split())
        confident_match = None

        if len(volumes) > 1:
            # Look for a volume that contains all search words
            for volume in volumes:
                volume_name_lower = volume['name'].lower()
                if all(word in volume_name_lower for word in search_words):
                    confident_match = volume
                    app_logger.info(f"Confident match found: '{volume['name']}' contains all search words: {search_words}")
                    break

        # If we have a confident match, use it; otherwise show modal for multiple volumes
        if confident_match:
            selected_volume = confident_match
            app_logger.info(f"Auto-selected confident match: {selected_volume['name']} ({selected_volume['start_year']})")
        elif len(volumes) > 1:
            # Multiple volumes and no confident match - show selection modal
            return jsonify({
                "success": False,
                "requires_selection": True,
                "parsed_filename": {
                    "series_name": series_name,
                    "issue_number": issue_number,
                    "year": year
                },
                "possible_matches": volumes,
                "message": f"Found {len(volumes)} volume(s). Please select the correct one."
            }), 200
        else:
            # Single volume - auto-select
            selected_volume = volumes[0]
            app_logger.info(f"Auto-selected single volume: {selected_volume['name']} ({selected_volume['start_year']})")

        # Get the issue
        issue_data = comicvine.get_issue_by_number(api_key, selected_volume['id'], issue_number, year)

        if not issue_data:
            return jsonify({
                "success": False,
                "error": f"Issue #{issue_number} not found in volume '{selected_volume['name']}'"
            }), 404

        # Map to ComicInfo format
        comicinfo_data = comicvine.map_to_comicinfo(issue_data, selected_volume)

        # Generate ComicInfo.xml
        comicinfo_xml = generate_comicinfo_xml(comicinfo_data)

        # Add ComicInfo.xml to the CBZ file
        add_comicinfo_to_cbz(file_path, comicinfo_xml)
        from database import set_has_comicinfo
        set_has_comicinfo(file_path)

        # Auto-move file if enabled
        new_file_path = None
        try:
            new_file_path = comicvine.auto_move_file(file_path, selected_volume, app.config)
        except Exception as move_error:
            app_logger.error(f"Auto-move failed but metadata was added successfully: {str(move_error)}")
            # Continue execution - metadata was added successfully even if move failed

        # Return success with metadata and rename configuration
        # Ensure image_url is a string (Pydantic HttpUrl isn't JSON serializable)
        img_url = issue_data.get('image_url')
        if img_url and not isinstance(img_url, str):
            img_url = str(img_url)

        response_data = {
            "success": True,
            "metadata": comicinfo_data,
            "image_url": img_url,
            "volume_info": {
                "id": selected_volume['id'],
                "name": selected_volume['name'],
                "start_year": selected_volume['start_year']
            },
            "rename_config": {
                "enabled": current_app.config.get("ENABLE_CUSTOM_RENAME", False),
                "pattern": current_app.config.get("CUSTOM_RENAME_PATTERN", ""),
                "auto_rename": current_app.config.get("ENABLE_AUTO_RENAME", False)
            }
        }

        # Add new file path to response if file was moved
        if new_file_path:
            response_data["moved"] = True
            response_data["new_file_path"] = new_file_path
            app_logger.info(f"✅ File moved to: {new_file_path}")

            # Update database caches and file index for the moved file
            log_file_if_in_data(new_file_path)
            invalidate_cache_for_path(os.path.dirname(file_path))
            invalidate_cache_for_path(os.path.dirname(new_file_path))
            update_index_on_move(file_path, new_file_path)

        return jsonify(response_data)

    except Exception as e:
        app_logger.error(f"Error in ComicVine search: {str(e)}")
        app_logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500



# =============================================================================
# ComicVine Metadata With Selection
# =============================================================================

@metadata_bp.route('/search-comicvine-metadata-with-selection', methods=['POST'])
def search_comicvine_metadata_with_selection():
    """Search ComicVine using user-selected volume"""
    from app import log_file_if_in_data, invalidate_cache_for_path, update_index_on_move
    try:
        data = request.get_json()
        file_path = data.get('file_path')
        file_name = data.get('file_name')
        volume_id = data.get('volume_id')
        publisher_name = data.get('publisher_name')
        issue_number = data.get('issue_number')
        year = data.get('year')

        app_logger.debug(f"DEBUG: search_comicvine_metadata_with_selection called - file={file_name}, volume_id={volume_id}, publisher={publisher_name}, issue={issue_number}")

        # Note: issue_number can be 0, so check for None explicitly
        if not file_path or not file_name or volume_id is None or issue_number is None:
            app_logger.error(f"ERROR: Missing required parameters - file_path={file_path}, file_name={file_name}, volume_id={volume_id}, issue_number={issue_number}")
            return jsonify({
                "success": False,
                "error": "Missing required parameters"
            }), 400

        # Check if ComicVine API key is configured
        api_key = current_app.config.get("COMICVINE_API_KEY", "").strip()
        if not api_key:
            return jsonify({
                "success": False,
                "error": "ComicVine API key not configured"
            }), 400

        # Get the issue
        issue_data = comicvine.get_issue_by_number(api_key, volume_id, str(issue_number), year)

        if not issue_data:
            return jsonify({
                "success": False,
                "error": f"Issue #{issue_number} not found in selected volume"
            }), 404

        # Create volume_data dict with the volume ID and publisher for metadata
        # Also include name and start_year for auto-move functionality
        volume_data = {
            'id': volume_id,
            'publisher_name': publisher_name,
            'name': issue_data.get('volume_name'),  # Series name from issue data
            'start_year': issue_data.get('year')  # Use issue year as fallback for start_year
        }

        # Map to ComicInfo format
        comicinfo_data = comicvine.map_to_comicinfo(issue_data, volume_data)

        # Generate ComicInfo.xml
        comicinfo_xml = generate_comicinfo_xml(comicinfo_data)

        # Add ComicInfo.xml to the CBZ file
        add_comicinfo_to_cbz(file_path, comicinfo_xml)
        from database import set_has_comicinfo
        set_has_comicinfo(file_path)

        # Auto-move file if enabled
        new_file_path = None
        try:
            new_file_path = comicvine.auto_move_file(file_path, volume_data, app.config)
        except Exception as move_error:
            app_logger.error(f"Auto-move failed but metadata was added successfully: {str(move_error)}")
            # Continue execution - metadata was added successfully even if move failed

        # Return success with metadata and rename configuration
        # Ensure image_url is a string (Pydantic HttpUrl isn't JSON serializable)
        img_url = issue_data.get('image_url')
        if img_url and not isinstance(img_url, str):
            img_url = str(img_url)

        response_data = {
            "success": True,
            "metadata": comicinfo_data,
            "image_url": img_url,
            "rename_config": {
                "enabled": current_app.config.get("ENABLE_CUSTOM_RENAME", False),
                "pattern": current_app.config.get("CUSTOM_RENAME_PATTERN", ""),
                "auto_rename": current_app.config.get("ENABLE_AUTO_RENAME", False)
            }
        }

        # Add new file path to response if file was moved
        if new_file_path:
            response_data["moved"] = True
            response_data["new_file_path"] = new_file_path
            app_logger.info(f"✅ File moved to: {new_file_path}")

            # Update database caches and file index for the moved file
            log_file_if_in_data(new_file_path)
            invalidate_cache_for_path(os.path.dirname(file_path))
            invalidate_cache_for_path(os.path.dirname(new_file_path))
            update_index_on_move(file_path, new_file_path)

        return jsonify(response_data)

    except Exception as e:
        app_logger.error(f"Error in ComicVine search with selection: {str(e)}")
        app_logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500
