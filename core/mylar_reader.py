"""Read a Mylar3 install: its ``mylar.db`` watchlist and its ``config.ini``.

Pure parsing. Nothing here touches Flask or CLU's own database, so every
quirk of Mylar's formats is unit-testable in isolation; the import itself
(path mapping, settings, Pull List writes) lives in ``models/mylar_import.py``.

Facts about Mylar these functions are built on (all from the mylar3 source):

- ``comics.ComicID`` is the bare ComicVine volume id. Installs that once used
  GCD carry ``G``-prefixed ids, which ComicVine cannot resolve.
- Columns arrive through ``ALTER TABLE`` upgrades, so an older database lacks
  some of them. Every read goes through the columns that actually exist.
- Mylar writes the string ``'None'`` where it means NULL.
- ``config.ini`` is written by configparser: keys are lowercase, ``%`` is
  escaped as ``%%`` and, with MINIMAL_INI, any key at its default is absent.
- With ``encrypt_passwords`` on, a secret is ``^~$z$`` + base64(plaintext +
  8 salt bytes). That is an encoding, not encryption, and it is reversible.
- ``extra_newznabs`` is one flat string of ``", "``-joined fields, seven per
  provider (six before config_version 11, which added the id).
"""

import base64
import configparser
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

ENCODED_PREFIX = "^~$z$"
_SALT_BYTES = 8

# Mylar's [Client] nzb_downloader.
NZB_DOWNLOADERS = {0: "sabnzbd", 1: "nzbget", 2: "blackhole", 3: None}

# Mylar's issue statuses. NULL is treated as Skipped, as Mylar's UI does.
ISSUE_STATUSES = (
    "Wanted", "Skipped", "Snatched", "Downloaded", "Archived", "Failed", "Ignored",
)

DEFAULT_NEWZNAB_CATEGORIES = "7030"


class MylarReadError(Exception):
    """The file is not something this module can read as Mylar's."""


def _clean(value):
    """Mylar's NULL: None, blank, or the literal string 'None'."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == "None":
        return None
    return text


def _to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_bool(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def decode_secret(value):
    """Undo Mylar's ``encrypt_passwords`` encoding; pass anything else through.

    A value that carries the prefix but does not decode is returned as-is:
    a user who typed a password beginning ``^~$z$`` is far likelier than a
    corrupt config, and handing back the original loses nothing.
    """
    if not isinstance(value, str) or not value.startswith(ENCODED_PREFIX):
        return value
    try:
        raw = base64.b64decode(value[len(ENCODED_PREFIX):], validate=True)
        if len(raw) < _SALT_BYTES:
            return value
        return raw[:-_SALT_BYTES].decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return value


# ---------------------------------------------------------------------------
# mylar.db
# ---------------------------------------------------------------------------

def open_mylar_db(path):
    """Open ``mylar.db`` read-only.

    Callers pass a private copy (``models.mylar_import`` stages one), never
    Mylar's live file: Mylar's database is not in WAL mode, so a reader there
    can take ``database is locked`` mid-import, and a read-write open could
    create a journal under Mylar's feet.
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    except sqlite3.DatabaseError as e:
        # connect() succeeds lazily; the first query is where a non-database
        # fails, with a live handle that would keep the staged file locked.
        if conn is not None:
            conn.close()
        raise MylarReadError(f"Not a readable SQLite database: {e}") from e
    if "comics" not in tables:
        conn.close()
        raise MylarReadError("This database has no 'comics' table; is it Mylar's mylar.db?")
    return conn


def _columns(conn, table):
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.DatabaseError:
        return set()


def _select(conn, table, wanted):
    """SELECT the subset of ``wanted`` columns the table actually has.

    Missing columns come back as None, so callers can read every name.
    """
    have = _columns(conn, table)
    present = [c for c in wanted if c in have]
    if not present:
        return []
    rows = conn.execute(f"SELECT {', '.join(present)} FROM {table}").fetchall()
    return [{c: (row[c] if c in have else None) for c in wanted} for row in rows]


def _series_year(row):
    """``Corrected_SeriesYear`` is the user's override of ``ComicYear``."""
    for key in ("Corrected_SeriesYear", "ComicYear"):
        year = _to_int(_clean(row.get(key)))
        if year and 1800 < year < 3000:
            return year
    return None


