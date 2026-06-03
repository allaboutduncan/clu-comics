"""Tests for the GET /api/gcd-api/cover endpoint (lazy GCD cover lookup)."""
from unittest.mock import patch, MagicMock


class TestGcdApiCoverEndpoint:

    def test_missing_series_id_returns_400(self, client):
        resp = client.get('/api/gcd-api/cover')
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False

    def test_not_configured_returns_success_false(self, client):
        inst = MagicMock()
        inst._get_client.return_value = None  # no GCD API credentials
        with patch('models.providers.gcd_api_provider.GCDApiProvider', return_value=inst):
            resp = client.get('/api/gcd-api/cover?series_id=70876&issue=1')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is False
        inst.get_cover_url.assert_not_called()

    def test_returns_cover_url(self, client):
        inst = MagicMock()
        inst._get_client.return_value = object()
        inst.get_cover_url.return_value = 'https://files1.comics.org/img/x.jpg'
        with patch('models.providers.gcd_api_provider.GCDApiProvider', return_value=inst):
            resp = client.get('/api/gcd-api/cover?series_id=70876&issue=1')
        data = resp.get_json()
        assert data['success'] is True
        assert data['cover_url'] == 'https://files1.comics.org/img/x.jpg'
        inst.get_cover_url.assert_called_once_with('70876', '1')

    def test_no_cover_found_returns_success_false(self, client):
        inst = MagicMock()
        inst._get_client.return_value = object()
        inst.get_cover_url.return_value = None
        with patch('models.providers.gcd_api_provider.GCDApiProvider', return_value=inst):
            resp = client.get('/api/gcd-api/cover?series_id=70876&issue=5')
        data = resp.get_json()
        assert data['success'] is False
        assert data['cover_url'] is None

    def test_issue_defaults_to_1_when_blank(self, client):
        inst = MagicMock()
        inst._get_client.return_value = object()
        inst.get_cover_url.return_value = 'https://files1.comics.org/img/y.jpg'
        with patch('models.providers.gcd_api_provider.GCDApiProvider', return_value=inst):
            resp = client.get('/api/gcd-api/cover?series_id=70876')
        assert resp.get_json()['success'] is True
        inst.get_cover_url.assert_called_once_with('70876', '1')
