"""End-to-end matching of a series' issues against the files on disk.

Covers helpers/collection.py: match_issues_to_collection. The reported bug was
that issue #1 was satisfied by "Nightwing 051 (2016).cbz" - the compiled rename
pattern had no leading digit boundary - and that one file could be claimed by
many issues because matched files were never consumed.
"""
import pytest
from tests.factories.db_factories import create_series, create_issue

RENAME_PATTERN = "{series_name} {issue_number} ({volume_year})"


def _objs(series_id):
    from core.database import get_series_by_id, get_issues_for_series
    from models.issue import IssueObj, SeriesObj

    series = get_series_by_id(series_id)
    issues = get_issues_for_series(series_id)
    return [IssueObj(i) for i in issues], SeriesObj(series)


def _setup(tmp_path, filenames, numbers, pattern=RENAME_PATTERN, name="Nightwing"):
    from core.database import set_user_preference

    if pattern is not None:
        set_user_preference("custom_rename_pattern", pattern, category="file_processing")

    series_dir = tmp_path / name
    series_dir.mkdir()
    for filename in filenames:
        (series_dir / filename).write_bytes(b"stub")

    series_id = create_series(name=name, volume=2016, mapped_path=str(series_dir))
    for number in numbers:
        create_issue(series_id=series_id, number=number)
    return series_id, str(series_dir)


class TestIssueNumberBoundary:
    """A file must only satisfy the issue it actually is."""

    def test_issue_one_is_not_satisfied_by_issue_51s_file(self, db_connection, tmp_path):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path, ["Nightwing 051 (2016).cbz"], ["1", "51"]
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert status["51"]["found"] is True
        assert status["1"]["found"] is False
        assert status["1"]["file_path"] is None

    def test_issue_ten_is_not_satisfied_by_issue_110s_file(self, db_connection, tmp_path):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path, ["Nightwing 110 (2016).cbz"], ["10", "110"]
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert status["110"]["found"] is True
        assert status["10"]["found"] is False

    def test_owned_issues_map_to_their_own_file(self, db_connection, tmp_path):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path,
            ["Nightwing 051 (2016).cbz", "Nightwing 052 (2016).cbz"],
            ["1", "51", "52"],
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert status["51"]["file_path"].endswith("Nightwing 051 (2016).cbz")
        assert status["52"]["file_path"].endswith("Nightwing 052 (2016).cbz")
        assert status["1"]["found"] is False


