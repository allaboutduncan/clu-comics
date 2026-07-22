"""Tests for helpers.collection.match_wanted_issues_to_files and its
per-file ComicInfo memoizer.

This is the single-pass matcher that replaced the old per-issue x per-file
re-scan in process_incoming_wanted_issues(). The tests lock in both the
matching behavior (filename regex, ComicInfo fallback, aliases, one-file-per-
issue) and the optimization guarantee (each archive is opened at most once).
"""
import io
import os
import zipfile

import pytest

from helpers.collection import (
    match_wanted_issues_to_files,
    extract_comicinfo_cached,
)

# Year/month/title-stripped pattern, exactly as process_incoming_wanted_issues
# feeds it to the matcher.
PATTERN = "{series_name} {issue_number}"


# ---- helpers ------------------------------------------------------------

def _touch(path, data=b"stub"):
    with open(path, "wb") as f:
        f.write(data)


def _make_cbz_with_comicinfo(path, series, number):
    """Write a minimal CBZ carrying a ComicInfo.xml with series/number."""
    xml = (
        '<?xml version="1.0"?>'
        f"<ComicInfo><Series>{series}</Series>"
        f"<Number>{number}</Number></ComicInfo>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ComicInfo.xml", xml)
        zf.writestr("001.jpg", b"\xff\xd8\xff")


def _wanted(series_name, number, mapped_path, series_id=1):
    return {
        "series_name": series_name,
        "number": number,
        "mapped_path": mapped_path,
        "series_id": series_id,
    }


def _no_aliases(_name):
    return ""


# ---- filename matching --------------------------------------------------

class TestFilenameMatching:

    def test_matches_by_filename(self, tmp_path):
        series_dir = tmp_path / "Batman"
        series_dir.mkdir()
        f = str(tmp_path / "Batman 005 (2020).cbz")
        _touch(f)

        wanted = [_wanted("Batman", "5", str(series_dir))]
        files = [("Batman 005 (2020).cbz", f)]

        matches = match_wanted_issues_to_files(
            wanted, files, PATTERN, alias_lookup=_no_aliases
        )
        assert len(matches) == 1
        assert matches[0]["src"] == f
        assert matches[0]["issue"]["number"] == "5"

    def test_no_match_leaves_empty(self, tmp_path):
        series_dir = tmp_path / "Batman"
        series_dir.mkdir()
        f = str(tmp_path / "Superman 005 (2020).cbz")
        _touch(f)

        wanted = [_wanted("Batman", "5", str(series_dir))]
        files = [("Superman 005 (2020).cbz", f)]

        matches = match_wanted_issues_to_files(
            wanted, files, PATTERN, alias_lookup=_no_aliases
        )
        assert matches == []

    def test_wrong_issue_number_does_not_match(self, tmp_path):
        series_dir = tmp_path / "Batman"
        series_dir.mkdir()
        f = str(tmp_path / "Batman 007 (2020).cbz")
        _touch(f)

        wanted = [_wanted("Batman", "5", str(series_dir))]
        files = [("Batman 007 (2020).cbz", f)]

        matches = match_wanted_issues_to_files(
            wanted, files, PATTERN, alias_lookup=_no_aliases
        )
        assert matches == []

    def test_folder_derived_name_matches_the_prefix(self, tmp_path):
        # DB name "The Ultimates" but files on disk say "Ultimates" -- the
        # folder-derived name plus the-prefix flexibility should still match.
        series_dir = tmp_path / "Ultimates"
        series_dir.mkdir()
        _touch(str(series_dir / "Ultimates 001 (2024).cbz"))  # sample existing file

        target = str(tmp_path / "Ultimates 002 (2024).cbz")
        _touch(target)

        wanted = [_wanted("The Ultimates", "2", str(series_dir))]
        files = [("Ultimates 002 (2024).cbz", target)]

        matches = match_wanted_issues_to_files(
            wanted, files, PATTERN, alias_lookup=_no_aliases
        )
        assert len(matches) == 1
        assert matches[0]["src"] == target


# ---- alias matching -----------------------------------------------------

