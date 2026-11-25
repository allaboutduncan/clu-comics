"""Storage for the Kavita reading sync.

Three things are kept: the per-library path prefixes (site-wide, set by the
owner), each CLU user's own Kavita auth key (``kavita_user_links``), and a log of
which Kavita chapters have been imported for which user. The server URL is the
``KAVITA_SERVER_URL`` setting.

A Kavita auth key answers only for the Kavita user it belongs to, so everything
about a link is keyed on the CLU user id. No function here returns a key except
``get_link``, which the sync and the connection test call; routes use
``get_link_status``.
"""
import json

from core.app_logging import app_logger
from core.database import db_conn
from core.settings import get_setting, set_setting

_STATUS_COLUMNS = (
    "user_id, kavita_username, connection_valid, last_sync, "
    "last_sync_read_count, last_sync_progress_count, last_error, "
    "last_sync_detail, updated_at"
)

# What a run found, kept so the pages can explain a run that imported nothing:
# "already in CLU" and "not found in CLU" both read as 0 imported otherwise.
DETAIL_KEYS = (
    "chapters", "matched", "already", "no_match", "ambiguous", "no_access",
    "unusable", "errors", "series", "series_skipped",
)


def get_server_url():
    return (get_setting("KAVITA_SERVER_URL") or "").strip().rstrip("/")


def set_server_url(url):
    return set_setting("KAVITA_SERVER_URL", (url or "").strip().rstrip("/"))


# ---------------------------------------------------------------------------
# Library path mappings
# ---------------------------------------------------------------------------

def get_library_mappings():
    """Every enabled library with its Kavita path prefix ('' when unset).

    Returns:
        List of dicts: {library_id, library_name, library_path, kavita_path_prefix}
    """
    try:
        with db_conn() as conn:
            if not conn:
                return []
            rows = conn.execute("""
                SELECT l.id AS library_id, l.name AS library_name, l.path AS library_path,
                       COALESCE(m.kavita_path_prefix, '') AS kavita_path_prefix
                FROM libraries l
                LEFT JOIN kavita_library_mappings m ON l.id = m.library_id
                WHERE l.enabled = 1
                ORDER BY l.name
            """).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        app_logger.error(f"Failed to get Kavita library mappings: {e}")
        return []


def save_library_mappings(mappings):
    """Replace the mappings with the non-empty prefixes in `mappings`.

    Args:
        mappings: List of dicts with 'library_id' and 'kavita_path_prefix'
    """
    try:
        rows = []
        for m in mappings or []:
            prefix = (m.get("kavita_path_prefix") or "").strip()
            if prefix:
                rows.append((int(m["library_id"]), prefix))
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        app_logger.error(f"Kavita library mappings rejected: {e}")
        return False
    try:
        with db_conn() as conn:
            if not conn:
                return False
            conn.execute("DELETE FROM kavita_library_mappings")
            conn.executemany(
                "INSERT INTO kavita_library_mappings (library_id, kavita_path_prefix, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                rows,
            )
            conn.commit()
        return True
    except Exception as e:
        app_logger.error(f"Failed to save Kavita library mappings: {e}")
        return False


# ---------------------------------------------------------------------------
# Per-user links
# ---------------------------------------------------------------------------

