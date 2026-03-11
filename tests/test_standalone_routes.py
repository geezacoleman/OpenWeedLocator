"""Tests for standalone controller Flask API routes (config editor endpoints)."""

import configparser
import json
import os
from unittest.mock import patch, MagicMock

import pytest


def _stabilize_mqtt(dashboard):
    """Stop MQTT background thread and force connected=True.

    The standalone_test_client fixture creates a real DashMQTTSubscriber
    (importlib.reload overwrites the patch). Its background loop_start()
    thread tries to connect to localhost, fails, and sets connected=False
    at unpredictable times. For route tests that go through _send_command,
    we need a stable connected=True state without a racing thread.
    """
    mqtt = dashboard.mqtt_client
    if mqtt and hasattr(mqtt, 'client'):
        try:
            mqtt.client.loop_stop()
        except Exception:
            pass
    if mqtt:
        mqtt.connected = True


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
            'filename': 'GENERAL_CONFIG.ini'
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
        assert data['restart_required'] is False  # Config pushed live via MQTT

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
            'config': 'GENERAL_CONFIG.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert data['restart_required'] is False  # Config pushed live via MQTT

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
        assert 'GENERAL_CONFIG' in data['active_config']

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
            'config': 'GENERAL_CONFIG.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 400
        assert data['success'] is False
        assert 'Cannot delete' in data['error']

        # File should still exist
        assert os.path.exists(os.path.join(str(tmp_dir), 'GENERAL_CONFIG.ini'))

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

        # Should find the GENERAL_CONFIG preset
        names = [c['name'] for c in data['configs']]
        assert 'GENERAL_CONFIG.ini' in names

    def test_default_flag_set_correctly(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        # Create a non-default config
        custom = os.path.join(str(tmp_dir), 'CUSTOM.ini')
        with open(custom, 'w') as f:
            f.write('[System]\nalgorithm = exg\n')

        resp = client.get('/api/config/list')
        data = resp.get_json()

        configs_by_name = {c['name']: c for c in data['configs']}
        assert configs_by_name['GENERAL_CONFIG.ini']['is_default'] is True
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


# ===========================================================================
# Tracking route
# ===========================================================================

@pytest.mark.unit
class TestTrackingRoute:
    """Tests for POST /api/tracking/set."""

    def test_enable_tracking(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/tracking/set', json={'value': True})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True
        assert data['tracking_enabled'] is True

    def test_disable_tracking(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/tracking/set', json={'value': False})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True
        assert data['tracking_enabled'] is False

    def test_tracking_persists_to_config(self, standalone_test_client):
        """Tracking toggle should write to INI file for reboot persistence.

        _persist_config_change uses copy-on-write for protected defaults
        (GENERAL_CONFIG.ini), so we read the active config via active_config.txt.
        """
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        client.post('/api/tracking/set', json={'value': True})

        # Follow active_config.txt to find the written file
        active_path = dashboard._get_active_config_path()
        resolved = dashboard._resolve_config_path(active_path)

        import configparser
        config = configparser.ConfigParser()
        config.read(resolved)
        assert config.has_section('Tracking')
        assert config.get('Tracking', 'tracking_enabled') == 'True'

    def test_tracking_no_mqtt_returns_500(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = None
        resp = client.post('/api/tracking/set', json={'value': True})
        assert resp.status_code == 500


@pytest.mark.unit
class TestSystemStatsTracking:
    """Tests for tracking_enabled in GET /api/system_stats response."""

    def test_stats_include_tracking_enabled(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/system_stats')
        data = resp.get_json()
        assert 'tracking_enabled' in data

    def test_stats_tracking_reflects_mqtt_state(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        if dashboard.mqtt_client:
            dashboard.mqtt_client.current_state['tracking_enabled'] = True
        resp = client.get('/api/system_stats')
        data = resp.get_json()
        assert data['tracking_enabled'] is True


# ===========================================================================
# Operational routes — field-critical endpoints
# ===========================================================================

@pytest.mark.unit
class TestSensitivityRoute:
    """Tests for POST /api/sensitivity/set."""

    def test_set_sensitivity_valid(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/sensitivity/set', json={'level': 'high'})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True

    def test_set_sensitivity_missing_level(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.post('/api/sensitivity/set', json={})
        data = resp.get_json()
        assert resp.status_code == 400
        assert data['success'] is False

    def test_set_sensitivity_custom_level(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        # DashMQTTSubscriber.set_sensitivity_level now accepts any name
        # (validation happens on the OWL side via SensitivityManager)
        resp = client.post('/api/sensitivity/set', json={'level': 'custom_preset'})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True

    def test_set_sensitivity_locked_by_hardware(self, standalone_test_client):
        """Hardware controller lock should prevent dashboard changes."""
        client, dashboard, tmp_dir = standalone_test_client
        dashboard._get_controller_type = lambda: 'ute'
        resp = client.post('/api/sensitivity/set', json={'level': 'high'})
        assert resp.status_code == 423  # Locked


@pytest.mark.unit
class TestAlgorithmRoute:
    """Tests for POST /api/algorithm/set."""

    def test_set_algorithm_valid(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/algorithm/set', json={'algorithm': 'exhsv'})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True

    def test_set_algorithm_invalid(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.post('/api/algorithm/set', json={'algorithm': 'invalid'})
        data = resp.get_json()
        assert resp.status_code == 400
        assert data['success'] is False

    def test_set_algorithm_gog_hybrid(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/algorithm/set', json={'algorithm': 'gog-hybrid'})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True


@pytest.mark.unit
class TestDetectionRoutes:
    """Tests for /api/detection/start and /api/detection/stop."""

    def test_start_detection(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/detection/start')
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True

    def test_stop_detection(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/detection/stop')
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True

    def test_detection_locked_by_hardware(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard._get_controller_type = lambda: 'advanced'
        resp = client.post('/api/detection/start')
        assert resp.status_code == 423


@pytest.mark.unit
class TestRecordingRoutes:
    """Tests for /api/recording/start and /api/recording/stop."""

    def test_start_recording(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/recording/start')
        data = resp.get_json()
        assert resp.status_code == 200

    def test_stop_recording(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/recording/stop')
        data = resp.get_json()
        assert resp.status_code == 200


@pytest.mark.unit
class TestNozzleRoutes:
    """Tests for /api/nozzles/all-on and /api/nozzles/all-off."""

    def test_all_nozzles_on(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/nozzles/all-on')
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True

    def test_all_nozzles_off(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/nozzles/all-off')
        data = resp.get_json()
        assert resp.status_code == 200

    def test_nozzles_locked_by_hardware(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard._get_controller_type = lambda: 'ute'
        resp = client.post('/api/nozzles/all-on')
        assert resp.status_code == 423


@pytest.mark.unit
class TestConfigParamRoutes:
    """Tests for config slider routes."""

    def test_set_config_param(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/config/param', json={'param': 'exg_min', 'value': 42})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True

    def test_set_config_param_missing_fields(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.post('/api/config/param', json={'param': 'exg_min'})
        data = resp.get_json()
        assert resp.status_code == 400

    def test_set_crop_buffer(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/config/crop_buffer', json={'value': 25})
        data = resp.get_json()
        assert resp.status_code == 200

    def test_set_confidence(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/config/confidence', json={'value': 0.75})
        data = resp.get_json()
        assert resp.status_code == 200


@pytest.mark.unit
class TestAIRoutes:
    """Tests for AI model routes."""

    def test_set_ai_model(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/ai/set_model', json={'model': 'yolo26n-seg.pt'})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True

    def test_set_ai_model_missing(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.post('/api/ai/set_model', json={})
        data = resp.get_json()
        assert resp.status_code == 400

    def test_set_detect_classes(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/ai/set_detect_classes', json={'classes': ['weed', 'crop']})
        data = resp.get_json()
        assert resp.status_code == 200


@pytest.mark.unit
class TestSystemStats:
    """Tests for GET /api/system_stats."""

    def test_returns_stats(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/system_stats')
        data = resp.get_json()

        assert resp.status_code == 200
        assert 'cpu_percent' in data
        assert 'memory_percent' in data
        assert 'owl_running' in data
        assert 'detection_enable' in data

    def test_includes_mqtt_state(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        # System stats endpoint merges MQTT state into response
        resp = client.get('/api/system_stats')
        data = resp.get_json()
        # The response should include MQTT-sourced keys
        assert 'detection_enable' in data
        assert 'owl_running' in data

    def test_includes_resolution(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/system_stats')
        data = resp.get_json()
        assert 'resolution_width' in data
        assert 'resolution_height' in data

    def test_resolution_from_mqtt_state(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        # Patch get_state on the mqtt_client to return a controlled dict.
        # dashboard.mqtt_client may be a real DashMQTTSubscriber (importlib.reload
        # re-binds the name after the patch context exits), so we cannot use
        # .return_value directly on a bound method — use patch.object instead.
        mqtt_state = {
            'resolution_width': 1456,
            'resolution_height': 1088,
            'owl_running': True,
        }
        if dashboard.mqtt_client:
            with patch.object(dashboard.mqtt_client, 'get_state', return_value=mqtt_state):
                resp = client.get('/api/system_stats')
                data = resp.get_json()
        else:
            # No mqtt_client: resolution falls back to defaults (0)
            resp = client.get('/api/system_stats')
            data = resp.get_json()
        assert data['resolution_width'] == 1456
        assert data['resolution_height'] == 1088


@pytest.mark.unit
class TestOWLServiceRoutes:
    """Tests for /api/owl/start and /api/owl/stop."""

    def test_start_owl(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        with patch.object(dashboard, 'control_owl_service', return_value=(True, 'Started')):
            resp = client.post('/api/owl/start')
            data = resp.get_json()
            assert resp.status_code == 200
            assert data['success'] is True

    def test_stop_owl(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        with patch.object(dashboard, 'control_owl_service', return_value=(True, 'Stopped')):
            resp = client.post('/api/owl/stop')
            data = resp.get_json()
            assert resp.status_code == 200

    def test_start_owl_failure(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        with patch.object(dashboard, 'control_owl_service', return_value=(False, 'Failed')):
            resp = client.post('/api/owl/start')
            assert resp.status_code == 500


@pytest.mark.unit
class TestConfigListAndLoad:
    """Tests for config library list and individual load."""

    def test_list_configs_includes_presets(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/config/list')
        data = resp.get_json()

        assert resp.status_code == 200
        names = [c['name'] for c in data['configs']]
        assert 'GENERAL_CONFIG.ini' in names

    def test_get_config_returns_sections(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/config')
        data = resp.get_json()

        assert resp.status_code == 200
        assert 'GreenOnBrown' in data['config']
        assert 'System' in data['config']


# ===========================================================================
# GPS, error polling, frame proxy, video feed, directories
# ===========================================================================

@pytest.mark.unit
class TestUpdateGPS:
    """Tests for POST /api/update_gps."""

    def test_sends_gps_data(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/update_gps', json={
            'latitude': -33.8688,
            'longitude': 151.2093,
            'accuracy': 2.5
        })
        data = resp.get_json()
        assert data['success'] is True

    def test_no_mqtt_returns_500(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = None
        resp = client.post('/api/update_gps', json={
            'latitude': 0, 'longitude': 0, 'accuracy': 0
        })
        assert resp.status_code == 500

    def test_missing_data_uses_defaults(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/update_gps', json={})
        data = resp.get_json()
        # Should not crash — defaults to 0.0 for missing fields
        assert data['success'] is True


@pytest.mark.unit
class TestGetErrors:
    """Tests for GET /api/get_errors."""

    def test_returns_empty_when_no_errors(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/get_errors')
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) == 0

    def test_returns_empty_when_no_mqtt(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = None
        resp = client.get('/api/get_errors')
        data = resp.get_json()
        assert data == []


@pytest.mark.unit
class TestDownloadFrame:
    """Tests for POST /api/download_frame."""

    def test_returns_503_when_owl_not_running(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        # No OWL running = urllib can't connect to port 8001
        with patch('urllib.request.urlopen', side_effect=Exception("Connection refused")):
            resp = client.post('/api/download_frame')
            assert resp.status_code == 500

    def test_returns_jpeg_on_success(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        mock_response = MagicMock()
        mock_response.read.return_value = b'\xff\xd8\xff\xe0fake_jpeg'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=mock_response):
            resp = client.post('/api/download_frame')
            assert resp.status_code == 200
            assert resp.content_type == 'image/jpeg'
            assert b'\xff\xd8' in resp.data


@pytest.mark.unit
class TestVideoFeed:
    """Tests for GET /video_feed."""

    def test_returns_multipart_content_type(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        import urllib.error
        # Even if stream fails, the response should have correct MIME type
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError("no stream")):
            resp = client.get('/video_feed')
            assert 'multipart/x-mixed-replace' in resp.content_type

    def test_streams_data_from_owl(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        mock_response = MagicMock()
        # Simulate two chunks then EOF
        mock_response.read.side_effect = [b'chunk1', b'chunk2', b'']
        with patch('urllib.request.urlopen', return_value=mock_response):
            resp = client.get('/video_feed')
            assert resp.status_code == 200


@pytest.mark.unit
class TestConfigDirectories:
    """Tests for GET /api/config/directories."""

    def test_returns_directory_list(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/config/directories')
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True
        assert 'directories' in data
        assert isinstance(data['directories'], list)
