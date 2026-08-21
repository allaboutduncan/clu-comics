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
