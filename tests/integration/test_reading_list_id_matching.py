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
