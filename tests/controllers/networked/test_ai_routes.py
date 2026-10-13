"""Networked side of honest AI state: the same /api/algorithm/set refusal
rule as standalone (shared utils/ai_capability.algorithm_refusal), and
/api/ai/install as a fleet update with with_ai."""

import json
import time
from unittest.mock import MagicMock

import pytest

NO_AI = {'cpu': {'installed': False},
         'hailo': {'present': False, 'chip': None, 'runtime': False},
         'accelerator': None}
CPU_AI = dict(NO_AI, cpu={'installed': True}, accelerator='cpu')


def _make_controller(tmp_path, monkeypatch, owls):
    from controller.networked.networked import CentralController

    ctrl = CentralController(config_file=str(tmp_path / 'missing.ini'))
    ctrl.mqtt_connected = True
    ctrl.mqtt_client = MagicMock()
    ctrl.mqtt_client.publish.return_value = MagicMock(rc=0)
    ctrl.owls_state = owls
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    return ctrl


@pytest.mark.unit
class TestAlgorithmRefusal:
    def test_ai_refused_when_any_connected_owl_has_no_ai(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch, {
            'owl-1': {'connected': True, 'ai': CPU_AI},
            'owl-2': {'connected': True, 'ai': NO_AI},
        })
        result = ctrl.send_command('all', 'set_algorithm', 'gog')
        assert result['success'] is False
        assert result['refused'] is True
        assert result['error'].startswith('owl-2: ')
        ctrl.mqtt_client.publish.assert_not_called()

    def test_ai_sent_when_every_owl_can_run_it(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch, {
            'owl-1': {'connected': True, 'ai': CPU_AI},
            'owl-2': {'connected': False, 'ai': NO_AI},  # offline: not a target
        })
        assert ctrl.send_command('all', 'set_algorithm', 'gog-hybrid')['success'] is True
        topic, payload = ctrl.mqtt_client.publish.call_args[0]
        assert topic == 'owl/owl-1/commands'
        assert json.loads(payload) == {'action': 'set_algorithm', 'value': 'gog-hybrid'}

    def test_single_owl_reason_is_plain(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch, {
            'owl-1': {'connected': True, 'ai': NO_AI}})
        result = ctrl.send_command('owl-1', 'set_algorithm', 'gog')
        assert result['error'] == 'AI is not installed on this OWL. Install AI, then try again.'

    def test_colour_modes_never_refused(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch, {
            'owl-1': {'connected': True, 'ai': NO_AI}})
        assert ctrl.send_command('all', 'set_algorithm', 'exhsv')['success'] is True


@pytest.mark.unit
class TestRoutes:
    def test_algorithm_set_refusal_is_400(self, networked_test_client):
        client, ctrl = networked_test_client
        ctrl.send_command.return_value = {'success': False, 'refused': True,
                                          'error': 'owl-2: AI is not installed'}
        resp = client.post('/api/algorithm/set', json={'algorithm': 'gog'})
        assert resp.status_code == 400
        assert resp.get_json()['error'] == 'owl-2: AI is not installed'
        ctrl.send_command.assert_called_once_with('all', 'set_algorithm', 'gog')

    def test_algorithm_set_accepts_lut_and_rejects_junk(self, networked_test_client):
        client, ctrl = networked_test_client
        assert client.post('/api/algorithm/set',
                           json={'algorithm': 'lut', 'device_id': 'owl-1'}).status_code == 200
        ctrl.send_command.assert_called_once_with('owl-1', 'set_algorithm', 'lut')
        assert client.post('/api/algorithm/set', json={'algorithm': 'maxg'}).status_code == 400

    def test_command_route_refusal_is_400(self, networked_test_client):
        client, ctrl = networked_test_client
        ctrl.send_command.return_value = {'success': False, 'refused': True, 'error': 'x'}
        resp = client.post('/api/command', json={'device_id': 'all',
                                                 'action': 'set_algorithm', 'value': 'gog'})
        assert resp.status_code == 400

    def test_ai_install_uses_the_owls_branch(self, networked_test_client):
        client, ctrl = networked_test_client
        ctrl.owls_state = {'owl-1': {'connected': True, 'git_branch': 'field-3.20'},
                           'owl-2': {'connected': True, 'git_branch': 'field-3.20'}}
        ctrl.start_fleet_update.return_value = {'success': True, 'queued': ['owl-1', 'owl-2'],
                                                'ref': 'field-3.20', 'with_ai': True}
        resp = client.post('/api/ai/install', json={})
        assert resp.status_code == 202
        ctrl.start_fleet_update.assert_called_once_with('all', 'field-3.20', with_ai=True)

    def test_ai_install_refuses_mixed_branches(self, networked_test_client):
        client, ctrl = networked_test_client
        ctrl.owls_state = {'owl-1': {'connected': True, 'git_branch': 'main'},
                           'owl-2': {'connected': True, 'git_branch': 'field-3.20'}}
        resp = client.post('/api/ai/install', json={})
        assert resp.status_code == 400
        ctrl.start_fleet_update.assert_not_called()
        # Choosing the OWL (or a ref) resolves it
        ctrl.start_fleet_update.return_value = {'success': True}
        client.post('/api/ai/install', json={'device_ids': ['owl-1']})
        ctrl.start_fleet_update.assert_called_once_with(['owl-1'], 'main', with_ai=True)


@pytest.mark.unit
class TestFleetInstall:
    def test_with_ai_reaches_the_owl_command(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch, {'owl-1': {'connected': True}})
        sent = []

        def publish(topic, payload):
            cmd = json.loads(payload)
            sent.append(cmd)
            ctrl.owls_state['owl-1']['software_update'] = {
                'request_id': cmd['request_id'], 'status': 'complete'}
            return MagicMock(rc=0)

        ctrl.mqtt_client.publish.side_effect = publish
        result = ctrl.start_fleet_update(['owl-1'], 'main', with_ai=True)
        assert result['with_ai'] is True
        ctrl._fleet_update_thread.join(timeout=10)
        assert sent[0]['action'] == 'update_software'
        assert sent[0]['with_ai'] is True
        assert ctrl.fleet_update['results'] == {'owl-1': 'complete'}

    def test_plain_update_sends_no_with_ai(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch, {'owl-1': {'connected': True}})
        sent = []

        def publish(topic, payload):
            cmd = json.loads(payload)
            sent.append(cmd)
            ctrl.owls_state['owl-1']['software_update'] = {
                'request_id': cmd['request_id'], 'status': 'complete'}
            return MagicMock(rc=0)

        ctrl.mqtt_client.publish.side_effect = publish
        ctrl.start_fleet_update(['owl-1'], 'main')
        ctrl._fleet_update_thread.join(timeout=10)
        assert 'with_ai' not in sent[0]
