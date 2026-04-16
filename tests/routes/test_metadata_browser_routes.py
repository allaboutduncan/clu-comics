"""
Routes: /library page and /api/metadata/browse JSON endpoints (metadata browser).
"""
import pytest


def _seed_file(conn, **kwargs):
    """Insert a file_index row with ci_* columns set."""
    defaults = {
        "name": "Test.cbz",
        "path": "/data/Test/Test.cbz",
        "type": "file",
        "size": 1024,
        "parent": "/data/Test",
        "ci_publisher": None,
        "ci_series": None,
        "ci_number": None,
        "ci_year": None,
        "ci_writer": None,
        "ci_penciller": None,
        "ci_characters": None,
        "ci_genre": None,
        "has_comicinfo": 1,
    }
    defaults.update(kwargs)
    cols = ",".join(defaults.keys())
    placeholders = ",".join("?" * len(defaults))
    conn.execute(
        f"INSERT INTO file_index ({cols}) VALUES ({placeholders})",
        list(defaults.values()),
    )

    # Mirror the write path that the metadata scanner follows in production,
    # so list-kind facets (writer / characters / genre) see this row.
    from core.database import _TAG_KINDS, _split_tag_values
    tag_rows = []
    for kind, column in _TAG_KINDS:
        for v in _split_tag_values(defaults.get(column)):
            tag_rows.append((defaults["path"], kind, v))
    if tag_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO file_metadata_tags(file_path, kind, value) VALUES (?,?,?)",
            tag_rows,
        )
    conn.commit()

    # Bust the in-memory TTL cache for the metadata browser so the newly
    # seeded rows are reflected in the next query.
    from core.database import invalidate_metadata_browser_cache
    invalidate_metadata_browser_cache()


@pytest.fixture
def seeded_library(db_connection):
    """Seed a small library across two publishers / three series."""
    conn = db_connection
    _seed_file(
        conn,
        name="Batman 001.cbz", path="/data/DC/Batman/Batman 001.cbz",
        parent="/data/DC/Batman",
        ci_publisher="DC Comics", ci_series="Batman", ci_number="1",
        ci_year="2020", ci_writer="Tom King",
        ci_characters="Batman, Catwoman", ci_genre="Superhero",
    )
    _seed_file(
        conn,
        name="Batman 002.cbz", path="/data/DC/Batman/Batman 002.cbz",
        parent="/data/DC/Batman",
        ci_publisher="DC Comics", ci_series="Batman", ci_number="2",
        ci_year="2020", ci_writer="Tom King",
        ci_characters="Batman, Joker", ci_genre="Superhero",
    )
    _seed_file(
        conn,
        name="ASM 001.cbz", path="/data/Marvel/ASM/ASM 001.cbz",
        parent="/data/Marvel/ASM",
        ci_publisher="Marvel", ci_series="Amazing Spider-Man", ci_number="1",
        ci_year="2018", ci_writer="Nick Spencer",
        ci_characters="Spider-Man, Mary Jane", ci_genre="Superhero",
    )
    _seed_file(
        conn,
        name="Saga 001.cbz", path="/data/Image/Saga/Saga 001.cbz",
        parent="/data/Image/Saga",
        ci_publisher="Image", ci_series="Saga", ci_number="1",
        ci_year="2012", ci_writer="Brian K. Vaughan",
        ci_characters="Alana, Marko", ci_genre="Sci-Fi, Fantasy",
    )
    return conn


def test_library_page_renders(client):
    """The /library page returns 200 with the browser UI scaffolding."""
    r = client.get("/library")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="mbBreadcrumb"' in body
    assert 'id="mbGrid"' in body


def test_browse_publisher_axis(client, seeded_library):
    r = client.get("/api/metadata/browse?axis=publisher")
    data = r.get_json()
    assert data["level"] == "publisher"
    names = sorted(i["name"] for i in data["items"])
    assert names == ["DC Comics", "Image", "Marvel"]


def test_browse_series_axis(client, seeded_library):
    r = client.get("/api/metadata/browse?axis=series")
    data = r.get_json()
    assert data["level"] == "series"
    names = {i["name"] for i in data["items"]}
    assert names == {"Batman", "Amazing Spider-Man", "Saga"}


def test_browse_series_filtered_by_publisher(client, seeded_library):
    r = client.get("/api/metadata/browse?axis=series&publisher=DC+Comics")
    data = r.get_json()
    assert data["level"] == "series"
    names = {i["name"] for i in data["items"]}
    assert names == {"Batman"}


def test_browse_issue_level_when_series_selected(client, seeded_library):
    r = client.get("/api/metadata/browse?axis=series&series=Batman")
    data = r.get_json()
    assert data["level"] == "issue"
    paths = sorted(i["path"] for i in data["items"])
    assert paths == [
        "/data/DC/Batman/Batman 001.cbz",
        "/data/DC/Batman/Batman 002.cbz",
    ]


def test_browse_year_axis_decade_bucket(client, seeded_library):
    r = client.get("/api/metadata/browse?axis=year")
    data = r.get_json()
    assert data["level"] == "decade"
    decades = {i["value"]: i["count"] for i in data["items"]}
    assert decades == {2020: 2, 2010: 2}


def test_browse_year_axis_single_decade_drilldown(client, seeded_library):
    r = client.get("/api/metadata/browse?axis=year&year_from=2020&year_to=2029")
    data = r.get_json()
    assert data["level"] == "year"
    years = {i["value"]: i["count"] for i in data["items"]}
    assert years == {2020: 2}


def test_browse_pagination(client, seeded_library):
    r = client.get("/api/metadata/browse?axis=publisher&limit=2&offset=0")
    first = r.get_json()
    assert first["total"] == 3
    assert len(first["items"]) == 2

    r = client.get("/api/metadata/browse?axis=publisher&limit=2&offset=2")
    second = r.get_json()
    assert second["total"] == 3
    assert len(second["items"]) == 1
    # No overlap
    seen = {i["name"] for i in first["items"]} | {i["name"] for i in second["items"]}
    assert seen == {"DC Comics", "Image", "Marvel"}


def test_browse_invalid_axis_returns_400(client):
    r = client.get("/api/metadata/browse?axis=bogus")
    assert r.status_code == 400


def test_series_cover_endpoint(client, seeded_library):
    r = client.get("/api/metadata/series-cover?series=Batman")
    data = r.get_json()
    assert data["path"] == "/data/DC/Batman/Batman 001.cbz"
    assert data["thumbnail_url"]  # URL string


def test_series_cover_missing_series_parameter(client):
    r = client.get("/api/metadata/series-cover")
    assert r.status_code == 400