def read_watchlist(conn):
    """Return ``{"series": [...], "excluded": [...], "annual_count": n}``.

    Each series is ``{cv_id, name, year, publisher, location, status, type,
    total, have, issues: [{number, status}]}``. ``status`` is ``Active`` or
    ``Paused`` (``Loading`` is a series Mylar was still adding: Active).
    ``excluded`` lists rows that cannot be imported, each with a reason.
    """
    comics = _select(conn, "comics", [
        "ComicID", "ComicName", "ComicYear", "Corrected_SeriesYear",
        "ComicPublisher", "ComicLocation", "Status", "Type", "Corrected_Type",
        "Total", "Have",
    ])

    issues_by_comic = {}
    if _columns(conn, "issues"):
        for row in _select(conn, "issues", ["ComicID", "Issue_Number", "Status"]):
            comic_id = _clean(row["ComicID"])
            number = _clean(row["Issue_Number"])
            if not comic_id or number is None:
                continue
            status = _clean(row["Status"]) or "Skipped"
            issues_by_comic.setdefault(comic_id, []).append(
                {"number": number, "status": status}
            )

    annual_count = 0
    if _columns(conn, "annuals"):
        try:
            annual_count = conn.execute("SELECT COUNT(*) FROM annuals").fetchone()[0]
        except sqlite3.DatabaseError:
            annual_count = 0

    series = []
    excluded = []
    for row in comics:
        comic_id = _clean(row["ComicID"])
        name = _clean(row["ComicName"]) or (f"ComicVine {comic_id}" if comic_id else "Unknown")
        if not comic_id:
            excluded.append({"name": name, "reason": "No ComicID"})
            continue
        cv_id = _to_int(comic_id)
        if cv_id is None or cv_id <= 0:
            reason = (
                "Grand Comics Database id (ComicVine cannot resolve it)"
                if comic_id.upper().startswith("G")
                else f"Unrecognised ComicID '{comic_id}'"
            )
            excluded.append({"name": name, "reason": reason})
            continue

        status = _clean(row["Status"]) or "Active"
        series.append({
            "cv_id": cv_id,
            "name": name,
            "year": _series_year(row),
            "publisher": _clean(row["ComicPublisher"]),
            "location": _clean(row["ComicLocation"]),
            "status": "Paused" if status.lower() == "paused" else "Active",
            "type": _clean(row["Corrected_Type"]) or _clean(row["Type"]),
            "total": _to_int(row["Total"]),
            "have": _to_int(row["Have"]),
            "issues": issues_by_comic.get(comic_id, []),
        })

    series.sort(key=lambda s: (s["name"].lower(), s["year"] or 0))
    return {"series": series, "excluded": excluded, "annual_count": annual_count}


