"""Tests for networked controller Flask API routes (config editor endpoints)."""

import json
import time
from unittest.mock import MagicMock, patch

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
        assert names['GENERAL_CONFIG'] is True
        assert names['CUSTOM'] is False


@pytest.mark.unit
class TestGetPreset:
    """Tests for GET /api/presets/<name>."""

    def test_returns_preset_config(self, networked_test_client):
        client, _ = networked_test_client

        resp = client.get('/api/presets/GENERAL_CONFIG')
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
                           json={'preset': 'GENERAL_CONFIG'})
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


# ===========================================================================
# Multi-OWL operational routes — field-critical for 2+ OWL deployments
# ===========================================================================

@pytest.mark.unit
class TestCommandRoute:
    """Tests for POST /api/command — send commands to individual or all OWLs."""

    def test_broadcast_to_all(self, networked_test_client):
        client, ctrl = networked_test_client
        ctrl.send_command.return_value = {
            'success': True, 'targets': ['owl-1', 'owl-2']
        }
        resp = client.post('/api/command', json={
            'device_id': 'all',
            'action': 'toggle_detection',
            'value': True
        })
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True
        ctrl.send_command.assert_called_once_with('all', 'toggle_detection', True)

    def test_command_to_specific_owl(self, networked_test_client):
        client, ctrl = networked_test_client
        resp = client.post('/api/command', json={
            'device_id': 'owl-1',
            'action': 'set_algorithm',
            'value': 'exhsv'
        })
        data = resp.get_json()
        assert resp.status_code == 200
        ctrl.send_command.assert_called_once_with('owl-1', 'set_algorithm', 'exhsv')

    def test_missing_device_id_returns_400(self, networked_test_client):
        client, ctrl = networked_test_client
        resp = client.post('/api/command', json={
            'action': 'toggle_detection'
        })
        assert resp.status_code == 400

    def test_missing_action_returns_400(self, networked_test_client):
        client, ctrl = networked_test_client
        resp = client.post('/api/command', json={
            'device_id': 'owl-1'
        })
        assert resp.status_code == 400

    def test_command_failure_propagates(self, networked_test_client):
        client, ctrl = networked_test_client
        ctrl.send_command.return_value = {
            'success': False, 'error': 'MQTT not connected'
        }
        resp = client.post('/api/command', json={
            'device_id': 'owl-1',
            'action': 'toggle_detection',
            'value': True
        })
        data = resp.get_json()
        assert data['success'] is False


@pytest.mark.unit
class TestRestartRoute:
    """Tests for POST /api/owl/<device_id>/restart."""

    def test_restart_specific_owl(self, networked_test_client):
        client, ctrl = networked_test_client
        resp = client.post('/api/owl/owl-1/restart')
        data = resp.get_json()
        assert resp.status_code == 200
        ctrl.send_command.assert_called_once_with('owl-1', 'restart_service')

    def test_restart_second_owl(self, networked_test_client):
        client, ctrl = networked_test_client
        resp = client.post('/api/owl/owl-2/restart')
        data = resp.get_json()
        assert resp.status_code == 200
        ctrl.send_command.assert_called_once_with('owl-2', 'restart_service')


@pytest.mark.unit
class TestSetActiveConfigRoute:
    """Tests for POST /api/config/<device_id>/set-active."""

    def test_set_active_config(self, networked_test_client):
        client, ctrl = networked_test_client
        resp = client.post('/api/config/owl-1/set-active', json={
            'config': 'config/GENERAL_CONFIG.ini'
        })
        data = resp.get_json()
        assert resp.status_code == 200
        ctrl.send_command.assert_called_once_with(
            'owl-1', 'set_active_config', 'config/GENERAL_CONFIG.ini'
        )

    def test_set_active_missing_path_returns_400(self, networked_test_client):
        client, ctrl = networked_test_client
        resp = client.post('/api/config/owl-1/set-active', json={})
        assert resp.status_code == 400


