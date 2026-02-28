from flask import Flask, render_template, request, Response, send_from_directory, send_file, redirect, jsonify, url_for, stream_with_context, render_template_string, flash
from werkzeug.utils import secure_filename
from werkzeug.routing import IntegerConverter
import subprocess
import io
import os
import shutil
import uuid
import sys
import threading
import time
import json
import logging
import signal
import select
import random
from models import comicvine
from datetime import datetime, timedelta
import time as time_module
from PIL import Image, ImageFilter, ImageDraw
try:
    import pwd
except ImportError:
    pwd = None
from functools import lru_cache
import hashlib
import re
import xml.etree.ElementTree as ET
import heapq
import zipfile
import rarfile
import traceback
import mysql.connector
import base64
from io import BytesIO
from api import app
import app_state
from favorites import favorites_bp

# Custom URL converter for signed integers (supports negative IDs)
class SignedIntConverter(IntegerConverter):
    regex = r'-?\d+'

app.url_map.converters['signed'] = SignedIntConverter
from opds import opds_bp
from models import gcd
from models import metron
from config import config, load_flask_config, write_config, load_config
from cbz_ops.edit import get_edit_modal, save_cbz, cropCenter, cropLeft, cropRight, cropFreeForm, get_image_data_url, modal_body_template
from memory_utils import initialize_memory_management, cleanup_on_exit, memory_context, get_global_monitor
from app_logging import app_logger, APP_LOG, MONITOR_LOG
from helpers import is_hidden
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import OrderedDict
from version import __version__
import requests
from packaging import version as pkg_version
from database import (init_db, get_db_connection, get_recent_files, log_recent_file, invalidate_browse_cache,
                      get_file_index_from_db, save_file_index_to_db, update_file_index_entry,
                      add_file_index_entry, delete_file_index_entry, clear_file_index_from_db,
                      sync_file_index_incremental, search_file_index,
                      get_rebuild_schedule, save_rebuild_schedule as db_save_rebuild_schedule, update_last_rebuild,
                      get_sync_schedule, save_sync_schedule as db_save_sync_schedule, update_last_sync,
                      get_path_counts_batch, get_directory_children, clear_stats_cache,
                      clear_stats_cache_keys, mark_issue_read, get_issues_read, get_recent_read_issues,
                      save_issues_bulk, get_issues_for_series, update_series_sync_time, get_wanted_issues,
                      delete_issues_for_series, get_series_needing_sync, get_all_mapped_series, get_series_by_id,
                      get_continue_reading_items, get_provider_credentials)
import recommendations
from models.stats import (get_library_stats, get_file_type_distribution, get_top_publishers,
                          get_reading_history_stats, get_largest_comics, get_top_series_by_count,
                          get_reading_heatmap_data)
from models.timeline import get_reading_timeline
# Add URL encoding support for template filters
from urllib.parse import quote_plus
from file_watcher import FileWatcher
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Register custom Jinja2 template filters
@app.template_filter('basename')
def basename_filter(path):
    """Extract the basename from a file path."""
    if path:
        return os.path.basename(path)
    return ''


def generate_series_slug(series_name, metron_id, volume=None):
    """
    Generate a URL-friendly slug for a series or issue page.
    Format: series-name-vVOLUME-ID (e.g., amazing-spider-man-v1-4984)

    The metron_id is a series_id or issue_id depending on the route:
    - For issue_view: issue_id (resolves to series_id then redirects)
    - For series_view: series_id (used directly)
    """

    # Ensure we have a valid ID
    if not metron_id:
        return None

    if not series_name:
        return str(metron_id)

    # Convert to lowercase and replace spaces/special chars with hyphens
    slug = str(series_name).lower()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    slug = slug.strip('-')

    # Add volume if provided
    if volume:
        slug = f"{slug}-v{volume}"

    # Always append ID for reliable lookup
    slug = f"{slug}-{metron_id}"

    return slug


# Make it available in templates
app.jinja_env.globals['generate_series_slug'] = generate_series_slug

load_config()

# Initialize Database
init_db()

# Migrate custom rename settings from config.ini to user_preferences DB (one-time)
from database import get_user_preference, set_user_preference
if get_user_preference('custom_rename_pattern') is None:
    _ini_pattern = config.get('SETTINGS', 'CUSTOM_RENAME_PATTERN', fallback='')
    _ini_enabled = config.getboolean('SETTINGS', 'ENABLE_CUSTOM_RENAME', fallback=False)
    if _ini_pattern or _ini_enabled:
        set_user_preference('custom_rename_pattern', _ini_pattern, category='file_processing')
        set_user_preference('enable_custom_rename', _ini_enabled, category='file_processing')
        app_logger.info("Migrated custom rename settings from config.ini to user_preferences DB")

# Migrate bootstrap_theme from config.ini to user_preferences DB
if get_user_preference('bootstrap_theme') is None:
    _ini_theme = config.get('SETTINGS', 'BOOTSTRAP_THEME', fallback='default')
    set_user_preference('bootstrap_theme', _ini_theme, category='personalization')
    app_logger.info("Migrated bootstrap_theme from config.ini to user_preferences DB")

# Migrate recommendation settings from config.ini to user_preferences DB
if get_user_preference('rec_enabled') is None:
    set_user_preference('rec_enabled', config.get('SETTINGS', 'REC_ENABLED', fallback='True') == 'True', category='personalization')
    set_user_preference('rec_provider', config.get('SETTINGS', 'REC_PROVIDER', fallback='gemini'), category='personalization')
    set_user_preference('rec_api_key', config.get('SETTINGS', 'REC_API_KEY', fallback=''), category='personalization')
    set_user_preference('rec_model', config.get('SETTINGS', 'REC_MODEL', fallback='gemini-2.0-flash'), category='personalization')
    app_logger.info("Migrated recommendation settings from config.ini to user_preferences DB")

# Backup database on startup (only if changed since last backup)
from database import backup_database
backup_database(max_backups=3)

# Register Blueprints
app.register_blueprint(favorites_bp)
app.register_blueprint(opds_bp)
from reading_lists import reading_lists_bp
app.register_blueprint(reading_lists_bp)
from routes.downloads import downloads_bp
app.register_blueprint(downloads_bp)
from routes.files import files_bp
app.register_blueprint(files_bp)
from routes.series import series_bp
app.register_blueprint(series_bp)
from routes.collection import collection_bp
app.register_blueprint(collection_bp)
from routes.metadata import metadata_bp
app.register_blueprint(metadata_bp)

# Start unified scheduler
app_state.scheduler.start()
app_logger.info("📅 Unified scheduler initialized")

# Function to perform scheduled file index rebuild
def scheduled_file_index_rebuild():
    """Rebuild the file index on schedule using incremental sync."""
    global index_built

    try:
        app_logger.info("🔄 Starting scheduled file index sync...")
        start_time = time.time()

        # Scan filesystem to get current state
        app_logger.info("Scanning filesystem...")
        filesystem_entries = scan_filesystem_for_sync()
        scan_time = time.time() - start_time
        app_logger.info(f"Filesystem scan completed: {len(filesystem_entries)} entries in {scan_time:.2f}s")

        # Incremental sync (preserves metadata for existing files)
        app_logger.info("Performing incremental sync...")
        sync_result = sync_file_index_incremental(filesystem_entries)
        app_logger.info(f"Sync result: {sync_result['added']} added, {sync_result['removed']} removed, {sync_result['unchanged']} unchanged")

        # Queue only NEW files for metadata scanning
        if sync_result['added'] > 0:
            from metadata_scanner import queue_files_for_scan, PRIORITY_NEW_FILE
            new_cbz_paths = [p for p in sync_result['new_paths'] if p.lower().endswith('.cbz')]
            if new_cbz_paths:
                queue_files_for_scan(new_cbz_paths, PRIORITY_NEW_FILE)
                app_logger.info(f"Queued {len(new_cbz_paths)} new CBZ files for metadata scanning")

        # Also queue any other files that still need metadata scanning
        # (e.g., previously added files that were never scanned)
        from metadata_scanner import queue_pending_files
        queued = queue_pending_files()
        if queued:
            app_logger.info(f"Queued {queued} additional files for metadata scanning")

        # Refresh in-memory index from DB
        file_index.clear()
        db_index = get_file_index_from_db()
        if db_index:
            file_index.extend(db_index)
        index_built = True

        # Update last rebuild timestamp
        update_last_rebuild()

        # Clear and pre-populate stats cache
        clear_stats_cache()
        get_library_stats()
        get_file_type_distribution()
        get_top_publishers()
        get_reading_history_stats()

        elapsed = time.time() - start_time
        app_logger.info(f"✅ Scheduled file index sync completed in {elapsed:.2f}s")
    except Exception as e:
        app_logger.error(f"❌ Scheduled file index sync failed: {e}")

# Job registry for unified scheduler
SCHEDULE_JOBS = {}  # Populated after callback functions are defined


def configure_schedule(schedule_name):
    """
    Generic scheduler configuration for any schedule type.
    Uses the unified schedules table and a single BackgroundScheduler.

    Args:
        schedule_name: One of 'rebuild', 'sync', 'getcomics', 'weekly_packs', 'komga'
    """
    try:
        from database import get_schedule

        job_config = SCHEDULE_JOBS.get(schedule_name)
        if not job_config:
            app_logger.error(f"Unknown schedule name: {schedule_name}")
            return

        schedule = get_schedule(schedule_name)
        if not schedule:
            app_logger.warning(f"No schedule found for '{schedule_name}'")
            return

        job_id = job_config['job_id']
        label = job_config['label']
        callback = job_config['callback']

        # Remove existing job for this schedule (not all jobs)
        try:
            app_state.scheduler.remove_job(job_id)
        except Exception:
            pass  # Job might not exist

        if schedule['frequency'] == 'disabled':
            app_logger.info(f"📅 Scheduled {label} is disabled")
            return

        # Parse time
        time_parts = schedule['time'].split(':')
        hour = int(time_parts[0])
        minute = int(time_parts[1])

        days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

        if schedule['frequency'] == 'daily':
            trigger = CronTrigger(hour=hour, minute=minute)
            app_state.scheduler.add_job(
                callback,
                trigger=trigger,
                id=job_id,
                name=f'Daily {label}',
                replace_existing=True
            )
            app_logger.info(f"📅 Scheduled daily {label} at {schedule['time']}")

        elif schedule['frequency'] == 'weekly':
            weekday = int(schedule['weekday'])
            trigger = CronTrigger(day_of_week=weekday, hour=hour, minute=minute)
            app_state.scheduler.add_job(
                callback,
                trigger=trigger,
                id=job_id,
                name=f'Weekly {label}',
                replace_existing=True
            )
            app_logger.info(f"📅 Scheduled weekly {label} on {days[weekday]} at {schedule['time']}")

    except Exception as e:
        app_logger.error(f"Failed to configure schedule '{schedule_name}': {e}")


def get_next_run_for_job(job_id):
    """Get the next scheduled run time for a specific job."""
    try:
        job = app_state.scheduler.get_job(job_id)
        if job and job.next_run_time:
            return job.next_run_time.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        pass
    return "Not scheduled"


# Backward-compatible wrapper
def configure_rebuild_schedule():
    """Configure the rebuild schedule based on database settings."""
    configure_schedule('rebuild')


# Function to perform scheduled series sync
def scheduled_series_sync():
    """Sync all mapped series from Metron API on schedule."""
    try:
        app_logger.info("🔄 Starting scheduled series sync...")
        start_time = time.time()

        # Get Metron API with credentials from config
        metron_username = app.config.get("METRON_USERNAME", "").strip()
        metron_password = app.config.get("METRON_PASSWORD", "").strip()
        if not metron_username or not metron_password:
            app_logger.warning("Metron credentials not configured, skipping scheduled sync")
            return

        api = metron.get_api(metron_username, metron_password)
        if not api:
            app_logger.warning("Failed to initialize Metron API, skipping scheduled sync")
            return

        # Get all mapped series
        series_list = get_all_mapped_series()
        if not series_list:
            app_logger.info("No mapped series to sync")
            update_last_sync()
            return

        success_count = 0
        fail_count = 0

        for series in series_list:
            series_id = series['id']
            try:
                # Fetch series info from API
                series_info = api.series(series_id)
                if not series_info:
                    fail_count += 1
                    continue

                # Fetch all issues
                all_issues_result = metron.get_all_issues_for_series(api, series_id)
                all_issues = list(all_issues_result) if all_issues_result else []

                # Save issues (INSERT OR REPLACE handles updates)
                save_issues_bulk(all_issues, series_id)

                # Clean up issues that no longer exist in API response
                if all_issues:
                    from database import cleanup_stale_issues
                    api_issue_ids = {i.id if hasattr(i, 'id') else i.get('id') for i in all_issues}
                    cleanup_stale_issues(series_id, api_issue_ids)

                update_series_sync_time(series_id, len(all_issues))

                success_count += 1
                app_logger.debug(f"Synced series {series_id}: {len(all_issues)} issues")

            except Exception as e:
                if metron.is_connection_error(e):
                    app_logger.warning(f"Metron unavailable while syncing series {series_id}: {e}")
                else:
                    app_logger.error(f"Error syncing series {series_id}: {e}")
                fail_count += 1

        # Update last sync timestamp
        update_last_sync()

        # Clear entire wanted cache since issues may have changed for multiple series
        from database import clear_wanted_cache_all
        clear_wanted_cache_all()
        app_logger.info("Cleared wanted cache after scheduled sync")

        elapsed = time.time() - start_time
        app_logger.info(f"✅ Scheduled series sync completed in {elapsed:.2f}s ({success_count} synced, {fail_count} failed)")

        # After syncing, check TARGET folder for wanted issues
        process_incoming_wanted_issues()

    except Exception as e:
        if metron.is_connection_error(e):
            app_logger.warning(f"Metron unavailable during scheduled sync: {e}")
        else:
            app_logger.error(f"Scheduled series sync failed: {e}")


def get_series_name_from_files(mapped_path, db_series_name):
    """
    Extract actual series name used in existing files.
    Falls back to database series name if no files exist.

    This helps match files when the database has "The Ultimates" but
    files are named "Ultimates 001.cbz".
    """
    if not mapped_path or not os.path.exists(mapped_path):
        app_logger.debug(f"get_series_name_from_files: path doesn't exist: {mapped_path}")
        return db_series_name

    comic_extensions = ('.cbz', '.cbr', '.zip', '.rar')
    try:
        files = [f for f in os.listdir(mapped_path)
                 if f.lower().endswith(comic_extensions)]
    except Exception:
        return db_series_name

    if not files:
        app_logger.debug(f"get_series_name_from_files: no files in {mapped_path}, using DB name: {db_series_name}")
        return db_series_name

    # Try to extract series name from first file
    # Pattern: "Series Name 001 (2024).cbz" -> "Series Name"
    first_file = files[0]
    # Remove extension
    name = os.path.splitext(first_file)[0]
    # Remove all parenthetical groups: "(2024)", "(1)", "(digital)", etc.
    name = re.sub(r'\s*\([^)]*\)', '', name)
    # Remove issue number at end: " 001" or " 1"
    name = re.sub(r'\s+\d+\s*$', '', name)

    if name:
        extracted = name.strip()
        if extracted != db_series_name:
            app_logger.info(f"get_series_name_from_files: extracted '{extracted}' from '{first_file}' (DB: '{db_series_name}')")
        return extracted

    return db_series_name


def process_incoming_wanted_issues():
    """
    Scan TARGET folder for wanted issues and move to series folders.
    Uses CUSTOM_RENAME_PATTERN for matching (excluding year) and renaming.
    Only processes MISSING issues (not already in collection) with store_date <= today.
    """
    from database import get_all_mapped_series, get_issues_for_series, get_manual_status_for_series
    from cbz_ops.rename import load_custom_rename_config
    from datetime import date

    target_folder = app.config.get('TARGET', '/downloads/processed')
    if not os.path.exists(target_folder):
        app_logger.debug(f"TARGET folder does not exist: {target_folder}")
        return

    today = date.today().isoformat()

    # Build list of truly MISSING issues (not in collection, store_date <= today)
    wanted = []
    mapped_series = get_all_mapped_series()

    for series in mapped_series:
        series_id = series['id']
        mapped_path = series.get('mapped_path')

        if not mapped_path or not os.path.exists(mapped_path):
            continue

        # Get cached issues for this series
        issues = get_issues_for_series(series_id)
        if not issues:
            continue

        # Convert issues to objects for matching function
        from models.issue import IssueObj, SeriesObj
        issue_objs = [IssueObj(i) for i in issues]
        series_obj = SeriesObj(series)

        # Check which issues are in collection
        issue_status = match_issues_to_collection(mapped_path, issue_objs, series_obj)

        # Get manual status for this series (owned/skipped)
        manual_status = get_manual_status_for_series(series_id)

        # Find missing issues with store_date <= today
        for issue in issues:
            issue_num = str(issue.get('number', ''))
            status = issue_status.get(issue_num, {})
            store_date = issue.get('store_date')

            # Only include if: not found AND not manually marked AND (store_date <= today OR no store_date)
            if not status.get('found') and issue_num not in manual_status:
                if not store_date or store_date <= today:
                    wanted.append({
                        'number': issue.get('number'),
                        'series_id': series_id,
                        'series_name': series.get('name'),
                        'series_volume': series.get('volume'),
                        'mapped_path': mapped_path,
                        'store_date': store_date,
                    })

    if not wanted:
        app_logger.info("No missing issues found")
        return

    app_logger.info(f"=== Checking {len(wanted)} MISSING issues against TARGET folder ===")
    for w in wanted[:10]:  # Log first 10 missing issues
        app_logger.info(f"  MISSING: '{w['series_name']}' #{w['number']} (store: {w['store_date']}, mapped: {w['mapped_path']})")

    # Load rename pattern
    enabled, pattern = load_custom_rename_config()
    if not enabled or not pattern:
        pattern = "{series_name} {issue_number} ({year})"

    # Create a matching pattern WITHOUT year (year can differ between sources)
    # Replace {year} and surrounding parens/spaces with flexible match
    match_pattern = pattern
    # Remove year placeholder and its common surrounding patterns
    match_pattern = re.sub(r'\s*\(\s*\{year\}\s*\)', '', match_pattern)  # " ({year})" -> ""
    match_pattern = re.sub(r'\s*\{year\}', '', match_pattern)  # remaining "{year}" -> ""
    match_pattern = match_pattern.strip()
    app_logger.debug(f"Using match pattern (no year): '{match_pattern}'")

    # Scan TARGET for comic files (including subdirectories)
    comic_extensions = ('.cbz', '.cbr', '.zip', '.rar')
    try:
        files = []  # List of (filename, full_path) tuples
        for root, dirs, filenames in os.walk(target_folder):
            for f in filenames:
                if f.lower().endswith(comic_extensions):
                    files.append((f, os.path.join(root, f)))
        app_logger.info(f"Found {len(files)} comic files in TARGET folder (including subdirectories):")
        for f, fp in files:
            app_logger.info(f"  FILE: {fp}")
    except Exception as e:
        app_logger.error(f"Failed to scan TARGET folder: {e}")
        return

    if not files:
        return

    moved_count = 0
    affected_series = set()  # Track series that had files moved
    for issue in wanted:
        db_series_name = issue['series_name']
        issue_number = issue['number']
        mapped_path = issue['mapped_path']

        # Get actual series name from existing files in the series folder
        # This handles cases like "The Ultimates" in DB but "Ultimates" in filenames
        actual_series_name = get_series_name_from_files(mapped_path, db_series_name)
        if actual_series_name != db_series_name:
            app_logger.debug(f"Using file-based name: '{actual_series_name}' instead of '{db_series_name}'")

        # Generate regex pattern for this issue (without year)
        regex = generate_filename_pattern(
            match_pattern,
            actual_series_name,
            issue_number
        )
        if not regex:
            app_logger.info(f"Failed to generate pattern for: {actual_series_name} #{issue_number}")
            continue

        app_logger.info(f"Checking: '{actual_series_name}' #{issue_number} | regex: {regex.pattern}")

        for filename, src in files[:]:  # Copy list to allow removal
            match_result = regex.match(filename)

            # Fallback: try ComicInfo.xml if regex didn't match
            if not match_result and filename.lower().endswith(('.cbz', '.zip')):
                comicinfo = extract_comicinfo(src)
                if comicinfo and comicinfo.get('number'):
                    meta_num = str(comicinfo['number']).strip().lstrip('0') or '0'
                    check_num = str(issue_number).strip().lstrip('0') or '0'
                    if meta_num == check_num:
                        meta_series = (comicinfo.get('series') or '').lower()
                        series_lower = actual_series_name.lower()
                        if meta_series and (series_lower in meta_series or meta_series in series_lower):
                            match_result = True

            app_logger.debug(f"  Testing '{filename}' -> {'MATCH' if match_result else 'no match'}")
            if match_result:
                app_logger.info(f"✓ Match found: '{filename}' matches '{actual_series_name} #{issue_number}'")

                # Found a match - move first, then rename
                dest_dir = mapped_path

                # Debug: Log paths and existence checks
                app_logger.debug(f"  Source path: {src} (exists: {os.path.exists(src)})")
                app_logger.debug(f"  Dest dir: {dest_dir} (exists: {os.path.exists(dest_dir)}, isdir: {os.path.isdir(dest_dir) if os.path.exists(dest_dir) else 'N/A'})")

                if not os.path.exists(src):
                    app_logger.warning(f"Source file missing: {src}")
                    continue

                if not os.path.exists(dest_dir):
                    app_logger.warning(f"Series folder missing: {dest_dir}")
                    continue

                # Move file with original name first
                temp_dest = os.path.join(dest_dir, filename)

                try:
                    shutil.move(src, temp_dest)
                    app_logger.info(f"Moved: {filename} -> {dest_dir}")
                    files.remove((filename, src))
                    moved_count += 1
                    affected_series.add(issue['series_id'])

                    # Now rename using get_renamed_filename
                    from cbz_ops.rename import get_renamed_filename
                    new_filename = get_renamed_filename(filename, file_path=temp_dest)
                    final_path = temp_dest
                    if new_filename and new_filename != filename:
                        final_path = os.path.join(dest_dir, new_filename)
                        os.rename(temp_dest, final_path)
                        app_logger.info(f"Renamed: {filename} -> {new_filename}")

                    # Auto-fetch metadata (try Metron first, then ComicVine as fallback)
                    app_logger.info(f"Auto-fetching metadata for: {final_path}")
                    final_path = auto_fetch_metron_metadata(final_path)
                    final_path = auto_fetch_comicvine_metadata(final_path)

                    # Add to file_index immediately so the file is searchable/visible
                    try:
                        file_stat = os.stat(final_path)
                        add_file_index_entry(
                            name=os.path.basename(final_path),
                            path=final_path,
                            entry_type='file',
                            size=file_stat.st_size,
                            parent=os.path.dirname(final_path),
                            modified_at=file_stat.st_mtime
                        )
                    except Exception as e:
                        app_logger.error(f"Failed to add {final_path} to file index: {e}")

                except Exception as e:
                    app_logger.error(f"Failed to move/rename {filename}: {e}")
                break

    if moved_count > 0:
        app_logger.info(f"✅ Processed {moved_count} wanted issue(s) from TARGET folder")

        # Invalidate collection status cache for affected series
        # This ensures the wanted list is updated to remove matched issues
        from database import invalidate_collection_status_for_series, clear_wanted_cache_for_series
        for series_id in affected_series:
            invalidate_collection_status_for_series(series_id)
            clear_wanted_cache_for_series(series_id)
            app_logger.info(f"Invalidated collection and wanted cache for series {series_id}")
    else:
        app_logger.info("No wanted issues matched files in TARGET folder")


def configure_sync_schedule():
    """Configure the sync schedule based on database settings."""
    configure_schedule('sync')