def read_mylar_db(path):
    """Open, read and close ``mylar.db`` at ``path`` (see :func:`read_watchlist`)."""
    conn = open_mylar_db(path)
    try:
        return read_watchlist(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# config.ini
# ---------------------------------------------------------------------------

def parse_client_url(url, port=None):
    """Split a Mylar client URL into CLU's ``host``/``port``/``use_ssl``/``url_base``.

    Mylar stores SABnzbd as one URL (``http://sab:8080/sabnzbd``) and NZBGet as
    a URL plus a separate port. A bare host is taken as http.
    """
    url = _clean(url)
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    try:
        url_port = parts.port
    except ValueError:
        url_port = None
    if not parts.hostname:
        return None
    return {
        "host": parts.hostname,
        "port": url_port or _to_int(port),
        "use_ssl": parts.scheme.lower() == "https",
        "url_base": parts.path.strip("/") or None,
    }


# Mylar keeps priorities as the client's own labels; CLU stores the number.
_SAB_PRIORITIES = {
    "paused": -2, "low": -1, "normal": 0, "high": 1, "force": 2,
}
_NZBGET_PRIORITIES = {
    "very low": -100, "low": -50, "normal": 0, "high": 50,
    "very high": 100, "force": 900,
}


def _priority(value, table):
    text = (_clean(value) or "").lower()
    if text in table:
        return table[text]
    return _to_int(text)  # "Default" and anything unknown -> None


def parse_extra_newznabs(raw, config_version=None):
    """Parse ``[Newznab] extra_newznabs`` into a list of providers.

    Fields per provider: name, host, verify, apikey, uid[#cat#cat...], enabled
    and (from config_version 11) id. The categories are everything after the
    first ``#`` in the uid field.

    Returns ``(providers, error)``. ``error`` is set when the token count does
    not divide into whole providers, which happens when a value itself
    contains ``", "``; the providers that did parse are still returned.
    """
    raw = _clean(raw)
    if not raw:
        return [], None
    tokens = raw.split(", ")

    version = _to_int(config_version)
    width = 6 if version is not None and version < 11 else 7
    error = None
    if len(tokens) % width:
        other = 13 - width  # 6 <-> 7
        if len(tokens) % other == 0:
            width = other
        else:
            error = (
                f"extra_newznabs has {len(tokens)} fields, not a multiple of "
                f"{width}; some providers could not be read"
            )

    providers = []
    for i in range(0, len(tokens) - width + 1, width):
        fields = tokens[i:i + width]
        name = _clean(fields[0]) or ""
        for flag in ("[local]", "[nzbhydra]"):
            if name.lower().endswith(flag):
                name = name[: -len(flag)].strip()
        url = _clean(decode_secret(fields[1]))
        if not url:
            continue
        uid_field = _clean(decode_secret(fields[4])) or ""
        cats = ""
        if "#" in uid_field:
            cats = ",".join(c for c in uid_field.split("#", 1)[1].split("#") if c.strip())
        providers.append({
            "name": name or url,
            "url": url,
            "api_key": _clean(decode_secret(fields[3])),
            "categories": cats or DEFAULT_NEWZNAB_CATEGORIES,
            "enabled": _to_bool(fields[5]),
        })
    return providers, error


def _count_extra(raw, width=7):
    tokens = (_clean(raw) or "").split(", ")
    return len(tokens) // width if tokens != [""] else 0


def read_config(path):
    """Parse Mylar's ``config.ini`` into the settings CLU can import.

    Every secret is passed through :func:`decode_secret`. Keys missing from the
    file (MINIMAL_INI) come back as None.
    """
    parser = configparser.RawConfigParser(strict=False)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            parser.read_file(f)
    except (configparser.Error, OSError) as e:
        raise MylarReadError(f"Could not read config.ini: {e}") from e
    if not parser.sections():
        raise MylarReadError("config.ini has no sections; is it Mylar's config.ini?")

    def get(section, key, secret=False):
        if not parser.has_option(section, key):
            return None
        value = parser.get(section, key).replace("%%", "%")
        if secret:
            value = decode_secret(value)
        return _clean(value)

    config_version = _to_int(get("General", "config_version"))

    sab = None
    sab_url = parse_client_url(get("SABnzbd", "sab_host"))
    if sab_url:
        sab = {
            **sab_url,
            "api_key": get("SABnzbd", "sab_apikey", secret=True),
            "username": get("SABnzbd", "sab_username"),
            "password": get("SABnzbd", "sab_password", secret=True),
            "category": get("SABnzbd", "sab_category"),
            "priority": _priority(get("SABnzbd", "sab_priority"), _SAB_PRIORITIES),
        }

    nzbget = None
    nzbget_url = parse_client_url(get("NZBGet", "nzbget_host"), get("NZBGet", "nzbget_port"))
    if nzbget_url:
        sub = get("NZBGet", "nzbget_sub")
        if sub and not nzbget_url["url_base"]:
            nzbget_url["url_base"] = sub.strip("/") or None
        nzbget = {
            **nzbget_url,
            "username": get("NZBGet", "nzbget_username"),
            "password": get("NZBGet", "nzbget_password", secret=True),
            "category": get("NZBGet", "nzbget_category"),
            "priority": _priority(get("NZBGet", "nzbget_priority"), _NZBGET_PRIORITIES),
        }

    newznabs, newznab_error = parse_extra_newznabs(
        get("Newznab", "extra_newznabs"), config_version
    )
    # The section's master switch turns every provider off in Mylar.
    if parser.has_option("Newznab", "newznab") and not _to_bool(get("Newznab", "newznab")):
        for provider in newznabs:
            provider["enabled"] = False

    return {
        "config_version": config_version,
        "destination_dir": get("General", "destination_dir"),
        "multiple_dest_dirs": get("General", "multiple_dest_dirs"),
        "folder_format": get("General", "folder_format"),
        "comicvine_api": get("CV", "comicvine_api", secret=True),
        "nzb_downloader": NZB_DOWNLOADERS.get(_to_int(get("Client", "nzb_downloader"))),
        "sabnzbd": sab,
        "nzbget": nzbget,
        "newznabs": newznabs,
        "newznab_error": newznab_error,
        "torznab_count": _count_extra(get("Torznab", "extra_torznabs")),
        "torrents_enabled": _to_bool(get("Torrents", "enable_torrents")),
    }
