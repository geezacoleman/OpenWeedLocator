"""Tests for networked controller Flask API routes (config editor endpoints)."""

import json
from unittest.mock import MagicMock

import pytest


@pytest.mark.unit
class TestGetDeviceConfig:
    """Tests for GET /api/config/<device_id>."""

    def test_returns_config_on_success(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        resp = client.get('/api/config/test-owl')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert 'config' in data
        assert 'GreenOnBrown' in data['config']
        mock_ctrl.request_device_config.assert_called_once_with('test-owl', timeout=3.0)

    def test_returns_504_on_timeout(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        mock_ctrl.request_device_config.return_value = None

        resp = client.get('/api/config/test-owl')
        data = resp.get_json()

        assert resp.status_code == 504
        assert data['success'] is False
        assert 'Timeout' in data['error']


@pytest.mark.unit
class TestPushDeviceConfig:
    """Tests for POST /api/config/<device_id>."""

    def test_sends_section_to_device(self, networked_test_client):
        client, mock_ctrl = networked_test_client

        resp = client.post('/api/config/test-owl',
                           json={'section': 'GreenOnBrown', 'params': {'exg_min': '30'}})
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        mock_ctrl.send_command.assert_called_once_with(
            'test-owl', 'set_config_section',
            {'section': 'GreenOnBrown', 'params': {'exg_min': '30'}}
        )

    def test_returns_400_on_missing_data(self, networked_test_client):
        client, _ = networked_test_client

        resp = client.post('/api/config/test-owl',
                           json={'section': 'GreenOnBrown'})  # no params
        assert resp.status_code == 400

    def test_returns_error_on_empty_body(self, networked_test_client):
        client, _ = networked_test_client

        resp = client.post('/api/config/test-owl',
                           data='', content_type='application/json')
        data = resp.get_json()

        # Empty body triggers a parse error caught by the route's exception handler
        assert resp.status_code in (400, 500)
        assert data['success'] is False


@pytest.mark.unit
class TestSaveDeviceConfig:
    """Tests for POST /api/config/<device_id>/save."""

    def test_sends_save_command(self, networked_test_client):
        client, mock_ctrl = networked_test_client

        resp = client.post('/api/config/test-owl/save', json={})
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        mock_ctrl.send_command.assert_called_once_with(
            'test-owl', 'save_config', {'filename': None}
        )


@pytest.mark.unit
class TestListPresets:
    """Tests for GET /api/presets."""

    def test_returns_preset_list(self, networked_test_client):
        client, _ = networked_test_client

        resp = client.get('/api/presets')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert len(data['presets']) == 2

        # Check is_default flags
        names = {p['name']: p['is_default'] for p in data['presets']}
        assert names['DAY_SENSITIVITY_1'] is True
        assert names['CUSTOM'] is False


@pytest.mark.unit
class TestGetPreset:
    """Tests for GET /api/presets/<name>."""

    def test_returns_preset_config(self, networked_test_client):
        client, _ = networked_test_client

        resp = client.get('/api/presets/DAY_SENSITIVITY_1')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert 'config' in data
        assert 'GreenOnBrown' in data['config']

    def test_returns_404_for_missing_preset(self, networked_test_client):
        client, _ = networked_test_client

        resp = client.get('/api/presets/NONEXISTENT')
        data = resp.get_json()

        assert resp.status_code == 404
        assert data['success'] is False


@pytest.mark.unit
class TestPushPreset:
    """Tests for POST /api/presets/push/<device_id>."""

    def test_pushes_all_sections_from_preset(self, networked_test_client):
        client, mock_ctrl = networked_test_client

        resp = client.post('/api/presets/push/test-owl',
                           json={'preset': 'DAY_SENSITIVITY_1'})
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert 'sections' in data['message']

        # send_command should have been called once per section
        assert mock_ctrl.send_command.call_count >= 1

    def test_returns_404_for_missing_preset(self, networked_test_client):
        client, _ = networked_test_client

        resp = client.post('/api/presets/push/test-owl',
                           json={'preset': 'NONEXISTENT'})
        data = resp.get_json()

        assert resp.status_code == 404
        assert data['success'] is False

    def test_returns_400_on_missing_preset_name(self, networked_test_client):
        client, _ = networked_test_client

        resp = client.post('/api/presets/push/test-owl', json={})
        data = resp.get_json()

        assert resp.status_code == 400
        assert data['success'] is False
