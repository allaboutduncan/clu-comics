"""Tests for models/providers/base.py -- dataclasses and extract_issue_number."""
import pytest
from models.providers.base import (
    ProviderType,
    SearchResult,
    IssueResult,
    ProviderCredentials,
    extract_issue_number,
)


# ===== ProviderType =====

class TestProviderType:

    def test_values(self):
        assert ProviderType.METRON.value == "metron"
        assert ProviderType.COMICVINE.value == "comicvine"
        assert ProviderType.GCD.value == "gcd"
        assert ProviderType.ANILIST.value == "anilist"
        assert ProviderType.BEDETHEQUE.value == "bedetheque"
        assert ProviderType.MANGADEX.value == "mangadex"

    def test_from_string(self):
        assert ProviderType("metron") == ProviderType.METRON

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            ProviderType("nonexistent")


# ===== SearchResult =====

class TestSearchResult:

    def test_to_dict(self):
        sr = SearchResult(
            provider=ProviderType.METRON,
            id="42",
            title="Batman",
            year=2020,
            publisher="DC Comics",
            issue_count=85,
            cover_url="https://example.com/cover.jpg",
            description="A dark knight story",
        )
        d = sr.to_dict()
        assert d["provider"] == "metron"
        assert d["id"] == "42"
        assert d["title"] == "Batman"
        assert d["year"] == 2020
        assert d["publisher"] == "DC Comics"
        assert d["issue_count"] == 85
        assert d["cover_url"] == "https://example.com/cover.jpg"
        assert d["description"] == "A dark knight story"

    def test_defaults_are_none(self):
        sr = SearchResult(provider=ProviderType.COMICVINE, id="1", title="Test")
        assert sr.year is None
        assert sr.publisher is None
        assert sr.issue_count is None
        assert sr.cover_url is None
        assert sr.description is None


# ===== IssueResult =====

class TestIssueResult:

    def test_to_dict(self):
        ir = IssueResult(
            provider=ProviderType.COMICVINE,
            id="100",
            series_id="42",
            issue_number="5",
            title="Pilot",
            cover_date="2020-01-15",
            store_date="2020-01-10",
            cover_url="https://example.com/issue.jpg",
            summary="First issue",
        )
        d = ir.to_dict()
        assert d["provider"] == "comicvine"
        assert d["id"] == "100"
        assert d["series_id"] == "42"
        assert d["issue_number"] == "5"
        assert d["title"] == "Pilot"
        assert d["summary"] == "First issue"

    def test_defaults_are_none(self):
        ir = IssueResult(
            provider=ProviderType.METRON, id="1", series_id="1", issue_number="1"
        )
        assert ir.title is None
        assert ir.cover_date is None
        assert ir.store_date is None
        assert ir.cover_url is None
        assert ir.summary is None


# ===== ProviderCredentials =====

class TestProviderCredentials:

    def test_to_dict_excludes_none(self):
        creds = ProviderCredentials(api_key="abc123")
        d = creds.to_dict()
        assert d == {"api_key": "abc123"}
        assert "username" not in d
        assert "password" not in d

    def test_to_dict_full(self):
        creds = ProviderCredentials(
            api_key="key",
            username="user",
            password="pass",
            host="localhost",
            port=3306,
            database="gcd",
        )
        d = creds.to_dict()
        assert len(d) == 6
        assert d["port"] == 3306

    def test_from_dict_roundtrip(self):
        original = {"api_key": "abc", "username": "user", "host": "localhost"}
        creds = ProviderCredentials.from_dict(original)
        assert creds.api_key == "abc"
        assert creds.username == "user"
        assert creds.host == "localhost"
        assert creds.password is None

    def test_from_dict_empty(self):
        creds = ProviderCredentials.from_dict({})
        assert creds.api_key is None
        assert creds.username is None


# ===== extract_issue_number =====

class TestExtractIssueNumber:

    @pytest.mark.parametrize("filename,expected", [
        # "Issue" keyword pattern
        ("Amazing Spider-Man (2018) Issue 080.BEY.cbz", "80.BEY"),
        ("Amazing Spider-Man (1999) Issue 700.1.cbz", "700.1"),
        # Hash pattern
        ("Batman #42.cbz", "42"),
        ("Batman #042.cbz", "42"),
        # 3+ digit pattern
        ("Amazing Spider-Man 078.BEY (2022).cbz", "78.BEY"),
        ("Amazing Spider-Man 001.cbz", "1"),
        ("X-Men v2 012.cbz", "12"),
        # Series with year in name (should get issue, not year)
        ("Spider-Man 2099 001 (1992).cbz", "1"),
        # Dash/underscore + digits
        ("Comic-001.cbz", "1"),
        # 1-2 digit pattern
        ("Batman 42 (2020).cbz", "42"),
        # Decimal issues
        ("Batman 050.LR.cbz", "50.LR"),
    ])
    def test_extraction(self, filename, expected):
        assert extract_issue_number(filename) == expected

    def test_no_match_returns_none(self):
        assert extract_issue_number("just-a-name.cbz") is None

    def test_strips_leading_zeros(self):
        assert extract_issue_number("Comic 001.cbz") == "1"
        assert extract_issue_number("Comic 010.cbz") == "10"
