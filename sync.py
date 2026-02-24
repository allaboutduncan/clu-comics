#!/usr/bin/env python3
"""
Background sync job for comic-utils series data.

This script syncs all mapped series from the Metron API to the local database.
It can be run via Windows Task Scheduler or cron for periodic updates.

Usage:
    python sync.py              # Sync all stale series (>24h since last sync)
    python sync.py --hours 12   # Sync series not updated in 12 hours
    python sync.py --force      # Force sync all mapped series
    python sync.py --series 123 # Sync a specific series by ID
"""

import argparse
import sys
from datetime import datetime
from app_logging import app_logger
from models import metron
from config import config
from database import (
    init_db, get_series_needing_sync, get_all_mapped_series, get_series_by_id,
    save_issues_bulk, update_series_sync_time, delete_issues_for_series,
    invalidate_collection_status_for_series
)


def get_metron_api():
    """Get Metron API client using credentials from config."""
    metron_username = config.get("METRON", "USERNAME", fallback="").strip()
    metron_password = config.get("METRON", "PASSWORD", fallback="").strip()

    if not metron_username or not metron_password:
        app_logger.error("Metron credentials not configured in config.ini")
        return None

    return metron.get_api(metron_username, metron_password)


def sync_series_from_api(api, series_id: int) -> dict:
    """
    Fetch latest data from Metron API and update the database.

    Args:
        api: Mokkari API client
        series_id: The Metron series ID to sync

    Returns:
        dict with sync result info
    """
    try:
        # Get current mapping info
        series_mapping = get_series_by_id(series_id)
        if not series_mapping:
            return {
                'series_id': series_id,
                'success': False,
                'error': 'Series not found in database'
            }

        # Fetch series info from API
        series_info = api.series(series_id)
        if not series_info:
            return {
                'series_id': series_id,
                'success': False,
                'error': 'Series not found in Metron API'
            }

        # Fetch all issues
        all_issues_result = metron.get_all_issues_for_series(api, series_id)
        all_issues = list(all_issues_result) if all_issues_result else []

        # Delete existing cached issues and save new ones
        delete_issues_for_series(series_id)
        save_issues_bulk(all_issues, series_id)
        update_series_sync_time(series_id, len(all_issues))

        # Invalidate collection status cache to force re-scan with new issue data
        invalidate_collection_status_for_series(series_id)

        series_name = series_mapping.get('name', f'Series {series_id}')
        app_logger.info(f"✓ Synced {series_name}: {len(all_issues)} issues")

        return {
            'series_id': series_id,
            'series_name': series_name,
            'success': True,
            'issue_count': len(all_issues)
        }

    except Exception as e:
        error_msg = 'Metron is currently unavailable' if metron.is_connection_error(e) else str(e)
        log = app_logger.warning if metron.is_connection_error(e) else app_logger.error
        log(f"Error syncing series {series_id}: {e}")
        return {
            'series_id': series_id,
            'success': False,
            'error': error_msg
        }


def sync_all_mapped_series(hours: int = 24, force: bool = False):
    """
    Sync all mapped series that need updating.

    Args:
        hours: Only sync series not updated within this many hours
        force: If True, sync all mapped series regardless of last sync time
    """
    # Initialize database
    init_db()

    # Get Metron API
    api = get_metron_api()
    if not api:
        app_logger.error("Metron API not configured. Check your settings.")
        return False

    # Get series to sync
    if force:
        series_list = get_all_mapped_series()
        app_logger.info(f"Force syncing all {len(series_list)} mapped series")
    else:
        series_list = get_series_needing_sync(hours)
        app_logger.info(f"Found {len(series_list)} series needing sync (stale > {hours}h)")

    if not series_list:
        app_logger.info("No series need syncing")
        return True

    # Sync each series
    results = []
    for series in series_list:
        series_id = series['id']
        result = sync_series_from_api(api, series_id)
        results.append(result)

    # Summary
    success_count = sum(1 for r in results if r['success'])
    fail_count = len(results) - success_count
    total_issues = sum(r.get('issue_count', 0) for r in results if r['success'])

    app_logger.info(f"Sync complete: {success_count} succeeded, {fail_count} failed, {total_issues} total issues cached")

    if fail_count > 0:
        for r in results:
            if not r['success']:
                app_logger.warning(f"  Failed: Series {r['series_id']} - {r.get('error', 'Unknown error')}")

    return fail_count == 0


def sync_single_series(series_id: int):
    """
    Sync a single series by ID.

    Args:
        series_id: The Metron series ID to sync
    """
    # Initialize database
    init_db()

    # Get Metron API
    api = get_metron_api()
    if not api:
        app_logger.error("Metron API not configured. Check your settings.")
        return False

    result = sync_series_from_api(api, series_id)

    if result['success']:
        app_logger.info(f"Successfully synced series {series_id}: {result['issue_count']} issues")
        return True
    else:
        app_logger.error(f"Failed to sync series {series_id}: {result.get('error', 'Unknown error')}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description='Sync comic series data from Metron API',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        '--hours', type=int, default=24,
        help='Sync series not updated within this many hours (default: 24)'
    )
    parser.add_argument(
        '--force', action='store_true',
        help='Force sync all mapped series regardless of last sync time'
    )
    parser.add_argument(
        '--series', type=int,
        help='Sync a specific series by Metron ID'
    )

    args = parser.parse_args()

    app_logger.info(f"Comic-Utils Sync started at {datetime.now().isoformat()}")

    if args.series:
        success = sync_single_series(args.series)
    else:
        success = sync_all_mapped_series(hours=args.hours, force=args.force)

    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