class TestFileClaiming:
    """A precise match consumes its file so it can't be reported twice."""

    def test_one_file_is_claimed_by_only_one_issue(self, db_connection, tmp_path):
        """Two issue rows that normalize to the same number ("7" and "07") both
        compile to the same pattern. Only one may take the single file on disk -
        without consumption both reported it and the series looked one issue
        more complete than it was.

        The filename has no separator before the number so the loose 4c fallback
        (which deliberately does not consume) cannot rescue the second issue -
        this isolates the precise-tier claim.
        """
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path, ["Nightwing007 (2016).cbz"], ["7", "07"]
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        found_paths = [s["file_path"] for s in status.values() if s["found"]]
        assert len(found_paths) == 1
        assert len(found_paths) == len(set(found_paths))

    def test_distinct_issues_keep_their_own_files(self, db_connection, tmp_path):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path,
            ["Nightwing 001 (2016).cbz", "Nightwing 002 (2016).cbz"],
            ["1", "2"],
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert status["1"]["file_path"].endswith("Nightwing 001 (2016).cbz")
        assert status["2"]["file_path"].endswith("Nightwing 002 (2016).cbz")

    def test_duplicate_copies_do_not_starve_the_issue(self, db_connection, tmp_path):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path,
            ["Nightwing 001 (2016).cbz", "Nightwing 001 (2016) (Digital).cbz"],
            ["1"],
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert status["1"]["found"] is True

    def test_range_file_still_satisfies_multiple_issues(self, db_connection, tmp_path):
        """The loose filename fallback must NOT consume: a collected/range file
        legitimately covers several issues, and claiming it would mark the rest
        missing and trigger re-downloads."""
        from helpers.collection import match_issues_to_collection

        # An empty rename pattern skips the precise tier entirely, so both
        # issues resolve through the generic "[\s\-_]0*N" fallback.
        series_id, path = _setup(
            tmp_path, ["Nightwing 001-006 (2016).cbz"], ["1", "6"], pattern=""
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert status["1"]["found"] is True
        assert status["6"]["found"] is True
        assert status["1"]["file_path"] == status["6"]["file_path"]


class TestPublicationTypeGuard:
    """An annual/TPB in the series folder numbers its own issues from 1, so it
    must not be offered to any matching tier as a regular issue."""

    def test_annual_does_not_satisfy_issue_one(self, db_connection, tmp_path):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path, ["Nightwing 2022 Annual Annual 001 (2023).cbz"], ["1"]
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert status["1"]["found"] is False
        assert status["1"]["file_path"] is None

    def test_annual_does_not_steal_the_real_issue_file(self, db_connection, tmp_path):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path,
            [
                "Nightwing 2022 Annual Annual 001 (2023).cbz",
                "Nightwing 001 (2016).cbz",
            ],
            ["1"],
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert status["1"]["file_path"].endswith("Nightwing 001 (2016).cbz")

    def test_annual_is_not_rescued_by_the_loose_fallback(self, db_connection, tmp_path):
        """Without a rename pattern the generic " 001 " fallback would match the
        annual too, so the guard has to run before every tier."""
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path,
            ["Nightwing 2022 Annual Annual 001 (2023).cbz"],
            ["1"],
            pattern="",
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert status["1"]["found"] is False

    def test_annual_series_still_matches_its_own_issues(self, db_connection, tmp_path):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path,
            ["Nightwing 2022 Annual Annual 001 (2023).cbz"],
            ["1"],
            name="Nightwing 2022 Annual",
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert status["1"]["found"] is True


class TestNegativeIssueNumbers:
    """Amazing Spider-Man (1963) #-1 — Marvel's 1997 "Flashback" month.

    The reported bug: the wanted page kept listing #-1 as missing even though
    "Amazing Spider-Man -001 (1997).cbz" sat in the mapped folder, because the
    matcher stripped the sign and then looked for zeros in front of it.
    """

    ASM = "Amazing Spider-Man"
    FILES = [
        "Amazing Spider-Man -001 (1997).cbz",
        "Amazing Spider-Man 001 (1963).cbz",
    ]

    def test_minus_one_is_found_on_scan(self, db_connection, tmp_path):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path, self.FILES, ["-1", "1"], name=self.ASM
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert status["-1"]["found"] is True
        assert status["-1"]["file_path"].endswith("Amazing Spider-Man -001 (1997).cbz")

    def test_minus_one_and_one_do_not_share_a_file(self, db_connection, tmp_path):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path, self.FILES, ["-1", "1"], name=self.ASM
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert status["1"]["file_path"].endswith("Amazing Spider-Man 001 (1963).cbz")
        assert status["-1"]["file_path"] != status["1"]["file_path"]

    def test_issue_one_is_still_missing_with_only_the_minus_one_file(
        self, db_connection, tmp_path
    ):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path, ["Amazing Spider-Man -001 (1997).cbz"], ["-1", "1"],
            name=self.ASM,
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert status["-1"]["found"] is True
        assert status["1"]["found"] is False

    def test_padded_db_number_matches_the_same_file(self, db_connection, tmp_path):
        """Providers hand back "-1" or "-01" — both are the same issue."""
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path, ["Amazing Spider-Man -001 (1997).cbz"], ["-01"],
            name=self.ASM,
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert status["-01"]["found"] is True

    def test_loose_fallback_also_honours_the_sign(self, db_connection, tmp_path):
        """With no rename pattern configured, tier 4c must agree with 4a."""
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path, ["Amazing Spider-Man -001 (1997).cbz"], ["-1", "1"],
            pattern="", name=self.ASM,
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert status["-1"]["found"] is True
        assert status["1"]["found"] is False


class TestStaleCacheSelfHeals:
    """Existing installs carry rows written by the pre-fix matcher.

    collection_status is a cache, not a record: it is invalidated when the
    folder holds more comic files than the cache matched while something is
    still missing. A stale "-1 is missing" row always satisfies that (the -1
    file is either unclaimed, or claimed by #1 leaving #1's file unclaimed), so
    the fix reaches existing libraries on the next scan without a purge.
    """

    ASM = "Amazing Spider-Man"

    def test_cached_missing_minus_one_is_rescanned(self, db_connection, tmp_path):
        from core.database import save_collection_status_bulk, get_issues_for_series
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path,
            [
                "Amazing Spider-Man -001 (1997).cbz",
                "Amazing Spider-Man 001 (1963).cbz",
            ],
            ["-1", "1"],
            name=self.ASM,
        )
        issue_ids = {str(i["number"]): i["id"] for i in get_issues_for_series(series_id)}

        # What the old matcher wrote: #1 pointing at the -1 file, -1 missing.
        wrong_file = str(tmp_path / self.ASM / "Amazing Spider-Man -001 (1997).cbz")
        save_collection_status_bulk([
            {
                "series_id": series_id, "issue_id": issue_ids["1"],
                "issue_number": "1", "found": 1, "file_path": wrong_file,
                "file_mtime": None, "matched_via": "pattern",
            },
            {
                "series_id": series_id, "issue_id": issue_ids["-1"],
                "issue_number": "-1", "found": 0, "file_path": None,
                "file_mtime": None, "matched_via": None,
            },
        ])

        # use_cache=True: the stale rows must not be trusted.
        issue_objs, series_obj = _objs(series_id)
        status = match_issues_to_collection(path, issue_objs, series_obj)

        assert status["-1"]["found"] is True
        assert status["-1"]["file_path"].endswith("Amazing Spider-Man -001 (1997).cbz")
        assert status["1"]["file_path"].endswith("Amazing Spider-Man 001 (1963).cbz")


