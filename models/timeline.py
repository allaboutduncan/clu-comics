import sqlite3
import os
from core.database import get_db_connection, get_user_preference, current_user_id
from core.app_logging import app_logger
from datetime import datetime, timedelta

def get_reading_timeline(limit=100, offset=0, year=None, month=None, user_id=None):
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

        # Build WHERE clauses. user_id first so it applies to every derived query
        # (detailed list, totals, top-publisher, series, streak).
        # The year/month filters use the user's local calendar, the same one
        # the entries below are grouped and labelled by.
        from core.user_time import localise_read_at_sql

        uid = current_user_id() if user_id is None else user_id
        where_clauses = ["r.user_id = ?", "r.hide = 0"]
        params = [uid]
        if year is not None:
            where_clauses.append(localise_read_at_sql("strftime('%Y', r.read_at) = ?"))
            params.append(str(year))
        if month is not None:
            where_clauses.append(localise_read_at_sql("strftime('%m', r.read_at) = ?"))
            params.append(str(month).zfill(2))
        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        # Bare WHERE (for queries without r. alias)
        bare_clauses = [clause.replace("r.", "") for clause in where_clauses]
        bare_where = ("WHERE " + " AND ".join(bare_clauses)) if bare_clauses else ""

        # 1. Get detailed reading history
        # Join issues_read with collection_status -> issues -> series for metadata
        # We use LEFT JOINs because some read files might not have metadata matches
        # Use COALESCE to fall back to issues_read metadata when joins return NULL
        # where_sql/bare_where contain only hardcoded strftime clauses with ? placeholders
        query = (  # nosec B608
            'SELECT'
            ' r.issue_path, r.read_at, r.time_spent,'
            ' s.name as series_name, s.volume_year,'
            ' i.number as issue_number, i.image as cover_image,'
            ' COALESCE(p.name, r.publisher) as publisher_name,'
            ' i.id as issue_id, r.writer, r.penciller'
            ' FROM issues_read r'
            ' LEFT JOIN collection_status cs ON cs.file_path = r.issue_path'
            ' LEFT JOIN issues i ON i.id = cs.issue_id'
            ' LEFT JOIN series s ON s.id = cs.series_id'
            ' LEFT JOIN publishers p ON p.id = s.publisher_id '
            + where_sql +
            ' ORDER BY r.read_at DESC'
            ' LIMIT ? OFFSET ?'
        )

        c.execute(query, params + [limit, offset])
        rows = c.fetchall()
        
        # 2. Get Statistics
        stats = {}
        
        # Total Read
        c.execute('SELECT COUNT(*) FROM issues_read ' + bare_where, params)  # nosec B608
        stats['total_read'] = c.fetchone()[0]

        # Top Publisher
        # This assumes matched issues. Unmatched ones won't count towards this.
        c.execute(  # nosec B608
            'SELECT p.name, COUNT(*) as count'
            ' FROM issues_read r'
            ' JOIN collection_status cs ON cs.file_path = r.issue_path'
            ' JOIN series s ON s.id = cs.series_id'
            ' JOIN publishers p ON p.id = s.publisher_id '
            + where_sql +
            ' GROUP BY p.name'
            ' ORDER BY count DESC'
            ' LIMIT 1',
            params
        )
        top_pub_row = c.fetchone()
        stats['top_publisher'] = top_pub_row[0] if top_pub_row else "N/A"

        # Total Series
        c.execute(  # nosec B608
            'SELECT COUNT(DISTINCT cs.series_id)'
            ' FROM issues_read r'
            ' JOIN collection_status cs ON cs.file_path = r.issue_path '
            + where_sql,
            params
        )
        stats['total_series'] = c.fetchone()[0]

        # Streak: consecutive local days with at least one read. The days and
        # "today" are both the user's -- it used to compare UTC dates against
        # the server's date.today(), so near midnight a streak could read as
        # broken (or a day long) depending on where the server was.
        from core.user_time import local_date, user_today
        from models.reading_insights import compute_streaks

        c.execute(  # nosec B608 - bare_where built from hardcoded clauses with ? placeholders
            'SELECT read_at FROM issues_read ' + bare_where,
            params
        )
        read_days = {local_date(row[0]) for row in c.fetchall()}
        read_days.discard(None)
        streak = compute_streaks(read_days, user_today())['current']

        stats['streak'] = streak
        
        conn.close()

        # 3. Process and Group Data
        # One offset parser for the whole app -- see core/user_time.py.
        from core.user_time import user_offset_hours

        tz_hours = user_offset_hours()

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
