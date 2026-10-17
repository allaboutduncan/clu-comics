"""Reading-list matching against a real database: ComicVine ids, colons in
series names, variant issue numbers, and ids filled in by a sync."""


def _add(db, path, *, series=None, number=None, year=None, cvid=None):
    from core.database import add_file_index_entry
    parent, name = path.rsplit("/", 1)
    add_file_index_entry(name=name, path=path, entry_type="file", size=1, parent=parent)
    if series is not None:
        db.execute(
            "UPDATE file_index SET has_comicinfo=1, ci_series=?, ci_number=?, ci_year=?, "
            "ci_cvid=? WHERE path=?",
            (series, number, year, cvid, path),
        )
        db.commit()
    return path


class TestMetadataSearch:

    def test_series_with_a_colon_is_found(self, db_connection):
        from core.database import search_by_comic_metadata
        p = _add(db_connection,
                 "/data/Marvel/ASM Sins (2020)/Amazing Spider-Man: Sins Rising Prelude #001 (2020).cbz",
                 series="Amazing Spider-Man: Sins Rising Prelude", number="1", year="2020")
        rows = search_by_comic_metadata("Amazing Spider-Man: Sins Rising Prelude", "1")
        assert [r["path"] for r in rows] == [p]

    def test_variant_and_regular_issue_are_distinct(self, db_connection):
        from core.database import search_by_comic_metadata
        base = "/data/Marvel/The Amazing Spider-Man (2018)"
        reg = _add(db_connection, f"{base}/The Amazing Spider-Man #050 (2020).cbz",
                   series="The Amazing Spider-Man", number="50", year="2020")
        lr = _add(db_connection, f"{base}/The Amazing Spider-Man #050.LR (2020).cbz",
                  series="The Amazing Spider-Man", number="50.LR", year="2020")
        assert [r["path"] for r in search_by_comic_metadata("The Amazing Spider-Man", "50")] == [reg]
        assert [r["path"] for r in search_by_comic_metadata("The Amazing Spider-Man", "50.LR")] == [lr]
        assert [r["path"] for r in search_by_comic_metadata("The Amazing Spider-Man", "050")] == [reg]


    def test_accented_capitals_match(self, db_connection):
        """SQLite's LOWER() folds ASCII only; both sides must fold alike."""
        from core.database import search_by_comic_metadata
        p = _add(db_connection, "/data/Marvel/Élektra (2014)/Élektra 001 (2014).cbz",
                 series="Élektra", number="1", year="2014")
        assert [r["path"] for r in search_by_comic_metadata("Élektra", "1")] == [p]

    def test_limit_applies_after_the_exact_checks(self, db_connection):
        """%x%men% also returns every other X-title's #1. The limit must not
        fill with those before "X-Men" itself."""
        from core.database import search_by_comic_metadata
        for title in ("All-New X-Men", "Astonishing X-Men", "Amazing X-Men",
                      "Extraordinary X-Men", "Uncanny X-Men"):
            _add(db_connection, f"/data/Marvel/{title}/{title} 001.cbz",
                 series=title, number="1")
        xmen = _add(db_connection, "/data/Marvel/X-Men/X-Men 001.cbz",
                    series="X-Men", number="1")
        rows = search_by_comic_metadata("X-Men", "1", limit=2)
        assert rows[0]["path"] == xmen
        assert len(rows) == 2

    def test_a_blank_number_is_not_issue_zero(self, db_connection):
        from core.database import search_by_comic_metadata
        _add(db_connection, "/data/Marvel/Hulk/Hulk untagged.cbz", series="Hulk", number="")
        zero = _add(db_connection, "/data/Marvel/Hulk/Hulk 000.cbz", series="Hulk", number="0")
        assert [r["path"] for r in search_by_comic_metadata("Hulk", "0")] == [zero]

    def test_negative_issue_numbers(self, db_connection):
        from core.database import search_by_comic_metadata
        neg = _add(db_connection, "/data/DC/Batman/Batman -1.cbz", series="Batman", number="-1")
        one = _add(db_connection, "/data/DC/Batman/Batman 001.cbz", series="Batman", number="1")
        assert [r["path"] for r in search_by_comic_metadata("Batman", "-1")] == [neg]
        assert [r["path"] for r in search_by_comic_metadata("Batman", "1")] == [one]


class TestMatchFile:

    def test_sins_rising_matches_by_metadata(self, db_connection):
        from models.cbl import CBLLoader
        p = _add(db_connection,
                 "/data/Marvel/ASM Sins (2020)/Amazing Spider-Man: Sins Rising Prelude #001 (2020).cbz",
                 series="Amazing Spider-Man: Sins Rising Prelude", number="1", year="2020")
        loader = CBLLoader("<ReadingList><Name>x</Name><Books/></ReadingList>",
                           filename="[Marvel] x.cbl")
        assert loader.match_file("Amazing Spider-Man: Sins Rising Prelude", "1",
                                 "2020", "2020") == p

    def test_cv_issue_id_beats_a_misleading_name(self, db_connection):
        from models.cbl import CBLLoader
        p = _add(db_connection, "/data/Odd/renamed by hand.cbz",
                 series="Something Else", number="9", cvid=781429)
        loader = CBLLoader("<ReadingList><Name>x</Name><Books/></ReadingList>")
        assert loader.match_file("Amazing Spider-Man: Sins Rising Prelude", "1",
                                 "2020", "2020", cv_issue_id=781429) == p


class TestSyncFillsIds:

    def test_kept_entry_gains_the_ids_the_source_now_names(self, db_connection):
        from core.database import (
            add_reading_list_entry, create_reading_list, sync_reading_list_entries,
        )
        list_id = create_reading_list("L")
        add_reading_list_entry(list_id, {"series": "S", "issue_number": "1",
                                         "volume": "2020", "year": "2020"})
        sync_reading_list_entries(list_id, [{
            "series": "S", "issue_number": "1", "volume": "2020", "year": "2020",
            "cv_issue_id": 781429, "cv_volume_id": 128879,
        }])
        row = db_connection.execute(
            "SELECT cv_issue_id, cv_volume_id FROM reading_list_entries WHERE reading_list_id=?",
            (list_id,)).fetchone()
        assert (row["cv_issue_id"], row["cv_volume_id"]) == (781429, 128879)

    def test_an_existing_id_is_never_overwritten(self, db_connection):
        from core.database import (
            add_reading_list_entry, create_reading_list, sync_reading_list_entries,
        )
        list_id = create_reading_list("L")
        add_reading_list_entry(list_id, {"series": "S", "issue_number": "1",
                                         "cv_issue_id": 5})
        sync_reading_list_entries(list_id, [{"series": "S", "issue_number": "1",
                                             "cv_issue_id": 6}])
        row = db_connection.execute(
            "SELECT cv_issue_id FROM reading_list_entries WHERE reading_list_id=?",
            (list_id,)).fetchone()
        assert row["cv_issue_id"] == 5


class TestScannerWritesCvid:

    def test_update_file_metadata_stores_cvid_and_zero(self, db_connection):
        from core.database import update_file_metadata
        a = _add(db_connection, "/data/a.cbz")
        b = _add(db_connection, "/data/b.cbz")
        ids = {r["path"]: r["id"] for r in db_connection.execute("SELECT id, path FROM file_index")}
        update_file_metadata(ids[a], {"ci_series": "S", "ci_cvid": 781429}, 1.0, 1)
        update_file_metadata(ids[b], {"ci_series": "S"}, 1.0, 1)
        got = dict(db_connection.execute("SELECT path, ci_cvid FROM file_index").fetchall())
        assert got == {a: 781429, b: 0}
