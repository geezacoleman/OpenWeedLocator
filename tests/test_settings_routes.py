"""Unit tests for controller/standalone/settings_routes.py (R3).

Token semantics matrix, network routes, password routes (with the
never-log-the-password guarantee), and the MQTT-first power paths.
Blueprint is mounted on a bare Flask app with injected fakes.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from controller.standalone.network_settings import NetworkSettingsManager
from controller.standalone.settings_routes import (
    PASSWORD_HELPER, make_token_reader, require_device_token,
    create_settings_blueprint,
)
from utils.network_manager import NetworkManagerError


class CapturingTimer:
    pending = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.daemon = False

    def start(self):
        CapturingTimer.pending.append(self)

    def cancel(self):
        if self in CapturingTimer.pending:
            CapturingTimer.pending.remove(self)

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
    manager.get_ip4.return_value = '10.42.0.1'
    manager.status.return_value = {'mode': 'hotspot', 'ssid': 'OWL-4b7d',
                                   'ip': '10.42.0.1', 'hotspot_profile': 'OWL-4b7d'}
    manager.scan.return_value = [
        {'ssid': 'HomeNet', 'signal': 70, 'security': 'WPA2'}]
    manager.activate_connection.return_value = True
    manager.last_error = None
    return manager


@pytest.fixture
def mgr(nm):
    return NetworkSettingsManager(nm, timer_factory=CapturingTimer)


def build_client(mgr, tmp_path, token=None, network_mode='standalone',
                 mqtt=None, owl_active=False, runner=None):
    ini = tmp_path / 'CONTROLLER.ini'
    lines = ['[Network]', f'mode = {network_mode}']
    if token:
        lines += ['[Security]', f'device_token = {token}']
    ini.write_text('\n'.join(lines) + '\n')

    app = Flask(__name__)
    app.config['TESTING'] = True
    bp = create_settings_blueprint(
        manager=mgr,
        token_reader=make_token_reader(str(ini)),
        mqtt_getter=lambda: mqtt,
        logger=logging.getLogger('test-settings'),
        controller_ini_path=str(ini),
        owl_active_getter=lambda: owl_active,
        runner=runner or MagicMock(return_value=MagicMock(returncode=0,
                                                          stdout='', stderr='')),
        timer_factory=CapturingTimer,
    )
    app.register_blueprint(bp)
    return app.test_client()


TOKEN = 'test-device-token-12345'
HEADERS = {'X-Device-Token': TOKEN}

MUTATING_ROUTES = [
    ('/api/settings/network/scan', {}),
    ('/api/settings/network/join', {'ssid': 'HomeNet'}),
    ('/api/settings/network/cancel', {}),
    ('/api/settings/password/user', {'password': 'longenough1'}),
    ('/api/settings/password/hotspot', {'password': 'longenough1'}),
    ('/api/system/shutdown', {}),
    ('/api/system/reboot', {}),
]


@pytest.mark.unit
class TestTokenMatrix:
    @pytest.mark.parametrize('route,payload', MUTATING_ROUTES)
    def test_missing_token_rejected(self, mgr, tmp_path, route, payload):
        client = build_client(mgr, tmp_path, token=TOKEN)
        response = client.post(route, json=payload)
        assert response.status_code == 403

    @pytest.mark.parametrize('route,payload', MUTATING_ROUTES)
    def test_wrong_token_rejected(self, mgr, tmp_path, route, payload):
        client = build_client(mgr, tmp_path, token=TOKEN)
        response = client.post(route, json=payload,
                               headers={'X-Device-Token': 'wrong-token'})
        assert response.status_code == 403

    def test_correct_token_allowed(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path, token=TOKEN)
        response = client.post('/api/settings/network/cancel', json={},
                               headers=HEADERS)
        assert response.status_code == 200

    def test_legacy_device_without_token_allows_headerless(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path, token=None)
        response = client.post('/api/settings/network/cancel', json={})
        assert response.status_code == 200

    def test_get_routes_never_403(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path, token=TOKEN)
        for route in ('/api/settings/network/status',
                      '/api/settings/network/scan',
                      '/api/settings/network/result'):
            response = client.get(route)
            assert response.status_code == 200, route


@pytest.mark.unit
class TestTokenReader:
    def test_missing_file_returns_none(self, tmp_path):
        reader = make_token_reader(str(tmp_path / 'nope.ini'))
        assert reader() is None

    def test_reads_token(self, tmp_path):
        ini = tmp_path / 'CONTROLLER.ini'
        ini.write_text('[Security]\ndevice_token = abc123\n')
        reader = make_token_reader(str(ini))
        assert reader() == 'abc123'

    def test_rewrite_refreshes_cache(self, tmp_path):
        import os
        ini = tmp_path / 'CONTROLLER.ini'
        ini.write_text('[Security]\ndevice_token = first\n')
        reader = make_token_reader(str(ini))
        assert reader() == 'first'
        ini.write_text('[Security]\ndevice_token = second\n')
        # Force a distinct mtime (same-second writes on coarse filesystems)
        stat = os.stat(ini)
        os.utime(ini, (stat.st_atime, stat.st_mtime + 2))
        assert reader() == 'second'

    def test_no_security_section_returns_none(self, tmp_path):
        ini = tmp_path / 'CONTROLLER.ini'
        ini.write_text('[Network]\nmode = standalone\n')
        reader = make_token_reader(str(ini))
        assert reader() is None


@pytest.mark.unit
class TestNetworkRoutes:
    def test_status_shape(self, mgr, tmp_path, nm):
        client = build_client(mgr, tmp_path)
        data = client.get('/api/settings/network/status').get_json()
        assert data['success'] is True
        assert data['network']['mode'] == 'hotspot'
        assert data['switch']['state'] == 'idle'

    def test_scan_hotspot_mode_202_and_cycles(self, mgr, tmp_path, nm):
        client = build_client(mgr, tmp_path)
        response = client.post('/api/settings/network/scan', json={})
        assert response.status_code == 202
        data = response.get_json()
        assert data['state'] == 'scanning'
        assert data['warning'] == 'hotspot_cycles'
        CapturingTimer.fire_all()
        nm.hotspot_down.assert_called_once()
        nm.hotspot_up.assert_called_once()
        cached = client.get('/api/settings/network/scan').get_json()
        assert cached['networks'][0]['ssid'] == 'HomeNet'
        assert cached['scanning'] is False

    def test_scan_client_mode_synchronous(self, mgr, tmp_path, nm):
        nm.status.return_value = {'mode': 'client', 'ssid': 'HomeNet',
                                  'ip': '192.168.0.9', 'hotspot_profile': 'OWL-4b7d'}
        client = build_client(mgr, tmp_path)
        response = client.post('/api/settings/network/scan', json={})
        assert response.status_code == 200
        data = response.get_json()
        assert data['networks'][0]['ssid'] == 'HomeNet'
        assert 'scanned_at' in data

    def test_join_requires_ssid(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path)
        response = client.post('/api/settings/network/join', json={})
        assert response.status_code == 400

    def test_join_short_password_rejected(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path)
        response = client.post('/api/settings/network/join',
                               json={'ssid': 'HomeNet', 'password': 'short'})
        assert response.status_code == 400

    def test_join_networked_mode_guarded(self, mgr, tmp_path):
        """Fleet OWLs hold controller-assigned static IPs — an in-place join
        would break rig membership, so the route refuses outright."""
        client = build_client(mgr, tmp_path, network_mode='networked')
        response = client.post('/api/settings/network/join',
                               json={'ssid': 'HomeNet', 'password': 'secret123'})
        assert response.status_code == 409

    def test_join_returns_202_with_verify_hints(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path)
        response = client.post('/api/settings/network/join',
                               json={'ssid': 'HomeNet', 'password': 'secret123'})
        assert response.status_code == 202
        data = response.get_json()
        assert data['state'] == 'pending'
        assert data['switch_delay_s'] == 5
        assert data['verify']['port'] == 443
        assert data['verify']['hostname'].endswith('.local')

    def test_join_conflict_409(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path)
        client.post('/api/settings/network/join',
                    json={'ssid': 'HomeNet', 'password': 'secret123'})
        response = client.post('/api/settings/network/join',
                               json={'ssid': 'OtherNet', 'password': 'secret123'})
        assert response.status_code == 409

    def test_result_reflects_join_outcome(self, mgr, tmp_path, nm):
        client = build_client(mgr, tmp_path)
        client.post('/api/settings/network/join',
                    json={'ssid': 'HomeNet', 'password': 'secret123'})
        CapturingTimer.fire_all()
        data = client.get('/api/settings/network/result').get_json()
        assert data['state'] == 'connected'
        assert data['previous'] == {'ssid': 'OWL-4b7d', 'mode': 'hotspot'}

    def test_cancel_during_switch_409(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path)
        mgr.state = 'switching'
        response = client.post('/api/settings/network/cancel', json={})
        assert response.status_code == 409


@pytest.mark.unit
class TestUserPassword:
    def test_short_password_400(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path)
        response = client.post('/api/settings/password/user',
                               json={'password': 'short'})
        assert response.status_code == 400

    def test_helper_missing_503(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path)
        with patch('controller.standalone.settings_routes.os.path.exists',
                   return_value=False):
            response = client.post('/api/settings/password/user',
                                   json={'password': 'longenough1'})
        assert response.status_code == 503
        assert 'setup.sh' in response.get_json()['error']

    def test_success_pipes_password_on_stdin(self, mgr, tmp_path):
        runner = MagicMock(return_value=MagicMock(returncode=0, stdout='',
                                                  stderr=''))
        client = build_client(mgr, tmp_path, runner=runner)
        with patch('controller.standalone.settings_routes.os.path.exists',
                   return_value=True):
            response = client.post('/api/settings/password/user',
                                   json={'password': 'longenough1'})
        assert response.status_code == 200
        argv = runner.call_args.args[0]
        assert argv == ['/usr/bin/sudo', '-n', PASSWORD_HELPER]
        assert runner.call_args.kwargs['input'] == 'longenough1\n'

    def test_password_never_logged(self, mgr, tmp_path, caplog):
        runner = MagicMock(return_value=MagicMock(returncode=1, stdout='',
                                                  stderr='chpasswd failed'))
        client = build_client(mgr, tmp_path, runner=runner)
        with caplog.at_level(logging.DEBUG):
            with patch('controller.standalone.settings_routes.os.path.exists',
                       return_value=True):
                client.post('/api/settings/password/user',
                            json={'password': 'sup3rsecretpw'})
        assert 'sup3rsecretpw' not in caplog.text

    def test_helper_failure_500(self, mgr, tmp_path):
        runner = MagicMock(return_value=MagicMock(returncode=1, stdout='',
                                                  stderr='chpasswd failed'))
        client = build_client(mgr, tmp_path, runner=runner)
        with patch('controller.standalone.settings_routes.os.path.exists',
                   return_value=True):
            response = client.post('/api/settings/password/user',
                                   json={'password': 'longenough1'})
        assert response.status_code == 500


@pytest.mark.unit
class TestHotspotPassword:
    def test_hotspot_mode_defers_rekey(self, mgr, tmp_path, nm):
        client = build_client(mgr, tmp_path)
        response = client.post('/api/settings/password/hotspot',
                               json={'password': 'newhotspot1'})
        assert response.status_code == 202
        assert response.get_json()['applied_in_s'] == 5
        nm.set_hotspot_password.assert_not_called()
        CapturingTimer.fire_all()
        nm.set_hotspot_password.assert_called_once_with('newhotspot1')

    def test_client_mode_modifies_profile_only(self, mgr, tmp_path, nm):
        nm.status.return_value = {'mode': 'client', 'ssid': 'HomeNet',
                                  'ip': '192.168.0.9', 'hotspot_profile': 'OWL-4b7d'}
        client = build_client(mgr, tmp_path)
        response = client.post('/api/settings/password/hotspot',
                               json={'password': 'newhotspot1'})
        assert response.status_code == 200
        # reapply=False: con up here would steal the radio from the client net
        nm.set_hotspot_password.assert_called_once_with('newhotspot1',
                                                        reapply=False)

    def test_short_password_400(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path)
        response = client.post('/api/settings/password/hotspot',
                               json={'password': 'short'})
        assert response.status_code == 400


@pytest.mark.unit
class TestPower:
    def test_shutdown_via_mqtt_when_owl_active(self, mgr, tmp_path):
        mqtt = MagicMock()
        client = build_client(mgr, tmp_path, mqtt=mqtt, owl_active=True)
        response = client.post('/api/system/shutdown', json={})
        assert response.status_code == 200
        assert response.get_json()['via'] == 'mqtt'
        mqtt._send_command.assert_called_once_with('shutdown')

    def test_shutdown_direct_when_owl_inactive(self, mgr, tmp_path):
        runner = MagicMock(return_value=MagicMock(returncode=0, stdout='',
                                                  stderr=''))
        mqtt = MagicMock()
        client = build_client(mgr, tmp_path, mqtt=mqtt, owl_active=False,
                              runner=runner)
        response = client.post('/api/system/shutdown', json={})
        assert response.status_code == 200
        assert response.get_json()['via'] == 'direct'
        mqtt._send_command.assert_not_called()
        runner.assert_not_called()  # deferred so the response flushes first
        CapturingTimer.fire_all()
        argv = runner.call_args.args[0]
        assert argv[:2] == ['/usr/bin/sudo', '-n']
        assert argv[-1] == 'now'
        assert 'shutdown' in argv[2]

    def test_shutdown_direct_when_mqtt_down(self, mgr, tmp_path):
        runner = MagicMock(return_value=MagicMock(returncode=0, stdout='',
                                                  stderr=''))
        client = build_client(mgr, tmp_path, mqtt=None, owl_active=True,
                              runner=runner)
        response = client.post('/api/system/shutdown', json={})
        assert response.get_json()['via'] == 'direct'

    def test_reboot_direct_has_no_now_argument(self, mgr, tmp_path):
        runner = MagicMock(return_value=MagicMock(returncode=0, stdout='',
                                                  stderr=''))
        client = build_client(mgr, tmp_path, runner=runner)
        response = client.post('/api/system/reboot', json={})
        assert response.get_json()['via'] == 'direct'
        CapturingTimer.fire_all()
        argv = runner.call_args.args[0]
        assert 'reboot' in argv[2]
        assert argv[-1] != 'now'

    def test_mqtt_failure_falls_back_to_direct(self, mgr, tmp_path):
        runner = MagicMock(return_value=MagicMock(returncode=0, stdout='',
                                                  stderr=''))
        mqtt = MagicMock()
        mqtt._send_command.side_effect = RuntimeError('broker gone')
        client = build_client(mgr, tmp_path, mqtt=mqtt, owl_active=True,
                              runner=runner)
        response = client.post('/api/system/reboot', json={})
        assert response.get_json()['via'] == 'direct'


@pytest.mark.unit
class TestStandaloneTokenGuards:
    """The two guards living in standalone.py itself (not the blueprint):
    /api/owl/restart and the R3 token retrofit on downloads DELETE."""

    def test_restart_route_403_without_token(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.token_reader = lambda: TOKEN
        response = client.post('/api/owl/restart')
        assert response.status_code == 403

    def test_restart_route_runs_systemctl_with_token(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.token_reader = lambda: TOKEN
        dashboard._run_systemctl_command = MagicMock(
            return_value=MagicMock(returncode=0, stdout='', stderr=''))
        response = client.post('/api/owl/restart', headers=HEADERS)
        assert response.status_code == 200
        dashboard._run_systemctl_command.assert_called_once_with(
            ['restart', 'owl.service'], needs_sudo=True)

    def test_restart_route_legacy_headerless(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.token_reader = lambda: None
        dashboard._run_systemctl_command = MagicMock(
            return_value=MagicMock(returncode=0, stdout='', stderr=''))
        response = client.post('/api/owl/restart')
        assert response.status_code == 200

    def test_downloads_delete_403_without_token(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.token_reader = lambda: TOKEN
        response = client.delete('/api/downloads/session/20260101')
        assert response.status_code == 403
