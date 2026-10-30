"""
Integration: metadata_browse / series_representative_path
against a real SQLite database.
"""
import pytest


def _seed(conn, **kwargs):
    defaults = {
        "name": "x.cbz",
        "path": "/data/x.cbz",
        "type": "file",
        "size": 1024,
        "parent": "/data",
        "ci_publisher": None,
        "ci_series": None,
        "ci_number": None,
        "ci_year": None,
        "ci_writer": None,
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
    from core.database import invalidate_metadata_browser_cache
    invalidate_metadata_browser_cache()


@pytest.fixture
def library(db_connection):
    c = db_connection
    _seed(c, name="Batman 001.cbz", path="/data/DC/Batman/001.cbz",
          parent="/data/DC/Batman",
          ci_publisher="DC", ci_series="Batman", ci_number="1", ci_year="2020",
          ci_writer="Tom King", ci_characters="Batman, Joker",
          ci_genre="Superhero")
    _seed(c, name="Batman 002.cbz", path="/data/DC/Batman/002.cbz",
          parent="/data/DC/Batman",
          ci_publisher="DC", ci_series="Batman", ci_number="2", ci_year="2020",
          ci_writer="Tom King", ci_characters="Batman, Catwoman",
          ci_genre="Superhero")
    _seed(c, name="Saga 001.cbz", path="/data/Image/Saga/001.cbz",
          parent="/data/Image/Saga",
          ci_publisher="Image", ci_series="Saga", ci_number="1", ci_year="2012",
          ci_writer="Brian K. Vaughan", ci_characters="Alana, Marko",
          ci_genre="Sci-Fi, Fantasy")
    return c


def test_metadata_browse_publisher_axis(library):
    from core.database import metadata_browse
    r = metadata_browse("publisher", {}, sort="count")
    assert r["level"] == "publisher"
    names = [i["name"] for i in r["items"]]
    # DC has 2 issues, Image 1 — count sort should put DC first
    assert names[0] == "DC"


def test_metadata_browse_series_includes_publisher(library):
    from core.database import metadata_browse
    r = metadata_browse("series", {})
    assert r["level"] == "series"
    by_name = {i["name"]: i for i in r["items"]}
    assert by_name["Batman"]["publisher"] == "DC"
    assert by_name["Saga"]["publisher"] == "Image"


def test_metadata_browse_series_selected_returns_issues(library):
    from core.database import metadata_browse
    r = metadata_browse("series", {"series": ["Batman"]})
    assert r["level"] == "issue"
    paths = sorted(i["path"] for i in r["items"])
    assert paths == [
        "/data/DC/Batman/001.cbz",
        "/data/DC/Batman/002.cbz",
    ]


def test_metadata_browse_year_decade_vs_year_level(library):
    from core.database import metadata_browse
    all_time = metadata_browse("year", {})
    assert all_time["level"] == "decade"
    decades = {i["value"] for i in all_time["items"]}
    assert decades == {2020, 2010}

    single = metadata_browse(
        "year", {"year_from": 2020, "year_to": 2029}
    )
    assert single["level"] == "year"
    years = {i["value"] for i in single["items"]}
    assert years == {2020}


def test_metadata_browse_search(library):
    from core.database import metadata_browse
    r = metadata_browse("series", {"search": "bat"})
    assert r["level"] == "series"
    names = [i["name"] for i in r["items"]]
    assert names == ["Batman"]



def _seed_letters(conn):
    """Publishers spread across the alphabet, plus one that starts with a digit."""
    for i, pub in enumerate(["Archie", "Boom", "Dark Horse", "Dynamite",
                             "IDW", "Oni", "2000 AD", " marvel"]):
        _seed(conn, name=f"x{i}.cbz", path=f"/data/L/{i}.cbz", parent="/data/L",
              ci_publisher=pub, ci_series=f"S{i}", ci_number="1", ci_year="2020")


def test_letter_filter_runs_before_paging(library):
    """A letter must find every match, not only those on the loaded page."""
    from core.database import metadata_browse
    _seed_letters(library)
    # Alphabetical page 1 of size 2 is "2000 AD", "Archie": no D on it.
    r = metadata_browse("publisher", {"letter": "D"}, limit=2, offset=0)
    assert [i["name"] for i in r["items"]] == ["Dark Horse", "DC"]
    assert r["total"] == 3          # Dark Horse, DC, Dynamite
    page2 = metadata_browse("publisher", {"letter": "D"}, limit=2, offset=2)
    assert [i["name"] for i in page2["items"]] == ["Dynamite"]


def test_letter_hash_is_everything_not_a_to_z(library):
    from core.database import metadata_browse
    _seed_letters(library)
    r = metadata_browse("publisher", {"letter": "#"})
    assert [i["name"] for i in r["items"]] == ["2000 AD"]


def test_letter_ignores_case_and_leading_space(library):
    from core.database import metadata_browse
    _seed_letters(library)
    r = metadata_browse("publisher", {"letter": "m"})
    assert [i["name"] for i in r["items"]] == [" marvel"]


def test_letters_cover_every_page_and_ignore_the_letter(library):
    from core.database import metadata_browse
    _seed_letters(library)
    expected = ["#", "A", "B", "D", "I", "M", "O"]
    first = metadata_browse("publisher", {}, limit=1, offset=0)
    assert first["letters"] == expected
    narrowed = metadata_browse("publisher", {"letter": "B"}, limit=1)
    assert narrowed["letters"] == expected


def test_letters_are_not_rescanned_per_page_sort_or_letter(library):
    """The browse cache key holds offset/limit/sort/letter; the letters do not
    depend on any of them, so a page turn must reuse the letters it has."""
    from core.database import metadata_browse, invalidate_metadata_browser_cache
    _seed_letters(library)
    expected = ["#", "A", "B", "D", "I", "M", "O"]
    assert metadata_browse("publisher", {}, limit=1, offset=0)["letters"] == expected
    # A new row behind the cache's back: only a rescan would see the Z.
    library.execute(
        "INSERT INTO file_index (name, path, type, size, parent, ci_publisher)"
        " VALUES ('z.cbz', '/data/L/z.cbz', 'file', 1, '/data/L', 'Zenescope')")
    library.commit()
    for kw in ({"offset": 1}, {"sort": "count"}, {"filters": {"letter": "B"}}):
        filters = kw.pop("filters", {})
        assert metadata_browse("publisher", filters, limit=1, **kw)["letters"] == expected
    # Invalidation (any file_index change) still reaches the letters.
    invalidate_metadata_browser_cache()
    assert metadata_browse("publisher", {}, limit=1)["letters"] == expected + ["Z"]


def test_all_total_ignores_search_and_letter(library):
    """"Showing 3 of 76 publishers": the server sends the 76 with every load."""
    from core.database import metadata_browse
    _seed_letters(library)
    whole = metadata_browse("publisher", {})
    assert whole["all_total"] == whole["total"] == 10
    lettered = metadata_browse("publisher", {"letter": "D"}, limit=2)
    assert (lettered["total"], lettered["all_total"]) == (3, 10)
    # The search matches file and series names: series S2 is Dark Horse's.
    searched = metadata_browse("publisher", {"search": "s2"})
    assert (searched["total"], searched["all_total"]) == (1, 10)
    # Other filters still narrow the whole: the "of N" is this view's total.
    issues = metadata_browse("issue", {"publisher": ["DC"], "search": "002"})
    assert (issues["total"], issues["all_total"]) == (1, 2)


def test_letter_applies_to_series_and_issue_levels(library):
    from core.database import metadata_browse
    series = metadata_browse("series", {"letter": "S"})
    assert {i["name"] for i in series["items"]} == {"Saga"}
    assert series["letters"] == ["B", "S"]
    issues = metadata_browse("issue", {"letter": "S"})
    assert [i["name"] for i in issues["items"]] == ["Saga 001.cbz"]


def test_year_levels_have_no_letters(library):
    from core.database import metadata_browse
    assert metadata_browse("year", {})["letters"] == []


def test_series_representative_path(library):
    from core.database import series_representative_path
    p = series_representative_path("Batman")
    assert p == "/data/DC/Batman/001.cbz"  # earliest issue number
