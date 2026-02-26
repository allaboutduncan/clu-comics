"""Shared fixtures for mocked tests -- mock objects for external APIs."""
import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass
from typing import Optional

from models.providers.base import ProviderCredentials, SearchResult, IssueResult, ProviderType


# ---------------------------------------------------------------------------
# Common credential fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def metron_creds():
    return ProviderCredentials(username="testuser", password="testpass")


@pytest.fixture
def comicvine_creds():
    return ProviderCredentials(api_key="fake-cv-api-key")


@pytest.fixture
def gcd_creds():
    return ProviderCredentials(
        host="localhost", port="3306", database="gcd",
        username="root", password="pass",
    )


# ---------------------------------------------------------------------------
# Mock Mokkari objects
# ---------------------------------------------------------------------------

def make_mock_series(*, id=100, name="Batman", year_began=2016, publisher_name="DC Comics", cv_id=12345):
    """Create a mock Mokkari series object."""
    s = MagicMock()
    s.id = id
    s.name = name
    s.year_began = year_began
    s.cv_id = cv_id
    s.display_name = name
    pub = MagicMock()
    pub.name = publisher_name
    s.publisher = pub
    return s


def make_mock_issue(*, id=500, number="1", name=None, cover_date="2020-01-15",
                    store_date="2020-01-13", image="https://example.com/cover.jpg",
                    series_id=100, desc="A great issue"):
    """Create a mock Mokkari issue object."""
    i = MagicMock()
    i.id = id
    i.number = number
    i.name = [name] if name else ["Issue Title"]
    i.cover_date = cover_date
    i.store_date = store_date
    i.image = image
    i.desc = desc
    i.story_titles = i.name
    series = MagicMock()
    series.id = series_id
    series.name = "Batman"
    series.year_began = 2016
    series.genres = []
    i.series = series
    i.publisher = MagicMock(name="DC Comics")
    i.publisher.name = "DC Comics"
    i.credits = []
    i.characters = []
    i.teams = []
    i.rating = MagicMock(name="Teen")
    i.rating.name = "Teen"
    i.resource_url = "https://metron.cloud/issue/500/"
    i.modified = "2024-01-01"
    i.page_count = 32
    # Support model_dump for Pydantic conversion
    i.model_dump = MagicMock(return_value={
        "id": id, "number": number, "story_titles": i.name,
        "cover_date": cover_date, "store_date": store_date,
        "series": {"id": series_id, "name": "Batman", "year_began": 2016, "genres": []},
        "publisher": {"name": "DC Comics"}, "credits": [], "characters": [], "teams": [],
        "rating": {"name": "Teen"}, "desc": desc,
        "resource_url": "https://metron.cloud/issue/500/", "modified": "2024-01-01",
        "page_count": 32, "image": image,
    })
    return i


# ---------------------------------------------------------------------------
# Mock Simyan/ComicVine objects
# ---------------------------------------------------------------------------

def make_mock_cv_volume(*, id=4050, name="Batman", start_year=2016,
                        publisher_name="DC Comics", count_of_issues=50):
    v = MagicMock()
    v.id = id
    v.name = name
    v.start_year = start_year
    v.count_of_issues = count_of_issues
    v.description = "The Dark Knight"
    pub = MagicMock()
    pub.name = publisher_name
    v.publisher = pub
    img = MagicMock()
    img.thumbnail = "https://example.com/thumb.jpg"
    v.image = img
    return v


def make_mock_cv_issue(*, id=1001, issue_number="1", name="Rebirth",
                       cover_date="2020-01-15", store_date=None):
    i = MagicMock()
    i.id = id
    i.issue_number = issue_number
    i.name = name
    i.cover_date = cover_date
    i.store_date = store_date
    i.description = "Batman returns"
    img = MagicMock()
    img.small_url = "https://example.com/small.jpg"
    img.thumb_url = "https://example.com/thumb.jpg"
    i.image = img
    vol = MagicMock()
    vol.id = 4050
    vol.name = "Batman"
    i.volume = vol
    return i


# ---------------------------------------------------------------------------
# Mock MySQL cursor/connection
# ---------------------------------------------------------------------------

def make_mock_mysql_connection(rows=None, fetchone_result=None):
    """Create a mock MySQL connection and cursor."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = rows or []
    cursor.fetchone.return_value = fetchone_result
    conn.cursor.return_value = cursor
    return conn, cursor