class TestAliasMatching:

    def test_matches_via_alias(self, tmp_path):
        series_dir = tmp_path / "Thor"
        series_dir.mkdir()
        f = str(tmp_path / "Mortal Thor 011 (2024).cbz")
        _touch(f)

        wanted = [_wanted("Thor", "11", str(series_dir))]
        files = [("Mortal Thor 011 (2024).cbz", f)]

        matches = match_wanted_issues_to_files(
            wanted, files, PATTERN, alias_lookup=lambda n: "Mortal Thor"
        )
        assert len(matches) == 1
        assert matches[0]["src"] == f


# ---- ComicInfo fallback -------------------------------------------------

class TestComicInfoFallback:

    def test_matches_via_comicinfo_when_filename_unhelpful(self, tmp_path):
        series_dir = tmp_path / "Saga"
        series_dir.mkdir()
        f = str(tmp_path / "download_xyz.cbz")  # filename gives no clue
        _make_cbz_with_comicinfo(f, series="Saga", number="12")

        wanted = [_wanted("Saga", "12", str(series_dir))]
        files = [("download_xyz.cbz", f)]

        matches = match_wanted_issues_to_files(
            wanted, files, PATTERN, alias_lookup=_no_aliases
        )
        assert len(matches) == 1
        assert matches[0]["src"] == f

    def test_comicinfo_wrong_number_rejected(self, tmp_path):
        series_dir = tmp_path / "Saga"
        series_dir.mkdir()
        f = str(tmp_path / "download_xyz.cbz")
        _make_cbz_with_comicinfo(f, series="Saga", number="99")

        wanted = [_wanted("Saga", "12", str(series_dir))]
        files = [("download_xyz.cbz", f)]

        matches = match_wanted_issues_to_files(
            wanted, files, PATTERN, alias_lookup=_no_aliases
        )
        assert matches == []


# ---- one file per issue -------------------------------------------------

class TestOneFilePerIssue:

    def test_file_matched_to_first_issue_only(self, tmp_path):
        series_dir = tmp_path / "Batman"
        series_dir.mkdir()
        f = str(tmp_path / "Batman 005 (2020).cbz")
        _touch(f)

        # Two identical wanted rows for #5; only one file exists.
        wanted = [
            _wanted("Batman", "5", str(series_dir)),
            _wanted("Batman", "5", str(series_dir)),
        ]
        files = [("Batman 005 (2020).cbz", f)]

        matches = match_wanted_issues_to_files(
            wanted, files, PATTERN, alias_lookup=_no_aliases
        )
        assert len(matches) == 1


# ---- optimization guarantee: archive opened at most once ----------------

class TestArchiveOpenedOnce:

    def test_each_archive_opened_at_most_once(self, tmp_path, monkeypatch):
        import helpers.collection as collection

        series_dir = tmp_path / "Saga"
        series_dir.mkdir()
        # A file that never matches by filename, so the ComicInfo fallback is
        # consulted for every wanted issue -- the pre-optimization hot path.
        f = str(tmp_path / "nomatch.cbz")
        _make_cbz_with_comicinfo(f, series="Nothing", number="0")

        calls = {"n": 0}
        real = collection.extract_comicinfo

        def counting(path):
            calls["n"] += 1
            return real(path)

        monkeypatch.setattr(collection, "extract_comicinfo", counting)

        # 20 wanted issues, all forcing the fallback against the one file.
        wanted = [_wanted("Saga", str(i), str(series_dir)) for i in range(1, 21)]
        files = [("nomatch.cbz", f)]

        match_wanted_issues_to_files(
            wanted, files, PATTERN, alias_lookup=_no_aliases
        )
        # Without memoization this would be ~20; with it, exactly 1.
        assert calls["n"] == 1


class TestExtractComicInfoCached:

    def test_reads_once_and_caches(self, tmp_path):
        f = str(tmp_path / "x.cbz")
        _make_cbz_with_comicinfo(f, series="Saga", number="1")
        cache = {}
        first = extract_comicinfo_cached(f, cache)
        assert first.get("number") == "1"
        assert f in cache
        # Second call returns the cached object (identity preserved).
        assert extract_comicinfo_cached(f, cache) is first

    def test_non_archive_returns_empty_without_read(self, tmp_path):
        f = str(tmp_path / "x.cbr")  # not cbz/zip -> no disk read
        cache = {}
        assert extract_comicinfo_cached(f, cache) == {}
        assert cache[f] == {}