def save_link(user_id, auth_key, kavita_username=None):
    """Store (or replace) a user's Kavita auth key. A saved key starts valid."""
    try:
        from models.providers.crypto import encrypt_credentials, is_crypto_available

        if not is_crypto_available():
            app_logger.error("Cannot store a Kavita auth key: cryptography is unavailable")
            return False
        ciphertext, nonce = encrypt_credentials({"auth_key": auth_key})
        with db_conn() as conn:
            if not conn:
                return False
            conn.execute(
                """
                INSERT INTO kavita_user_links
                    (user_id, credentials_encrypted, credentials_nonce, kavita_username,
                     connection_valid, last_error, updated_at)
                VALUES (?, ?, ?, ?, 1, NULL, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    credentials_encrypted = excluded.credentials_encrypted,
                    credentials_nonce = excluded.credentials_nonce,
                    kavita_username = excluded.kavita_username,
                    connection_valid = 1,
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, ciphertext, nonce, kavita_username),
            )
            conn.commit()
        return True
    except Exception as e:
        app_logger.error(f"Failed to save Kavita link for user {user_id}: {e}")
        return False


def get_link(user_id):
    """A user's link **including the decrypted auth key**, or None.

    For the sync and the connection test only. Never return this from a route.
    """
    try:
        with db_conn() as conn:
            if not conn:
                return None
            row = conn.execute(
                f"SELECT {_STATUS_COLUMNS}, credentials_encrypted, credentials_nonce "
                "FROM kavita_user_links WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        link = _status_dict(row)
        link["auth_key"] = ""
        if row["credentials_encrypted"] and row["credentials_nonce"]:
            from models.providers.crypto import decrypt_credentials, is_crypto_available

            if is_crypto_available():
                creds = decrypt_credentials(
                    row["credentials_encrypted"], row["credentials_nonce"]
                )
                link["auth_key"] = creds.get("auth_key", "")
        return link
    except Exception as e:
        app_logger.error(f"Failed to read Kavita link for user {user_id}: {e}")
        return None


def _status_dict(row):
    return {
        "user_id": row["user_id"],
        "kavita_username": row["kavita_username"] or "",
        "connection_valid": bool(row["connection_valid"]),
        "last_sync": row["last_sync"],
        "last_sync_read_count": row["last_sync_read_count"] or 0,
        "last_sync_progress_count": row["last_sync_progress_count"] or 0,
        "last_error": row["last_error"] or "",
        "detail": _load_detail(row["last_sync_detail"]),
    }


def _load_detail(raw):
    """The stored counts as {key: int}, or {} when there are none."""
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: int(data[k]) for k in DETAIL_KEYS if isinstance(data.get(k), int)}


def get_link_status(user_id):
    """A user's link without the key, or None when they have not linked."""
    try:
        with db_conn() as conn:
            if not conn:
                return None
            row = conn.execute(
                f"SELECT {_STATUS_COLUMNS} FROM kavita_user_links WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return _status_dict(row) if row else None
    except Exception as e:
        app_logger.error(f"Failed to read Kavita link status for user {user_id}: {e}")
        return None


def list_links():
    """Every linked user, without keys, with the CLU username.

    Returns:
        List of status dicts plus 'username', ordered by username.
    """
    try:
        with db_conn() as conn:
            if not conn:
                return []
            rows = conn.execute(
                """
                SELECT k.user_id, k.kavita_username, k.connection_valid, k.last_sync,
                       k.last_sync_read_count, k.last_sync_progress_count,
                       k.last_error, k.last_sync_detail, k.updated_at,
                       u.username AS username
                FROM kavita_user_links k
                JOIN users u ON u.id = k.user_id
                ORDER BY u.username COLLATE NOCASE
                """
            ).fetchall()
        out = []
        for row in rows:
            status = _status_dict(row)
            status["username"] = row["username"]
            out.append(status)
        return out
    except Exception as e:
        app_logger.error(f"Failed to list Kavita links: {e}")
        return []


def delete_link(user_id):
    """Forget a user's key. Their import log stays: it is what stops a read
    they un-marked from coming back if they link again."""
    try:
        with db_conn() as conn:
            if not conn:
                return False
            conn.execute("DELETE FROM kavita_user_links WHERE user_id = ?", (user_id,))
            conn.commit()
        return True
    except Exception as e:
        app_logger.error(f"Failed to delete Kavita link for user {user_id}: {e}")
        return False


def set_link_invalid(user_id, error):
    """Kavita rejected the key. The schedule leaves this user alone until they
    save a key again."""
    return _update_link(
        user_id,
        "connection_valid = 0, last_error = ?, updated_at = CURRENT_TIMESTAMP",
        (error,),
    )


def record_sync_error(user_id, error):
    """A run failed for a reason other than the key. ``last_sync`` is not moved."""
    return _update_link(
        user_id, "last_error = ?, updated_at = CURRENT_TIMESTAMP", (error,)
    )


def record_sync_result(user_id, read_count, progress_count, started_at, detail=None):
    """A run finished. `started_at` is when it began (``YYYY-MM-DD HH:MM:SS``
    UTC), so progress made in Kavita while it ran is picked up next time.
    `detail` is the run's counts (``DETAIL_KEYS``)."""
    counts = {k: int(v) for k, v in (detail or {}).items() if k in DETAIL_KEYS}
    return _update_link(
        user_id,
        "last_sync = ?, last_sync_read_count = ?, last_sync_progress_count = ?, "
        "last_sync_detail = ?, last_error = NULL, updated_at = CURRENT_TIMESTAMP",
        (started_at, read_count, progress_count, json.dumps(counts)),
    )


def _update_link(user_id, assignments, params):
    try:
        with db_conn() as conn:
            if not conn:
                return False
            conn.execute(
                f"UPDATE kavita_user_links SET {assignments} WHERE user_id = ?",
                (*params, user_id),
            )
            conn.commit()
        return True
    except Exception as e:
        app_logger.error(f"Failed to update Kavita link for user {user_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# Import log and the writes the sync makes
# ---------------------------------------------------------------------------

def synced_chapter_ids(user_id, sync_type="read"):
    """Kavita chapter ids already imported for this user, as a set of strings.

    Returns None when the log could not be read. The caller must not treat that
    as "nothing imported": it would re-import reads the user has since un-marked.
    """
    try:
        with db_conn() as conn:
            if not conn:
                return None
            rows = conn.execute(
                "SELECT kavita_chapter_id FROM kavita_sync_log "
                "WHERE user_id = ? AND sync_type = ?",
                (user_id, sync_type),
            ).fetchall()
        return {str(r[0]) for r in rows}
    except Exception as e:
        app_logger.error(f"Failed to read the Kavita import log for user {user_id}: {e}")
        return None


def log_synced(user_id, chapter_id, kavita_path, clu_path, sync_type="read"):
    try:
        with db_conn() as conn:
            if not conn:
                return False
            conn.execute(
                """
                INSERT OR REPLACE INTO kavita_sync_log
                    (user_id, kavita_chapter_id, kavita_path, clu_path, sync_type, synced_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (user_id, str(chapter_id), kavita_path, clu_path, sync_type),
            )
            conn.commit()
        return True
    except Exception as e:
        app_logger.error(f"Failed to log a Kavita import for user {user_id}: {e}")
        return False


def clear_sync_log():
    """Forget every import. Chapter ids belong to one Kavita server, so the log
    means nothing once the server URL points somewhere else."""
    try:
        with db_conn() as conn:
            if not conn:
                return False
            conn.execute("DELETE FROM kavita_sync_log")
            conn.commit()
        return True
    except Exception as e:
        app_logger.error(f"Failed to clear the Kavita import log: {e}")
        return False


# How many imported reads with no credits one run tries to fill in.
MAX_CREDIT_FILLS_PER_RUN = 50

_NO_CREDITS = ("", "", "", "")


def credits_for(issue_path):
    """``(writer, penciller, characters, publisher)`` for a comic.

    The timeline and Insights read these off the ``issues_read`` row, and the
    reader fills them from ComicInfo when it marks an issue read; an imported
    read has to do the same or it shows as "Unknown Publisher". The file index
    is asked first because it costs no file I/O; the archive is read only when
    the index has nothing. Blank strings when neither knows.
    """
    from core.metadata_normalize import normalize_credit_list, strip_provider_ids

    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT ci_writer, ci_penciller, ci_characters, ci_publisher "
                "FROM file_index WHERE path = ?",
                (issue_path,),
            ).fetchone() if conn else None
        if row and any(row[i] for i in range(4)):
            return (
                normalize_credit_list(row[0] or ""),
                normalize_credit_list(row[1] or ""),
                normalize_credit_list(row[2] or ""),
                strip_provider_ids(row[3] or ""),
            )
    except Exception as e:
        app_logger.debug(f"Kavita sync: no indexed credits for '{issue_path}': {e}")

    try:
        # The reading-history backfill's own reader, so both fill a row the
        # same way. It opens no database connection.
        from core.reading_metadata_backfill import _metadata_for

        values, _reason = _metadata_for(issue_path)
        if values:
            return tuple(v or "" for v in values)
    except Exception as e:
        app_logger.debug(f"Kavita sync: no ComicInfo credits for '{issue_path}': {e}")
    return _NO_CREDITS


def fill_blank_credits(user_id, limit=MAX_CREDIT_FILLS_PER_RUN):
    """Give imported reads that have no credits at all the ones their file has.

    Covers reads imported before credits were filled at import, and files
    tagged since. Only rows this sync logged, and only when all four fields are
    blank, so nothing a user or the reader wrote is replaced.

    Returns:
        How many rows were filled.
    """
    try:
        with db_conn() as conn:
            if not conn:
                return 0
            paths = [r[0] for r in conn.execute(
                """
                SELECT r.issue_path FROM issues_read r
                WHERE r.user_id = ?
                  AND COALESCE(r.writer, '') = '' AND COALESCE(r.penciller, '') = ''
                  AND COALESCE(r.characters, '') = '' AND COALESCE(r.publisher, '') = ''
                  AND r.issue_path IN (
                      SELECT clu_path FROM kavita_sync_log
                      WHERE user_id = ? AND sync_type = 'read')
                ORDER BY r.read_at DESC
                LIMIT ?
                """,
                (user_id, user_id, limit),
            ).fetchall()]
    except Exception as e:
        app_logger.error(f"Kavita sync: could not list reads without credits: {e}")
        return 0

    # No connection is open while the files are read.
    updates = []
    for path in paths:
        credits = credits_for(path)
        if credits != _NO_CREDITS:
            updates.append((*credits, user_id, path))
    if not updates:
        return 0
    try:
        with db_conn() as conn:
            if not conn:
                return 0
            conn.executemany(
                "UPDATE issues_read SET writer = ?, penciller = ?, characters = ?, "
                "publisher = ? WHERE user_id = ? AND issue_path = ?",
                updates,
            )
            conn.commit()
        return len(updates)
    except Exception as e:
        app_logger.error(f"Kavita sync: could not fill credits: {e}")
        return 0


def import_read(issue_path, user_id, read_at=None, page_count=0, credits=None):
    """Record a read that happened in Kavita, unless CLU already has one.

    ``INSERT OR IGNORE``, not ``mark_issue_read``'s ``OR REPLACE``: a read CLU
    recorded itself keeps its date, time spent and credits. `credits` is
    ``credits_for(issue_path)``; the caller reads it before this opens a
    connection.

    Returns:
        True when a row was added, False when one existed, None on failure.
    """
    writer, penciller, characters, publisher = credits or _NO_CREDITS
    try:
        with db_conn() as conn:
            if not conn:
                return None
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO issues_read
                    (user_id, issue_path, read_at, page_count, time_spent,
                     writer, penciller, characters, publisher)
                VALUES (?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?, 0, ?, ?, ?, ?)
                """,
                (user_id, issue_path, read_at, page_count or 0,
                 writer, penciller, characters, publisher),
            )
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        app_logger.error(f"Failed to import a Kavita read for '{issue_path}': {e}")
        return None