@pytest.mark.unit
class TestVideoProxyRoutes:
    """Tests for snapshot and video feed proxy routes."""

    def test_snapshot_converts_underscores_to_hyphens(self, networked_test_client):
        """Device IDs with underscores should be converted to hyphens."""
        client, ctrl = networked_test_client
        with patch('controller.networked.networked.requests') as mock_requests:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = b'\xff\xd8\xff\xe0'  # JPEG header bytes
            mock_requests.get.return_value = mock_response

            resp = client.get('/api/snapshot/owl_1')
            call_url = mock_requests.get.call_args[0][0]
            assert 'owl-1' in call_url
            assert 'owl_1' not in call_url

    def test_snapshot_offline_owl_returns_502(self, networked_test_client):
        client, ctrl = networked_test_client
        import requests as real_requests
        with patch('controller.networked.networked.requests') as mock_requests:
            mock_requests.exceptions = real_requests.exceptions
            mock_requests.get.side_effect = real_requests.exceptions.ConnectionError("offline")

            resp = client.get('/api/snapshot/owl-1')
            assert resp.status_code == 502

    def test_snapshot_non_200_returns_502(self, networked_test_client):
        client, ctrl = networked_test_client
        with patch('controller.networked.networked.requests') as mock_requests:
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_requests.get.return_value = mock_response

            resp = client.get('/api/snapshot/owl-1')
            assert resp.status_code == 502

    def test_snapshot_uses_static_ip_when_available(self, networked_test_client):
        """Proxy should use OWL's static IP instead of .local when known from MQTT state."""
        client, ctrl = networked_test_client
        ctrl.owls_state['owl-1'] = {'static_ip': '192.168.1.11'}

        with patch('controller.networked.networked.requests') as mock_requests:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = b'\xff\xd8\xff\xe0'
            mock_requests.get.return_value = mock_response

            resp = client.get('/api/snapshot/owl-1')
            call_url = mock_requests.get.call_args[0][0]
            assert '192.168.1.11' in call_url
            assert '.local' not in call_url

    def test_snapshot_falls_back_to_mdns_without_ip(self, networked_test_client):
        """Proxy should fall back to .local hostname when static_ip is not in MQTT state."""
        client, ctrl = networked_test_client
        ctrl.owls_state['owl-2'] = {'device_id': 'owl-2'}  # No static_ip

        with patch('controller.networked.networked.requests') as mock_requests:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = b'\xff\xd8\xff\xe0'
            mock_requests.get.return_value = mock_response

            resp = client.get('/api/snapshot/owl-2')
            call_url = mock_requests.get.call_args[0][0]
            assert 'owl-2.local' in call_url

    def test_video_proxy_uses_static_ip(self, networked_test_client):
        """Video feed proxy should use IP address for reliable multi-OWL streaming."""
        client, ctrl = networked_test_client
        ctrl.owls_state['owl-1'] = {'static_ip': '192.168.1.11'}

        with patch('controller.networked.networked.requests') as mock_requests:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {'Content-Type': 'multipart/x-mixed-replace; boundary=FRAME'}
            mock_response.iter_content.return_value = iter([b'data'])
            mock_requests.get.return_value = mock_response

            resp = client.get('/api/video_feed/owl-1')
            call_url = mock_requests.get.call_args[0][0]
            assert '192.168.1.11' in call_url
            assert '.local' not in call_url


@pytest.mark.unit
class TestModelDeployRoute:
    """Tests for POST /api/models/deploy — deploy model to multiple OWLs."""

    def test_deploy_missing_model_name_returns_400(self, networked_test_client):
        client, ctrl = networked_test_client
        resp = client.post('/api/models/deploy', json={
            'device_ids': ['owl-1']
        })
        assert resp.status_code == 400

    def test_deploy_missing_device_ids_returns_400(self, networked_test_client):
        client, ctrl = networked_test_client
        resp = client.post('/api/models/deploy', json={
            'model_name': 'test.pt'
        })
        assert resp.status_code == 400

    def test_deploy_empty_data_returns_400(self, networked_test_client):
        client, ctrl = networked_test_client
        resp = client.post('/api/models/deploy', json={})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# System shutdown / fix-screen / reboot routes
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSystemShutdown:
    """Tests for POST /api/system/shutdown."""

    def test_sends_shutdown_to_all_owls(self, networked_test_client):
        client, ctrl = networked_test_client
        ctrl.send_command.return_value = {'success': True, 'targets': ['owl-1', 'owl-2']}

        with patch('controller.networked.networked.threading') as mock_threading:
            with patch('controller.networked.networked.subprocess'):
                resp = client.post('/api/system/shutdown')
                data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert 'owl-1' in data['owls_notified']
        ctrl.send_command.assert_called_once_with('all', 'shutdown')

    def test_starts_background_shutdown_thread(self, networked_test_client):
        client, ctrl = networked_test_client
        ctrl.send_command.return_value = {'success': True, 'targets': []}

        with patch('controller.networked.networked.threading') as mock_threading:
            with patch('controller.networked.networked.subprocess'):
                resp = client.post('/api/system/shutdown')

        assert resp.status_code == 200
        mock_threading.Thread.assert_called_once()
        mock_threading.Thread.return_value.start.assert_called_once()

    def test_returns_success_even_if_no_owls(self, networked_test_client):
        client, ctrl = networked_test_client
        ctrl.send_command.return_value = {'success': True, 'targets': []}

        with patch('controller.networked.networked.threading'):
            with patch('controller.networked.networked.subprocess'):
                resp = client.post('/api/system/shutdown')
                data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert data['owls_notified'] == []


