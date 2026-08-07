"""Tests for models/comicvine_source.py -- local-DB-first ComicVine access.

Metron stays the preferred provider overall; this module is only consulted once
a sidecar's ComicVine id has failed to resolve to a Metron series. Between the
two ComicVine sources the local dump wins: it costs no requests and no
rate-limit budget, and the per-volume issue listing is the most expensive call
in a library sweep.
"""
from unittest.mock import patch

import pytest

from models import comicvine_source


@pytest.fixture
def no_local(monkeypatch):
    monkeypatch.setattr(comicvine_source, "_local_available", lambda: False)


@pytest.fixture
def no_api(monkeypatch):
    monkeypatch.setattr(comicvine_source, "_api_key", lambda app=None: None)


class TestIsAvailable:
    """Gating ComicVine mapping on the API key alone silently disabled it for
    anyone running only the local dump."""

    def test_available_with_local_db_only(self, no_api, monkeypatch):
        monkeypatch.setattr(comicvine_source, "_local_available", lambda: True)
        assert comicvine_source.is_available() is True

    def test_available_with_api_key_only(self, no_local, monkeypatch):
        monkeypatch.setattr(comicvine_source, "_api_key", lambda app=None: "KEY")
        assert comicvine_source.is_available() is True

    def test_unavailable_with_neither(self, no_local, no_api):
        assert comicvine_source.is_available() is False

    def test_describe_names_the_configured_sources(self, monkeypatch):
        monkeypatch.setattr(comicvine_source, "_local_available", lambda: True)
        monkeypatch.setattr(comicvine_source, "_api_key", lambda app=None: "KEY")
        assert comicvine_source.describe() == "local DB + API"

    def test_describe_when_nothing_configured(self, no_local, no_api):
        assert comicvine_source.describe() == "none"


class TestGetAllIssuesForVolume:

    def test_prefers_the_local_db(self, monkeypatch):
        """The dump answers without spending any API budget."""
        monkeypatch.setattr(comicvine_source, "_local_available", lambda: True)
        monkeypatch.setattr(comicvine_source, "_api_key", lambda app=None: "KEY")

        with patch("models.comicvine_sqlite.get_all_issues_for_volume",
                   return_value=[{"id": 1}]) as local, \
             patch("models.comicvine.get_all_issues_for_volume") as api:
            result = comicvine_source.get_all_issues_for_volume(4050)

        assert result == [{"id": 1}]
        local.assert_called_once_with(4050)
        api.assert_not_called()

    def test_falls_through_to_the_api_when_the_dump_lacks_the_volume(self, monkeypatch):
        """Coverage is the union of both sources, not just the dump's."""
        monkeypatch.setattr(comicvine_source, "_local_available", lambda: True)
        monkeypatch.setattr(comicvine_source, "_api_key", lambda app=None: "KEY")

        with patch("models.comicvine_sqlite.get_all_issues_for_volume",
                   return_value=[]), \
             patch("models.comicvine.get_all_issues_for_volume",
                   return_value=[{"id": 2}]) as api:
            result = comicvine_source.get_all_issues_for_volume(4050)

        assert result == [{"id": 2}]
        api.assert_called_once_with("KEY", 4050)

    def test_local_db_error_falls_through_to_the_api(self, monkeypatch):
        monkeypatch.setattr(comicvine_source, "_local_available", lambda: True)
        monkeypatch.setattr(comicvine_source, "_api_key", lambda app=None: "KEY")

        with patch("models.comicvine_sqlite.get_all_issues_for_volume",
                   side_effect=RuntimeError("db locked")), \
             patch("models.comicvine.get_all_issues_for_volume",
                   return_value=[{"id": 3}]):
            assert comicvine_source.get_all_issues_for_volume(4050) == [{"id": 3}]

    def test_uses_the_api_when_there_is_no_local_db(self, no_local, monkeypatch):
        monkeypatch.setattr(comicvine_source, "_api_key", lambda app=None: "KEY")

        with patch("models.comicvine.get_all_issues_for_volume",
                   return_value=[{"id": 4}]) as api:
            assert comicvine_source.get_all_issues_for_volume(4050) == [{"id": 4}]

        api.assert_called_once_with("KEY", 4050)

    def test_returns_empty_with_no_source(self, no_local, no_api):
        assert comicvine_source.get_all_issues_for_volume(4050) == []

    def test_api_failure_returns_empty_rather_than_raising(self, no_local, monkeypatch):
        """Callers fall back to sidecar data; an exception here would abort a
        whole sweep."""
        monkeypatch.setattr(comicvine_source, "_api_key", lambda app=None: "KEY")

        with patch("models.comicvine.get_all_issues_for_volume",
                   side_effect=RuntimeError("rate limited")):
            assert comicvine_source.get_all_issues_for_volume(4050) == []


class TestGetVolumeDetails:

    def test_prefers_the_local_db(self, monkeypatch):
        monkeypatch.setattr(comicvine_source, "_local_available", lambda: True)
        monkeypatch.setattr(comicvine_source, "_api_key", lambda app=None: "KEY")

        with patch("models.comicvine_sqlite.get_volume_details",
                   return_value={"name": "Batman"}), \
             patch("models.comicvine.get_volume_details") as api:
            assert comicvine_source.get_volume_details(4050)["name"] == "Batman"

        api.assert_not_called()

    def test_unnamed_local_row_falls_through_to_the_api(self, monkeypatch):
        """A row with no name isn't authoritative -- callers use `name` to decide
        whether to trust the metadata over a folder-derived guess."""
        monkeypatch.setattr(comicvine_source, "_local_available", lambda: True)
        monkeypatch.setattr(comicvine_source, "_api_key", lambda app=None: "KEY")

        with patch("models.comicvine_sqlite.get_volume_details",
                   return_value={"name": None}), \
             patch("models.comicvine.get_volume_details",
                   return_value={"name": "Batman"}) as api:
            assert comicvine_source.get_volume_details(4050)["name"] == "Batman"

        api.assert_called_once_with("KEY", 4050)

    def test_returns_empty_dict_with_no_source(self, no_local, no_api):
        assert comicvine_source.get_volume_details(4050) == {}


class TestAgainstTheRealLocalDatabase:
    """End-to-end through the real SQLite fixture, not mocks."""

    def test_reads_issues_from_the_configured_dump(
        self, comicvine_sqlite_configured, monkeypatch
    ):
        monkeypatch.setattr(comicvine_source, "_api_key", lambda app=None: None)

        assert comicvine_source.is_available() is True
        issues = comicvine_source.get_all_issues_for_volume(4050)

        assert len(issues) == 1
        assert issues[0]["number"] == "1"

    def test_reads_volume_details_from_the_configured_dump(
        self, comicvine_sqlite_configured, monkeypatch
    ):
        monkeypatch.setattr(comicvine_source, "_api_key", lambda app=None: None)

        details = comicvine_source.get_volume_details(4050)

        assert details["name"] == "Batman"
        assert details["publisher_name"] == "DC Comics"
