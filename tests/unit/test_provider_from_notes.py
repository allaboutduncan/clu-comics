"""provider_from_notes maps every provider's Notes wording to one id."""
import pytest

from core.comicinfo import (
    provider_from_notes, manual_edit_notes, fill_blank_comicinfo_bytes,
)

NOTES = [
    ("Metadata from Metron. Resource URL: https://x — modified 2024-01-01.", "metron"),
    ("Metadata from Metron. Issue ID: 5", "metron"),
    ("Metadata from ComicVine. Issue ID: 5", "comicvine"),
    ("Metadata from ComicVine CVDB. Volume ID: 1 — retrieved 2024-01-01.", "comicvine_sqlite"),
    ("Metadata from ComicVine (Local DB). Issue ID: 5", "comicvine_sqlite"),
    ("Metadata from GCD (Grand Comics Database). Series ID: 1", "gcd"),
    ("Metadata from Grand Comics Database. Issue ID: 5", "gcd"),
    ("Metadata from Grand Comic Database (GCD). Issue ID: 5", "gcd"),
    ("Metadata from GCD REST API. Issue ID: 5", "gcd_api"),
    ("Metadata from INDUCKS. Issue: x", "inducks"),
    ("Metadata from AniList. Media ID: 1", "anilist"),
    ("Metadata from Bedetheque. Album ID: 1", "bedetheque"),
    ("Status: Ongoing. Metadata from MangaDex.", "mangadex"),
    ("Metadata from MangaUpdates.", "mangaupdates"),
]


@pytest.mark.parametrize("notes,expected", NOTES)
def test_known_wording(notes, expected):
    assert provider_from_notes(notes) == expected


def test_manual_stamp():
    assert provider_from_notes(manual_edit_notes()) == "manual"


@pytest.mark.parametrize("notes", [
    None, "", "   ", "Scraped metadata from Amazon", "hand written note",
    "I like Metron", "Metadata from SomethingElse",
])
def test_no_provider(notes):
    assert provider_from_notes(notes) is None


class TestFillBlank:
    def test_existing_wins_blank_filled_notes_kept(self):
        old = b"<ComicInfo><Title>Mine</Title><Writer></Writer><Notes>Metadata from Metron.</Notes></ComicInfo>"
        new = b"<ComicInfo><Title>Theirs</Title><Writer>Alan</Writer><Year>2020</Year><Notes>Metadata from ComicVine.</Notes></ComicInfo>"
        out, filled = fill_blank_comicinfo_bytes(old, new)
        assert sorted(filled) == ["Writer", "Year"]
        assert b"<Title>Mine</Title>" in out
        assert b"<Writer>Alan</Writer>" in out
        assert b"Metadata from Metron." in out
        assert b"ComicVine" not in out

    def test_nothing_to_fill_returns_original(self):
        old = b"<ComicInfo><Title>Mine</Title></ComicInfo>"
        out, filled = fill_blank_comicinfo_bytes(old, b"<ComicInfo><Title>X</Title></ComicInfo>")
        assert out == old and filled == []

    def test_unparseable_is_noop(self):
        out, filled = fill_blank_comicinfo_bytes(b"<ComicInfo>", b"<ComicInfo/>")
        assert out == b"<ComicInfo>" and filled == []