# Function to perform scheduled GetComics auto-download
def scheduled_getcomics_download():
    """Auto-download wanted issues from GetComics on schedule."""
    try:
        from database import get_all_mapped_series, get_issues_for_series, update_last_getcomics_run, get_manual_status_for_series
        from models.getcomics import search_getcomics, get_download_links, score_getcomics_result
        from api import download_queue, download_progress
        from datetime import date

        app_logger.info("Starting scheduled GetComics auto-download...")
        start_time = time.time()

        today = date.today().isoformat()
        download_count = 0
        search_count = 0

        # Get all mapped series
        mapped_series = get_all_mapped_series()

        for series in mapped_series:
            series_id = series['id']
            series_name = series.get('name', '')
            series_year = series.get('volume_year') or series.get('year_began')
            mapped_path = series.get('mapped_path')

            if not mapped_path or not os.path.exists(mapped_path):
                continue

            # Get cached issues for this series
            issues = get_issues_for_series(series_id)
            if not issues:
                continue

            # Convert issues to objects for matching function
            from models.issue import IssueObj, SeriesObj
            issue_objs = [IssueObj(i) for i in issues]
            series_obj = SeriesObj(series)

            # Check which issues are in collection
            issue_status = match_issues_to_collection(mapped_path, issue_objs, series_obj)

            # Get manual status for this series (owned/skipped)
            manual_status = get_manual_status_for_series(series_id)

            # Find wanted issues with store_date <= today (already released)
            for issue in issues:
                issue_num = str(issue.get('number', ''))
                status = issue_status.get(issue_num, {})
                store_date = issue.get('store_date')

                # Skip if already found in collection
                if status.get('found'):
                    continue

                # Skip if manually marked as owned or skipped
                if issue_num in manual_status:
                    continue

                # Only process issues with store_date <= today (already released)
                if not store_date or store_date > today:
                    continue

                # Search GetComics for this issue
                search_count += 1

                # Get year from store_date or series (used in query and scoring)
                issue_year = int(store_date[:4]) if store_date else series_year

                query = f"{series_name} {issue_num} {issue_year}" if issue_year else f"{series_name} {issue_num}"
                app_logger.info(f"🔍 Searching GetComics for: {query}")

                # Rate limit - avoid hammering GetComics
                time.sleep(2)

                results = search_getcomics(query, max_pages=1)
                if not results:
                    app_logger.debug(f"No results found for: {query}")
                    continue

                # Score results and find best match
                best_result = None
                best_score = 0

                for result in results:
                    score = score_getcomics_result(
                        result['title'],
                        series_name,
                        issue_num,
                        issue_year
                    )
                    if score > best_score:
                        best_score = score
                        best_result = result

                # Only queue download if score >= 65 (series + issue match minimum)
                if best_score >= 65 and best_result:
                    app_logger.info(f"✅ Found match (score={best_score}): {best_result['title']}")

                    # Get download links
                    links = get_download_links(best_result['link'])

                    # Use config-driven provider priority
                    priority_str = config.get("SETTINGS", "DOWNLOAD_PROVIDER_PRIORITY",
                                               fallback="pixeldrain,download_now,mega")
                    priority_order = [p.strip() for p in priority_str.split(",") if p.strip()]
                    available = [(p, links[p]) for p in priority_order if links.get(p)]

                    download_url = available[0][1] if available else None
                    fallback_urls = available[1:] if len(available) > 1 else []

                    if download_url:
                        # Queue the download (matching manual download structure)
                        filename = f"{series_name} {issue_num}.cbz".replace('/', '-').replace('\\', '-')
                        download_id = str(uuid.uuid4())

                        # Set up progress tracking (same structure as manual download)
                        download_progress[download_id] = {
                            'url': download_url,
                            'progress': 0,
                            'bytes_total': 0,
                            'bytes_downloaded': 0,
                            'status': 'queued',
                            'filename': filename,
                            'error': None,
                            'provider': None,
                        }

                        # Queue task (same structure as manual download)
                        task = {
                            'download_id': download_id,
                            'url': download_url,
                            'dest_filename': filename,
                            'internal': True,
                            'fallback_urls': fallback_urls,
                        }
                        download_queue.put(task)

                        download_count += 1
                        app_logger.info(f"📥 Queued download: {filename}")
                    else:
                        app_logger.warning(f"No download link found for: {best_result['title']}")
                else:
                    app_logger.debug(f"No good match found for {series_name} #{issue_num} (best score: {best_score})")

        # Update last run timestamp
        update_last_getcomics_run()

        elapsed = time.time() - start_time
        app_logger.info(f"✅ GetComics auto-download completed in {elapsed:.2f}s ({search_count} searched, {download_count} queued)")

    except Exception as e:
        app_logger.error(f"❌ GetComics auto-download failed: {e}")


def configure_getcomics_schedule():
    """Configure the GetComics auto-download schedule based on database settings."""
    configure_schedule('getcomics')


# Function to perform scheduled Weekly Packs auto-download
def scheduled_weekly_packs_download():
    """Auto-download weekly packs from GetComics on schedule."""
    try:
        from database import (get_weekly_packs_config, update_last_weekly_packs_run,
                              log_weekly_pack_download, is_weekly_pack_downloaded)
        from models.getcomics import (find_latest_weekly_pack_url, check_weekly_pack_availability,
                                      parse_weekly_pack_page, get_weekly_pack_url_for_date,
                                      get_weekly_pack_dates_in_range)
        from api import download_queue, download_progress


        app_logger.info("📦 Starting scheduled Weekly Packs download...")
        start_time = time.time()

        # Get configuration
        config = get_weekly_packs_config()
        if not config:
            app_logger.warning("No weekly packs config found in database")
            return

        if not config['enabled']:
            app_logger.info("Weekly packs is disabled, skipping")
            return

        if not config['publishers']:
            app_logger.info("No publishers selected for weekly packs, skipping")
            return

        format_pref = config['format']
        publishers = config['publishers']
        start_date = config.get('start_date')
        total_download_count = 0
        latest_successful_pack = None
        any_not_ready = False

        # If start_date is set, check all weeks in range
        if start_date:
            today = datetime.now().strftime('%Y-%m-%d')
            pack_dates = get_weekly_pack_dates_in_range(start_date, today)
            app_logger.info(f"Checking {len(pack_dates)} potential pack dates from {start_date} to {today}")

            for pack_date in pack_dates:
                # Check if all publishers for this pack have been downloaded
                all_downloaded = all(
                    is_weekly_pack_downloaded(pack_date, pub, format_pref)
                    for pub in publishers
                )
                if all_downloaded:
                    app_logger.debug(f"Pack {pack_date} already downloaded for all publishers, skipping")
                    continue

                # Construct URL and check availability
                pack_url = get_weekly_pack_url_for_date(pack_date)
                app_logger.info(f"Checking pack {pack_date} -> {pack_url}")

                if not check_weekly_pack_availability(pack_url):
                    app_logger.info(f"Pack {pack_date} links not ready yet")
                    any_not_ready = True
                    continue

                # Parse download links
                download_links = parse_weekly_pack_page(pack_url, format_pref, publishers)
                if not download_links:
                    app_logger.warning(f"No download links found for {pack_date}")
                    continue

                # Queue downloads for publishers not yet downloaded
                for publisher, pixeldrain_url in download_links.items():
                    if is_weekly_pack_downloaded(pack_date, publisher, format_pref):
                        app_logger.debug(f"Already downloaded {pack_date} {publisher}, skipping")
                        continue

                    filename = f"{pack_date} {publisher} Week ({format_pref}).zip"
                    filename = filename.replace('/', '-').replace('\\', '-')
                    download_id = str(uuid.uuid4())

                    download_progress[download_id] = {
                        'url': pixeldrain_url,
                        'progress': 0,
                        'bytes_total': 0,
                        'bytes_downloaded': 0,
                        'status': 'queued',
                        'filename': filename,
                        'error': None,
                        'provider': None,
                    }

                    task = {
                        'download_id': download_id,
                        'url': pixeldrain_url,
                        'dest_filename': filename,
                        'internal': True,
                        'weekly_pack_info': {
                            'pack_date': pack_date,
                            'publisher': publisher,
                            'format': format_pref
                        }
                    }
                    download_queue.put(task)
                    log_weekly_pack_download(pack_date, publisher, format_pref, pixeldrain_url, 'queued')
                    total_download_count += 1
                    latest_successful_pack = pack_date
                    app_logger.info(f"📥 Queued weekly pack download: {filename}")

        else:
            # No start_date: just check the latest pack from homepage
            pack_url, pack_date = find_latest_weekly_pack_url()
            if not pack_url or not pack_date:
                app_logger.warning("Could not find weekly pack on GetComics homepage")
                update_last_weekly_packs_run()
                return

            app_logger.info(f"Found weekly pack: {pack_date} -> {pack_url}")

            # Check if we already downloaded this pack
            if config['last_successful_pack'] == pack_date:
                app_logger.info(f"Already downloaded pack {pack_date}, skipping")
                update_last_weekly_packs_run()
                return

            if not check_weekly_pack_availability(pack_url):
                app_logger.info(f"Weekly pack {pack_date} links not ready yet")
                any_not_ready = True
            else:
                download_links = parse_weekly_pack_page(pack_url, format_pref, publishers)
                if download_links:
                    for publisher, pixeldrain_url in download_links.items():
                        filename = f"{pack_date} {publisher} Week ({format_pref}).zip"
                        filename = filename.replace('/', '-').replace('\\', '-')
                        download_id = str(uuid.uuid4())

                        download_progress[download_id] = {
                            'url': pixeldrain_url,
                            'progress': 0,
                            'bytes_total': 0,
                            'bytes_downloaded': 0,
                            'status': 'queued',
                            'filename': filename,
                            'error': None,
                            'provider': None,
                        }

                        task = {
                            'download_id': download_id,
                            'url': pixeldrain_url,
                            'dest_filename': filename,
                            'internal': True,
                            'weekly_pack_info': {
                                'pack_date': pack_date,
                                'publisher': publisher,
                                'format': format_pref
                            }
                        }
                        download_queue.put(task)
                        log_weekly_pack_download(pack_date, publisher, format_pref, pixeldrain_url, 'queued')
                        total_download_count += 1
                        latest_successful_pack = pack_date
                        app_logger.info(f"📥 Queued weekly pack download: {filename}")

        # Update last run timestamp
        update_last_weekly_packs_run(latest_successful_pack)

        # Schedule retry if any packs weren't ready
        if any_not_ready and config['retry_enabled']:
            schedule_weekly_packs_retry()

        elapsed = time.time() - start_time
        app_logger.info(f"✅ Weekly packs download completed in {elapsed:.2f}s ({total_download_count} packs queued)")

    except Exception as e:
        app_logger.error(f"❌ Weekly packs download failed: {e}")


def schedule_weekly_packs_retry():
    """Schedule a one-time retry job for tomorrow at the same time."""
    try:
        from database import get_weekly_packs_config

        config = get_weekly_packs_config()
        if not config:
            return

        # Parse time
        time_parts = config['time'].split(':')
        hour = int(time_parts[0])
        minute = int(time_parts[1])

        # Calculate tomorrow's date/time
        tomorrow = datetime.now() + timedelta(days=1)
        run_time = tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # Add a one-time job (DateTrigger)
        from apscheduler.triggers.date import DateTrigger
        trigger = DateTrigger(run_date=run_time)

        app_state.scheduler.add_job(
            scheduled_weekly_packs_download,
            trigger=trigger,
            id='weekly_packs_retry',
            name='Weekly Packs Retry',
            replace_existing=True
        )

        app_logger.info(f"📅 Scheduled weekly packs retry for {run_time.strftime('%Y-%m-%d %H:%M')}")

    except Exception as e:
        app_logger.error(f"Failed to schedule weekly packs retry: {e}")


def configure_weekly_packs_schedule():
    """Configure the Weekly Packs schedule, with special logic for disabling getcomics."""
    try:
        from database import get_weekly_packs_config, save_schedule as db_save_schedule, get_schedule as db_get_schedule

        config = get_weekly_packs_config()
        if not config:
            app_logger.warning("No weekly packs config found in database")
            return

        # When weekly packs is enabled, disable getcomics individual downloads
        if config['enabled']:
            gc_sched = db_get_schedule('getcomics')
            if gc_sched and gc_sched['frequency'] != 'disabled':
                app_logger.info("📅 Disabling GetComics individual downloads (weekly packs enabled)")
                db_save_schedule('getcomics', 'disabled', gc_sched['time'], gc_sched['weekday'])
                configure_schedule('getcomics')

        configure_schedule('weekly_packs')

    except Exception as e:
        app_logger.error(f"Failed to configure weekly packs schedule: {e}")


#########################
# Komga Reading Sync    #
#########################

def map_komga_path(komga_path, komga_prefix, clu_prefix):
    """
    Convert a Komga file path to a CLU file path using prefix mapping.

    Example:
        komga_path:   /comics/Marvel/Spider-Man 001.cbz
        komga_prefix: /comics
        clu_prefix:   /data
        result:       /data/Marvel/Spider-Man 001.cbz
    """
    if not komga_prefix or not clu_prefix:
        return komga_path

    # Normalize path separators
    komga_path = komga_path.replace('\\', '/')
    komga_prefix = komga_prefix.rstrip('/').replace('\\', '/')
    clu_prefix = clu_prefix.rstrip('/').replace('\\', '/')

    if komga_path.startswith(komga_prefix):
        relative = komga_path[len(komga_prefix):]
        return clu_prefix + relative

    return komga_path


def map_komga_path_multi(komga_path, mappings):
    """
    Try each library mapping; return first match or original path.
    Mappings should be sorted by prefix length descending so longer prefixes match first.

    Args:
        komga_path: The file path as Komga sees it
        mappings: List of dicts with 'komga_prefix' and 'clu_prefix'

    Returns:
        Mapped CLU path, or original path if no mapping matches
    """
    for m in mappings:
        result = map_komga_path(komga_path, m['komga_prefix'], m['clu_prefix'])
        if result != komga_path:
            return result
    return komga_path


def find_clu_file_by_name(filename):
    """
    Search CLU's file_index for a file by exact filename.
    Used as a fallback when Komga path mapping doesn't find a file.

    Args:
        filename: The filename to search for (e.g., 'Spider-Man 001.cbz')

    Returns:
        Matched CLU file path, or None
    """
    try:
        conn = get_db_connection()
        if not conn:
            return None
        c = conn.cursor()
        c.execute('''
            SELECT path FROM file_index
            WHERE name = ? AND type = 'file'
            LIMIT 1
        ''', (filename,))
        row = c.fetchone()
        conn.close()
        return row['path'] if row else None
    except Exception:
        return None


def run_komga_sync():
    """
    Sync reading history and progress from Komga to CLU.

    Phase 1: Import completed reads into issues_read table
    Phase 2: Import in-progress positions into reading_positions table
    """
    from database import (
        get_komga_config, is_komga_book_synced, mark_komga_book_synced,
        update_komga_last_sync
    )
    from models.komga import KomgaClient, extract_book_info

    cfg = get_komga_config()
    if not cfg or not cfg.get('server_url'):
        app_logger.warning("Komga sync: not configured")
        return {'success': False, 'error': 'Komga not configured'}

    if not cfg.get('username') or not cfg.get('password'):
        app_logger.warning("Komga sync: credentials not set")
        return {'success': False, 'error': 'Komga credentials not set'}

    try:
        client = KomgaClient(cfg['server_url'], cfg['username'], cfg['password'])
    except Exception as e:
        app_logger.error(f"Komga sync: failed to create client: {e}")
        return {'success': False, 'error': str(e)}

    # Build active mappings from per-library configuration
    # Sort by komga_prefix length descending so longer prefixes match first
    active_mappings = []
    for m in cfg.get('library_mappings', []):
        komga_pfx = (m.get('komga_path_prefix') or '').strip()
        clu_pfx = (m.get('library_path') or '').strip()
        if komga_pfx and clu_pfx:
            active_mappings.append({'komga_prefix': komga_pfx, 'clu_prefix': clu_pfx})
    active_mappings.sort(key=lambda x: len(x['komga_prefix']), reverse=True)

    read_count = 0
    progress_count = 0
    skip_count = 0
    no_match_count = 0

    app_logger.info("🔄 Starting Komga reading sync...")
    start_time = time.time()

    # Phase 1: Sync completed reads
    try:
        for book in client.get_all_read_books():
            info = extract_book_info(book)
            book_id = info['id']

            if is_komga_book_synced(book_id, 'read'):
                skip_count += 1
                continue

            # Map Komga path to CLU path
            clu_path = map_komga_path_multi(info['url'], active_mappings)

            # If mapped path doesn't exist, try filename fallback
            if not clu_path or not os.path.exists(clu_path):
                clu_path = find_clu_file_by_name(info['name'])

            if not clu_path or not os.path.exists(clu_path):
                app_logger.debug(f"Komga sync: no CLU match for {info['url']} ({info['name']})")
                no_match_count += 1
                continue

            # Mark as read in CLU using existing function
            mark_issue_read(
                issue_path=clu_path,
                read_at=info['read_date'],
                page_count=info['page_count']
            )
            mark_komga_book_synced(book_id, info['url'], clu_path, 'read')
            read_count += 1
    except Exception as e:
        app_logger.error(f"Komga sync phase 1 (reads) error: {e}")

    # Phase 2: Sync in-progress reading positions
    try:
        from database import save_reading_position
        for book in client.get_all_in_progress_books():
            info = extract_book_info(book)
            book_id = info['id']

            clu_path = map_komga_path_multi(info['url'], active_mappings)
            if not clu_path or not os.path.exists(clu_path):
                clu_path = find_clu_file_by_name(info['name'])

            if not clu_path or not os.path.exists(clu_path):
                app_logger.debug(f"Komga sync: no CLU match for in-progress {info['name']}")
                continue

            save_reading_position(
                comic_path=clu_path,
                page_number=info['current_page'],
                total_pages=info['page_count']
            )
            mark_komga_book_synced(book_id, info['url'], clu_path, 'progress')
            progress_count += 1
    except Exception as e:
        app_logger.error(f"Komga sync phase 2 (progress) error: {e}")

    update_komga_last_sync(read_count, progress_count)

    # Clear stats caches so new data shows up
    clear_stats_cache_keys(['library_stats', 'reading_history', 'reading_heatmap'])

    elapsed = time.time() - start_time
    app_logger.info(
        f"✅ Komga sync completed in {elapsed:.2f}s: "
        f"{read_count} read, {progress_count} in-progress, "
        f"{skip_count} skipped, {no_match_count} unmatched"
    )

    return {
        'success': True,
        'read_count': read_count,
        'progress_count': progress_count,
        'skip_count': skip_count,
        'no_match_count': no_match_count,
        'elapsed': round(elapsed, 2)
    }


def scheduled_komga_sync():
    """Run Komga reading sync on schedule."""
    try:
        app_logger.info("🔄 Starting scheduled Komga reading sync...")
        run_komga_sync()
    except Exception as e:
        app_logger.error(f"❌ Scheduled Komga sync failed: {e}")


def configure_komga_sync_schedule():
    """Configure the Komga sync schedule based on database settings."""
    configure_schedule('komga')


# Populate job registry now that all callback functions are defined
SCHEDULE_JOBS.update({
    'rebuild': {'callback': scheduled_file_index_rebuild, 'job_id': 'file_index_rebuild', 'label': 'File Index Rebuild'},
    'sync': {'callback': scheduled_series_sync, 'job_id': 'series_sync', 'label': 'Series Sync'},
    'getcomics': {'callback': scheduled_getcomics_download, 'job_id': 'getcomics_download', 'label': 'GetComics Auto-Download'},
    'weekly_packs': {'callback': scheduled_weekly_packs_download, 'job_id': 'weekly_packs_download', 'label': 'Weekly Packs Download'},
    'komga': {'callback': scheduled_komga_sync, 'job_id': 'komga_sync', 'label': 'Komga Reading Sync'},
})


# Thread pool for thumbnail generation
thumbnail_executor = ThreadPoolExecutor(max_workers=2)

def scan_library_task():
    """Background task to scan library for new/changed files and generate thumbnails."""
    app_logger.info("Starting background library scan for thumbnails...")

    conn = get_db_connection()
    if not conn:
        app_logger.error("Could not connect to DB for library scan")
        return

    try:
        # Get all existing jobs to minimize DB queries in loop
        # Map path -> (status, file_mtime)
        cursor = conn.execute("SELECT path, status, file_mtime FROM thumbnail_jobs")
        existing_jobs = {row['path']: (row['status'], row['file_mtime']) for row in cursor.fetchall()}

        count_queued = 0
        count_skipped = 0

        # Iterate over all configured library roots
        library_roots = get_library_roots()
        if not library_roots:
            app_logger.warning("No libraries configured, skipping scan")
            return

        for library_root in library_roots:
            if not os.path.exists(library_root):
                app_logger.warning(f"Library path not found, skipping: {library_root}")
                continue
            app_logger.info(f"Scanning library: {library_root}")
            for root, dirs, files in os.walk(library_root):
                for file in files:
                    if file.lower().endswith(('.cbz', '.cbr', '.zip', '.rar', '.pdf')):
                        full_path = os.path.join(root, file)
                        try:
                            stat = os.stat(full_path)
                            current_mtime = stat.st_mtime

                            should_process = False

                            if full_path not in existing_jobs:
                                should_process = True  # New file
                            else:
                                status, stored_mtime = existing_jobs[full_path]
                                # Check if file modified since last scan
                                # stored_mtime might be None if migrated
                                if stored_mtime is None or current_mtime > stored_mtime:
                                    should_process = True
                                elif status == 'error':
                                    # Optional: Retry errors? Let's skip for now to avoid loops,
                                    # or maybe retry once per startup?
                                    # For now, assume errors are permanent until file changes.
                                    pass

                            if should_process:
                                # Update DB to mark as pending/processing and update mtime
                                conn.execute("""
                                    INSERT INTO thumbnail_jobs (path, status, file_mtime, updated_at)
                                    VALUES (?, 'processing', ?, CURRENT_TIMESTAMP)
                                    ON CONFLICT(path) DO UPDATE SET
                                        status='processing',
                                        file_mtime=excluded.file_mtime,
                                        updated_at=CURRENT_TIMESTAMP
                                """, (full_path, current_mtime))

                                # Queue the job
                                path_hash = hashlib.md5(full_path.encode('utf-8'), usedforsecurity=False).hexdigest()
                                shard_dir = path_hash[:2]
                                filename = f"{path_hash}.jpg"
                                thumbnails_dir = os.path.join(config.get("SETTINGS", "CACHE_DIR", fallback="/cache"), "thumbnails")
                                cache_path = os.path.join(thumbnails_dir, shard_dir, filename)

                                thumbnail_executor.submit(generate_thumbnail_task, full_path, cache_path)
                                count_queued += 1
                            else:
                                count_skipped += 1

                        except OSError as e:
                            app_logger.error(f"Error accessing file {full_path}: {e}")

                # Commit batches or at end? At end is fine for single thread scan
                conn.commit()
            
        app_logger.info(f"Library scan complete. Queued {count_queued} thumbnails, skipped {count_skipped}.")
        
    except Exception as e:
        app_logger.error(f"Error during library scan: {e}")
    finally:
        conn.close()

# Start background scanner
def start_background_scanner():
    # Delay slightly to let app startup finish
    def run():
        time.sleep(5) 
        scan_library_task()
    
    thread = threading.Thread(target=run, daemon=True)
    thread.start()

start_background_scanner()

def refresh_wanted_cache_background():
    """
    Rebuild wanted issues cache for all mapped series.
    This runs in a background thread and does the heavy file I/O work.
    """
    with app_state.wanted_refresh_lock:
        if app_state.wanted_refresh_in_progress:
            app_logger.info("Wanted refresh already in progress, skipping")
            return
        app_state.wanted_refresh_in_progress = True

    try:
        from database import (get_all_mapped_series, get_issues_for_series,
                             save_wanted_issues_for_series, clear_wanted_cache_all,
                             get_manual_status_for_series)

        app_logger.info("Starting wanted issues cache refresh...")
        start_time = time.time()

        # Clear existing cache
        clear_wanted_cache_all()

        # Get all mapped series
        mapped_series = get_all_mapped_series()
        total_wanted = 0

        for series in mapped_series:
            series_id = series['id']
            series_name = series.get('name', '')
            series_volume = series.get('volume')
            mapped_path = series.get('mapped_path')

            if not mapped_path or not os.path.exists(mapped_path):
                continue

            # Get cached issues for this series
            issues = get_issues_for_series(series_id)
            if not issues:
                continue

            # Convert issues to objects for matching function
            from models.issue import IssueObj, SeriesObj
            issue_objs = [IssueObj(i) for i in issues]
            series_obj = SeriesObj(series)

            # Check which issues are in collection
            issue_status = match_issues_to_collection(mapped_path, issue_objs, series_obj)

            # Get manual status for this series (owned/skipped)
            manual_status = get_manual_status_for_series(series_id)

            # Find missing/wanted issues (exclude found and manually marked)
            wanted_list = []
            for issue in issues:
                issue_num = str(issue.get('number', ''))
                status = issue_status.get(issue_num, {})
                has_manual = issue_num in manual_status

                if not status.get('found') and not has_manual:
                    wanted_list.append(issue)

            # Save to cache
            if wanted_list:
                save_wanted_issues_for_series(series_id, series_name, series_volume, wanted_list)
                total_wanted += len(wanted_list)

        elapsed = time.time() - start_time
        app_logger.info(f"Wanted issues cache refresh complete: {total_wanted} issues in {elapsed:.2f}s")

    except Exception as e:
        app_logger.error(f"Error during wanted issues cache refresh: {e}")
    finally:
        with app_state.wanted_refresh_lock:
            app_state.wanted_refresh_in_progress = False
            app_state.wanted_last_refresh_time = time.time()


# Moved to helpers/collection.py - re-exported for backward compatibility
# Moved to helpers/collection.py - re-exported for backward compatibility
from helpers.collection import generate_filename_pattern, extract_comicinfo, match_issues_to_collection


# app = Flask(__name__)

# Legacy constant for backwards compatibility - use get_library_roots() instead
DATA_DIR = "/data"  # Directory to browse (deprecated, kept for compatibility)
TARGET_DIR = config.get("SETTINGS", "TARGET", fallback="/processed")


# Moved to helpers/library.py - re-exported for backward compatibility
from helpers.library import get_library_roots, get_default_library, is_valid_library_path, get_library_for_path

#########################
#   Recent Files Helper #
#########################

def log_file_if_in_data(file_path):
    """
    Log a file to recent_files if it's in a configured library and is a comic file.

    Args:
        file_path: Full path to the file
    """
    try:
        # Check if file is in any configured library
        if not is_valid_library_path(file_path):
            app_logger.debug(f"File not in any configured library: {file_path}")
            return

        # Check if it's a file (not directory)
        if not os.path.isfile(file_path):
            return

        # Check if it's a comic file
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in ['.cbz', '.cbr']:
            return

        # Log the file
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else None
        success = log_recent_file(file_path, file_name, file_size)
        if success:
            app_logger.info(f"📚 Logged recent file to database: {file_name}")
        else:
            app_logger.warning(f"Failed to log recent file: {file_name}")

    except Exception as e:
        app_logger.error(f"Error logging recent file {file_path}: {e}")

