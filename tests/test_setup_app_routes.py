"""
Unit tests for the first-boot setup API (controller/setup/).

The NetworkManager is a MagicMock and timers fire only when the test
says so, so the full join handoff (including failure rollback) runs
synchronously on any platform.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from controller.setup.firstboot_state import FirstBootState
from controller.setup.setup_app import create_app


class CapturingTimer:
    """threading.Timer stand-in — collects callbacks; tests fire them."""

    pending = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.daemon = False

    def start(self):
        CapturingTimer.pending.append(self)

    def fire(self):
        CapturingTimer.pending.remove(self)
        self.function()

    @classmethod
    def fire_all(cls):
        while cls.pending:
            cls.pending[0].fire()


@pytest.fixture(autouse=True)
def clear_timers():
    CapturingTimer.pending = []
    yield
    CapturingTimer.pending = []


@pytest.fixture
def nm():
    manager = MagicMock()
    manager.is_supported.return_value = True
    manager.get_hotspot_connection.return_value = 'OWL-4'
    manager.get_ip4.return_value = '10.42.0.1'
    manager.status.return_value = {'mode': 'hotspot', 'ssid': 'OWL-4',
                                   'ip': '10.42.0.1', 'hotspot_profile': 'OWL-4'}
    manager.scan.return_value = [
        {'ssid': 'HomeNet', 'signal': 80, 'security': 'WPA2'}]
    manager.activate_connection.return_value = True
    manager.last_error = None
    return manager


@pytest.fixture
def state(nm, tmp_path):
    flag = tmp_path / 'owl-firstboot.flag'
    flag.touch()
    return FirstBootState(nm, flag_path=flag,
                          state_file=tmp_path / 'state.json',
                          timer_factory=CapturingTimer,
                          system_runner=MagicMock(),
                          exit_fn=MagicMock())


@pytest.fixture
def client(nm, state):
    app = create_app(network_manager=nm, state=state)
    app.config['TESTING'] = True
    return app.test_client()


@pytest.mark.unit
class TestInfo:
    def test_info_shape(self, client):
        with patch('controller.setup.setup_app.check_camera',
                   return_value={'ok': True, 'detail': 'frame received'}), \
                patch('controller.setup.setup_app.get_owl_service_status',
                      return_value='active'):
            data = client.get('/setup/api/info').get_json()
        assert data['success'] is True
        assert data['hotspot'] == {'ssid': 'OWL-4', 'ip': '10.42.0.1'}
        assert data['camera']['ok'] is True
        assert data['owl_service'] == 'active'
        assert 'version' in data and 'device_id' in data
        # Contract version: the app blocks with an "update" message on a
        # mismatch — this field must never silently disappear
        from version import APP_CONTRACT_VERSION
        assert data['contract_version'] == APP_CONTRACT_VERSION
        assert isinstance(data['contract_version'], int)

    def test_camera_failure_reported_not_fatal(self, client):
        with patch('controller.setup.setup_app.check_camera',
                   return_value={'ok': False, 'detail': 'no frame from OWL'}):
            data = client.get('/setup/api/info').get_json()
        assert data['success'] is True
        assert data['camera']['ok'] is False

    def test_first_request_marks_in_setup(self, client, state):
        assert state.state == 'await_phone'
        client.get('/setup/api/info')
        assert state.state == 'in_setup'


@pytest.mark.unit
class TestCameraFrame:
    def test_proxies_jpeg(self, client):
        fake_response = MagicMock()
        fake_response.read.return_value = b'\xff\xd8jpegbytes'
        fake_response.__enter__ = lambda s: fake_response
        fake_response.__exit__ = MagicMock(return_value=False)
        with patch('controller.setup.setup_app.urllib.request.urlopen',
                   return_value=fake_response):
            response = client.get('/setup/api/camera/frame')
        assert response.status_code == 200
        assert response.mimetype == 'image/jpeg'
        assert response.data.startswith(b'\xff\xd8')

    def test_503_when_owl_not_streaming(self, client):
        import urllib.error
        with patch('controller.setup.setup_app.urllib.request.urlopen',
                   side_effect=urllib.error.URLError('refused')):
            response = client.get('/setup/api/camera/frame')
        assert response.status_code == 503
        assert response.get_json()['success'] is False


@pytest.mark.unit
class TestWifiScan:
    def test_cached_scan_default(self, client, state, nm):
        state.scan_cache = [{'ssid': 'Cached', 'signal': 50, 'security': 'WPA2'}]
        data = client.get('/setup/api/wifi/scan').get_json()
        assert data['cached'] is True
        assert data['networks'][0]['ssid'] == 'Cached'
        nm.scan.assert_not_called()

    def test_rescan_cycles_hotspot(self, client, nm):
        with patch('controller.setup.firstboot_state.time.sleep'):
            data = client.get('/setup/api/wifi/scan?rescan=true').get_json()
        assert data['cached'] is False
        assert data['networks'][0]['ssid'] == 'HomeNet'
        # AP must come down before the scan and back up after
        call_order = [c[0] for c in
                      (nm.hotspot_down.mock_calls + nm.mock_calls) if c[0] in
                      ('hotspot_down', 'scan', 'hotspot_up')]
        assert call_order.index('hotspot_down') < call_order.index('scan') \
            < call_order.index('hotspot_up')


@pytest.mark.unit
class TestWifiJoin:
    def test_join_returns_202_with_verify_info(self, client):
        response = client.post('/setup/api/wifi/join',
                               json={'ssid': 'HomeNet', 'password': 'secret123'})
        assert response.status_code == 202
        data = response.get_json()
        assert data['state'] == 'pending'
        assert data['verify']['service_type'] == '_owl-setup._tcp'
        assert len(CapturingTimer.pending) == 1  # switch scheduled, not run

    def test_missing_ssid_rejected(self, client):
        response = client.post('/setup/api/wifi/join', json={'password': 'secret123'})
        assert response.status_code == 400

    def test_short_password_rejected(self, client):
        response = client.post('/setup/api/wifi/join',
                               json={'ssid': 'HomeNet', 'password': 'short'})
        assert response.status_code == 400

    def test_double_join_conflicts(self, client):
        client.post('/setup/api/wifi/join',
                    json={'ssid': 'HomeNet', 'password': 'secret123'})
        response = client.post('/setup/api/wifi/join',
                               json={'ssid': 'Other', 'password': 'secret123'})
        assert response.status_code == 409

    def test_successful_switch(self, client, state, nm):
        client.post('/setup/api/wifi/join',
                    json={'ssid': 'HomeNet', 'password': 'secret123'})
        nm.get_ip4.return_value = '192.168.1.57'
        CapturingTimer.fire_all()

        nm.add_wifi_connection.assert_called_once_with('HomeNet', 'secret123')
        nm.hotspot_down.assert_called_once()
        result = client.get('/setup/api/wifi/result').get_json()
        assert result['state'] == 'connected'
        assert result['ip'] == '192.168.1.57'

    def test_failed_switch_rolls_back_to_hotspot(self, client, nm):
        nm.activate_connection.return_value = False
        nm.last_error = 'auth'
        client.post('/setup/api/wifi/join',
                    json={'ssid': 'HomeNet', 'password': 'wrongpass1'})
        CapturingTimer.fire_all()

        nm.delete_connection.assert_called_once_with('HomeNet')
        nm.hotspot_up.assert_called_once()
        result = client.get('/setup/api/wifi/result').get_json()
        assert result['state'] == 'failed'
        assert result['error'] == 'auth'

    def test_result_persists_across_app_recreation(self, client, state, nm, tmp_path):
        client.post('/setup/api/wifi/join',
                    json={'ssid': 'HomeNet', 'password': 'secret123'})
        nm.get_ip4.return_value = '192.168.1.57'
        CapturingTimer.fire_all()

        reloaded = FirstBootState(nm, flag_path=state.flag_path,
                                  state_file=state.state_file,
                                  timer_factory=CapturingTimer,
                                  system_runner=MagicMock(),
                                  exit_fn=MagicMock())
        assert reloaded.wifi_state == 'connected'
        assert reloaded.wifi_ssid == 'HomeNet'


@pytest.mark.unit
class TestHotspotRestore:
    def test_restores_and_clears_result(self, client, state, nm):
        state.wifi_state = 'failed'
        state.wifi_ssid = 'HomeNet'
        response = client.post('/setup/api/hotspot/restore')
        assert response.get_json()['success'] is True
        nm.delete_connection.assert_called_with('HomeNet')
        nm.hotspot_up.assert_called()
        assert client.get('/setup/api/wifi/result').get_json()['state'] == 'idle'


@pytest.mark.unit
class TestStatus:
    def test_status_shape(self, client, state):
        data = client.get('/setup/api/status').get_json()
        assert data['success'] is True
        assert data['flag_armed'] is True
        assert data['network']['mode'] == 'hotspot'

    def test_flag_absent_reported(self, client, state):
        state.flag_path.unlink()
        assert client.get('/setup/api/status').get_json()['flag_armed'] is False


@pytest.mark.unit
class TestFinish:
    def test_standalone_requires_new_password(self, client):
        response = client.post('/setup/api/finish', json={'mode': 'standalone'})
        assert response.status_code == 400

    def test_standalone_applies_password_and_tears_down(self, client, state, nm):
        response = client.post('/setup/api/finish',
                               json={'mode': 'standalone',
                                     'new_password': 'paddock-relay-42'})
        assert response.get_json()['success'] is True
        nm.set_hotspot_password.assert_called_once_with('paddock-relay-42')
        assert not state.flag_path.exists()
        state._system_runner.assert_called_once_with(
            ['ufw', 'delete', 'allow', '8088/tcp'])
        assert len(CapturingTimer.pending) == 1  # delayed exit scheduled
        CapturingTimer.fire_all()
        state._exit_fn.assert_called_once()

    def test_wifi_finish_requires_connected(self, client):
        response = client.post('/setup/api/finish', json={'mode': 'wifi'})
        assert response.status_code == 409

    def test_wifi_finish_after_connected(self, client, state):
        state.wifi_state = 'connected'
        response = client.post('/setup/api/finish', json={'mode': 'wifi'})
        assert response.get_json()['success'] is True
        assert not state.flag_path.exists()

    def test_unknown_mode_rejected(self, client):
        response = client.post('/setup/api/finish', json={'mode': 'banana'})
        assert response.status_code == 400


@pytest.mark.unit
class TestCors:
    def test_allowed_origin_is_reflected(self, client):
        response = client.get('/setup/api/status',
                              headers={'Origin': 'http://localhost:4180'})
        assert response.headers['Access-Control-Allow-Origin'] \
            == 'http://localhost:4180'
        assert response.headers['Vary'] == 'Origin'

    def test_null_origin_allowed_for_file_pages(self, client):
        response = client.get('/setup/api/status', headers={'Origin': 'null'})
        assert response.headers['Access-Control-Allow-Origin'] == 'null'

    def test_unknown_origin_gets_no_cors_grant(self, client):
        response = client.get('/setup/api/status',
                              headers={'Origin': 'http://evil.example'})
        assert 'Access-Control-Allow-Origin' not in response.headers

    def test_preflight_options_for_allowed_origin(self, client):
        response = client.open('/setup/api/wifi/join', method='OPTIONS',
                               headers={'Origin': 'http://localhost:4180'})
        assert response.status_code in (200, 204)
        assert 'POST' in response.headers['Access-Control-Allow-Methods']


@pytest.mark.unit
class TestOwlDetectionSuppression:
    """owl.py can't import on Windows (cv2/GPIO) — inspect the source."""

    def test_owl_checks_first_boot_flag(self):
        from pathlib import Path
        source = (Path(__file__).parent.parent / 'owl.py').read_text(encoding='utf-8')
        assert 'OWL_FIRSTBOOT_FLAG' in source
        assert 'owl-firstboot.flag' in source
        # The flag must gate the value fed into the shared detection state
        flag_block = source.split('OWL_FIRSTBOOT_FLAG')[1][:600]
        assert 'detection_enable_config = False' in flag_block

    def test_flag_never_touches_the_hot_path(self):
        """HOT-PATH GUARD: first-boot code may exist ONLY in __init__ (one
        stat at boot) and the update_state thread (slow re-stat while armed).
        Any reference from another method — especially the detection loop —
        would add per-frame cost and must fail this test."""
        import ast
        from pathlib import Path
        source = (Path(__file__).parent.parent / 'owl.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        offenders = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                segment = ast.get_source_segment(source, node) or ''
                if 'firstboot' in segment.lower():
                    offenders.add(node.name)
        # update_state contains nested defs? No — but walk visits nested
        # functions separately, so only the directly-containing names appear
        assert offenders <= {'__init__', 'update_state'}, (
            f'first-boot references leaked into: {offenders}')

    def test_update_state_forces_detection_off_while_armed(self):
        """While the flag exists, a hardware switch or dashboard toggle must
        not re-enable detection (bench relay-click regression)."""
        import ast
        from pathlib import Path
        source = (Path(__file__).parent.parent / 'owl.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        update_state = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == 'update_state')
        segment = ast.get_source_segment(source, update_state)
        assert '_firstboot_active' in segment
        assert 'self._detection_enable = False' in segment


@pytest.mark.unit
class TestStartup:
    def test_dev_mode_without_nmcli(self, nm, state):
        nm.is_supported.return_value = False
        state.startup()
        assert state.state == 'in_setup'
        nm.hotspot_up.assert_not_called()

    def test_normal_startup_scans_then_raises_ap(self, nm, state):
        with patch('controller.setup.firstboot_state.time.sleep'):
            state.startup()
        assert state.state == 'await_phone'
        assert state.scan_cache[0]['ssid'] == 'HomeNet'
        nm.hotspot_up.assert_called()
        # Fixed setup PSK re-applied so a flag-only re-arm still works
        from controller.setup.firstboot_state import SETUP_HOTSPOT_PSK
        nm.set_hotspot_password.assert_called_once_with(SETUP_HOTSPOT_PSK)

    def test_startup_recreates_ufw_rule(self, nm, state):
        with patch('controller.setup.firstboot_state.time.sleep'):
            state.startup()
        ufw_calls = [c for c in state._system_runner.mock_calls
                     if c.args and c.args[0][:2] == ['ufw', 'allow']]
        assert ufw_calls, 'startup must re-add the setup port ufw rule'

    def test_reboot_onto_client_network_resumes(self, nm, state):
        nm.status.return_value = {'mode': 'client', 'ssid': 'HomeNet',
                                  'ip': '192.168.1.57', 'hotspot_profile': 'OWL-4'}
        state.startup()
        assert state.wifi_state == 'connected'
        assert state.wifi_ip == '192.168.1.57'
        nm.hotspot_up.assert_not_called()


# ---------------------------------------------------------------------------
# Controller join (networked rig provisioning)
# ---------------------------------------------------------------------------

JOIN_PAYLOAD = {
    'ssid': 'RigNet', 'password': 'rigpass123',
    'device_id': 'owl-3', 'static_ip': '192.168.1.13',
    'gateway': '192.168.1.1', 'subnet_prefix': 24,
    'broker_ip': '192.168.1.2', 'broker_port': 1883,
}


@pytest.fixture
def ctrl_state(nm, tmp_path):
    """FirstBootState with a real CONTROLLER.ini in tmp_path."""
    import configparser
    ini = tmp_path / 'CONTROLLER.ini'
    config = configparser.ConfigParser()
    config['WebDashboard'] = {'port': '8000'}          # must survive untouched
    config['MQTT'] = {'enable': 'True', 'broker_ip': 'localhost',
                      'device_id': 'auto'}
    with open(ini, 'w') as handle:
        config.write(handle)
    flag = tmp_path / 'owl-firstboot.flag'
    flag.touch()
    return FirstBootState(nm, flag_path=flag,
                          state_file=tmp_path / 'state.json',
                          timer_factory=CapturingTimer,
                          system_runner=MagicMock(
                              return_value=MagicMock(returncode=0)),
                          exit_fn=MagicMock(),
                          controller_ini=ini)


@pytest.fixture
def ctrl_client(nm, ctrl_state):
    app = create_app(network_manager=nm, state=ctrl_state)
    app.config['TESTING'] = True
    return app.test_client()


@pytest.mark.unit
class TestControllerJoinValidation:
    def test_missing_fields_rejected(self, ctrl_client):
        response = ctrl_client.post('/setup/api/controller/join',
                                    json={'ssid': 'RigNet'})
        assert response.status_code == 400
        assert 'Missing fields' in response.get_json()['error']

    def test_bad_device_id_rejected(self, ctrl_client):
        payload = dict(JOIN_PAYLOAD, device_id='OWL_3!')
        assert ctrl_client.post('/setup/api/controller/join',
                                json=payload).status_code == 400

    def test_bad_ip_rejected(self, ctrl_client):
        payload = dict(JOIN_PAYLOAD, static_ip='not-an-ip')
        assert ctrl_client.post('/setup/api/controller/join',
                                json=payload).status_code == 400

    def test_short_password_rejected(self, ctrl_client):
        payload = dict(JOIN_PAYLOAD, password='short')
        assert ctrl_client.post('/setup/api/controller/join',
                                json=payload).status_code == 400

    def test_double_join_conflicts(self, ctrl_client):
        assert ctrl_client.post('/setup/api/controller/join',
                                json=JOIN_PAYLOAD).status_code == 202
        assert ctrl_client.post('/setup/api/controller/join',
                                json=JOIN_PAYLOAD).status_code == 409


@pytest.mark.unit
class TestControllerJoinSwitch:
    def test_202_shape_points_at_static_ip(self, ctrl_client):
        data = ctrl_client.post('/setup/api/controller/join',
                                json=JOIN_PAYLOAD).get_json()
        assert data['state'] == 'pending'
        assert data['verify']['static_ip'] == '192.168.1.13'
        assert data['verify']['hostname'] == 'owl-3.local'

    def test_successful_switch_sequence(self, ctrl_client, ctrl_state, nm):
        ctrl_client.post('/setup/api/controller/join', json=JOIN_PAYLOAD)
        nm.get_ip4.return_value = '192.168.1.13'
        CapturingTimer.fire_all()

        nm.set_hostname.assert_called_once_with('owl-3',
                                                ctrl_state._system_runner)
        nm.add_wifi_connection.assert_called_once_with(
            'RigNet', 'rigpass123', static_ip='192.168.1.13',
            gateway='192.168.1.1', prefix=24, dns=None)
        nm.hotspot_down.assert_called_once()
        restart_calls = [c for c in ctrl_state._system_runner.mock_calls
                         if c.args and c.args[0] == ['systemctl', 'restart',
                                                     'owl.service']]
        assert restart_calls

        result = ctrl_client.get('/setup/api/wifi/result').get_json()
        assert result['state'] == 'connected'
        assert result['mode'] == 'controller'
        assert result['device_id'] == 'owl-3'
        assert result['static_ip'] == '192.168.1.13'

    def test_ini_written_and_other_sections_preserved(self, ctrl_client,
                                                      ctrl_state):
        import configparser
        ctrl_client.post('/setup/api/controller/join', json=JOIN_PAYLOAD)
        CapturingTimer.fire_all()

        config = configparser.ConfigParser()
        config.read(ctrl_state.controller_ini)
        assert config.get('MQTT', 'broker_ip') == '192.168.1.2'
        assert config.get('MQTT', 'device_id') == 'owl-3'
        assert config.get('Network', 'mode') == 'networked'
        assert config.get('Network', 'static_ip') == '192.168.1.13'
        assert config.get('Network', 'controller_ip') == '192.168.1.2'
        assert config.get('WebDashboard', 'port') == '8000'

    def test_failed_activation_reverts_but_keeps_config(self, ctrl_client,
                                                        ctrl_state, nm):
        import configparser
        nm.activate_connection.return_value = False
        nm.last_error = 'auth'
        ctrl_client.post('/setup/api/controller/join', json=JOIN_PAYLOAD)
        CapturingTimer.fire_all()

        nm.delete_connection.assert_called_once_with('RigNet')
        nm.hotspot_up.assert_called_once()
        result = ctrl_client.get('/setup/api/wifi/result').get_json()
        assert result['state'] == 'failed'
        assert result['error'] == 'auth'
        # Target config deliberately kept for the retry
        config = configparser.ConfigParser()
        config.read(ctrl_state.controller_ini)
        assert config.get('MQTT', 'device_id') == 'owl-3'

    def test_hostname_failure_reverts_without_profile_delete(self, ctrl_client,
                                                             nm):
        from utils.network_manager import NetworkManagerError
        nm.set_hostname.side_effect = NetworkManagerError('nope')
        ctrl_client.post('/setup/api/controller/join', json=JOIN_PAYLOAD)
        CapturingTimer.fire_all()

        nm.add_wifi_connection.assert_not_called()
        nm.delete_connection.assert_not_called()
        nm.hotspot_up.assert_called_once()
        result = ctrl_client.get('/setup/api/wifi/result').get_json()
        assert result['state'] == 'failed'

    def test_mode_fields_survive_reboot(self, ctrl_client, ctrl_state, nm):
        ctrl_client.post('/setup/api/controller/join', json=JOIN_PAYLOAD)
        nm.get_ip4.return_value = '192.168.1.13'
        CapturingTimer.fire_all()

        reloaded = FirstBootState(nm, flag_path=ctrl_state.flag_path,
                                  state_file=ctrl_state.state_file,
                                  timer_factory=CapturingTimer,
                                  system_runner=MagicMock(),
                                  exit_fn=MagicMock(),
                                  controller_ini=ctrl_state.controller_ini)
        assert reloaded.wifi_mode == 'controller'
        assert reloaded.join_device_id == 'owl-3'
        assert reloaded.join_static_ip == '192.168.1.13'


@pytest.mark.unit
class TestControllerFinish:
    def test_finish_requires_connected_controller_join(self, ctrl_client):
        response = ctrl_client.post('/setup/api/finish',
                                    json={'mode': 'controller'})
        assert response.status_code == 409

    def test_wifi_join_does_not_satisfy_controller_finish(self, ctrl_client,
                                                          ctrl_state):
        ctrl_state.wifi_state = 'connected'
        ctrl_state.wifi_mode = 'wifi'
        assert ctrl_client.post('/setup/api/finish',
                                json={'mode': 'controller'}).status_code == 409

    def test_finish_after_controller_join(self, ctrl_client, ctrl_state, nm):
        ctrl_client.post('/setup/api/controller/join', json=JOIN_PAYLOAD)
        nm.get_ip4.return_value = '192.168.1.13'
        CapturingTimer.fire_all()

        response = ctrl_client.post('/setup/api/finish',
                                    json={'mode': 'controller'})
        assert response.get_json()['success'] is True
        assert not ctrl_state.flag_path.exists()
