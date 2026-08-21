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


# ---- spin-off guard -----------------------------------------------------

class TestSpinOffSubtitleGuard:
    """A spin-off must not be filed as an issue of the series it spun off from.

    Every matching tier used to allow arbitrary text between the series name
    and the issue number, so "Teenage Mutant Ninja Turtles - Nightwatcher 003"
    satisfied the wanted entry for TMNT #3. Unlike the collection-status
    matcher, this one drives shutil.move plus a rename, so the mistake is
    destructive: the wrong comic lands in the folder wearing the right name.

    The discriminator is position. A spin-off puts its subtitle BEFORE the
    issue number; a real issue title comes AFTER it.
    """

    TMNT = "Teenage Mutant Ninja Turtles"

    def _match_one(self, tmp_path, filenames, series=None, issue="3"):
        series = series or self.TMNT
        series_dir = tmp_path / series.replace("/", "-")
        series_dir.mkdir()
        files = []
        for name in filenames:
            path = str(tmp_path / name)
            _touch(path)
            files.append((name, path))
        wanted = [_wanted(series, issue, str(series_dir))]
        return match_wanted_issues_to_files(
            wanted, files, PATTERN, alias_lookup=_no_aliases
        )

    def test_dash_subtitled_spinoff_is_not_moved_into_the_parent_series(self, tmp_path):
        """The reported case: four spin-offs and the real issue, all #3 (2024)."""
        matches = self._match_one(tmp_path, [
            "Teenage Mutant Ninja Turtles - Saturday Morning Adventures 003.cbz",
            "Teenage Mutant Ninja Turtles - Nightwatcher 003 (2024).cbz",
            "Teenage Mutant Ninja Turtles 003 (2024).cbz",
            "Teenage Mutant Ninja Turtles - The Last Ronin II - Re-Evolution 003 (2024).cbz",
            "Teenage Mutant Ninja Turtles - Black, White, & Green 003 (2024).cbz",
        ])
        assert len(matches) == 1
        assert matches[0]["filename"] == "Teenage Mutant Ninja Turtles 003 (2024).cbz"

    def test_spinoff_alone_yields_no_match(self, tmp_path):
        """With the real issue absent, the spin-off must not stand in for it."""
        matches = self._match_one(
            tmp_path, ["Teenage Mutant Ninja Turtles - Nightwatcher 003 (2024).cbz"]
        )
        assert matches == []

    def test_dashless_getcomics_filename_is_still_rejected(self, tmp_path):
        r"""The filename CLU actually writes for a GetComics grab.

        clu-source-search.js sanitises the post title with
        /[^a-zA-Z0-9\s\-#]/g, which strips the en dash getcomics.org uses --
        so the file arrives with no separator at all. A dash-based guard would
        miss this; the rule is "no letters in the gap", not "no dash".
        """
        matches = self._match_one(
            tmp_path, ["Teenage Mutant Ninja Turtles  Nightwatcher 3 2024.cbz"]
        )
        assert matches == []

    def test_en_dash_spinoff_is_rejected(self, tmp_path):
        matches = self._match_one(
            tmp_path,
            ["Teenage Mutant Ninja Turtles \u2013 Black, White, & Green 003 (2024).cbz"],
        )
        assert matches == []

    def test_nested_spinoff_is_rejected(self, tmp_path):
        matches = self._match_one(
            tmp_path,
            ["Teenage Mutant Ninja Turtles - The Last Ronin II - Re-Evolution 003 (2024).cbz"],
        )
        assert matches == []

    def test_comicinfo_cannot_smuggle_a_spinoff_in(self, tmp_path):
        """The ComicInfo tier never sees the filename, so it needs its own guard."""
        series_dir = tmp_path / "Teenage Mutant Ninja Turtles"
        series_dir.mkdir()
        f = str(tmp_path / "Nightwatcher 003.cbz")
        _make_cbz_with_comicinfo(
            f, "Teenage Mutant Ninja Turtles: Nightwatcher", "3"
        )
        wanted = [_wanted(self.TMNT, "3", str(series_dir))]
        matches = match_wanted_issues_to_files(
            wanted, [("Nightwatcher 003.cbz", f)], PATTERN, alias_lookup=_no_aliases
        )
        assert matches == []

    # -- non-regression: everything the guard must NOT break ---------------

    def test_issue_subtitle_after_the_number_still_matches(self, tmp_path):
        """A story-arc title following the issue number is normal and fine."""
        matches = self._match_one(
            tmp_path, ["Nightwing 117 - Absolute Power.cbz"],
            series="Nightwing", issue="117",
        )
        assert len(matches) == 1

    def test_no_separator_still_matches(self, tmp_path):
        matches = self._match_one(
            tmp_path, ["Nightwing001.cbz"], series="Nightwing", issue="1"
        )
        assert len(matches) == 1

    def test_punctuated_series_name_still_matches(self, tmp_path):
        matches = self._match_one(
            tmp_path, ["K.O. 003.cbz"], series="K.O.", issue="3"
        )
        assert len(matches) == 1

    @pytest.mark.parametrize("filename", [
        "Batman v3 005 (2016).cbz",
        "Batman Vol. 3 005 (2016).cbz",
        "Batman No. 5 (2016).cbz",
        "Batman Issue 5.cbz",
    ])
    def test_volume_marker_between_series_and_issue_still_matches(self, tmp_path, filename):
        matches = self._match_one(
            tmp_path, [filename], series="Batman", issue="5"
        )
        assert len(matches) == 1, filename

    @pytest.mark.parametrize("filename", [
        "Batman (2016) 005.cbz",
        "Batman [2016] #005.cbz",
        "Batman 2024 005.cbz",
    ])
    def test_release_tags_between_series_and_issue_still_match(self, tmp_path, filename):
        matches = self._match_one(
            tmp_path, [filename], series="Batman", issue="5"
        )
        assert len(matches) == 1, filename

    def test_words_that_are_not_volume_markers_still_block(self, tmp_path):
        """"No" is only a marker when digits follow it."""
        matches = self._match_one(
            tmp_path, ["Batman - No Mans Land 005.cbz"], series="Batman", issue="5"
        )
        assert matches == []

    def test_comicinfo_shorter_series_still_matches(self, tmp_path):
        """DB "The Ultimates" vs ComicInfo "Ultimates" -- the safe direction."""
        series_dir = tmp_path / "The Ultimates"
        series_dir.mkdir()
        f = str(tmp_path / "unhelpful.cbz")
        _make_cbz_with_comicinfo(f, "Ultimates", "5")
        wanted = [_wanted("The Ultimates", "5", str(series_dir))]
        matches = match_wanted_issues_to_files(
            wanted, [("unhelpful.cbz", f)], PATTERN, alias_lookup=_no_aliases
        )
        assert len(matches) == 1

    def test_comicinfo_volume_tagged_series_still_matches(self, tmp_path):
        """ComicInfo "Batman (2016)" is Batman, not a spin-off."""
        series_dir = tmp_path / "Batman"
        series_dir.mkdir()
        f = str(tmp_path / "unhelpful.cbz")
        _make_cbz_with_comicinfo(f, "Batman (2016)", "5")
        wanted = [_wanted("Batman", "5", str(series_dir))]
        matches = match_wanted_issues_to_files(
            wanted, [("unhelpful.cbz", f)], PATTERN, alias_lookup=_no_aliases
        )
        assert len(matches) == 1


