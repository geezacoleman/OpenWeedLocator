"""Tests for standalone controller Flask API routes (config editor endpoints)."""

import configparser
import json
import os

import pytest


@pytest.mark.unit
class TestGetConfig:
    """Tests for GET /api/config."""

    def test_returns_config_on_success(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/config')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert 'config' in data
        assert 'GreenOnBrown' in data['config']
        assert 'config_name' in data
        assert 'available_configs' in data

    def test_config_contains_all_sections(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/config')
        data = resp.get_json()

        config = data['config']
        for section in ['System', 'GreenOnBrown', 'Camera', 'Controller', 'DataCollection', 'Relays']:
            assert section in config, f"Missing section: {section}"

    def test_returns_404_when_config_missing(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        # Point to a nonexistent file
        dashboard._get_active_config_path = lambda: 'config/NONEXISTENT.ini'
        dashboard._resolve_config_path = lambda p: os.path.join(
            str(tmp_dir), 'NONEXISTENT.ini')

        resp = client.get('/api/config')
        data = resp.get_json()

        assert resp.status_code == 404
        assert data['success'] is False


@pytest.mark.unit
class TestSaveConfig:
    """Tests for POST /api/config."""

    def test_saves_new_config_file(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        new_config = {
            'GreenOnBrown': {'exg_min': '30', 'exg_max': '180'},
            'System': {'algorithm': 'exg'}
        }

        resp = client.post('/api/config', json={
            'config': new_config,
            'filename': 'my_custom.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert data['filename'] == 'my_custom.ini'

        # Verify file was actually written
        saved_path = os.path.join(str(tmp_dir), 'my_custom.ini')
        assert os.path.exists(saved_path)

        saved = configparser.ConfigParser()
        saved.read(saved_path)
        assert saved.get('GreenOnBrown', 'exg_min') == '30'

    def test_blocks_overwrite_of_default_presets(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config', json={
            'config': {'System': {'algorithm': 'exg'}},
            'filename': 'DAY_SENSITIVITY_1.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 400
        assert data['success'] is False
        assert 'Cannot overwrite' in data['error']

    def test_returns_400_on_missing_config_data(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config', json={'filename': 'test.ini'})
        data = resp.get_json()

        assert resp.status_code == 400
        assert data['success'] is False

    def test_save_with_set_active(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config', json={
            'config': {'System': {'algorithm': 'exg'}},
            'filename': 'activated.ini',
            'set_active': True
        })
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert data['is_active'] is True
        assert data['restart_required'] is True

        # Verify active_config.txt was updated
        pointer = os.path.join(str(tmp_dir), 'active_config.txt')
        with open(pointer, 'r') as f:
            assert 'activated.ini' in f.read()


@pytest.mark.unit
class TestSetActiveConfig:
    """Tests for POST /api/config/set-active."""

    def test_sets_active_config(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config/set-active', json={
            'config': 'DAY_SENSITIVITY_1.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert data['restart_required'] is True

    def test_returns_400_on_missing_config_name(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config/set-active', json={})
        data = resp.get_json()

        assert resp.status_code == 400
        assert data['success'] is False

    def test_returns_404_for_nonexistent_config(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config/set-active', json={
            'config': 'NONEXISTENT.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 404
        assert data['success'] is False


@pytest.mark.unit
class TestResetDefault:
    """Tests for POST /api/config/reset-default."""

    def test_resets_to_default(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        # First set a non-default active config
        pointer = os.path.join(str(tmp_dir), 'active_config.txt')
        with open(pointer, 'w') as f:
            f.write('config/my_custom.ini')

        resp = client.post('/api/config/reset-default')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert 'DAY_SENSITIVITY_2' in data['active_config']

        # Pointer file should be removed
        assert not os.path.exists(pointer)


@pytest.mark.unit
class TestDeleteConfig:
    """Tests for POST /api/config/delete."""

    def test_deletes_custom_config(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        # Create a custom config to delete
        custom = os.path.join(str(tmp_dir), 'my_custom.ini')
        with open(custom, 'w') as f:
            f.write('[System]\nalgorithm = exg\n')

        resp = client.post('/api/config/delete', json={
            'config': 'my_custom.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert not os.path.exists(custom)

    def test_blocks_deletion_of_default_presets(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config/delete', json={
            'config': 'DAY_SENSITIVITY_1.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 400
        assert data['success'] is False
        assert 'Cannot delete' in data['error']

        # File should still exist
        assert os.path.exists(os.path.join(str(tmp_dir), 'DAY_SENSITIVITY_1.ini'))

    def test_returns_404_for_nonexistent_config(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config/delete', json={
            'config': 'NONEXISTENT.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 404
        assert data['success'] is False

    def test_returns_400_on_missing_name(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config/delete', json={})
        data = resp.get_json()

        assert resp.status_code == 400
        assert data['success'] is False


@pytest.mark.unit
class TestListConfigs:
    """Tests for GET /api/config/list."""

    def test_returns_config_list(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.get('/api/config/list')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert 'configs' in data
        assert 'active_config' in data

        # Should find at least the 3 preset files
        names = [c['name'] for c in data['configs']]
        assert 'DAY_SENSITIVITY_1.ini' in names
        assert 'DAY_SENSITIVITY_2.ini' in names
        assert 'DAY_SENSITIVITY_3.ini' in names

    def test_default_flag_set_correctly(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        # Create a non-default config
        custom = os.path.join(str(tmp_dir), 'CUSTOM.ini')
        with open(custom, 'w') as f:
            f.write('[System]\nalgorithm = exg\n')

        resp = client.get('/api/config/list')
        data = resp.get_json()

        configs_by_name = {c['name']: c for c in data['configs']}
        assert configs_by_name['DAY_SENSITIVITY_1.ini']['is_default'] is True
        assert configs_by_name['CUSTOM.ini']['is_default'] is False


@pytest.mark.unit
class TestControllerConfig:
    """Tests for GET /api/controller_config."""

    def test_returns_controller_type(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.get('/api/controller_config')
        data = resp.get_json()

        assert resp.status_code == 200
        assert 'controller_type' in data
        assert 'hardware_active' in data
        assert data['controller_type'] == 'none'
        assert data['hardware_active'] is False
