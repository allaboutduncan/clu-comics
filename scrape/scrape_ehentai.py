import os
import ssl
import urllib3
import requests
import zipfile
import shutil
import re
import time
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from requests.exceptions import RequestException, ConnectTimeout
from app_logging import app_logger

# Disable warnings about unverified HTTPS requests
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Create a custom SSL context that allows weaker DH keys
ssl_context = ssl.create_default_context()
ssl_context.set_ciphers("DEFAULT:@SECLEVEL=1")

session = requests.Session()
adapter = requests.adapters.HTTPAdapter()
session.mount("https://", adapter)

def download_image(img_url, folder, img_name, log_callback=None):
    """Download an image from E-Hentai"""
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    try:
        response = session.get(img_url, stream=True, verify=False)  # Force no SSL verification
        if response.status_code == 200:
            img_path = os.path.join(folder, img_name)
            with open(img_path, 'wb') as file:
                for chunk in response.iter_content(1024):
                    file.write(chunk)
            log(f"Downloaded: {img_name}")
            return True
        else:
            log(f"Failed to download: {img_url}")
            return False
    except requests.exceptions.SSLError as e:
        log(f"SSL Error: {e} - Skipping {img_url}")
        return False

def cleanup_empty_folder(folder_path, log_callback=None):
    """Remove folder if it exists and is empty or only contains temp files"""
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    try:
        if os.path.exists(folder_path) and os.path.isdir(folder_path):
            # Check if folder is empty or only contains hidden/temp files
            files = [f for f in os.listdir(folder_path) if not f.startswith('.')]
            if len(files) == 0:
                shutil.rmtree(folder_path, ignore_errors=True)
                log(f"Cleaned up empty folder: {os.path.basename(folder_path)}")
    except Exception as e:
        app_logger.warning(f"Could not cleanup folder {folder_path}: {e}")

def create_cbz(folder, log_callback=None):
    """Create CBZ file from folder with unique naming to prevent overwrites"""
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    # Generate base CBZ path
    cbz_filename = f"{folder}.cbz"

    # Check if file already exists and add counter if needed
    if os.path.exists(cbz_filename):
        base_path = folder
        counter = 1
        while os.path.exists(cbz_filename):
            cbz_filename = f"{base_path}_({counter}).cbz"
            counter += 1
        log(f"File already exists, using unique name: {os.path.basename(cbz_filename)}")

    with zipfile.ZipFile(cbz_filename, 'w', zipfile.ZIP_DEFLATED) as cbz:
        for root, _, files in os.walk(folder):
            for file in sorted(files):
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, start=folder)
                cbz.write(file_path, arcname)
    log(f"Created CBZ archive: {cbz_filename}")

    # Remove the directory after creating the CBZ file
    shutil.rmtree(folder)
    log(f"Removed directory: {folder}")

    return cbz_filename

