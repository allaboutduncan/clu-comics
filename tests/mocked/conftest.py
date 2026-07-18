"""Shared fixtures for mocked tests -- mock objects for external APIs."""
import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass
from typing import Optional

from models.providers.base import ProviderCredentials, SearchResult, IssueResult, ProviderType


# ---------------------------------------------------------------------------
# Reset module-level GCD table cache between tests so one test's mocked schema
# doesn't leak into the next.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_gcd_table_cache():
    from models.gcd import invalidate_gcd_table_cache
    invalidate_gcd_table_cache()
    yield
    invalidate_gcd_table_cache()


# ---------------------------------------------------------------------------
# Reset the module-level Metron session cache and rate limiter between tests
# so one test's mocked Session/timing doesn't leak into the next.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_metron_session_cache():
    from models.metron import invalidate_session_cache
    invalidate_session_cache()
    yield
    invalidate_session_cache()


# ---------------------------------------------------------------------------
# Common credential fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def metron_creds():
    return ProviderCredentials(username="testuser", password="testpass")


@pytest.fixture
def comicvine_creds():
    return ProviderCredentials(api_key="fake-cv-api-key")


@pytest.fixture
def gcd_creds(gcd_db_path):
    return ProviderCredentials(database_path=str(gcd_db_path))


# ---------------------------------------------------------------------------
# GCD SQLite test database
#
# The GCD provider now reads a user-supplied SQLite dump. Rather than mock
# cursors, these fixtures build a tiny real SQLite file with the GCD schema and
# a handful of rows, so the ported SQL is exercised end to end.
# ---------------------------------------------------------------------------

def build_gcd_sqlite(path, *, core_only=False):
    """Create a minimal GCD SQLite database at `path`.

    Contains a Batman series (id 200) published by DC Comics with English
    language, issues #1/#2/#10 (plus a bracketed variant), a story with a
    writer credit and a character. When `core_only` is True, only the core
    tables are created (to exercise missing-table handling).
    """
    import sqlite3
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()

    cur.executescript(
        """
        CREATE TABLE stddata_language (id INTEGER PRIMARY KEY, code TEXT);
        CREATE TABLE gcd_publisher (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE gcd_series (
            id INTEGER PRIMARY KEY, name TEXT, year_began INTEGER,
            year_ended INTEGER, publisher_id INTEGER, language_id INTEGER
        );
        CREATE TABLE gcd_issue (
            id INTEGER PRIMARY KEY, number TEXT, volume TEXT, series_id INTEGER,
            indicia_publisher_id INTEGER, key_date TEXT, on_sale_date TEXT,
            title TEXT, rating TEXT, page_count INTEGER,
            page_count_uncertain INTEGER, deleted INTEGER DEFAULT 0
        );
        CREATE TABLE gcd_story (
            id INTEGER PRIMARY KEY, issue_id INTEGER, title TEXT, synopsis TEXT,
            notes TEXT, genre TEXT, characters TEXT, page_count INTEGER,
            sequence_number INTEGER, type_id INTEGER, script TEXT, pencils TEXT,
            inks TEXT, colors TEXT, letters TEXT, editing TEXT,
            deleted INTEGER DEFAULT 0
        );
        """
    )

    cur.executemany("INSERT INTO stddata_language (id, code) VALUES (?, ?)",
                    [(1, "en")])
    cur.executemany("INSERT INTO gcd_publisher (id, name) VALUES (?, ?)",
                    [(10, "DC Comics")])
    cur.executemany(
        "INSERT INTO gcd_series (id, name, year_began, year_ended, publisher_id, language_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(200, "Batman", 1940, None, 10, 1)],
    )
    cur.executemany(
        "INSERT INTO gcd_issue (id, number, volume, series_id, indicia_publisher_id, "
        "key_date, on_sale_date, title, rating, page_count, page_count_uncertain, deleted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (500, "1", "1", 200, None, "1940-04-01", "1940-03-01", "The Beginning", "", 64, 0, 0),
            (502, "2", "1", 200, None, "1940-05-01", "1940-04-01", "", "", 64, 0, 0),
            (510, "10", "1", 200, None, "1941-01-01", "1940-12-01", "", "", 64, 0, 0),
            (511, "[nn]", "1", 200, None, "1941-02-01", "1941-01-01", "", "", 64, 0, 0),
        ],
    )
    cur.executemany(
        "INSERT INTO gcd_story (id, issue_id, title, synopsis, notes, genre, characters, "
        "page_count, sequence_number, type_id, script, pencils, inks, colors, letters, editing, deleted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(900, 500, "Story One", "A synopsis", "", "superhero", "Batman; Robin",
          64, 1, 1, "", "", "", "", "", "", 0)],
    )

    if not core_only:
        cur.executescript(
            """
            CREATE TABLE gcd_credit_type (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE gcd_creator (id INTEGER PRIMARY KEY, gcd_official_name TEXT, sort_name TEXT);
            CREATE TABLE gcd_story_credit (
                id INTEGER PRIMARY KEY, story_id INTEGER, creator_id INTEGER,
                credit_type_id INTEGER, credited_as TEXT, credit_name TEXT,
                deleted INTEGER DEFAULT 0
            );
            CREATE TABLE gcd_issue_credit (
                id INTEGER PRIMARY KEY, issue_id INTEGER, creator_id INTEGER,
                credit_type_id INTEGER, credited_as TEXT, credit_name TEXT,
                deleted INTEGER DEFAULT 0
            );
            CREATE TABLE gcd_indicia_publisher (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE gcd_story_type (id INTEGER PRIMARY KEY, name TEXT, sort_code INTEGER);
            CREATE TABLE gcd_story_character (story_id INTEGER, character_id INTEGER);
            CREATE TABLE gcd_character (id INTEGER PRIMARY KEY, name TEXT);
            """
        )
        cur.executemany("INSERT INTO gcd_credit_type (id, name) VALUES (?, ?)",
                        [(1, "script"), (2, "pencils")])
        cur.executemany("INSERT INTO gcd_creator (id, gcd_official_name, sort_name) VALUES (?, ?, ?)",
                        [(700, "Bob Kane", "Kane, Bob")])
        cur.executemany(
            "INSERT INTO gcd_story_credit (id, story_id, creator_id, credit_type_id, "
            "credited_as, credit_name, deleted) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(800, 900, 700, 1, "", "", 0)],
        )
        cur.executemany("INSERT INTO gcd_story_type (id, name, sort_code) VALUES (?, ?, ?)",
                        [(1, "comic story", 1)])
        cur.executemany("INSERT INTO gcd_character (id, name) VALUES (?, ?)",
                        [(600, "Batman")])
        cur.executemany("INSERT INTO gcd_story_character (story_id, character_id) VALUES (?, ?)",
                        [(900, 600)])

    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def gcd_db_path(tmp_path):
    """Path to a fully-populated GCD SQLite test database."""
    return build_gcd_sqlite(tmp_path / "gcd.db")