@pytest.mark.unit
class TestFixScreen:
    """Tests for POST /api/system/fix-screen."""

    def test_success_returns_needs_reboot(self, networked_test_client):
        client, _ = networked_test_client

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = 'ok'
        mock_result.stderr = ''

        with patch('controller.networked.networked.subprocess.run', return_value=mock_result):
            resp = client.post('/api/system/fix-screen')
            data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert data['needs_reboot'] is True

    def test_apt_failure_returns_500(self, networked_test_client):
        client, _ = networked_test_client

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = 'package not found'

        with patch('controller.networked.networked.subprocess.run', return_value=mock_result):
            resp = client.post('/api/system/fix-screen')
            data = resp.get_json()

        assert resp.status_code == 500
        assert data['success'] is False

    def test_timeout_returns_504(self, networked_test_client):
        client, _ = networked_test_client

        import subprocess as sp
        with patch('controller.networked.networked.subprocess.run', side_effect=sp.TimeoutExpired('apt', 120)):
            resp = client.post('/api/system/fix-screen')
            data = resp.get_json()

        assert resp.status_code == 504
        assert data['success'] is False
        assert 'timed out' in data['error']


@pytest.mark.unit
class TestSystemReboot:
    """Tests for POST /api/system/reboot."""

    def test_returns_success_and_starts_thread(self, networked_test_client):
        client, _ = networked_test_client

        with patch('controller.networked.networked.threading') as mock_threading:
            with patch('controller.networked.networked.subprocess'):
                resp = client.post('/api/system/reboot')
                data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        mock_threading.Thread.assert_called_once()
        mock_threading.Thread.return_value.start.assert_called_once()


@pytest.mark.unit
class TestGPSBreadcrumbs:
    """Tests for GET /api/gps/breadcrumbs (live track polyline data)."""

    def test_returns_empty_when_gps_disabled(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        mock_ctrl.gps_manager = None

        resp = client.get('/api/gps/breadcrumbs')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['coordinates'] == []
        assert data['recording'] is False

    def test_returns_coordinates_when_recording(self, networked_test_client):
        client, mock_ctrl = networked_test_client

        fake_coords = [[151.2093, -33.8688], [151.2110, -33.8700]]
        recorder = MagicMock()
        # `coordinates` is a property on TrackRecorder — expose as attribute on mock
        recorder.coordinates = list(fake_coords)
        recorder.recording = True
        mock_ctrl.gps_manager = MagicMock()
        mock_ctrl.gps_manager.recorder = recorder

        resp = client.get('/api/gps/breadcrumbs')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['coordinates'] == fake_coords
        assert data['recording'] is True

    def test_returns_empty_list_when_not_recording(self, networked_test_client):
        client, mock_ctrl = networked_test_client

        recorder = MagicMock()
        recorder.coordinates = []
        recorder.recording = False
        mock_ctrl.gps_manager = MagicMock()
        mock_ctrl.gps_manager.recorder = recorder

        resp = client.get('/api/gps/breadcrumbs')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['coordinates'] == []
        assert data['recording'] is False


@pytest.mark.unit
class TestGPSStateSchema:
    """Tests that /api/gps emits the new connection schema (gps_connected + source)."""

    def test_schema_includes_source_and_gps_connected(self, networked_test_client):
        client, mock_ctrl = networked_test_client

        fake_state = {
            'fix': {'latitude': -33.8688, 'longitude': 151.2093, 'fix_valid': True,
                    'speed_kmh': 5.0, 'heading': 180.0, 'satellites': 8,
                    'hdop': 1.1, 'altitude': 45.0, 'age_seconds': 0.5},
            'connection': {'gps_connected': True, 'gps_enabled': True, 'source': 'serial'},
            'session': {'active': True, 'distance_km': 0.1,
                        'time_active_s': 60, 'area_hectares': 0.0, 'boom_width_m': 12.0},
        }
        mock_ctrl.gps_manager = MagicMock()
        mock_ctrl.gps_manager.get_state.return_value = fake_state

        resp = client.get('/api/gps')
        data = resp.get_json()

        assert resp.status_code == 200
        assert 'tcp_connected' not in data['connection']
        assert data['connection']['gps_connected'] is True
        assert data['connection']['source'] == 'serial'

    def test_returns_gps_disabled_when_no_manager(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        mock_ctrl.gps_manager = None

        resp = client.get('/api/gps')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['connection']['gps_enabled'] is False