class TestNegativeIssueNumbers:
    """A wanted "-1" must find the file it already has, and only that file.

    This is the download-scan half of the -1 bug: with the sign discarded, the
    scan never recognized "Amazing Spider-Man -001 (1997).cbz" as issue -1, and
    a wanted #1 would have grabbed it — moving and renaming the wrong comic.
    """

    ASM = "Amazing Spider-Man"

    def _match(self, tmp_path, filenames, wanted_numbers):
        series_dir = tmp_path / self.ASM
        series_dir.mkdir()
        files = []
        for name in filenames:
            path = str(tmp_path / name)
            _touch(path)
            files.append((name, path))
        wanted = [_wanted(self.ASM, n, str(series_dir)) for n in wanted_numbers]
        return match_wanted_issues_to_files(
            wanted, files, PATTERN, alias_lookup=_no_aliases
        )

    def test_minus_one_matches_its_file(self, tmp_path):
        matches = self._match(
            tmp_path, ["Amazing Spider-Man -001 (1997).cbz"], ["-1"]
        )
        assert len(matches) == 1
        assert matches[0]["filename"] == "Amazing Spider-Man -001 (1997).cbz"

    def test_minus_one_and_one_keep_their_own_files(self, tmp_path):
        matches = self._match(
            tmp_path,
            [
                "Amazing Spider-Man -001 (1997).cbz",
                "Amazing Spider-Man 001 (1963).cbz",
            ],
            ["1", "-1"],
        )
        by_number = {m["issue"]["number"]: m["filename"] for m in matches}
        assert by_number["1"] == "Amazing Spider-Man 001 (1963).cbz"
        assert by_number["-1"] == "Amazing Spider-Man -001 (1997).cbz"

    def test_issue_one_does_not_move_the_minus_one_file(self, tmp_path):
        matches = self._match(
            tmp_path, ["Amazing Spider-Man -001 (1997).cbz"], ["1"]
        )
        assert matches == []

    def test_comicinfo_padded_negative_still_matches(self, tmp_path):
        """<Number>-01</Number> is the same issue as a wanted "-1"."""
        series_dir = tmp_path / self.ASM
        series_dir.mkdir()
        f = str(tmp_path / "unhelpful.cbz")
        _make_cbz_with_comicinfo(f, self.ASM, "-01")
        wanted = [_wanted(self.ASM, "-1", str(series_dir))]
        matches = match_wanted_issues_to_files(
            wanted, [("unhelpful.cbz", f)], PATTERN, alias_lookup=_no_aliases
        )
        assert len(matches) == 1

    def test_comicinfo_negative_does_not_satisfy_issue_one(self, tmp_path):
        series_dir = tmp_path / self.ASM
        series_dir.mkdir()
        f = str(tmp_path / "unhelpful.cbz")
        _make_cbz_with_comicinfo(f, self.ASM, "-1")
        wanted = [_wanted(self.ASM, "1", str(series_dir))]
        matches = match_wanted_issues_to_files(
            wanted, [("unhelpful.cbz", f)], PATTERN, alias_lookup=_no_aliases
        )
        assert matches == []
