"""Tests for models/issue.py -- IssueObj and SeriesObj data classes."""
import pytest


class TestIssueObj:

    def test_all_fields_from_dict(self):
        from models.issue import IssueObj
        data = {
            "number": "42",
            "id": 12345,
            "name": "The Answer",
            "store_date": "2024-01-15",
            "cover_date": "2024-02-01",
            "image": "https://example.com/cover.jpg",
        }
        issue = IssueObj(data)
        assert issue.number == "42"
        assert issue.id == 12345
        assert issue.name == "The Answer"
        assert issue.store_date == "2024-01-15"
        assert issue.cover_date == "2024-02-01"
        assert issue.image == "https://example.com/cover.jpg"

    def test_missing_fields_are_none(self):
        from models.issue import IssueObj
        issue = IssueObj({})
        assert issue.number is None
        assert issue.id is None
        assert issue.name is None
        assert issue.store_date is None
        assert issue.cover_date is None
        assert issue.image is None

    def test_partial_data(self):
        from models.issue import IssueObj
        issue = IssueObj({"number": "1", "name": "Pilot"})
        assert issue.number == "1"
        assert issue.name == "Pilot"
        assert issue.id is None


class TestSeriesObj:

    def test_all_fields_from_dict(self):
        from models.issue import SeriesObj
        data = {"name": "Batman", "volume": 2020, "id": 999}
        series = SeriesObj(data)
        assert series.name == "Batman"
        assert series.volume == 2020
        assert series.id == 999

    def test_missing_fields_are_none(self):
        from models.issue import SeriesObj
        series = SeriesObj({})
        assert series.name is None
        assert series.volume is None
        assert series.id is None

    def test_partial_data(self):
        from models.issue import SeriesObj
        series = SeriesObj({"name": "X-Men"})
        assert series.name == "X-Men"
        assert series.volume is None
        assert series.id is None