def scrape_gallery(url, output_dir=None, log_callback=None, progress_callback=None):
    """Scrape an E-Hentai gallery"""
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    def update_progress(data):
        if progress_callback:
            progress_callback(data)

    first_url = url + "?nw=always"
    headers = {'User-Agent': 'Mozilla/5.0'}
    all_links = []
    save_folder = None  # Track folder for cleanup

    try:
        log(f"Scraping: {first_url}")
        response = requests.get(first_url, headers=headers, timeout=30)
        if response.status_code != 200:
            log("Failed to access the URL.")
            raise Exception("Failed to access the URL")

        soup = BeautifulSoup(response.text, 'html.parser')
        title = soup.title.string.strip()
        title = re.sub(r'\[.*?\]', '', title)  # Remove content in brackets
        title = title.replace(" - E-Hentai Galleries", "").strip()
        title = title.replace("#", "")  # Remove '#' from directory name
        title = title.replace(":", "")  # Remove ':' from directory name

        # Use output_dir if provided, otherwise use current directory
        if output_dir:
            save_folder = os.path.join(output_dir, title)
        else:
            save_folder = os.path.join(os.getcwd(), title)

        # Ensure unique folder name to prevent conflicts
        if os.path.exists(save_folder):
            base_folder = save_folder
            counter = 1
            while os.path.exists(save_folder):
                save_folder = f"{base_folder}_({counter})"
                counter += 1
            log(f"Folder exists, using unique name: {os.path.basename(save_folder)}")

        os.makedirs(save_folder, exist_ok=True)
        log(f"Saving to: {save_folder}")

        gpc = soup.find('p', class_='gpc')
        if not gpc:
            log("Could not determine the number of images.")
            raise Exception("Could not determine the number of images")

        match = re.search(r'Showing \d+ - \d+ of (\d+) images', gpc.text)
        if not match:
            log("Could not parse the total number of images.")
            raise Exception("Could not parse the total number of images")

        total_images = int(match.group(1))
        total_pages = (total_images + 39) // 40  # Each page contains up to 40 images

        log(f"Total images: {total_images}, Total pages: {total_pages}")
        update_progress({
            'status': 'Collecting image links',
            'current': title,
            'progress': 0
        })

        # Collect all image page links
        for page in range(total_pages):
            page_url = first_url if page == 0 else url + f'?p={page}'
            log(f"Scraping page {page + 1}/{total_pages}: {page_url}")
            response = requests.get(page_url, headers=headers, timeout=30)
            if response.status_code != 200:
                log(f"Failed to access page {page}.")
                continue

            time.sleep(1)  # Pause to avoid overwhelming the server

            soup = BeautifulSoup(response.text, 'html.parser')
            gallery_div = soup.find('div', id='gdt')
            if not gallery_div:
                log(f"No gallery found on page {page}.")
                continue

            links = [urljoin(url, a['href']) for a in gallery_div.find_all('a', href=True)]
            all_links.extend(links)

            # Update progress for link collection
            progress = int(((page + 1) / total_pages) * 30)  # First 30% for collecting links
            update_progress({
                'status': 'Collecting image links',
                'current': f'{title} (page {page + 1}/{total_pages})',
                'progress': progress
            })

        log(f"Found {len(all_links)} image page links")

        # Download images
        success_count = 0
        for index, link in enumerate(all_links, start=1):
            log(f"Processing image {index}/{len(all_links)}")

            img_page = requests.get(link, headers=headers, timeout=30)
            if img_page.status_code != 200:
                log(f"Failed to access image page: {link}")
                continue

            img_soup = BeautifulSoup(img_page.text, 'html.parser')
            img_div = img_soup.find('div', id='i3')
            if not img_div:
                log(f"No image found on: {link}")
                continue

            img_tag = img_div.find('img')
            if img_tag and 'src' in img_tag.attrs:
                img_url = img_tag['src']
                img_ext = os.path.splitext(img_url)[-1]
                img_name = f"image_{index:04d}{img_ext}"

                if download_image(img_url, save_folder, img_name, log_callback):
                    success_count += 1
            else:
                log(f"No valid image found on: {link}")

            # Update progress for downloads (30% to 100%)
            progress = 30 + int((index / len(all_links)) * 70)
            update_progress({
                'status': 'Downloading images',
                'current': f'{title} ({index}/{len(all_links)})',
                'progress': progress
            })

            time.sleep(0.5)  # Small delay between downloads

        log(f"Downloaded {success_count}/{len(all_links)} images")

        # Check if folder has any files before creating CBZ
        if not os.path.exists(save_folder) or not os.listdir(save_folder):
            log("No files downloaded successfully")
            raise Exception("No files downloaded successfully")

        # Create CBZ
        update_progress({
            'status': 'Creating CBZ',
            'current': title,
            'progress': 95
        })

        cbz_path = create_cbz(save_folder, log_callback)

        update_progress({
            'status': 'Complete',
            'current': title,
            'progress': 100
        })

        return cbz_path

    except Exception as e:
        # Clean up empty folder on error
        if save_folder:
            cleanup_empty_folder(save_folder, log_callback)
        raise

def scrape_urls(urls, output_dir=None, log_callback=None, progress_callback=None):
    """Scrape multiple E-Hentai galleries"""
    def log(msg):
        if log_callback:
            log_callback(msg)
        app_logger.info(msg)

    def update_progress(data):
        if progress_callback:
            progress_callback(data)

    results = []
    total_urls = len(urls)

    for idx, url in enumerate(urls, start=1):
        log(f"\n=== Processing URL {idx}/{total_urls} ===")
        log(f"URL: {url}")

        retry_attempts = 3
        success = False

        while retry_attempts > 0:
            try:
                cbz_path = scrape_gallery(url, output_dir, log_callback, progress_callback)
                success = True
                results.append({
                    'url': url,
                    'success': True,
                    'cbz_path': cbz_path
                })
                log(f"Successfully processed: {url}")
                break
            except ConnectTimeout:
                retry_attempts -= 1
                if retry_attempts > 0:
                    log(f"Timeout occurred. Retrying in 10 seconds...")
                    time.sleep(10)
                else:
                    log(f"Request failed for {url} after retries.")
                    results.append({
                        'url': url,
                        'success': False,
                        'error': 'Timeout after retries'
                    })
            except RequestException as e:
                log(f"Request failed for {url}: {e}")
                results.append({
                    'url': url,
                    'success': False,
                    'error': str(e)
                })
                break
            except Exception as e:
                log(f"Error processing {url}: {e}")
                results.append({
                    'url': url,
                    'success': False,
                    'error': str(e)
                })
                break

        # Update overall progress
        overall_progress = int((idx / total_urls) * 100)
        update_progress({
            'status': f'Processing gallery {idx}/{total_urls}',
            'current': url,
            'progress': overall_progress
        })

        # Delay between galleries to avoid overwhelming the server
        if idx < total_urls:
            time.sleep(2)

    return results
