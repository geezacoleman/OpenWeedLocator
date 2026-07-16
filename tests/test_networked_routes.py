"""Tests for networked controller Flask API routes (config editor endpoints)."""

import json
import os
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
            'test-owl', 'save_config', {'filename': None, 'name': None, 'notes': None}
        )


@pytest.mark.unit
class TestApiOwlsCloud:
    """/api/owls exposes fleet-level cloud (Noktura) link fields."""

    def test_owls_includes_cloud_fields(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        mock_ctrl.cloud_enable = True
        mock_ctrl.cloud_connected = True
        mock_ctrl.cloud_device_id = 'east-farm'
        mock_ctrl.cloud_portal_url = 'https://app.noktura.tech'
        mock_ctrl.get_recent_owls.return_value = {}

        data = client.get('/api/owls').get_json()
        assert data['cloud_enabled'] is True
        assert data['cloud_connected'] is True
        assert data['cloud_device_id'] == 'east-farm'
        assert data['cloud_portal_url'] == 'https://app.noktura.tech'

    def test_owls_cloud_not_configured(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        mock_ctrl.cloud_enable = False
        mock_ctrl.cloud_connected = None
        mock_ctrl.cloud_device_id = ''
        mock_ctrl.cloud_portal_url = ''
        mock_ctrl.get_recent_owls.return_value = {}

        data = client.get('/api/owls').get_json()
        assert data['cloud_enabled'] is False
        assert data['cloud_connected'] is None


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
class TestConfigLibraryRoundTrip:
    """Save-to-library then Load must return exactly the values saved.

    Backend half of the 2026-07-10 field bug: adjusted settings saved to a new
    file came back as the OLD values on Load. (The frontend half — folding live
    slider values into the posted config — is pinned in test_config_files.py.)
    """

    def test_saved_values_survive_load(self, networked_test_client, tmp_config_dir):
        client, _ = networked_test_client
        import io
        import controller.networked.networked as net_mod

        resp = client.get('/api/presets/GENERAL_CONFIG')
        config = resp.get_json()['config']
        config['GreenOnBrown']['exg_min'] = '42'
        config['GreenOnBrown']['hue_max'] = '77'

        # Redirect the library write to the tmp config dir (the route derives
        # the real repo config/ from __file__) — same serialization code runs.
        def _write_to_tmp(path, write_callable):
            buf = io.StringIO()
            write_callable(buf)
            (tmp_config_dir / os.path.basename(str(path))).write_text(buf.getvalue())

        with patch.object(net_mod, 'atomic_write_config', side_effect=_write_to_tmp):
            save = client.post('/api/config/library', json={
                'config': config, 'name': 'field test', 'notes': ''
            })
        save_data = save.get_json()
        assert save.status_code == 200 and save_data['success'] is True
        preset_name = save_data['filename'].rsplit('.ini', 1)[0]

        loaded = client.get('/api/presets/' + preset_name).get_json()
        assert loaded['success'] is True
        assert loaded['config']['GreenOnBrown']['exg_min'] == '42'
        assert loaded['config']['GreenOnBrown']['hue_max'] == '77'


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


@pytest.mark.unit
class TestPainterSwatchRoute:
    """Tests for GET /api/painter/swatch (networked controller)."""

    def test_swatch_serves_png_with_coverage_header(self, networked_test_client,
                                                    tmp_path):
        import numpy as np
        import controller.networked.networked as net_mod
        from utils.lut_manager import LUTProfileManager

        client, _ = networked_test_client

        # Point the module's profile manager at a tmp dir with one profile
        mgr = LUTProfileManager(tmp_path / 'lut_profiles')
        green = np.tile(np.array([40, 180, 60], np.uint8), (1000, 1))
        brown = np.tile(np.array([60, 90, 120], np.uint8), (1000, 1))
        mgr.save('swatchy', green, brown)
        old_mgr = net_mod.lut_profile_manager
        net_mod.lut_profile_manager = mgr
        try:
            resp = client.get('/api/painter/swatch?name=swatchy&sensitivity=50')
            assert resp.status_code == 200
            assert resp.mimetype == 'image/png'
            # Coverage rides along as a header for the dashboard's fill square
            assert float(resp.headers['X-Coverage']) > 0

            resp = client.get('/api/painter/swatch?name=ghost')
            assert resp.status_code == 404
        finally:
            net_mod.lut_profile_manager = old_mgr


# ---------------------------------------------------------------------------
# Fleet registration API (phone-app "add an OWL" flow)
# ---------------------------------------------------------------------------

@pytest.fixture
def fleet_client(networked_test_client, tmp_path):
    """networked_test_client with a REAL FleetRoster on the mock controller."""
    from controller.networked.fleet_roster import FleetRoster
    client, mock_ctrl = networked_test_client
    mock_ctrl.fleet_roster = FleetRoster('192.168.1.2',
                                         path=tmp_path / 'fleet.json')
    mock_ctrl.get_fleet_view.return_value = ([], [], [])
    mock_ctrl.config.getint.return_value = 1883
    # JSON-serializable cloud fields for /api/owls responses
    mock_ctrl.cloud_enable = False
    mock_ctrl.cloud_connected = None
    mock_ctrl.cloud_device_id = ''
    mock_ctrl.cloud_portal_url = ''
    return client, mock_ctrl


@pytest.mark.unit
class TestFleetApi:
    def test_fleet_shape_identifies_controller(self, fleet_client):
        client, mock_ctrl = fleet_client
        data = client.get('/api/fleet').get_json()
        assert data['success'] is True
        assert data['controller']['device_id'] == 'owl-controller'
        assert data['controller']['controller_ip'] == '192.168.1.2'
        assert data['controller']['gateway'] == '192.168.1.1'
        # App checks this on rig discovery — must never silently disappear
        assert isinstance(data['controller']['contract_version'], int)
        assert data['devices'] == []
        assert data['reservations'] == []

    def test_reserve_returns_full_provisioning_payload(self, fleet_client):
        client, _ = fleet_client
        resp = client.post('/api/fleet/reserve', json={'name': 'Left boom'})
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['device_id'] == 'owl-1'
        assert data['assigned_ip'] == '192.168.1.11'
        assert data['gateway'] == '192.168.1.1'
        assert data['broker_ip'] == '192.168.1.2'
        assert data['broker_port'] == 1883
        assert data['reservation_ttl_s'] == 1800

    def test_reserve_skips_live_unregistered_devices(self, fleet_client):
        client, mock_ctrl = fleet_client
        mock_ctrl.get_fleet_view.return_value = ([], [], ['owl-1'])
        data = client.post('/api/fleet/reserve', json={}).get_json()
        assert data['device_id'] == 'owl-2'

    def test_reserve_invalid_name_conflicts(self, fleet_client):
        client, _ = fleet_client
        resp = client.post('/api/fleet/reserve', json={'name': '<script>'})
        assert resp.status_code == 409

    def test_confirm_promotes_reservation(self, fleet_client):
        client, _ = fleet_client
        client.post('/api/fleet/reserve', json={})
        resp = client.post('/api/fleet/confirm/owl-1',
                           json={'observed_ip': '192.168.1.11'})
        assert resp.status_code == 200
        assert resp.get_json()['device']['status'] == 'registered'

    def test_confirm_unknown_404(self, fleet_client):
        client, _ = fleet_client
        assert client.post('/api/fleet/confirm/owl-9',
                           json={}).status_code == 404

    def test_rename_and_validation(self, fleet_client):
        client, _ = fleet_client
        client.post('/api/fleet/reserve', json={})
        ok = client.patch('/api/fleet/owl-1', json={'name': 'Right boom'})
        assert ok.get_json()['device']['name'] == 'Right boom'
        assert client.patch('/api/fleet/owl-1',
                            json={'name': '<x>'}).status_code == 400
        assert client.patch('/api/fleet/owl-9',
                            json={'name': 'ok name'}).status_code == 404

    def test_delete_cancels_reservation(self, fleet_client):
        client, mock_ctrl = fleet_client
        client.post('/api/fleet/reserve', json={})
        assert client.delete('/api/fleet/owl-1').status_code == 200
        assert mock_ctrl.fleet_roster.get('owl-1') is None
        assert client.delete('/api/fleet/owl-1').status_code == 404

    def test_roster_unavailable_503(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        mock_ctrl.fleet_roster = None
        assert client.get('/api/fleet').status_code == 503
        assert client.post('/api/fleet/reserve', json={}).status_code == 503

    def test_fleet_is_reachable_through_access_guard(self, fleet_client):
        """Locked-down installs (DASHBOARD_OPEN unset) must still serve
        /api/fleet to LAN clients — the phone app depends on it."""
        client, _ = fleet_client
        import controller.networked.networked as net_mod
        with patch.object(net_mod, 'DASHBOARD_OPEN', False):
            allowed = client.get('/api/fleet',
                                 headers={'X-Real-IP': '192.168.1.50'})
            assert allowed.status_code == 200
            blocked = client.get('/api/presets',
                                 headers={'X-Real-IP': '192.168.1.50'})
            assert blocked.status_code == 403

    def test_guard_prefix_does_not_leak_to_lookalike_routes(self, fleet_client):
        """'/api/fleetfoo' must NOT inherit the fleet public grant."""
        client, _ = fleet_client
        import controller.networked.networked as net_mod
        with patch.object(net_mod, 'DASHBOARD_OPEN', False):
            response = client.get('/api/fleetfoo',
                                  headers={'X-Real-IP': '192.168.1.50'})
            assert response.status_code == 403


@pytest.mark.unit
class TestFleetTokenAuth:
    """Rename/remove from the LAN require the bearer token minted at
    reserve time; the kiosk (localhost) is always allowed."""

    def test_reserve_mints_a_token(self, fleet_client):
        client, _ = fleet_client
        data = client.post('/api/fleet/reserve', json={}).get_json()
        assert data['fleet_token']

    def test_token_never_appears_in_public_views(self, fleet_client):
        client, mock_ctrl = fleet_client
        client.post('/api/fleet/reserve', json={})
        mock_ctrl.get_fleet_view.return_value = \
            mock_ctrl.fleet_roster.snapshot() + (frozenset(),)
        fleet = client.get('/api/fleet').get_json()
        blob = str(fleet)
        assert 'token' not in blob

    def test_lan_mutations_need_the_token(self, fleet_client):
        client, _ = fleet_client
        token = client.post('/api/fleet/reserve', json={}).get_json()['fleet_token']
        lan = {'X-Real-IP': '192.168.1.50'}

        assert client.patch('/api/fleet/owl-1', json={'name': 'Boom'},
                            headers=lan).status_code == 403
        assert client.delete('/api/fleet/owl-1',
                             headers=lan).status_code == 403
        assert client.patch('/api/fleet/owl-1', json={'name': 'Boom'},
                            headers={**lan, 'X-Fleet-Token': 'nope'}
                            ).status_code == 403

        good = {**lan, 'X-Fleet-Token': token}
        assert client.patch('/api/fleet/owl-1', json={'name': 'Boom'},
                            headers=good).status_code == 200
        assert client.delete('/api/fleet/owl-1',
                             headers=good).status_code == 200

    def test_kiosk_localhost_needs_no_token(self, fleet_client):
        client, _ = fleet_client
        client.post('/api/fleet/reserve', json={})
        # test_client requests come from 127.0.0.1 — the kiosk recovery path
        assert client.patch('/api/fleet/owl-1',
                            json={'name': 'Boom'}).status_code == 200
        assert client.delete('/api/fleet/owl-1').status_code == 200


@pytest.mark.unit
class TestApiOwlsFleetOverlay:
    def test_registered_offline_stub_appears(self, fleet_client):
        client, mock_ctrl = fleet_client
        mock_ctrl.get_recent_owls.return_value = {}
        mock_ctrl.fleet_roster.reserve(name='Left boom')
        mock_ctrl.fleet_roster.confirm('owl-1')

        owls = client.get('/api/owls').get_json()['owls']
        assert owls['owl-1']['registered'] is True
        assert owls['owl-1']['connected'] is False
        assert owls['owl-1']['friendly_name'] == 'Left boom'
        assert owls['owl-1']['assigned_ip'] == '192.168.1.11'

    def test_live_entry_gains_roster_fields(self, fleet_client):
        client, mock_ctrl = fleet_client
        mock_ctrl.get_recent_owls.return_value = {
            'owl-1': {'device_id': 'owl-1', 'connected': True, 'cpu_temp': 61}}
        mock_ctrl.fleet_roster.reserve(name='Left boom')
        mock_ctrl.fleet_roster.confirm('owl-1')

        owls = client.get('/api/owls').get_json()['owls']
        assert owls['owl-1']['connected'] is True
        assert owls['owl-1']['registered'] is True
        assert owls['owl-1']['friendly_name'] == 'Left boom'
        assert owls['owl-1']['cpu_temp'] == 61

    def test_empty_roster_leaves_owls_untouched(self, fleet_client):
        client, mock_ctrl = fleet_client
        mock_ctrl.get_recent_owls.return_value = {
            'owl-7': {'device_id': 'owl-7', 'connected': True}}
        owls = client.get('/api/owls').get_json()['owls']
        assert owls == {'owl-7': {'device_id': 'owl-7', 'connected': True}}

    def test_absent_roster_is_byte_identical(self, fleet_client):
        """The isolation contract: /api/owls with no roster (None) must be
        byte-for-byte what an empty roster produces — the fleet feature is
        invisible until someone uses the app flow."""
        client, mock_ctrl = fleet_client
        mock_ctrl.get_recent_owls.return_value = {
            'owl-7': {'device_id': 'owl-7', 'connected': True, 'cpu_temp': 55}}

        with_empty_roster = client.get('/api/owls').data
        mock_ctrl.fleet_roster = None
        with_no_roster = client.get('/api/owls').data
        assert with_empty_roster == with_no_roster
