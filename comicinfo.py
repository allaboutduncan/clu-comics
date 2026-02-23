import os
import sys
import re
import zipfile
import xml.etree.ElementTree as ET
import defusedxml.ElementTree as SafeET
from app_logging import app_logger
from config import config, load_config

load_config()

ignore = config.get("SETTINGS", "IGNORED_FILES", fallback=".DS_Store,cover.jpg")
xml_year = config.getboolean("SETTINGS", "XML_YEAR", fallback=False)
xml_markdown = config.getboolean("SETTINGS", "XML_MARKDOWN", fallback=False)
xml_list = config.getboolean("SETTINGS", "XML_LIST", fallback=False)

app_logger.info(f"************* XML Settings *************")
app_logger.info(f"Markdown: {xml_markdown}")
app_logger.info(f"List: {xml_list}")


#########################
#   Helper Functions    #
#########################

def clean_markdown(md_text: str) -> str:
    """
    Removes all Markdown headings (lines starting with '#'),
    table lines (lines containing '|'), and bold text ('**...**' or '__...__')
    from the given Markdown text.

    :param md_text: The original Markdown text (string).
    :return:        The cleaned text with table lines, bold text, and headers removed.
    """
    # Split into individual lines
    lines = md_text.splitlines()
    cleaned_lines = []

    # Regex to remove bold text (including the bolded content)
    remove_bold_pattern = re.compile(r'(\*\*.*?\*\*)|(__.*?__)')

    for line in lines:
        # Trim leading whitespace to check heading more reliably
        stripped_line = line.lstrip()

        # Skip if it's a heading line (starts with one or more '#')
        if stripped_line.startswith('#'):
            continue

        # Skip if the line looks like a table line (contains '|')
        if '|' in line:
            continue

        # Remove bold text entirely
        line_no_bold = remove_bold_pattern.sub('', line)

        # Append the cleaned line
        cleaned_lines.append(line_no_bold)

    # Join lines back and strip extra whitespace
    return "\n".join(cleaned_lines).strip()


def clean_markdown_list(md_text: str) -> str:
    """
    Removes blocks of text starting with '*List' and any following tables,
    regardless of whether there's a blank line between them.
    
    A table is considered part of '*List' if it directly follows '*List'
    or if there's a blank line in between.

    :param md_text: The original Markdown text (string).
    :return:        The cleaned text with '*List' sections and associated tables removed.
    """
    lines = md_text.splitlines()
    cleaned_lines = []
    removing_list_block = False

    for i, line in enumerate(lines):
        stripped_line = line.lstrip()

        # Detect start of '*List' block
        if stripped_line.startswith('*List'):
            removing_list_block = True
            continue  # Skip this line
        
        # If removing, check for table structure (lines containing '|')
        if removing_list_block:
            # Remove tables (lines containing '|') or blank lines in between
            if stripped_line == "" or '|' in stripped_line:
                continue
            else:
                removing_list_block = False  # Stop removing if normal text appears
        
        # Append the line if it's not part of the removed section
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()



def get_year_from_file(directory):
    """
    Returns the 4-digit year from the first file (sorted alphanumerically)
    in the given directory. Assumes filenames have a format like:
        
    If no match is found, returns None.
    """
    ignore_list_str=ignore
    # Parse the comma-separated ignore list into a set for faster lookups
    ignore_list = {ignore_name.strip() for ignore_name in ignore_list_str.split(',') if ignore_name.strip()}
    app_logger.info(f"Ignore these files: {ignore_list}")

    # Get all entries from the directory
    entries = os.listdir(directory)
    
    # Filter to include only files, and exclude ignored filenames
    files = [
        f for f in entries 
        if os.path.isfile(os.path.join(directory, f)) and f not in ignore_list
    ]
    
    # If there are no files left after filtering, return None
    if not files:
        return None
    
    # Sort files alphanumerically
    files.sort()
    
    # Take the first file
    first_file = files[0]
    app_logger.info(f"First File: {first_file}")
    
    # Use regex to find a 4-digit year pattern in parentheses
    match = re.search(r'\((\d{4})\)', first_file)
    if match:
        file_year = match.group(1)
        return file_year
    else:
        return None

#########################
#     XML Functions     #
#########################