class TestIssueAddedAfterCaching:
    """A series re-fetch can add an issue the cache has no row for (#797).

    With every cached row found and unchanged the cache read as valid, and the
    cached branch answers from cached rows only, so the new issue was absent
    from the result. Callers read absent as missing: it went on the wanted
    list and was downloaded again while its file sat in the folder.
    """

    FILES = [f"Nightwing {n:03d} (2016).cbz" for n in range(1, 7)]

    def test_new_issue_with_file_on_disk_is_found(self, db_connection, tmp_path):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(tmp_path, self.FILES, ["1", "2", "3", "4", "5"])
        issue_objs, series_obj = _objs(series_id)
        first = match_issues_to_collection(path, issue_objs, series_obj)
        assert all(first[str(n)]["found"] for n in range(1, 6))

        # The sync adds #6; 006.cbz was already in the folder.
        create_issue(series_id=series_id, number="6")
        issue_objs, series_obj = _objs(series_id)
        status = match_issues_to_collection(path, issue_objs, series_obj)

        assert status["6"]["found"] is True
        assert status["6"]["file_path"].endswith("Nightwing 006 (2016).cbz")
        assert all(status[str(n)]["found"] for n in range(1, 6))

    def test_new_issue_gets_a_cache_row(self, db_connection, tmp_path):
        from core.database import get_collection_status_for_series
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(tmp_path, self.FILES, ["1", "2", "3", "4", "5"])
        issue_objs, series_obj = _objs(series_id)
        match_issues_to_collection(path, issue_objs, series_obj)

        create_issue(series_id=series_id, number="6")
        issue_objs, series_obj = _objs(series_id)
        match_issues_to_collection(path, issue_objs, series_obj)

        rows = {r["issue_number"]: r for r in get_collection_status_for_series(series_id)}
        assert rows["6"]["found"] == 1

    def test_new_issue_without_a_file_is_reported_missing(self, db_connection, tmp_path):
        """Absent from the result and "not found" are different answers."""
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(tmp_path, self.FILES[:5], ["1", "2", "3", "4", "5"])
        issue_objs, series_obj = _objs(series_id)
        match_issues_to_collection(path, issue_objs, series_obj)

        create_issue(series_id=series_id, number="6")
        issue_objs, series_obj = _objs(series_id)
        status = match_issues_to_collection(path, issue_objs, series_obj)

        assert status["6"] == {"found": False, "file_path": None}

    def test_complete_cache_is_still_used(self, db_connection, tmp_path, monkeypatch):
        """The new check must not turn every call into a re-scan."""
        import helpers.collection as collection

        series_id, path = _setup(tmp_path, self.FILES[:5], ["1", "2", "3", "4", "5"])
        issue_objs, series_obj = _objs(series_id)
        collection.match_issues_to_collection(path, issue_objs, series_obj)

        def _no_rescan(*args, **kwargs):
            raise AssertionError("a complete cache must not be re-scanned")

        monkeypatch.setattr(collection, "save_collection_status_bulk", _no_rescan, raising=False)
        monkeypatch.setattr("core.database.save_collection_status_bulk", _no_rescan)

        status = collection.match_issues_to_collection(path, issue_objs, series_obj)
        assert all(status[str(n)]["found"] for n in range(1, 6))