def update_recent_files_from_scan(comic_files):
    """
    Update the recent_files database with the 100 most recently modified comic files.
    This is called during index build to capture files added outside the app.

    Args:
        comic_files: List of dicts with keys: path, name, size, mtime
    """
    try:
        if not comic_files:
            app_logger.debug("No comic files found during scan")
            return

        # Get database connection
        conn = get_db_connection()
        if not conn:
            app_logger.error("Could not get database connection for recent files update")
            return

        c = conn.cursor()

        # Check how many files we currently have
        c.execute('SELECT COUNT(*) FROM recent_files')
        current_count = c.fetchone()[0]

        # Only do a full rescan if we have fewer than 100 files
        # This preserves files added via the app (which have accurate timestamps)
        # while still populating the list for files added externally
        if current_count >= 100:
            conn.close()
            app_logger.debug(f"Recent files already has {current_count} entries, skipping scan update")
            return

        app_logger.info(f"Populating recent_files database from {len(comic_files)} scanned files ({current_count} existing)...")

        # Sort by modification time (most recent first) - use heapq for efficiency with large lists
        # If we have a huge number of files, use heapq.nlargest for better performance
        if len(comic_files) > 10000:
            app_logger.info("Large library detected, using optimized sorting...")
            top_100 = heapq.nlargest(100, comic_files, key=lambda x: x['mtime'])
        else:
            sorted_files = sorted(comic_files, key=lambda x: x['mtime'], reverse=True)
            top_100 = sorted_files[:100]

        # Clear existing entries and insert fresh data
        c.execute('DELETE FROM recent_files')

        # Batch insert for better performance
        records = [
            (
                file_info['path'],
                file_info['name'],
                file_info['size'],
                datetime.fromtimestamp(file_info['mtime']).strftime('%Y-%m-%d %H:%M:%S')
            )
            for file_info in top_100
        ]

        c.executemany('''
            INSERT INTO recent_files (file_path, file_name, file_size, added_at)
            VALUES (?, ?, ?, ?)
        ''', records)

        conn.commit()
        conn.close()

        app_logger.info(f"✅ Recent files database populated with {len(top_100)} files")

    except Exception as e:
        app_logger.error(f"Error updating recent files from scan: {e}")
        app_logger.error(f"Traceback: {traceback.format_exc()}")

# Moved to helpers/library.py - re-exported for backward compatibility
from helpers.library import is_critical_path, get_critical_path_error_message

#########################
#     Cache System      #
#########################

# Global cache for directory listings with thread safety
cache_lock = threading.RLock()
directory_cache = OrderedDict()  # Use OrderedDict for LRU behavior
cache_timestamps = {}
cache_stats = {
    'hits': 0,
    'misses': 0,
    'evictions': 0,
    'invalidations': 0
}
CACHE_DURATION = 5  # Cache for 5 seconds
MAX_CACHE_SIZE = 500  # Increased maximum number of cached directories
CACHE_REBUILD_INTERVAL = 6 * 60 * 60  # 6 hours in seconds
last_cache_rebuild = time.time()
last_cache_invalidation = None  # Track when cache was last invalidated

def get_directory_hash(path):
    """Generate a more robust hash for directory contents to detect changes."""
    try:
        stat = os.stat(path)
        # Include inode and creation time for better change detection
        inode = getattr(stat, 'st_ino', 0)
        ctime = getattr(stat, 'st_ctime', 0)
        # Use modification time, size, inode, and creation time
        return f"{stat.st_mtime}_{stat.st_size}_{inode}_{ctime}"
    except Exception as e:
        app_logger.debug(f"Error generating hash for {path}: {e}")
        return "error"

def is_cache_valid(cache_key):
    """Check if cached data is still valid with thread safety."""
    with cache_lock:
        if cache_key not in cache_timestamps:
            app_logger.debug(f"Cache key not found in timestamps: {cache_key}")
            return False

        # Check if cache has expired
        age = time.time() - cache_timestamps[cache_key]
        if age > CACHE_DURATION:
            app_logger.debug(f"Cache expired for {cache_key} (age: {age:.1f}s > {CACHE_DURATION}s)")
            return False

        # For browse: cache, just use TTL validation (no hash check)
        # The hash changes on directory access which causes false invalidations
        # We have automatic invalidation on file operations, so TTL is sufficient
        if cache_key.startswith("browse:"):
            app_logger.debug(f"Browse cache valid for {cache_key} (age: {age:.1f}s)")
            return True

        # For regular directory listings, still use hash validation
        actual_path = cache_key
        current_hash = get_directory_hash(actual_path)
        cached_data = directory_cache.get(cache_key, {})
        cached_hash = cached_data.get('hash') if isinstance(cached_data, dict) else None

        if current_hash != cached_hash:
            app_logger.debug(f"Hash mismatch for {cache_key}: current={current_hash}, cached={cached_hash}")
            return False

        return True

def cleanup_cache():
    """Remove expired entries from cache with improved LRU management."""
    with cache_lock:
        current_time = time.time()
        expired_paths = [
            path for path, timestamp in cache_timestamps.items()
            if current_time - timestamp > CACHE_DURATION
        ]

        for path in expired_paths:
            if path in directory_cache:
                directory_cache.pop(path, None)
                cache_timestamps.pop(path, None)
                cache_stats['evictions'] += 1

        # Enforce size limit with LRU eviction
        while len(directory_cache) > MAX_CACHE_SIZE:
            # Remove oldest item (first in OrderedDict)
            oldest_path = next(iter(directory_cache))
            directory_cache.pop(oldest_path, None)
            cache_timestamps.pop(oldest_path, None)
            cache_stats['evictions'] += 1


