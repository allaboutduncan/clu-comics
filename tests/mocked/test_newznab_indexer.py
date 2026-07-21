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

CAPS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<caps><server version="1.0"/><categories/></caps>
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
        mock_get.return_value = MagicMock(status_code=200, content=CAPS_XML)
        assert _indexer().test_connection() is True
        # Connection test uses t=caps (no query) to avoid "Missing parameter".
        assert mock_get.call_args.kwargs["params"]["t"] == "caps"

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


class TestNewznabSearch:

    @patch("requests.get")
    def test_parses_items(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=VALID_SEARCH_XML)
        results = _indexer().search("batman", indexer_id=3)
        assert len(results) == 1
        r = results[0]
        assert r.title == "Batman 001 (2020)"
        assert r.nzb_url == "https://indexer.example/getnzb/abc.nzb"
        assert r.indexer_id == 3

    @patch("requests.get")
    def test_error_returns_empty(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=ERROR_XML)
        results = _indexer().search("batman")
        assert results == []
        assert _indexer().config is not None

    @patch("requests.get", side_effect=Exception("boom"))
    def test_exception_returns_empty(self, mock_get):
        assert _indexer().search("batman") == []

    @patch("requests.get")
    def test_categories_passed(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=VALID_SEARCH_XML)
        _indexer(categories="7000,7030").search("batman")
        assert mock_get.call_args.kwargs["params"]["cat"] == "7000,7030"

    @patch("requests.get")
    def test_defaults_to_comics_category(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=VALID_SEARCH_XML)
        _indexer(categories=None).search("batman")
        assert mock_get.call_args.kwargs["params"]["cat"] == "7030"

    @patch("requests.get")
    def test_link_fallback_when_no_enclosure(self, mock_get):
        # Some Newznab variants omit <enclosure> and put the NZB URL in <link>.
        xml = b"""<?xml version="1.0"?>
        <rss><channel><item>
          <title>Treehouse of Horror 004 (2024)</title>
          <link>https://althub.co.za/getnzb/xyz.nzb</link>
        </item></channel></rss>"""
        mock_get.return_value = MagicMock(status_code=200, content=xml)
        results = _indexer().search("treehouse of horror 04")
        assert len(results) == 1
        assert results[0].nzb_url == "https://althub.co.za/getnzb/xyz.nzb"