def _sanitize_xml(xml_data: bytes) -> bytes:
    """
    Sanitize XML data by removing/fixing common issues that cause parse errors.

    :param xml_data: Raw XML bytes
    :return: Sanitized XML bytes
    """
    try:
        # Decode to string for manipulation
        xml_str = xml_data.decode('utf-8', errors='ignore')

        # Remove invalid XML characters (control characters except tab, newline, carriage return)
        # Valid XML chars: #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]
        import re
        # This regex removes invalid XML 1.0 characters
        xml_str = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x84\x86-\x9F]', '', xml_str)

        # Fix common XML escape issues
        # First, unescape any already-escaped entities to avoid double-escaping
        xml_str = xml_str.replace('&amp;', '&')
        xml_str = xml_str.replace('&lt;', '<')
        xml_str = xml_str.replace('&gt;', '>')
        xml_str = xml_str.replace('&quot;', '"')
        xml_str = xml_str.replace('&apos;', "'")

        # Now we need to escape only the ampersands that are not part of entity references
        # This is tricky - we'll escape all ampersands except those followed by valid entity patterns
        # Valid entities: &amp; &lt; &gt; &quot; &apos; &#nnn; &#xhh;
        def escape_ampersand(match):
            text = match.group(0)
            # Don't escape if it's already a valid entity reference
            if re.match(r'&(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);', text):
                return text
            return '&amp;'

        # Find all ampersands and escape those that aren't part of entities
        # But to keep it simple and avoid complex regex, let's just escape & that appear in text content
        # We need to be careful not to break existing valid XML structure

        # Split by tags and only fix text content between tags
        parts = re.split(r'(<[^>]+>)', xml_str)
        for i in range(len(parts)):
            # Only process text content (not tags)
            if not parts[i].startswith('<'):
                # Escape special characters in text content
                parts[i] = parts[i].replace('&', '&amp;')
                parts[i] = parts[i].replace('<', '&lt;')
                parts[i] = parts[i].replace('>', '&gt;')

        xml_str = ''.join(parts)

        return xml_str.encode('utf-8')
    except Exception as e:
        app_logger.error(f"Error sanitizing XML: {e}")
        return xml_data  # Return original if sanitization fails


def read_comicinfo_xml(xml_data: bytes) -> dict:
    """
    Parse the raw bytes of a ComicInfo.xml file and return a dictionary
    of element.tag -> element.text for each child node.

    :param xml_data: Bytes of the original ComicInfo.xml content.
    :return:         Dictionary containing the XML tags and their text values.
    """
    try:
        root = SafeET.fromstring(xml_data)
        data = {}

        # Handle both namespaced and non-namespaced XML
        # Remove namespace prefixes from tag names for consistency
        for child in root:
            # Get the tag name without namespace
            tag_name = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            data[tag_name] = child.text if child.text else ""

        return data
    except ET.ParseError as e:
        app_logger.error(f"XML parsing error: {e}")
        # Try to sanitize and parse again
        try:
            app_logger.info("Attempting to sanitize and re-parse XML...")
            sanitized_xml = _sanitize_xml(xml_data)
            root = SafeET.fromstring(sanitized_xml)
            data = {}

            for child in root:
                tag_name = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                data[tag_name] = child.text if child.text else ""

            app_logger.info("Successfully parsed XML after sanitization")
            return data
        except Exception as sanitize_error:
            app_logger.error(f"Failed to parse XML even after sanitization: {sanitize_error}")
            return {}
    except Exception as e:
        app_logger.error(f"Unexpected error parsing ComicInfo.xml: {e}")
        return {}


def read_comicinfo_from_zip(zip_path: str) -> dict:
    """
    Reads ComicInfo.xml from a .zip or .cbz file and returns the parsed data as a dict.
    If ComicInfo.xml does not exist, returns an empty dict.

    :param zip_path: Path to the .zip or .cbz file.
    :return:         Dictionary of ComicInfo.xml data (tags -> text).
    """
    _, ext = os.path.splitext(zip_path)
    if ext.lower() not in ['.zip', '.cbz']:
        raise ValueError("Only .zip or .cbz files are supported by this function.")

    try:
        with zipfile.ZipFile(zip_path, 'r') as z:
            xml_data = z.read("ComicInfo.xml")
            return read_comicinfo_xml(xml_data)
    except KeyError:
        # "ComicInfo.xml" not found inside the archive
        return {}


