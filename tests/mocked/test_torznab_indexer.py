"""Tests for models/indexers/torznab_indexer.py -- mocked HTTP + XML."""
import pytest
from unittest.mock import MagicMock, patch

from models.indexers import IndexerConfig, IndexerType, get_indexer_impl

VALID_SEARCH_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <item>
      <title>Batman 001 (2020)</title>
      <enclosure url="magnet:?xt=urn:btih:abc123" type="application/x-bittorrent"/>
      <torznab:attr name="seeders" value="12"/>
      <torznab:attr name="peers" value="3"/>
      <torznab:attr name="size" value="52428800"/>
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
    defaults = {"name": "MyTracker", "url": "https://api.mytracker.example", "api_key": "KEY"}
    defaults.update(cfg)
    return get_indexer_impl(IndexerType.TORZNAB, IndexerConfig(**defaults))


class TestTorznabTestConnection:

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


class TestTorznabSearch:

    @patch("requests.get")
    def test_parses_items_with_seeders(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=VALID_SEARCH_XML)
        results = _indexer().search("batman", indexer_id=3)
        assert len(results) == 1
        r = results[0]
        assert r.title == "Batman 001 (2020)"
        assert r.download_url == "magnet:?xt=urn:btih:abc123"
        assert r.indexer_id == 3
        assert r.seeders == 12
        assert r.peers == 3
        assert r.size == 52428800

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
        _indexer(categories="5000,5070").search("batman")
        assert mock_get.call_args.kwargs["params"]["cat"] == "5000,5070"

    @patch("requests.get")
    def test_no_default_category(self, mock_get):
        # Unlike Newznab's comics category (7030), Torznab has no universal
        # default — an unconfigured category omits the filter entirely.
        mock_get.return_value = MagicMock(status_code=200, content=VALID_SEARCH_XML)
        _indexer(categories=None).search("batman")
        assert "cat" not in mock_get.call_args.kwargs["params"]

    @patch("requests.get")
    def test_link_fallback_when_no_enclosure(self, mock_get):
        # Some Torznab variants omit <enclosure> and put the magnet/torrent
        # URL in <link> instead.
        xml = b"""<?xml version="1.0"?>
        <rss><channel><item>
          <title>Treehouse of Horror 004 (2024)</title>
          <link>magnet:?xt=urn:btih:def456</link>
        </item></channel></rss>"""
        mock_get.return_value = MagicMock(status_code=200, content=xml)
        results = _indexer().search("treehouse of horror 04")
        assert len(results) == 1
        assert results[0].download_url == "magnet:?xt=urn:btih:def456"

    @patch("requests.get")
    def test_magneturl_attr_used_when_no_enclosure_url(self, mock_get):
        xml = b"""<?xml version="1.0"?>
        <rss xmlns:torznab="http://torznab.com/schemas/2015/feed">
        <channel><item>
          <title>Batman 002 (2020)</title>
          <torznab:attr name="magneturl" value="magnet:?xt=urn:btih:ghi789"/>
        </item></channel></rss>"""
        mock_get.return_value = MagicMock(status_code=200, content=xml)
        results = _indexer().search("batman")
        assert len(results) == 1
        assert results[0].download_url == "magnet:?xt=urn:btih:ghi789"