def get_directory_listing(path):
    """Get directory listing with optimized file system operations, caching, and memory awareness."""
    try:
        # Check if we have valid cached data
        if is_cache_valid(path):
            with cache_lock:
                cached_data = directory_cache.get(path)
                if cached_data:
                    cache_stats['hits'] += 1
                    app_logger.debug(f"Cache HIT for directory: {path}")
                    # Move to end for LRU
                    directory_cache.move_to_end(path)
                    return cached_data

        # Cache miss - need to read from filesystem
        cache_stats['misses'] += 1
        app_logger.debug(f"Cache MISS for directory: {path}")

        # Check memory before operation and adjust cache size if needed
        monitor = get_global_monitor()
        memory_usage = monitor.get_memory_usage()

        # Reduce cache size if memory is high
        if memory_usage > 800:  # 800MB threshold
            with cache_lock:
                target_size = max(50, MAX_CACHE_SIZE // 2)
                while len(directory_cache) > target_size:
                    oldest_path = next(iter(directory_cache))
                    directory_cache.pop(oldest_path, None)
                    cache_timestamps.pop(oldest_path, None)
                    cache_stats['evictions'] += 1

        with memory_context("list_directories"):
            entries = os.listdir(path)

            # Single pass to categorize entries
            directories = []
            files = []
            excluded_extensions = {".png", ".jpg", ".jpeg", ".gif", ".html", ".css", ".ds_store", ".json", ".db"}
            excluded_files = {"cvinfo"}
            allowed_files = {"missing.txt"}

            for entry in entries:
                if entry.startswith(('.', '_')):
                    continue

                full_path = os.path.join(path, entry)
                try:
                    stat = os.stat(full_path)
                    if stat.st_mode & 0o40000:  # Directory
                        directories.append(entry)
                    else:  # File
                        # Check if file should be excluded (but allow specific files like missing.txt)
                        if entry.lower() in excluded_files:
                            continue
                        if entry.lower() in allowed_files or not any(entry.lower().endswith(ext) for ext in excluded_extensions):
                            files.append({
                                "name": entry,
                                "size": stat.st_size
                            })
                except (OSError, IOError):
                    # Skip files we can't access
                    continue

            # Sort both lists
            directories.sort(key=lambda s: s.lower())
            files.sort(key=lambda f: f["name"].lower())

            result = {
                "directories": directories,
                "files": files,
                "hash": get_directory_hash(path)
            }

            # Store in cache
            with cache_lock:
                directory_cache[path] = result
                cache_timestamps[path] = time.time()
                # Move to end for LRU
                directory_cache.move_to_end(path)

                # Enforce cache size limit
                while len(directory_cache) > MAX_CACHE_SIZE:
                    oldest_path = next(iter(directory_cache))
                    directory_cache.pop(oldest_path, None)
                    cache_timestamps.pop(oldest_path, None)
                    cache_stats['evictions'] += 1

            # Cleanup expired entries periodically
            if cache_stats['misses'] % 10 == 0:
                cleanup_cache()

            return result
    except Exception as e:
        app_logger.error(f"Error getting directory listing for {path}: {e}")
        raise

def invalidate_cache_for_path(path):
    """Invalidate cache for a specific path and its parent with improved tracking."""
    global last_cache_invalidation

    # Skip cache invalidation for WATCH and TARGET directories
    if is_critical_path(path):
        app_logger.debug(f"Skipping cache invalidation for critical path: {path}")
        return

    # Invalidate database browse cache
    invalidate_browse_cache(path)

    with cache_lock:
        invalidated_count = 0

        # Invalidate the specific path (both regular and browse: prefixed)
        if path in directory_cache:
            directory_cache.pop(path, None)
            cache_timestamps.pop(path, None)
            invalidated_count += 1

        browse_key = f"browse:{path}"
        if browse_key in directory_cache:
            directory_cache.pop(browse_key, None)
            cache_timestamps.pop(browse_key, None)
            invalidated_count += 1

        # Also invalidate parent directory cache (both regular and browse: prefixed)
        parent = os.path.dirname(path)
        if parent:
            if parent in directory_cache:
                directory_cache.pop(parent, None)
                cache_timestamps.pop(parent, None)
                invalidated_count += 1

            parent_browse_key = f"browse:{parent}"
            if parent_browse_key in directory_cache:
                directory_cache.pop(parent_browse_key, None)
                cache_timestamps.pop(parent_browse_key, None)
                invalidated_count += 1

        # Invalidate any child directory caches (both regular and browse: prefixed)
        paths_to_invalidate = []
        for cached_key in directory_cache.keys():
            # Handle both regular paths and browse: prefixed keys
            if cached_key.startswith("browse:"):
                cached_path = cached_key[7:]
            else:
                cached_path = cached_key

            if cached_path.startswith(path + os.sep):
                paths_to_invalidate.append(cached_key)

        for cached_key in paths_to_invalidate:
            directory_cache.pop(cached_key, None)
            cache_timestamps.pop(cached_key, None)
            invalidated_count += 1

        cache_stats['invalidations'] += invalidated_count

    # Also invalidate directory stats cache when files change
    app_state.data_dir_stats_last_update = 0

    # Track when cache invalidation occurred
    last_cache_invalidation = time.time()

    if invalidated_count > 0:
        app_logger.debug(f"Invalidated {invalidated_count} memory cache entries for path: {path}")

def rebuild_entire_cache():
    """Rebuild the entire directory cache and search index."""
    global directory_cache, cache_timestamps, last_cache_rebuild, last_cache_invalidation

    app_logger.info("🔄 Starting scheduled cache rebuild...")
    start_time = time.time()

    with cache_lock:
        cleared_count = len(directory_cache)
        # Clear all caches
        directory_cache.clear()
        cache_timestamps.clear()
        # Keep performance stats but mark rebuild
        cache_stats['evictions'] += cleared_count

    # Rebuild search index
    build_file_index()

    # Update rebuild timestamp and reset invalidation
    last_cache_rebuild = time.time()
    last_cache_invalidation = None  # Reset invalidation tracking after rebuild

    rebuild_time = time.time() - start_time
    app_logger.info(f"✅ Cache rebuild completed in {rebuild_time:.2f} seconds ({cleared_count} entries cleared)")

    # Warm up cache with frequently accessed directories
    warmup_cache()

    return rebuild_time

def warmup_cache():
    """Proactively cache frequently accessed directories."""
    warmup_paths = [DATA_DIR, TARGET_DIR]

    # Add common subdirectories
    for base_path in [DATA_DIR, TARGET_DIR]:
        try:
            if os.path.exists(base_path):
                subdirs = [d for d in os.listdir(base_path)
                          if os.path.isdir(os.path.join(base_path, d)) and not d.startswith('.') and not d.startswith('_')]
                # Add first few subdirectories to warmup
                for subdir in subdirs[:5]:
                    warmup_paths.append(os.path.join(base_path, subdir))
        except (OSError, IOError):
            continue

    # Pre-cache these directories
    warmed_count = 0
    for path in warmup_paths:
        try:
            if os.path.exists(path) and path not in directory_cache:
                listing_data = get_directory_listing(path)
                with cache_lock:
                    directory_cache[path] = listing_data
                    cache_timestamps[path] = time.time()
                warmed_count += 1

                # Don't warm up too many at once
                if warmed_count >= 10:
                    break
        except Exception as e:
            app_logger.debug(f"Failed to warm up cache for {path}: {e}")

    if warmed_count > 0:
        app_logger.info(f"🔥 Warmed up cache with {warmed_count} frequently accessed directories")

@app.route('/warmup-cache', methods=['POST'])
def warmup_cache_endpoint():
    """Manually trigger cache warmup."""
    try:
        warmup_cache()
        return jsonify({"success": True, "message": "Cache warmup completed"})
    except Exception as e:
        app_logger.error(f"Error during cache warmup: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

def should_rebuild_cache():
    """Check if it's time to rebuild the cache based on the interval."""
    global last_cache_rebuild
    return time.time() - last_cache_rebuild >= CACHE_REBUILD_INTERVAL

@app.route('/clear-cache', methods=['POST'])
def clear_cache():
    """Manually clear the directory cache."""
    global directory_cache, cache_timestamps, last_cache_invalidation

    with cache_lock:
        cleared_count = len(directory_cache)
        directory_cache.clear()
        cache_timestamps.clear()
        # Reset stats
        cache_stats['hits'] = 0
        cache_stats['misses'] = 0
        cache_stats['evictions'] = 0
        cache_stats['invalidations'] = 0

    last_cache_invalidation = time.time()
    app_state.data_dir_stats_last_update = 0  # Also invalidate directory stats cache

    # Also clear stats cache (for insights page charts)
    clear_stats_cache()

    app_logger.info(f"Directory cache cleared manually ({cleared_count} entries)")
    return jsonify({"success": True, "message": f"Cache cleared ({cleared_count} entries)"})

#########################
#   File Index Routes   #
#########################

@app.route('/api/rebuild-file-index', methods=['POST'])
def api_rebuild_file_index():
    """Manually rebuild the file index using incremental sync."""
    global index_built

    try:
        app_logger.info("🔄 Manual file index sync requested...")
        start_time = time.time()

        # Scan filesystem to get current state
        app_logger.info("Scanning filesystem...")
        filesystem_entries = scan_filesystem_for_sync()
        scan_time = time.time() - start_time
        app_logger.info(f"Filesystem scan completed: {len(filesystem_entries)} entries in {scan_time:.2f}s")

        # Incremental sync (preserves metadata for existing files)
        app_logger.info("Performing incremental sync...")
        sync_result = sync_file_index_incremental(filesystem_entries)
        app_logger.info(f"Sync result: {sync_result['added']} added, {sync_result['removed']} removed, {sync_result['unchanged']} unchanged")

        # Queue only NEW files for metadata scanning
        if sync_result['added'] > 0:
            from metadata_scanner import queue_files_for_scan, PRIORITY_NEW_FILE
            new_cbz_paths = [p for p in sync_result['new_paths'] if p.lower().endswith('.cbz')]
            if new_cbz_paths:
                queue_files_for_scan(new_cbz_paths, PRIORITY_NEW_FILE)
                app_logger.info(f"Queued {len(new_cbz_paths)} new CBZ files for metadata scanning")

        # Also queue any other files that still need metadata scanning
        # (e.g., previously added files that were never scanned)
        from metadata_scanner import queue_pending_files
        queued = queue_pending_files()
        if queued:
            app_logger.info(f"Queued {queued} additional files for metadata scanning")

        # Refresh in-memory index from DB
        file_index.clear()
        db_index = get_file_index_from_db()
        if db_index:
            file_index.extend(db_index)
        index_built = True

        # Update last rebuild timestamp
        update_last_rebuild()

        # Clear and pre-populate stats cache
        clear_stats_cache()
        get_library_stats()
        get_file_type_distribution()
        get_top_publishers()
        get_reading_history_stats()

        elapsed = time.time() - start_time
        app_logger.info(f"✅ Manual file index sync completed in {elapsed:.2f}s")

        return jsonify({
            "success": True,
            "message": f"File index synced successfully in {elapsed:.2f} seconds",
            "added": sync_result['added'],
            "removed": sync_result['removed'],
            "unchanged": sync_result['unchanged'],
            "total_files": len([e for e in file_index if e['type'] == 'file']),
            "total_directories": len([e for e in file_index if e['type'] == 'directory'])
        })
    except Exception as e:
        app_logger.error(f"❌ File index sync failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/file-index-status', methods=['GET'])
def api_file_index_status():
    """Get the current status of the file index."""
    try:
        schedule = get_rebuild_schedule()

        total_files = len([e for e in file_index if e['type'] == 'file'])
        total_directories = len([e for e in file_index if e['type'] == 'directory'])

        last_rebuild = None
        if schedule and schedule.get('last_rebuild'):
            # Format last rebuild timestamp
            try:
                rebuild_dt = datetime.fromisoformat(schedule['last_rebuild'])
                time_diff = datetime.now() - rebuild_dt
                if time_diff.days > 0:
                    last_rebuild = f"{time_diff.days} day(s) ago"
                elif time_diff.seconds >= 3600:
                    hours = time_diff.seconds // 3600
                    last_rebuild = f"{hours} hour(s) ago"
                elif time_diff.seconds >= 60:
                    minutes = time_diff.seconds // 60
                    last_rebuild = f"{minutes} minute(s) ago"
                else:
                    last_rebuild = "Just now"
            except Exception:
                last_rebuild = schedule['last_rebuild']

        return jsonify({
            "success": True,
            "total_files": total_files,
            "total_directories": total_directories,
            "last_rebuild": last_rebuild,
            "index_built": index_built
        })
    except Exception as e:
        app_logger.error(f"Failed to get file index status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/get-rebuild-schedule', methods=['GET'])
def api_get_rebuild_schedule():
    """Get the current rebuild schedule configuration."""
    try:
        schedule = get_rebuild_schedule()
        if not schedule:
            return jsonify({
                "success": True,
                "schedule": {
                    "frequency": "disabled",
                    "time": "02:00",
                    "weekday": 0
                },
                "next_run": "Not scheduled"
            })

        return jsonify({
            "success": True,
            "schedule": {
                "frequency": schedule['frequency'],
                "time": schedule['time'],
                "weekday": schedule['weekday']
            },
            "next_run": get_next_run_for_job('file_index_rebuild')
        })
    except Exception as e:
        app_logger.error(f"Failed to get rebuild schedule: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/save-rebuild-schedule', methods=['POST'])
def api_save_rebuild_schedule():
    """Save the rebuild schedule configuration."""
    try:
        data = request.get_json()
        frequency = data.get('frequency', 'disabled')
        time_str = data.get('time', '02:00')
        weekday = int(data.get('weekday', 0))

        # Validate inputs
        if frequency not in ['disabled', 'daily', 'weekly']:
            return jsonify({"success": False, "error": "Invalid frequency"}), 400

        # Save to database
        if not db_save_rebuild_schedule(frequency, time_str, weekday):
            return jsonify({"success": False, "error": "Failed to save schedule to database"}), 500

        # Reconfigure the scheduler
        configure_rebuild_schedule()

        app_logger.info(f"✅ Rebuild schedule saved: {frequency} at {time_str}")

        return jsonify({
            "success": True,
            "message": f"Rebuild schedule saved successfully: {frequency} at {time_str}"
        })
    except Exception as e:
        app_logger.error(f"Failed to save rebuild schedule: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/run-sync-now', methods=['POST'])
def api_run_sync_now():
    """Manually trigger a series sync immediately."""
    try:
        # Run in a background thread to not block the request
        threading.Thread(target=scheduled_series_sync, daemon=True).start()
        return jsonify({
            "success": True,
            "message": "Series sync started in background"
        })
    except Exception as e:
        app_logger.error(f"Failed to start sync: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


#########################
#     Global Values     #
#########################

# Global file index for fast searching
file_index = []
index_built = False

def build_file_index():
    """Build an in-memory index of all files and directories for fast searching"""
    global file_index, index_built

    if index_built:
        return

    # Try to load from database first
    app_logger.info("Loading file index from database...")
    start_time = time.time()

    db_index = get_file_index_from_db()
    if db_index and len(db_index) > 0:
        file_index = db_index
        index_built = True
        load_time = time.time() - start_time
        app_logger.info(f"✅ File index loaded from database: {len(file_index)} items in {load_time:.2f} seconds")
        return

    # Database empty, build from filesystem
    app_logger.info("Database empty, building file index from filesystem...")
    start_time = time.time()

    file_index.clear()
    excluded_extensions = {".png", ".jpg", ".jpeg", ".gif", ".html", ".css", ".ds_store", ".json", ".db"}
    excluded_files = {"cvinfo"}
    allowed_files = {"missing.txt"}

    # Track comic files for recent files database
    comic_files = []

    # Helper function to check for folder thumbnail
    def check_has_thumbnail(folder_path):
        for ext in ['.png', '.jpg', '.jpeg']:
            if os.path.exists(os.path.join(folder_path, f'folder{ext}')):
                return 1
        return 0

    try:
        # Iterate over all configured library roots
        library_roots = get_library_roots()
        if not library_roots:
            app_logger.warning("No libraries configured, cannot build file index")
            return

        for library_root in library_roots:
            if not os.path.exists(library_root):
                app_logger.warning(f"Library path not found, skipping: {library_root}")
                continue

            app_logger.info(f"Indexing library: {library_root}")

            for root, dirs, files in os.walk(library_root):
                # Skip hidden directories
                dirs[:] = [d for d in dirs if not d.startswith('.') and not d.startswith('_')]

                # Index directories
                for name in dirs:
                    try:
                        full_dir_path = os.path.join(root, name)
                        rel_path = os.path.relpath(full_dir_path, library_root)
                        # Use actual library root path in the stored path
                        dir_path = os.path.join(library_root, rel_path).replace('\\', '/')
                        parent_rel = os.path.dirname(rel_path)
                        parent_path = os.path.join(library_root, parent_rel).replace('\\', '/') if parent_rel else library_root
                        file_index.append({
                            "name": name,
                            "path": dir_path,
                            "type": "directory",
                            "parent": parent_path,
                            "has_thumbnail": check_has_thumbnail(full_dir_path)
                        })
                    except (OSError, IOError):
                        continue

                # Index files (excluding certain extensions, but allow specific files)
                for name in files:
                    if name.startswith('.') or name.startswith('_'):
                        continue

                    # Skip explicitly excluded files
                    if name.lower() in excluded_files:
                        continue

                    # Skip excluded file types (but allow specific files like missing.txt)
                    if name.lower() not in allowed_files and any(name.lower().endswith(ext) for ext in excluded_extensions):
                        continue

                    try:
                        full_path = os.path.join(root, name)
                        rel_path = os.path.relpath(full_path, library_root)
                        file_size = os.path.getsize(full_path)
                        mtime = os.path.getmtime(full_path)

                        # Use actual library root path in the stored path
                        file_path = os.path.join(library_root, rel_path).replace('\\', '/')
                        parent_rel = os.path.dirname(rel_path)
                        parent_path = os.path.join(library_root, parent_rel).replace('\\', '/') if parent_rel else library_root

                        file_index.append({
                            "name": name,
                            "path": file_path,
                            "type": "file",
                            "size": file_size,
                            "parent": parent_path,
                            "modified_at": mtime
                        })

                        # Track comic files for recent files list
                        if name.lower().endswith(('.cbz', '.cbr')):
                            try:
                                mtime = os.path.getmtime(full_path)
                                comic_files.append({
                                    'path': full_path,
                                    'name': name,
                                    'size': file_size,
                                    'mtime': mtime
                                })
                            except (OSError, IOError):
                                pass

                    except (OSError, IOError):
                        continue

    except Exception as e:
        app_logger.error(f"Error building file index: {e}")
        return

    build_time = time.time() - start_time
    app_logger.info(f"File index built successfully: {len(file_index)} items in {build_time:.2f} seconds")
    index_built = True

    # Save index to database for persistence
    app_logger.info("Saving file index to database...")
    save_start = time.time()
    if save_file_index_to_db(file_index):
        save_time = time.time() - save_start
        app_logger.info(f"✅ File index saved to database in {save_time:.2f} seconds")
    else:
        app_logger.warning("Failed to save file index to database")



    # Legacy: update_recent_files_from_scan(comic_files) - Removed as we now use file_index directly


def scan_filesystem_for_sync():
    """
    Scan the filesystem and return a list of entries without modifying the database.

    Used by incremental sync to compare filesystem state with database state.
    Excludes TARGET folder (from app.config) as those files should not be indexed.
    Scans all enabled libraries.

    Returns:
        List of dicts with {name, path, type, size, parent, has_thumbnail, modified_at}
    """
    entries = []
    excluded_extensions = {".png", ".jpg", ".jpeg", ".gif", ".html", ".css", ".ds_store", ".json", ".db"}
    excluded_files = {"cvinfo"}
    allowed_files = {"missing.txt"}

    # Get TARGET from app.config (the authoritative source)
    target_dir = app.config.get('TARGET', '/downloads/processed')
    normalized_target_dir = os.path.normpath(target_dir)

    def is_in_target_dir(path):
        """Check if path is within TARGET folder (should be excluded from index)."""
        normalized_path = os.path.normpath(path)
        try:
            common_path = os.path.commonpath([normalized_path, normalized_target_dir])
            return os.path.samefile(common_path, normalized_target_dir)
        except (ValueError, OSError):
            return normalized_path.startswith(normalized_target_dir)

    def check_has_thumbnail(folder_path):
        for ext in ['.png', '.jpg', '.jpeg']:
            if os.path.exists(os.path.join(folder_path, f'folder{ext}')):
                return 1
        return 0

    # Get all library roots to scan
    library_roots = get_library_roots()

    for library_root in library_roots:
        if not os.path.exists(library_root):
            app_logger.warning(f"Library path does not exist, skipping: {library_root}")
            continue

        try:
            for root, dirs, files in os.walk(library_root):
                # Skip hidden directories and TARGET_DIR
                dirs[:] = [d for d in dirs if not d.startswith('.') and not d.startswith('_')
                           and not is_in_target_dir(os.path.join(root, d))]

                # Index directories
                for name in dirs:
                    try:
                        full_dir_path = os.path.join(root, name)
                        rel_path = os.path.relpath(full_dir_path, library_root)
                        entries.append({
                            "name": name,
                            "path": f"{library_root}/{rel_path}",
                            "type": "directory",
                            "parent": f"{library_root}/{os.path.dirname(rel_path)}" if os.path.dirname(rel_path) else library_root,
                            "has_thumbnail": check_has_thumbnail(full_dir_path),
                            "size": None,
                            "modified_at": None
                        })
                    except (OSError, IOError):
                        continue

                # Index files
                for name in files:
                    if name.startswith('.') or name.startswith('_'):
                        continue

                    # Skip explicitly excluded files
                    if name.lower() in excluded_files:
                        continue

                    # Skip excluded file types (but allow specific files like missing.txt)
                    if name.lower() not in allowed_files and any(name.lower().endswith(ext) for ext in excluded_extensions):
                        continue

                    try:
                        full_path = os.path.join(root, name)
                        rel_path = os.path.relpath(full_path, library_root)
                        file_size = os.path.getsize(full_path)
                        mtime = os.path.getmtime(full_path)

                        entries.append({
                            "name": name,
                            "path": f"{library_root}/{rel_path}",
                            "type": "file",
                            "size": file_size,
                            "parent": f"{library_root}/{os.path.dirname(rel_path)}" if os.path.dirname(rel_path) else library_root,
                            "has_thumbnail": 0,
                            "modified_at": mtime
                        })
                    except (OSError, IOError):
                        continue

        except Exception as e:
            app_logger.error(f"Error scanning library {library_root} for sync: {e}")

    return entries


def update_index_on_move(old_path, new_path):
    """
    Update file index when a file or directory is moved.
    Handles three scenarios:
    1. Move from outside /data to /data -> ADD to index (unless in TARGET folder)
    2. Move within /data -> UPDATE in index
    3. Move from /data to outside /data -> DELETE from index

    Note: Files in TARGET folder (from app.config) are excluded from the index.

    Args:
        old_path: Original path
        new_path: New path after move
    """
    try:
        # Normalize paths for comparison
        normalized_old = os.path.normpath(old_path)
        normalized_new = os.path.normpath(new_path)
        normalized_data_dir = os.path.normpath(DATA_DIR)

        # Get TARGET from app.config (the authoritative source)
        target_dir = app.config.get('TARGET', '/downloads/processed')
        normalized_target_dir = os.path.normpath(target_dir)

        # Check if path is in TARGET folder (should be excluded from index)
        def is_in_target_dir(path):
            try:
                common_path = os.path.commonpath([path, normalized_target_dir])
                return os.path.samefile(common_path, normalized_target_dir)
            except (ValueError, OSError):
                # Fallback: Check if normalized path starts with TARGET folder
                return path.startswith(normalized_target_dir)

        # Check if old and new paths are in DATA_DIR using robust comparison
        # Same logic as log_file_if_in_data() for consistency
        def is_in_data_dir(path):
            try:
                common_path = os.path.commonpath([path, normalized_data_dir])
                return os.path.samefile(common_path, normalized_data_dir)
            except (ValueError, OSError):
                # Fallback: Check if normalized path starts with DATA_DIR
                return path.startswith(normalized_data_dir)

        old_in_data = is_in_data_dir(normalized_old)
        new_in_data = is_in_data_dir(normalized_new)
        new_in_target = is_in_target_dir(normalized_new)

        # Debug logging to help diagnose path comparison issues
        app_logger.debug(f"Path comparison - Old: {normalized_old} (in_data: {old_in_data})")
        app_logger.debug(f"Path comparison - New: {normalized_new} (in_data: {new_in_data}, in_target: {new_in_target})")
        app_logger.debug(f"Path comparison - DATA_DIR: {normalized_data_dir}, TARGET_DIR: {normalized_target_dir}")

        # Skip files in TARGET_DIR - they should not be indexed
        if new_in_target:
            app_logger.debug(f"Skipping index update - file is in TARGET folder: {new_path}")
            return

        # Scenario 1: Moving INTO /data (from WATCH/TEMP) -> ADD to index
        if not old_in_data and new_in_data:
            app_logger.info(f"📥 Adding to index (moved into /data): {new_path}")
            update_index_on_create(new_path)
            return

        # Scenario 2: Moving OUT OF /data -> DELETE from index
        if old_in_data and not new_in_data:
            app_logger.info(f"📤 Removing from index (moved out of /data): {old_path}")
            update_index_on_delete(old_path)
            return

        # Scenario 3: Moving WITHIN /data -> UPDATE in index
        if old_in_data and new_in_data:
            app_logger.info(f"🔄 Updating index (moved within /data): {old_path} -> {new_path}")

            excluded_extensions = {".png", ".jpg", ".jpeg", ".gif", ".html", ".css", ".ds_store", ".json", ".db", ".xml"}
            excluded_files = {"cvinfo"}
            allowed_files = {"missing.txt"}
            is_file = os.path.isfile(new_path)

            if is_file:
                # Update single file entry
                file_name = os.path.basename(new_path)
                _, ext = os.path.splitext(file_name.lower())

                # Skip excluded files (but allow specific files like missing.txt)
                if file_name.lower() in excluded_files:
                    return
                if file_name.lower() not in allowed_files and (ext in excluded_extensions or file_name.startswith(('.', '-', '_'))):
                    return

                parent = os.path.dirname(new_path)
                update_file_index_entry(old_path, name=file_name, new_path=new_path, parent=parent)
                app_logger.debug(f"Updated file index for moved file: {old_path} -> {new_path}")

            else:
                # Update directory and all children
                # First update the directory itself
                dir_name = os.path.basename(new_path)
                parent = os.path.dirname(new_path)
                update_file_index_entry(old_path, name=dir_name, new_path=new_path, parent=parent)

                # Update all children paths
                # We need to update paths that start with old_path to start with new_path
                conn = get_db_connection()
                if conn:
                    c = conn.cursor()
                    # Update all entries whose path starts with old_path
                    c.execute('''
                        UPDATE file_index
                        SET path = ? || SUBSTR(path, ?),
                            parent = ? || SUBSTR(parent, ?)
                        WHERE path LIKE ?
                    ''', (new_path, len(old_path) + 1, new_path, len(old_path) + 1, f"{old_path}/%"))

                    conn.commit()
                    rows_affected = c.rowcount
                    conn.close()
                    app_logger.debug(f"Updated {rows_affected} child entries for moved directory: {old_path} -> {new_path}")

            return

        # Scenario 4: Both outside /data -> do nothing
        app_logger.debug(f"File moved outside /data, no index update needed: {old_path} -> {new_path}")

    except Exception as e:
        app_logger.error(f"Failed to update index on move {old_path} -> {new_path}: {e}")

def update_index_on_delete(path):
    """
    Update file index when a file or directory is deleted.

    Args:
        path: Path of deleted item
    """
    try:
        delete_file_index_entry(path)
        app_logger.debug(f"Updated file index for deleted item: {path}")
    except Exception as e:
        app_logger.error(f"Failed to update index on delete {path}: {e}")

def update_index_on_create(path):
    """
    Update file index when a file or directory is created.
    If it's a directory, recursively indexes all contents.

    Args:
        path: Path of new item
    """
    try:
        excluded_extensions = {".png", ".jpg", ".jpeg", ".gif", ".html", ".css", ".ds_store", ".json", ".db", ".xml"}
        excluded_files = {"cvinfo"}
        allowed_files = {"missing.txt"}

        is_file = os.path.isfile(path)
        name = os.path.basename(path)
        parent = os.path.dirname(path)

        if is_file:
            # Check if file should be indexed (but allow specific files like missing.txt)
            if name.lower() in excluded_files:
                return
            _, ext = os.path.splitext(name.lower())
            if name.lower() not in allowed_files and (ext in excluded_extensions or name.startswith(('.', '-', '_'))):
                return

            size = os.path.getsize(path) if os.path.exists(path) else None
            mtime = os.path.getmtime(path) if os.path.exists(path) else None
            add_file_index_entry(name, path, 'file', size=size, parent=parent, modified_at=mtime)
            app_logger.debug(f"Added file to index: {path}")
        else:
            # Directory - add it and recursively add all contents
            add_file_index_entry(name, path, 'directory', parent=parent)
            app_logger.debug(f"Added directory to index: {path}")

            # Recursively index all files and subdirectories
            try:
                for root, dirs, files in os.walk(path):
                    # Skip hidden directories
                    dirs[:] = [d for d in dirs if not d.startswith('.') and not d.startswith('_')]

                    # Index subdirectories
                    for dir_name in dirs:
                        dir_path = os.path.join(root, dir_name)
                        dir_parent = os.path.dirname(dir_path)
                        add_file_index_entry(dir_name, dir_path, 'directory', parent=dir_parent)

                    # Index files
                    for file_name in files:
                        if file_name.startswith('.') or file_name.startswith('_'):
                            continue

                        if file_name.lower() in excluded_files:
                            continue

                        _, ext = os.path.splitext(file_name.lower())
                        if file_name.lower() not in allowed_files and ext in excluded_extensions:
                            continue

                        file_path = os.path.join(root, file_name)
                        file_parent = os.path.dirname(file_path)
                        try:
                            file_size = os.path.getsize(file_path)
                            add_file_index_entry(file_name, file_path, 'file', size=file_size, parent=file_parent)
                        except (OSError, IOError):
                            continue

                app_logger.info(f"Recursively indexed directory and contents: {path}")
            except Exception as e:
                app_logger.error(f"Error recursively indexing directory {path}: {e}")

    except Exception as e:
        app_logger.error(f"Failed to update index on create {path}: {e}")

@app.context_processor
def inject_global_vars():
    return {
        'monitor': os.getenv("MONITOR", "no"),
        'version': __version__,
        'bootstrap_theme': app.config.get('BOOTSTRAP_THEME', 'default')
    }

@app.context_processor
def inject_metron_available():
    """Inject metron_available flag for templates (e.g., to show/hide Pull List menu)."""
    # Check if Metron credentials exist in the database
    metron_creds = get_provider_credentials('metron')
    return {'metron_available': metron_creds is not None and metron_creds.get('username') and metron_creds.get('password')}

#########################
#     Logging Setup     #
#########################

# app_logger, APP_LOG, and MONITOR_LOG are now imported from app_logging module
# Set log level from config (default to INFO = debug disabled)
debug_enabled = config.get("SETTINGS", "ENABLE_DEBUG_LOGGING", fallback="False") == "True"
app_logger.setLevel(logging.DEBUG if debug_enabled else logging.INFO)
app_logger.info(f"App started successfully! (Debug logging: {'enabled' if debug_enabled else 'disabled'})")

# Initialize memory management
initialize_memory_management()

@app.route('/api/continue-reading', methods=['GET'])
def api_continue_reading():
    """Get comics with in-progress reading positions for Continue Reading section."""
    try:
        limit = request.args.get('limit', 10, type=int)
        if limit > 100:
            limit = 100  # Cap at 100

        items = get_continue_reading_items(limit=limit)

        return jsonify({
            "success": True,
            "items": items,
            "total_count": len(items)
        })

    except Exception as e:
        app_logger.error(f"Error in api_continue_reading: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


#####################################
#  Auto-Fetch ComicVine Metadata    #
#####################################
def auto_fetch_comicvine_metadata(destination_path):
    """
    Automatically fetch ComicVine metadata for moved files if conditions are met.
    Only triggers for non-root /data directories that have a cvinfo file.

    Returns:
        The final file path (renamed path if file was renamed, original path otherwise)
    """
    try:
        from models.comicvine import auto_fetch_metadata_for_folder

        # Check 1: Is COMICVINE_API_KEY configured?
        api_key = app.config.get("COMICVINE_API_KEY", "")
        if not api_key:
            app_logger.debug("ComicVine API key not configured, skipping auto-metadata")
            return destination_path

        # Determine the folder to check for cvinfo
        if os.path.isfile(destination_path):
            folder_path = os.path.dirname(destination_path)
            target_file = destination_path
        else:
            folder_path = destination_path
            target_file = None

        # Check: Is this a non-root /data directory? (must have at least 2 path components after /data)
        data_dir = DATA_DIR
        rel_path = os.path.relpath(folder_path, data_dir)
        # Normalize path separators for cross-platform compatibility
        rel_path_normalized = rel_path.replace("\\", "/")
        if rel_path == "." or "/" not in rel_path_normalized:
            app_logger.debug(f"Skipping auto-metadata for root-level directory: {folder_path}")
            return destination_path

        # Trigger metadata fetch for the folder
        result = auto_fetch_metadata_for_folder(folder_path, api_key, target_file=target_file)

        if result['processed'] > 0:
            app_logger.info(f"Auto-fetched ComicVine metadata: {result['processed']} processed, {result['skipped']} skipped, {result['errors']} errors")

            # Queue processed files for metadata scanning to update file_index
            from metadata_scanner import queue_file_for_scan, PRIORITY_NEW_FILE
            for detail in result.get('details', []):
                if detail.get('status') == 'success':
                    # Use renamed path if available, otherwise original
                    file_path = detail.get('renamed_to') or detail.get('file')
                    if file_path and file_path.lower().endswith('.cbz'):
                        queue_file_for_scan(file_path, PRIORITY_NEW_FILE)
                        app_logger.debug(f"Queued for metadata scan: {os.path.basename(file_path)}")

            # Check if the target file was renamed and return the new path
            if target_file:
                for detail in result.get('details', []):
                    if detail.get('renamed_to') and detail.get('file') == target_file:
                        app_logger.info(f"File was renamed: {target_file} -> {detail['renamed_to']}")
                        return detail['renamed_to']

        return destination_path

    except Exception as e:
        app_logger.error(f"Error in auto-fetch metadata: {e}")
        return destination_path


def auto_fetch_metron_metadata(destination_path):
    """
    Automatically fetch Metron metadata for moved files if conditions are met.
    Only triggers for non-root /data directories that have a cvinfo file.

    Returns:
        The final file path (renamed path if file was renamed, original path otherwise)
    """
    try:
        from models.metron import (
            get_api, get_series_id,
            get_issue_metadata, map_to_comicinfo
        )
        from models.providers.base import extract_issue_number
        from models.comicvine import generate_comicinfo_xml, add_comicinfo_to_archive
        from comicinfo import read_comicinfo_from_zip
        from cbz_ops.rename import load_custom_rename_config, smart_title_case

        # Check 1: Are Metron credentials configured?
        username = app.config.get("METRON_USERNAME", "")
        password = app.config.get("METRON_PASSWORD", "")
        if not username or not password:
            app_logger.debug("Metron credentials not configured, skipping Metron metadata")
            return destination_path

        # Determine the folder to check for cvinfo
        if os.path.isfile(destination_path):
            folder_path = os.path.dirname(destination_path)
            target_file = destination_path
        else:
            folder_path = destination_path
            target_file = None

        # Check 3: Is this a non-root /data directory?
        data_dir = DATA_DIR
        rel_path = os.path.relpath(folder_path, data_dir)
        rel_path_normalized = rel_path.replace("\\", "/")
        if rel_path == "." or "/" not in rel_path_normalized:
            app_logger.debug(f"Skipping Metron metadata for root-level directory: {folder_path}")
            return destination_path

        # Check 4: Does cvinfo exist?
        cvinfo_path = None
        for filename in os.listdir(folder_path):
            if filename.lower() == 'cvinfo':
                cvinfo_path = os.path.join(folder_path, filename)
                break

        if not cvinfo_path:
            app_logger.debug(f"No cvinfo file found in {folder_path}, skipping Metron metadata")
            return destination_path

        # Initialize Metron API
        api = get_api(username, password)
        if not api:
            app_logger.warning("Failed to initialize Metron API")
            return destination_path

        # Get Metron series ID (from cvinfo or lookup by CV ID)
        series_id = get_series_id(cvinfo_path, api)
        if not series_id:
            app_logger.debug("Could not determine Metron series ID")
            return destination_path

        # Track if we've saved cvinfo fields (only need to do once per folder)
        cvinfo_fields_saved = False

        # If target_file specified, only process that file
        if target_file:
            files_to_process = [target_file]
        else:
            # Get all comic files in folder
            files_to_process = [
                os.path.join(folder_path, f) for f in os.listdir(folder_path)
                if f.lower().endswith(('.cbz', '.cbr'))
            ]

        processed = 0
        renamed_path = None

        for file_path in files_to_process:
            # Skip if already has metadata
            existing = read_comicinfo_from_zip(file_path)
            existing_notes = existing.get('Notes', '').strip() if existing else ''
            # Skip if has metadata, unless it's just Amazon scraped data
            if existing_notes and 'Scraped metadata from Amazon' not in existing_notes:
                app_logger.debug(f"Skipping {file_path} - already has metadata")
                continue

            # Extract issue number from filename
            issue_number = extract_issue_number(os.path.basename(file_path))
            if not issue_number:
                app_logger.warning(f"Could not extract issue number from {file_path}")
                continue

            # Fetch metadata from Metron
            issue_data = get_issue_metadata(api, series_id, issue_number)
            if not issue_data:
                continue

            # Save publisher_name and start_year to cvinfo (once per folder)
            if not cvinfo_fields_saved:
                from models.metron import write_cvinfo_fields, _get_attr
                publisher = _get_attr(issue_data, 'publisher', {}) or {}
                publisher_name = _get_attr(publisher, 'name', None)
                series = _get_attr(issue_data, 'series', {}) or {}
                year_began = _get_attr(series, 'year_began', None)
                if publisher_name or year_began:
                    write_cvinfo_fields(cvinfo_path, publisher_name, year_began)
                cvinfo_fields_saved = True

            # Map to ComicInfo format
            metadata = map_to_comicinfo(issue_data)

            # Generate and add ComicInfo.xml
            xml_content = generate_comicinfo_xml(metadata)
            if add_comicinfo_to_archive(file_path, xml_content):
                processed += 1
                app_logger.info(f"Added Metron metadata to {file_path}")

                # Auto-rename if enabled
                try:
                    custom_enabled, custom_pattern = load_custom_rename_config()
                    if custom_enabled and custom_pattern:
                        series = metadata.get('Series', '')
                        series = series.replace(':', ' -')
                        series = re.sub(r'[<>"/\\|?*]', '', series)
                        series = smart_title_case(series)
                        issue_num_padded = str(metadata.get('Number', '')).zfill(3)
                        year = str(metadata.get('Year', ''))

                        issue_title = metadata.get('Title', '')
                        if issue_title:
                            issue_title = re.sub(r'[<>:"/\\|?*]', '', issue_title)
                            issue_title = re.sub(r'[\x00-\x1f]', '', issue_title)
                            issue_title = issue_title.strip('. ')

                        new_name = custom_pattern
                        new_name = re.sub(r'\{series_name\}', series, new_name, flags=re.IGNORECASE)
                        new_name = re.sub(r'\{issue_number\}', issue_num_padded, new_name, flags=re.IGNORECASE)
                        new_name = re.sub(r'\{year\}|\{YYYY\}', year, new_name, flags=re.IGNORECASE)
                        new_name = re.sub(r'\{volume_number\}', '', new_name, flags=re.IGNORECASE)
                        new_name = re.sub(r'\{issue_title\}', issue_title, new_name, flags=re.IGNORECASE)
                        new_name = re.sub(r'\s+', ' ', new_name).strip()
                        new_name = re.sub(r'\s*\(\s*\)', '', new_name).strip()
                        new_name = re.sub(r'\s*-\s*(?=\(|$)', ' ', new_name).strip()
                        new_name = re.sub(r'\s+', ' ', new_name).strip()

                        _, ext = os.path.splitext(file_path)
                        new_name = new_name + ext

                        directory = os.path.dirname(file_path)
                        old_name = os.path.basename(file_path)
                        new_path = os.path.join(directory, new_name)

                        if new_name != old_name and not os.path.exists(new_path):
                            os.rename(file_path, new_path)
                            app_logger.info(f"Renamed: {old_name} -> {new_name}")
                            if file_path == target_file:
                                renamed_path = new_path
                except Exception as rename_error:
                    app_logger.error(f"Error during auto-rename: {rename_error}")

        if processed > 0:
            app_logger.info(f"Auto-fetched Metron metadata: {processed} files processed")

            # Queue file for metadata scanning to update file_index
            final_path = renamed_path if renamed_path else destination_path
            if final_path.lower().endswith('.cbz'):
                from metadata_scanner import queue_file_for_scan, PRIORITY_NEW_FILE
                queue_file_for_scan(final_path, PRIORITY_NEW_FILE)
                app_logger.debug(f"Queued for metadata scan: {os.path.basename(final_path)}")

        return renamed_path if renamed_path else destination_path

    except Exception as e:
        if metron.is_connection_error(e):
            app_logger.warning(f"Metron unavailable during auto-fetch metadata: {e}")
        else:
            app_logger.error(f"Error in auto-fetch Metron metadata: {e}")
        return destination_path


def resize_upload(file_path, target_dir):
    """
    Resize an uploaded image to match dimensions of existing images in the directory.

    Args:
        file_path: Path to the uploaded file
        target_dir: Directory containing existing images

    Returns:
        True if resized, False if no resize needed or no reference image found
    """
    try:
        # Find first existing image in directory (excluding the just-uploaded file)
        image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
        reference_image = None

        for filename in sorted(os.listdir(target_dir)):
            if filename == os.path.basename(file_path):
                continue  # Skip the uploaded file itself

            ext = os.path.splitext(filename)[1].lower()
            if ext in image_extensions:
                ref_path = os.path.join(target_dir, filename)
                if os.path.isfile(ref_path):
                    reference_image = ref_path
                    break

        if not reference_image:
            app_logger.info(f"No reference image found in {target_dir}, skipping resize")
            return False

        # Get reference dimensions
        with Image.open(reference_image) as ref_img:
            target_width, target_height = ref_img.size

        # Open and resize the uploaded image
        with Image.open(file_path) as img:
            current_width, current_height = img.size

            # Skip if already same size
            if current_width == target_width and current_height == target_height:
                app_logger.info(f"Image {file_path} already matches dimensions ({target_width}x{target_height})")
                return False

            # Convert RGBA/P modes to RGB for JPEG compatibility
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGB')

            # Resize to match reference dimensions
            resized = img.resize((target_width, target_height), Image.Resampling.LANCZOS)

            # Save back to same path (preserve format based on extension)
            ext = os.path.splitext(file_path)[1].lower()
            if ext in ('.jpg', '.jpeg'):
                resized.save(file_path, 'JPEG', quality=95)
            elif ext == '.png':
                resized.save(file_path, 'PNG')
            elif ext == '.webp':
                resized.save(file_path, 'WEBP', quality=95)
            else:
                resized.save(file_path)

            app_logger.info(f"Resized {file_path} from {current_width}x{current_height} to {target_width}x{target_height}")
            return True

    except Exception as e:
        app_logger.error(f"Error resizing upload {file_path}: {e}")
        return False



def find_folder_thumbnail(folder_path):
    """Find a folder thumbnail image in the given directory.

    Args:
        folder_path: Path to the directory to search

    Returns:
        Path to the thumbnail image if found, None otherwise
    """
    allowed_extensions = {'.png', '.gif', '.jpg', '.jpeg'}
    allowed_names = {'folder'}  # Only use folder.* thumbnails, ignore cover.*

    try:
        entries = os.listdir(folder_path)
        for entry in entries:
            name_without_ext, ext = os.path.splitext(entry.lower())
            if name_without_ext in allowed_names and ext in allowed_extensions:
                return os.path.join(folder_path, entry)
    except (OSError, IOError):
        pass

    return None


def find_folder_thumbnails_batch(folder_paths):
    """
    Find folder/cover thumbnails for multiple directories.
    Uses ThreadPoolExecutor to parallelize filesystem checks.

    Note: Cannot use file_index because image files (.png, .jpg, etc.)
    are excluded from the index during build.

    Args:
        folder_paths: List of directory paths to check

    Returns:
        Dict mapping path -> thumbnail_path or None
    """
    if not folder_paths:
        return {}

    results = {}

    # Use thread pool to parallelize filesystem checks
    with ThreadPoolExecutor(max_workers=min(10, len(folder_paths))) as executor:
        future_to_path = {
            executor.submit(find_folder_thumbnail, path): path
            for path in folder_paths
        }
        for future in as_completed(future_to_path):
            path = future_to_path[future]
            try:
                thumb = future.result()
                results[path] = thumb
                if thumb:
                    app_logger.debug(f"Found thumbnail for {path}: {thumb}")
            except Exception as e:
                app_logger.error(f"Error finding thumbnail for {path}: {e}")
                results[path] = None

    # Fill in any missing paths
    for p in folder_paths:
        if p not in results:
            results[p] = None

    return results


@app.route('/api/read/<path:comic_path>/page/<int:page_num>')
def read_comic_page(comic_path, page_num):
    """Serve a specific page from a comic file."""

    # Add leading slash if missing (for absolute paths on Unix systems)
    if not comic_path.startswith('/'):
        comic_path = '/' + comic_path

    if not os.path.exists(comic_path):
        app_logger.error(f"Comic file not found: {comic_path}")
        return send_file('static/images/error.svg', mimetype='image/svg+xml')

    try:
        # Determine archive type
        ext = os.path.splitext(comic_path)[1].lower()

        # Get list of image files from archive
        image_files = []
        archive = None

        if ext in ['.cbz', '.zip']:
            archive = zipfile.ZipFile(comic_path, 'r')
            all_files = archive.namelist()
        elif ext == '.cbr':
            archive = rarfile.RarFile(comic_path, 'r')
            all_files = archive.namelist()
        else:
            return jsonify({"error": "Unsupported file format"}), 400

        # Filter for image files
        image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')
        for filename in all_files:
            if filename.lower().endswith(image_extensions):
                # Skip macOS metadata files
                if not filename.startswith('__MACOSX') and not os.path.basename(filename).startswith('.'):
                    image_files.append(filename)

        # Sort naturally
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]
        image_files.sort(key=natural_sort_key)

        # Check if page number is valid
        if page_num < 0 or page_num >= len(image_files):
            return jsonify({"error": "Invalid page number"}), 400

        # Read the requested page
        target_file = image_files[page_num]
        image_data = archive.read(target_file)

        # Close archive
        archive.close()

        # Determine mime type
        file_ext = os.path.splitext(target_file)[1].lower()
        mime_types = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.bmp': 'image/bmp'
        }
        mime_type = mime_types.get(file_ext, 'image/jpeg')

        # Return image
        return Response(image_data, mimetype=mime_type)

    except Exception as e:
        app_logger.error(f"Error reading comic page {page_num} from {comic_path}: {e}")
        app_logger.error(traceback.format_exc())
        if archive:
            archive.close()
        return send_file('static/images/error.svg', mimetype='image/svg+xml')


@app.route('/api/read/<path:comic_path>/page/<int:page_num>/info')
def read_comic_page_info(comic_path, page_num):
    """Get information about a specific page in a comic file."""

    # Add leading slash if missing (for absolute paths on Unix systems)
    if not comic_path.startswith('/'):
        comic_path = '/' + comic_path

    if not os.path.exists(comic_path):
        return jsonify({"success": False, "error": "File not found"}), 404

    try:
        ext = os.path.splitext(comic_path)[1].lower()
        archive = None

        if ext in ['.cbz', '.zip']:
            archive = zipfile.ZipFile(comic_path, 'r')
        elif ext == '.cbr':
            archive = rarfile.RarFile(comic_path, 'r')
        else:
            return jsonify({"success": False, "error": "Unsupported format"}), 400

        # Get list of image files
        image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')
        image_files = []
        for filename in archive.namelist():
            if filename.lower().endswith(image_extensions):
                if not filename.startswith('__MACOSX') and not os.path.basename(filename).startswith('.'):
                    image_files.append(filename)

        # Sort naturally
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]
        image_files.sort(key=natural_sort_key)

        if page_num < 0 or page_num >= len(image_files):
            archive.close()
            return jsonify({"success": False, "error": "Invalid page number"}), 400

        target_file = image_files[page_num]

        # Get file info from archive
        info = archive.getinfo(target_file)
        file_size = info.file_size if hasattr(info, 'file_size') else info.compress_size
        file_name = os.path.basename(target_file)

        archive.close()

        return jsonify({
            "success": True,
            "page_num": page_num,
            "file_name": file_name,
            "file_size": file_size,
            "archive_path": target_file
        })

    except Exception as e:
        app_logger.error(f"Error getting page info for {comic_path} page {page_num}: {e}")
        if archive:
            archive.close()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/read/<path:comic_path>/info')
def read_comic_info(comic_path):
    """Get information about a comic file (page count, etc.)."""

    # Add leading slash if missing (for absolute paths on Unix systems)
    if not comic_path.startswith('/'):
        comic_path = '/' + comic_path

    if not os.path.exists(comic_path):
        return jsonify({"error": "Comic file not found"}), 404

    try:
        # Determine archive type
        ext = os.path.splitext(comic_path)[1].lower()

        # Get list of image files from archive
        image_files = []

        if ext in ['.cbz', '.zip']:
            with zipfile.ZipFile(comic_path, 'r') as archive:
                all_files = archive.namelist()
        elif ext == '.cbr':
            with rarfile.RarFile(comic_path, 'r') as archive:
                all_files = archive.namelist()
        else:
            return jsonify({"error": "Unsupported file format"}), 400

        # Filter for image files
        image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')
        for filename in all_files:
            if filename.lower().endswith(image_extensions):
                # Skip macOS metadata files
                if not filename.startswith('__MACOSX') and not os.path.basename(filename).startswith('.'):
                    image_files.append(filename)

        return jsonify({
            "success": True,
            "page_count": len(image_files),
            "filename": os.path.basename(comic_path)
        })

    except Exception as e:
        app_logger.error(f"Error getting comic info for {comic_path}: {e}")
        app_logger.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route('/api/mark-comic-read', methods=['POST'])
def api_mark_comic_read():
    """Mark a comic as read in the database."""
    data = request.get_json()
    comic_path = data.get('path')
    read_at = data.get('read_at')  # Optional ISO timestamp for backfilling
    page_count = data.get('page_count', 0)
    time_spent = data.get('time_spent', 0)

    if not comic_path:
        return jsonify({"error": "Missing path parameter"}), 400

    # Extract metadata from ComicInfo.xml if available
    comic_info = None
    writer = ''
    penciller = ''
    characters = ''
    publisher = ''
    try:
        from comicinfo import read_comicinfo_from_zip
        if os.path.exists(comic_path) and comic_path.lower().endswith(('.cbz', '.zip')):
            comic_info = read_comicinfo_from_zip(comic_path)
            if comic_info:
                writer = comic_info.get('Writer', '')
                penciller = comic_info.get('Penciller', '')
                characters = comic_info.get('Characters', '')
                publisher = comic_info.get('Publisher', '')
    except Exception as e:
        app_logger.warning(f"Could not extract ComicInfo.xml metadata: {e}")

    try:
        mark_issue_read(comic_path, read_at, page_count, time_spent,
                        writer=writer, penciller=penciller, characters=characters, publisher=publisher)
        clear_stats_cache_keys(['library_stats', 'reading_history', 'reading_heatmap'])
        app_logger.info(f"Marked comic as read: {comic_path}" + (f" at {read_at}" if read_at else ""))
    except Exception as e:
        app_logger.error(f"Error marking comic as read: {e}")
        return jsonify({"error": str(e)}), 500

    # Scrobble to Metron if configured (non-blocking — local read already saved)
    try:
        metron_username = app.config.get("METRON_USERNAME", "").strip()
        metron_password = app.config.get("METRON_PASSWORD", "").strip()
        if metron_username and metron_password:
            from models import metron as metron_module
            api = metron_module.get_api(metron_username, metron_password)
            if api:
                metron_issue_id = metron_module.resolve_metron_issue_id(
                    api, comic_path, comic_info.get('Number') if comic_info else None
                )
                if metron_issue_id:
                    metron_module.scrobble_issue(api, metron_issue_id, read_at)
                    app_logger.info(f"Scrobbled to Metron: issue {metron_issue_id}")
    except Exception as e:
        app_logger.warning(f"Metron scrobble failed (non-blocking): {e}")

    return jsonify({"success": True})


@app.route('/api/reading-trends/<field>')
def api_reading_trends(field):
    """Get top values for a metadata field (writer, penciller, characters, publisher)."""
    from database import get_reading_trends

    valid_fields = ['writer', 'penciller', 'characters', 'publisher']
    if field not in valid_fields:
        return jsonify({"error": f"Invalid field. Must be one of: {', '.join(valid_fields)}"}), 400

    year = request.args.get('year', type=int)
    limit = request.args.get('limit', 10, type=int)

    trends = get_reading_trends(field, year=year, limit=limit)
    return jsonify(trends)


@app.route('/api/backfill-reading-metadata', methods=['POST'])
def api_backfill_reading_metadata():
    """Re-read ComicInfo.xml for all issues_read entries and update metadata fields."""
    from comicinfo import read_comicinfo_from_zip

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Database connection failed"}), 500

        c = conn.cursor()
        c.execute('SELECT id, issue_path FROM issues_read')
        rows = c.fetchall()

        updated_count = 0
        skipped_count = 0
        skipped_issues = []

        for row in rows:
            issue_id = row[0]
            issue_path = row[1]
            filename = os.path.basename(issue_path)

            # Skip if file doesn't exist
            if not os.path.exists(issue_path):
                skipped_count += 1
                skipped_issues.append({"file": filename, "reason": "File not found"})
                continue

            # Skip if not a CBZ/ZIP
            if not issue_path.lower().endswith(('.cbz', '.zip')):
                skipped_count += 1
                skipped_issues.append({"file": filename, "reason": "Not a CBZ/ZIP file"})
                continue

            try:
                comic_info = read_comicinfo_from_zip(issue_path)
                if comic_info:
                    writer = comic_info.get('Writer', '')
                    penciller = comic_info.get('Penciller', '')
                    characters = comic_info.get('Characters', '')
                    publisher = comic_info.get('Publisher', '')

                    c.execute('''
                        UPDATE issues_read
                        SET writer = ?, penciller = ?, characters = ?, publisher = ?
                        WHERE id = ?
                    ''', (writer, penciller, characters, publisher, issue_id))
                    updated_count += 1
                else:
                    skipped_count += 1
                    skipped_issues.append({"file": filename, "reason": "No ComicInfo.xml"})
            except Exception as e:
                app_logger.warning(f"Could not read ComicInfo.xml for {issue_path}: {e}")
                skipped_count += 1
                skipped_issues.append({"file": filename, "reason": f"Error: {str(e)}"})

        conn.commit()
        conn.close()

        # Clear stats cache
        clear_stats_cache_keys(['library_stats', 'reading_history'])

        app_logger.info(f"Backfill complete: {updated_count} updated, {skipped_count} skipped")
        return jsonify({
            "success": True,
            "updated": updated_count,
            "skipped": skipped_count,
            "skipped_issues": skipped_issues
        })

    except Exception as e:
        app_logger.error(f"Error during backfill: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/recently-read')
def api_recently_read():
    """Get recently read issues with metadata for display."""
    limit = request.args.get('limit', 20, type=int)
    issues = get_issues_read()[:limit]

    result = []
    for issue in issues:
        path = issue['issue_path']
        if os.path.exists(path):
            result.append({
                'name': os.path.basename(path),
                'path': path,
                'read_at': issue['read_at'],
                'thumbnail_url': url_for('get_thumbnail', path=path)
            })
    return jsonify(result)


@app.route('/api/reading-position', methods=['GET', 'POST', 'DELETE'])
def api_reading_position():
    """
    Manage reading position bookmarks.
    GET: Get saved position for a comic
    POST: Save/update position for a comic
    DELETE: Remove saved position
    """
    from database import save_reading_position, get_reading_position, delete_reading_position

    if request.method == 'GET':
        comic_path = request.args.get('path')
        if not comic_path:
            return jsonify({"error": "Missing path parameter"}), 400

        position = get_reading_position(comic_path)
        if position:
            return jsonify({
                "page_number": position['page_number'],
                "total_pages": position['total_pages'],
                "updated_at": position['updated_at'],
                "time_spent": position.get('time_spent', 0)
            })
        return jsonify({"page_number": None})

    elif request.method == 'POST':
        data = request.get_json()
        comic_path = data.get('comic_path')
        page_number = data.get('page_number')
        total_pages = data.get('total_pages')
        time_spent = data.get('time_spent', 0)

        if not comic_path or page_number is None:
            return jsonify({"error": "Missing comic_path or page_number"}), 400

        success = save_reading_position(comic_path, page_number, total_pages, time_spent)
        return jsonify({"success": success})

    elif request.method == 'DELETE':
        comic_path = request.args.get('path')
        if not comic_path:
            return jsonify({"error": "Missing path parameter"}), 400

        success = delete_reading_position(comic_path)
        return jsonify({"success": success})


def generate_thumbnail_task(file_path, cache_path):
    """Background task to generate thumbnail."""
    app_logger.info(f"Starting thumbnail generation for {file_path}")
    
    # Skip CBR and RAR files - they are not supported by this background task
    if file_path.lower().endswith(('.cbr', '.rar')):
        app_logger.info(f"Skipping thumbnail generation for CBR/RAR file: {file_path}")
        conn = get_db_connection()
        if conn:
            conn.execute('UPDATE thumbnail_jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE path = ?', ('skipped', file_path))
            conn.commit()
            conn.close()
        return
    
    try:
        # Extract and resize
        import zipfile
        from PIL import Image
        
        # Ensure cache directory exists
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        
        with zipfile.ZipFile(file_path, 'r') as zf:
            file_list = zf.namelist()
            image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
            image_files = sorted([f for f in file_list if os.path.splitext(f.lower())[1] in image_extensions], key=str.lower)
            
            if image_files:
                with zf.open(image_files[0]) as image_file:
                    img = Image.open(image_file)
                    if img.mode in ('RGBA', 'LA', 'P'):
                        img = img.convert('RGB')
                    
                    # Resize to 300px height
                    aspect_ratio = img.width / img.height
                    new_height = 300
                    new_width = int(new_height * aspect_ratio)
                    img.thumbnail((new_width, new_height), Image.Resampling.LANCZOS)
                    
                    img.save(cache_path, format='JPEG', quality=85)
                    
                    # Update DB success
                    conn = get_db_connection()
                    if conn:
                        conn.execute('UPDATE thumbnail_jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE path = ?', ('completed', file_path))
                        conn.commit()
                        conn.close()
                        app_logger.info(f"Thumbnail generated successfully for {file_path}")
            else:
                raise Exception("No images found in archive")
                
    except Exception as e:
        app_logger.error(f"Error generating thumbnail for {file_path}: {e}")
        conn = get_db_connection()
        if conn:
            conn.execute('UPDATE thumbnail_jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE path = ?', ('error', file_path))
            conn.commit()
            conn.close()


def generate_thumbnail_sync(file_path: str, cache_path: str) -> bool:
    """
    Generate a thumbnail synchronously for immediate use.
    Used by folder thumbnail generation when individual thumbnails don't exist yet.

    Args:
        file_path: Path to the comic file (CBZ or CBR)
        cache_path: Where to save the thumbnail

    Returns:
        True if successful, False otherwise
    """
    try:
        import zipfile
        from PIL import Image

        # Ensure cache directory exists
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)

        image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}

        # Handle CBZ files
        if file_path.lower().endswith(('.cbz', '.zip')):
            with zipfile.ZipFile(file_path, 'r') as zf:
                file_list = zf.namelist()
                image_files = sorted([
                    f for f in file_list
                    if os.path.splitext(f.lower())[1] in image_extensions
                    and not f.startswith('__MACOSX')
                    and not os.path.basename(f).startswith('.')
                ], key=str.lower)

                if not image_files:
                    app_logger.warning(f"No images found in {file_path}")
                    return False

                with zf.open(image_files[0]) as image_file:
                    img = Image.open(image_file)
                    if img.mode in ('RGBA', 'LA', 'P'):
                        img = img.convert('RGB')

                    # Resize to 300px height
                    aspect_ratio = img.width / img.height
                    new_height = 300
                    new_width = int(new_height * aspect_ratio)
                    img.thumbnail((new_width, new_height), Image.Resampling.LANCZOS)

                    img.save(cache_path, format='JPEG', quality=85)
                    app_logger.info(f"Generated thumbnail sync for {file_path}")
                    return True

        # Handle CBR files
        elif file_path.lower().endswith('.cbr'):
            import rarfile
            with rarfile.RarFile(file_path, 'r') as rf:
                file_list = rf.namelist()
                image_files = sorted([
                    f for f in file_list
                    if os.path.splitext(f.lower())[1] in image_extensions
                    and not f.startswith('__MACOSX')
                    and not os.path.basename(f).startswith('.')
                ], key=str.lower)

                if not image_files:
                    app_logger.warning(f"No images found in {file_path}")
                    return False

                with rf.open(image_files[0]) as image_file:
                    img = Image.open(image_file)
                    if img.mode in ('RGBA', 'LA', 'P'):
                        img = img.convert('RGB')

                    # Resize to 300px height
                    aspect_ratio = img.width / img.height
                    new_height = 300
                    new_width = int(new_height * aspect_ratio)
                    img.thumbnail((new_width, new_height), Image.Resampling.LANCZOS)

                    img.save(cache_path, format='JPEG', quality=85)
                    app_logger.info(f"Generated thumbnail sync for {file_path}")
                    return True

        else:
            app_logger.warning(f"Unsupported file type: {file_path}")
            return False

    except Exception as e:
        app_logger.error(f"generate_thumbnail_sync failed for {file_path}: {e}")
        return False


@app.route('/api/thumbnail')
def get_thumbnail():
    """Serve or generate thumbnail for a file."""
    file_path = request.args.get('path')
    if not file_path:
        return jsonify({"error": "Missing path"}), 400
        
    # Calculate cache path
    cache_dir = config.get("SETTINGS", "CACHE_DIR", fallback="/cache")
    thumbnails_dir = os.path.join(cache_dir, "thumbnails")
    
    # Create a hash of the file path to use as filename
    import hashlib
    path_hash = hashlib.md5(file_path.encode('utf-8'), usedforsecurity=False).hexdigest()
    
    # Sharding: use first 2 chars of hash as subdirectory to avoid too many files in one folder
    shard_dir = path_hash[:2]
    filename = f"{path_hash}.jpg"
    
    # Full path for checking existence / generation
    cache_path = os.path.join(thumbnails_dir, shard_dir, filename)
    
    # Check if thumbnail exists
    if os.path.exists(cache_path):
        return send_from_directory(os.path.join(thumbnails_dir, shard_dir), filename)
        
    # Check DB status
    conn = get_db_connection()
    job = None
    if conn:
        job = conn.execute('SELECT * FROM thumbnail_jobs WHERE path = ?', (file_path,)).fetchone()
        conn.close()
        
    if job and job['status'] == 'completed' and os.path.exists(cache_path):
        return send_from_directory(os.path.join(thumbnails_dir, shard_dir), filename)
        
    if job and job['status'] == 'processing':
        return redirect(url_for('static', filename='images/loading.svg'))
        
    if job and job['status'] == 'error':
        return redirect(url_for('static', filename='images/error.svg'))
        
    # Insert 'processing' status synchronously to prevent race conditions
    conn = get_db_connection()
    if conn:
        conn.execute('INSERT OR REPLACE INTO thumbnail_jobs (path, status) VALUES (?, ?)', (file_path, 'processing'))
        conn.commit()
        conn.close()

    # Submit task
    thumbnail_executor.submit(generate_thumbnail_task, file_path, cache_path)

    return redirect(url_for('static', filename='images/loading.svg'))


def create_nested_folder_thumbnail(comic_stack_img, folder_icon_path, canvas_size=(200, 300)):
    """Composite the comic stack behind a folder icon for nested folder thumbnails."""
    folder_icon = Image.open(folder_icon_path).convert("RGBA")

    stack = comic_stack_img.convert("RGBA")

    # Scale stack to 175px wide with proportionate height
    new_w = 190
    aspect_ratio = stack.height / stack.width
    new_h = int(new_w * aspect_ratio)

    stack_resized = stack.resize((new_w, new_h), Image.Resampling.LANCZOS)

    # Position stack: centered horizontally, 20px from bottom
    x_pos = (canvas_size[0] - new_w) // 2
    y_pos = canvas_size[1] - new_h - 20  # 20px from bottom

    # Create final canvas
    final_thumb = Image.new("RGBA", canvas_size, (0, 0, 0, 0))

    # Paste stack FIRST (behind)
    final_thumb.paste(stack_resized, (x_pos, y_pos), mask=stack_resized)

    # Paste folder icon ON TOP (in front)
    final_thumb.paste(folder_icon, (0, 0), mask=folder_icon)

    # Resize final image to 167px width with proportionate height
    final_w = 167
    aspect = final_thumb.height / final_thumb.width
    final_h = int(final_w * aspect)
    final_thumb = final_thumb.resize((final_w, final_h), Image.Resampling.LANCZOS)

    return final_thumb


@app.route('/api/generate-folder-thumbnail', methods=['POST'])
def generate_folder_thumbnail():
    """Generate a fanned stack thumbnail for a folder using cached thumbnails."""
    data = request.get_json()
    folder_path = data.get('folder_path')

    if not folder_path:
        return jsonify({"error": "Missing folder_path"}), 400

    if not os.path.exists(folder_path) or not os.path.isdir(folder_path):
        return jsonify({"error": "Invalid folder path"}), 400

    try:
        # Get cache directory
        cache_dir = config.get("SETTINGS", "CACHE_DIR", fallback="/cache")
        thumbnails_dir = os.path.join(cache_dir, "thumbnails")

        # Define excluded extensions
        excluded_extensions = {".png", ".jpg", ".jpeg", ".gif" ".html", ".css", ".ds_store", "cvinfo", ".json", ".db", ".xml"}

        # Find comic files in the folder
        comic_files = []
        is_nested = False  # Track if we're using nested folder comics

        # First, check for direct comic files
        for item in sorted(os.listdir(folder_path)):
            item_path = os.path.join(folder_path, item)
            if os.path.isfile(item_path):
                _, ext = os.path.splitext(item.lower())
                if ext not in excluded_extensions and not item.startswith(('.', '-', '_')):
                    if ext in ['.cbz', '.cbr', '.zip']:
                        comic_files.append(item_path)

        # If no direct comics, scan subfolders
        if not comic_files:
            is_nested = True
            subfolder_comics = {}  # {subfolder_path: [comic_files]}

            for item in sorted(os.listdir(folder_path)):
                item_path = os.path.join(folder_path, item)
                if os.path.isdir(item_path) and not item.startswith(('.', '_')):
                    folder_comics = []
                    for subitem in sorted(os.listdir(item_path)):
                        subitem_path = os.path.join(item_path, subitem)
                        if os.path.isfile(subitem_path):
                            _, ext = os.path.splitext(subitem.lower())
                            if ext in ['.cbz', '.cbr', '.zip']:
                                folder_comics.append(subitem_path)
                    if folder_comics:
                        subfolder_comics[item_path] = folder_comics

            if not subfolder_comics:
                return jsonify({"error": "No comic files found in folder or subfolders"}), 400

            # Distribute 4 slots across subfolders
            MAX_COVERS = 4
            subfolders = list(subfolder_comics.keys())
            num_folders = len(subfolders)

            if num_folders >= MAX_COVERS:
                # 4+ folders: 1 from each of first 4
                for i in range(MAX_COVERS):
                    comic_files.append(subfolder_comics[subfolders[i]][0])
            else:
                # Fewer than 4 folders: distribute evenly with extras going to earlier folders
                per_folder = MAX_COVERS // num_folders
                remainder = MAX_COVERS % num_folders

                for i, folder in enumerate(subfolders):
                    count = per_folder + (1 if i < remainder else 0)
                    comic_files.extend(subfolder_comics[folder][:count])

        # Get cached thumbnail paths for the first 4 comics
        MAX_COVERS = 4
        selected_files = comic_files[:MAX_COVERS]
        cached_thumbs = []

        for file_path in selected_files:
            # Calculate cache path using same method as get_thumbnail
            path_hash = hashlib.md5(file_path.encode('utf-8'), usedforsecurity=False).hexdigest()
            shard_dir = path_hash[:2]
            filename = f"{path_hash}.jpg"
            cache_path = os.path.join(thumbnails_dir, shard_dir, filename)

            if os.path.exists(cache_path):
                cached_thumbs.append(cache_path)
            else:
                # Generate thumbnail synchronously if missing
                try:
                    if generate_thumbnail_sync(file_path, cache_path):
                        cached_thumbs.append(cache_path)
                except Exception as e:
                    app_logger.warning(f"Failed to generate thumbnail for {file_path}: {e}")

        if not cached_thumbs:
            return jsonify({"error": "Could not generate any thumbnails for comics in this folder"}), 400

        # Create fanned stack thumbnail
        CANVAS_SIZE = (200, 300)
        THUMB_SIZE = (150, 245)
        ROTATION_LIMIT = 10
        Y_OFFSET = 0

        final_canvas = Image.new('RGBA', CANVAS_SIZE, (0, 0, 0, 0))

        # Reverse cached_thumbs so we paste from back to front (001 pasted last/on top)
        reversed_thumbs = list(reversed(cached_thumbs))

        # --- 3. Define Angles ---
        angles = []
        for i in range(len(reversed_thumbs)):
            if i == len(reversed_thumbs) - 1:
                angles.append(0)
            else:
                # Randomize rotation for background images
                angles.append(random.randint(-ROTATION_LIMIT, ROTATION_LIMIT))

        for i, thumb_path in enumerate(reversed_thumbs):
            try:
                # Open and resize cached thumbnail
                img = Image.open(thumb_path).convert("RGBA")

                # Fit to thumb size
                img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)

                # Create centered image
                fitted_img = Image.new('RGBA', THUMB_SIZE, (0, 0, 0, 0))
                paste_x = (THUMB_SIZE[0] - img.width) // 2
                paste_y = (THUMB_SIZE[1] - img.height) // 2
                fitted_img.paste(img, (paste_x, paste_y), img if img.mode == 'RGBA' else None)

                # Create layer for rotation and shadow
                layer_size = (int(THUMB_SIZE[0] * 1.5), int(THUMB_SIZE[1] * 1.5))
                layer = Image.new('RGBA', layer_size, (0, 0, 0, 0))

                # Calculate center position
                layer_paste_x = (layer_size[0] - THUMB_SIZE[0]) // 2
                layer_paste_y = (layer_size[1] - THUMB_SIZE[1]) // 2

                # Add drop shadow
                shadow = Image.new('RGBA', layer_size, (0, 0, 0, 0))
                shadow_box = (layer_paste_x + 4, layer_paste_y + 4,
                             layer_paste_x + THUMB_SIZE[0] + 4, layer_paste_y + THUMB_SIZE[1] + 4)

                d = ImageDraw.Draw(shadow)
                d.rectangle(shadow_box, fill=(0, 0, 0, 120))
                shadow = shadow.filter(ImageFilter.GaussianBlur(radius=5))

                # Composite shadow + image
                layer = Image.alpha_composite(layer, shadow)
                layer.paste(fitted_img, (layer_paste_x, layer_paste_y), fitted_img)

                # Rotate the layer
                angle = angles[i]
                rotated_layer = layer.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)

                # Position on canvas with Y_OFFSET to move stack down
                final_x = (CANVAS_SIZE[0] - rotated_layer.width) // 2
                final_y = ((CANVAS_SIZE[1] - rotated_layer.height) // 2) + Y_OFFSET

                final_canvas.paste(rotated_layer, (final_x, final_y), rotated_layer)

            except Exception as e:
                app_logger.error(f"Error processing thumbnail {thumb_path}: {e}")

        # Remove any existing folder thumbnail files to allow regeneration
        for ext in ['folder.png', 'folder.jpg', 'folder.jpeg', 'folder.gif']:
            existing_thumb = os.path.join(folder_path, ext)
            if os.path.exists(existing_thumb):
                try:
                    os.remove(existing_thumb)
                    app_logger.info(f"Removed existing thumbnail: {existing_thumb}")
                except Exception as e:
                    app_logger.error(f"Error removing existing thumbnail {existing_thumb}: {e}")

        # If using nested folder comics, overlay on folder icon
        if is_nested:
            folder_icon_path = os.path.join(app.static_folder, 'images', 'folder-fill-200x300.png')
            if os.path.exists(folder_icon_path):
                final_canvas = create_nested_folder_thumbnail(final_canvas, folder_icon_path)

        # Save to folder
        output_path = os.path.join(folder_path, "folder.png")
        final_canvas.save(output_path, "PNG")

        app_logger.info(f"Generated folder thumbnail: {output_path}")

        # Invalidate cache to show new thumbnail
        invalidate_cache_for_path(folder_path)

        return jsonify({"success": True, "thumbnail_path": output_path})

    except Exception as e:
        app_logger.error(f"Error generating folder thumbnail: {e}")
        return jsonify({"error": str(e)}), 500


def generate_folder_thumbnail_internal(folder_path):
    """Internal function to generate folder thumbnail. Returns True on success, False on failure."""
    try:
        # Get cache directory
        cache_dir = config.get("SETTINGS", "CACHE_DIR", fallback="/cache")
        thumbnails_dir = os.path.join(cache_dir, "thumbnails")

        # Define excluded extensions
        excluded_extensions = {".png", ".jpg", ".jpeg", ".gif", ".html", ".css", ".ds_store", "cvinfo", ".json", ".db", ".xml"}

        # Find comic files in the folder
        comic_files = []
        is_nested = False

        # Check for direct comic files
        for item in sorted(os.listdir(folder_path)):
            item_path = os.path.join(folder_path, item)
            if os.path.isfile(item_path):
                _, ext = os.path.splitext(item.lower())
                if ext not in excluded_extensions and not item.startswith(('.', '-', '_')):
                    if ext in ['.cbz', '.cbr', '.zip']:
                        comic_files.append(item_path)

        # If no direct comics, scan subfolders
        if not comic_files:
            is_nested = True
            subfolder_comics = {}

            for item in sorted(os.listdir(folder_path)):
                item_path = os.path.join(folder_path, item)
                if os.path.isdir(item_path) and not item.startswith(('.', '_')):
                    folder_comics = []
                    for subitem in sorted(os.listdir(item_path)):
                        subitem_path = os.path.join(item_path, subitem)
                        if os.path.isfile(subitem_path):
                            _, ext = os.path.splitext(subitem.lower())
                            if ext in ['.cbz', '.cbr', '.zip']:
                                folder_comics.append(subitem_path)
                    if folder_comics:
                        subfolder_comics[item_path] = folder_comics

            if not subfolder_comics:
                return False

            # Distribute 4 slots across subfolders
            MAX_COVERS = 4
            subfolders = list(subfolder_comics.keys())
            num_folders = len(subfolders)

            if num_folders >= MAX_COVERS:
                for i in range(MAX_COVERS):
                    comic_files.append(subfolder_comics[subfolders[i]][0])
            else:
                per_folder = MAX_COVERS // num_folders
                remainder = MAX_COVERS % num_folders
                for i, folder in enumerate(subfolders):
                    count = per_folder + (1 if i < remainder else 0)
                    comic_files.extend(subfolder_comics[folder][:count])

        # Get cached thumbnail paths for the first 4 comics
        MAX_COVERS = 4
        selected_files = comic_files[:MAX_COVERS]
        cached_thumbs = []

        for file_path in selected_files:
            path_hash = hashlib.md5(file_path.encode('utf-8'), usedforsecurity=False).hexdigest()
            shard_dir = path_hash[:2]
            filename = f"{path_hash}.jpg"
            cache_path = os.path.join(thumbnails_dir, shard_dir, filename)

            if os.path.exists(cache_path):
                cached_thumbs.append(cache_path)
            else:
                # Generate thumbnail synchronously if missing
                try:
                    if generate_thumbnail_sync(file_path, cache_path):
                        cached_thumbs.append(cache_path)
                except Exception as e:
                    app_logger.warning(f"Failed to generate thumbnail for {file_path}: {e}")

        if not cached_thumbs:
            return False

        # Create fanned stack thumbnail
        CANVAS_SIZE = (200, 300)
        THUMB_SIZE = (150, 245)
        ROTATION_LIMIT = 10
        Y_OFFSET = 0

        final_canvas = Image.new('RGBA', CANVAS_SIZE, (0, 0, 0, 0))
        reversed_thumbs = list(reversed(cached_thumbs))

        angles = []
        for i in range(len(reversed_thumbs)):
            if i == len(reversed_thumbs) - 1:
                angles.append(0)
            else:
                angles.append(random.randint(-ROTATION_LIMIT, ROTATION_LIMIT))

        for i, thumb_path in enumerate(reversed_thumbs):
            try:
                img = Image.open(thumb_path).convert("RGBA")
                img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)

                fitted_img = Image.new('RGBA', THUMB_SIZE, (0, 0, 0, 0))
                paste_x = (THUMB_SIZE[0] - img.width) // 2
                paste_y = (THUMB_SIZE[1] - img.height) // 2
                fitted_img.paste(img, (paste_x, paste_y), img if img.mode == 'RGBA' else None)

                layer_size = (int(THUMB_SIZE[0] * 1.5), int(THUMB_SIZE[1] * 1.5))
                layer = Image.new('RGBA', layer_size, (0, 0, 0, 0))

                layer_paste_x = (layer_size[0] - THUMB_SIZE[0]) // 2
                layer_paste_y = (layer_size[1] - THUMB_SIZE[1]) // 2

                shadow = Image.new('RGBA', layer_size, (0, 0, 0, 0))
                shadow_box = (layer_paste_x + 4, layer_paste_y + 4,
                             layer_paste_x + THUMB_SIZE[0] + 4, layer_paste_y + THUMB_SIZE[1] + 4)

                d = ImageDraw.Draw(shadow)
                d.rectangle(shadow_box, fill=(0, 0, 0, 120))
                shadow = shadow.filter(ImageFilter.GaussianBlur(radius=5))

                layer = Image.alpha_composite(layer, shadow)
                layer.paste(fitted_img, (layer_paste_x, layer_paste_y), fitted_img)

                angle = angles[i]
                rotated_layer = layer.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)

                final_x = (CANVAS_SIZE[0] - rotated_layer.width) // 2
                final_y = ((CANVAS_SIZE[1] - rotated_layer.height) // 2) + Y_OFFSET

                final_canvas.paste(rotated_layer, (final_x, final_y), rotated_layer)

            except Exception as e:
                app_logger.error(f"Error processing thumbnail {thumb_path}: {e}")

        # Remove existing folder thumbnails
        for ext in ['folder.png', 'folder.jpg', 'folder.jpeg', 'folder.gif']:
            existing_thumb = os.path.join(folder_path, ext)
            if os.path.exists(existing_thumb):
                try:
                    os.remove(existing_thumb)
                except Exception as e:
                    app_logger.error(f"Error removing existing thumbnail {existing_thumb}: {e}")

        # If nested, overlay on folder icon
        if is_nested:
            folder_icon_path = os.path.join(app.static_folder, 'images', 'folder-fill-200x300.png')
            if os.path.exists(folder_icon_path):
                final_canvas = create_nested_folder_thumbnail(final_canvas, folder_icon_path)

        output_path = os.path.join(folder_path, "folder.png")
        final_canvas.save(output_path, "PNG")

        app_logger.info(f"Generated folder thumbnail: {output_path}")
        invalidate_cache_for_path(folder_path)

        return True

    except Exception as e:
        app_logger.error(f"Error generating folder thumbnail for {folder_path}: {e}")
        return False


@app.route('/api/generate-all-missing-thumbnails', methods=['POST'])
def generate_all_missing_thumbnails():
    """Generate folder thumbnails for all subfolders missing them (recursive)."""
    data = request.get_json()
    root_path = data.get('path')

    if not root_path or not os.path.isdir(root_path):
        return jsonify({"error": "Invalid path"}), 400

    generated = 0
    errors = 0
    skipped = 0

    # Recursively traverse all directories
    for dirpath, dirnames, filenames in os.walk(root_path):
        # Skip hidden directories
        dirnames[:] = [d for d in dirnames if not d.startswith(('.', '_'))]

        # Skip the root path itself
        if dirpath == root_path:
            continue

        # Check if folder already has thumbnail
        has_thumb = False
        for ext in ['.png', '.jpg', '.jpeg', '.gif']:
            if os.path.exists(os.path.join(dirpath, f'folder{ext}')):
                has_thumb = True
                break

        if has_thumb:
            skipped += 1
            continue

        # Generate thumbnail using internal function
        try:
            result = generate_folder_thumbnail_internal(dirpath)
            if result:
                generated += 1
            else:
                errors += 1
        except Exception as e:
            app_logger.error(f"Error generating thumbnail for {dirpath}: {e}")
            errors += 1

    message = f"Generated {generated} thumbnails"
    if skipped:
        message += f", skipped {skipped} existing"
    if errors:
        message += f" ({errors} errors)"

    return jsonify({
        "success": True,
        "generated": generated,
        "skipped": skipped,
        "errors": errors,
        "message": message
    })


@app.route('/api/download')
def download_file():
    """Download or view a file from the server."""
    file_path = request.args.get('path')

    if not file_path:
        return jsonify({"error": "Missing path parameter"}), 400

    # Security: Ensure the file path is within allowed directories
    normalized_path = os.path.normpath(file_path)
    if not (is_valid_library_path(normalized_path) or
            normalized_path.startswith(os.path.normpath(TARGET_DIR))):
        return jsonify({"error": "Access denied"}), 403

    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return jsonify({"error": "File not found"}), 404

    try:
        # Determine MIME type based on file extension
        ext = os.path.splitext(file_path)[1].lower()
        comic_mime_types = {
            '.cbz': 'application/vnd.comicbook+zip',
            '.cbr': 'application/vnd.comicbook-rar',
            '.pdf': 'application/pdf',
            '.epub': 'application/epub+zip',
        }
        mime_type = comic_mime_types.get(ext, 'application/octet-stream')

        return send_file(file_path, as_attachment=True, mimetype=mime_type)
    except Exception as e:
        app_logger.error(f"Error serving file {file_path}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/read-text-file')
def read_text_file():
    """Read and return the contents of a text file."""
    file_path = request.args.get('path')

    if not file_path:
        return "Missing path parameter", 400

    # Security: Ensure the file path is within allowed directories
    normalized_path = os.path.normpath(file_path)
    if not (is_valid_library_path(normalized_path) or
            normalized_path.startswith(os.path.normpath(TARGET_DIR))):
        return "Access denied", 403

    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return "File not found", 404

    # Check if file is a text file
    if not file_path.lower().endswith('.txt'):
        return "Only .txt files are supported", 400

    try:
        # Read the file with UTF-8 encoding, falling back to latin-1 if needed
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            # Try with latin-1 encoding if UTF-8 fails
            with open(file_path, 'r', encoding='latin-1') as f:
                content = f.read()

        return content, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except Exception as e:
        app_logger.error(f"Error reading text file {file_path}: {e}")
        return f"Error reading file: {str(e)}", 500



#########################
#  Serve Static Files   #
#########################
STATIC_DIR = "static"
os.makedirs(STATIC_DIR, exist_ok=True)

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(STATIC_DIR, filename)

@app.route('/manifest.json')
def serve_manifest():
    """Serve PWA manifest from root URL."""
    return send_from_directory(STATIC_DIR, 'manifest.json')

#########################
# Restart Flask App     #
#########################
def restart_app():
    """Gracefully restart the Flask application."""
    time.sleep(2)  # Delay to ensure the response is sent before restart
    os.execv(sys.executable, ['python'] + sys.argv)

@app.route('/restart', methods=['POST'])
def restart():
    threading.Thread(target=restart_app).start()  # Restart in a separate thread
    app_logger.info(f"Restarting Flask app...")
    return jsonify({"message": "Restarting Flask app..."}), 200

#########################
# Version Check         #
#########################
# Cache for version check (avoid hitting GitHub API too frequently)
version_check_cache = {
    "last_check": 0,
    "latest_version": None,
    "error": None
}
VERSION_CACHE_DURATION = 21600  # 6 hours in seconds

@app.route('/api/version-check')
def version_check():
    """
    Check for updates by comparing current version with latest GitHub release.
    Caches the result for 6 hours to respect GitHub API rate limits.
    """
    current_time = time.time()

    # Return cached result if within cache duration
    if current_time - version_check_cache["last_check"] < VERSION_CACHE_DURATION:
        if version_check_cache["error"]:
            return jsonify({
                "current_version": __version__,
                "error": version_check_cache["error"]
            }), 200

        return jsonify({
            "current_version": __version__,
            "latest_version": version_check_cache["latest_version"],
            "update_available": pkg_version.parse(version_check_cache["latest_version"]) > pkg_version.parse(__version__),
            "release_url": f"https://github.com/allaboutduncan/comic-utils/releases/tag/v{version_check_cache['latest_version']}"
        }), 200

    # Fetch latest version from GitHub
    try:
        response = requests.get(
            "https://api.github.com/repos/allaboutduncan/comic-utils/releases/latest",
            timeout=5
        )
        response.raise_for_status()

        release_data = response.json()
        latest_version = release_data.get("tag_name", "").lstrip("v")

        # Update cache
        version_check_cache["last_check"] = current_time
        version_check_cache["latest_version"] = latest_version
        version_check_cache["error"] = None

        return jsonify({
            "current_version": __version__,
            "latest_version": latest_version,
            "update_available": pkg_version.parse(latest_version) > pkg_version.parse(__version__),
            "release_url": f"https://github.com/allaboutduncan/comic-utils/releases/tag/v{latest_version}"
        }), 200

    except requests.exceptions.RequestException as e:
        error_msg = f"Failed to check for updates: {str(e)}"
        app_logger.warning(error_msg)

        # Update cache with error
        version_check_cache["last_check"] = current_time
        version_check_cache["error"] = error_msg

        return jsonify({
            "current_version": __version__,
            "error": error_msg
        }), 200

#########################
#   Scrape Page Routes  #
#########################

from queue import Queue
from scrape.scrape_readcomiconline import scrape_series
from scrape.scrape_ehentai import scrape_urls as scrape_ehentai_urls
from scrape.scrape_erofus import scrape as scrape_erofus_url

# Store active scrape tasks
# Each task has: log_queue, progress_queue, status, buffered_logs
scrape_tasks = {}

@app.route("/scrape")
def scrape_page():
    """Render the scrape page"""
    # Use TARGET_DIR directly to avoid file monitor processing
    target_dir = config.get("SETTINGS", "TARGET", fallback="/processed")
    # Get active tasks for status display
    active_tasks = []
    for task_id, task_info in scrape_tasks.items():
        if task_info["status"] == "running":
            active_tasks.append({
                "task_id": task_id,
                "status": task_info["status"]
            })
    return render_template("scrape.html", target_dir=target_dir, active_tasks=active_tasks)

@app.route("/scrape-readcomiconline", methods=["POST"])
def scrape_readcomiconline():
    """Start scraping readcomiconline URLs"""
    try:
        data = request.json
        urls = data.get("urls", [])
        # Use TARGET_DIR directly to avoid file monitor processing and overwrites
        output_dir = data.get("output_dir", config.get("SETTINGS", "TARGET", fallback="/processed"))

        if not urls:
            return jsonify({"success": False, "error": "No URLs provided"}), 400

        # Create a unique task ID
        task_id = str(uuid.uuid4())

        # Create a queue for logs and progress
        log_queue = Queue()
        progress_queue = Queue()

        # Store task info with buffered logs for reconnection
        scrape_tasks[task_id] = {
            "log_queue": log_queue,
            "progress_queue": progress_queue,
            "status": "running",
            "buffered_logs": [],
            "last_progress": {}
        }

        # Start scraping in a background thread
        def scrape_worker():
            def log_callback(msg):
                log_queue.put(msg)

            def progress_callback(data):
                progress_queue.put(data)

            try:
                for url in urls:
                    log_queue.put(f"\n{'='*60}")
                    log_queue.put(f"Processing: {url}")
                    log_queue.put('='*60)

                    scrape_series(url, output_dir, log_callback, progress_callback)

                log_queue.put("\n=== All URLs processed ===")
                scrape_tasks[task_id]["status"] = "completed"
                log_queue.put("__COMPLETED__")  # Signal completion

            except Exception as e:
                log_queue.put(f"\n=== Error: {str(e)} ===")
                scrape_tasks[task_id]["status"] = "error"
                log_queue.put("__ERROR__")  # Signal error

        thread = threading.Thread(target=scrape_worker, daemon=True)
        thread.start()

        return jsonify({"success": True, "task_id": task_id}), 200

    except Exception as e:
        app_logger.error(f"Error starting scrape: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/scrape-ehentai", methods=["POST"])
def scrape_ehentai():
    """Start scraping E-Hentai URLs"""
    try:
        data = request.json
        urls = data.get("urls", [])
        # Use TARGET_DIR directly to avoid file monitor processing and overwrites
        output_dir = data.get("output_dir", config.get("SETTINGS", "TARGET", fallback="/processed"))

        if not urls:
            return jsonify({"success": False, "error": "No URLs provided"}), 400

        # Create a unique task ID
        task_id = str(uuid.uuid4())

        # Create a queue for logs and progress
        log_queue = Queue()
        progress_queue = Queue()

        # Store task info with buffered logs for reconnection
        scrape_tasks[task_id] = {
            "log_queue": log_queue,
            "progress_queue": progress_queue,
            "status": "running",
            "buffered_logs": [],
            "last_progress": {}
        }

        # Start scraping in a background thread
        def scrape_worker():
            def log_callback(msg):
                log_queue.put(msg)

            def progress_callback(data):
                progress_queue.put(data)

            try:
                # Scrape all URLs
                scrape_ehentai_urls(urls, output_dir, log_callback, progress_callback)

                log_queue.put("\n=== All URLs processed ===")
                scrape_tasks[task_id]["status"] = "completed"
                log_queue.put("__COMPLETED__")  # Signal completion

            except Exception as e:
                log_queue.put(f"\n=== Error: {str(e)} ===")
                scrape_tasks[task_id]["status"] = "error"
                log_queue.put("__ERROR__")  # Signal error

        thread = threading.Thread(target=scrape_worker, daemon=True)
        thread.start()

        return jsonify({"success": True, "task_id": task_id}), 200

    except Exception as e:
        app_logger.error(f"Error starting E-Hentai scrape: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/scrape-erofus", methods=["POST"])
def scrape_erofus():
    """Start scraping Erofus URLs"""
    try:
        data = request.json
        urls = data.get("urls", [])
        # Use TARGET_DIR directly to avoid file monitor processing and overwrites
        output_dir = data.get("output_dir", config.get("SETTINGS", "TARGET", fallback="/processed"))

        if not urls:
            return jsonify({"success": False, "error": "No URLs provided"}), 400

        # Create a unique task ID
        task_id = str(uuid.uuid4())

        # Create a queue for logs and progress
        log_queue = Queue()
        progress_queue = Queue()

        # Store task info with buffered logs for reconnection
        scrape_tasks[task_id] = {
            "log_queue": log_queue,
            "progress_queue": progress_queue,
            "status": "running",
            "buffered_logs": [],
            "last_progress": {}
        }

        # Start scraping in a background thread
        def scrape_worker():
            def log_callback(msg):
                log_queue.put(msg)

            def progress_callback(data):
                progress_queue.put(data)

            try:
                for url in urls:
                    log_queue.put(f"\n{'='*60}")
                    log_queue.put(f"Processing: {url}")
                    log_queue.put('='*60)

                    scrape_erofus_url(url, output_dir, log_callback, progress_callback)

                log_queue.put("\n=== All URLs processed ===")
                scrape_tasks[task_id]["status"] = "completed"
                log_queue.put("__COMPLETED__")  # Signal completion

            except Exception as e:
                error_details = traceback.format_exc()
                log_queue.put(f"\n=== Error: {str(e)} ===")
                log_queue.put(f"Exception type: {type(e).__name__}")
                log_queue.put(f"Traceback:\n{error_details}")
                app_logger.error(f"Erofus scrape error: {e}\n{error_details}")
                scrape_tasks[task_id]["status"] = "error"
                log_queue.put("__ERROR__")  # Signal error

        thread = threading.Thread(target=scrape_worker, daemon=True)
        thread.start()

        return jsonify({"success": True, "task_id": task_id}), 200

    except Exception as e:
        app_logger.error(f"Error starting Erofus scrape: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/scrape-stream/<task_id>")
def scrape_stream(task_id):
    """Server-Sent Events stream for scrape logs and progress"""
    def generate():
        if task_id not in scrape_tasks:
            yield f"data: Task not found\n\n"
            return

        task = scrape_tasks[task_id]
        log_queue = task["log_queue"]
        progress_queue = task["progress_queue"]

        # Send buffered logs first (for reconnections)
        for buffered_log in task["buffered_logs"]:
            yield f"data: {buffered_log}\n\n"

        # Send last known progress
        if task["last_progress"]:
            import json
            yield f"event: progress\ndata: {json.dumps(task['last_progress'])}\n\n"

        # Keepalive counter - send keepalive every 15 seconds
        keepalive_counter = 0
        keepalive_interval = 150  # 150 * 0.1s = 15 seconds

        while True:
            has_activity = False

            # Check for log messages
            if not log_queue.empty():
                msg = log_queue.get()
                has_activity = True

                if msg == "__COMPLETED__":
                    yield f"event: completed\ndata: {{}}\n\n"
                    break
                elif msg == "__ERROR__":
                    yield f"event: error\ndata: {{}}\n\n"
                    break
                else:
                    # Buffer the log for reconnections
                    task["buffered_logs"].append(msg)
                    # Keep only last 100 log lines
                    if len(task["buffered_logs"]) > 100:
                        task["buffered_logs"].pop(0)
                    yield f"data: {msg}\n\n"

            # Check for progress updates
            if not progress_queue.empty():
                progress_data = progress_queue.get()
                has_activity = True
                # Store last progress for reconnections
                task["last_progress"] = progress_data
                import json
                yield f"event: progress\ndata: {json.dumps(progress_data)}\n\n"

            # Send keepalive if no activity
            if not has_activity:
                keepalive_counter += 1
                if keepalive_counter >= keepalive_interval:
                    yield f": keepalive\n\n"
                    keepalive_counter = 0
            else:
                keepalive_counter = 0

            time.sleep(0.1)

        # Clean up task after streaming completes
        if task_id in scrape_tasks:
            del scrape_tasks[task_id]

    return Response(generate(), mimetype="text/event-stream")

@app.route("/scrape-status")
def scrape_status():
    """Get current scrape status for badge display"""
    try:
        # Check if there are any active scrape tasks
        active_count = 0
        total_progress = 0

        for task_id, task_info in scrape_tasks.items():
            if task_info["status"] == "running":
                active_count += 1
                # Get last known progress
                if "last_progress" in task_info and "progress" in task_info["last_progress"]:
                    total_progress += task_info["last_progress"]["progress"]

        if active_count > 0:
            avg_progress = int(total_progress / active_count)
            return jsonify({
                "active": active_count,
                "progress": avg_progress
            })
        else:
            return jsonify({
                "active": 0,
                "progress": 0
            })
    except Exception as e:
        app_logger.error(f"Error getting scrape status: {e}")
        return jsonify({"active": 0, "progress": 0})

#########################
#   Config Page Route   #
#########################
def sanitize_config_value(value: str) -> str:
    """
    Sanitize a config value to ensure it's safe for INI file storage.
    Removes newlines and strips whitespace while preserving special characters.
    """
    if not value:
        return ""
    # Remove newlines and carriage returns (would break INI format)
    sanitized = value.replace('\n', '').replace('\r', '')
    # Strip leading/trailing whitespace
    return sanitized.strip()


#####################################
#    Config API Endpoints (AJAX)    #
#####################################

@app.route('/api/config/file-processing', methods=['POST'])
def save_file_processing_config():
    """Save file processing settings via AJAX."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        # Ensure SETTINGS section exists
        if "SETTINGS" not in config:
            config["SETTINGS"] = {}

        # Update config values
        config["SETTINGS"]["WATCH"] = data.get("watch", "/temp")
        config["SETTINGS"]["TARGET"] = data.get("target", "/processed")
        config["SETTINGS"]["AUTOCONVERT"] = str(data.get("autoConvert", False))
        config["SETTINGS"]["READ_SUBDIRECTORIES"] = str(data.get("readSubdirectories", False))
        config["SETTINGS"]["AUTO_UNPACK"] = str(data.get("autoUnpack", False))
        config["SETTINGS"]["AUTO_RENAME_MONITOR"] = str(data.get("autoRenameMonitor", True))
        config["SETTINGS"]["MOVE_DIRECTORY"] = str(data.get("moveDirectory", False))
        config["SETTINGS"]["CONSOLIDATE_DIRECTORIES"] = str(data.get("consolidateDirectories", False))
        config["SETTINGS"]["IGNORED_EXTENSIONS"] = data.get("ignored_extensions", "")
        config["SETTINGS"]["AUTO_CLEANUP_ORPHAN_FILES"] = str(data.get("autoCleanupOrphanFiles", False))
        config["SETTINGS"]["CLEANUP_INTERVAL_HOURS"] = data.get("cleanupIntervalHours", "24")
        config["SETTINGS"]["CONVERT_SUBDIRECTORIES"] = str(data.get("convertSubdirectories", False))
        config["SETTINGS"]["SKIPPED_FILES"] = data.get("skippedFiles", "")
        config["SETTINGS"]["DELETED_FILES"] = data.get("deletedFiles", "")
        config["SETTINGS"]["ENABLE_CUSTOM_RENAME"] = str(data.get("enableCustomRename", False))
        config["SETTINGS"]["CUSTOM_RENAME_PATTERN"] = data.get("customRenamePattern", "")
        config["SETTINGS"]["ENABLE_AUTO_RENAME"] = str(data.get("enableAutoRename", False))

        # Also persist custom rename settings to user_preferences DB
        from database import set_user_preference
        set_user_preference('enable_custom_rename', data.get("enableCustomRename", False), category='file_processing')
        set_user_preference('custom_rename_pattern', data.get("customRenamePattern", ""), category='file_processing')
        config["SETTINGS"]["ENABLE_AUTO_MOVE"] = str(data.get("enableAutoMove", False))
        config["SETTINGS"]["CUSTOM_MOVE_PATTERN"] = data.get("customMovePattern", "{publisher}/{series_name}/v{year}")

        write_config()
        load_flask_config(app)
        return jsonify({"success": True, "message": "File processing settings saved"})
    except Exception as e:
        app_logger.error(f"Error saving file processing config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/config/download-api', methods=['POST'])
def save_download_api_config():
    """Save download and API settings via AJAX."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        # Ensure SETTINGS section exists
        if "SETTINGS" not in config:
            config["SETTINGS"] = {}

        # Update config values
        config["SETTINGS"]["HEADERS"] = data.get("customHeaders", "")
        config["SETTINGS"]["PIXELDRAIN_API_KEY"] = sanitize_config_value(data.get("pixeldrainApiKey", ""))
        config["SETTINGS"]["COMICVINE_API_KEY"] = sanitize_config_value(data.get("comicvineApiKey", ""))
        config["SETTINGS"]["METRON_USERNAME"] = sanitize_config_value(data.get("metronUsername", ""))
        config["SETTINGS"]["METRON_PASSWORD"] = sanitize_config_value(data.get("metronPassword", ""))
        config["SETTINGS"]["DOWNLOAD_PROVIDER_PRIORITY"] = data.get("downloadProviderPriority", "pixeldrain,download_now,mega")

        # Also save API credentials to DB (provider_credentials)
        try:
            from database import save_provider_credentials
            metron_u = sanitize_config_value(data.get("metronUsername", ""))
            metron_p = sanitize_config_value(data.get("metronPassword", ""))
            if metron_u and metron_p:
                save_provider_credentials('metron', {'username': metron_u, 'password': metron_p})
            cv_key = sanitize_config_value(data.get("comicvineApiKey", ""))
            if cv_key:
                save_provider_credentials('comicvine', {'api_key': cv_key})
        except Exception:
            pass

        write_config()
        load_flask_config(app)
        return jsonify({"success": True, "message": "Download & API settings saved"})
    except Exception as e:
        app_logger.error(f"Error saving download/API config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/preferences/<key>', methods=['GET'])
def api_get_preference(key):
    """Get a user preference value."""
    from database import get_user_preference
    value = get_user_preference(key)
    return jsonify({"success": True, "value": value})


@app.route('/api/preferences/<key>', methods=['POST'])
def api_save_preference(key):
    """Save a user preference value."""
    from database import set_user_preference
    data = request.get_json()
    if not data or 'value' not in data:
        return jsonify({"success": False, "error": "No value provided"}), 400
    set_user_preference(key, data['value'], category=data.get('category', 'general'))
    return jsonify({"success": True})


@app.route('/api/config/system-perf', methods=['POST'])
def save_system_perf_config():
    """Save system and performance settings via AJAX."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        # Ensure SETTINGS section exists
        if "SETTINGS" not in config:
            config["SETTINGS"] = {}

        # Update config values
        config["SETTINGS"]["OPERATION_TIMEOUT"] = data.get("operationTimeout", "3600")
        config["SETTINGS"]["LARGE_FILE_THRESHOLD"] = data.get("largeFileThreshold", "500")
        config["SETTINGS"]["TIMEZONE"] = data.get("timezone", "UTC")
        config["SETTINGS"]["REBUILD_FREQUENCY"] = data.get("rebuildFrequency", "disabled")
        config["SETTINGS"]["REBUILD_TIME"] = data.get("rebuildTime", "02:00")
        config["SETTINGS"]["REBUILD_WEEKDAY"] = data.get("rebuildWeekday", "0")
        config["SETTINGS"]["SYNC_FREQUENCY"] = data.get("syncFrequency", "disabled")
        config["SETTINGS"]["SYNC_TIME"] = data.get("syncTime", "03:00")
        config["SETTINGS"]["SYNC_WEEKDAY"] = data.get("syncWeekday", "0")
        config["SETTINGS"]["GETCOMICS_FREQUENCY"] = data.get("getcomicsFrequency", "disabled")
        config["SETTINGS"]["GETCOMICS_TIME"] = data.get("getcomicsTime", "04:00")
        config["SETTINGS"]["GETCOMICS_WEEKDAY"] = data.get("getcomicsWeekday", "0")
        config["SETTINGS"]["IGNORED_TERMS"] = data.get("ignored_terms", "")
        config["SETTINGS"]["IGNORED_FILES"] = data.get("ignored_files", "")
        config["SETTINGS"]["ENABLE_DEBUG_LOGGING"] = str(data.get("enableDebugLogging", False))
        config["SETTINGS"]["XML_YEAR"] = str(data.get("xmlYear", False))
        config["SETTINGS"]["XML_MARKDOWN"] = str(data.get("xmlMarkdown", False))
        config["SETTINGS"]["XML_LIST"] = str(data.get("xmlList", False))

        write_config()
        load_flask_config(app)

        # Update logger level dynamically
        import logging
        if config["SETTINGS"]["ENABLE_DEBUG_LOGGING"] == "True":
            app_logger.setLevel(logging.DEBUG)
        else:
            app_logger.setLevel(logging.INFO)

        return jsonify({"success": True, "message": "System & Performance settings saved"})
    except Exception as e:
        app_logger.error(f"Error saving system/perf config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/config/styling', methods=['POST'])
def save_styling_config():
    """Save styling settings via AJAX."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        set_user_preference('bootstrap_theme', data.get("bootstrapTheme", "default"), category='personalization')
        app.config["BOOTSTRAP_THEME"] = data.get("bootstrapTheme", "default")
        return jsonify({"success": True, "message": "Styling settings saved"})
    except Exception as e:
        app_logger.error(f"Error saving styling config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/config/dashboard', methods=['POST'])
def save_dashboard_config():
    """Save dashboard layout settings via AJAX."""
    from database import set_user_preference
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        valid_ids = {'favorites', 'want_to_read', 'continue_reading', 'discover', 'recently_added', 'library'}

        # Validate and sanitize order
        raw_order = data.get("dashboardOrder", [])
        if isinstance(raw_order, str):
            raw_order = [s.strip() for s in raw_order.split(',') if s.strip()]
        order = [s for s in raw_order if s in valid_ids]
        # Append any missing sections at the end to prevent data loss
        for sid in valid_ids:
            if sid not in order:
                order.append(sid)

        # Validate hidden list
        raw_hidden = data.get("dashboardHidden", [])
        if isinstance(raw_hidden, str):
            raw_hidden = [s.strip() for s in raw_hidden.split(',') if s.strip()]
        hidden = [s for s in raw_hidden if s in valid_ids]

        set_user_preference('dashboard_order', order, category='dashboard')
        set_user_preference('dashboard_hidden', hidden, category='dashboard')

        return jsonify({"success": True, "message": "Dashboard settings saved"})
    except Exception as e:
        app_logger.error(f"Error saving dashboard config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/config/recommendations', methods=['POST'])
def save_recommendations_config():
    """Save recommendation settings via AJAX."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        set_user_preference('rec_enabled', bool(data.get("recEnabled", False)), category='personalization')
        set_user_preference('rec_provider', data.get("recProvider", "gemini"), category='personalization')
        set_user_preference('rec_api_key', data.get("recApiKey", ""), category='personalization')
        set_user_preference('rec_model', data.get("recModel", "gemini-2.0-flash"), category='personalization')

        return jsonify({"success": True, "message": "Recommendation settings saved"})
    except Exception as e:
        app_logger.error(f"Error saving recommendations config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/config", methods=["GET", "POST"])
def config_page():
    if request.method == "POST":
        # Ensure SETTINGS section exists
        if "SETTINGS" not in config:
            config["SETTINGS"] = {}

        # Safely update config values
        new_watch = request.form.get("watch", "/temp")
        new_target = request.form.get("target", "/processed")
        
        # Validate that watch and target are not the same
        if new_watch == new_target:
            return jsonify({"error": "Watch and target folders cannot be the same"}), 400
        
        # Validate that watch and target are not subdirectories of each other
        if new_watch.startswith(new_target + "/") or new_target.startswith(new_watch + "/"):
            return jsonify({"error": "Watch and target folders cannot be subdirectories of each other"}), 400
        
        config["SETTINGS"]["WATCH"] = new_watch
        config["SETTINGS"]["TARGET"] = new_target
        config["SETTINGS"]["IGNORED_TERMS"] = request.form.get("ignored_terms", "")
        config["SETTINGS"]["IGNORED_FILES"] = request.form.get("ignored_files", "")
        config["SETTINGS"]["IGNORED_EXTENSIONS"] = request.form.get("ignored_extensions", "")
        config["SETTINGS"]["AUTOCONVERT"] = str(request.form.get("autoConvert") == "on")
        config["SETTINGS"]["READ_SUBDIRECTORIES"] = str(request.form.get("readSubdirectories") == "on")
        config["SETTINGS"]["CONVERT_SUBDIRECTORIES"] = str(request.form.get("convertSubdirectories") == "on")        
        config["SETTINGS"]["XML_YEAR"] = str(request.form.get("xmlYear") == "on")
        config["SETTINGS"]["XML_MARKDOWN"] = str(request.form.get("xmlMarkdown") == "on")
        config["SETTINGS"]["XML_LIST"] = str(request.form.get("xmlList") == "on")
        config["SETTINGS"]["MOVE_DIRECTORY"] = str(request.form.get("moveDirectory") == "on")
        config["SETTINGS"]["CONSOLIDATE_DIRECTORIES"] = str(request.form.get("consolidateDirectories") == "on")
        config["SETTINGS"]["AUTO_UNPACK"] = str(request.form.get("autoUnpack") == "on")
        config["SETTINGS"]["AUTO_RENAME_MONITOR"] = str(request.form.get("autoRenameMonitor") == "on")
        config["SETTINGS"]["AUTO_CLEANUP_ORPHAN_FILES"] = str(request.form.get("autoCleanupOrphanFiles") == "on")
        config["SETTINGS"]["CLEANUP_INTERVAL_HOURS"] = request.form.get("cleanupIntervalHours", "1")
        config["SETTINGS"]["HEADERS"] = request.form.get("customHeaders", "")
        config["SETTINGS"]["SKIPPED_FILES"] = request.form.get("skippedFiles", "")
        config["SETTINGS"]["DELETED_FILES"] = request.form.get("deletedFiles", "")
        config["SETTINGS"]["OPERATION_TIMEOUT"] = request.form.get("operationTimeout", "3600")
        config["SETTINGS"]["LARGE_FILE_THRESHOLD"] = request.form.get("largeFileThreshold", "500")
        config["SETTINGS"]["PIXELDRAIN_API_KEY"] = sanitize_config_value(request.form.get("pixeldrainApiKey", ""))
        config["SETTINGS"]["COMICVINE_API_KEY"] = sanitize_config_value(request.form.get("comicvineApiKey", ""))
        config["SETTINGS"]["METRON_USERNAME"] = sanitize_config_value(request.form.get("metronUsername", ""))
        config["SETTINGS"]["METRON_PASSWORD"] = sanitize_config_value(request.form.get("metronPassword", ""))

        # Also save API credentials to DB (provider_credentials)
        try:
            from database import save_provider_credentials
            metron_u = sanitize_config_value(request.form.get("metronUsername", ""))
            metron_p = sanitize_config_value(request.form.get("metronPassword", ""))
            if metron_u and metron_p:
                save_provider_credentials('metron', {'username': metron_u, 'password': metron_p})
            cv_key = sanitize_config_value(request.form.get("comicvineApiKey", ""))
            if cv_key:
                save_provider_credentials('comicvine', {'api_key': cv_key})
        except Exception:
            pass

        config["SETTINGS"]["ENABLE_CUSTOM_RENAME"] = str(request.form.get("enableCustomRename") == "on")
        config["SETTINGS"]["CUSTOM_RENAME_PATTERN"] = request.form.get("customRenamePattern", "")
        config["SETTINGS"]["ENABLE_AUTO_RENAME"] = str(request.form.get("enableAutoRename") == "on")
        config["SETTINGS"]["ENABLE_AUTO_MOVE"] = str(request.form.get("enableAutoMove") == "on")
        config["SETTINGS"]["CUSTOM_MOVE_PATTERN"] = request.form.get("customMovePattern", "{publisher}/{series_name}/v{year}")
        config["SETTINGS"]["DOWNLOAD_PROVIDER_PRIORITY"] = request.form.get("downloadProviderPriority", "pixeldrain,download_now,mega")
        config["SETTINGS"]["ENABLE_DEBUG_LOGGING"] = str(request.form.get("enableDebugLogging") == "on")
        config["SETTINGS"]["TIMEZONE"] = request.form.get("timezone", "UTC")

        # Styling and Recommendations are saved to user_preferences DB
        set_user_preference('bootstrap_theme', request.form.get("bootstrapTheme", "default"), category='personalization')
        app.config["BOOTSTRAP_THEME"] = request.form.get("bootstrapTheme", "default")

        set_user_preference('rec_enabled', request.form.get("recEnabled") == "on", category='personalization')
        set_user_preference('rec_provider', request.form.get("recProvider", "gemini"), category='personalization')
        set_user_preference('rec_api_key', request.form.get("recApiKey", ""), category='personalization')
        set_user_preference('rec_model', request.form.get("recModel", "gemini-2.0-flash"), category='personalization')

        write_config()  # Save changes to config.ini
        load_flask_config(app)  # Reload into Flask config

        # Update logger level dynamically
        import logging
        if config["SETTINGS"]["ENABLE_DEBUG_LOGGING"] == "True":
            app_logger.setLevel(logging.DEBUG)
            app_logger.info("Debug logging enabled")
        else:
            app_logger.setLevel(logging.INFO)
            app_logger.info("Debug logging disabled")

        return redirect(url_for("config_page"))

    # Ensure SETTINGS section is a dictionary before accessing
    settings = config["SETTINGS"] if "SETTINGS" in config else {}

    from database import get_user_preference, get_provider_credentials
    from routes.collection import get_dashboard_order

    # Load credential display values from DB if available
    try:
        _metron_creds = get_provider_credentials('metron')
        _cv_creds = get_provider_credentials('comicvine')
    except Exception:
        _metron_creds = None
        _cv_creds = None

    return render_template(
        "config.html",
        watch=settings.get("WATCH", "/temp"),
        target=settings.get("TARGET", "/processed"),
        ignored_terms=settings.get("IGNORED_TERMS", ""),
        ignored_files=settings.get("IGNORED_FILES", ""),
        ignored_extensions=settings.get("IGNORED_EXTENSIONS", ""),
        autoConvert=settings.get("AUTOCONVERT", "False") == "True",
        readSubdirectories=settings.get("READ_SUBDIRECTORIES", "False") == "True",
        convertSubdirectories=settings.get("CONVERT_SUBDIRECTORIES", "False") == "True",
        xmlYear=settings.get("XML_YEAR", "False") == "True",
        xmlMarkdown=settings.get("XML_MARKDOWN", "False") == "True",
        xmlList=settings.get("XML_LIST", "False") == "True",
        moveDirectory=settings.get("MOVE_DIRECTORY", "False") == "True",
        consolidateDirectories=settings.get("CONSOLIDATE_DIRECTORIES", "False") == "True",
        autoUnpack=settings.get("AUTO_UNPACK", "False") == "True",
        autoRenameMonitor=settings.get("AUTO_RENAME_MONITOR", "True") == "True",
        autoCleanupOrphanFiles=settings.get("AUTO_CLEANUP_ORPHAN_FILES", "False") == "True",
        cleanupIntervalHours=settings.get("CLEANUP_INTERVAL_HOURS", "1"),
        skippedFiles=settings.get("SKIPPED_FILES", ""),
        deletedFiles=settings.get("DELETED_FILES", ""),
        customHeaders=settings.get("HEADERS", ""),
        operationTimeout=settings.get("OPERATION_TIMEOUT", "3600"),
        largeFileThreshold=settings.get("LARGE_FILE_THRESHOLD", "500"),
        pixeldrainApiKey=settings.get("PIXELDRAIN_API_KEY", ""),
        comicvineApiKey=(_cv_creds.get('api_key', '') if _cv_creds else '') or settings.get("COMICVINE_API_KEY", ""),
        metronUsername=(_metron_creds.get('username', '') if _metron_creds else '') or settings.get("METRON_USERNAME", ""),
        metronPassword=(_metron_creds.get('password', '') if _metron_creds else '') or settings.get("METRON_PASSWORD", ""),
        enableCustomRename=settings.get("ENABLE_CUSTOM_RENAME", "False") == "True",
        customRenamePattern=settings.get("CUSTOM_RENAME_PATTERN", ""),
        enableAutoRename=settings.get("ENABLE_AUTO_RENAME", "False") == "True",
        enableAutoMove=settings.get("ENABLE_AUTO_MOVE", "False") == "True",
        customMovePattern=settings.get("CUSTOM_MOVE_PATTERN", "{publisher}/{series_name}/v{year}"),
        downloadProviderPriority=settings.get("DOWNLOAD_PROVIDER_PRIORITY", "pixeldrain,download_now,mega"),
        enableDebugLogging=settings.get("ENABLE_DEBUG_LOGGING", "False") == "True",
        bootstrapTheme=get_user_preference('bootstrap_theme', default='default'),
        timezone=settings.get("TIMEZONE", "UTC"),
        config=settings,  # Pass full settings dictionary
        rec_enabled=get_user_preference('rec_enabled', default=True),
        rec_provider=get_user_preference('rec_provider', default='gemini'),
        rec_api_key=get_user_preference('rec_api_key', default=''),
        rec_model=get_user_preference('rec_model', default='gemini-2.0-flash'),
        dashboard_order=get_dashboard_order(),
        dashboard_hidden=get_user_preference('dashboard_hidden', default=[])
    )

#########################
#   Streaming Routes    #
#########################
@app.route('/stream/<script_type>')
def stream_logs(script_type):
    file_path = request.args.get('file_path')  # Get file_path for single_file script
    directory = request.args.get('directory')  # Get directory for rebuild/rename script

    # Define supported script types for single file actions
    single_file_scripts = ['single_file', 'crop', 'remove', 'delete','enhance_single', 'add']

    # Check if the correct parameter is passed for single_file scripts
    if script_type in single_file_scripts:
        if not file_path:
            return Response("Missing file_path for single file action.", status=400)
        elif not os.path.isfile(file_path):
            return Response("Invalid file_path.", status=400)

        script_module = f"cbz_ops.{script_type}"

        def generate_logs():
            # Track single-file convert/rebuild operations
            op_id = None
            if script_type == 'single_file':
                op_id = app_state.register_operation("convert", os.path.basename(file_path))
            _progress_re = re.compile(r'Compression progress: ([\d.]+)% \((\d+)/(\d+)')
            _step_re = re.compile(r'Step (\d+)/(\d+): (.+)')

            process = subprocess.Popen(
                ['python', '-u', '-m', script_module, file_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=0
            )
            # Capture both stdout and stderr
            try:
                for line in process.stdout:
                    if op_id:
                        m = _progress_re.search(line)
                        if m:
                            pct = int(float(m.group(1)))
                            app_state.update_operation(op_id, current=pct, total=100, detail=f"Compressing ({m.group(2)}/{m.group(3)} files)")
                        else:
                            m2 = _step_re.search(line)
                            if m2:
                                app_state.update_operation(op_id, detail=m2.group(3).strip('.\n'))
                    yield f"data: {line}\n\n"  # Format required by SSE
                for line in process.stderr:
                    yield f"data: ERROR: {line}\n\n"
            except GeneratorExit:
                # Client disconnected — continue monitoring subprocess in background
                if process.poll() is None and op_id:
                    def _drain_and_track():
                        try:
                            for line in process.stdout:
                                m = _progress_re.search(line)
                                if m:
                                    pct = int(float(m.group(1)))
                                    app_state.update_operation(op_id, current=pct, total=100,
                                        detail=f"Compressing ({m.group(2)}/{m.group(3)} files)")
                            process.wait()
                            app_state.complete_operation(op_id, error=(process.returncode != 0))
                        except Exception:
                            app_state.complete_operation(op_id, error=True)
                    threading.Thread(target=_drain_and_track, daemon=True).start()
                return

            process.wait()
            if op_id:
                app_state.complete_operation(op_id, error=(process.returncode != 0))
            if process.returncode != 0:
                yield f"data: An error occurred while streaming logs. Return code: {process.returncode}.\n\n"
            else:
                yield "event: completed\ndata: Process completed successfully.\n\n"

        return Response(generate_logs(), content_type='text/event-stream')

    # Handle scripts that operate on directories
    elif script_type in ['rebuild', 'rename', 'convert', 'pdf', 'missing', 'enhance_dir','comicinfo']:
        if not directory or not os.path.isdir(directory):
            return Response("Invalid or missing directory path.", status=400)

        # Scripts in cbz_ops/ package vs root
        cbz_ops_scripts = ['rebuild', 'rename', 'convert', 'pdf', 'enhance_dir']
        if script_type in cbz_ops_scripts:
            script_cmd = ['-m', f"cbz_ops.{script_type}"]
        else:
            script_cmd = [f"{script_type}.py"]

        def generate_logs():
            # Set longer timeout for large file operations
            timeout_seconds = int(config.get("SETTINGS", "OPERATION_TIMEOUT", fallback="3600"))

            # Track conversion/rebuild operations
            op_id = None
            if script_type in ('convert', 'rebuild'):
                op_id = app_state.register_operation(script_type, os.path.basename(directory))

            process = subprocess.Popen(
                ['python', '-u'] + script_cmd + [directory],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=0
            )

            _convert_re = re.compile(r'Processing file: (.+?) \((\d+)/(\d+)\)')

            try:
                while True:
                    # Check if process is still running
                    if process.poll() is not None:
                        break

                    # Use select with timeout to check for output
                    ready, _, _ = select.select([process.stdout, process.stderr], [], [], 1.0)

                    if ready:
                        for stream in ready:
                            line = stream.readline()
                            if line:
                                if stream == process.stderr:
                                    yield f"data: ERROR: {line}\n\n"
                                else:
                                    # Parse conversion progress
                                    if op_id:
                                        m = _convert_re.search(line)
                                        if m:
                                            app_state.update_operation(
                                                op_id,
                                                current=int(m.group(2)),
                                                total=int(m.group(3)),
                                                detail=m.group(1),
                                            )
                                    yield f"data: {line}\n\n"
                            else:
                                # No more output from this stream
                                continue
                    else:
                        # No output available, send keepalive for long operations
                        if script_type in ['convert', 'rebuild']:
                            yield f"data: \n\n"  # Keepalive to prevent timeout

            except GeneratorExit:
                # Client disconnected — continue monitoring subprocess in background
                if process.poll() is None and op_id:
                    def _drain_and_track():
                        try:
                            for line in process.stdout:
                                m = _convert_re.search(line)
                                if m:
                                    app_state.update_operation(
                                        op_id,
                                        current=int(m.group(2)),
                                        total=int(m.group(3)),
                                        detail=m.group(1),
                                    )
                            process.wait(timeout=timeout_seconds)
                            app_state.complete_operation(op_id, error=(process.returncode != 0))
                        except Exception:
                            app_state.complete_operation(op_id, error=True)
                    threading.Thread(target=_drain_and_track, daemon=True).start()
                return

            # Wait for process to complete
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                if op_id:
                    app_state.complete_operation(op_id, error=True)
                yield f"data: ERROR: Process timed out after {timeout_seconds} seconds\n\n"
                return

            if op_id:
                app_state.complete_operation(op_id, error=(process.returncode != 0))

            if script_type == 'missing' and process.returncode == 0:
                # Define the path to the generated missing.txt
                missing_file_path = os.path.join(directory, "missing.txt")
                
                if os.path.exists(missing_file_path):
                    # Generate a unique filename to prevent overwriting
                    unique_id = uuid.uuid4().hex
                    static_missing_filename = f"missing_{unique_id}.txt"
                    static_missing_path = os.path.join(STATIC_DIR, static_missing_filename)
                    
                    try:
                        shutil.move(missing_file_path, static_missing_path)
                        missing_url = f"/static/{static_missing_filename}"
                        yield f"data: Download missing list: <a href='{missing_url}' target='_blank'>missing.txt</a>\n\n"
                    except Exception as e:
                        yield f"data: ERROR: Failed to move missing.txt: {str(e)}\n\n"

            if process.returncode != 0:
                yield f"data: An error occurred while streaming logs. Return code: {process.returncode}.\n\n"
            else:
                yield "event: completed\ndata: Process completed successfully.\n\n"

        headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive"
        }
        return Response(generate_logs(), headers=headers, content_type='text/event-stream')

    return Response("Invalid script type.", status=400)

#########################
#       Home Page       #
#########################
@app.route('/')
def index():
    from routes.collection import get_dashboard_sections
    # These environment variables are set/updated by load_config_into_env()
    watch = config.get("SETTINGS", "WATCH", fallback="/temp")
    convert_subdirectories = config.getboolean('SETTINGS', 'CONVERT_SUBDIRECTORIES', fallback=False)
    return render_template('collection.html',
                           watch=watch,
                           config=app.config,
                           convertSubdirectories=convert_subdirectories,
                           rec_enabled=get_user_preference('rec_enabled', default=True),
                           dashboard_sections=get_dashboard_sections())
    
#########################
#        App Logs       #
#########################
# Route for app logs page
@app.route('/logs')
def logs_page():
    """Combined logs page with tabs for app and monitor logs."""
    return render_template('logs.html', config=app.config)

@app.route('/app-logs')
def app_logs_page():
    return redirect(url_for('logs_page'))

# Route for monitor logs page
@app.route('/mon-logs')
def mon_logs_page():
    return redirect(url_for('logs_page'))

# Function to stream logs in real-time (tail last 1000 lines to prevent timeout)
def stream_logs_file(log_file):
    with open(log_file, "r") as file:
        # Tail approach: read last N lines efficiently
        MAX_LINES = 1000
        lines = []

        # Seek to end and work backwards to find last N lines
        file.seek(0, 2)  # Go to end of file
        file_size = file.tell()

        if file_size > 0:
            # Read in chunks from the end
            buffer_size = 8192
            position = file_size

            while position > 0 and len(lines) < MAX_LINES:
                # Move back by buffer_size or to start of file
                position = max(0, position - buffer_size)
                file.seek(position)
                chunk = file.read(min(buffer_size, file_size - position))
                lines = chunk.splitlines() + lines

            # Keep only last MAX_LINES
            lines = lines[-MAX_LINES:]

            # Yield initial lines
            for line in lines:
                yield f"data: {line}\n\n"

        # Now stream new lines as they're added
        file.seek(0, 2)  # Move to end for new content
        while True:
            line = file.readline()
            if line:
                yield f"data: {line}\n\n"
            else:
                time.sleep(1)  # Wait for new log entries

# Streaming endpoint for application logs
@app.route('/stream/app')
def stream_app_logs():
    return Response(stream_logs_file(APP_LOG), content_type='text/event-stream')

# Streaming endpoint for monitor logs
@app.route('/stream/mon')
def stream_mon_logs():
    return Response(stream_logs_file(MONITOR_LOG), content_type='text/event-stream')

#########################
#    Edit CBZ Route     #
#########################
@app.route('/edit', methods=['GET'])
def edit_cbz():
    """
    Processes the provided CBZ file (via 'file_path' query parameter) and returns a JSON
    object containing:
      - modal_body: HTML snippet for inline editing,
      - folder_name, zip_file_path, original_file_path for the hidden form fields.
    """
    file_path = request.args.get('file_path')
    if not file_path:
        return jsonify({"error": "Missing file path parameter"}), 400
    try:
        result = get_edit_modal(file_path)  # Reuse existing logic for generating modal content
        return jsonify(result)
    except Exception as e:
        app_logger.error(f"Error in /edit route: {e}")
        return jsonify({"error": str(e)}), 500

# Register the save route using the imported save_cbz function.
app.add_url_rule('/save', view_func=save_cbz, methods=['POST'])

#########################
#    Monitor Process    #
#########################
monitor_process = None  # Track subprocess

def run_monitor():
    global monitor_process
    app_logger.info("Attempting to start monitor.py...")
    
    monitor_process = subprocess.Popen(
        [sys.executable, 'monitor.py'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    stdout, stderr = monitor_process.communicate()
    if stdout:
        app_logger.info(f"monitor.py stdout:\n{stdout}")
    if stderr:
        app_logger.error(f"monitor.py stderr:\n{stderr}")

def cleanup():
    """Terminate monitor.py before shutdown."""
    if monitor_process and monitor_process.poll() is None:
        app_logger.info("Terminating monitor.py process...")
        monitor_process.terminate()
        try:
            monitor_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            app_logger.warning("Monitor did not terminate in time. Force killing...")
            monitor_process.kill()

def shutdown_server():
    app_logger.info("Shutting down Flask...")
    cleanup()
    os._exit(0)

# Handle termination signals
signal.signal(signal.SIGTERM, lambda signum, frame: shutdown_server())
signal.signal(signal.SIGINT, lambda signum, frame: shutdown_server())

@app.route('/api/operations')
def active_operations():
    ops = app_state.get_active_operations()
    return jsonify({"operations": ops})

@app.route('/watch-count')
def watch_count():
    watch_dir = config.get("SETTINGS", "WATCH", fallback="/temp")
    ignored_exts = config.get("SETTINGS", "IGNORED_EXTENSIONS", fallback=".crdownload")
    ignored = set(ext.strip().lower() for ext in ignored_exts.split(",") if ext.strip())

    total = 0
    for root, _, files in os.walk(watch_dir):
        for f in files:
            if f.startswith('.') or f.startswith('_'):
                continue
            if any(f.lower().endswith(ext) for ext in ignored):
                continue
            total += 1
    return jsonify({"total_files": total})

@app.route('/health')
def health_check():
    """Health check endpoint for Docker health check"""
    try:
        # Simple health check - verify app is responding
        return jsonify({
            "status": "healthy",
            "message": "CLU is running",
            "version": "1.0"
        }), 200
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e)
        }), 500

@app.route('/gcd-status')
def gcd_status():
    """Check GCD data status"""
    try:
        gcd_data_dir = "/app/gcd_data"
        metadata_file = os.path.join(gcd_data_dir, "metadata.txt")

        status = {
            "gcd_enabled": os.environ.get('GCD_ENABLED', 'false').lower() == 'true',
            "database_configured": bool(os.environ.get('DATABASE_URL')),
            "metadata_exists": os.path.exists(metadata_file),
            "gcd_data_dir": gcd_data_dir
        }

        if status["metadata_exists"]:
            with open(metadata_file, 'r') as f:
                status["metadata"] = f.read()

        # Check for GCD data files
        if os.path.exists(gcd_data_dir):
            gcd_files = []
            for filename in os.listdir(gcd_data_dir):
                if any(pattern in filename.lower() for pattern in ['gcd', 'comics']):
                    if filename.endswith(('.sql', '.sql.gz', '.zip')):
                        file_path = os.path.join(gcd_data_dir, filename)
                        gcd_files.append({
                            "name": filename,
                            "size": os.path.getsize(file_path),
                            "modified": datetime.fromtimestamp(os.path.getmtime(file_path)).isoformat()
                        })
            status["gcd_files"] = gcd_files

        return jsonify(status)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/gcd-mysql-status')
def gcd_mysql_status():
    """Check if GCD MySQL database is configured"""
    return jsonify(gcd.check_mysql_status())

@app.route('/gcd-import', methods=['POST'])
def trigger_gcd_import():
    """Trigger GCD data import"""
    try:
        import subprocess

        # Run the import script
        result = subprocess.run([
            'python3', '/app/scripts/download_gcd.py', '--import'
        ], capture_output=True, text=True, timeout=3600)  # 1 hour timeout

        return jsonify({
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        })

    except subprocess.TimeoutExpired:
        return jsonify({
            "success": False,
            "error": "Import operation timed out (1 hour limit)"
        }), 408
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/insights')
def insights_page():
    from database import get_reading_totals
    library_stats = get_library_stats()
    file_types = get_file_type_distribution()
    top_publishers = get_top_publishers()
    reading_history = get_reading_history_stats()
    largest_comics = get_largest_comics()
    top_series = get_top_series_by_count()
    reading_heatmap = get_reading_heatmap_data()
    reading_totals = get_reading_totals()

    return render_template('insights.html',
                           library_stats=library_stats,
                           file_types=file_types,
                           top_publishers=top_publishers,
                           reading_history=reading_history,
                           largest_comics=largest_comics,
                           top_series=top_series,
                           reading_heatmap=reading_heatmap,
                           reading_totals=reading_totals,
                           rec_enabled=get_user_preference('rec_enabled', default=True))

@app.route('/api/insights')
def api_insights():
    """Return library stats as JSON for Homepage custom API widget."""
    from database import get_reading_stats_by_year

    library_stats = get_library_stats()
    if not library_stats:
        return jsonify({"error": "Failed to get stats"}), 500

    # Get reading stats (all-time)
    reading_stats = get_reading_stats_by_year(None)
    total_seconds = reading_stats.get('total_time', 0)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60

    return jsonify({
        "total_files": library_stats.get('total_files', 0),
        "total_size": library_stats.get('total_size', 0),
        "issues_read": library_stats.get('total_read', 0),
        "root_folders": library_stats.get('root_folders', 0),
        "pages_read": reading_stats.get('total_pages', 0),
        "time_reading": total_seconds,
        "time_reading_hours": hours,
        "time_reading_minutes": minutes
    })


@app.route('/api/reading-stats')
def api_reading_stats():
    """Get reading statistics, optionally filtered by year."""
    from database import get_reading_stats_by_year
    from wrapped import get_years_with_reading_data

    year = request.args.get('year')

    # If year is 'all' or empty, get all-time stats
    if year and year != 'all':
        try:
            year = int(year)
        except ValueError:
            return jsonify({"success": False, "error": "Invalid year"}), 400
    else:
        year = None

    stats = get_reading_stats_by_year(year)

    # Get available years for the dropdown
    years = get_years_with_reading_data()
    current_year = datetime.now().year
    if current_year not in years:
        years.insert(0, current_year)

    # Calculate hours and minutes for display
    total_seconds = stats.get('total_time', 0)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60

    return jsonify({
        "success": True,
        "stats": {
            "total_read": stats.get('total_read', 0),
            "total_pages": stats.get('total_pages', 0),
            "total_time": total_seconds,
            "hours": hours,
            "minutes": minutes
        },
        "years": years,
        "selected_year": year if year else "all"
    })


#########################
#   Yearly Wrapped      #
#########################

@app.route('/api/wrapped/years')
def api_wrapped_years():
    """Get list of years with reading data. Defaults to current year if none found."""
    from wrapped import get_years_with_reading_data

    try:
        years = get_years_with_reading_data()
    except Exception as e:
        app_logger.error(f"Error getting wrapped years: {e}")
        years = []

    # Default to current year if no data found
    if not years:
        years = [datetime.now().year]

    return jsonify({"years": years})


@app.route('/api/wrapped/<int:year>')
def api_wrapped_data(year):
    """Return wrapped stats as JSON."""
    from wrapped import get_all_wrapped_stats
    try:
        stats = get_all_wrapped_stats(year)
        return jsonify(stats)
    except Exception as e:
        app_logger.error(f"Error getting wrapped stats: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/wrapped/<int:year>/image/<int:slide_num>')
def api_wrapped_image(year, slide_num):
    """Return individual wrapped slide as PNG image."""
    from wrapped import (
        generate_summary_slide,
        generate_most_read_series_slide,
        generate_series_highlights_slide
    )

    # Get current theme from config
    # Get current theme from config
    theme = config.get('SETTINGS', 'BOOTSTRAP_THEME', fallback='default')

    try:
        if slide_num == 1:
            image_bytes = generate_summary_slide(year, theme)
        elif slide_num == 2:
            image_bytes = generate_most_read_series_slide(year, theme)
        elif slide_num == 3:
            image_bytes = generate_series_highlights_slide(year, theme)
        else:
            return jsonify({"error": "Invalid slide number (1-3)"}), 400

        return Response(image_bytes, mimetype='image/png')
    except Exception as e:
        app_logger.error(f"Error generating wrapped image: {e}")
        app_logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/wrapped/<int:year>/download')
def api_wrapped_download(year):
    """Download all wrapped images as ZIP."""
    from wrapped import generate_all_wrapped_images
    import zipfile

    theme = config.get('SETTINGS', 'BOOTSTRAP_THEME', fallback='default')

    try:
        slides = generate_all_wrapped_images(year, theme)

        # Create ZIP in memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for filename, image_bytes in slides:
                zf.writestr(f"wrapped_{year}/{filename}", image_bytes)

        zip_buffer.seek(0)

        return Response(
            zip_buffer.getvalue(),
            mimetype='application/zip',
            headers={
                'Content-Disposition': f'attachment; filename=wrapped_{year}.zip'
            }
        )
    except Exception as e:
        app_logger.error(f"Error generating wrapped ZIP: {e}")
        return jsonify({"error": str(e)}), 500


##########################
#   Monthly Wrapped      #
##########################

@app.route('/api/wrapped/monthly/<int:year>/<int:month>')
def api_monthly_wrapped_data(year, month):
    """Return monthly wrapped stats as JSON."""
    from wrapped import get_monthly_wrapped_stats
    if month < 1 or month > 12:
        return jsonify({"error": "Invalid month (1-12)"}), 400
    try:
        stats = get_monthly_wrapped_stats(year, month)
        return jsonify(stats)
    except Exception as e:
        app_logger.error(f"Error getting monthly wrapped stats: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/wrapped/monthly/<int:year>/<int:month>/image/<int:slide_num>')
def api_monthly_wrapped_image(year, month, slide_num):
    """Return a monthly wrapped slide as PNG image."""
    from wrapped import generate_monthly_recap_slide, generate_monthly_all_issues_slide
    if month < 1 or month > 12:
        return jsonify({"error": "Invalid month (1-12)"}), 400
    if slide_num not in (1, 2):
        return jsonify({"error": "Invalid slide number (1-2)"}), 400

    theme = config.get('SETTINGS', 'BOOTSTRAP_THEME', fallback='default')

    try:
        if slide_num == 1:
            image_bytes = generate_monthly_recap_slide(year, month, theme)
        else:
            image_bytes = generate_monthly_all_issues_slide(year, month, theme)
        return Response(image_bytes, mimetype='image/png')
    except Exception as e:
        app_logger.error(f"Error generating monthly wrapped image: {e}")
        app_logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/wrapped/monthly/<int:year>/<int:month>/download')
def api_monthly_wrapped_download(year, month):
    """Download all monthly wrapped images as ZIP."""
    from wrapped import generate_all_monthly_wrapped, MONTH_NAMES
    import zipfile

    if month < 1 or month > 12:
        return jsonify({"error": "Invalid month (1-12)"}), 400

    theme = config.get('SETTINGS', 'BOOTSTRAP_THEME', fallback='default')

    try:
        slides = generate_all_monthly_wrapped(year, month, theme)
        month_str = str(month).zfill(2)

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for filename, image_bytes in slides:
                zf.writestr(f"monthly_wrapped_{year}_{month_str}/{filename}", image_bytes)

        zip_buffer.seek(0)

        return Response(
            zip_buffer.getvalue(),
            mimetype='application/zip',
            headers={
                'Content-Disposition': f'attachment; filename=monthly_wrapped_{year}_{month_str}.zip'
            }
        )
    except Exception as e:
        app_logger.error(f"Error generating monthly wrapped ZIP: {e}")
        return jsonify({"error": str(e)}), 500


#########################
#   Application Start   #
#########################

# Build search index in background thread
def build_index_background():
    try:
        build_file_index()
        app_logger.info("✅ Search index built successfully and ready for use")
    except Exception as e:
        app_logger.error(f"❌ Error building search index: {e}")

# Cache maintenance background thread
def cache_maintenance_background():
    """Background thread that checks and rebuilds cache every hour."""
    while True:
        try:
            time.sleep(60 * 60)  # Check every hour
            if should_rebuild_cache():
                rebuild_entire_cache()
        except Exception as e:
            app_logger.error(f"Error in cache maintenance thread: {e}")

# Pre-build browse cache for root directory
def prebuild_browse_cache():
    """Pre-build browse cache for DATA_DIR root on startup."""
    try:
        app_logger.info(f"🔄 Pre-building browse cache for {DATA_DIR}...")
        # Trigger a browse request internally to build and cache
        from routes.collection import api_browse
        with app.test_request_context(f'/api/browse?path={DATA_DIR}'):
            api_browse()
        app_logger.info(f"✅ Browse cache pre-built for {DATA_DIR}")
    except Exception as e:
        app_logger.error(f"❌ Error pre-building browse cache: {e}")

# Start file watcher for /data directory in background
def start_file_watcher_background():
    try:
        app_logger.info(f"Initializing file watcher for {DATA_DIR}...")
        file_watcher = FileWatcher(watch_path=DATA_DIR, debounce_seconds=2)
        if file_watcher.start():
            app_logger.info(f"👁️ File watcher started for {DATA_DIR} (tracking recent files)...")
        else:
            app_logger.warning("⚠️ File watcher failed to start")
    except Exception as e:
        app_logger.error(f"❌ Failed to initialize file watcher: {e}")
        app_logger.error(f"Traceback: {traceback.format_exc()}")

def start_metadata_scanner_background():
    """Start metadata scanner after file index is built."""
    global index_built
    try:
        # Wait for file index to be built first
        wait_count = 0
        while not index_built:
            time.sleep(1)
            wait_count += 1
            if wait_count > 300:  # 5 minute timeout
                app_logger.warning("Metadata scanner timed out waiting for file index")
                return

        # Start the scanner
        from metadata_scanner import start_metadata_scanner
        start_metadata_scanner()
    except Exception as e:
        app_logger.error(f"Failed to start metadata scanner: {e}")


def start_background_services():
    """Start all background services. Called once on app startup."""
    app_logger.info("Flask app is starting up...")

    # Start index building in background
    threading.Thread(target=build_index_background, daemon=True).start()
    app_logger.info("🔄 Building search index in background...")

    # Pre-build browse cache in background
    threading.Thread(target=prebuild_browse_cache, daemon=True).start()
    app_logger.info("🔄 Pre-building browse cache for root directory...")

    # Start cache maintenance in background
    threading.Thread(target=cache_maintenance_background, daemon=True).start()
    app_logger.info("🔄 Cache maintenance thread started (checks every hour, rebuilds every 6 hours)...")

    # Start file watcher
    threading.Thread(target=start_file_watcher_background, daemon=True).start()
    app_logger.info("🔄 File watcher initialization started in background...")

    # Start metadata scanner (waits for index to be built)
    threading.Thread(target=start_metadata_scanner_background, daemon=True).start()
    app_logger.info("🔄 Metadata scanner initialization queued (waiting for index)...")

    # Configure rebuild schedule from database
    configure_rebuild_schedule()

    # Configure sync schedule from database
    configure_sync_schedule()

    # Configure GetComics schedule from database
    configure_getcomics_schedule()

    # Configure Weekly Packs schedule from database
    configure_weekly_packs_schedule()

    # Configure Komga reading sync schedule from database
    configure_komga_sync_schedule()

    # Start monitor if enabled
    if os.environ.get("MONITOR", "").strip().lower() == "yes":
        app_logger.info("MONITOR=yes detected. Starting monitor.py...")
        threading.Thread(target=run_monitor, daemon=True).start()

    if pwd is not None:
        user_name = pwd.getpwuid(os.geteuid()).pw_name
    else:
        user_name = os.getenv('USERNAME', 'unknown')
    app_logger.info(f"Running as user: {user_name}")

# Start background services when module is imported (works with Gunicorn)
start_background_services()


#########################
# Komga API Endpoints   #
#########################

@app.route('/api/komga/config', methods=['GET'])
def api_get_komga_config():
    """Get current Komga sync configuration (password masked)."""
    from database import get_komga_config
    cfg = get_komga_config()
    if cfg:
        # Mask password for display
        cfg['password'] = '***' if cfg.get('password') else ''
    return jsonify({"success": True, "config": cfg or {}})


@app.route('/api/komga/config', methods=['POST'])
def api_save_komga_config():
    """Save Komga sync configuration."""
    from database import save_komga_config as db_save_komga_config
    data = request.get_json()

    success = db_save_komga_config(
        server_url=data.get('server_url', ''),
        username=data.get('username', ''),
        password=data.get('password', ''),
        enabled=data.get('enabled', False),
        frequency=data.get('frequency', 'disabled'),
        time=data.get('time', '05:00'),
        weekday=data.get('weekday', 0),
        library_mappings=data.get('library_mappings', [])
    )

    if success:
        configure_komga_sync_schedule()
        return jsonify({"success": True, "message": "Komga configuration saved"})
    return jsonify({"success": False, "error": "Failed to save configuration"}), 500


@app.route('/api/komga/test', methods=['POST'])
def api_test_komga_connection():
    """Test Komga server connectivity."""
    from database import get_komga_config
    from models.komga import KomgaClient

    cfg = get_komga_config()
    if not cfg or not cfg.get('server_url'):
        return jsonify({"success": False, "valid": False, "error": "Komga not configured. Save settings first."})

    username = cfg.get('username', '')
    password = cfg.get('password', '')

    app_logger.info(f"Komga test: url={cfg['server_url']}, user={'set' if username else 'empty'}, pass={'set' if password else 'empty'}")

    if not username or not password:
        return jsonify({"success": False, "valid": False, "error": "Komga credentials not set. Save credentials first."})

    try:
        client = KomgaClient(cfg['server_url'], username, password)
        valid, details = client.test_connection()
        if valid:
            return jsonify({"success": True, "valid": True, "message": details})
        else:
            return jsonify({"success": True, "valid": False, "error": details})
    except Exception as e:
        app_logger.error(f"Komga connection test error: {e}")
        return jsonify({"success": False, "valid": False, "error": str(e)})


@app.route('/api/komga/sync', methods=['POST'])
def api_sync_komga_now():
    """Manually trigger Komga reading sync."""
    threading.Thread(target=run_komga_sync, daemon=True).start()
    return jsonify({"success": True, "message": "Komga sync started in background"})


@app.route('/api/komga/sync/status', methods=['GET'])
def api_komga_sync_status():
    """Get Komga sync status and statistics."""
    from database import get_komga_sync_stats, get_komga_config

    stats = get_komga_sync_stats()
    cfg = get_komga_config()

    # Get next scheduled run
    next_run_str = get_next_run_for_job('komga_sync')
    next_run = next_run_str if next_run_str != "Not scheduled" else None

    return jsonify({
        "success": True,
        "total_synced_read": stats.get('total_synced_read', 0),
        "total_synced_progress": stats.get('total_synced_progress', 0),
        "last_sync": stats.get('last_sync'),
        "last_sync_read_count": cfg.get('last_sync_read_count', 0) if cfg else 0,
        "last_sync_progress_count": cfg.get('last_sync_progress_count', 0) if cfg else 0,
        "next_run": next_run
    })


@app.route('/api/metadata-scan-status', methods=['GET'])
def api_metadata_scan_status():
    """Get current metadata scanning progress and status."""
    try:
        from metadata_scanner import get_scanner_status
        return jsonify(get_scanner_status())
    except ImportError:
        return jsonify({
            'enabled': False,
            'error': 'Metadata scanner not available'
        })
    except Exception as e:
        app_logger.error(f"Error getting metadata scan status: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/metadata-scan-trigger', methods=['POST'])
def api_metadata_scan_trigger():
    """Manually trigger a metadata scan of pending files."""
    try:
        from metadata_scanner import queue_pending_files, get_scanner_status

        queued = queue_pending_files()
        status = get_scanner_status()

        return jsonify({
            'success': True,
            'message': f"Queued {queued} files for scanning",
            'status': status
        })
    except Exception as e:
        app_logger.error(f"Error triggering metadata scan: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/recommendations', methods=['GET', 'POST'])
def api_recommendations():
    try:
        from config import CONFIG_DIR
        recommendations_file = os.path.join(CONFIG_DIR, 'recommendations.json')

        if request.method == 'GET':
            if os.path.exists(recommendations_file):
                try:
                    with open(recommendations_file, 'r') as f:
                        return jsonify(json.load(f))
                except json.JSONDecodeError:
                    return jsonify([])
            return jsonify([])

        data = request.json or {}
        
        # Get settings from request or user preferences DB
        provider = data.get('provider') or get_user_preference('rec_provider', default='gemini')
        api_key = data.get('api_key') or get_user_preference('rec_api_key', default='')
        model = data.get('model') or get_user_preference('rec_model', default='gemini-2.0-flash')
        
        # If no API key in request or config, error
        if not api_key:
             return jsonify({"error": "API Key is required. Please configure it in Settings."}), 400

        # Fetch reading history
        reading_history = get_recent_read_issues(limit=200)
        
        if not reading_history:
             return jsonify({"error": "No reading history found to generate recommendations from."}), 404
             
        # Call recommendations module
        recommendations_list = recommendations.get_recommendations(api_key, provider, model, reading_history)
        
        if isinstance(recommendations_list, dict) and "error" in recommendations_list:
             return jsonify(recommendations_list), 500
        
        # Save recommendations
        try:
             with open(recommendations_file, 'w') as f:
                 json.dump(recommendations_list, f)
        except Exception as e:
             app_logger.error(f"Failed to save recommendations: {e}")
             
        return jsonify(recommendations_list)
        
    except Exception as e:
        app_logger.error(f"Error generating recommendations: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/timeline')
def timeline():
    limit = 50
    year = request.args.get('year', None, type=int)
    month = request.args.get('month', None, type=int)

    data = get_reading_timeline(limit=limit, offset=0, year=year, month=month)

    if not data:
        flash("Error loading timeline data", "error")
        return redirect(url_for('index'))

    # Get distinct years for the filter dropdown
    available_years = []
    try:
        conn = get_db_connection()
        if conn:
            c = conn.cursor()
            c.execute("SELECT DISTINCT strftime('%Y', read_at) FROM issues_read ORDER BY 1 DESC")
            available_years = [row[0] for row in c.fetchall()]
            conn.close()
    except Exception:
        pass

    return render_template('timeline.html',
                          stats=data['stats'],
                          timeline=data['timeline'],
                          filter_year=year,
                          filter_month=month,
                          available_years=available_years)


@app.route('/api/timeline')
def api_timeline():
    """API endpoint for lazy loading timeline data."""
    offset = request.args.get('offset', 0, type=int)
    limit = request.args.get('limit', 50, type=int)
    year = request.args.get('year', None, type=int)
    month = request.args.get('month', None, type=int)

    data = get_reading_timeline(limit=limit, offset=offset, year=year, month=month)

    if not data:
        return jsonify({"error": "Failed to load timeline data"}), 500

    # Add thumbnail URLs to each item
    for group in data['timeline']:
        for item in group['entries']:
            if item.get('issue_path'):
                item['thumbnail_url'] = url_for('get_thumbnail', path=item['issue_path'])
            else:
                item['thumbnail_url'] = None

    return jsonify({
        "timeline": data['timeline'],
        "has_more": len(data['timeline']) > 0,
        "stats": data['stats']
    })


if __name__ == '__main__':
    # Only used for local development (python app.py)
    app.run(debug=False, use_reloader=False, threaded=True, host='0.0.0.0', port=5577)  # nosec B104 - Docker requires binding to all interfaces