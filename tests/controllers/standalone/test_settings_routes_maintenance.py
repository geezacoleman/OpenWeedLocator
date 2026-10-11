"""Maintenance-card routes on the device-settings blueprint: temporary
join / revert, the internet probe, and the software-update start/status
pair. Reuses build_client + fixtures from test_settings_routes.py."""

import json
import time
from unittest.mock import MagicMock

import pytest

from controller.standalone import network_settings as ns
from utils import software_update as swu
from test_settings_routes import (   # noqa: F401 (fixtures)
    CapturingTimer, clear_timers, nm, mgr, build_client, GIT_INFO,
)


@pytest.mark.unit
class TestTemporaryJoinRoutes:
    def test_temporary_join_202_shape(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path)
        response = client.post('/api/settings/network/join',
                               json={'ssid': 'PhoneAP', 'password': 'phonepass1',
                                     'temporary': True, 'wait_s': 999})
        assert response.status_code == 202
        data = response.get_json()
        assert data['temporary'] is True
        assert data['wait_s'] == ns.TEMP_WAIT_MAX_S
        assert data['ttl_s'] == ns.TEMP_TTL_S
        assert data['con_name'] == ns.TEMP_CON_NAME
        assert data['switch_delay_s'] == ns.SWITCH_DELAY_S
        assert mgr.temporary is True

    def test_ordinary_join_body_unchanged(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path)
        response = client.post('/api/settings/network/join',
                               json={'ssid': 'HomeNet', 'password': 'secret123'})
        assert response.status_code == 202
        data = response.get_json()
        assert 'temporary' not in data
        assert set(data) == {'success', 'state', 'switch_delay_s', 'verify'}

    def test_temporary_join_needs_hotspot_mode(self, mgr, nm, tmp_path):
        nm.status.return_value = {'mode': 'client', 'ssid': 'FarmNet',
                                  'ip': '192.168.1.50'}
        client = build_client(mgr, tmp_path)
        response = client.post('/api/settings/network/join',
                               json={'ssid': 'PhoneAP', 'password': 'phonepass1',
                                     'temporary': True})
        assert response.status_code == 409
        assert 'own hotspot' in response.get_json()['error']

    def test_revert_409_when_nothing_to_revert(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path)
        response = client.post('/api/settings/network/revert', json={})
        assert response.status_code == 409

    def test_revert_202_cancels_a_pending_temporary_join(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path)
        client.post('/api/settings/network/join',
                    json={'ssid': 'PhoneAP', 'password': 'phonepass1',
                          'temporary': True})
        response = client.post('/api/settings/network/revert', json={})
        assert response.status_code == 202
        data = response.get_json()
        assert data['state'] == 'cancelled'
        assert data['delay_s'] == ns.TEMP_REVERT_DELAY_S
        assert mgr.state == 'idle'

    def test_status_switch_carries_temporary_fields(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path)
        data = client.get('/api/settings/network/status').get_json()
        assert data['switch']['temporary'] is False
        assert 'ended_reason' in data['switch']


@pytest.mark.unit
class TestInternetRoute:
    def test_reports_probe_result(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path, internet={
            'reachable': False, 'has_route': True, 'can_ping': False,
            'default_dev': 'eth0', 'checked_at': 1,
            'error': 'No internet connectivity'})
        data = client.get('/api/settings/internet').get_json()
        assert data['success'] is True
        assert data['internet']['reachable'] is False
        assert data['internet']['default_dev'] == 'eth0'


NO_INTERNET = {'reachable': False, 'has_route': False, 'can_ping': False,
               'default_dev': None, 'checked_at': 1, 'error': 'No default route'}