def _cbz(path, series, number):
    """A real archive carrying only a ComicInfo.xml."""
    import zipfile

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "ComicInfo.xml",
            f"<ComicInfo><Series>{series}</Series><Number>{number}</Number></ComicInfo>",
        )


def _setup_named(tmp_path, name, filenames, issues, pattern=RENAME_PATTERN):
    """Like ``_setup`` but for any series, with (number, issue name) pairs."""
    from core.database import set_user_preference

    set_user_preference("custom_rename_pattern", pattern or "", category="file_processing")
    series_dir = tmp_path / "series"
    series_dir.mkdir()
    for filename in filenames:
        (series_dir / filename).write_bytes(b"stub")
    series_id = create_series(name=name, volume=2016, mapped_path=str(series_dir))
    for issue in issues:
        number, issue_name = issue if isinstance(issue, tuple) else (issue, None)
        create_issue(series_id=series_id, number=number, name=issue_name)
    return series_id, str(series_dir)


def _found(status):
    return sorted(number for number, row in status.items() if row["found"])


class TestOneFileIsOneIssue:
    """#792: the last tier searched a filename for the wanted number anywhere,
    so one archive was "found" for several issues and the extra ones were
    never wanted or searched. It now reads the number the file names."""

    # (series, file, issue numbers on the series, the ones that are owned)
    CASES = [
        ("Batman", "Batman 400 - Knightfall Part 1.cbz", ["1", "400"], ["400"]),
        ("Spider-Man 2099", "Spider-Man 2099 005 (1993).cbz", ["5", "2099"], ["5"]),
        ("X-23: Target X",
         "X-23 - Target X (2007) Volume 01 Issue 002 - Part Two.cbz",
         ["1", "2", "23"], ["2"]),
        ("The New 52: Futures End",
         "The New 52 - Futures End (2014) Volume 003 - Volume 3.cbz",
         ["3", "52"], ["3"]),
        ("X", "X 010 (2001) - Chapter 2.cbz", ["2", "10"], ["10"]),
        ("X", "X 011 (2001) - Book 3.cbz", ["3", "11"], ["11"]),
        ("X", "X 012 of 6 (2001).cbz", ["6", "12"], ["12"]),
        ("Green Lantern", "Green Lantern 141 (1990) - House on Fire, Part 1.cbz",
         ["1", "141"], ["141"]),
        ("New Avengers", "New Avengers 021 (2005) - Disassembled, Part 1.cbr",
         ["1", "21"], ["21"]),
        # A collected edition in a singles folder is nobody's issue.
        ("Alpha Flight", "Alpha Flight Epic Collection 003 (2020).cbz", ["3"], []),
        # "v2" on a single issue is the series volume, not the issue.
        ("Batman", "Batman v2 012.cbz", ["2", "12"], ["12"]),
    ]

    @pytest.mark.parametrize("pattern", [RENAME_PATTERN, ""])
    @pytest.mark.parametrize("series,filename,numbers,owned", CASES)
    def test_only_the_named_issue_is_owned(
            self, db_connection, tmp_path, series, filename, numbers, owned, pattern):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup_named(tmp_path, series, [filename], numbers, pattern)
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert _found(status) == sorted(owned)

    def test_the_repro_from_the_report(self, db_connection, tmp_path):
        """Dict-shaped issues and series, exactly as the issue calls it."""
        from helpers.collection import match_issues_to_collection

        folder = tmp_path / "Batman"
        folder.mkdir()
        (folder / "Batman 400 - Knightfall Part 1.cbz").write_bytes(b"stub")

        status = match_issues_to_collection(
            str(folder),
            [{"id": 1, "number": "1"}, {"id": 2, "number": "400"}],
            {"id": None, "name": "Batman"},
            use_cache=False,
        )

        assert status["1"] == {"found": False, "file_path": None}
        assert status["400"]["found"] is True

    def test_a_file_whose_name_does_not_start_with_the_series_is_still_read(
            self, db_connection, tmp_path):
        """The tier has no series check, so a folder named differently from
        the provider's series keeps its matches."""
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup_named(
            tmp_path, "Uncanny X-Men", ["X-Men 141 (1981).cbz"], ["1", "141"], ""
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert _found(status) == ["141"]


class TestComicInfoOutranksTheFilename:
    """#792: "Alpha Flight 5 (1983).cbz" tagged <Number>6 was both #5 and #6."""

    def _run(self, tmp_path, pattern):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup_named(tmp_path, "Alpha Flight", [], ["5", "6"], pattern)
        import os
        _cbz(os.path.join(path, "Alpha Flight 5 (1983).cbz"), "Alpha Flight", 6)
        issue_objs, series_obj = _objs(series_id)
        return match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

    def test_without_a_rename_pattern_the_tag_decides(self, db_connection, tmp_path):
        assert _found(self._run(tmp_path, "")) == ["6"]

    def test_the_file_is_never_two_issues(self, db_connection, tmp_path):
        assert len(_found(self._run(tmp_path, RENAME_PATTERN))) == 1

    def test_a_tag_for_an_issue_the_series_lacks_still_refuses(self, db_connection, tmp_path):
        from helpers.collection import match_issues_to_collection
        import os

        series_id, path = _setup_named(tmp_path, "Alpha Flight", [], ["5"], "")
        _cbz(os.path.join(path, "Alpha Flight 5 (1983).cbz"), "Alpha Flight", 6)
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert _found(status) == []

    def test_a_tag_from_another_series_does_not_refuse(self, db_connection, tmp_path):
        from helpers.collection import match_issues_to_collection
        import os

        series_id, path = _setup_named(tmp_path, "Alpha Flight", [], ["5"], "")
        _cbz(os.path.join(path, "Alpha Flight 5 (1983).cbz"), "Marvel Reprints", 1)
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert _found(status) == ["5"]


class TestCollectedVolumes:
    """#795: an owned volume named "Series V02 - Title 2026" or "Series v07
    (2015)" read as missing and was downloaded again."""

    PATTERN = "{series_name} {issue_number} ({volume_year}) - {issue_title}"

    def _status(self, tmp_path, series, filenames, issues):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup_named(tmp_path, series, filenames, issues, self.PATTERN)
        issue_objs, series_obj = _objs(series_id)
        return match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

    def test_volumes_named_by_the_issue_list(self, db_connection, tmp_path):
        status = self._status(
            tmp_path, "Absolute Superman",
            ["Absolute Superman V01 - Last Dust of Krypton 2025.cbz",
             "Absolute Superman V02 - Son of the Demon 2026.cbz"],
            [("1", "Vol. 1: Last Dust of Krypton"), ("2", "Vol. 2: Son of the Demon"),
             ("3", "Vol. 3")],
        )
        assert _found(status) == ["1", "2"]
        assert status["2"]["file_path"].endswith("V02 - Son of the Demon 2026.cbz")

    def test_format_word_in_the_series_name(self, db_connection, tmp_path):
        status = self._status(
            tmp_path, "Avengers Omnibus",
            ["Avengers Omnibus V02 2023.cbz", "Avengers Omnibus V03 2023.cbz"],
            ["1", "2", "3"],
        )
        assert _found(status) == ["2", "3"]

    def test_masterworks_line(self, db_connection, tmp_path):
        status = self._status(
            tmp_path, "Marvel Masterworks: Marvel Two-In-One",
            ["Marvel Masterworks - Marvel Two-In-One v02 (2017).cbz"],
            ["1", "2"],
        )
        assert _found(status) == ["2"]

    def test_lower_case_marker_before_a_title(self, db_connection, tmp_path):
        status = self._status(
            tmp_path, "Batman and Robin",
            ["Batman and Robin v07 - Robin Rises (2015).cbz"],
            [(str(n), f"Volume {n}") for n in range(1, 8)],
        )
        assert _found(status) == ["7"]

    def test_plainly_numbered_volume_still_matches(self, db_connection, tmp_path):
        status = self._status(
            tmp_path, "Avengers Omnibus", ["Avengers Omnibus 002 (2023).cbz"], ["2"]
        )
        assert _found(status) == ["2"]

    def test_a_singles_series_does_not_read_a_bare_marker(self, db_connection, tmp_path):
        """"Batman v2 (2011)" in a singles folder is not Batman #2."""
        status = self._status(tmp_path, "Batman", ["Batman v2 (2011).cbz"], ["2"])
        assert _found(status) == []


class TestFormatWordAfterTheYear:
    """#857: "Symbiote Spider-Man (2019) Volume 01 TPB.cbz" in the five-issue
    series' folder was #1, so #1 read as owned and was never searched for."""

    MINI = ["1", "2", "3", "4", "5"]

    def _status(self, tmp_path, series, filenames, issues, pattern):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup_named(tmp_path, series, filenames, issues, pattern)
        issue_objs, series_obj = _objs(series_id)
        return match_issues_to_collection(path, issue_objs, series_obj, use_cache=False), path

    @pytest.mark.parametrize("pattern", [RENAME_PATTERN, ""])
    @pytest.mark.parametrize("series,filename", [
        ("Symbiote Spider-Man", "Symbiote Spider-Man (2019) Volume 01 TPB.cbz"),
        ("What If? Age of Ultron", "What If - Age of Ultron (2014) Volume 01 TPB.cbr"),
        ("Venom: First Host", "Venom - First Host (2018) Volume 01 TPB.cbz"),
    ])
    def test_a_trade_is_not_issue_one(self, db_connection, tmp_path, series, filename, pattern):
        status, _path = self._status(tmp_path, series, [filename], self.MINI, pattern)
        assert _found(status) == []

    def test_the_real_issue_beside_it_is_the_one_found(self, db_connection, tmp_path):
        status, _path = self._status(
            tmp_path, "Symbiote Spider-Man",
            ["Symbiote Spider-Man (2019) Volume 01 TPB.cbz",
             "Symbiote Spider-Man 001 (2019).cbz"],
            self.MINI, "",
        )
        assert _found(status) == ["1"]
        assert status["1"]["file_path"].endswith("Symbiote Spider-Man 001 (2019).cbz")

    def test_its_comicinfo_number_does_not_claim_the_issue(self, db_connection, tmp_path):
        """A trade is tagged <Number>1 of a volume with the same name."""
        import os
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup_named(tmp_path, "Symbiote Spider-Man", [], self.MINI, "")
        _cbz(os.path.join(path, "Symbiote Spider-Man (2019) Volume 01 TPB.cbz"),
             "Symbiote Spider-Man", 1)
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert _found(status) == []

    @pytest.mark.parametrize("pattern", [RENAME_PATTERN, ""])
    def test_a_one_book_trade_series_keeps_its_file(self, db_connection, tmp_path, pattern):
        """ComicVine's "All-New Doop" TPB volume: one issue, named "TPB"."""
        status, _path = self._status(
            tmp_path, "All-New Doop", ["All-New Doop (2014) Volume 01 TPB.cbz"],
            [("1", "TPB")], pattern,
        )
        assert _found(status) == ["1"]

    @pytest.mark.parametrize("filename", [
        "Nightwing 001 (2016) - Annual Checkup.cbz",
        "Nightwing 001 (2016) (Digital) (TPB-Empire).cbz",
        "Nightwing (2016) Volume 001.cbz",
    ])
    def test_a_real_issue_is_still_owned(self, db_connection, tmp_path, filename):
        """Title text after the year, a release tag, and a name with no format
        word at all are not format labels."""
        status, _path = self._status(tmp_path, "Nightwing", [filename], ["1", "2"], "")
        assert _found(status) == ["1"]


class TestRangeFiles:
    def test_every_issue_in_the_range_is_owned(self, db_connection, tmp_path):
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path, ["Nightwing 001-006 (2016).cbz"], ["1", "3", "6", "7"], pattern=""
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert _found(status) == ["1", "3", "6"]

    def test_a_range_file_claimed_by_one_issue_still_serves_the_rest(
            self, db_connection, tmp_path):
        """With a rename pattern the precise tier claims the file for #1; the
        loose tier must still hand it to the other issues it covers."""
        from helpers.collection import match_issues_to_collection

        series_id, path = _setup(
            tmp_path, ["Nightwing 001-006 (2016).cbz"], ["1", "4", "6"]
        )
        issue_objs, series_obj = _objs(series_id)

        status = match_issues_to_collection(path, issue_objs, series_obj, use_cache=False)

        assert _found(status) == ["1", "4", "6"]
