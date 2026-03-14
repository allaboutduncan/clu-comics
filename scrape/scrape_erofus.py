import os
import re
import time
import zipfile
import shutil
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from core.app_logging import app_logger

BASE = "https://www.erofus.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# HTTP session for downloads
session = requests.Session()
retries = Retry(total=5, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retries))
session.headers.update({"User-Agent": UA})

def safe_title(text: str) -> str:
    """Sanitize title for filesystem"""
    t = ' '.join((text or 'comic').split())
    return re.sub(r'[<>:"/\\|?*\r\n]', '', t).strip() or "comic"

def get_series_links(publisher_url: str, log_callback=None) -> list[str]:
    """Get all series links from a publisher page"""
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    log(f"Fetching series links from: {publisher_url}")
    r = session.get(publisher_url, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Extract base path from publisher URL (e.g., /comics/brainstorm-comics)
    parsed = urlparse(publisher_url)
    publisher_path = parsed.path.rstrip("/")
    log(f"Publisher path: {publisher_path}")

    # Find all series links that start with the publisher path
    # Pattern: /comics/[publisher-slug]/[series-slug]
    series_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Check if this is a series link (starts with publisher path and has one more segment)
        if href.startswith(publisher_path + "/"):
            # Count path segments after publisher path
            relative_path = href[len(publisher_path):].strip("/")
            # Series links have exactly one more segment (no /issue- suffix)
            if relative_path and "/" not in relative_path and "/issue-" not in relative_path.lower():
                full_url = urljoin(BASE, href)
                series_links.append(full_url)

    # Remove duplicates and sort
    series_links = sorted(set(series_links))
    log(f"Found {len(series_links)} series links")
    if len(series_links) > 0:
        log(f"First 3 series: {series_links[:3]}")
    return series_links

def get_issue_links(series_url: str, log_callback=None) -> list[str]:
    """Get all issue links from a series page

    Returns a list of issue URLs. If the series is a single-issue comic (no /issue- links),
    returns the series URL itself as the only issue.
    """
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    r = session.get(series_url, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Extract base path from series URL (e.g., /eros-comix/elizabeth-bathory)
    parsed = urlparse(series_url)
    series_path = parsed.path.rstrip("/")

    # Find all issue links that contain the series path
    issue_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Check if this is an issue link (contains series path + /issue-)
        if series_path in href and "/issue-" in href.lower():
            full_url = urljoin(BASE, href)
            issue_links.append(full_url)

    # If no issue links found, check if this is a single-issue comic
    if not issue_links:
        # Look for page links to confirm this is a direct comic page
        page_links = soup.find_all("a", href=re.compile(r'^/pic/'), class_="a-click")
        if page_links:
            log("Detected single-issue comic (no /issue- links, but has page links)")
            return [series_url]
        else:
            log("No issue links or page links found")
            return []

    # Remove duplicates and sort
    issue_links = sorted(set(issue_links))
    log(f"Found {len(issue_links)} issue links")
    return issue_links

def get_page_links(issue_url: str, log_callback=None) -> list[str]:
    """Get all page links from an issue page"""
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    r = session.get(issue_url, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Extract series and issue names from URL
    # e.g., /comics/eurotica-comics/nina/issue-1 -> eurotica-comics/nina/issue-1
    parsed = urlparse(issue_url)
    path_parts = parsed.path.strip("/").split("/")

    # Remove 'comics' prefix if present to get the series/issue identifier
    if path_parts[0] == "comics":
        path_parts = path_parts[1:]

    # Create identifier pattern (e.g., "eurotica-comics/nina/issue-1")
    identifier = "/".join(path_parts)

    # Find all page links that start with /pic/ and contain the identifier
    page_links = []
    for a in soup.find_all("a", href=True, class_="a-click"):
        href = a["href"]
        # Check if this is a page link (/pic/.../series/issue/page-number)
        if href.startswith("/pic/") and identifier in href and re.search(r'/\d+$', href):
            full_url = urljoin(BASE, href)
            page_links.append(full_url)

    # Remove duplicates and sort by page number
    page_links = sorted(set(page_links), key=lambda x: int(x.split('/')[-1]))
    log(f"Found {len(page_links)} page links")
    return page_links

def get_image_url(page_url: str, log_callback=None) -> str:
    """Extract the medium image URL from a page"""
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    r = session.get(page_url, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Find the medium image in .picture-full-container
    container = soup.find("div", class_="picture-full-container")
    if container:
        img = container.find("img", src=re.compile(r'/medium/'))
        if img and img.get("src"):
            return urljoin(BASE, img["src"])

    # Fallback: search anywhere for medium image
    img = soup.find("img", src=re.compile(r'/medium/'))
    if img and img.get("src"):
        return urljoin(BASE, img["src"])

    log(f"Warning: No medium image found on {page_url}")
    return None

def download_image(url: str, output_path: str, log_callback=None) -> bool:
    """Download image using requests"""
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    try:
        with session.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in r.iter_content(32768):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as e:
        log(f"Download failed: {str(e)[:60]}")
        return False

def create_cbz(folder: str, log_callback=None):
    """Create CBZ file from folder with unique naming to prevent overwrites"""
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    # Generate base CBZ path
    cbz = f"{folder}.cbz"

    # Check if file already exists and add counter if needed
    if os.path.exists(cbz):
        base_path = folder
        counter = 1
        while os.path.exists(cbz):
            cbz = f"{base_path}_({counter}).cbz"
            counter += 1
        log(f"File already exists, using unique name: {os.path.basename(cbz)}")

    with zipfile.ZipFile(cbz, "w", zipfile.ZIP_DEFLATED) as z:
        for fname in sorted(os.listdir(folder)):
            z.write(os.path.join(folder, fname), arcname=fname)
    shutil.rmtree(folder, ignore_errors=True)
    log(f"Created {cbz}")
    return cbz

def cleanup_empty_folder(folder_path, log_callback=None):
    """Remove folder if it exists and is empty or only contains temp files"""
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    try:
        if os.path.exists(folder_path) and os.path.isdir(folder_path):
            files = [f for f in os.listdir(folder_path) if not f.startswith('.')]
            if len(files) == 0:
                shutil.rmtree(folder_path, ignore_errors=True)
                log(f"Cleaned up empty folder: {os.path.basename(folder_path)}")
    except Exception as e:
        app_logger.warning(f"Could not cleanup folder {folder_path}: {e}")

def scrape_issue(issue_url: str, output_dir: str = None, log_callback=None, progress_callback=None):
    """Scrape a single issue"""
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    def update_progress(data):
        if progress_callback:
            progress_callback(data)

    log(f"Scraping issue: {issue_url}")
    update_progress({"status": "Getting page links", "current": issue_url})

    # Get all page links
    page_links = get_page_links(issue_url, log_callback)
    if not page_links:
        log("No pages found")
        return None

    # Extract issue title from URL
    parsed = urlparse(issue_url)
    path_parts = parsed.path.strip("/").split("/")
    issue_name = safe_title(path_parts[-1])  # e.g., "issue-1"
    series_name = safe_title(path_parts[-2]) # e.g., "elizabeth-bathory"
    title = f"{series_name}_{issue_name}"

    # Create folder
    folder = title
    if output_dir:
        folder = os.path.join(output_dir, folder)

    # Ensure unique folder name
    if os.path.exists(folder):
        base_folder = folder
        counter = 1
        while os.path.exists(folder):
            folder = f"{base_folder}_({counter})"
            counter += 1
        log(f"Folder exists, using unique name: {os.path.basename(folder)}")

    os.makedirs(folder, exist_ok=True)
    log(f"Saving to: {os.path.basename(folder)}")

    # Download all pages
    success_count = 0
    for i, page_url in enumerate(page_links, 1):
        log(f"Processing page {i}/{len(page_links)}")
        update_progress({
            "status": "Downloading images",
            "current": f"{i}/{len(page_links)}",
            "progress": (i / len(page_links)) * 90
        })

        # Get image URL
        img_url = get_image_url(page_url, log_callback)
        if not img_url:
            continue

        # Determine extension
        ext = os.path.splitext(img_url.split("?")[0])[-1].lower()
        if not ext or len(ext) > 5:
            ext = ".jpg"

        # Download image
        output_path = os.path.join(folder, f"{i:03d}{ext}")
        if download_image(img_url, output_path, log_callback):
            success_count += 1
            log(f"Downloaded {i}/{len(page_links)}")
        else:
            log(f"Failed {i}/{len(page_links)}")

        time.sleep(0.3)  # Be nice to the server

    # Check if we downloaded anything
    if success_count == 0:
        log("No images downloaded successfully")
        cleanup_empty_folder(folder, log_callback)
        return None

    # Create CBZ
    update_progress({"status": "Creating CBZ", "current": title, "progress": 95})
    cbz_path = create_cbz(folder, log_callback)
    update_progress({"status": "Completed", "current": os.path.basename(cbz_path), "progress": 100})
    return cbz_path

def detect_url_type(url: str, log_callback=None) -> str:
    """Detect if URL is a publisher, series, or issue page

    Returns:
        'publisher' - Publisher page with multiple series
        'series' - Series page with multiple issues
        'issue' - Direct issue page with comic pages
    """
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    try:
        # Check URL structure first
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]

        # Publisher URL pattern: /comics/[publisher-slug] (2 parts)
        # Series URL pattern: /comics/[publisher-slug]/[series-slug] (3 parts)
        # Issue URL pattern: /comics/[publisher-slug]/[series-slug]/issue-X (4 parts with "issue-")

        if len(path_parts) == 2 and path_parts[0] == "comics":
            log("URL structure suggests publisher page")
            # Verify by checking for series links
            r = session.get(url, timeout=25)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            publisher_path = parsed.path.rstrip("/")
            series_count = 0
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith(publisher_path + "/") and "/issue-" not in href.lower():
                    relative_path = href[len(publisher_path):].strip("/")
                    if relative_path and "/" not in relative_path:
                        series_count += 1
                        if series_count > 3:  # Found enough to confirm it's a publisher page
                            break

            if series_count > 0:
                log(f"Detected publisher page (found {series_count}+ series)")
                return 'publisher'

        # Check page content to distinguish between series and issue
        r = session.get(url, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Look for page links (links starting with /pic/)
        page_links = soup.find_all("a", href=re.compile(r'^/pic/'), class_="a-click")

        # If we find page links, this is an issue URL
        if len(page_links) > 0:
            log(f"Detected issue page (found {len(page_links)} page links)")
            return 'issue'

        # Otherwise, it's a series page
        log("Detected series page")
        return 'series'

    except Exception as e:
        log(f"Error detecting URL type: {e}")
        # Default to treating as series page
        return 'series'

def is_issue_url(url: str, log_callback=None) -> bool:
    """Check if URL points directly to issue pages (vs a series page with multiple issues)

    DEPRECATED: Use detect_url_type() instead for better accuracy
    """
    return detect_url_type(url, log_callback) == 'issue'

def scrape_publisher(publisher_url: str, output_dir: str = None, log_callback=None, progress_callback=None):
    """Scrape all series from a publisher page"""
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    def update_progress(data):
        if progress_callback:
            progress_callback(data)

    log(f"Scraping publisher: {publisher_url}")
    update_progress({"status": "Getting series links", "current": publisher_url})

    # Get all series links
    series_links = get_series_links(publisher_url, log_callback)
    if not series_links:
        log("No series found")
        return []

    log(f"Found {len(series_links)} series to scrape")
    results = []
    for idx, series_url in enumerate(series_links, 1):
        try:
            log(f"\n{'='*80}")
            log(f"Processing series {idx}/{len(series_links)}: {series_url}")
            log('='*80)
            update_progress({
                "status": f"Processing series {idx}/{len(series_links)}",
                "current": series_url,
                "progress": (idx - 1) / len(series_links) * 100
            })

            # Scrape all issues from this series
            series_results = scrape_series(series_url, output_dir, log_callback, progress_callback)
            if series_results:
                results.extend(series_results)

            time.sleep(1.0)  # Be extra nice between series
        except Exception as e:
            log(f"Error on series {series_url}: {e}")

    log(f"\n{'='*80}")
    log(f"Publisher scrape complete: {len(results)} total issues downloaded")
    log('='*80)
    return results

def scrape_series(series_url: str, output_dir: str = None, log_callback=None, progress_callback=None):
    """Scrape all issues from a series or a single issue"""
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    def update_progress(data):
        if progress_callback:
            progress_callback(data)

    # Check if URL is a direct issue or series page
    if is_issue_url(series_url, log_callback):
        # Single issue - scrape directly
        log(f"Detected direct issue link: {series_url}")
        result = scrape_issue(series_url, output_dir, log_callback, progress_callback)
        return [result] if result else []
    else:
        # Series - get all issues
        log(f"Detected series link, fetching all issues...")

        # Get all issue links
        issue_links = get_issue_links(series_url, log_callback)
        if not issue_links:
            log("No issues found")
            return []

        results = []
        for idx, issue_url in enumerate(issue_links, 1):
            try:
                log(f"\n{'='*60}")
                log(f"Scraping issue {idx}/{len(issue_links)}: {issue_url}")
                log('='*60)
                update_progress({
                    "status": f"Processing issue {idx}/{len(issue_links)}",
                    "current": issue_url,
                    "progress": 0
                })
                result = scrape_issue(issue_url, output_dir, log_callback, progress_callback)
                if result:
                    results.append(result)
                time.sleep(0.5)
            except Exception as e:
                log(f"Error on {issue_url}: {e}")

        return results

def scrape(url: str, output_dir: str = None, log_callback=None, progress_callback=None):
    """Main entry point for scraping - automatically detects URL type and scrapes accordingly

    Args:
        url: Publisher, series, or issue URL
        output_dir: Directory to save CBZ files
        log_callback: Function to call for logging messages
        progress_callback: Function to call for progress updates

    Returns:
        List of paths to created CBZ files
    """
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    try:
        # Ensure output directory exists if specified
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            log(f"Output directory: {output_dir}")

        # Detect URL type
        log(f"Detecting URL type for: {url}")
        url_type = detect_url_type(url, log_callback)
        log(f"Detected URL type: {url_type}")

        if url_type == 'publisher':
            return scrape_publisher(url, output_dir, log_callback, progress_callback)
        elif url_type == 'series':
            return scrape_series(url, output_dir, log_callback, progress_callback)
        elif url_type == 'issue':
            result = scrape_issue(url, output_dir, log_callback, progress_callback)
            return [result] if result else []
        else:
            log(f"Unknown URL type: {url_type}")
            return []
    except Exception as e:
        log(f"ERROR in scrape(): {type(e).__name__}: {str(e)}")
        app_logger.error(f"Error in scrape(): {e}", exc_info=True)
        raise  # Re-raise to let the caller handle it
