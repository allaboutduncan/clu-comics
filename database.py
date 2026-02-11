import sqlite3
import os
import re
import hashlib
import zipfile
from datetime import datetime
from typing import Optional
from config import config
from app_logging import app_logger

def get_db_path():
    # Ensure we get the latest config value
    cache_dir = config.get("SETTINGS", "CACHE_DIR", fallback="/cache")
    if not os.path.exists(cache_dir):
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError as e:
            app_logger.error(f"Failed to create cache directory {cache_dir}: {e}")
            # Fallback to a local directory if /cache is not writable (e.g. running locally without docker)
            cache_dir = "cache"
            os.makedirs(cache_dir, exist_ok=True)
            
    return os.path.join(cache_dir, "comic_utils.db")

def init_db():
    """Initialize the SQLite database and create tables if they don't exist."""
    try:
        db_path = get_db_path()
        app_logger.info(f"Initializing database at {db_path}")

        conn = sqlite3.connect(db_path, timeout=30)
        c = conn.cursor()

        # Enable WAL mode for better concurrency (allows reads during writes)
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA busy_timeout=30000')
        # Enable foreign key enforcement for ON DELETE CASCADE
        c.execute('PRAGMA foreign_keys=ON')
        
        # Create thumbnail_jobs table
        c.execute('''
            CREATE TABLE IF NOT EXISTS thumbnail_jobs (
                path TEXT PRIMARY KEY,
                status TEXT,
                file_mtime REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create recent_files table (rotating log of last 100 files added to /data)
        c.execute('''
            CREATE TABLE IF NOT EXISTS recent_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_size INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create file_index table (persistent file index for fast search)
        c.execute('''
            CREATE TABLE IF NOT EXISTS file_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,
                size INTEGER,
                parent TEXT,
                has_thumbnail INTEGER DEFAULT 0,
                modified_at REAL,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Migration: Add has_thumbnail column if it doesn't exist (for existing databases)
        c.execute("PRAGMA table_info(file_index)")
        columns = [col[1] for col in c.fetchall()]
        if 'has_thumbnail' not in columns:
            c.execute('ALTER TABLE file_index ADD COLUMN has_thumbnail INTEGER DEFAULT 0')

        # Migration: Add modified_at column if it doesn't exist
        if 'modified_at' not in columns:
            c.execute('ALTER TABLE file_index ADD COLUMN modified_at REAL')

        # Migration: Add ComicInfo.xml metadata columns for file_index
        metadata_columns = [
            'ci_title',        # Title field from ComicInfo
            'ci_series',       # Series name
            'ci_number',       # Issue number
            'ci_count',        # Total issues in series
            'ci_volume',       # Volume number
            'ci_year',         # Publication year
            'ci_writer',       # Writer(s) - comma-separated
            'ci_penciller',    # Penciller(s) - comma-separated
            'ci_inker',        # Inker(s) - comma-separated
            'ci_colorist',     # Colorist(s) - comma-separated
            'ci_letterer',     # Letterer(s) - comma-separated
            'ci_coverartist',  # Cover artist(s) - comma-separated
            'ci_publisher',    # Publisher name
            'ci_genre',        # Genre(s) - comma-separated
            'ci_characters',   # Characters - comma-separated
            'metadata_scanned_at'  # Timestamp of last scan (REAL)
        ]
        for col in metadata_columns:
            if col not in columns:
                col_type = 'REAL' if col == 'metadata_scanned_at' else 'TEXT'
                c.execute(f'ALTER TABLE file_index ADD COLUMN {col} {col_type}')
                app_logger.info(f"Migrating file_index: adding {col} column")

        # Migration: Add first_indexed_at column to track when files were first added
        if 'first_indexed_at' not in columns:
            c.execute('ALTER TABLE file_index ADD COLUMN first_indexed_at REAL')
            # Backfill existing records with modified_at as a fallback
            c.execute('UPDATE file_index SET first_indexed_at = modified_at WHERE first_indexed_at IS NULL')
            app_logger.info("Migrating file_index: adding first_indexed_at column")

        # Create indexes for file_index table
        c.execute('CREATE INDEX IF NOT EXISTS idx_file_index_name ON file_index(name)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_file_index_parent ON file_index(parent)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_file_index_type ON file_index(type)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_file_index_path ON file_index(path)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_file_index_metadata_scan ON file_index(metadata_scanned_at, modified_at)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_file_index_characters ON file_index(ci_characters)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_file_index_writer ON file_index(ci_writer)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_file_index_first_indexed ON file_index(first_indexed_at)')

        # Create rebuild_schedule table (store file index rebuild schedule)
        c.execute('''
            CREATE TABLE IF NOT EXISTS rebuild_schedule (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                frequency TEXT NOT NULL DEFAULT 'disabled',
                time TEXT NOT NULL DEFAULT '02:00',
                weekday INTEGER DEFAULT 0,
                last_rebuild TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Insert default schedule if not exists
        c.execute('SELECT COUNT(*) FROM rebuild_schedule WHERE id = 1')
        if c.fetchone()[0] == 0:
            c.execute('''
                INSERT INTO rebuild_schedule (id, frequency, time, weekday)
                VALUES (1, 'disabled', '02:00', 0)
            ''')

        # Create sync_schedule table (store series sync schedule)
        c.execute('''
            CREATE TABLE IF NOT EXISTS sync_schedule (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                frequency TEXT NOT NULL DEFAULT 'disabled',
                time TEXT NOT NULL DEFAULT '03:00',
                weekday INTEGER DEFAULT 0,
                last_sync TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Insert default sync schedule if not exists
        c.execute('SELECT COUNT(*) FROM sync_schedule WHERE id = 1')
        if c.fetchone()[0] == 0:
            c.execute('''
                INSERT INTO sync_schedule (id, frequency, time, weekday)
                VALUES (1, 'disabled', '03:00', 0)
            ''')

        # Create GetComics auto-download schedule table
        c.execute('''
            CREATE TABLE IF NOT EXISTS getcomics_schedule (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                frequency TEXT NOT NULL DEFAULT 'disabled',
                time TEXT NOT NULL DEFAULT '03:00',
                weekday INTEGER DEFAULT 0,
                last_run TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Insert default getcomics schedule if not exists
        c.execute('SELECT COUNT(*) FROM getcomics_schedule WHERE id = 1')
        if c.fetchone()[0] == 0:
            c.execute('''
                INSERT INTO getcomics_schedule (id, frequency, time, weekday)
                VALUES (1, 'disabled', '03:00', 0)
            ''')

        # Create Weekly Packs configuration table
        c.execute('''
            CREATE TABLE IF NOT EXISTS weekly_packs_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER DEFAULT 0,
                format TEXT NOT NULL DEFAULT 'JPG',
                publishers TEXT NOT NULL DEFAULT '[]',
                weekday INTEGER DEFAULT 2,
                time TEXT NOT NULL DEFAULT '10:00',
                retry_enabled INTEGER DEFAULT 1,
                start_date TEXT,
                last_run TIMESTAMP,
                last_successful_pack TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Migration: Add start_date column if it doesn't exist (for existing installations)
        try:
            c.execute('ALTER TABLE weekly_packs_config ADD COLUMN start_date TEXT')
        except Exception:
            pass  # Column already exists

        # Insert default weekly packs config if not exists
        c.execute('SELECT COUNT(*) FROM weekly_packs_config WHERE id = 1')
        if c.fetchone()[0] == 0:
            c.execute('''
                INSERT INTO weekly_packs_config (id, enabled, format, publishers, weekday, time, retry_enabled)
                VALUES (1, 0, 'JPG', '[]', 2, '10:00', 1)
            ''')

        # Create Weekly Packs download history table
        c.execute('''
            CREATE TABLE IF NOT EXISTS weekly_packs_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pack_date TEXT NOT NULL,
                publisher TEXT NOT NULL,
                format TEXT NOT NULL,
                download_url TEXT,
                status TEXT DEFAULT 'queued',
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(pack_date, publisher, format)
            )
        ''')

        # Create wanted_issues table (cache pre-computed wanted issues)
        c.execute('''
            CREATE TABLE IF NOT EXISTS wanted_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                issue_id INTEGER NOT NULL,
                issue_number TEXT,
                issue_name TEXT,
                store_date TEXT,
                cover_date TEXT,
                image TEXT,
                series_name TEXT,
                series_volume INTEGER,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(series_id, issue_id)
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_wanted_issues_series ON wanted_issues(series_id)')

        # Create browse_cache table (cache pre-computed browse results)
        c.execute('''
            CREATE TABLE IF NOT EXISTS browse_cache (
                path TEXT PRIMARY KEY,
                result TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create index for faster lookups
        c.execute('CREATE INDEX IF NOT EXISTS idx_browse_cache_path ON browse_cache(path)')

        # Note: favorite_publishers table has been merged into publishers table
        # with path and favorite columns. Drop if exists for clean migration.
        c.execute('DROP TABLE IF EXISTS favorite_publishers')

        # Create favorite_series table (folders within publishers)
        c.execute('''
            CREATE TABLE IF NOT EXISTS favorite_series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_path TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_favorite_series_path ON favorite_series(series_path)')

        # Create reading_lists table
        c.execute('''
            CREATE TABLE IF NOT EXISTS reading_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source TEXT,
                thumbnail_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Migrate reading_lists table: add thumbnail_path if not exists
        c.execute("PRAGMA table_info(reading_lists)")
        columns = [row[1] for row in c.fetchall()]
        if 'thumbnail_path' not in columns:
            app_logger.info("Migrating reading_lists table: adding thumbnail_path column")
            c.execute("ALTER TABLE reading_lists ADD COLUMN thumbnail_path TEXT")
        if 'tags' not in columns:
            app_logger.info("Migrating reading_lists table: adding tags column")
            c.execute("ALTER TABLE reading_lists ADD COLUMN tags TEXT DEFAULT '[]'")

        # Create reading_list_entries table
        c.execute('''
            CREATE TABLE IF NOT EXISTS reading_list_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reading_list_id INTEGER NOT NULL,
                series TEXT,
                issue_number TEXT,
                volume INTEGER,
                year INTEGER,
                matched_file_path TEXT,
                manual_override_path TEXT,
                FOREIGN KEY (reading_list_id) REFERENCES reading_lists (id) ON DELETE CASCADE
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_reading_list_entries_list_id ON reading_list_entries(reading_list_id)')

        # Create issues_read table (comic files marked as read)
        c.execute('''
            CREATE TABLE IF NOT EXISTS issues_read (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_path TEXT NOT NULL UNIQUE,
                read_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                page_count INTEGER DEFAULT 0,
                time_spent INTEGER DEFAULT 0
            )
        ''')
        
        # Check if we need to migrate issues_read table
        c.execute("PRAGMA table_info(issues_read)")
        columns = [row[1] for row in c.fetchall()]
        if 'page_count' not in columns:
            app_logger.info("Migrating issues_read table: adding page_count column")
            c.execute("ALTER TABLE issues_read ADD COLUMN page_count INTEGER DEFAULT 0")
        if 'time_spent' not in columns:
            app_logger.info("Migrating issues_read table: adding time_spent column")
            c.execute("ALTER TABLE issues_read ADD COLUMN time_spent INTEGER DEFAULT 0")
        # Add metadata columns for reading trends
        if 'writer' not in columns:
            app_logger.info("Migrating issues_read table: adding writer column")
            c.execute("ALTER TABLE issues_read ADD COLUMN writer TEXT DEFAULT ''")
        if 'penciller' not in columns:
            app_logger.info("Migrating issues_read table: adding penciller column")
            c.execute("ALTER TABLE issues_read ADD COLUMN penciller TEXT DEFAULT ''")
        if 'characters' not in columns:
            app_logger.info("Migrating issues_read table: adding characters column")
            c.execute("ALTER TABLE issues_read ADD COLUMN characters TEXT DEFAULT ''")
        if 'publisher' not in columns:
            app_logger.info("Migrating issues_read table: adding publisher column")
            c.execute("ALTER TABLE issues_read ADD COLUMN publisher TEXT DEFAULT ''")
        c.execute('CREATE INDEX IF NOT EXISTS idx_issues_read_path ON issues_read(issue_path)')

        # Create to_read table (files and folders marked as "want to read")
        c.execute('''
            CREATE TABLE IF NOT EXISTS to_read (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_to_read_path ON to_read(path)')

        # Create stats_cache table (cache computed statistics)
        c.execute('''
            CREATE TABLE IF NOT EXISTS stats_cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create user_preferences table (key-value store for user settings)
        c.execute('''
            CREATE TABLE IF NOT EXISTS user_preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create reading_positions table (save reading position for comics)
        c.execute('''
            CREATE TABLE IF NOT EXISTS reading_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                comic_path TEXT NOT NULL UNIQUE,
                page_number INTEGER NOT NULL,
                total_pages INTEGER,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                time_spent INTEGER DEFAULT 0
            )
        ''')

        # Check if we need to migrate reading_positions table
        c.execute("PRAGMA table_info(reading_positions)")
        columns = [row[1] for row in c.fetchall()]
        if 'time_spent' not in columns:
            app_logger.info("Migrating reading_positions table: adding time_spent column")
            c.execute("ALTER TABLE reading_positions ADD COLUMN time_spent INTEGER DEFAULT 0")
        c.execute('CREATE INDEX IF NOT EXISTS idx_reading_positions_path ON reading_positions(comic_path)')

        # Migration: Check if file_mtime column exists, add if not
        c.execute("PRAGMA table_info(thumbnail_jobs)")
        columns = [column[1] for column in c.fetchall()]
        if 'file_mtime' not in columns:
            app_logger.info("Migrating database: adding file_mtime column")
            c.execute("ALTER TABLE thumbnail_jobs ADD COLUMN file_mtime REAL")

        # Migration: Drop file_move_history table if it exists (removed feature)
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='file_move_history'")
        if c.fetchone():
            app_logger.info("Migrating database: dropping file_move_history table (removed feature)")
            c.execute("DROP TABLE file_move_history")

        # Clean up orphaned reading list entries from previous deletes
        c.execute('''
            DELETE FROM reading_list_entries
            WHERE reading_list_id NOT IN (SELECT id FROM reading_lists)
        ''')
        if c.rowcount > 0:
            app_logger.info(f"Cleaned up {c.rowcount} orphaned reading list entries")

        # Create publishers table (Metron publishers with optional local path mapping)
        c.execute('''
            CREATE TABLE IF NOT EXISTS publishers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                path TEXT,
                favorite INTEGER DEFAULT 0,
                logo TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Migration: Add path, favorite, and logo columns to existing publishers table
        try:
            c.execute("PRAGMA table_info(publishers)")
            columns = [row[1] for row in c.fetchall()]
            app_logger.info(f"Publishers table columns before migration: {columns}")
            if 'path' not in columns:
                c.execute('ALTER TABLE publishers ADD COLUMN path TEXT')
                conn.commit()
                app_logger.info("Added 'path' column to publishers table")
            if 'favorite' not in columns:
                c.execute('ALTER TABLE publishers ADD COLUMN favorite INTEGER DEFAULT 0')
                conn.commit()
                app_logger.info("Added 'favorite' column to publishers table")
            if 'logo' not in columns:
                c.execute('ALTER TABLE publishers ADD COLUMN logo TEXT')
                conn.commit()
                app_logger.info("Added 'logo' column to publishers table")
        except Exception as migration_error:
            app_logger.error(f"Publisher migration error: {migration_error}")

        c.execute('CREATE INDEX IF NOT EXISTS idx_publishers_path ON publishers(path)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_publishers_favorite ON publishers(favorite)')

        # Create series table (Metron series with local mapping)
        c.execute('''
            CREATE TABLE IF NOT EXISTS series (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                sort_name TEXT,
                volume INTEGER,
                status TEXT,
                publisher_id INTEGER,
                imprint TEXT,
                volume_year INTEGER,
                year_end INTEGER,
                desc TEXT,
                cv_id INTEGER,
                gcd_id INTEGER,
                resource_url TEXT,
                mapped_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (publisher_id) REFERENCES publishers(id)
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_series_cv_id ON series(cv_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_series_gcd_id ON series(gcd_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_series_mapped_path ON series(mapped_path)')

        # Migration: Add issue_count and last_synced_at columns to series table
        c.execute("PRAGMA table_info(series)")
        series_columns = [col[1] for col in c.fetchall()]
        if 'issue_count' not in series_columns:
            c.execute('ALTER TABLE series ADD COLUMN issue_count INTEGER')
            app_logger.info("Added issue_count column to series table")
        if 'last_synced_at' not in series_columns:
            c.execute('ALTER TABLE series ADD COLUMN last_synced_at TIMESTAMP')
            app_logger.info("Added last_synced_at column to series table")
        if 'cover_image' not in series_columns:
            c.execute('ALTER TABLE series ADD COLUMN cover_image TEXT')
            app_logger.info("Added cover_image column to series table")

        # Create issues table (Metron issues cached for tracked series)
        c.execute('''
            CREATE TABLE IF NOT EXISTS issues (
                id INTEGER PRIMARY KEY,
                series_id INTEGER NOT NULL,
                number TEXT,
                name TEXT,
                cover_date TEXT,
                store_date TEXT,
                image TEXT,
                resource_url TEXT,
                cv_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_issues_series_id ON issues(series_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_issues_store_date ON issues(store_date)')

        # Create collection_status table (cache for issue-to-file mappings)
        c.execute('''
            CREATE TABLE IF NOT EXISTS collection_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                issue_id INTEGER NOT NULL,
                issue_number TEXT NOT NULL,
                found INTEGER DEFAULT 0,
                file_path TEXT,
                file_mtime REAL,
                matched_via TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(series_id, issue_id),
                FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE,
                FOREIGN KEY (issue_id) REFERENCES issues(id) ON DELETE CASCADE
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_collection_status_series ON collection_status(series_id)')

        # Create issue_manual_status table (for manually marking issues as owned/skipped)
        c.execute('''
            CREATE TABLE IF NOT EXISTS issue_manual_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                issue_number TEXT NOT NULL,
                status TEXT NOT NULL,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(series_id, issue_number),
                FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_issue_manual_status_series ON issue_manual_status(series_id)')

        # Create libraries table (multiple library roots support)
        c.execute('''
            CREATE TABLE IF NOT EXISTS libraries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_libraries_path ON libraries(path)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_libraries_enabled ON libraries(enabled)')

        # Create provider_credentials table (encrypted API credentials)
        c.execute('''
            CREATE TABLE IF NOT EXISTS provider_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_type TEXT NOT NULL UNIQUE,
                credentials_encrypted BLOB NOT NULL,
                credentials_nonce BLOB NOT NULL,
                is_valid INTEGER DEFAULT 0,
                last_tested TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create library_providers table (provider configuration per library)
        c.execute('''
            CREATE TABLE IF NOT EXISTS library_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                library_id INTEGER NOT NULL,
                provider_type TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                FOREIGN KEY (library_id) REFERENCES libraries(id) ON DELETE CASCADE,
                UNIQUE(library_id, provider_type)
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_library_providers_library ON library_providers(library_id)')

        # Create provider_cache table (unified cache for all providers)
        c.execute('''
            CREATE TABLE IF NOT EXISTS provider_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_type TEXT NOT NULL,
                cache_type TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                UNIQUE(provider_type, cache_type, provider_id)
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_provider_cache_lookup ON provider_cache(provider_type, cache_type, provider_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_provider_cache_expires ON provider_cache(expires_at)')

        # Create Komga sync configuration table (single-row)
        c.execute('''
            CREATE TABLE IF NOT EXISTS komga_sync_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                server_url TEXT NOT NULL DEFAULT '',
                credentials_encrypted BLOB,
                credentials_nonce BLOB,
                path_prefix_komga TEXT NOT NULL DEFAULT '',
                path_prefix_clu TEXT NOT NULL DEFAULT '',
                enabled INTEGER DEFAULT 0,
                last_sync TIMESTAMP,
                last_sync_read_count INTEGER DEFAULT 0,
                last_sync_progress_count INTEGER DEFAULT 0,
                frequency TEXT NOT NULL DEFAULT 'disabled',
                time TEXT NOT NULL DEFAULT '05:00',
                weekday INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('INSERT OR IGNORE INTO komga_sync_config (id, server_url) VALUES (1, "")')

        # Create Komga sync log table (tracks which books have been synced)
        c.execute('''
            CREATE TABLE IF NOT EXISTS komga_sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                komga_book_id TEXT NOT NULL,
                komga_path TEXT NOT NULL,
                clu_path TEXT,
                sync_type TEXT NOT NULL,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(komga_book_id, sync_type)
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_komga_sync_book ON komga_sync_log(komga_book_id)')

        # Create unified schedules table
        c.execute('''
            CREATE TABLE IF NOT EXISTS schedules (
                name TEXT PRIMARY KEY,
                frequency TEXT NOT NULL DEFAULT 'disabled',
                time TEXT NOT NULL DEFAULT '02:00',
                weekday INTEGER DEFAULT 0,
                last_run TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Migration: Populate schedules from legacy tables if empty
        c.execute('SELECT COUNT(*) FROM schedules')
        if c.fetchone()[0] == 0:
            # Migrate rebuild_schedule
            c.execute('SELECT frequency, time, weekday, last_rebuild FROM rebuild_schedule WHERE id = 1')
            row = c.fetchone()
            if row:
                c.execute('INSERT INTO schedules (name, frequency, time, weekday, last_run) VALUES (?, ?, ?, ?, ?)',
                          ('rebuild', row[0], row[1], row[2], row[3]))
            else:
                c.execute('INSERT INTO schedules (name, frequency, time, weekday) VALUES (?, ?, ?, ?)',
                          ('rebuild', 'disabled', '02:00', 0))

            # Migrate sync_schedule
            c.execute('SELECT frequency, time, weekday, last_sync FROM sync_schedule WHERE id = 1')
            row = c.fetchone()
            if row:
                c.execute('INSERT INTO schedules (name, frequency, time, weekday, last_run) VALUES (?, ?, ?, ?, ?)',
                          ('sync', row[0], row[1], row[2], row[3]))
            else:
                c.execute('INSERT INTO schedules (name, frequency, time, weekday) VALUES (?, ?, ?, ?)',
                          ('sync', 'disabled', '03:00', 0))

            # Migrate getcomics_schedule
            c.execute('SELECT frequency, time, weekday, last_run FROM getcomics_schedule WHERE id = 1')
            row = c.fetchone()
            if row:
                c.execute('INSERT INTO schedules (name, frequency, time, weekday, last_run) VALUES (?, ?, ?, ?, ?)',
                          ('getcomics', row[0], row[1], row[2], row[3]))
            else:
                c.execute('INSERT INTO schedules (name, frequency, time, weekday) VALUES (?, ?, ?, ?)',
                          ('getcomics', 'disabled', '03:00', 0))

            # Migrate weekly_packs_config (map enabled -> frequency)
            c.execute('SELECT enabled, weekday, time, last_run FROM weekly_packs_config WHERE id = 1')
            row = c.fetchone()
            if row:
                freq = 'weekly' if row[0] else 'disabled'
                c.execute('INSERT INTO schedules (name, frequency, time, weekday, last_run) VALUES (?, ?, ?, ?, ?)',
                          ('weekly_packs', freq, row[2], row[1], row[3]))
            else:
                c.execute('INSERT INTO schedules (name, frequency, time, weekday) VALUES (?, ?, ?, ?)',
                          ('weekly_packs', 'disabled', '10:00', 2))

            # Migrate komga_sync_config
            c.execute('SELECT frequency, time, weekday, last_sync FROM komga_sync_config WHERE id = 1')
            row = c.fetchone()
            if row:
                c.execute('INSERT INTO schedules (name, frequency, time, weekday, last_run) VALUES (?, ?, ?, ?, ?)',
                          ('komga', row[0] or 'disabled', row[1] or '05:00', row[2] or 0, row[3]))
            else:
                c.execute('INSERT INTO schedules (name, frequency, time, weekday) VALUES (?, ?, ?, ?)',
                          ('komga', 'disabled', '05:00', 0))

            app_logger.info("Migrated schedule data from legacy tables to unified schedules table")

        # Migration: Auto-create default library if table is empty and /data exists
        c.execute('SELECT COUNT(*) FROM libraries')
        if c.fetchone()[0] == 0:
            # Check if /data exists (Docker mount) or use first available path
            if os.path.exists('/data'):
                c.execute('''
                    INSERT INTO libraries (name, path, enabled)
                    VALUES ('Library', '/data', 1)
                ''')
                app_logger.info("Created default library with path /data")

        conn.commit()
        conn.close()
        app_logger.info("Database initialized successfully")
        return True
    except Exception as e:
        app_logger.error(f"Failed to initialize database: {e}")
        return False

def get_db_connection():
    """Get a connection to the SQLite database."""
    try:
        conn = sqlite3.connect(get_db_path(), timeout=30)
        conn.row_factory = sqlite3.Row
        # Ensure WAL mode and busy timeout for better concurrency
        conn.execute('PRAGMA busy_timeout=30000')
        # Enable foreign key enforcement for ON DELETE CASCADE
        conn.execute('PRAGMA foreign_keys=ON')
        return conn
    except Exception as e:
        app_logger.error(f"Failed to connect to database: {e}")
        return None


# =============================================================================
# Database Backup Functions
# =============================================================================

def backup_database(max_backups: int = 3) -> bool:
    """
    Create a ZIP backup of the database if it has changed since last backup.

    Args:
        max_backups: Maximum number of backups to retain (default 3)

    Returns:
        True if backup created, False if skipped or error
    """
    try:
        db_path = get_db_path()
        if not os.path.exists(db_path):
            app_logger.info("Database does not exist yet, skipping backup")
            return False

        cache_dir = os.path.dirname(db_path)

        # Calculate current DB hash (MD5 for speed)
        def get_file_hash(filepath):
            hash_md5 = hashlib.md5()
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()

        current_hash = get_file_hash(db_path)

        # Check last backup hash
        hash_file = os.path.join(cache_dir, ".db_backup_hash")
        if os.path.exists(hash_file):
            with open(hash_file, "r") as f:
                last_hash = f.read().strip()
            if last_hash == current_hash:
                app_logger.debug("Database unchanged since last backup, skipping")
                return False

        # Create timestamped backup filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"comic_utils_backup_{timestamp}.zip"
        backup_path = os.path.join(cache_dir, backup_name)

        # Create ZIP with database and WAL files
        with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_path, "comic_utils.db")

            # Include WAL files if they exist
            wal_path = db_path + "-wal"
            shm_path = db_path + "-shm"
            if os.path.exists(wal_path):
                zf.write(wal_path, "comic_utils.db-wal")
            if os.path.exists(shm_path):
                zf.write(shm_path, "comic_utils.db-shm")

        app_logger.info(f"Database backup created: {backup_name}")

        # Save current hash
        with open(hash_file, "w") as f:
            f.write(current_hash)

        # Cleanup old backups (keep only max_backups most recent)
        _cleanup_old_backups(cache_dir, max_backups)

        return True

    except Exception as e:
        app_logger.error(f"Database backup failed: {e}")
        return False


def _cleanup_old_backups(cache_dir: str, max_backups: int):
    """Remove old backup files, keeping only the most recent ones."""
    try:
        # Find all backup files
        backup_files = [
            f for f in os.listdir(cache_dir)
            if f.startswith("comic_utils_backup_") and f.endswith(".zip")
        ]

        if len(backup_files) <= max_backups:
            return

        # Sort by name (timestamp in name ensures chronological order)
        backup_files.sort(reverse=True)

        # Delete oldest backups beyond max_backups
        for old_backup in backup_files[max_backups:]:
            old_path = os.path.join(cache_dir, old_backup)
            os.remove(old_path)
            app_logger.info(f"Removed old backup: {old_backup}")

    except Exception as e:
        app_logger.warning(f"Error cleaning up old backups: {e}")


# =============================================================================
# Libraries CRUD Operations
# =============================================================================

def get_libraries(enabled_only=True):
    """
    Get all libraries.

    Args:
        enabled_only: If True, only return enabled libraries (default True)

    Returns:
        List of dictionaries with id, name, path, enabled, created_at
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()
        if enabled_only:
            c.execute('SELECT id, name, path, enabled, created_at FROM libraries WHERE enabled = 1 ORDER BY name')
        else:
            c.execute('SELECT id, name, path, enabled, created_at FROM libraries ORDER BY name')

        results = [dict(row) for row in c.fetchall()]
        conn.close()
        return results
    except Exception as e:
        app_logger.error(f"Error getting libraries: {e}")
        return []


def get_library_by_id(library_id):
    """
    Get a library by its ID.

    Args:
        library_id: The library ID

    Returns:
        Dictionary with library data, or None if not found
    """
    try:
        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()
        c.execute('SELECT id, name, path, enabled, created_at FROM libraries WHERE id = ?', (library_id,))
        row = c.fetchone()
        conn.close()

        return dict(row) if row else None
    except Exception as e:
        app_logger.error(f"Error getting library by ID {library_id}: {e}")
        return None


def add_library(name, path):
    """
    Add a new library.

    Args:
        name: Display name for the library
        path: Root path for the library (must be unique)

    Returns:
        The new library ID, or None on error
    """
    try:
        # Normalize the path
        normalized_path = os.path.normpath(path)

        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()
        c.execute('''
            INSERT INTO libraries (name, path, enabled)
            VALUES (?, ?, 1)
        ''', (name, normalized_path))

        library_id = c.lastrowid
        conn.commit()
        conn.close()

        app_logger.info(f"Added library '{name}' with path {normalized_path}")
        return library_id
    except sqlite3.IntegrityError:
        app_logger.error(f"Library with path {path} already exists")
        return None
    except Exception as e:
        app_logger.error(f"Error adding library: {e}")
        return None


def update_library(library_id, name=None, path=None, enabled=None):
    """
    Update a library.

    Args:
        library_id: The library ID to update
        name: New name (optional)
        path: New path (optional)
        enabled: New enabled state (optional)

    Returns:
        True if successful, False otherwise
    """
    try:
        updates = []
        params = []

        if name is not None:
            updates.append('name = ?')
            params.append(name)
        if path is not None:
            updates.append('path = ?')
            params.append(os.path.normpath(path))
        if enabled is not None:
            updates.append('enabled = ?')
            params.append(1 if enabled else 0)

        if not updates:
            return True  # Nothing to update

        params.append(library_id)

        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute(f'''
            UPDATE libraries SET {', '.join(updates)} WHERE id = ?
        ''', params)

        conn.commit()
        conn.close()

        app_logger.info(f"Updated library ID {library_id}")
        return True
    except sqlite3.IntegrityError:
        app_logger.error(f"Cannot update library - path already exists")
        return False
    except Exception as e:
        app_logger.error(f"Error updating library {library_id}: {e}")
        return False


def delete_library(library_id):
    """
    Delete a library.

    Args:
        library_id: The library ID to delete

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('DELETE FROM libraries WHERE id = ?', (library_id,))

        conn.commit()
        conn.close()

        app_logger.info(f"Deleted library ID {library_id}")
        return True
    except Exception as e:
        app_logger.error(f"Error deleting library {library_id}: {e}")
        return False


def log_recent_file(file_path, file_name=None, file_size=None):
    """
    Log a recently added file to the database with rotation (keep only last 100).

    Args:
        file_path: Full path to the file
        file_name: Name of the file (optional, will extract from path if not provided)
        file_size: Size of the file in bytes (optional, will calculate if not provided)
    """
    try:
        if file_name is None:
            file_name = os.path.basename(file_path)

        if file_size is None and os.path.exists(file_path):
            file_size = os.path.getsize(file_path)

        conn = get_db_connection()
        if not conn:
            app_logger.error("Could not get database connection to log recent file")
            return False

        c = conn.cursor()

        # Check if file already exists
        c.execute('SELECT id FROM recent_files WHERE file_path = ?', (file_path,))
        existing = c.fetchone()

        if existing:
            # Update existing entry with new timestamp
            c.execute('''
                UPDATE recent_files
                SET file_name = ?, file_size = ?, added_at = CURRENT_TIMESTAMP
                WHERE file_path = ?
            ''', (file_name, file_size, file_path))
        else:
            # Insert new file
            c.execute('''
                INSERT INTO recent_files (file_path, file_name, file_size)
                VALUES (?, ?, ?)
            ''', (file_path, file_name, file_size))

        # Count total files
        c.execute('SELECT COUNT(*) FROM recent_files')
        count = c.fetchone()[0]

        # If we have more than 100, delete the oldest ones
        if count > 100:
            c.execute('''
                DELETE FROM recent_files
                WHERE id IN (
                    SELECT id FROM recent_files
                    ORDER BY added_at ASC
                    LIMIT ?
                )
            ''', (count - 100,))

        conn.commit()
        conn.close()
        app_logger.debug(f"Logged recent file: {file_name}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to log recent file {file_path}: {e}")
        return False

def get_recent_files(limit=100):
    """
    Get the most recently added files to the library (by first indexed date).

    This returns files ordered by when they were first added to the index,
    not by file modification time. Renamed/updated files keep their original
    indexed date and won't appear at the top of the list.

    Args:
        limit: Maximum number of files to return (default 100)

    Returns:
        List of dictionaries containing file information, or empty list on error
    """
    target = config.get("SETTINGS", "TARGET", fallback="/data/downloads/processed")
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Could not get database connection to retrieve recent files")
            return []

        c = conn.cursor()

        # Query file_index for recent comic files by first_indexed_at (when file was first added)
        c.execute('''
            SELECT path as file_path, name as file_name, size as file_size,
                   datetime(first_indexed_at, 'unixepoch', 'localtime') as added_at
            FROM file_index
            WHERE type = 'file'
            AND (name LIKE '%.cbz' OR name LIKE '%.cbr')
            AND first_indexed_at IS NOT NULL
            AND parent NOT LIKE ?
            ORDER BY first_indexed_at DESC
            LIMIT ?
        ''', (f"{target}%", limit))

        rows = c.fetchall()
        conn.close()

        # Convert to list of dictionaries
        files = []
        for row in rows:
            files.append({
                'file_path': row['file_path'],
                'file_name': row['file_name'],
                'file_size': row['file_size'],
                'added_at': row['added_at']
            })

        return files

    except Exception as e:
        app_logger.error(f"Failed to retrieve recent files: {e}")
        return []

#########################
#   File Index Functions #
#########################

def get_file_index_from_db():
    """
    Load the file index from the database.

    Returns:
        List of dictionaries containing file index entries, or empty list on error
    """
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Could not get database connection to retrieve file index")
            return []

        c = conn.cursor()
        c.execute('''
            SELECT name, path, type, size, parent
            FROM file_index
            ORDER BY type DESC, name ASC
        ''')

        rows = c.fetchall()
        conn.close()

        # Convert to list of dictionaries
        index = []
        for row in rows:
            entry = {
                'name': row['name'],
                'path': row['path'],
                'type': row['type'],
                'parent': row['parent']
            }
            if row['size'] is not None:
                entry['size'] = row['size']
            index.append(entry)

        app_logger.info(f"Loaded {len(index)} entries from file index database")
        return index

    except Exception as e:
        app_logger.error(f"Failed to retrieve file index: {e}")
        return []


def get_directory_children(parent_path, max_retries=3):
    """
    Get all direct children of a directory from file_index.
    Used for fast directory browsing without filesystem access.

    Args:
        parent_path: The parent directory path to query
        max_retries: Number of times to retry on database lock

    Returns:
        Tuple of (directories, files) where each is a list of dictionaries
    """
    import time

    for attempt in range(max_retries):
        conn = None
        try:
            conn = get_db_connection()
            if not conn:
                app_logger.error("Could not get database connection for directory children")
                return [], []

            c = conn.cursor()
            c.execute('''
                SELECT name, path, type, size, has_thumbnail
                FROM file_index
                WHERE parent = ?
                ORDER BY type DESC, name COLLATE NOCASE ASC
            ''', (parent_path,))

            rows = c.fetchall()
            conn.close()

            directories = []
            files = []
            for row in rows:
                entry = {
                    'name': row['name'],
                    'path': row['path'],
                    'type': row['type']
                }
                if row['type'] == 'directory':
                    entry['has_thumbnail'] = bool(row['has_thumbnail']) if row['has_thumbnail'] else False
                    directories.append(entry)
                else:
                    entry['size'] = row['size'] if row['size'] else 0
                    files.append(entry)

            return directories, files

        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() and attempt < max_retries - 1:
                app_logger.warning(f"Database locked, retrying ({attempt + 1}/{max_retries})...")
                if conn:
                    conn.close()
                time.sleep(0.5 * (attempt + 1))  # Exponential backoff
                continue
            app_logger.error(f"Failed to get directory children for {parent_path}: {e}")
            return [], []
        except Exception as e:
            app_logger.error(f"Failed to get directory children for {parent_path}: {e}")
            if conn:
                conn.close()
            return [], []


def save_file_index_to_db(file_index):
    """
    Save the entire file index to the database (batch operation).

    Args:
        file_index: List of dictionaries with keys: name, path, type, size, parent, has_thumbnail

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Could not get database connection to save file index")
            return False

        c = conn.cursor()

        # Clear existing index
        c.execute('DELETE FROM file_index')

        # Prepare batch insert
        import time
        current_time = time.time()
        records = [
            (
                entry['name'],
                entry['path'],
                entry['type'],
                entry.get('size'),
                entry['parent'],
                entry.get('has_thumbnail', 0),
                entry.get('modified_at'),
                current_time  # first_indexed_at
            )
            for entry in file_index
        ]

        # Batch insert
        c.executemany('''
            INSERT INTO file_index (name, path, type, size, parent, has_thumbnail, modified_at, first_indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', records)

        conn.commit()
        conn.close()

        app_logger.info(f"Saved {len(records)} entries to file index database")
        return True

    except Exception as e:
        app_logger.error(f"Failed to save file index: {e}")
        return False


def get_path_counts(path):
    """
    Get recursive folder and file counts for a path using file_index.

    Args:
        path: Directory path (e.g., '/data/Marvel')

    Returns:
        Tuple of (folder_count, file_count) or (0, 0) on error
    """
    try:
        conn = get_db_connection()
        if not conn:
            return (0, 0)

        c = conn.cursor()
        # Use path prefix to count all descendants (recursive)
        # Ensure trailing slash to avoid matching partial names (e.g., /data/Marvel vs /data/MarvelMax)
        path_prefix = path.rstrip('/') + '/'

        c.execute('''
            SELECT
                SUM(CASE WHEN type = 'directory' THEN 1 ELSE 0 END) as folder_count,
                SUM(CASE WHEN type = 'file' THEN 1 ELSE 0 END) as file_count
            FROM file_index
            WHERE path LIKE ? || '%'
        ''', (path_prefix,))

        row = c.fetchone()
        conn.close()

        if row:
            return (row['folder_count'] or 0, row['file_count'] or 0)
        return (0, 0)

    except Exception as e:
        app_logger.error(f"Failed to get path counts for '{path}': {e}")
        return (0, 0)


def get_path_counts_batch(paths):
    """
    Get recursive folder and file counts for multiple paths in ONE query.
    Much faster than calling get_path_counts() N times.

    Args:
        paths: List of directory paths (e.g., ['/data/Marvel', '/data/DC'])

    Returns:
        Dict mapping path -> (folder_count, file_count)
    """
    if not paths:
        return {}

    try:
        conn = get_db_connection()
        if not conn:
            return {p: (0, 0) for p in paths}

        c = conn.cursor()
        results = {}

        # Process in batches of 100 to avoid SQLite parameter limits
        BATCH_SIZE = 100
        for i in range(0, len(paths), BATCH_SIZE):
            batch = paths[i:i + BATCH_SIZE]
            path_prefixes = [p.rstrip('/') + '/' for p in batch]

            # Build UNION ALL query for batch - one query instead of N
            query_parts = []
            params = []
            for path, prefix in zip(batch, path_prefixes):
                query_parts.append('''
                    SELECT ? as path,
                        SUM(CASE WHEN type = 'directory' THEN 1 ELSE 0 END) as folder_count,
                        SUM(CASE WHEN type = 'file' THEN 1 ELSE 0 END) as file_count
                    FROM file_index WHERE path LIKE ? || '%'
                ''')
                params.extend([path, prefix])

            c.execute(' UNION ALL '.join(query_parts), params)
            for row in c.fetchall():
                results[row['path']] = (row['folder_count'] or 0, row['file_count'] or 0)

        conn.close()

        # Fill missing paths with (0, 0)
        for p in paths:
            if p not in results:
                results[p] = (0, 0)

        return results

    except Exception as e:
        app_logger.error(f"Failed to get batch path counts: {e}")
        return {p: (0, 0) for p in paths}


def update_file_index_entry(path, name=None, new_path=None, parent=None, size=None):
    """
    Update a single file index entry incrementally.

    Args:
        path: Current path of the entry (used to find the record)
        name: New name (optional)
        new_path: New path (optional, for move/rename operations)
        parent: New parent path (optional)
        size: New size (optional)
        modified_at: New modification timestamp (optional)
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()

        # Build UPDATE query dynamically based on provided fields
        updates = []
        params = []

        if name is not None:
            updates.append("name = ?")
            params.append(name)

        if new_path is not None:
            updates.append("path = ?")
            params.append(new_path)

        if parent is not None:
            updates.append("parent = ?")
            params.append(parent)

        if size is not None:
            updates.append("size = ?")
            params.append(size)

        if modified_at is not None:
            updates.append("modified_at = ?")
            params.append(modified_at)

        if not updates:
            conn.close()
            return True  # Nothing to update

        updates.append("last_updated = CURRENT_TIMESTAMP")
        params.append(path)  # WHERE clause parameter

        query = f"UPDATE file_index SET {', '.join(updates)} WHERE path = ?"
        c.execute(query, params)

        conn.commit()
        rows_affected = c.rowcount
        conn.close()

        if rows_affected > 0:
            app_logger.debug(f"Updated file index entry: {path}")
            return True
        else:
            app_logger.warning(f"File index entry not found for update: {path}")
            return False

    except Exception as e:
        app_logger.error(f"Failed to update file index entry {path}: {e}")
        return False

def add_file_index_entry(name, path, entry_type, size=None, parent=None, has_thumbnail=0, modified_at=None):
    """
    Add a new entry to the file index.

    Args:
        name: File or directory name
        path: Full path
        entry_type: 'file' or 'directory'
        size: File size in bytes (optional, None for directories)
        parent: Parent directory path (optional)
        has_thumbnail: 1 if directory has folder.png/jpg, 0 otherwise (optional)
        modified_at: Modification timestamp (optional)

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        import time
        c = conn.cursor()

        # Use ON CONFLICT to preserve first_indexed_at for existing entries
        c.execute('''
            INSERT INTO file_index (name, path, type, size, parent, has_thumbnail, modified_at, first_indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                name = excluded.name,
                type = excluded.type,
                size = excluded.size,
                parent = excluded.parent,
                has_thumbnail = excluded.has_thumbnail,
                modified_at = excluded.modified_at
        ''', (name, path, entry_type, size, parent, has_thumbnail, modified_at, time.time()))

        conn.commit()
        conn.close()

        app_logger.debug(f"Added file index entry: {path}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to add file index entry {path}: {e}")
        return False

def delete_file_index_entry(path):
    """
    Delete an entry from the file index.

    Args:
        path: Full path of the entry to delete

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()

        # Delete the entry
        c.execute('DELETE FROM file_index WHERE path = ?', (path,))

        # Also delete any children (for directories)
        c.execute('DELETE FROM file_index WHERE parent = ? OR path LIKE ?', (path, f"{path}/%"))

        conn.commit()
        rows_affected = c.rowcount
        conn.close()

        if rows_affected > 0:
            app_logger.debug(f"Deleted {rows_affected} file index entries for: {path}")
            return True
        else:
            app_logger.warning(f"File index entry not found for deletion: {path}")
            return False

    except Exception as e:
        app_logger.error(f"Failed to delete file index entry {path}: {e}")
        return False


def delete_file_index_entries(paths, dir_paths=None):
    """
    Batch-delete multiple entries from the file index in a single transaction.

    Args:
        paths: List of full paths to delete
        dir_paths: Optional subset of paths that are directories (need children cleanup)

    Returns:
        Number of rows deleted
    """
    if not paths:
        return 0
    try:
        conn = get_db_connection()
        if not conn:
            return 0

        c = conn.cursor()
        total_deleted = 0

        # Delete exact path entries
        c.executemany('DELETE FROM file_index WHERE path = ?', [(p,) for p in paths])
        total_deleted += c.rowcount

        # Delete children only for directory paths
        if dir_paths:
            for dp in dir_paths:
                c.execute('DELETE FROM file_index WHERE parent = ? OR path LIKE ?', (dp, f"{dp}/%"))
                total_deleted += c.rowcount

        conn.commit()
        conn.close()

        if total_deleted > 0:
            app_logger.debug(f"Batch-deleted {total_deleted} file index entries for {len(paths)} paths")
        return total_deleted

    except Exception as e:
        app_logger.error(f"Failed to batch-delete file index entries: {e}")
        return 0


def clear_file_index_from_db():
    """
    Clear all entries from the file index database.

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('DELETE FROM file_index')

        conn.commit()
        rows_affected = c.rowcount
        conn.close()

        app_logger.info(f"Cleared {rows_affected} entries from file index database")
        return True

    except Exception as e:
        app_logger.error(f"Failed to clear file index database: {e}")
        return False


def sync_file_index_incremental(filesystem_entries):
    """
    Incrementally sync file_index with filesystem.

    - Adds new entries (files in filesystem but not in DB)
    - Removes orphaned entries (files in DB but not in filesystem)
    - Preserves existing entries (keeps metadata intact)

    Args:
        filesystem_entries: List of dicts with {path, name, type, size, parent, has_thumbnail, modified_at}

    Returns:
        Dict with counts: {'added': N, 'removed': N, 'unchanged': N, 'new_paths': [...]}
    """
    try:
        conn = get_db_connection()
        if not conn:
            return {'added': 0, 'removed': 0, 'unchanged': 0, 'new_paths': []}

        c = conn.cursor()

        # Get all current paths in DB
        c.execute('SELECT path FROM file_index')
        db_paths = set(row[0] for row in c.fetchall())

        # Get all paths from filesystem scan
        fs_paths = set(entry['path'] for entry in filesystem_entries)

        # Find differences
        new_paths = fs_paths - db_paths          # In filesystem, not in DB -> ADD
        removed_paths = db_paths - fs_paths      # In DB, not in filesystem -> REMOVE
        existing_paths = fs_paths & db_paths     # In both -> KEEP (preserve metadata)

        # Remove orphaned entries
        if removed_paths:
            for path in removed_paths:
                c.execute('DELETE FROM file_index WHERE path = ?', (path,))
            app_logger.info(f"Removed {len(removed_paths)} orphaned entries from file_index")

        # Add new entries
        import time
        current_time = time.time()
        new_entries = [e for e in filesystem_entries if e['path'] in new_paths]
        for entry in new_entries:
            # Use ON CONFLICT to preserve first_indexed_at for existing entries
            c.execute('''
                INSERT INTO file_index (name, path, type, size, parent, has_thumbnail, modified_at, first_indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    name = excluded.name,
                    type = excluded.type,
                    size = excluded.size,
                    parent = excluded.parent,
                    has_thumbnail = excluded.has_thumbnail,
                    modified_at = excluded.modified_at
            ''', (
                entry['name'],
                entry['path'],
                entry['type'],
                entry.get('size'),
                entry.get('parent'),
                entry.get('has_thumbnail', 0),
                entry.get('modified_at'),
                current_time
            ))

        conn.commit()
        conn.close()

        if new_paths:
            app_logger.info(f"Added {len(new_paths)} new entries to file_index")

        return {
            'added': len(new_paths),
            'removed': len(removed_paths),
            'unchanged': len(existing_paths),
            'new_paths': list(new_paths)
        }

    except Exception as e:
        app_logger.error(f"Failed to sync file index incrementally: {e}")
        return {'added': 0, 'removed': 0, 'unchanged': 0, 'new_paths': []}


def search_file_index(query, limit=100):
    """
    Search the file index for entries matching the query.

    Args:
        query: Search query string
        limit: Maximum number of results to return

    Returns:
        List of matching entries
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()

        # Search with LIKE for partial matching (case-insensitive)
        c.execute('''
            SELECT name, path, type, size, parent
            FROM file_index
            WHERE LOWER(name) LIKE LOWER(?)
            ORDER BY type DESC, name ASC
            LIMIT ?
        ''', (f'%{query}%', limit))

        rows = c.fetchall()
        conn.close()

        # Convert to list of dictionaries
        results = []
        for row in rows:
            entry = {
                'name': row['name'],
                'path': row['path'],
                'type': row['type'],
                'parent': row['parent']
            }
            if row['size'] is not None:
                entry['size'] = row['size']
            results.append(entry)

        return results

    except Exception as e:
        app_logger.error(f"Failed to search file index: {e}")
        return []


# ============================================
# File Index Metadata Scanning Functions
# ============================================

def update_file_metadata(file_id, metadata_dict, scanned_at):
    """
    Update ComicInfo.xml metadata columns for a file_index entry.

    Args:
        file_id: ID of the file_index entry
        metadata_dict: Dict with keys like 'ci_title', 'ci_writer', etc.
        scanned_at: Unix timestamp of when scan completed

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            UPDATE file_index
            SET ci_title = ?, ci_series = ?, ci_number = ?, ci_count = ?,
                ci_volume = ?, ci_year = ?, ci_writer = ?, ci_penciller = ?,
                ci_inker = ?, ci_colorist = ?, ci_letterer = ?, ci_coverartist = ?,
                ci_publisher = ?, ci_genre = ?, ci_characters = ?,
                metadata_scanned_at = ?
            WHERE id = ?
        ''', (
            metadata_dict.get('ci_title', ''),
            metadata_dict.get('ci_series', ''),
            metadata_dict.get('ci_number', ''),
            metadata_dict.get('ci_count', ''),
            metadata_dict.get('ci_volume', ''),
            metadata_dict.get('ci_year', ''),
            metadata_dict.get('ci_writer', ''),
            metadata_dict.get('ci_penciller', ''),
            metadata_dict.get('ci_inker', ''),
            metadata_dict.get('ci_colorist', ''),
            metadata_dict.get('ci_letterer', ''),
            metadata_dict.get('ci_coverartist', ''),
            metadata_dict.get('ci_publisher', ''),
            metadata_dict.get('ci_genre', ''),
            metadata_dict.get('ci_characters', ''),
            scanned_at,
            file_id
        ))

        conn.commit()
        conn.close()
        return True

    except Exception as e:
        app_logger.error(f"Failed to update file metadata for id {file_id}: {e}")
        return False


def update_metadata_scanned_at(file_id, scanned_at):
    """
    Mark a file as scanned without updating metadata fields.
    Used when file has no ComicInfo.xml or on error.

    Args:
        file_id: ID of the file_index entry
        scanned_at: Unix timestamp (or None)

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('UPDATE file_index SET metadata_scanned_at = ? WHERE id = ?',
                  (scanned_at, file_id))

        conn.commit()
        conn.close()
        return True

    except Exception as e:
        app_logger.error(f"Failed to update metadata_scanned_at for id {file_id}: {e}")
        return False


def get_files_needing_metadata_scan(limit=1000):
    """
    Get files that need metadata scanning.

    Criteria:
    - type = 'file'
    - path ends with .cbz or .zip
    - metadata_scanned_at IS NULL OR metadata_scanned_at < modified_at

    Args:
        limit: Maximum number of files to return

    Returns:
        List of dicts with id, path, modified_at
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()
        c.execute('''
            SELECT id, path, modified_at
            FROM file_index
            WHERE type = 'file'
            AND (LOWER(path) LIKE '%.cbz' OR LOWER(path) LIKE '%.zip')
            AND (metadata_scanned_at IS NULL OR metadata_scanned_at < modified_at)
            ORDER BY modified_at DESC
            LIMIT ?
        ''', (limit,))

        rows = c.fetchall()
        conn.close()

        return [{'id': r['id'], 'path': r['path'], 'modified_at': r['modified_at']} for r in rows]

    except Exception as e:
        app_logger.error(f"Failed to get files needing metadata scan: {e}")
        return []


def get_metadata_scan_stats():
    """
    Get statistics for metadata scanning progress.

    Returns:
        Dict with total, scanned, pending counts
    """
    try:
        conn = get_db_connection()
        if not conn:
            return {'total': 0, 'scanned': 0, 'pending': 0}

        c = conn.cursor()

        # Total CBZ/ZIP files
        c.execute('''
            SELECT COUNT(*) as count FROM file_index
            WHERE type = 'file'
            AND (LOWER(path) LIKE '%.cbz' OR LOWER(path) LIKE '%.zip')
        ''')
        total = c.fetchone()['count']

        # Files needing scan
        c.execute('''
            SELECT COUNT(*) as count FROM file_index
            WHERE type = 'file'
            AND (LOWER(path) LIKE '%.cbz' OR LOWER(path) LIKE '%.zip')
            AND (metadata_scanned_at IS NULL OR metadata_scanned_at < modified_at)
        ''')
        pending = c.fetchone()['count']

        conn.close()

        return {
            'total': total,
            'scanned': total - pending,
            'pending': pending
        }

    except Exception as e:
        app_logger.error(f"Failed to get metadata scan stats: {e}")
        return {'total': 0, 'scanned': 0, 'pending': 0}


def get_file_index_entry_by_path(path):
    """
    Get a file_index entry by its path.

    Args:
        path: The file path to look up

    Returns:
        Dict with file_index entry data, or None if not found
    """
    try:
        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()
        c.execute('SELECT id, path, modified_at FROM file_index WHERE path = ?', (path,))
        row = c.fetchone()
        conn.close()

        if row:
            return {'id': row['id'], 'path': row['path'], 'modified_at': row['modified_at']}
        return None

    except Exception as e:
        app_logger.error(f"Failed to get file_index entry for {path}: {e}")
        return None


#########################
#   Unified Schedules   #
#########################

def get_schedule(name):
    """
    Get schedule configuration by name from the unified schedules table.

    Args:
        name: Schedule name ('rebuild', 'sync', 'getcomics', 'weekly_packs', 'komga')

    Returns:
        Dict with frequency, time, weekday, last_run, or None on error
    """
    try:
        conn = get_db_connection()
        if not conn:
            return None
        c = conn.cursor()
        c.execute('SELECT frequency, time, weekday, last_run FROM schedules WHERE name = ?', (name,))
        row = c.fetchone()
        conn.close()
        if row:
            return {
                'frequency': row['frequency'],
                'time': row['time'],
                'weekday': row['weekday'],
                'last_run': row['last_run']
            }
        return None
    except Exception as e:
        app_logger.error(f"Failed to get schedule '{name}': {e}")
        return None


def save_schedule(name, frequency, time, weekday=0):
    """
    Save schedule configuration by name to the unified schedules table.

    Args:
        name: Schedule name
        frequency: 'disabled', 'daily', or 'weekly'
        time: Time in HH:MM format
        weekday: Day of week (0=Monday, 6=Sunday)

    Returns:
        True if successful
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False
        c = conn.cursor()
        c.execute('''
            INSERT INTO schedules (name, frequency, time, weekday, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
                frequency = excluded.frequency,
                time = excluded.time,
                weekday = excluded.weekday,
                updated_at = CURRENT_TIMESTAMP
        ''', (name, frequency, time, weekday))
        conn.commit()
        conn.close()
        app_logger.info(f"Saved schedule '{name}': {frequency} at {time}")
        return True
    except Exception as e:
        app_logger.error(f"Failed to save schedule '{name}': {e}")
        return False


def update_schedule_last_run(name):
    """
    Update the last_run timestamp for a schedule.

    Args:
        name: Schedule name

    Returns:
        True if successful
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False
        c = conn.cursor()
        c.execute('UPDATE schedules SET last_run = CURRENT_TIMESTAMP WHERE name = ?', (name,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        app_logger.error(f"Failed to update last run for schedule '{name}': {e}")
        return False


# Backward-compatible wrappers for rebuild schedule
def get_rebuild_schedule():
    """Get the current file index rebuild schedule."""
    s = get_schedule('rebuild')
    if s:
        s['last_rebuild'] = s.pop('last_run')
    return s

def save_rebuild_schedule(frequency, time, weekday=0):
    """Save the file index rebuild schedule."""
    return save_schedule('rebuild', frequency, time, weekday)

def update_last_rebuild():
    """Update the last_rebuild timestamp to current time."""
    return update_schedule_last_run('rebuild')


# Backward-compatible wrappers for sync schedule
def get_sync_schedule():
    """Get the current series sync schedule."""
    s = get_schedule('sync')
    if s:
        s['last_sync'] = s.pop('last_run')
    return s

def save_sync_schedule(frequency, time, weekday=0):
    """Save the series sync schedule."""
    return save_schedule('sync', frequency, time, weekday)

def update_last_sync():
    """Update the last_sync timestamp to current time."""
    return update_schedule_last_run('sync')


# Backward-compatible wrappers for getcomics schedule
def get_getcomics_schedule():
    """Get the GetComics auto-download schedule."""
    return get_schedule('getcomics')

def save_getcomics_schedule(frequency, time, weekday=0):
    """Save the GetComics auto-download schedule."""
    return save_schedule('getcomics', frequency, time, weekday)

def update_last_getcomics_run():
    """Update the last_run timestamp for GetComics auto-download."""
    return update_schedule_last_run('getcomics')


#########################
#   Weekly Packs        #
#########################

def get_weekly_packs_config():
    """
    Get the Weekly Packs configuration.
    Reads non-schedule fields from weekly_packs_config, schedule fields from schedules table.
    """
    try:
        import json

        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()
        c.execute('''
            SELECT enabled, format, publishers, retry_enabled, last_successful_pack, start_date
            FROM weekly_packs_config WHERE id = 1
        ''')
        row = c.fetchone()
        conn.close()

        if not row:
            return None

        sched = get_schedule('weekly_packs')

        return {
            'enabled': bool(row['enabled']),
            'format': row['format'],
            'publishers': json.loads(row['publishers']) if row['publishers'] else [],
            'retry_enabled': bool(row['retry_enabled']),
            'last_successful_pack': row['last_successful_pack'],
            'start_date': row['start_date'],
            'weekday': sched['weekday'] if sched else 2,
            'time': sched['time'] if sched else '10:00',
            'last_run': sched['last_run'] if sched else None,
        }

    except Exception as e:
        app_logger.error(f"Failed to get weekly packs config: {e}")
        return None


def save_weekly_packs_config(enabled, format_pref, publishers, weekday, time, retry_enabled, start_date=None):
    """
    Save the Weekly Packs configuration.
    Non-schedule fields go to weekly_packs_config, schedule fields go to schedules table.
    """
    try:
        import json

        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            UPDATE weekly_packs_config
            SET enabled = ?, format = ?, publishers = ?,
                retry_enabled = ?, start_date = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
        ''', (int(enabled), format_pref, json.dumps(publishers), int(retry_enabled), start_date))

        conn.commit()
        conn.close()

        # Save schedule fields to unified table
        freq = 'weekly' if enabled else 'disabled'
        save_schedule('weekly_packs', freq, time, weekday)

        app_logger.info(f"Saved weekly packs config: enabled={enabled}, format={format_pref}, publishers={publishers}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to save weekly packs config: {e}")
        return False


def update_last_weekly_packs_run(pack_date=None):
    """
    Update the last_run timestamp for Weekly Packs and optionally the last successful pack date.
    """
    try:
        update_schedule_last_run('weekly_packs')

        if pack_date:
            conn = get_db_connection()
            if conn:
                c = conn.cursor()
                c.execute('''
                    UPDATE weekly_packs_config
                    SET last_successful_pack = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                ''', (pack_date,))
                conn.commit()
                conn.close()

        app_logger.info(f"Updated last weekly packs run timestamp (pack_date={pack_date})")
        return True

    except Exception as e:
        app_logger.error(f"Failed to update last weekly packs run timestamp: {e}")
        return False


def log_weekly_pack_download(pack_date, publisher, format_pref, download_url, status='queued'):
    """
    Record a weekly pack download attempt in history.

    Args:
        pack_date: Date of the pack (e.g., "2026-01-14")
        publisher: Publisher name ('DC', 'Marvel', 'Image', 'INDIE')
        format_pref: Format ('JPG' or 'WEBP')
        download_url: The PIXELDRAIN download URL
        status: Download status ('queued', 'downloading', 'completed', 'failed')

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO weekly_packs_history (pack_date, publisher, format, download_url, status, downloaded_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (pack_date, publisher, format_pref, download_url, status))

        conn.commit()
        conn.close()

        app_logger.info(f"Logged weekly pack download: {pack_date} {publisher} {format_pref} - {status}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to log weekly pack download: {e}")
        return False


def update_weekly_pack_status(pack_date, publisher, format_pref, status):
    """
    Update the status of a weekly pack download.

    Args:
        pack_date: Date of the pack (e.g., "2026-01-14")
        publisher: Publisher name ('DC', 'Marvel', 'Image', 'INDIE')
        format_pref: Format ('JPG' or 'WEBP')
        status: New status ('queued', 'downloading', 'completed', 'failed')

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            UPDATE weekly_packs_history
            SET status = ?, downloaded_at = CURRENT_TIMESTAMP
            WHERE pack_date = ? AND publisher = ? AND format = ?
        ''', (status, pack_date, publisher, format_pref))

        conn.commit()
        conn.close()

        return True

    except Exception as e:
        app_logger.error(f"Failed to update weekly pack status: {e}")
        return False


def is_weekly_pack_downloaded(pack_date: str, publisher: str, format_pref: str) -> bool:
    """
    Check if a specific weekly pack has already been downloaded.

    Args:
        pack_date: Pack date in YYYY.MM.DD format
        publisher: Publisher name (DC, Marvel, Image, INDIE)
        format_pref: Format (JPG or WEBP)

    Returns:
        True if already downloaded (status is 'queued' or 'completed'), False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            SELECT COUNT(*) FROM weekly_packs_history
            WHERE pack_date = ? AND publisher = ? AND format = ?
            AND status IN ('queued', 'completed', 'downloading')
        ''', (pack_date, publisher, format_pref))
        count = c.fetchone()[0]
        conn.close()

        return count > 0

    except Exception as e:
        app_logger.error(f"Failed to check weekly pack status: {e}")
        return False


def get_weekly_packs_history(limit=20):
    """
    Get recent weekly pack download history.

    Args:
        limit: Maximum number of records to return

    Returns:
        List of dictionaries with pack download history, or empty list on error
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()
        c.execute('''
            SELECT pack_date, publisher, format, download_url, status, downloaded_at
            FROM weekly_packs_history
            ORDER BY downloaded_at DESC
            LIMIT ?
        ''', (limit,))
        rows = c.fetchall()
        conn.close()

        return [
            {
                'pack_date': row[0],
                'publisher': row[1],
                'format': row[2],
                'download_url': row[3],
                'status': row[4],
                'downloaded_at': row[5]
            }
            for row in rows
        ]

    except Exception as e:
        app_logger.error(f"Failed to get weekly packs history: {e}")
        return []


#########################
#   Browse Cache        #
#########################

def get_browse_cache(path):
    """
    Get cached browse result for a path.

    Args:
        path: Directory path

    Returns:
        Dictionary with browse result, or None if not cached
    """
    try:
        import json

        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()
        c.execute('''
            SELECT result, updated_at
            FROM browse_cache
            WHERE path = ?
        ''', (path,))

        row = c.fetchone()
        conn.close()

        if row:
            return json.loads(row['result'])
        return None

    except Exception as e:
        app_logger.error(f"Failed to get browse cache for '{path}': {e}")
        return None

def save_browse_cache(path, result):
    """
    Save browse result to cache with retry logic for database locks.

    Args:
        path: Directory path
        result: Dictionary with browse result

    Returns:
        True if successful, False otherwise
    """
    import json
    import time

    max_retries = 3
    retry_delay = 0.5  # seconds

    for attempt in range(max_retries):
        try:
            conn = get_db_connection()
            if not conn:
                return False

            c = conn.cursor()
            c.execute('''
                INSERT OR REPLACE INTO browse_cache (path, result, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (path, json.dumps(result)))

            conn.commit()
            conn.close()

            app_logger.debug(f"Saved browse cache for: {path}")
            return True

        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                app_logger.warning(f"Database locked, retrying save_browse_cache for '{path}' (attempt {attempt + 1}/{max_retries})")
                time.sleep(retry_delay * (attempt + 1))  # Exponential backoff
                continue
            app_logger.error(f"Failed to save browse cache for '{path}': {e}")
            return False
        except Exception as e:
            app_logger.error(f"Failed to save browse cache for '{path}': {e}")
            return False

    return False

def invalidate_browse_cache(path):
    """
    Invalidate browse cache for a specific path.

    Args:
        path: Directory path to invalidate

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()

        # Delete the specific path
        c.execute('DELETE FROM browse_cache WHERE path = ?', (path,))

        # Also delete parent path (so parent sees new subdirectory)
        parent = os.path.dirname(path)
        if parent:
            c.execute('DELETE FROM browse_cache WHERE path = ?', (parent,))

        # Delete any child paths
        c.execute('DELETE FROM browse_cache WHERE path LIKE ?', (f"{path}/%",))

        conn.commit()
        rows_affected = c.rowcount
        conn.close()

        if rows_affected > 0:
            app_logger.debug(f"Invalidated {rows_affected} browse cache entries for: {path}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to invalidate browse cache for '{path}': {e}")
        return False

def clear_browse_cache():
    """
    Clear all browse cache entries.

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('DELETE FROM browse_cache')

        conn.commit()
        count = c.rowcount
        conn.close()

        app_logger.info(f"Cleared browse cache ({count} entries)")
        return True

    except Exception as e:
        app_logger.error(f"Failed to clear browse cache: {e}")
        return False


# =============================================================================
# Favorite Publishers CRUD Operations (using publishers table)
# =============================================================================

def set_publisher_favorite(publisher_path, favorite=True):
    """
    Set or unset a publisher as favorite by path.
    If the publisher doesn't exist, creates it with just the path.

    Args:
        publisher_path: Full path to the publisher folder
        favorite: True to favorite, False to unfavorite

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        favorite_val = 1 if favorite else 0

        # Check if publisher exists by path
        c.execute('SELECT id FROM publishers WHERE path = ?', (publisher_path,))
        existing = c.fetchone()

        if existing:
            # Update existing publisher
            c.execute('UPDATE publishers SET favorite = ? WHERE path = ?', (favorite_val, publisher_path))
        else:
            # Create new publisher with just path (no Metron ID)
            # Use negative autoincrement for local-only publishers
            c.execute('SELECT MIN(id) FROM publishers')
            min_id = c.fetchone()[0]
            new_id = (min_id - 1) if min_id and min_id < 0 else -1

            # Extract name from path (last folder name)
            import os
            name = os.path.basename(publisher_path.rstrip('/\\')) or publisher_path

            c.execute('''
                INSERT INTO publishers (id, name, path, favorite, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (new_id, name, publisher_path, favorite_val))

        conn.commit()
        conn.close()

        action = "Added" if favorite else "Removed"
        app_logger.info(f"{action} favorite publisher: {publisher_path}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to set favorite publisher '{publisher_path}': {e}")
        return False


def add_favorite_publisher(publisher_path):
    """
    Add a publisher to favorites.
    Wrapper for set_publisher_favorite for backwards compatibility.

    Args:
        publisher_path: Full path to the publisher folder

    Returns:
        True if successful, False otherwise
    """
    return set_publisher_favorite(publisher_path, favorite=True)


def remove_favorite_publisher(publisher_path):
    """
    Remove a publisher from favorites.
    Wrapper for set_publisher_favorite for backwards compatibility.

    Args:
        publisher_path: Full path to the publisher folder

    Returns:
        True if successful, False otherwise
    """
    return set_publisher_favorite(publisher_path, favorite=False)


def get_favorite_publishers():
    """
    Get all favorite publishers.

    Returns:
        List of dicts with publisher_path, name, and created_at, or empty list on error
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()
        c.execute('''
            SELECT id, name, path as publisher_path, created_at
            FROM publishers
            WHERE favorite = 1 AND path IS NOT NULL
            ORDER BY path
        ''')
        rows = c.fetchall()
        conn.close()

        return [dict(row) for row in rows]

    except Exception as e:
        app_logger.error(f"Failed to get favorite publishers: {e}")
        return []


def is_favorite_publisher(publisher_path):
    """
    Check if a publisher is favorited.

    Args:
        publisher_path: Full path to the publisher folder

    Returns:
        True if favorited, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('SELECT favorite FROM publishers WHERE path = ?', (publisher_path,))
        result = c.fetchone()
        conn.close()

        return result is not None and result[0] == 1

    except Exception as e:
        app_logger.error(f"Failed to check favorite publisher '{publisher_path}': {e}")
        return False


def get_publisher_by_path(publisher_path):
    """
    Get a publisher by its local path.

    Args:
        publisher_path: Full path to the publisher folder

    Returns:
        Dict with publisher info, or None if not found
    """
    try:
        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()
        c.execute('SELECT id, name, path, favorite, created_at FROM publishers WHERE path = ?', (publisher_path,))
        row = c.fetchone()
        conn.close()

        return dict(row) if row else None

    except Exception as e:
        app_logger.error(f"Failed to get publisher by path '{publisher_path}': {e}")
        return None


def save_publisher_path(publisher_path, name=None, publisher_id=None):
    """
    Save or update a publisher by path. Can optionally link to Metron publisher ID.

    Args:
        publisher_path: Full path to the publisher folder
        name: Optional publisher name (extracted from path if not provided)
        publisher_id: Optional Metron publisher ID to link with

    Returns:
        True if successful, False otherwise
    """
    try:
        import os
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()

        # Extract name from path if not provided
        if not name:
            name = os.path.basename(publisher_path.rstrip('/\\')) or publisher_path

        if publisher_id:
            # Update existing Metron publisher with path
            c.execute('''
                UPDATE publishers SET path = ?, name = COALESCE(?, name)
                WHERE id = ?
            ''', (publisher_path, name, publisher_id))

            if c.rowcount == 0:
                # Publisher doesn't exist, create it
                c.execute('''
                    INSERT INTO publishers (id, name, path, created_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ''', (publisher_id, name, publisher_path))
        else:
            # Check if already exists by path
            c.execute('SELECT id FROM publishers WHERE path = ?', (publisher_path,))
            existing = c.fetchone()

            if existing:
                c.execute('UPDATE publishers SET name = ? WHERE path = ?', (name, publisher_path))
            else:
                # Create new local-only publisher with negative ID
                c.execute('SELECT MIN(id) FROM publishers')
                min_id = c.fetchone()[0]
                new_id = (min_id - 1) if min_id and min_id < 0 else -1

                c.execute('''
                    INSERT INTO publishers (id, name, path, created_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ''', (new_id, name, publisher_path))

        conn.commit()
        conn.close()

        app_logger.info(f"Saved publisher path: {publisher_path}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to save publisher path '{publisher_path}': {e}")
        return False


# =============================================================================
# Favorite Series CRUD Operations
# =============================================================================

def add_favorite_series(series_path):
    """
    Add a series to favorites.

    Args:
        series_path: Full path to the series folder

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            INSERT OR IGNORE INTO favorite_series (series_path)
            VALUES (?)
        ''', (series_path,))

        conn.commit()
        conn.close()

        app_logger.info(f"Added favorite series: {series_path}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to add favorite series '{series_path}': {e}")
        return False


def remove_favorite_series(series_path):
    """
    Remove a series from favorites.

    Args:
        series_path: Full path to the series folder

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('DELETE FROM favorite_series WHERE series_path = ?', (series_path,))

        conn.commit()
        conn.close()

        app_logger.info(f"Removed favorite series: {series_path}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to remove favorite series '{series_path}': {e}")
        return False


def get_favorite_series():
    """
    Get all favorite series.

    Returns:
        List of dicts with series_path and created_at, or empty list on error
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()
        c.execute('SELECT series_path, created_at FROM favorite_series ORDER BY series_path')
        rows = c.fetchall()
        conn.close()

        return [dict(row) for row in rows]

    except Exception as e:
        app_logger.error(f"Failed to get favorite series: {e}")
        return []


def is_favorite_series(series_path):
    """
    Check if a series is favorited.

    Args:
        series_path: Full path to the series folder

    Returns:
        True if favorited, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('SELECT 1 FROM favorite_series WHERE series_path = ?', (series_path,))
        result = c.fetchone()
        conn.close()

        return result is not None

    except Exception as e:
        app_logger.error(f"Failed to check favorite series '{series_path}': {e}")
        return False


# =============================================================================
# Issues Read CRUD Operations
# =============================================================================

def mark_issue_read(issue_path, read_at=None, page_count=0, time_spent=0,
                    writer='', penciller='', characters='', publisher=''):
    """
    Mark an issue as read.

    Args:
        issue_path: Full path to the issue file
        read_at: Optional ISO timestamp string (e.g. "2024-01-15T14:30:00").
                 If None, uses CURRENT_TIMESTAMP.
        page_count: Number of pages in the issue (optional)
        time_spent: Time spent reading in seconds (optional)
        writer: Writer(s) from ComicInfo.xml (comma-separated if multiple)
        penciller: Penciller(s) from ComicInfo.xml (comma-separated if multiple)
        characters: Characters from ComicInfo.xml (comma-separated)
        publisher: Publisher from ComicInfo.xml

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()

        if read_at:
            c.execute('''
                INSERT OR REPLACE INTO issues_read
                (issue_path, read_at, page_count, time_spent, writer, penciller, characters, publisher)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (issue_path, read_at, page_count, time_spent, writer, penciller, characters, publisher))
        else:
            c.execute('''
                INSERT OR REPLACE INTO issues_read
                (issue_path, read_at, page_count, time_spent, writer, penciller, characters, publisher)
                VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?)
            ''', (issue_path, page_count, time_spent, writer, penciller, characters, publisher))

        conn.commit()
        conn.close()

        app_logger.info(f"Marked issue as read: {issue_path}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to mark issue as read '{issue_path}': {e}")
        return False


def unmark_issue_read(issue_path):
    """
    Remove read status from an issue.

    Args:
        issue_path: Full path to the issue file

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('DELETE FROM issues_read WHERE issue_path = ?', (issue_path,))

        conn.commit()
        conn.close()

        app_logger.info(f"Unmarked issue as read: {issue_path}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to unmark issue as read '{issue_path}': {e}")
        return False


def get_issues_read():
    """
    Get all read issues.

    Returns:
        List of dicts with issue_path and read_at, or empty list on error
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()
        c.execute('SELECT issue_path, read_at, page_count, time_spent FROM issues_read ORDER BY read_at DESC')
        rows = c.fetchall()
        conn.close()

        return [dict(row) for row in rows]

    except Exception as e:
        app_logger.error(f"Failed to get read issues: {e}")
        return []


def get_reading_totals():
    """
    Get total pages read and total time spent.

    Returns:
        Dict with 'total_pages' and 'total_time' (in seconds)
    """
    try:
        conn = get_db_connection()
        if not conn:
            return {'total_pages': 0, 'total_time': 0}

        c = conn.cursor()
        c.execute('SELECT SUM(page_count), SUM(time_spent) FROM issues_read')
        row = c.fetchone()
        conn.close()

        total_pages = row[0] if row and row[0] else 0
        total_time = row[1] if row and row[1] else 0

        return {'total_pages': total_pages, 'total_time': total_time}

    except Exception as e:
        app_logger.error(f"Failed to get reading totals: {e}")
        return {'total_pages': 0, 'total_time': 0}


def get_reading_stats_by_year(year=None):
    """
    Get reading statistics, optionally filtered by year.

    Args:
        year: Year to filter by (e.g., 2024), or None for all time

    Returns:
        Dict with 'total_read', 'total_pages', 'total_time' (in seconds)
    """
    try:
        conn = get_db_connection()
        if not conn:
            return {'total_read': 0, 'total_pages': 0, 'total_time': 0}

        c = conn.cursor()

        if year:
            # Filter by year
            c.execute('''
                SELECT COUNT(*), COALESCE(SUM(page_count), 0), COALESCE(SUM(time_spent), 0)
                FROM issues_read
                WHERE strftime('%Y', read_at) = ?
            ''', (str(year),))
        else:
            # All time
            c.execute('SELECT COUNT(*), COALESCE(SUM(page_count), 0), COALESCE(SUM(time_spent), 0) FROM issues_read')

        row = c.fetchone()
        conn.close()

        return {
            'total_read': row[0] if row else 0,
            'total_pages': row[1] if row else 0,
            'total_time': row[2] if row else 0
        }

    except Exception as e:
        app_logger.error(f"Failed to get reading stats by year: {e}")
        return {'total_read': 0, 'total_pages': 0, 'total_time': 0}


def get_reading_trends(field_name, year=None, limit=10):
    """
    Get top values for a metadata field (writer, penciller, characters, publisher).
    Splits comma-separated values and counts each occurrence.

    Args:
        field_name: Column name ('writer', 'penciller', 'characters', 'publisher')
        year: Optional year to filter by (e.g., 2024)
        limit: Maximum number of results to return (default 10)

    Returns:
        List of dicts: [{'name': 'Batman', 'count': 42}, ...]
    """
    # Validate field name to prevent SQL injection
    valid_fields = ['writer', 'penciller', 'characters', 'publisher']
    if field_name not in valid_fields:
        app_logger.warning(f"Invalid field name for reading trends: {field_name}")
        return []

    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()

        # Query all non-empty values for the field
        if year:
            c.execute(f'''
                SELECT {field_name} FROM issues_read
                WHERE {field_name} != '' AND {field_name} IS NOT NULL
                AND strftime('%Y', read_at) = ?
            ''', (str(year),))
        else:
            c.execute(f'''
                SELECT {field_name} FROM issues_read
                WHERE {field_name} != '' AND {field_name} IS NOT NULL
            ''')

        rows = c.fetchall()
        conn.close()

        # Count occurrences of each value (splitting comma-separated)
        counts = {}
        for row in rows:
            value = row[0]
            if value:
                # Split by comma and count each individual value
                for item in value.split(','):
                    item = item.strip()
                    if item:
                        counts[item] = counts.get(item, 0) + 1

        # Sort by count descending and return top N
        sorted_items = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]
        return [{'name': name, 'count': count} for name, count in sorted_items]

    except Exception as e:
        app_logger.error(f"Failed to get reading trends for {field_name}: {e}")
        return []


def get_files_by_metadata(field_name, value, limit=50, offset=0):
    """
    Get comic files matching a specific metadata value from file_index.

    Args:
        field_name: 'writer', 'penciller', 'characters', 'publisher'
        value: The metadata value to search for (e.g., 'Stan Lee')
        limit: Results per page
        offset: Pagination offset

    Returns:
        Dict with 'files' list and 'total' count
    """
    field_mapping = {
        'writer': 'ci_writer',
        'penciller': 'ci_penciller',
        'characters': 'ci_characters',
        'publisher': 'ci_publisher'
    }

    if field_name not in field_mapping:
        app_logger.warning(f"Invalid field name for metadata browse: {field_name}")
        return {'files': [], 'total': 0}

    db_column = field_mapping[field_name]

    try:
        conn = get_db_connection()
        if not conn:
            return {'files': [], 'total': 0}

        c = conn.cursor()

        # Use LIKE with wildcards to handle comma-separated values
        like_pattern = f'%{value}%'

        # Get total count first
        c.execute(f'''
            SELECT COUNT(*) FROM file_index
            WHERE type = 'file'
            AND (LOWER(name) LIKE '%.cbz' OR LOWER(name) LIKE '%.cbr')
            AND {db_column} LIKE ?
        ''', (like_pattern,))
        total = c.fetchone()[0]

        # Get paginated results
        # Use CAST for numeric sorting of issue numbers (handles "8" before "18")
        c.execute(f'''
            SELECT name, path, size, ci_series, ci_number, ci_year, ci_publisher
            FROM file_index
            WHERE type = 'file'
            AND (LOWER(name) LIKE '%.cbz' OR LOWER(name) LIKE '%.cbr')
            AND {db_column} LIKE ?
            ORDER BY ci_series COLLATE NOCASE, CAST(ci_number AS INTEGER) ASC, ci_number ASC
            LIMIT ? OFFSET ?
        ''', (like_pattern, limit, offset))

        rows = c.fetchall()
        conn.close()

        files = []
        for row in rows:
            files.append({
                'name': row['name'],
                'path': row['path'],
                'size': row['size'],
                'series': row['ci_series'] or '',
                'number': row['ci_number'] or '',
                'year': row['ci_year'] or '',
                'publisher': row['ci_publisher'] or ''
            })

        return {'files': files, 'total': total}

    except Exception as e:
        app_logger.error(f"Failed to get files by metadata: {e}")
        return {'files': [], 'total': 0}


def get_files_by_metadata_grouped(field_name, value):
    """
    Get comic files matching a metadata value, grouped appropriately.

    For characters/publisher: Single-level grouping by series
    For writer/penciller: Nested grouping - Publisher -> Series -> Files

    Args:
        field_name: 'writer', 'penciller', 'characters', 'publisher'
        value: The metadata value to search for

    Returns:
        Dict with 'groups' list, 'total' count, and 'nested' flag
    """
    field_mapping = {
        'writer': 'ci_writer',
        'penciller': 'ci_penciller',
        'characters': 'ci_characters',
        'publisher': 'ci_publisher'
    }

    if field_name not in field_mapping:
        app_logger.warning(f"Invalid field name for metadata browse: {field_name}")
        return {'groups': [], 'total': 0, 'nested': False}

    search_column = field_mapping[field_name]
    # Writer/penciller use nested grouping (publisher -> series)
    use_nested = field_name in ('writer', 'penciller')

    try:
        conn = get_db_connection()
        if not conn:
            return {'groups': [], 'total': 0, 'nested': use_nested}

        c = conn.cursor()
        like_pattern = f'%{value}%'

        # Query all matching files, ordered for grouping
        # Use CAST for numeric sorting of issue numbers (handles "8" before "18")
        c.execute(f'''
            SELECT name, path, size, ci_series, ci_number, ci_year, ci_publisher
            FROM file_index
            WHERE type = 'file'
            AND (LOWER(name) LIKE '%.cbz' OR LOWER(name) LIKE '%.cbr')
            AND {search_column} LIKE ?
            ORDER BY ci_publisher COLLATE NOCASE, ci_series COLLATE NOCASE,
                     CAST(ci_number AS INTEGER) ASC, ci_number ASC
        ''', (like_pattern,))

        rows = c.fetchall()
        conn.close()

        total = len(rows)

        if use_nested:
            # Nested grouping: Publisher -> Series -> Files
            publishers_dict = {}
            for row in rows:
                publisher = row['ci_publisher'] or ''
                series = row['ci_series'] or ''

                if publisher not in publishers_dict:
                    publishers_dict[publisher] = {}

                if series not in publishers_dict[publisher]:
                    publishers_dict[publisher][series] = []

                publishers_dict[publisher][series].append({
                    'name': row['name'],
                    'path': row['path'],
                    'size': row['size'],
                    'series': series,
                    'number': row['ci_number'] or '',
                    'year': row['ci_year'] or '',
                    'publisher': publisher
                })

            # Convert to nested structure and sort
            groups = []
            for pub_name, series_dict in publishers_dict.items():
                series_list = [
                    {'name': s_name, 'count': len(files), 'files': files}
                    for s_name, files in series_dict.items()
                ]
                series_list.sort(key=lambda s: s['count'], reverse=True)

                pub_count = sum(s['count'] for s in series_list)
                groups.append({
                    'name': pub_name,
                    'count': pub_count,
                    'series': series_list
                })

            groups.sort(key=lambda g: g['count'], reverse=True)
            return {'groups': groups, 'total': total, 'nested': True}

        else:
            # Single-level grouping by series (for characters/publisher)
            groups_dict = {}
            for row in rows:
                series_name = row['ci_series'] or ''
                if series_name not in groups_dict:
                    groups_dict[series_name] = []

                groups_dict[series_name].append({
                    'name': row['name'],
                    'path': row['path'],
                    'size': row['size'],
                    'series': series_name,
                    'number': row['ci_number'] or '',
                    'year': row['ci_year'] or '',
                    'publisher': row['ci_publisher'] or ''
                })

            groups = [
                {'name': name, 'count': len(files), 'files': files}
                for name, files in groups_dict.items()
            ]
            groups.sort(key=lambda g: g['count'], reverse=True)

            return {'groups': groups, 'total': total, 'nested': False}

    except Exception as e:
        app_logger.error(f"Failed to get files by metadata grouped: {e}")
        return {'groups': [], 'total': 0}


def is_issue_read(issue_path):
    """
    Check if an issue has been read.

    Args:
        issue_path: Full path to the issue file

    Returns:
        True if read, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('SELECT 1 FROM issues_read WHERE issue_path = ?', (issue_path,))
        result = c.fetchone()
        conn.close()

        return result is not None

    except Exception as e:
        app_logger.error(f"Failed to check if issue is read '{issue_path}': {e}")
        return False


def get_issue_read_date(issue_path):
    """
    Get the date an issue was read.

    Args:
        issue_path: Full path to the issue file

    Returns:
        Read date as string, or None if not read or on error
    """
    try:
        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()
        c.execute('SELECT read_at FROM issues_read WHERE issue_path = ?', (issue_path,))
        result = c.fetchone()
        conn.close()

        return result['read_at'] if result else None

    except Exception as e:
        app_logger.error(f"Failed to get read date for issue '{issue_path}': {e}")
        return None


# =============================================================================
# To Read Functions
# =============================================================================

def add_to_read(path, item_type='file'):
    """
    Add an item to the 'to read' list.

    Args:
        path: Full path to the file or folder
        item_type: 'file' or 'folder'

    Returns:
        True on success, False on error
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            INSERT OR IGNORE INTO to_read (path, type)
            VALUES (?, ?)
        ''', (path, item_type))

        conn.commit()
        conn.close()

        app_logger.info(f"Added to 'to read': {path} ({item_type})")
        return True

    except Exception as e:
        app_logger.error(f"Failed to add to 'to read' '{path}': {e}")
        return False


def remove_to_read(path):
    """
    Remove an item from the 'to read' list.

    Args:
        path: Full path to the file or folder

    Returns:
        True on success, False on error
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('DELETE FROM to_read WHERE path = ?', (path,))

        conn.commit()
        conn.close()

        app_logger.info(f"Removed from 'to read': {path}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to remove from 'to read' '{path}': {e}")
        return False


def compute_display_name(path):
    """
    Compute a display name from a path.
    If the folder name is a version pattern (v2024, v2023, etc.),
    prepend the parent folder name.

    Example: /data/Image/Geiger/v2024 -> "Geiger v2024"
    """
    # Get the folder/file name from path
    name = os.path.basename(path.rstrip('/'))

    # Check if name matches version pattern (v followed by 4 digits)
    if re.match(r'^v\d{4}$', name, re.IGNORECASE):
        # Get parent folder name
        parent_path = os.path.dirname(path.rstrip('/'))
        parent_name = os.path.basename(parent_path)
        if parent_name and parent_name != 'data':
            return f"{parent_name} {name}"

    return name


def get_to_read_items(limit=None):
    """
    Get all 'to read' items.

    Args:
        limit: Optional limit on number of items returned

    Returns:
        List of dicts with path, type, name, created_at keys, or empty list on error
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()
        if limit:
            c.execute('SELECT path, type, created_at FROM to_read ORDER BY created_at DESC LIMIT ?', (limit,))
        else:
            c.execute('SELECT path, type, created_at FROM to_read ORDER BY created_at DESC')

        results = []
        for row in c.fetchall():
            item = dict(row)
            # Compute display name from path
            item['name'] = compute_display_name(item['path'])
            results.append(item)

        conn.close()

        return results

    except Exception as e:
        app_logger.error(f"Failed to get 'to read' items: {e}")
        return []


def is_to_read(path):
    """
    Check if an item is in the 'to read' list.

    Args:
        path: Full path to the file or folder

    Returns:
        True if in list, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('SELECT 1 FROM to_read WHERE path = ?', (path,))
        result = c.fetchone()
        conn.close()

        return result is not None

    except Exception as e:
        app_logger.error(f"Failed to check 'to read' status for '{path}': {e}")
        return False


# =============================================================================
# Stats Cache Functions
# =============================================================================

def get_cached_stats(key):
    """
    Get cached stats by key.

    Args:
        key: Cache key (e.g., 'library_stats', 'file_type_distribution')

    Returns:
        Cached value (parsed from JSON) or None if not found
    """
    try:
        import json

        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()
        c.execute('SELECT value FROM stats_cache WHERE key = ?', (key,))
        row = c.fetchone()
        conn.close()

        if row:
            return json.loads(row['value'])
        return None

    except Exception as e:
        app_logger.error(f"Failed to get cached stats for '{key}': {e}")
        return None


def save_cached_stats(key, value):
    """
    Save stats to cache.

    Args:
        key: Cache key
        value: Value to cache (will be JSON-encoded)

    Returns:
        True if successful, False otherwise
    """
    try:
        import json

        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO stats_cache (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (key, json.dumps(value)))

        conn.commit()
        conn.close()

        app_logger.debug(f"Saved stats cache for: {key}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to save stats cache for '{key}': {e}")
        return False


def clear_stats_cache():
    """
    Clear all cached stats.

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('DELETE FROM stats_cache')

        conn.commit()
        count = c.rowcount
        conn.close()

        app_logger.info(f"Cleared stats cache ({count} entries)")
        return True

    except Exception as e:
        app_logger.error(f"Failed to clear stats cache: {e}")
        return False


def clear_stats_cache_keys(keys):
    """
    Clear specific cache keys while preserving others.

    Args:
        keys: List of cache keys to invalidate (e.g., ['library_stats', 'reading_history'])

    Returns:
        True if successful, False otherwise
    """
    if not keys:
        return True

    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        placeholders = ','.join('?' * len(keys))
        c.execute(f'DELETE FROM stats_cache WHERE key IN ({placeholders})', keys)

        conn.commit()
        count = c.rowcount
        conn.close()

        app_logger.info(f"Cleared stats cache keys {keys} ({count} entries)")
        return True

    except Exception as e:
        app_logger.error(f"Failed to clear stats cache keys {keys}: {e}")
        return False


# =============================================================================
# User Preferences (key-value store for user settings)
# =============================================================================

def get_user_preference(key, default=None):
    """
    Get a user preference by key.

    Args:
        key: Preference key (e.g., 'dashboard_order', 'dashboard_hidden')
        default: Default value if key not found

    Returns:
        Stored value (parsed from JSON) or default if not found
    """
    try:
        import json

        conn = get_db_connection()
        if not conn:
            return default

        c = conn.cursor()
        c.execute('SELECT value FROM user_preferences WHERE key = ?', (key,))
        row = c.fetchone()
        conn.close()

        if row:
            return json.loads(row['value'])
        return default

    except Exception as e:
        app_logger.error(f"Failed to get user preference '{key}': {e}")
        return default


def set_user_preference(key, value, category='general'):
    """
    Save a user preference.

    Args:
        key: Preference key
        value: Value to store (will be JSON-encoded)
        category: Category for grouping preferences (e.g., 'dashboard', 'ui')

    Returns:
        True if successful, False otherwise
    """
    try:
        import json

        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO user_preferences (key, value, category, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ''', (key, json.dumps(value), category))

        conn.commit()
        conn.close()

        app_logger.debug(f"Saved user preference: {key}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to save user preference '{key}': {e}")
        return False


# =============================================================================
# Reading Positions (bookmark reading progress in comics)
# =============================================================================

def save_reading_position(comic_path, page_number, total_pages=None, time_spent=0):
    """
    Save or update reading position for a comic.

    Args:
        comic_path: Full path to the comic file
        page_number: Current page number (0-indexed)
        total_pages: Total pages in the comic (optional)
        time_spent: Total time spent reading in seconds (optional)

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO reading_positions (comic_path, page_number, total_pages, updated_at, time_spent)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
        ''', (comic_path, page_number, total_pages, time_spent))

        conn.commit()
        conn.close()

        app_logger.debug(f"Saved reading position: {comic_path} at page {page_number}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to save reading position for '{comic_path}': {e}")
        return False


def get_reading_position(comic_path):
    """
    Get saved reading position for a comic.

    Args:
        comic_path: Full path to the comic file

    Returns:
        Dict with page_number and total_pages, or None if not found
    """
    try:
        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()
        c.execute('''
            SELECT page_number, total_pages, updated_at, time_spent
            FROM reading_positions
            WHERE comic_path = ?
        ''', (comic_path,))

        row = c.fetchone()
        conn.close()

        if row:
            return {
                'page_number': row['page_number'],
                'total_pages': row['total_pages'],
                'updated_at': row['updated_at'],
                'time_spent': row['time_spent'] if 'time_spent' in row.keys() else 0
            }
        return None

    except Exception as e:
        app_logger.error(f"Failed to get reading position for '{comic_path}': {e}")
        return None


def delete_reading_position(comic_path):
    """
    Remove saved reading position for a comic.

    Args:
        comic_path: Full path to the comic file

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('DELETE FROM reading_positions WHERE comic_path = ?', (comic_path,))

        conn.commit()
        conn.close()

        app_logger.debug(f"Deleted reading position for: {comic_path}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to delete reading position for '{comic_path}': {e}")
        return False


def get_all_reading_positions():
    """
    Get all saved reading positions.

    Returns:
        List of dicts with comic_path, page_number, total_pages, updated_at
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()
        c.execute('''
            SELECT comic_path, page_number, total_pages, updated_at
            FROM reading_positions
            ORDER BY updated_at DESC
        ''')

        rows = c.fetchall()
        conn.close()

        return [dict(row) for row in rows]

    except Exception as e:
        app_logger.error(f"Failed to get all reading positions: {e}")
        return []


def get_continue_reading_items(limit=10):
    """
    Get comics with saved reading positions that are in-progress (not completed).

    Args:
        limit: Maximum number of items to return (default 10)

    Returns:
        List of dicts with comic_path, file_name, page_number, total_pages,
        updated_at, and progress_percent
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()
        c.execute('''
            SELECT rp.comic_path,
                   COALESCE(fi.name, rp.comic_path) as file_name,
                   rp.page_number,
                   rp.total_pages,
                   rp.updated_at
            FROM reading_positions rp
            LEFT JOIN file_index fi ON rp.comic_path = fi.path
            WHERE rp.page_number > 0
              AND rp.total_pages IS NOT NULL
              AND rp.total_pages > 0
              AND rp.page_number < rp.total_pages - 1
            ORDER BY rp.updated_at DESC
            LIMIT ?
        ''', (limit,))

        rows = c.fetchall()
        conn.close()

        items = []
        for row in rows:
            item = dict(row)
            # Calculate progress percentage
            if item['total_pages'] and item['total_pages'] > 0:
                item['progress_percent'] = round((item['page_number'] / item['total_pages']) * 100)
            else:
                item['progress_percent'] = 0
            # Extract just filename if full path
            if '/' in item['file_name']:
                item['file_name'] = item['file_name'].split('/')[-1]
            elif '\\' in item['file_name']:
                item['file_name'] = item['file_name'].split('\\')[-1]
            items.append(item)

        return items

    except Exception as e:
        app_logger.error(f"Failed to get continue reading items: {e}")
        return []


#########################
#   Reading Lists       #
#########################

def create_reading_list(name, source=None):
    """
    Create a new reading list.
    
    Args:
        name: Name of the reading list
        source: Source of the list (e.g., filename or URL)
        
    Returns:
        ID of the new reading list, or None on error
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'INSERT INTO reading_lists (name, source) VALUES (?, ?)',
            (name, source)
        )
        list_id = c.lastrowid
        conn.commit()
        conn.close()
        return list_id
    except Exception as e:
        app_logger.error(f"Error creating reading list: {str(e)}")
        return None

def add_reading_list_entry(list_id, data):
    """
    Add an entry to a reading list.
    
    Args:
        list_id: ID of the reading list
        data: Dictionary containing entry data (series, issue_number, etc.)
        
    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            INSERT INTO reading_list_entries 
            (reading_list_id, series, issue_number, volume, year, matched_file_path)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            list_id, 
            data.get('series'), 
            data.get('issue_number'), 
            data.get('volume'), 
            data.get('year'),
            data.get('matched_file_path')
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        app_logger.error(f"Error adding reading list entry: {str(e)}")
        return False

def get_reading_lists():
    """
    Get all reading lists.
    
    Returns:
        List of dictionaries containing reading list info
    """
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # Get lists with entry counts and read counts
        c.execute('''
            SELECT rl.*,
                   COUNT(DISTINCT rle.id) as entry_count,
                   COUNT(DISTINCT ir.id) as read_count
            FROM reading_lists rl
            LEFT JOIN reading_list_entries rle ON rl.id = rle.reading_list_id
            LEFT JOIN issues_read ir ON COALESCE(rle.manual_override_path, rle.matched_file_path) = ir.issue_path
            GROUP BY rl.id
            ORDER BY rl.created_at DESC
        ''')
        
        lists = [dict(row) for row in c.fetchall()]
        
        import json
        # Get covers for each list
        for lst in lists:
            c.execute('''
                SELECT COALESCE(manual_override_path, matched_file_path) as path
                FROM reading_list_entries
                WHERE reading_list_id = ?
                AND COALESCE(manual_override_path, matched_file_path) IS NOT NULL
                LIMIT 5
            ''', (lst['id'],))

            # Get valid covers from entries
            covers = [row['path'] for row in c.fetchall() if row['path']]

            # If there's a specific thumbnail set, ensure it's first
            if lst['thumbnail_path']:
                if lst['thumbnail_path'] in covers:
                    covers.remove(lst['thumbnail_path'])
                covers.insert(0, lst['thumbnail_path'])

            # Limit to 5 covers
            lst['covers'] = covers[:5]

            # Parse tags JSON
            try:
                lst['tags'] = json.loads(lst.get('tags') or '[]')
            except (json.JSONDecodeError, TypeError):
                lst['tags'] = []

        conn.close()
        return lists
    except Exception as e:
        app_logger.error(f"Error getting reading lists: {str(e)}")
        return []

def get_reading_list(list_id):
    """
    Get a specific reading list and its entries.
    
    Args:
        list_id: ID of the reading list
        
    Returns:
        Dictionary containing list info and entries, or None on error
    """
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # Get list info
        c.execute('SELECT * FROM reading_lists WHERE id = ?', (list_id,))
        list_row = c.fetchone()
        
        if not list_row:
            conn.close()
            return None
            
        result = dict(list_row)
        
        # Get entries
        c.execute('''
            SELECT * FROM reading_list_entries 
            WHERE reading_list_id = ? 
            ORDER BY id ASC
        ''', (list_id,))
        
        result['entries'] = [dict(row) for row in c.fetchall()]
        conn.close()
        return result
    except Exception as e:
        app_logger.error(f"Error getting reading list {list_id}: {str(e)}")
        return None

def update_reading_list_entry_match(entry_id, file_path):
    """
    Update the matched file for a reading list entry.

    Args:
        entry_id: ID of the entry
        file_path: Path to the matched file (or None to clear both paths)

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()
        if file_path is None:
            # Clear both manual override and auto-matched path
            c.execute(
                'UPDATE reading_list_entries SET manual_override_path = NULL, matched_file_path = NULL WHERE id = ?',
                (entry_id,)
            )
        else:
            # Set manual override
            c.execute(
                'UPDATE reading_list_entries SET manual_override_path = ? WHERE id = ?',
                (file_path, entry_id)
            )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        app_logger.error(f"Error updating reading list entry {entry_id}: {str(e)}")
        return False

def clear_thumbnail_if_matches_entry(list_id, entry_id):
    """
    Clear the list's thumbnail if it matches the entry's file path.

    Args:
        list_id: ID of the reading list
        entry_id: ID of the entry being cleared

    Returns:
        True if thumbnail was cleared, False otherwise
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Get entry's current file paths
        c.execute(
            'SELECT manual_override_path, matched_file_path FROM reading_list_entries WHERE id = ?',
            (entry_id,)
        )
        entry = c.fetchone()
        if not entry:
            conn.close()
            return False

        entry_path = entry[0] or entry[1]  # manual_override_path or matched_file_path
        if not entry_path:
            conn.close()
            return False

        # Get list's thumbnail
        c.execute('SELECT thumbnail_path FROM reading_lists WHERE id = ?', (list_id,))
        result = c.fetchone()
        if result and result[0] == entry_path:
            # Thumbnail matches entry being cleared, so clear it
            c.execute('UPDATE reading_lists SET thumbnail_path = NULL WHERE id = ?', (list_id,))
            conn.commit()
            conn.close()
            return True

        conn.close()
        return False
    except Exception as e:
        app_logger.error(f"Error checking/clearing thumbnail for list {list_id}: {str(e)}")
        return False


def delete_reading_list(list_id):
    """
    Delete a reading list and all its entries.

    Args:
        list_id: ID of the reading list

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Delete entries first, then the list
        c.execute('DELETE FROM reading_list_entries WHERE reading_list_id = ?', (list_id,))
        c.execute('DELETE FROM reading_lists WHERE id = ?', (list_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        app_logger.error(f"Error deleting reading list {list_id}: {str(e)}")
        return False


def cleanup_orphaned_reading_list_entries():
    """
    Delete reading_list_entries that reference non-existent reading lists.

    Returns:
        Number of orphaned entries deleted
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            DELETE FROM reading_list_entries
            WHERE reading_list_id NOT IN (SELECT id FROM reading_lists)
        ''')
        deleted_count = c.rowcount
        conn.commit()
        conn.close()
        if deleted_count > 0:
            app_logger.info(f"Cleaned up {deleted_count} orphaned reading list entries")
        return deleted_count
    except Exception as e:
        app_logger.error(f"Error cleaning up orphaned entries: {str(e)}")
        return 0


def update_reading_list_thumbnail(list_id, thumbnail_path):
    """
    Update the thumbnail for a reading list.

    Args:
        list_id: ID of the reading list
        thumbnail_path: Path to the comic file to use as thumbnail

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE reading_lists SET thumbnail_path = ? WHERE id = ?',
                  (thumbnail_path, list_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        app_logger.error(f"Error updating reading list thumbnail {list_id}: {str(e)}")
        return False


def update_reading_list_name(list_id, name):
    """
    Update the name of a reading list.

    Args:
        list_id: ID of the reading list
        name: New name for the reading list

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE reading_lists SET name = ? WHERE id = ?', (name, list_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        app_logger.error(f"Error updating reading list name {list_id}: {str(e)}")
        return False


def update_reading_list_tags(list_id, tags):
    """
    Update the tags for a reading list.

    Args:
        list_id: ID of the reading list
        tags: List of tag strings

    Returns:
        True if successful, False otherwise
    """
    import json
    try:
        conn = get_db_connection()
        c = conn.cursor()
        tags_json = json.dumps(tags) if tags else '[]'
        c.execute('UPDATE reading_lists SET tags = ? WHERE id = ?', (tags_json, list_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        app_logger.error(f"Error updating reading list tags {list_id}: {str(e)}")
        return False


def get_all_reading_list_tags():
    """
    Get all unique tags across all reading lists for autocomplete.

    Returns:
        List of unique tag strings
    """
    import json
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT tags FROM reading_lists WHERE tags IS NOT NULL AND tags != "[]"')
        rows = c.fetchall()
        conn.close()

        all_tags = set()
        for row in rows:
            try:
                tags = json.loads(row[0]) if row[0] else []
                all_tags.update(tags)
            except json.JSONDecodeError:
                pass

        return sorted(list(all_tags))
    except Exception as e:
        app_logger.error(f"Error getting all reading list tags: {str(e)}")
        return []

def get_recent_read_issues(limit=None):
    """
    Get the most recently read issues for recommendation context.

    Args:
        limit: Maximum number of issues to return (None = no limit)

    Returns:
        List of dictionaries with 'issue_path', 'series_name' (inferred), 'read_at'
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()

        # We need to extract series info. For now, we'll return the path and let the consumer process it,
        # or we can try to be smart about it.
        if limit:
            c.execute('''
                SELECT issue_path, read_at
                FROM issues_read
                ORDER BY read_at DESC
                LIMIT ?
            ''', (limit,))
        else:
            c.execute('''
                SELECT issue_path, read_at
                FROM issues_read
                ORDER BY read_at DESC
            ''')

        rows = c.fetchall()
        conn.close()

        results = []
        for row in rows:
            # Simple inference: get the parent folder name as series name
            path = row['issue_path']
            series_name = os.path.basename(os.path.dirname(path))

            results.append({
                'title': os.path.basename(path), # Filename as title
                'series': series_name,
                'path': path,
                'read_at': row['read_at']
            })

        return results
    except Exception as e:
        app_logger.error(f"Error fetching recent read issues: {e}")
        return []


# =============================================================================
# Metron Publishers CRUD Operations
# =============================================================================

def save_publisher(publisher_id, name, path=None, logo=None):
    """
    Save or update a publisher in the database.

    Args:
        publisher_id: Metron publisher ID
        name: Publisher name
        path: Optional local filesystem path
        logo: Optional path to logo image

    Returns:
        True if successful, False otherwise
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            INSERT INTO publishers (id, name, path, logo, created_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                path = COALESCE(excluded.path, publishers.path),
                logo = COALESCE(excluded.logo, publishers.logo)
        ''', (publisher_id, name, path, logo))

        conn.commit()
        app_logger.info(f"Saved publisher: {name} (ID: {publisher_id})")
        return True

    except Exception as e:
        app_logger.error(f"Failed to save publisher {publisher_id}: {e}")
        return False

    finally:
        if conn:
            conn.close()


def get_publisher(publisher_id):
    """
    Get a publisher by ID.

    Args:
        publisher_id: Metron publisher ID

    Returns:
        Dict with publisher info, or None if not found
    """
    try:
        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()
        c.execute('SELECT id, name, path, favorite, logo, created_at FROM publishers WHERE id = ?', (publisher_id,))
        row = c.fetchone()
        conn.close()

        return dict(row) if row else None

    except Exception as e:
        app_logger.error(f"Failed to get publisher {publisher_id}: {e}")
        return None


def get_all_publishers():
    """
    Get all publishers from the database.

    Returns:
        List of dicts with publisher info, or empty list on error
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()
        c.execute('''
            SELECT id, name, path, favorite, logo, created_at
            FROM publishers
            ORDER BY name
        ''')
        rows = c.fetchall()
        conn.close()

        return [dict(row) for row in rows]

    except Exception as e:
        app_logger.error(f"Failed to get all publishers: {e}")
        return []


def delete_publisher(publisher_id):
    """
    Delete a publisher from the database.

    Args:
        publisher_id: Publisher ID to delete

    Returns:
        True if successful, False otherwise
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('DELETE FROM publishers WHERE id = ?', (publisher_id,))
        conn.commit()

        if c.rowcount > 0:
            app_logger.info(f"Deleted publisher ID: {publisher_id}")
            return True
        else:
            app_logger.warning(f"Publisher ID {publisher_id} not found")
            return False

    except Exception as e:
        app_logger.error(f"Failed to delete publisher {publisher_id}: {e}")
        return False

    finally:
        if conn:
            conn.close()


def update_publisher_logo(publisher_id, logo_path):
    """
    Update the logo path for a publisher.

    Args:
        publisher_id: Publisher ID to update
        logo_path: Path to the logo image

    Returns:
        True if successful, False otherwise
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('UPDATE publishers SET logo = ? WHERE id = ?', (logo_path, publisher_id))
        conn.commit()

        if c.rowcount > 0:
            app_logger.info(f"Updated logo for publisher ID: {publisher_id}")
            return True
        else:
            app_logger.warning(f"Publisher ID {publisher_id} not found")
            return False

    except Exception as e:
        app_logger.error(f"Failed to update publisher logo {publisher_id}: {e}")
        return False

    finally:
        if conn:
            conn.close()


# =============================================================================
# Metron Series CRUD Operations
# =============================================================================

def save_series_mapping(series_data, mapped_path, cover_image=None):
    """
    Save a Metron series with its local directory mapping.

    Args:
        series_data: Dictionary with series data from Metron API
        mapped_path: Local directory path to map to
        cover_image: Optional cover image URL from first issue

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()

        # Extract publisher_id
        publisher = series_data.get('publisher', {})
        publisher_id = publisher.get('id') if isinstance(publisher, dict) else None

        # Handle status - can be string or object
        status = series_data.get('status')
        if isinstance(status, dict):
            status = status.get('name', str(status))

        # Handle imprint - can be string or object
        imprint = series_data.get('imprint')
        if isinstance(imprint, dict):
            imprint = imprint.get('name', str(imprint))

        # Convert URL fields to strings (mokkari returns Pydantic HttpUrl types)
        resource_url = series_data.get('resource_url')
        if resource_url:
            resource_url = str(resource_url)
        if cover_image:
            cover_image = str(cover_image)

        c.execute('''
            INSERT OR REPLACE INTO series
            (id, name, sort_name, volume, status, publisher_id, imprint,
             volume_year, year_end, desc, cv_id, gcd_id, resource_url, mapped_path,
             cover_image, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM series WHERE id = ?), CURRENT_TIMESTAMP),
                    CURRENT_TIMESTAMP)
        ''', (
            series_data.get('id'),
            series_data.get('name'),
            series_data.get('sort_name'),
            series_data.get('volume'),
            status,
            publisher_id,
            imprint,
            series_data.get('year_began'),  # stored as volume_year
            series_data.get('year_end'),
            series_data.get('desc'),
            series_data.get('cv_id'),
            series_data.get('gcd_id'),
            resource_url,
            mapped_path,
            cover_image,
            series_data.get('id')  # For the COALESCE subquery
        ))

        conn.commit()
        conn.close()

        app_logger.info(f"Saved series mapping: {series_data.get('name')} -> {mapped_path}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to save series mapping: {e}")
        return False


def update_series_desc(series_id, desc):
    """
    Update the description for a series.

    Args:
        series_id: Metron series ID
        desc: Description text to set

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            UPDATE series
            SET desc = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (desc, series_id))

        conn.commit()
        conn.close()

        app_logger.info(f"Updated description for series {series_id}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to update series description: {e}")
        return False


def get_series_mapping(series_id):
    """
    Get the mapped path for a series.

    Args:
        series_id: Metron series ID

    Returns:
        Mapped path string, or None if not mapped
    """
    try:
        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()
        c.execute('SELECT mapped_path FROM series WHERE id = ?', (series_id,))
        row = c.fetchone()
        conn.close()

        return row['mapped_path'] if row else None

    except Exception as e:
        app_logger.error(f"Failed to get series mapping for {series_id}: {e}")
        return None


def get_series_by_id(series_id):
    """
    Get full series info by ID.

    Args:
        series_id: Metron series ID

    Returns:
        Dict with series info, or None if not found
    """
    try:
        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()
        c.execute('''
            SELECT ms.*, p.name as publisher_name, ms.volume_year as year_began
            FROM series ms
            LEFT JOIN publishers p ON ms.publisher_id = p.id
            WHERE ms.id = ?
        ''', (series_id,))
        row = c.fetchone()
        conn.close()

        return dict(row) if row else None

    except Exception as e:
        app_logger.error(f"Failed to get series {series_id}: {e}")
        return None


def get_all_mapped_series():
    """
    Get all series that have been mapped to local directories.

    Returns:
        List of dicts with series info
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()
        c.execute('''
            SELECT ms.*, p.name as publisher_name, ms.volume_year as year_began
            FROM series ms
            LEFT JOIN publishers p ON ms.publisher_id = p.id
            WHERE ms.mapped_path IS NOT NULL AND ms.mapped_path != ''
            ORDER BY ms.name
        ''')
        rows = c.fetchall()
        conn.close()

        return [dict(row) for row in rows]

    except Exception as e:
        app_logger.error(f"Failed to get mapped series: {e}")
        return []


def normalize_series_name(name):
    """
    Normalize a series name for matching purposes.
    Handles variations like "Spider-Man" vs "Spiderman", "The Avengers" vs "Avengers".
    """
    import re
    if not name:
        return ""
    # Lowercase
    name = name.lower()
    # Remove leading articles
    name = re.sub(r'^(the|a|an)\s+', '', name)
    # Remove punctuation and special characters (keep alphanumerics and spaces)
    name = re.sub(r'[^a-z0-9\s]', '', name)
    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def get_tracked_series_lookup():
    """
    Get a lookup set of tracked series by (normalized_name, volume).
    Used for matching releases to tracked series without needing series ID.

    Returns:
        Set of (normalized_name, volume) tuples
    """
    try:
        conn = get_db_connection()
        if not conn:
            return set()

        c = conn.cursor()
        c.execute('''
            SELECT name, volume
            FROM series
            WHERE mapped_path IS NOT NULL
        ''')
        rows = c.fetchall()
        conn.close()

        lookup = set()
        for row in rows:
            normalized = normalize_series_name(row['name'])
            volume = row['volume'] or 0
            lookup.add((normalized, volume))

        return lookup

    except Exception as e:
        app_logger.error(f"Failed to get tracked series lookup: {e}")
        return set()


def remove_series_mapping(series_id):
    """
    Remove the mapping for a series (keeps series data, clears mapped_path).

    Args:
        series_id: Metron series ID

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            UPDATE series
            SET mapped_path = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (series_id,))

        conn.commit()
        conn.close()

        app_logger.info(f"Removed series mapping for ID: {series_id}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to remove series mapping for {series_id}: {e}")
        return False


# =============================================================================
# Issues CRUD Operations (Metron Issue Caching)
# =============================================================================

def save_issue(issue_data, series_id):
    """
    Save or update a single issue in the database.

    Args:
        issue_data: Dict with issue data from Metron API
        series_id: Metron series ID

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()

        # Extract issue ID
        issue_id = issue_data.get('id')
        if not issue_id:
            app_logger.error("Issue data missing 'id' field")
            return False

        c.execute('''
            INSERT OR REPLACE INTO issues
            (id, series_id, number, name, cover_date, store_date, image, resource_url, cv_id,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM issues WHERE id = ?), CURRENT_TIMESTAMP),
                    CURRENT_TIMESTAMP)
        ''', (
            issue_id,
            series_id,
            str(issue_data.get('number', '')),
            issue_data.get('issue_name') or issue_data.get('name'),
            issue_data.get('cover_date'),
            issue_data.get('store_date'),
            str(issue_data.get('image')) if issue_data.get('image') else None,
            str(issue_data.get('resource_url')) if issue_data.get('resource_url') else None,
            issue_data.get('cv_id'),
            issue_id
        ))

        conn.commit()
        conn.close()
        return True

    except Exception as e:
        app_logger.error(f"Failed to save issue {issue_data.get('id')}: {e}")
        return False


def save_issues_bulk(issues_list, series_id):
    """
    Save multiple issues in a single transaction.

    Args:
        issues_list: List of issue dicts from Metron API
        series_id: Metron series ID

    Returns:
        Number of issues saved, or -1 on error
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return -1

        c = conn.cursor()
        saved_count = 0

        for issue_data in issues_list:
            # Handle Pydantic models or dicts - convert first
            if hasattr(issue_data, 'model_dump'):
                issue_dict = issue_data.model_dump(mode='json')
            elif hasattr(issue_data, 'dict'):
                issue_dict = issue_data.dict()
            elif hasattr(issue_data, 'id'):
                # Object with attributes - convert to dict
                issue_dict = {
                    'id': getattr(issue_data, 'id', None),
                    'number': getattr(issue_data, 'number', ''),
                    'name': getattr(issue_data, 'issue_name', None) or getattr(issue_data, 'name', None),
                    'cover_date': getattr(issue_data, 'cover_date', None),
                    'store_date': getattr(issue_data, 'store_date', None),
                    'image': getattr(issue_data, 'image', None),
                    'resource_url': getattr(issue_data, 'resource_url', None),
                    'cv_id': getattr(issue_data, 'cv_id', None),
                }
            else:
                issue_dict = issue_data

            issue_id = issue_dict.get('id')
            if not issue_id:
                continue

            c.execute('''
                INSERT OR REPLACE INTO issues
                (id, series_id, number, name, cover_date, store_date, image, resource_url, cv_id,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                        COALESCE((SELECT created_at FROM issues WHERE id = ?), CURRENT_TIMESTAMP),
                        CURRENT_TIMESTAMP)
            ''', (
                issue_id,
                series_id,
                str(issue_dict.get('number', '')),
                issue_dict.get('issue_name') or issue_dict.get('name'),
                issue_dict.get('cover_date'),
                issue_dict.get('store_date'),
                str(issue_dict.get('image')) if issue_dict.get('image') else None,
                str(issue_dict.get('resource_url')) if issue_dict.get('resource_url') else None,
                issue_dict.get('cv_id'),
                issue_id
            ))
            saved_count += 1

        conn.commit()
        app_logger.info(f"Saved {saved_count} issues for series {series_id}")
        return saved_count

    except Exception as e:
        app_logger.error(f"Failed to save issues bulk for series {series_id}: {e}")
        return -1

    finally:
        if conn:
            conn.close()


def get_issues_for_series(series_id):
    """
    Get all cached issues for a series.

    Args:
        series_id: Metron series ID

    Returns:
        List of issue dicts, or empty list on error
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()
        c.execute('''
            SELECT id, series_id, number, name, cover_date, store_date, image, resource_url, cv_id,
                   created_at, updated_at
            FROM issues
            WHERE series_id = ?
            ORDER BY
                CASE WHEN number GLOB '[0-9]*' THEN CAST(number AS INTEGER) ELSE 999999 END,
                number
        ''', (series_id,))

        rows = c.fetchall()
        conn.close()

        return [dict(row) for row in rows]

    except Exception as e:
        app_logger.error(f"Failed to get issues for series {series_id}: {e}")
        return []


def get_issue_by_id(issue_id):
    """
    Get a single issue by ID.

    Args:
        issue_id: Metron issue ID

    Returns:
        Issue dict, or None if not found
    """
    try:
        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()
        c.execute('SELECT * FROM issues WHERE id = ?', (issue_id,))
        row = c.fetchone()
        conn.close()

        return dict(row) if row else None

    except Exception as e:
        app_logger.error(f"Failed to get issue {issue_id}: {e}")
        return None


def get_wanted_issues():
    """
    Get all released issues from mapped series that may be missing.
    Returns issues with store_date <= today (released) or NULL (unknown).

    Returns:
        List of issue dicts with series info, or empty list on error
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()
        c.execute('''
            SELECT i.*, s.name as series_name, s.volume as series_volume,
                   s.mapped_path, s.publisher_id
            FROM issues i
            JOIN series s ON i.series_id = s.id
            WHERE s.mapped_path IS NOT NULL
              AND (i.store_date <= date('now') OR i.store_date IS NULL)
            ORDER BY s.name, i.number
        ''')

        rows = c.fetchall()
        conn.close()

        return [dict(row) for row in rows]

    except Exception as e:
        app_logger.error(f"Failed to get wanted issues: {e}")
        return []


def update_series_sync_time(series_id, issue_count=None):
    """
    Update the last_synced_at timestamp for a series.

    Args:
        series_id: Metron series ID
        issue_count: Optional issue count to update

    Returns:
        True if successful, False otherwise
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        if issue_count is not None:
            c.execute('''
                UPDATE series
                SET last_synced_at = CURRENT_TIMESTAMP, issue_count = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (issue_count, series_id))
        else:
            c.execute('''
                UPDATE series
                SET last_synced_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (series_id,))

        conn.commit()
        app_logger.info(f"Updated sync time for series {series_id}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to update sync time for series {series_id}: {e}")
        return False

    finally:
        if conn:
            conn.close()


def get_series_needing_sync(hours=24):
    """
    Get mapped series that haven't been synced recently.

    Args:
        hours: Number of hours since last sync to consider stale

    Returns:
        List of series dicts needing sync
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()
        c.execute('''
            SELECT *, volume_year as year_began
            FROM series
            WHERE mapped_path IS NOT NULL
              AND (last_synced_at IS NULL
                   OR last_synced_at < datetime('now', ? || ' hours'))
            ORDER BY last_synced_at ASC NULLS FIRST
        ''', (f'-{hours}',))

        rows = c.fetchall()
        conn.close()

        return [dict(row) for row in rows]

    except Exception as e:
        app_logger.error(f"Failed to get series needing sync: {e}")
        return []


def delete_issues_for_series(series_id):
    """
    Delete all cached issues for a series.

    Args:
        series_id: Metron series ID

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('DELETE FROM issues WHERE series_id = ?', (series_id,))

        deleted = c.rowcount
        conn.commit()
        conn.close()

        app_logger.info(f"Deleted {deleted} issues for series {series_id}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to delete issues for series {series_id}: {e}")
        return False


def cleanup_stale_issues(series_id, valid_issue_ids):
    """
    Remove issues that no longer exist in API response.
    Used to clean up deleted issues without removing all and re-inserting.

    Args:
        series_id: Metron series ID
        valid_issue_ids: Set of issue IDs that should be kept
    """
    if not valid_issue_ids:
        return

    try:
        conn = get_db_connection()
        if not conn:
            return

        c = conn.cursor()
        placeholders = ','.join('?' * len(valid_issue_ids))
        c.execute(f'''
            DELETE FROM issues
            WHERE series_id = ? AND id NOT IN ({placeholders})
        ''', (series_id, *valid_issue_ids))

        deleted = c.rowcount
        conn.commit()
        conn.close()

        if deleted > 0:
            app_logger.info(f"Cleaned up {deleted} stale issue(s) for series {series_id}")

    except Exception as e:
        app_logger.error(f"Failed to cleanup stale issues for series {series_id}: {e}")


# =============================================================================
# Collection Status Cache Functions
# =============================================================================

def get_collection_status_for_series(series_id):
    """
    Get cached collection status for a series.

    Args:
        series_id: Metron series ID

    Returns:
        List of dicts with issue_id, issue_number, found, file_path, file_mtime, matched_via
        or None if no cache exists
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()
        c.execute('''
            SELECT issue_id, issue_number, found, file_path, file_mtime, matched_via
            FROM collection_status
            WHERE series_id = ?
        ''', (series_id,))

        rows = c.fetchall()
        return [dict(row) for row in rows] if rows else None
    except Exception as e:
        app_logger.error(f"Failed to get collection status for series {series_id}: {e}")
        return None
    finally:
        if conn:
            conn.close()


def save_collection_status_bulk(entries):
    """
    Save multiple collection status entries in a transaction.

    Args:
        entries: List of dicts with series_id, issue_id, issue_number, found, file_path, file_mtime, matched_via

    Returns:
        True if successful, False otherwise
    """
    if not entries:
        return True

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.executemany('''
            INSERT OR REPLACE INTO collection_status
            (series_id, issue_id, issue_number, found, file_path, file_mtime, matched_via, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', [(e['series_id'], e['issue_id'], e['issue_number'],
               e['found'], e['file_path'], e['file_mtime'], e['matched_via'])
              for e in entries])
        conn.commit()
        app_logger.debug(f"Saved {len(entries)} collection status entries")
        return True
    except Exception as e:
        app_logger.error(f"Failed to save collection status bulk: {e}")
        return False
    finally:
        if conn:
            conn.close()


def invalidate_collection_status_for_series(series_id):
    """
    Remove cached collection status for a series (triggers re-scan on next view).

    Args:
        series_id: Metron series ID
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return

        c = conn.cursor()
        c.execute('DELETE FROM collection_status WHERE series_id = ?', (series_id,))
        deleted = c.rowcount
        conn.commit()
        if deleted > 0:
            app_logger.debug(f"Invalidated {deleted} collection status entries for series {series_id}")
    except Exception as e:
        app_logger.error(f"Failed to invalidate collection status for series {series_id}: {e}")
    finally:
        if conn:
            conn.close()


def invalidate_collection_status_for_path(file_path):
    """
    Invalidate cache entries that reference a specific file path or directory.
    Called when files are added, removed, or modified.

    Args:
        file_path: Path to the file or directory that changed
    """
    import os
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return

        c = conn.cursor()

        # Get the directory (if file_path is a file, get its parent)
        if os.path.isfile(file_path):
            directory = os.path.dirname(file_path)
        else:
            directory = file_path

        # Find series with this mapped_path
        c.execute('SELECT id FROM series WHERE mapped_path = ?', (directory,))
        rows = c.fetchall()

        if rows:
            series_ids = [row[0] for row in rows]
            placeholders = ','.join('?' * len(series_ids))
            c.execute(f'DELETE FROM collection_status WHERE series_id IN ({placeholders})', series_ids)
            deleted = c.rowcount
            conn.commit()
            if deleted > 0:
                app_logger.debug(f"Invalidated {deleted} collection status entries for path {directory}")
    except Exception as e:
        app_logger.error(f"Failed to invalidate collection status for path {file_path}: {e}")
    finally:
        if conn:
            conn.close()


# =====================================================
# WANTED ISSUES CACHE FUNCTIONS
# =====================================================

def get_cached_wanted_issues():
    """
    Get all cached wanted issues from the database.

    Returns:
        List of dicts with issue and series info, sorted by store_date
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return []

        c = conn.cursor()
        c.execute('''
            SELECT id, series_id, issue_id, issue_number, issue_name,
                   store_date, cover_date, image, series_name, series_volume, cached_at
            FROM wanted_issues
            ORDER BY store_date ASC, series_name ASC, issue_number ASC
        ''')
        rows = c.fetchall()

        return [{
            'id': row[0],
            'series_id': row[1],
            'issue_id': row[2],
            'issue_number': row[3],
            'issue_name': row[4],
            'store_date': row[5],
            'cover_date': row[6],
            'image': row[7],
            'series_name': row[8],
            'series_volume': row[9],
            'cached_at': row[10]
        } for row in rows]
    except Exception as e:
        app_logger.error(f"Failed to get cached wanted issues: {e}")
        return []
    finally:
        if conn:
            conn.close()


def save_wanted_issues_for_series(series_id, series_name, series_volume, wanted_list):
    """
    Save wanted issues for a series to the cache.
    Replaces any existing entries for this series.

    Args:
        series_id: Metron series ID
        series_name: Series name
        series_volume: Series volume number
        wanted_list: List of issue dicts with id, number, name, store_date, cover_date, image
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        # Clear existing entries for this series
        c.execute('DELETE FROM wanted_issues WHERE series_id = ?', (series_id,))

        # Insert new entries
        if wanted_list:
            c.executemany('''
                INSERT INTO wanted_issues
                (series_id, issue_id, issue_number, issue_name, store_date, cover_date, image, series_name, series_volume, cached_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', [(series_id, i.get('id'), i.get('number'), i.get('name'),
                   i.get('store_date'), i.get('cover_date'), i.get('image'),
                   series_name, series_volume)
                  for i in wanted_list])

        conn.commit()
        app_logger.debug(f"Saved {len(wanted_list)} wanted issues for series {series_id}")
        return True
    except Exception as e:
        app_logger.error(f"Failed to save wanted issues for series {series_id}: {e}")
        return False
    finally:
        if conn:
            conn.close()


def clear_wanted_cache_for_series(series_id):
    """
    Clear wanted cache for a specific series.
    Called when downloads complete or series is synced.

    Args:
        series_id: Metron series ID
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return

        c = conn.cursor()
        c.execute('DELETE FROM wanted_issues WHERE series_id = ?', (series_id,))
        deleted = c.rowcount
        conn.commit()
        if deleted > 0:
            app_logger.debug(f"Cleared {deleted} wanted issues cache entries for series {series_id}")
    except Exception as e:
        app_logger.error(f"Failed to clear wanted cache for series {series_id}: {e}")
    finally:
        if conn:
            conn.close()


def clear_wanted_cache_all():
    """Clear all wanted issues cache."""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return

        c = conn.cursor()
        c.execute('DELETE FROM wanted_issues')
        deleted = c.rowcount
        conn.commit()
        if deleted > 0:
            app_logger.debug(f"Cleared all {deleted} wanted issues cache entries")
    except Exception as e:
        app_logger.error(f"Failed to clear wanted cache: {e}")
    finally:
        if conn:
            conn.close()


def get_wanted_cache_age():
    """
    Get the age of the oldest cache entry.

    Returns:
        String like "5 minutes ago" or None if cache is empty
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return None

        c = conn.cursor()
        c.execute('SELECT MIN(cached_at) FROM wanted_issues')
        row = c.fetchone()

        if not row or not row[0]:
            return None

        from datetime import datetime
        cached_at = datetime.fromisoformat(row[0].replace('Z', '+00:00')) if 'T' in row[0] else datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
        now = datetime.now()
        diff = now - cached_at

        seconds = diff.total_seconds()
        if seconds < 60:
            return "just now"
        elif seconds < 3600:
            minutes = int(seconds / 60)
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        elif seconds < 86400:
            hours = int(seconds / 3600)
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        else:
            days = int(seconds / 86400)
            return f"{days} day{'s' if days != 1 else ''} ago"
    except Exception as e:
        app_logger.error(f"Failed to get wanted cache age: {e}")
        return None
    finally:
        if conn:
            conn.close()


# ============================================================
# Issue Manual Status Functions (owned/skipped)
# ============================================================

def ensure_manual_status_table():
    """Ensure the issue_manual_status table exists (for existing databases)."""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS issue_manual_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                issue_number TEXT NOT NULL,
                status TEXT NOT NULL,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(series_id, issue_number),
                FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_issue_manual_status_series ON issue_manual_status(series_id)')
        conn.commit()
        return True
    except Exception as e:
        app_logger.error(f"Failed to ensure manual status table: {e}")
        return False
    finally:
        if conn:
            conn.close()


def get_manual_status_for_series(series_id):
    """
    Get all manually-marked issue statuses for a series.

    Args:
        series_id: Metron series ID

    Returns:
        Dict mapping issue_number to {status, notes} or empty dict
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return {}

        c = conn.cursor()
        c.execute('''
            SELECT issue_number, status, notes
            FROM issue_manual_status
            WHERE series_id = ?
        ''', (series_id,))

        rows = c.fetchall()
        result = {row['issue_number']: {'status': row['status'], 'notes': row['notes']} for row in rows}
        if result:
            app_logger.debug(f"Manual status for series {series_id}: {result}")
        return result
    except Exception as e:
        app_logger.error(f"Failed to get manual status for series {series_id}: {e}")
        return {}
    finally:
        if conn:
            conn.close()


def set_manual_status(series_id, issue_number, status, notes=None):
    """
    Set or update manual status for an issue.

    Args:
        series_id: Metron series ID
        issue_number: Issue number (as string)
        status: 'owned' or 'skipped'
        notes: Optional notes text

    Returns:
        True if successful, False otherwise
    """
    if status not in ('owned', 'skipped'):
        app_logger.error(f"Invalid manual status: {status}")
        return False

    # Ensure table exists (for existing databases)
    ensure_manual_status_table()

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            INSERT INTO issue_manual_status (series_id, issue_number, status, notes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(series_id, issue_number) DO UPDATE SET
                status = excluded.status,
                notes = excluded.notes,
                created_at = CURRENT_TIMESTAMP
        ''', (series_id, str(issue_number), status, notes))
        conn.commit()
        app_logger.info(f"Set manual status for series {series_id} issue {issue_number}: {status}")
        return True
    except Exception as e:
        app_logger.error(f"Failed to set manual status for series {series_id} issue {issue_number}: {e}")
        return False
    finally:
        if conn:
            conn.close()


def clear_manual_status(series_id, issue_number):
    """
    Clear manual status for an issue (revert to normal detection).

    Args:
        series_id: Metron series ID
        issue_number: Issue number (as string)

    Returns:
        True if successful, False otherwise
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return False

        c = conn.cursor()
        c.execute('''
            DELETE FROM issue_manual_status
            WHERE series_id = ? AND issue_number = ?
        ''', (series_id, str(issue_number)))
        deleted = c.rowcount
        conn.commit()
        if deleted > 0:
            app_logger.info(f"Cleared manual status for series {series_id} issue {issue_number}")
        return True
    except Exception as e:
        app_logger.error(f"Failed to clear manual status for series {series_id} issue {issue_number}: {e}")
        return False
    finally:
        if conn:
            conn.close()


def bulk_set_manual_status(series_id, issue_numbers, status, notes=None):
    """
    Set manual status for multiple issues at once.

    Args:
        series_id: Metron series ID
        issue_numbers: List of issue numbers (as strings)
        status: 'owned' or 'skipped'
        notes: Optional notes text (applies to all)

    Returns:
        Number of issues updated, or -1 on error
    """
    if status not in ('owned', 'skipped'):
        app_logger.error(f"Invalid manual status: {status}")
        return -1

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return -1

        c = conn.cursor()
        count = 0
        for issue_number in issue_numbers:
            c.execute('''
                INSERT INTO issue_manual_status (series_id, issue_number, status, notes)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(series_id, issue_number) DO UPDATE SET
                    status = excluded.status,
                    notes = excluded.notes,
                    created_at = CURRENT_TIMESTAMP
            ''', (series_id, str(issue_number), status, notes))
            count += 1
        conn.commit()
        app_logger.info(f"Bulk set manual status for series {series_id}: {count} issues marked as {status}")
        return count
    except Exception as e:
        app_logger.error(f"Failed to bulk set manual status for series {series_id}: {e}")
        return -1
    finally:
        if conn:
            conn.close()


# =============================================================================
# Provider Credential Functions
# =============================================================================

def save_provider_credentials(provider_type: str, credentials: dict) -> bool:
    """
    Save encrypted provider credentials to the database.

    Args:
        provider_type: Provider identifier (e.g., 'metron', 'comicvine')
        credentials: Dictionary of credential data

    Returns:
        True if saved successfully, False otherwise
    """
    try:
        from models.providers.crypto import encrypt_credentials, is_crypto_available

        if not is_crypto_available():
            app_logger.error("Cannot save credentials: cryptography library not available")
            return False

        ciphertext, nonce = encrypt_credentials(credentials)
        conn = get_db_connection()
        conn.execute('''
            INSERT INTO provider_credentials (provider_type, credentials_encrypted, credentials_nonce, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(provider_type) DO UPDATE SET
                credentials_encrypted = excluded.credentials_encrypted,
                credentials_nonce = excluded.credentials_nonce,
                updated_at = CURRENT_TIMESTAMP
        ''', (provider_type, ciphertext, nonce))
        conn.commit()
        conn.close()
        app_logger.info(f"Saved credentials for provider: {provider_type}")
        return True
    except Exception as e:
        app_logger.error(f"Failed to save provider credentials for {provider_type}: {e}")
        return False


def get_provider_credentials(provider_type: str) -> Optional[dict]:
    """
    Get decrypted provider credentials from the database.

    Args:
        provider_type: Provider identifier (e.g., 'metron', 'comicvine')

    Returns:
        Decrypted credentials dictionary, or None if not found
    """
    try:
        from models.providers.crypto import decrypt_credentials, is_crypto_available

        if not is_crypto_available():
            app_logger.error("Cannot get credentials: cryptography library not available")
            return None

        conn = get_db_connection()
        row = conn.execute('''
            SELECT credentials_encrypted, credentials_nonce
            FROM provider_credentials WHERE provider_type = ?
        ''', (provider_type,)).fetchone()
        conn.close()

        if not row:
            return None

        return decrypt_credentials(row['credentials_encrypted'], row['credentials_nonce'])
    except Exception as e:
        app_logger.error(f"Failed to get provider credentials for {provider_type}: {e}")
        return None


def get_provider_credentials_masked(provider_type: str) -> Optional[dict]:
    """
    Get masked provider credentials (safe for frontend display).

    Args:
        provider_type: Provider identifier

    Returns:
        Dictionary with masked credential values
    """
    try:
        from models.providers.crypto import mask_credentials_dict

        creds = get_provider_credentials(provider_type)
        if not creds:
            return None
        return mask_credentials_dict(creds)
    except Exception as e:
        app_logger.error(f"Failed to get masked credentials for {provider_type}: {e}")
        return None


def get_all_provider_credentials_status() -> list:
    """
    Get status of all configured providers (without decrypting credentials).

    Returns:
        List of dicts with provider_type, is_valid, last_tested
    """
    try:
        conn = get_db_connection()
        rows = conn.execute('''
            SELECT provider_type, is_valid, last_tested, updated_at
            FROM provider_credentials
            ORDER BY provider_type
        ''').fetchall()
        conn.close()

        return [dict(row) for row in rows]
    except Exception as e:
        app_logger.error(f"Failed to get provider credentials status: {e}")
        return []


def update_provider_validity(provider_type: str, is_valid: bool) -> bool:
    """
    Update provider connection test result.

    Args:
        provider_type: Provider identifier
        is_valid: Whether the last connection test was successful

    Returns:
        True if updated successfully
    """
    try:
        conn = get_db_connection()
        conn.execute('''
            UPDATE provider_credentials
            SET is_valid = ?, last_tested = CURRENT_TIMESTAMP
            WHERE provider_type = ?
        ''', (1 if is_valid else 0, provider_type))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        app_logger.error(f"Failed to update provider validity for {provider_type}: {e}")
        return False


def delete_provider_credentials(provider_type: str) -> bool:
    """
    Delete provider credentials from the database.

    Args:
        provider_type: Provider identifier

    Returns:
        True if deleted successfully
    """
    try:
        conn = get_db_connection()
        conn.execute('DELETE FROM provider_credentials WHERE provider_type = ?', (provider_type,))
        conn.commit()
        conn.close()
        app_logger.info(f"Deleted credentials for provider: {provider_type}")
        return True
    except Exception as e:
        app_logger.error(f"Failed to delete provider credentials for {provider_type}: {e}")
        return False


def register_provider_configured(provider_type: str, is_valid: bool = True) -> bool:
    """
    Register a provider as configured (for providers that don't require auth).

    Creates a record in provider_credentials with empty placeholder credentials,
    marking the provider as available for use.

    Args:
        provider_type: Provider identifier (e.g., 'anilist', 'bedetheque')
        is_valid: Whether the provider is valid/working

    Returns:
        True if registered successfully, False otherwise
    """
    try:
        from models.providers.crypto import encrypt_credentials, is_crypto_available

        if not is_crypto_available():
            app_logger.error("Cannot register provider: cryptography library not available")
            return False

        # Encrypt empty credentials as placeholder
        ciphertext, nonce = encrypt_credentials({"_configured": True})

        conn = get_db_connection()
        conn.execute('''
            INSERT INTO provider_credentials (provider_type, credentials_encrypted, credentials_nonce, is_valid, last_tested, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(provider_type) DO UPDATE SET
                is_valid = excluded.is_valid,
                last_tested = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
        ''', (provider_type, ciphertext, nonce, 1 if is_valid else 0))
        conn.commit()
        conn.close()
        app_logger.info(f"Registered provider as configured: {provider_type}")
        return True
    except Exception as e:
        app_logger.error(f"Failed to register provider {provider_type}: {e}")
        return False


# =============================================================================
# Library Provider Configuration Functions
# =============================================================================

def get_library_providers(library_id: int) -> list:
    """
    Get enabled providers for a library, ordered by priority.

    Args:
        library_id: Library ID

    Returns:
        List of provider configurations
    """
    try:
        conn = get_db_connection()
        rows = conn.execute('''
            SELECT provider_type, priority, enabled
            FROM library_providers
            WHERE library_id = ?
            ORDER BY priority ASC
        ''', (library_id,)).fetchall()
        conn.close()

        return [dict(row) for row in rows]
    except Exception as e:
        app_logger.error(f"Failed to get library providers for library {library_id}: {e}")
        return []


def set_library_providers(library_id: int, providers: list) -> bool:
    """
    Set the provider configuration for a library.

    Args:
        library_id: Library ID
        providers: List of dicts with provider_type, priority, enabled

    Returns:
        True if saved successfully
    """
    try:
        conn = get_db_connection()

        # Clear existing providers for this library
        conn.execute('DELETE FROM library_providers WHERE library_id = ?', (library_id,))

        # Insert new providers
        for p in providers:
            conn.execute('''
                INSERT INTO library_providers (library_id, provider_type, priority, enabled)
                VALUES (?, ?, ?, ?)
            ''', (library_id, p['provider_type'], p.get('priority', 0), p.get('enabled', 1)))

        conn.commit()
        conn.close()
        app_logger.info(f"Set {len(providers)} providers for library {library_id}")
        return True
    except Exception as e:
        app_logger.error(f"Failed to set library providers for library {library_id}: {e}")
        return False


def add_library_provider(library_id: int, provider_type: str, priority: int = 0, enabled: bool = True) -> bool:
    """
    Add a provider to a library.

    Args:
        library_id: Library ID
        provider_type: Provider identifier
        priority: Priority order (lower = higher priority)
        enabled: Whether the provider is enabled

    Returns:
        True if added successfully
    """
    try:
        conn = get_db_connection()
        conn.execute('''
            INSERT INTO library_providers (library_id, provider_type, priority, enabled)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(library_id, provider_type) DO UPDATE SET
                priority = excluded.priority,
                enabled = excluded.enabled
        ''', (library_id, provider_type, priority, 1 if enabled else 0))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        app_logger.error(f"Failed to add provider {provider_type} to library {library_id}: {e}")
        return False


def remove_library_provider(library_id: int, provider_type: str) -> bool:
    """
    Remove a provider from a library.

    Args:
        library_id: Library ID
        provider_type: Provider identifier

    Returns:
        True if removed successfully
    """
    try:
        conn = get_db_connection()
        conn.execute('''
            DELETE FROM library_providers
            WHERE library_id = ? AND provider_type = ?
        ''', (library_id, provider_type))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        app_logger.error(f"Failed to remove provider {provider_type} from library {library_id}: {e}")
        return False


#########################
#   Komga Sync Functions #
#########################

def get_komga_config():
    """
    Get Komga sync configuration from the database.
    Reads credentials/paths from komga_sync_config, schedule fields from schedules table.
    """
    try:
        conn = get_db_connection()
        if not conn:
            return None
        c = conn.cursor()
        c.execute('SELECT * FROM komga_sync_config WHERE id = 1')
        row = c.fetchone()
        conn.close()

        if not row:
            return None

        # Get schedule fields from unified table
        sched = get_schedule('komga')

        result = {
            'server_url': row['server_url'] or '',
            'username': '',
            'password': '',
            'path_prefix_komga': row['path_prefix_komga'] or '',
            'path_prefix_clu': row['path_prefix_clu'] or '',
            'enabled': bool(row['enabled']),
            'last_sync': sched['last_run'] if sched else row['last_sync'],
            'last_sync_read_count': row['last_sync_read_count'] or 0,
            'last_sync_progress_count': row['last_sync_progress_count'] or 0,
            'frequency': sched['frequency'] if sched else 'disabled',
            'time': sched['time'] if sched else '05:00',
            'weekday': sched['weekday'] if sched else 0,
        }

        # Decrypt credentials if present
        if row['credentials_encrypted'] and row['credentials_nonce']:
            try:
                from models.providers.crypto import decrypt_credentials, is_crypto_available
                if is_crypto_available():
                    creds = decrypt_credentials(
                        row['credentials_encrypted'],
                        row['credentials_nonce']
                    )
                    result['username'] = creds.get('username', '')
                    result['password'] = creds.get('password', '')
            except Exception as e:
                app_logger.error(f"Failed to decrypt Komga credentials: {e}")

        return result
    except Exception as e:
        app_logger.error(f"Failed to get Komga config: {e}")
        return None


def save_komga_config(server_url, username='', password='',
                      path_prefix_komga='', path_prefix_clu='',
                      enabled=False, frequency='disabled',
                      time='05:00', weekday=0):
    """
    Save Komga sync configuration to the database.
    Credentials/paths go to komga_sync_config, schedule fields go to schedules table.
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        # If password is empty, keep existing encrypted credentials
        credentials_encrypted = None
        credentials_nonce = None

        if username or password:
            # If password is provided (not masked), encrypt new credentials
            if password and password != '***':
                try:
                    from models.providers.crypto import encrypt_credentials, is_crypto_available
                    if is_crypto_available():
                        creds = {'username': username, 'password': password}
                        credentials_encrypted, credentials_nonce = encrypt_credentials(creds)
                except Exception as e:
                    app_logger.error(f"Failed to encrypt Komga credentials: {e}")
                    conn.close()
                    return False
            else:
                # Password is masked ('***'), keep existing credentials but update username
                c = conn.cursor()
                c.execute('SELECT credentials_encrypted, credentials_nonce FROM komga_sync_config WHERE id = 1')
                row = c.fetchone()
                if row and row['credentials_encrypted']:
                    # Decrypt, update username, re-encrypt
                    try:
                        from models.providers.crypto import decrypt_credentials, encrypt_credentials, is_crypto_available
                        if is_crypto_available():
                            existing_creds = decrypt_credentials(
                                row['credentials_encrypted'],
                                row['credentials_nonce']
                            )
                            existing_creds['username'] = username
                            credentials_encrypted, credentials_nonce = encrypt_credentials(existing_creds)
                    except Exception as e:
                        app_logger.error(f"Failed to update Komga credentials: {e}")
                        # Keep existing credentials
                        credentials_encrypted = row['credentials_encrypted']
                        credentials_nonce = row['credentials_nonce']
                else:
                    # No existing credentials and password is masked - skip
                    pass

        if credentials_encrypted is not None:
            conn.execute('''
                UPDATE komga_sync_config SET
                    server_url = ?,
                    credentials_encrypted = ?,
                    credentials_nonce = ?,
                    path_prefix_komga = ?,
                    path_prefix_clu = ?,
                    enabled = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
            ''', (server_url, credentials_encrypted, credentials_nonce,
                  path_prefix_komga, path_prefix_clu,
                  1 if enabled else 0))
        else:
            # Update everything except credentials
            conn.execute('''
                UPDATE komga_sync_config SET
                    server_url = ?,
                    path_prefix_komga = ?,
                    path_prefix_clu = ?,
                    enabled = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
            ''', (server_url, path_prefix_komga, path_prefix_clu,
                  1 if enabled else 0))

        conn.commit()
        conn.close()

        # Save schedule fields to unified table
        save_schedule('komga', frequency, time, weekday)

        app_logger.info("Komga config saved successfully")
        return True
    except Exception as e:
        app_logger.error(f"Failed to save Komga config: {e}")
        return False


def update_komga_last_sync(read_count=0, progress_count=0):
    """
    Update the last sync timestamp and counts.
    Timestamp goes to schedules table, counts go to komga_sync_config.
    """
    try:
        update_schedule_last_run('komga')

        conn = get_db_connection()
        if not conn:
            return
        conn.execute('''
            UPDATE komga_sync_config SET
                last_sync_read_count = ?,
                last_sync_progress_count = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
        ''', (read_count, progress_count))
        conn.commit()
        conn.close()
    except Exception as e:
        app_logger.error(f"Failed to update Komga last sync: {e}")


def is_komga_book_synced(komga_book_id, sync_type='read'):
    """
    Check if a Komga book has already been synced.

    Args:
        komga_book_id: Komga book ID
        sync_type: 'read' or 'progress'

    Returns:
        True if already synced
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False
        c = conn.cursor()
        c.execute('''
            SELECT 1 FROM komga_sync_log
            WHERE komga_book_id = ? AND sync_type = ?
        ''', (komga_book_id, sync_type))
        result = c.fetchone() is not None
        conn.close()
        return result
    except Exception as e:
        app_logger.error(f"Failed to check Komga sync status: {e}")
        return False


def mark_komga_book_synced(komga_book_id, komga_path, clu_path, sync_type='read'):
    """
    Record that a Komga book has been synced to CLU.

    Args:
        komga_book_id: Komga book ID
        komga_path: File path as seen by Komga
        clu_path: Matched file path in CLU
        sync_type: 'read' or 'progress'

    Returns:
        True on success
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False
        conn.execute('''
            INSERT OR REPLACE INTO komga_sync_log
                (komga_book_id, komga_path, clu_path, sync_type, synced_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (komga_book_id, komga_path, clu_path, sync_type))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        app_logger.error(f"Failed to mark Komga book synced: {e}")
        return False


def get_komga_sync_stats():
    """
    Get Komga sync statistics.

    Returns:
        Dict with total_synced_read, total_synced_progress, last_sync
    """
    try:
        conn = get_db_connection()
        if not conn:
            return {'total_synced_read': 0, 'total_synced_progress': 0, 'last_sync': None}

        c = conn.cursor()

        c.execute("SELECT COUNT(*) FROM komga_sync_log WHERE sync_type = 'read'")
        total_read = c.fetchone()[0]

        c.execute("SELECT COUNT(*) FROM komga_sync_log WHERE sync_type = 'progress'")
        total_progress = c.fetchone()[0]

        c.execute('SELECT last_sync FROM komga_sync_config WHERE id = 1')
        row = c.fetchone()
        last_sync = row['last_sync'] if row else None

        conn.close()
        return {
            'total_synced_read': total_read,
            'total_synced_progress': total_progress,
            'last_sync': last_sync
        }
    except Exception as e:
        app_logger.error(f"Failed to get Komga sync stats: {e}")
        return {'total_synced_read': 0, 'total_synced_progress': 0, 'last_sync': None}
