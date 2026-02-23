import sqlite3
import os
import re
from database import get_db_connection, get_db_path, get_cached_stats, save_cached_stats
from app_logging import app_logger
from config import config

def get_library_stats():
    """
    Get high-level statistics about the library.
    """
    # Check cache first
    cached = get_cached_stats('library_stats')
    if cached:
        return cached

    try:
        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()

        stats = {}

        # Total files and size
        c.execute("SELECT COUNT(*), SUM(size) FROM file_index WHERE type = 'file'")
        row = c.fetchone()
        stats['total_files'] = row[0] or 0
        stats['total_size'] = row[1] or 0

        # Total directories
        c.execute("SELECT COUNT(*) FROM file_index WHERE type = 'directory'")
        stats['total_directories'] = c.fetchone()[0] or 0

        # Root folders (publishers - top-level directories under /data)
        c.execute("SELECT COUNT(*) FROM file_index WHERE type = 'directory' AND parent = '/data'")
        stats['root_folders'] = c.fetchone()[0] or 0

        # Total read issues
        c.execute("SELECT COUNT(*) FROM issues_read")
        stats['total_read'] = c.fetchone()[0] or 0

        # Total to-read
        c.execute("SELECT COUNT(*) FROM to_read")
        stats['total_to_read'] = c.fetchone()[0] or 0

        conn.close()

        # Save to cache
        save_cached_stats('library_stats', stats)

        return stats
    except Exception as e:
        app_logger.error(f"Error getting library stats: {e}")
        return None

def get_file_type_distribution():
    """
    Get the distribution of file types in the library.
    """
    # Check cache first
    cached = get_cached_stats('file_type_distribution')
    if cached:
        return cached

    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()

        # We need to extract extension from path since type is just 'file'
        # SQLite doesn't have great string manipulation, but we can try
        # This is a bit expensive, might need optimization for huge libraries
        c.execute("SELECT path FROM file_index WHERE type = 'file'")
        rows = c.fetchall()

        extensions = {}
        for row in rows:
            path = row['path']
            ext = os.path.splitext(path)[1].lower().replace('.', '')
            if not ext:
                ext = 'unknown'
            extensions[ext] = extensions.get(ext, 0) + 1

        # Convert to list for chart
        data = [{'type': k, 'count': v} for k, v in extensions.items()]
        data.sort(key=lambda x: x['count'], reverse=True)

        conn.close()

        # Save to cache
        save_cached_stats('file_type_distribution', data)

        return data
    except Exception as e:
        app_logger.error(f"Error getting file type distribution: {e}")
        return []

def get_top_publishers(limit=10):
    """
    Get the top publishers (root folders) by file count.
    """
    # Check cache first
    cached = get_cached_stats('top_publishers')
    if cached:
        return cached[:limit]

    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()

        # This assumes publishers are the top-level folders in /data
        # We look for files whose parent starts with /data/PublisherName

        # First get all top level directories
        c.execute("SELECT path, name FROM file_index WHERE parent = '/data' AND type = 'directory'")
        publishers = c.fetchall()

        publisher_stats = []

        for pub in publishers:
            pub_path = pub['path']
            pub_name = pub['name']

            # Count files recursively
            # We can use the existing get_path_counts logic or do it here
            c.execute("SELECT COUNT(*) FROM file_index WHERE path LIKE ? AND type = 'file'", (f"{pub_path}%",))
            count = c.fetchone()[0]

            if count > 0:
                publisher_stats.append({'name': pub_name, 'count': count})

        publisher_stats.sort(key=lambda x: x['count'], reverse=True)

        conn.close()

        # Save to cache (full list, limit applied on return)
        save_cached_stats('top_publishers', publisher_stats)

        return publisher_stats[:limit]
    except Exception as e:
        app_logger.error(f"Error getting top publishers: {e}")
        return []

def get_reading_history_stats():
    """
    Get reading history statistics grouped by day (MM-DD-YYYY format).
    Returns daily read counts for the last 3 months (~90 days).
    Applies timezone offset from config settings.
    """
    # Get timezone offset from config
    tz_offset = config['SETTINGS'].get('TIMEZONE', 'UTC') if 'SETTINGS' in config else 'UTC'

    # Build offset string for SQLite datetime()
    if tz_offset == 'UTC':
        offset_str = '+0 hours'
    else:
        try:
            hours = float(tz_offset)
            sign = '+' if hours >= 0 else ''
            offset_str = f'{sign}{hours} hours'
        except (ValueError, TypeError):
            offset_str = '+0 hours'

    # Validate offset_str format to prevent SQL injection
    if not re.match(r'^[+-]\d+(\.\d+)? hours$', offset_str):
        offset_str = '+0 hours'

    # Check cache first (include timezone in cache key)
    cache_key = f'reading_history_{tz_offset}'
    cached = get_cached_stats(cache_key)
    if cached:
        return cached

    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()

        # Extract date from read_at timestamp in MM-DD-YYYY format, applying timezone offset
        # offset_str is validated above to match the pattern [+-]N[.N] hours
        c.execute(
            "SELECT strftime('%m-%d-%Y', datetime(read_at, '" + offset_str + "')) as date, COUNT(*) as count"
            " FROM issues_read"
            " GROUP BY date"
            " ORDER BY datetime(read_at, '" + offset_str + "') DESC"
            " LIMIT 90"
        )

        rows = c.fetchall()

        history = [{'date': row['date'], 'count': row['count']} for row in rows]
        # Reverse to show chronological order for charts
        history.reverse()

        conn.close()

        # Save to cache (with timezone-specific key)
        save_cached_stats(cache_key, history)

        return history
    except Exception as e:
        app_logger.error(f"Error getting reading history: {e}")
        return []