@pytest.mark.unit
class TestUpdateStart:
    def _post(self, client, payload=None):
        return client.post('/api/settings/update/start', json=payload or {})

    def test_bad_ref_400(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path)
        response = self._post(client, {'ref': '../evil'})
        assert response.status_code == 400
        assert response.get_json()['code'] == 'invalid_ref'

    def test_in_progress_409_when_status_fresh(self, mgr, tmp_path):
        (tmp_path / '.update_status.json').write_text(json.dumps(
            {'request_id': 'old', 'status': 'fetching',
             'updated_at': int(time.time())}))
        client = build_client(mgr, tmp_path)
        response = self._post(client)
        assert response.status_code == 409
        assert response.get_json()['code'] == 'in_progress'

    def test_stale_status_does_not_block(self, mgr, tmp_path):
        (tmp_path / '.update_status.json').write_text(json.dumps(
            {'request_id': 'old', 'status': 'fetching',
             'updated_at': int(time.time()) - swu.STALE_AFTER_S - 5}))
        client = build_client(mgr, tmp_path)
        assert self._post(client).status_code == 202

    def test_data_transfer_busy_409(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path,
                              update_state={'data_transfer': {'status': 'zipping'}})
        response = self._post(client)
        assert response.status_code == 409
        assert response.get_json()['code'] == 'data_transfer_busy'

    def test_no_internet_409(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path, internet=NO_INTERNET)
        response = self._post(client)
        assert response.status_code == 409
        assert response.get_json()['code'] == 'no_internet'

    def test_sudo_denied_503(self, mgr, tmp_path):
        denied = MagicMock(return_value=MagicMock(returncode=1, stdout='', stderr=''))
        popen = MagicMock()
        client = build_client(mgr, tmp_path, runner=denied, popen=popen)
        response = self._post(client)
        assert response.status_code == 503
        assert response.get_json()['code'] == 'permissions_missing'
        popen.assert_not_called()
        assert not (tmp_path / '.update_status.json').exists()

    def test_launch_uses_shared_argv_and_seeds_status(self, mgr, tmp_path):
        popen = MagicMock()
        client = build_client(mgr, tmp_path, popen=popen)
        response = self._post(client)
        assert response.status_code == 202
        data = response.get_json()
        assert data['status'] == 'starting'
        assert data['ref'] == 'main'          # the unit's branch by default
        request_id = data['request_id']
        popen.assert_called_once()
        assert popen.call_args[0][0] == swu.update_argv(
            'main', request_id, status_file=tmp_path / '.update_status.json')
        seeded = swu.read_status(tmp_path / '.update_status.json')
        assert seeded['request_id'] == request_id
        assert seeded['status'] == 'starting'

    def test_explicit_ref_is_used(self, mgr, tmp_path):
        popen = MagicMock()
        client = build_client(mgr, tmp_path, popen=popen)
        data = self._post(client, {'ref': 'v3.15.0'}).get_json()
        assert data['ref'] == 'v3.15.0'
        argv = popen.call_args[0][0]
        assert argv[argv.index('--ref') + 1] == 'v3.15.0'


@pytest.mark.unit
class TestUpdateStatus:
    def _write(self, tmp_path, status, updated_at):
        (tmp_path / '.update_status.json').write_text(json.dumps(
            {'request_id': 'req-1', 'status': status, 'ref': 'main',
             'updated_at': updated_at}))

    def test_idle_without_file_and_version_fields(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path)
        data = client.get('/api/settings/update/status').get_json()
        assert data['update']['status'] == 'idle'
        assert data['git'] == GIT_INFO
        assert data['dashboard_restart_pending'] is False
        assert 'version' in data

    def test_fresh_complete_restarts_dashboard_once(self, mgr, tmp_path):
        now = int(time.time())
        self._write(tmp_path, 'complete', now + 5)
        runner = MagicMock(return_value=MagicMock(returncode=0, stdout='', stderr=''))
        client = build_client(mgr, tmp_path, runner=runner,
                              process_started_at=now - 100)
        data = client.get('/api/settings/update/status').get_json()
        assert data['update']['status'] == 'complete'
        assert data['dashboard_restart_pending'] is True
        assert len(CapturingTimer.pending) == 1
        CapturingTimer.fire_all()
        argv = runner.call_args[0][0]
        assert argv[:2] == ['/usr/bin/sudo', '-n']
        assert argv[-2:] == ['restart', 'owl-dash.service']
        # Second read: still pending, no second timer
        data = client.get('/api/settings/update/status').get_json()
        assert data['dashboard_restart_pending'] is True
        assert CapturingTimer.pending == []

    def test_old_complete_does_not_restart(self, mgr, tmp_path):
        now = int(time.time())
        self._write(tmp_path, 'complete', now - 500)
        client = build_client(mgr, tmp_path, process_started_at=now - 10)
        data = client.get('/api/settings/update/status').get_json()
        assert data['dashboard_restart_pending'] is False
        assert CapturingTimer.pending == []

    def test_rolled_back_never_restarts(self, mgr, tmp_path):
        now = int(time.time())
        self._write(tmp_path, 'rolled_back', now + 5)
        client = build_client(mgr, tmp_path, process_started_at=now - 100)
        data = client.get('/api/settings/update/status').get_json()
        assert data['dashboard_restart_pending'] is False
        assert CapturingTimer.pending == []

    def test_restart_unavailable_without_sudo_rule(self, mgr, tmp_path):
        now = int(time.time())
        self._write(tmp_path, 'complete', now + 5)
        denied = MagicMock(return_value=MagicMock(returncode=1, stdout='', stderr=''))
        client = build_client(mgr, tmp_path, runner=denied,
                              process_started_at=now - 100)
        data = client.get('/api/settings/update/status').get_json()
        assert data['dashboard_restart_pending'] is False
        assert data['warning'] == 'dashboard_restart_unavailable'
        assert CapturingTimer.pending == []
