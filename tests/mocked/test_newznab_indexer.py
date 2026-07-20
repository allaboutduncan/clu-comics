"""Tests for models/indexers/newznab_indexer.py -- mocked HTTP + XML."""
import pytest
from unittest.mock import MagicMock, patch

from models.indexers import IndexerConfig, IndexerType, get_indexer_impl

VALID_SEARCH_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
  <channel>
    <item>
      <title>Batman 001 (2020)</title>
      <enclosure url="https://indexer.example/getnzb/abc.nzb" type="application/x-nzb"/>
    </item>
  </channel>
</rss>
"""

ERROR_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<error code="100" description="Incorrect user credentials"/>
"""

MALFORMED_XML = b"<rss><channel><item></rss"


def _indexer(**cfg):
    defaults = {"name": "NZBgeek", "url": "https://api.nzbgeek.info", "api_key": "KEY"}
    defaults.update(cfg)
    return get_indexer_impl(IndexerType.NEWZNAB, IndexerConfig(**defaults))


class TestNewznabTestConnection:

    @patch("requests.get")
    def test_valid(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=VALID_SEARCH_XML)
        assert _indexer().test_connection() is True

    @patch("requests.get")
    def test_error_response(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=ERROR_XML)
        assert _indexer().test_connection() is False

    @patch("requests.get")
    def test_non_200(self, mock_get):
        mock_get.return_value = MagicMock(status_code=500, content=b"")
        assert _indexer().test_connection() is False

    @patch("requests.get")
    def test_malformed_xml(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=MALFORMED_XML)
        assert _indexer().test_connection() is False

    @patch("requests.get", side_effect=Exception("dns failure"))
    def test_exception_swallowed(self, mock_get):
        assert _indexer().test_connection() is False

    def test_missing_url(self):
        assert _indexer(url="").test_connection() is False


class TestNewznabSearchSeam:

    def test_search_not_implemented(self):
        # Locks the PR 2 seam so it can't silently ship half-wired.
        with pytest.raises(NotImplementedError):
            _indexer().search("batman")