def get_largest_comics(limit=20):
    """
    Get the largest comic files by size.

    Args:
        limit: Maximum number of results to return

    Returns:
        List of dicts with name and size
    """
    # Check cache first
    cached = get_cached_stats('largest_comics')
    if cached:
        return cached[:limit]

    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()

        # Get files with comic extensions, ordered by size
        c.execute("""
            SELECT name, path, size FROM file_index
            WHERE type = 'file'
            AND (LOWER(path) LIKE '%.cbz' OR LOWER(path) LIKE '%.cbr'
                 OR LOWER(path) LIKE '%.cb7' OR LOWER(path) LIKE '%.pdf')
            ORDER BY size DESC
            LIMIT ?
        """, (limit,))

        rows = c.fetchall()
        data = [{'name': row['name'], 'size': row['size'] or 0} for row in rows]

        conn.close()

        # Save to cache
        save_cached_stats('largest_comics', data)

        return data
    except Exception as e:
        app_logger.error(f"Error getting largest comics: {e}")
        return []

def get_top_series_by_count(limit=20):
    """
    Get folders (series) with the most files.

    Args:
        limit: Maximum number of results to return

    Returns:
        List of dicts with name, count, and path
    """
    # Check cache first
    cached = get_cached_stats('top_series_by_count')
    if cached:
        return cached[:limit]

    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()

        # Count files per parent directory (excluding root /data)
        c.execute("""
            SELECT parent, COUNT(*) as file_count
            FROM file_index
            WHERE type = 'file' AND parent != '/data'
            GROUP BY parent
            ORDER BY file_count DESC
            LIMIT ?
        """, (limit,))

        rows = c.fetchall()

        # Extract folder name from path
        # If folder looks like a volume (v1938, v2011), combine with parent folder
        import re
        data = []
        for row in rows:
            parent_path = row['parent']
            parts = parent_path.split('/') if parent_path else []
            folder_name = parts[-1] if parts else 'Unknown'

            # Check if folder name is a volume indicator (v followed by 4 digits)
            if len(parts) >= 2 and re.match(r'^v\d{4}$', folder_name, re.IGNORECASE):
                # Combine parent folder with volume: "Action Comics v1938"
                series_name = parts[-2]
                folder_name = f"{series_name} {folder_name}"

            data.append({
                'name': folder_name,
                'count': row['file_count'],
                'path': parent_path
            })

        conn.close()

        # Save to cache
        save_cached_stats('top_series_by_count', data)

        return data
    except Exception as e:
        app_logger.error(f"Error getting top series by count: {e}")
        return []

def get_reading_heatmap_data():
    """
    Get reading counts grouped by year and month for heatmap display.
    Returns dict: { "2024": [jan_count, feb_count, ..., dec_count], ... }
    """
    cached = get_cached_stats('reading_heatmap')
    if cached:
        return cached

    try:
        conn = get_db_connection()
        if not conn:
            return {}

        c = conn.cursor()

        # Get counts grouped by year and month
        c.execute("""
            SELECT strftime('%Y', read_at) as year,
                   strftime('%m', read_at) as month,
                   COUNT(*) as count
            FROM issues_read
            GROUP BY year, month
            ORDER BY year, month
        """)

        rows = c.fetchall()
        conn.close()

        # Build nested dict: { year: [12 month counts] }
        heatmap = {}
        for row in rows:
            year = row['year']
            month = int(row['month']) - 1  # 0-indexed
            count = row['count']

            if year not in heatmap:
                heatmap[year] = [0] * 12
            heatmap[year][month] = count

        # Ensure all years from min to current are included
        if heatmap:
            min_year = int(min(heatmap.keys()))
            max_year = int(max(heatmap.keys()))
            for y in range(min_year, max_year + 1):
                if str(y) not in heatmap:
                    heatmap[str(y)] = [0] * 12

        save_cached_stats('reading_heatmap', heatmap)
        return heatmap

    except Exception as e:
        app_logger.error(f"Error getting reading heatmap: {e}")
        return {}
