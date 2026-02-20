import sqlite3
import os
from database import get_db_connection
from app_logging import app_logger
from datetime import datetime, timedelta
from config import config

def get_reading_timeline(limit=100, offset=0, year=None, month=None):
    """
    Get reading history with full metadata for the timeline view.
    Groups results by date. Optionally filters by year and/or month.

    Returns:
        dict: {
            'stats': {'total_read': int, 'streak': int, 'top_publisher': str, 'total_series': int},
            'timeline': [
                {
                    'date': 'DEC 20, 2025',
                    'entries': [
                        {
                            'title': 'Something Is Killing the Children',
                            'issue_number': '031',
                            'year': '2019',
                            'publisher': 'BOOM! Studios',
                            'read_at': '02:30 PM',
                            'cover': '/path/to/cover.jpg',
                            'id': 123
                        },
                        ...
                    ]
                },
                ...
            ]
        }
    """
    try:
        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()

        # Build WHERE clauses for optional year/month filtering
        where_clauses = []
        params = []
        if year is not None:
            where_clauses.append("strftime('%Y', r.read_at) = ?")
            params.append(str(year))
        if month is not None:
            where_clauses.append("strftime('%m', r.read_at) = ?")
            params.append(str(month).zfill(2))
        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        # Bare WHERE (for queries without r. alias)
        bare_clauses = [clause.replace("r.", "") for clause in where_clauses]
        bare_where = ("WHERE " + " AND ".join(bare_clauses)) if bare_clauses else ""

        # 1. Get detailed reading history
        # Join issues_read with collection_status -> issues -> series for metadata
        # We use LEFT JOINs because some read files might not have metadata matches
        # Use COALESCE to fall back to issues_read metadata when joins return NULL
        query = f"""
            SELECT
                r.issue_path,
                r.read_at,
                r.time_spent,
                s.name as series_name,
                s.volume_year,
                i.number as issue_number,
                i.image as cover_image,
                COALESCE(p.name, r.publisher) as publisher_name,
                i.id as issue_id,
                r.writer,
                r.penciller
            FROM issues_read r
            LEFT JOIN collection_status cs ON cs.file_path = r.issue_path
            LEFT JOIN issues i ON i.id = cs.issue_id
            LEFT JOIN series s ON s.id = cs.series_id
            LEFT JOIN publishers p ON p.id = s.publisher_id
            {where_sql}
            ORDER BY r.read_at DESC
            LIMIT ? OFFSET ?
        """

        c.execute(query, params + [limit, offset])
        rows = c.fetchall()
        
        # 2. Get Statistics
        stats = {}
        
        # Total Read
        c.execute(f"SELECT COUNT(*) FROM issues_read {bare_where}", params)
        stats['total_read'] = c.fetchone()[0]

        # Top Publisher
        # This assumes matched issues. Unmatched ones won't count towards this.
        c.execute(f"""
            SELECT p.name, COUNT(*) as count
            FROM issues_read r
            JOIN collection_status cs ON cs.file_path = r.issue_path
            JOIN series s ON s.id = cs.series_id
            JOIN publishers p ON p.id = s.publisher_id
            {where_sql}
            GROUP BY p.name
            ORDER BY count DESC
            LIMIT 1
        """, params)
        top_pub_row = c.fetchone()
        stats['top_publisher'] = top_pub_row[0] if top_pub_row else "N/A"

        # Total Series
        c.execute(f"""
            SELECT COUNT(DISTINCT cs.series_id)
            FROM issues_read r
            JOIN collection_status cs ON cs.file_path = r.issue_path
            {where_sql}
        """, params)
        stats['total_series'] = c.fetchone()[0]

        # Calculate Streak (consecutive days with at least one read)
        c.execute(f"""
            SELECT date(read_at) as read_date
            FROM issues_read
            {bare_where}
            GROUP BY read_date
            ORDER BY read_date DESC
        """, params)
        dates = [row[0] for row in c.fetchall()]
        
        streak = 0
        if dates:
            from datetime import date, timedelta
            today = date.today()
            current_check = today
            
            # Check if we read something today
            if dates[0] == today.isoformat():
                streak = 1
                dates.pop(0)
                current_check -= timedelta(days=1)
            elif dates[0] == (today - timedelta(days=1)).isoformat():
                # Or if we read something yesterday (streak is still alive)
                current_check -= timedelta(days=1)
            else:
                # Streak broken or not started recently
                streak = 0
                
            # Count backwards
            for d in dates:
                if d == current_check.isoformat():
                    streak += 1
                    current_check -= timedelta(days=1)
                else:
                    break
                    
        stats['streak'] = streak
        
        conn.close()

        # 3. Process and Group Data
        # Get timezone offset from config
        tz_offset = config['SETTINGS'].get('TIMEZONE', 'UTC') if 'SETTINGS' in config else 'UTC'
        tz_hours = 0
        if tz_offset != 'UTC':
            try:
                tz_hours = float(tz_offset)
            except (ValueError, TypeError):
                tz_hours = 0

        timeline = []
        current_date_group = None
        current_entries = []

        for row in rows:
            # Parse timestamp and apply timezone offset
            try:
                dt = datetime.fromisoformat(row['read_at']) if row['read_at'] else datetime.now()
                if tz_hours != 0:
                    dt = dt + timedelta(hours=tz_hours)
            except ValueError:
                 # Handle potential non-iso formats if any legacy data exists
                 dt = datetime.now()

            # Format: "DEC 20, 2025"
            date_str = dt.strftime("%b %d, %Y").upper()
            
            # Check if we need a new group
            if current_date_group != date_str:
                if current_date_group:
                    timeline.append({
                        'date': current_date_group,
                        'entries': current_entries
                    })
                current_date_group = date_str
                current_entries = []
            
            # Process Item
            # Fallback for unmatched files
            series_name = row['series_name']
            if not series_name:
                # Try to extract from filename
                filename = os.path.basename(row['issue_path'])
                series_name = os.path.splitext(filename)[0]
                
            item = {
                'title': series_name,
                'year': f"({row['volume_year']})" if row['volume_year'] else "",
                'full_title': f"{series_name} ({row['volume_year']})" if row['volume_year'] else series_name,
                'issue_number': row['issue_number'] if row['issue_number'] else "",
                'publisher': row['publisher_name'] or "Unknown Publisher",
                'read_time': dt.strftime("%I:%M %p"), # "02:30 PM"
                'issue_path': row['issue_path'],  # For local thumbnail generation
                'id': row['issue_id']
            }
            
            # If issue_number is present in series_name (fallback), try to clean it? 
            # For now, trust the DB triggers or existing logic.
            
            current_entries.append(item)
            
        # Append last group
        if current_date_group:
            timeline.append({
                'date': current_date_group,
                'entries': current_entries
            })
            
        return {
            'stats': stats,
            'timeline': timeline
        }

    except Exception as e:
        app_logger.error(f"Error getting reading timeline: {e}")
        return None
