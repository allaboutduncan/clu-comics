"""ci_provider column: written by scans, queried by folder."""


def _add(path, parent, provider, has_ci=1):
    from core.database import add_file_index_entry
    add_file_index_entry(name=path.rsplit("/", 1)[-1], path=path,
                         entry_type="file", size=1, parent=parent)
    return path


def _set(db, path, provider, has_ci=1):
    db.execute("UPDATE file_index SET ci_provider=?, has_comicinfo=? WHERE path=?",
               (provider, has_ci, path))
    db.commit()


class TestProviderMix:
    def test_mix_and_files(self, db_connection):
        from core.database import get_provider_mix, get_files_by_provider
        base = "/data/DC/Batman"
        for name, prov, ci in [
            ("a", "metron", 1), ("b", "metron", 1), ("c", "comicvine", 1),
            ("d", "", 1), ("e", None, 1), ("f", None, 0),
        ]:
            p = _add(f"{base}/{name}.cbz", base, prov)
            _set(db_connection, p, prov, ci)
        # A sibling folder sharing the name prefix must not leak in.
        other = _add("/data/DC/Batman Beyond/z.cbz", "/data/DC/Batman Beyond", "metron")
        _set(db_connection, other, "metron")
        # A nested folder only counts when recursive.
        nested = _add(f"{base}/Annuals/n.cbz", f"{base}/Annuals", "metron")
        _set(db_connection, nested, "metron")

        mix = get_provider_mix(base, recursive=False)
        assert mix["providers"] == {"metron": 2, "comicvine": 1}
        assert (mix["untagged"], mix["unscanned"], mix["no_xml"], mix["total"]) == (1, 1, 1, 6)
        assert get_provider_mix(base, recursive=True)["providers"]["metron"] == 3

        assert get_files_by_provider(base, False, providers=["metron"]) == [
            f"{base}/a.cbz", f"{base}/b.cbz"]
        assert get_files_by_provider(base, False, include_untagged=True,
                                     exclude_providers=["metron"]) == [
            f"{base}/c.cbz".replace("c.", "d.")]

    def test_scan_writes_provider(self, db_connection):
        from core.database import update_file_metadata, get_files_needing_provider_backfill
        p = _add("/data/X/1.cbz", "/data/X", None)
        _set(db_connection, p, None)
        assert [f["path"] for f in get_files_needing_provider_backfill()] == [p]
        fid = db_connection.execute("SELECT id FROM file_index WHERE path=?", (p,)).fetchone()[0]
        update_file_metadata(fid, {"ci_provider": "metron"}, 1.0, has_comicinfo=1)
        assert db_connection.execute(
            "SELECT ci_provider FROM file_index WHERE path=?", (p,)).fetchone()[0] == "metron"
        assert get_files_needing_provider_backfill() == []


class TestIndexSyncAfterWrite:
    def test_comicinfo_write_sets_provider_and_authoritative_metronid(self, db_connection):
        from core.database import update_file_index_from_comicinfo
        p = _add("/data/Y/1.cbz", "/data/Y", None)
        db_connection.execute("UPDATE file_index SET ci_metronid=55 WHERE path=?", (p,))
        db_connection.commit()

        # Merge-style write without MetronId: the old id is left alone.
        update_file_index_from_comicinfo(p, {"Series": "S", "Notes": "Metadata from ComicVine. Issue ID: 1"})
        row = db_connection.execute(
            "SELECT ci_provider, ci_metronid FROM file_index WHERE path=?", (p,)).fetchone()
        assert (row[0], row[1]) == ("comicvine", 55)

        # Replace-style write: the archive no longer has a MetronId.
        update_file_index_from_comicinfo(
            p, {"Series": "S", "Notes": "Metadata from ComicVine. Issue ID: 1"}, authoritative=True)
        assert db_connection.execute(
            "SELECT ci_metronid FROM file_index WHERE path=?", (p,)).fetchone()[0] == 0

    def test_no_notes_leaves_provider_for_the_scanner(self, db_connection):
        from core.database import update_file_index_from_comicinfo
        p = _add("/data/Y/2.cbz", "/data/Y", None)
        update_file_index_from_comicinfo(p, {"Series": "S"})
        assert db_connection.execute(
            "SELECT ci_provider FROM file_index WHERE path=?", (p,)).fetchone()[0] is None
