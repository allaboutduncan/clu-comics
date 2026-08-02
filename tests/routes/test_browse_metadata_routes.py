"""
Routes: /browse/<category>/<name> and /api/browse/<category>/<name>.

Previously untested. Covers provider-ID normalization of the incoming name so
links carrying "Ron Lim [3258]" resolve to the same page as "Ron Lim".
"""

from urllib.parse import quote

import pytest


def _seed(conn, name, **ci):
    cols = {
        "name": name,
        "path": "/data/" + name,
        "type": "file",
        "size": 1024,
        "parent": "/data",
        "has_comicinfo": 1,
        "ci_publisher": "Marvel",
        "ci_series": "Silver Surfer",
        "ci_number": "1",
    }
    cols.update(ci)
    conn.execute(
        f"INSERT INTO file_index ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        list(cols.values()),
    )
    conn.commit()


@pytest.fixture
def library(db_connection):
    from core.database import (
        _backfill_file_metadata_tags,
        _backfill_normalize_credits,
    )
    _seed(db_connection, "A1.cbz", ci_penciller="Ron Lim [3258]")
    _seed(db_connection, "A2.cbz", ci_penciller="Ron Lim")
    _seed(db_connection, "A3.cbz", ci_writer="Roger Stern [41502]")
    _backfill_file_metadata_tags(db_connection)
    _backfill_normalize_credits(db_connection)
    return db_connection


class TestBrowsePage:
    def test_clean_name_renders(self, client, library):
        resp = client.get("/browse/penciller/" + quote("Ron Lim"))
        assert resp.status_code == 200
        assert b"Ron Lim" in resp.data

    def test_stale_link_with_provider_id_resolves(self, client, library):
        """The CBZ Info modal links the raw XML value, brackets and all."""
        resp = client.get("/browse/penciller/" + quote("Ron Lim [3258]"))
        assert resp.status_code == 200
        # Header shows the canonical name, not the tagger's ID.
        assert b"[3258]" not in resp.data
        assert b"Ron Lim" in resp.data

    def test_both_spellings_give_the_same_count(self, client, library):
        clean = client.get("/browse/penciller/" + quote("Ron Lim")).data
        dirty = client.get("/browse/penciller/" + quote("Ron Lim [3258]")).data
        assert b"2 comics" in clean
        assert b"2 comics" in dirty

    def test_artist_alias_maps_to_penciller(self, client, library):
        resp = client.get("/browse/artist/" + quote("Ron Lim"))
        assert resp.status_code == 200
        assert b"2 comics" in resp.data

    def test_writer_category(self, client, library):
        resp = client.get("/browse/writer/" + quote("Roger Stern"))
        assert resp.status_code == 200
        assert b"Roger Stern" in resp.data

    def test_unknown_name_shows_empty_state(self, client, library):
        resp = client.get("/browse/penciller/" + quote("Nobody At All"))
        assert resp.status_code == 200
        assert b"No comics found" in resp.data

    def test_invalid_category_redirects(self, client, library):
        resp = client.get("/browse/bogus/" + quote("Ron Lim"))
        assert resp.status_code == 302
        assert "/insights" in resp.headers["Location"]

    @pytest.mark.parametrize(
        "character",
        [
            "Superman",
            "Mr. Drysdale",        # period
            "The Mob",             # leading article
            "Simone DeNiege",      # internal capitals
            "Lois Lane",           # space
            "Bibbo",               # single word
        ],
    )
    def test_character_links_from_the_cbz_info_modal_resolve(
        self, client, db_connection, character
    ):
        """The CBZ Info modal builds /browse/characters/<encodeURIComponent(n)>
        from names it strips client-side; every one must route and resolve."""
        from core.database import update_file_metadata

        db_connection.execute(
            "INSERT INTO file_index (name, path, type, size, parent,"
            " has_comicinfo, ci_series, ci_number)"
            " VALUES ('S1.cbz','/data/S1.cbz','file',1,'/data',1,'Superman','1')"
        )
        db_connection.commit()
        fid = db_connection.execute(
            "SELECT id FROM file_index WHERE path='/data/S1.cbz'"
        ).fetchone()[0]
        update_file_metadata(
            fid,
            {"ci_characters": "Superman [1807], Lois Lane [1808], "
                              "Mr. Drysdale [13043], The Mob [9948], "
                              "Simone DeNiege [13028], Bibbo [12960]"},
            1.0, 1,
        )

        # quote() with no safe chars mirrors JS encodeURIComponent closely
        # enough for these names (space -> %20, period untouched).
        resp = client.get("/browse/characters/" + quote(character))
        assert resp.status_code == 200
        assert b"1 comic" in resp.data


class TestBrowseApi:
    def test_returns_matching_files(self, client, library):
        resp = client.get("/api/browse/penciller/" + quote("Ron Lim"))
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 2
        assert sorted(f["name"] for f in data["files"]) == ["A1.cbz", "A2.cbz"]
        assert all("thumbnail_url" in f for f in data["files"])

    def test_stale_link_resolves(self, client, library):
        resp = client.get("/api/browse/penciller/" + quote("Ron Lim [3258]"))
        assert resp.get_json()["total"] == 2

    def test_pagination(self, client, library):
        resp = client.get(
            "/api/browse/penciller/" + quote("Ron Lim") + "?limit=1&offset=0"
        )
        data = resp.get_json()
        assert data["total"] == 2
        assert len(data["files"]) == 1

    def test_invalid_category(self, client, library):
        resp = client.get("/api/browse/bogus/" + quote("Ron Lim"))
        assert resp.status_code == 400
        assert "error" in resp.get_json()