@pytest.fixture
def gcd_core_only_db_path(tmp_path):
    """Path to a GCD SQLite database missing the optional/auxiliary tables."""
    return build_gcd_sqlite(tmp_path / "gcd_core.db", core_only=True)


@pytest.fixture
def gcd_configured(gcd_db_path, monkeypatch):
    """Point models.gcd at the full test database via saved credentials."""
    monkeypatch.setattr(
        "models.gcd._get_saved_credentials",
        lambda: {"database_path": str(gcd_db_path)},
    )
    return str(gcd_db_path)


# ---------------------------------------------------------------------------
# Mock Mokkari objects
# ---------------------------------------------------------------------------

def make_mock_series(*, id=100, name="Batman", year_began=2016, publisher_name="DC Comics", cv_id=12345):
    """Create a mock Mokkari series object."""
    s = MagicMock()
    s.id = id
    s.name = name
    s.year_began = year_began
    s.cv_id = cv_id
    s.display_name = name
    pub = MagicMock()
    pub.name = publisher_name
    s.publisher = pub
    return s


def make_mock_issue(*, id=500, number="1", name=None, cover_date="2020-01-15",
                    store_date="2020-01-13", image="https://example.com/cover.jpg",
                    series_id=100, desc="A great issue"):
    """Create a mock Mokkari issue object."""
    i = MagicMock()
    i.id = id
    i.number = number
    i.name = [name] if name else ["Issue Title"]
    i.cover_date = cover_date
    i.store_date = store_date
    i.image = image
    i.desc = desc
    i.story_titles = i.name
    series = MagicMock()
    series.id = series_id
    series.name = "Batman"
    series.year_began = 2016
    series.genres = []
    i.series = series
    i.publisher = MagicMock(name="DC Comics")
    i.publisher.name = "DC Comics"
    i.credits = []
    i.characters = []
    i.teams = []
    i.rating = MagicMock(name="Teen")
    i.rating.name = "Teen"
    i.resource_url = "https://metron.cloud/issue/500/"
    i.modified = "2024-01-01"
    i.page_count = 32
    # Support model_dump for Pydantic conversion
    i.model_dump = MagicMock(return_value={
        "id": id, "number": number, "story_titles": i.name,
        "cover_date": cover_date, "store_date": store_date,
        "series": {"id": series_id, "name": "Batman", "year_began": 2016, "genres": []},
        "publisher": {"name": "DC Comics"}, "credits": [], "characters": [], "teams": [],
        "rating": {"name": "Teen"}, "desc": desc,
        "resource_url": "https://metron.cloud/issue/500/", "modified": "2024-01-01",
        "page_count": 32, "image": image,
    })
    return i


# ---------------------------------------------------------------------------
# Mock Simyan/ComicVine objects
# ---------------------------------------------------------------------------

def make_mock_cv_volume(*, id=4050, name="Batman", start_year=2016,
                        publisher_name="DC Comics", count_of_issues=50):
    v = MagicMock()
    v.id = id
    v.name = name
    v.start_year = start_year
    v.count_of_issues = count_of_issues
    v.description = "The Dark Knight"
    pub = MagicMock()
    pub.name = publisher_name
    v.publisher = pub
    img = MagicMock()
    img.thumbnail = "https://example.com/thumb.jpg"
    v.image = img
    return v


