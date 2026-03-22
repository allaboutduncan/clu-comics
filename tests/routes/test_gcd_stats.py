"""Tests for the GET /api/providers/gcd/stats endpoint."""
import pytest
from unittest.mock import patch


class TestGcdStatsEndpoint:
    """Test the GCD database stats API endpoint."""

    def test_returns_stats_when_connected(self, client):
        """Returns 200 with stats when GCD database is reachable."""
        fake_stats = {
            'series': 250000,
            'issues': 2500000,
            'stories': 3000000,
            'publishers': 50000,
            'creators': 400000,
            'table_count': 5,
        }
        with patch('models.gcd.get_database_stats', return_value=fake_stats):
            resp = client.get('/api/providers/gcd/stats')

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['stats'] == fake_stats

    def test_returns_expected_keys(self, client):
        """Stats dict contains all expected keys."""
        fake_stats = {
            'series': 1,
            'issues': 2,
            'stories': 3,
            'publishers': 4,
            'creators': 5,
            'table_count': 5,
        }
        with patch('models.gcd.get_database_stats', return_value=fake_stats):
            resp = client.get('/api/providers/gcd/stats')

        stats = resp.get_json()['stats']
        for key in ('series', 'issues', 'stories', 'publishers', 'creators', 'table_count'):
            assert key in stats

    def test_returns_503_when_not_connected(self, client):
        """Returns 503 when the GCD database is unreachable."""
        with patch('models.gcd.get_database_stats', return_value=None):
            resp = client.get('/api/providers/gcd/stats')

        assert resp.status_code == 503
        data = resp.get_json()
        assert data['success'] is False

    def test_returns_500_on_unexpected_error(self, client):
        """Returns 500 when an unexpected exception occurs."""
        with patch('models.gcd.get_database_stats', side_effect=RuntimeError("boom")):
            resp = client.get('/api/providers/gcd/stats')

        assert resp.status_code == 500
        data = resp.get_json()
        assert 'error' in data
