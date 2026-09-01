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
# Reset the module-level ComicVine (Simyan) client cache between tests. Beyond
# isolation this matters for resources: each cached client owns a background
# limiter thread and SQLite connections, and invalidating closes them.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_cv_client_cache():
    from models.comicvine import invalidate_cv_client_cache
    invalidate_cv_client_cache()
    yield
    invalidate_cv_client_cache()


# ---------------------------------------------------------------------------
# Clear the shared provider rate limiters between tests. They are process-wide
# by design, so without this one test's requests eat the next one's budget and
# a later test blocks on a window it never filled.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_provider_rate_limiters():
    from helpers.rate_limit import reset_all_limiters
    reset_all_limiters()
    yield
    reset_all_limiters()


# ---------------------------------------------------------------------------
# Drop cached provider instances between tests -- they're keyed by credentials
# and shared process-wide, so without this a test inherits the neighbour's
# instance (and its mocked client).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_provider_instance_cache():
    from models.providers import invalidate_provider_cache
    invalidate_provider_cache()
    yield
    invalidate_provider_cache()


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

    Also contains an Italian series, Diabolik (id 201, Astorina, issue #1), so
    the language filter can be exercised: it is invisible to an English-only
    search and found when 'it' is configured.
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
                    [(1, "en"), (2, "it")])
    cur.executemany("INSERT INTO gcd_publisher (id, name) VALUES (?, ?)",
                    [(10, "DC Comics"), (11, "Astorina")])
    cur.executemany(
        "INSERT INTO gcd_series (id, name, year_began, year_ended, publisher_id, language_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(200, "Batman", 1940, None, 10, 1),
         (201, "Diabolik", 1962, None, 11, 2)],
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
            (520, "1", "1", 201, None, "1962-11-01", "1962-11-01", "Il re del terrore", "", 128, 0, 0),
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
                       publisher_name="DC Comics", site_url=None):
    i = MagicMock()
    i.id = id
    i.issue_number = issue_number
    i.name = name
    i.cover_date = cover_date
    i.store_date = store_date
    i.description = "Batman returns"
    # Simyan always populates site_url (site_detail_url) -> ComicInfo Web.
    i.site_url = site_url or f"https://comicvine.gamespot.com/issue/4000-{id}/"
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
        {"id": 3, "name": "Julius Schwartz", "role": "editor"},
    ])
    characters = json.dumps([{"id": 9, "name": "Batman"}, {"id": 10, "name": "Robin"}])
    teams = json.dumps([{"id": 5, "name": "Justice League"}])
    locations = json.dumps([{"id": 7, "name": "Gotham City"}])
    story_arcs = json.dumps([{"id": 3, "name": "Year One"}, {"id": 4, "name": "Second Arc"}])
    cur.executemany(
        "INSERT INTO cv_issue (id, volume_id, name, issue_number, cover_date, "
        "store_date, description, image_url, site_detail_url, character_credits, "
        "person_credits, team_credits, location_credits, story_arc_credits) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(500, 4050, "The Beginning", "1", "2016-06-01", "2016-05-15",
          "An origin story.", "https://example.com/issue1.jpg",
          "https://comicvine.gamespot.com/batman-1/4000-500/",
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




# ---------------------------------------------------------------------------
# INDUCKS SQLite test database
#
# The INDUCKS provider reads a user-built SQLite database. As with GCD, these
# fixtures build a tiny real file rather than mocking cursors, so the SQL is
# actually exercised -- which matters more here than usual: the start-year
# derivation has a trap in it that only real rows can catch.
# ---------------------------------------------------------------------------

def build_inducks_sqlite(path, *, core_only=False):
    """Create a minimal INDUCKS SQLite database at `path`.

    Models the two shapes that matter:

    * ``it/TL`` "Topolino (libretto)" -- an anthology issue with two stories, a
      cover that must NOT contribute credits, per-story writers, pencillers and
      characters, and an issue set whose ``oldestdate`` values include both an
      empty string and the ``9999-12-31`` unknown sentinel. That is the exact
      shape a naive ``MIN(SUBSTR(oldestdate, 1, 4))`` gets wrong.
    * ``it/TG`` "Topolino (giornale)" -- a second publication whose
      qualifier-stripped title collides with the first, so the bare name
      "Topolino" is ambiguous and the stripped name is not usable as a Series.

    ``xx/UN`` has no usable date at all, for the year=None path.
    """
    import sqlite3
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE inducks_publication (
            publicationcode TEXT PRIMARY KEY, countrycode TEXT,
            languagecode TEXT, title TEXT
        );
        CREATE TABLE inducks_issue (
            issuecode TEXT PRIMARY KEY, publicationcode TEXT, issuenumber TEXT,
            title TEXT, pages TEXT, price TEXT, oldestdate TEXT
        );
        CREATE TABLE inducks_issuedate (issuecode TEXT, date TEXT, kindofdate TEXT);
        CREATE TABLE inducks_entry (
            entrycode TEXT PRIMARY KEY, issuecode TEXT, storyversioncode TEXT,
            position TEXT, title TEXT
        );
        CREATE TABLE inducks_storyversion (
            storyversioncode TEXT PRIMARY KEY, storycode TEXT, kind TEXT
        );
        CREATE TABLE inducks_storyjob (
            storyversioncode TEXT, personcode TEXT, plotwritartink TEXT
        );
        CREATE TABLE inducks_character (charactercode TEXT PRIMARY KEY, charactername TEXT);
        """
    )
    if not core_only:
        cur.executescript(
            """
            CREATE TABLE inducks_story (storycode TEXT PRIMARY KEY, title TEXT);
            CREATE TABLE inducks_person (personcode TEXT PRIMARY KEY, fullname TEXT);
            CREATE TABLE inducks_publisher (publisherid TEXT PRIMARY KEY, publishername TEXT);
            CREATE TABLE inducks_publishingjob (publisherid TEXT, issuecode TEXT);
            CREATE TABLE inducks_appearance (storyversioncode TEXT, charactercode TEXT);
            CREATE TABLE inducks_charactername (
                charactercode TEXT, languagecode TEXT, charactername TEXT, preferred TEXT
            );
            """
        )

    cur.executemany(
        "INSERT INTO inducks_publication (publicationcode, countrycode, languagecode, title) "
        "VALUES (?, ?, ?, ?)",
        [
            ("it/TL", "it", "it", "Topolino (libretto)"),
            ("it/TG", "it", "it", "Topolino (giornale)"),
            ("it/ZP", "it", "it", "Zio Paperone"),
            ("xx/UN", "it", "it", "Senza data"),
            ("us/WDC", "us", "en", "Walt Disney's Comics and Stories"),
        ],
    )

    cur.executemany(
        "INSERT INTO inducks_issue (issuecode, publicationcode, issuenumber, title, "
        "pages, price, oldestdate) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            # The empty string sorts before every real date, and 9999-12-31 is
            # INDUCKS' "unknown". Both must be excluded before MIN() sees them,
            # or it/TL reports no start year at all.
            ("it/TL    1", "it/TL", "1", "Il primo numero", "68", "", "1949-04-07"),
            ("it/TL    2", "it/TL", "2", "", "68", "", ""),
            ("it/TL    3", "it/TL", "3", "", "68", "", "9999-12-31"),
            ("it/TL 3200", "it/TL", "3200", "", "164", "", "2017-06-27"),
            ("it/TG    1", "it/TG", "1", "", "8", "", "1932-12-31"),
            ("it/ZP    1", "it/ZP", "1", "", "100", "", "1987-01-01"),
            ("xx/UN    1", "xx/UN", "1", "", "", "", ""),
            ("us/WDC   1", "us/WDC", "1", "", "64", "", "1940-10-01"),
        ],
    )

    # A more precise date than oldestdate, which is what the model prefers.
    cur.executemany(
        "INSERT INTO inducks_issuedate (issuecode, date, kindofdate) VALUES (?, ?, ?)",
        [("it/TL    1", "1949-04-07", "d"), ("it/TL 3200", "2017-06-27", "d")],
    )

    # Issue 1 of it/TL: a cover plus two stories. The cover's artist must not
    # reach ComicInfo -- a cover artist is not the penciller of the book.
    cur.executemany(
        "INSERT INTO inducks_entry (entrycode, issuecode, storyversioncode, position, title) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("e1", "it/TL    1", "sv-cover", "1", ""),
            ("e2", "it/TL    1", "sv-a", "2", "Topolino e il cobra bianco"),
            ("e3", "it/TL    1", "sv-b", "3", "Paperino e il ladro di uova"),
        ],
    )
    cur.executemany(
        "INSERT INTO inducks_storyversion (storyversioncode, storycode, kind) VALUES (?, ?, ?)",
        [("sv-cover", "st-cover", "c"), ("sv-a", "st-a", "n"), ("sv-b", "st-b", "n")],
    )
    cur.executemany(
        "INSERT INTO inducks_storyjob (storyversioncode, personcode, plotwritartink) "
        "VALUES (?, ?, ?)",
        [
            ("sv-cover", "p-cover", "a"),
            ("sv-a", "p-bottaro", "w"),
            ("sv-a", "p-bottaro", "a"),
            ("sv-b", "p-scarpa", "w"),
            ("sv-b", "p-cavazzano", "a"),
            ("sv-b", "p-inker", "i"),
        ],
    )
    cur.executemany(
        "INSERT INTO inducks_character (charactercode, charactername) VALUES (?, ?)",
        [("c-mm", "Mickey Mouse"), ("c-dd", "Donald Duck")],
    )

    if not core_only:
        cur.executemany("INSERT INTO inducks_story (storycode, title) VALUES (?, ?)",
                        [("st-cover", ""), ("st-a", "Topolino e il cobra bianco"),
                         ("st-b", "Paperino e il ladro di uova")])
        cur.executemany("INSERT INTO inducks_person (personcode, fullname) VALUES (?, ?)",
                        [("p-bottaro", "Luciano Bottaro"), ("p-scarpa", "Romano Scarpa"),
                         ("p-cavazzano", "Giorgio Cavazzano"), ("p-inker", "Sandro Zemolin"),
                         ("p-cover", "Coverist")])
        cur.executemany("INSERT INTO inducks_publisher (publisherid, publishername) VALUES (?, ?)",
                        [("mondadori", "Arnoldo Mondadori Editore"),
                         ("panini", "Panini Comics")])
        # Two publishers on one issue, oldest first: the last row is the imprint
        # that actually produced it.
        cur.executemany("INSERT INTO inducks_publishingjob (publisherid, issuecode) VALUES (?, ?)",
                        [("mondadori", "it/TL    1"), ("panini", "it/TL    1")])
        cur.executemany("INSERT INTO inducks_appearance (storyversioncode, charactercode) VALUES (?, ?)",
                        [("sv-a", "c-mm"), ("sv-b", "c-dd"), ("sv-cover", "c-mm")])
        cur.executemany(
            "INSERT INTO inducks_charactername (charactercode, languagecode, charactername, preferred) "
            "VALUES (?, ?, ?, ?)",
            [("c-mm", "it", "Topolino", "Y"), ("c-dd", "it", "Paperino", "Y"),
             ("c-dd", "it", "Paolino Paperino", "N")],
        )

    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def inducks_db_path(tmp_path):
    """Path to a fully-populated INDUCKS SQLite test database."""
    return build_inducks_sqlite(tmp_path / "inducks.db")


@pytest.fixture
def inducks_creds(inducks_db_path):
    return ProviderCredentials(database_path=str(inducks_db_path))


@pytest.fixture
def inducks_configured(inducks_db_path, monkeypatch):
    """Point models.inducks at the test database, scoped to Italy."""
    import models.inducks as inducks_module
    inducks_module.invalidate_inducks_table_cache()
    monkeypatch.setattr(
        "models.inducks._get_saved_credentials",
        lambda: {"database_path": str(inducks_db_path)},
    )
    monkeypatch.setattr("models.inducks.get_configured_countries", lambda: ["it"])
    yield str(inducks_db_path)
    inducks_module.invalidate_inducks_table_cache()