def make_mock_cv_issue(*, id=1001, issue_number="1", name="Rebirth",
                       cover_date="2020-01-15", store_date=None,
                       publisher_name="DC Comics"):
    i = MagicMock()
    i.id = id
    i.issue_number = issue_number
    i.name = name
    i.cover_date = cover_date
    i.store_date = store_date
    i.description = "Batman returns"
    img = MagicMock()
    img.small_url = "https://example.com/small.jpg"
    img.thumb_url = "https://example.com/thumb.jpg"
    i.image = img
    vol = MagicMock()
    vol.id = 4050
    vol.name = "Batman"
    if publisher_name is None:
        vol.publisher = None
    else:
        pub = MagicMock()
        pub.name = publisher_name
        vol.publisher = pub
    i.volume = vol
    return i


# ---------------------------------------------------------------------------
# ComicVine SQLite test database
#
# The comicvine_sqlite provider reads a user-supplied SQLite dump. These fixtures
# build a tiny real SQLite file (cv_publisher/cv_volume/cv_issue with
# ComicVine-API-style JSON credit columns) so the parsing + map_to_comicinfo
# reuse are exercised end to end.
# ---------------------------------------------------------------------------

def build_comicvine_sqlite(path, *, extra_alias_volumes=False):
    """Create a minimal ComicVine SQLite database at `path`.

    Contains a Batman volume (id 4050) with one issue (#1). When
    `extra_alias_volumes` is True, two additional volumes match the alias
    "Batman" but NOT by name — used to exercise the ambiguous-selection path.
    """
    import json
    import sqlite3
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE cv_publisher (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE cv_volume (
            id INTEGER PRIMARY KEY, name TEXT, aliases TEXT, start_year INTEGER,
            publisher_id INTEGER, count_of_issues INTEGER, description TEXT,
            image_url TEXT, site_detail_url TEXT
        );
        CREATE TABLE cv_issue (
            id INTEGER PRIMARY KEY, volume_id INTEGER, name TEXT, issue_number TEXT,
            cover_date TEXT, store_date TEXT, description TEXT, image_url TEXT,
            site_detail_url TEXT, character_credits TEXT, person_credits TEXT,
            team_credits TEXT, location_credits TEXT, story_arc_credits TEXT,
            associated_images TEXT
        );
        """
    )
    cur.executemany("INSERT INTO cv_publisher (id, name) VALUES (?, ?)", [(1, "DC Comics")])
    cur.executemany(
        "INSERT INTO cv_volume (id, name, aliases, start_year, publisher_id, "
        "count_of_issues, description, image_url, site_detail_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(4050, "Batman", "", 2016, 1, 100, "The Dark Knight",
          "https://example.com/batman.jpg", "https://comicvine.gamespot.com/volume/4050-4050/")],
    )
    person = json.dumps([
        {"id": 1, "name": "Bob Kane", "role": "writer, penciler"},
        {"id": 2, "name": "Jerry Robinson", "role": "penciler, cover"},
    ])
    characters = json.dumps([{"id": 9, "name": "Batman"}, {"id": 10, "name": "Robin"}])
    teams = json.dumps([{"id": 5, "name": "Justice League"}])
    locations = json.dumps([{"id": 7, "name": "Gotham City"}])
    story_arcs = json.dumps([{"id": 3, "name": "Year One"}, {"id": 4, "name": "Second Arc"}])
    cur.executemany(
        "INSERT INTO cv_issue (id, volume_id, name, issue_number, cover_date, "
        "store_date, description, image_url, character_credits, person_credits, "
        "team_credits, location_credits, story_arc_credits) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(500, 4050, "The Beginning", "1", "2016-06-01", "2016-05-15",
          "An origin story.", "https://example.com/issue1.jpg",
          characters, person, teams, locations, story_arcs)],
    )

    if extra_alias_volumes:
        # Two volumes whose NAME lacks "Batman" but whose alias matches it, so a
        # "Batman" search returns >1 rows with no name-confident match.
        cur.executemany(
            "INSERT INTO cv_volume (id, name, aliases, start_year, publisher_id, "
            "count_of_issues, description, image_url, site_detail_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (4060, "The Dark Knight", "Batman", 2011, 1, 50, "", "", ""),
                (4061, "Caped Crusader", "Batman", 1999, 1, 30, "", "", ""),
            ],
        )
        # Rename 4050 so its name no longer contains "Batman" either.
        cur.execute("UPDATE cv_volume SET name = 'World Finest', aliases = 'Batman' WHERE id = 4050")

    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def comicvine_sqlite_db_path(tmp_path):
    """Path to a populated ComicVine SQLite test database."""
    return build_comicvine_sqlite(tmp_path / "comicvine.db")


@pytest.fixture
def comicvine_sqlite_configured(comicvine_sqlite_db_path, monkeypatch):
    """Point models.comicvine_sqlite at the test database via saved credentials."""
    monkeypatch.setattr(
        "models.comicvine_sqlite._get_saved_credentials",
        lambda: {"database_path": str(comicvine_sqlite_db_path)},
    )
    return str(comicvine_sqlite_db_path)