def update_comicinfo_xml(xml_data: bytes, updates: dict) -> bytes:
    """
    Given the raw bytes of a ComicInfo.xml file (xml_data) and a dict (updates),
    parse and update the specified tags, then return the updated XML as bytes.

    :param xml_data: Bytes of the original ComicInfo.xml content.
    :param updates:  Dict of XML tag -> new value, e.g. {'Title': 'My New Title'}.
    :return:         Updated XML bytes.
    """
    try:
        root = SafeET.fromstring(xml_data)
    except ET.ParseError as e:
        app_logger.error(f"XML parsing error in update_comicinfo_xml: {e}")
        app_logger.info("Attempting to sanitize XML before updating...")
        try:
            sanitized_xml = _sanitize_xml(xml_data)
            root = SafeET.fromstring(sanitized_xml)
            app_logger.info("Successfully parsed XML after sanitization")
        except Exception as sanitize_error:
            app_logger.error(f"Failed to parse XML even after sanitization: {sanitize_error}")
            raise  # Re-raise to let caller handle it

    # Update or add the specified tags
    for tag, new_value in updates.items():
        elem = root.find(tag)
        if elem is not None:
            elem.text = new_value
        else:
            ET.SubElement(root, tag).text = new_value

    # Convert the updated XML back into bytes (with XML declaration)
    updated_xml_bytes = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    return updated_xml_bytes


def update_comicinfo_in_zip(zip_path: str, updates: dict):
    """
    Updates the 'ComicInfo.xml' entry in a ZIP or CBZ without extracting
    all files to disk. Internally, this still rebuilds the ZIP because
    in-place edits aren't supported by the ZIP format.

    :param zip_path: Path to the .zip or .cbz file.
    :param updates:  Dict of XML tag -> new value, e.g. {'Title': 'Updated Title'}.
    """
    _, ext = os.path.splitext(zip_path)
    if ext.lower() not in ['.zip', '.cbz']:
        raise ValueError("Only .zip or .cbz files are supported by this function.")
    
    temp_zip_path = zip_path + ".tmpzip"

    with zipfile.ZipFile(zip_path, 'r') as old_zip, \
         zipfile.ZipFile(temp_zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as new_zip:

        for item in old_zip.infolist():
            if item.filename == "ComicInfo.xml":
                # 1. Read the XML data from the old zip
                xml_data = old_zip.read(item.filename)

                # 2. Pass it to our separate function for updates
                updated_xml_data = update_comicinfo_xml(xml_data, updates)

                # 3. Write the updated file into the new zip
                new_zip.writestr(item, updated_xml_data)
            else:
                # Copy all other files as-is
                new_zip.writestr(item, old_zip.read(item.filename))

    # Replace the original ZIP/CBZ with the updated one
    os.replace(temp_zip_path, zip_path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        app_logger.error("No directory provided!")
    else:
        directory = sys.argv[1]
    
    # Get the year from first file
    file_year = get_year_from_file(directory)
    app_logger.info(f"Year from First File is: {file_year}")
    
    # Loop over all files in the specified directory.
    # For each .zip or .cbz, read the current info and update it.
    for filename in os.listdir(directory):
        if filename.lower().endswith(('.zip', '.cbz')):
            comic_path = os.path.join(directory, filename)

            # 1) Read the current ComicInfo.xml data
            info_before = read_comicinfo_from_zip(comic_path)
            app_logger.info(f"Processing: {filename}")
            comments = info_before.get("Comments", "Not found")
            volume = file_year
            app_logger.info("Before update:")
            app_logger.info(f"Comments: {comments}")
            app_logger.info(f"Volume: {volume}")

            # 2) If you want to mix in existing fields (e.g., partially preserve "Comments"),
            #    you can incorporate them here. For example, to keep old Comments + new:
            # xml_updates["Comments"] = (
            #     info_before.get("Comments", "") + "\n---\n" + "Your appended text"
            # )

            # Or if you wanted to apply clean_markdown() on the existing Comments:
            # if "Comments" in info_before:
            #     old_comments = info_before["Comments"]
            #     xml_updates["Comments"] = clean_markdown(old_comments)
            #################################################################################
            # Any fields you want to update go in this dictionary
            # e.g., you could replace "Comments" with a new cleaned markdown,
            # change "Volume" values, or anything else the ComicInfo.xml has.    
                    
            # Initialize xml_updates
            xml_updates = {}

            # Update xml_updates based on settings
            if xml_year:
                xml_updates["Volume"] = file_year

            if xml_markdown and not xml_list:
                xml_updates["Comments"] = clean_markdown(comments)
            elif xml_list:
                xml_updates["Comments"] = clean_markdown_list(comments)

            # 3) Update the CBZ with new values
            update_comicinfo_in_zip(comic_path, xml_updates)
            
            # 4) Read again to verify
            info_after = read_comicinfo_from_zip(comic_path)
            comments = info_after.get("Comments", "")
            volume = info_after.get("Volume", "")
            app_logger.info("After update:")
            app_logger.info(f"Comments: {comments}")
            app_logger.info(f"Volume: {volume}")
            app_logger.info("-" * 50)

    app_logger.info("\nAll .cbz/.zip files in the directory have been processed.")
