"""Tests for fleet software-update orchestration in the networked controller.

The orchestrator updates devices SEQUENTIALLY and aborts the remaining queue
on the first non-complete result — a bad ref must never roll across the
whole fleet.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest


def _make_controller(tmp_path, monkeypatch):
    """Real CentralController with a mocked MQTT client and no real sleeps."""
    from controller.networked.networked import CentralController

    ctrl = CentralController(config_file=str(tmp_path / 'missing.ini'))
    ctrl.mqtt_connected = True
    ctrl.mqtt_client = MagicMock()
    ctrl.mqtt_client.publish.return_value = MagicMock(rc=0)  # MQTT_ERR_SUCCESS
    ctrl.owls_state = {
        'owl-1': {'connected': True},
        'owl-2': {'connected': True},
        'owl-3': {'connected': False},
    }
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    return ctrl


def _auto_complete(ctrl, statuses=None):
    """Make publish() immediately report a terminal status for the device."""
    statuses = statuses or {}

    def fake_publish(topic, payload):
        device_id = topic.split('/')[1]
        cmd = json.loads(payload)
        ctrl.owls_state.setdefault(device_id, {})['software_update'] = {
            'request_id': cmd['request_id'],
            'status': statuses.get(device_id, 'complete'),
        }
        return MagicMock(rc=0)

    ctrl.mqtt_client.publish.side_effect = fake_publish


@pytest.mark.unit
class TestStartFleetUpdateValidation:

    def test_invalid_ref_rejected(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        for bad in ('../x', 'a..b', 'https://evil', '', '-rf', 'has space'):
            result = ctrl.start_fleet_update('all', bad)
            assert result['success'] is False, f"ref {bad!r} should be rejected"
        assert not ctrl.fleet_update['active']

    def test_disconnected_device_rejected(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        result = ctrl.start_fleet_update(['owl-3'], 'main')
        assert result['success'] is False
        assert 'owl-3' in result['error']

    def test_unknown_device_rejected(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        result = ctrl.start_fleet_update(['owl-99'], 'main')
        assert result['success'] is False

    def test_mqtt_disconnected_rejected(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        ctrl.mqtt_connected = False
        result = ctrl.start_fleet_update('all', 'main')
        assert result['success'] is False

    def test_refused_while_active(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        ctrl.fleet_update['active'] = True
        result = ctrl.start_fleet_update('all', 'main')
        assert result['success'] is False
        assert 'in progress' in result['error']

    def test_all_resolves_to_connected_devices(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        _auto_complete(ctrl)
        result = ctrl.start_fleet_update('all', 'main')
        assert result['success'] is True
        assert result['queued'] == ['owl-1', 'owl-2']  # owl-3 disconnected
        ctrl._fleet_update_thread.join(timeout=10)


@pytest.mark.unit
class TestFleetUpdateWorker:

    def test_sequential_all_complete(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        order = []
        orig_update = ctrl._update_one_device

        def tracking_update(device_id, ref):
            order.append(device_id)
            return orig_update(device_id, ref)

        ctrl._update_one_device = tracking_update
        _auto_complete(ctrl)

        result = ctrl.start_fleet_update(['owl-1', 'owl-2'], 'main')
        assert result['success'] is True
        ctrl._fleet_update_thread.join(timeout=10)

        assert order == ['owl-1', 'owl-2']
        status = ctrl.get_fleet_update_status()
        assert status['active'] is False
        assert status['results'] == {'owl-1': 'complete', 'owl-2': 'complete'}
        assert status['error'] == ''

    def test_abort_on_first_failure(self, tmp_path, monkeypatch):
        """owl-1 rolls back -> owl-2 must NOT be updated."""
        ctrl = _make_controller(tmp_path, monkeypatch)
        _auto_complete(ctrl, statuses={'owl-1': 'rolled_back'})

        result = ctrl.start_fleet_update(['owl-1', 'owl-2'], 'main')
        assert result['success'] is True
        ctrl._fleet_update_thread.join(timeout=10)

        status = ctrl.get_fleet_update_status()
        assert status['results']['owl-1'] == 'rolled_back'
        assert status['results']['owl-2'] == 'aborted'
        assert 'owl-1' in status['error']
        # owl-2 never received an update command
        sent_to = [c.args[0] for c in ctrl.mqtt_client.publish.call_args_list
                   if hasattr(c, 'args') and c.args]
        assert not any('owl-2' in t for t in sent_to)

    def test_timeout_marks_device_and_aborts(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        ctrl.FLEET_UPDATE_DEVICE_TIMEOUT = 0.01  # publish never reports back

        result = ctrl.start_fleet_update(['owl-1', 'owl-2'], 'main')
        assert result['success'] is True
        ctrl._fleet_update_thread.join(timeout=10)

        status = ctrl.get_fleet_update_status()
        assert status['results']['owl-1'] == 'timeout'
        assert status['results']['owl-2'] == 'aborted'

    def test_stale_request_id_ignored(self, tmp_path, monkeypatch):
        """A terminal status from a PREVIOUS update must not satisfy this one."""
        ctrl = _make_controller(tmp_path, monkeypatch)
        ctrl.FLEET_UPDATE_DEVICE_TIMEOUT = 0.01
        # Device reports a stale terminal status with a different request_id
        ctrl.owls_state['owl-1']['software_update'] = {
            'request_id': 'old-request', 'status': 'complete'
        }

        assert ctrl._update_one_device('owl-1', 'main') == 'timeout'

    def test_include_controller_runs_last_after_success(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        _auto_complete(ctrl)
        ctrl._launch_controller_self_update = MagicMock()

        ctrl.start_fleet_update(['owl-1', 'owl-2'], 'main', include_controller=True)
        ctrl._fleet_update_thread.join(timeout=10)

        ctrl._launch_controller_self_update.assert_called_once_with('main')

    def test_include_controller_skipped_on_abort(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        _auto_complete(ctrl, statuses={'owl-1': 'error'})
        ctrl._launch_controller_self_update = MagicMock()

        ctrl.start_fleet_update(['owl-1', 'owl-2'], 'main', include_controller=True)
        ctrl._fleet_update_thread.join(timeout=10)

        ctrl._launch_controller_self_update.assert_not_called()

    def test_controller_self_update_argv(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        with patch('controller.networked.networked.subprocess.Popen') as mock_popen:
            ctrl._launch_controller_self_update('main')
        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        assert argv[:6] == ['sudo', '-n', '/usr/bin/systemd-run',
                            '--unit=owl-update', '--collect',
                            '--property=RuntimeMaxSec=1800']
        assert '--profile' in argv and argv[argv.index('--profile') + 1] == 'controller'
        assert '--ref' in argv and argv[argv.index('--ref') + 1] == 'main'


@pytest.mark.unit
class TestFleetUpdateRoutes:

    def test_post_update_calls_controller(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        mock_ctrl.start_fleet_update.return_value = {'success': True, 'queued': ['owl-1'], 'ref': 'main'}

        resp = client.post('/api/update', json={'device_ids': ['owl-1'], 'ref': 'main'})

        assert resp.status_code == 200
        mock_ctrl.start_fleet_update.assert_called_once_with(['owl-1'], 'main', False)

    def test_post_update_defaults(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        mock_ctrl.start_fleet_update.return_value = {'success': True, 'queued': [], 'ref': 'main'}

        resp = client.post('/api/update', json={})

        assert resp.status_code == 200
        mock_ctrl.start_fleet_update.assert_called_once_with('all', 'main', False)

    def test_post_update_bad_device_ids_type(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        resp = client.post('/api/update', json={'device_ids': 'owl-1'})
        assert resp.status_code == 400
        mock_ctrl.start_fleet_update.assert_not_called()

    def test_post_update_failure_is_400(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        mock_ctrl.start_fleet_update.return_value = {'success': False, 'error': 'invalid ref'}
        resp = client.post('/api/update', json={'ref': '../bad'})
        assert resp.status_code == 400

    def test_get_update_status(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        mock_ctrl.get_fleet_update_status.return_value = {'active': False, 'results': {}}
        mock_ctrl.owls_state = {
            'owl-1': {'connected': True, 'version': '3.0.0', 'git_branch': 'main',
                      'git_commit': 'abc1234', 'software_update': {'status': 'idle'}},
        }

        resp = client.get('/api/update/status')

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['fleet_update'] == {'active': False, 'results': {}}
        assert data['devices']['owl-1']['git_commit'] == 'abc1234'
