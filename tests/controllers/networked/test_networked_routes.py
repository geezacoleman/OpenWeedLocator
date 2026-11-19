"""Networked controller Flask routes.

Routes that command the OWLs (spray, recording, restart, shutdown, config)
run against a REAL CentralController with only the broker client replaced,
and assert the exact MQTT messages and the stored desired state. Read-only
and fleet-roster routes use the mocked-controller client.
"""

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def rig3(make_controller, client_for):
    """(client, ctrl): owl-1 and owl-2 connected, owl-3 offline."""
    ctrl = make_controller(owls=('owl-1', 'owl-2', 'owl-3'))
    ctrl.owls_state['owl-3']['connected'] = False
    return client_for(ctrl), ctrl


class TestGetDeviceConfig:
    """GET /api/config/<device_id>: ask the OWL, wait for its answer."""

    def test_asks_the_owl_and_returns_what_it_answers(self, rig3, hears, sent):
        client, ctrl = rig3
        ctrl.owls_state['owl-1']['config'] = {'config': {'stale': {}}}   # from an earlier request
        answer = {'config': {'GreenOnBrown': {'exg_min': '25'}}, 'config_name': 'DAY.ini'}

        def owl_answers(topic, payload, *a, **kw):
            if json.loads(payload) == {'action': 'get_config'}:
                hears(ctrl, 'owl-1', answer, kind='config')
            return MagicMock(rc=0)

        ctrl.mqtt_client.publish.side_effect = owl_answers
        resp = client.get('/api/config/owl-1')

        assert resp.status_code == 200
        assert resp.get_json() == {'success': True, **answer}
        assert sent(ctrl, 'owl-1') == [{'action': 'get_config'}]
        assert sent(ctrl, 'owl-2') == []

    def test_504_when_the_owl_never_answers(self, rig3, monkeypatch, sent):
        import controller.networked.networked as net_mod
        client, ctrl = rig3
        waited = [0.0]

        class Clock:
            def __getattr__(self, name):
                return getattr(time, name)

            def time(self):
                return time.time() + waited[0]

            def sleep(self, seconds):
                waited[0] += seconds

        monkeypatch.setattr(net_mod, 'time', Clock())
        resp = client.get('/api/config/owl-1')

        assert resp.status_code == 504
        assert resp.get_json()['success'] is False
        assert 2.9 <= waited[0] <= 3.2, 'gives the OWL 3 s'
        assert sent(ctrl, 'owl-1') == [{'action': 'get_config'}]


class TestPushDeviceConfig:
    """POST /api/config/<device_id>."""

    def test_sends_the_section_to_that_owl_only(self, rig3, sent):
        client, ctrl = rig3
        resp = client.post('/api/config/owl-1',
                           json={'section': 'GreenOnBrown', 'params': {'exg_min': '30'}})
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True
        assert sent(ctrl) == [('owl/owl-1/commands', {
            'action': 'set_config_section', 'section': 'GreenOnBrown',
            'params': {'exg_min': '30'}})]

    def test_a_relay_pin_the_owls_gps_uses_is_refused_and_not_sent(self, rig3, sent):
        client, ctrl = rig3
        ctrl.owls_state['owl-1']['protected_pins'] = {'14': 'GPS serial (TX)'}
        resp = client.post('/api/config/owl-1',
                           json={'section': 'Relays', 'params': {'0': '14'}})
        assert resp.status_code == 400
        assert 'GPS' in resp.get_json()['error']
        assert sent(ctrl) == []

    @pytest.mark.parametrize('body', [{'section': 'GreenOnBrown'}, {'params': {'exg_min': '1'}}, {}])
    def test_400_and_nothing_sent_without_section_and_params(self, rig3, sent, body):
        client, ctrl = rig3
        resp = client.post('/api/config/owl-1', json=body)
        assert resp.status_code == 400
        assert sent(ctrl) == []


class TestSaveDeviceConfig:
    """POST /api/config/<device_id>/save."""

    def test_tells_the_owl_to_save_under_the_name_given(self, rig3, sent):
        client, ctrl = rig3
        resp = client.post('/api/config/owl-2/save',
                           json={'filename': 'north_block.ini', 'name': 'North block', 'notes': 'wet'})
        assert resp.get_json()['success'] is True
        assert sent(ctrl) == [('owl/owl-2/commands', {
            'action': 'save_config', 'filename': 'north_block.ini',
            'name': 'North block', 'notes': 'wet'})]


class TestApiOwlsCloud:
    """/api/owls carries the fleet-level cloud (Noktura) link."""

    def test_cloud_fields_come_from_the_cloud_section(self, make_controller, client_for, hears):
        ctrl = make_controller('[Cloud]\nenable = True\ndevice_id = east-farm\n'
                               'portal_url = https://app.noktura.tech/\n')
        client = client_for(ctrl)
        data = client.get('/api/owls').get_json()
        assert (data['cloud_enabled'], data['cloud_connected'], data['cloud_device_id'],
                data['cloud_portal_url']) == (True, None, 'east-farm', 'https://app.noktura.tech')
        assert list(data['owls']) == ['owl-1']

        ctrl._on_message(None, None, MagicMock(
            topic='$SYS/broker/connection/owl-bridge-east-farm/state', payload=b'1'))
        assert client.get('/api/owls').get_json()['cloud_connected'] is True

    def test_not_configured(self, make_controller, client_for):
        data = client_for(make_controller()).get('/api/owls').get_json()
        assert (data['cloud_enabled'], data['cloud_connected'], data['cloud_device_id']) == (
            False, None, '')


class TestPresets:
    """GET /api/presets and /api/presets/<name>: the config/ folder of the
    cab unit, read by the real controller."""

    @pytest.fixture
    def client(self, make_controller, client_for, owl_home):
        config = owl_home / 'config'
        (config / 'GENERAL_CONFIG.ini').write_text('[GreenOnBrown]\nexg_min = 25\n')
        (config / 'CUSTOM.ini').write_text('[GreenOnBrown]\nexg_min = 40\n\n[System]\nrelay_num = 2\n')
        (config / 'GEOMETRY.ini').write_text('[Camera]\ncrop_left = 0\n')
        (config / 'DAY_SENSITIVITY_TEMPLATE.ini').write_text('[GreenOnBrown]\n')
        (config / 'notes.txt').write_text('not a preset')
        return client_for(make_controller())

    def test_lists_selectable_presets_only(self, client, owl_home):
        data = client.get('/api/presets').get_json()
        assert data['success'] is True
        assert [(p['name'], p['filename'], p['is_default']) for p in data['presets']] == [
            ('CUSTOM', 'CUSTOM.ini', False), ('GENERAL_CONFIG', 'GENERAL_CONFIG.ini', True)]

    def test_returns_the_file_as_sections(self, client):
        data = client.get('/api/presets/CUSTOM').get_json()
        assert data['success'] is True
        assert data['config'] == {'GreenOnBrown': {'exg_min': '40'}, 'System': {'relay_num': '2'}}
        assert data['config_name'] == 'CUSTOM.ini'

    def test_404_for_a_preset_that_does_not_exist(self, client):
        resp = client.get('/api/presets/NONEXISTENT')
        assert resp.status_code == 404
        assert resp.get_json()['success'] is False


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


class TestPushPreset:
    """POST /api/presets/push/<device_id>."""

    def test_every_section_of_the_preset_goes_to_that_owl(self, rig3, sent, owl_home):
        import configparser
        client, ctrl = rig3
        preset = owl_home / 'config' / 'FIELD_TEST.ini'
        preset.write_text('[GreenOnBrown]\nexg_min = 31\nhue_max = 77\n\n[System]\nrelay_num = 4\n')

        resp = client.post('/api/presets/push/owl-1', json={'preset': 'FIELD_TEST'})

        assert resp.status_code == 200
        assert resp.get_json() == {
            'success': True, 'message': 'Pushed 2 sections from FIELD_TEST.ini to owl-1'}
        assert sent(ctrl) == [
            ('owl/owl-1/commands', {'action': 'set_config_section', 'section': 'GreenOnBrown',
                                    'params': {'exg_min': '31', 'hue_max': '77'}}),
            ('owl/owl-1/commands', {'action': 'set_config_section', 'section': 'System',
                                    'params': {'relay_num': '4'}})]

    def test_404_and_nothing_sent_for_a_preset_that_does_not_exist(self, rig3, sent):
        client, ctrl = rig3
        resp = client.post('/api/presets/push/owl-1', json={'preset': 'NONEXISTENT'})
        assert resp.status_code == 404
        assert resp.get_json()['success'] is False
        assert sent(ctrl) == []

    def test_a_preset_name_cannot_reach_outside_the_config_folder(self, rig3, sent, owl_home):
        client, ctrl = rig3
        (owl_home / 'secret.ini').write_text('[MQTT]\nbroker_ip = 10.9.9.9\n')
        resp = client.post('/api/presets/push/owl-1', json={'preset': '../secret'})
        assert resp.status_code == 404
        assert sent(ctrl) == []

    def test_400_without_a_preset_name(self, rig3, sent):
        client, ctrl = rig3
        resp = client.post('/api/presets/push/owl-1', json={})
        assert resp.status_code == 400
        assert sent(ctrl) == []


# ===========================================================================
# Commands to the OWLs: spray, recording, restart, shutdown
# ===========================================================================

SPOT_ON = {'action': 'set_detection_enable', 'value': True}
SPOT_OFF = {'action': 'set_detection_enable', 'value': False}
BLANKET = {'action': 'set_detection_mode', 'value': 2}


class TestCommandRoute:
    """POST /api/command: what the dashboard's buttons send."""

    def _command(self, client, device_id, action, **value):
        return client.post('/api/command', json=dict({'device_id': device_id, 'action': action}, **value))

    def test_start_all_reaches_every_connected_owl_and_is_remembered(self, rig3, sent):
        client, ctrl = rig3
        resp = self._command(client, 'all', 'toggle_detection', value=True)
        assert resp.status_code == 200
        assert resp.get_json() == {'success': True, 'message': 'Sent to 2 OWLs',
                                   'targets': ['owl-1', 'owl-2']}
        assert sent(ctrl) == [('owl/owl-1/commands', SPOT_ON), ('owl/owl-2/commands', SPOT_ON)]
        assert ctrl.desired_state == {'all': {'spray': 'spot'}}

    def test_stop_all_overrules_one_owls_own_blanket(self, rig3, sent, hears):
        client, ctrl = rig3
        self._command(client, 'owl-2', 'toggle_all_nozzles', value=True)
        assert sent(ctrl) == [('owl/owl-2/commands', BLANKET)]
        assert ctrl.desired_state == {'owl-2': {'spray': 'blanket'}}
        ctrl.mqtt_client.publish.reset_mock()

        resp = self._command(client, 'all', 'toggle_detection', value=False)
        assert resp.get_json()['targets'] == ['owl-1', 'owl-2']
        assert sent(ctrl) == [('owl/owl-1/commands', SPOT_OFF), ('owl/owl-2/commands', SPOT_OFF)]
        assert ctrl.desired_state == {'all': {'spray': 'off'}, 'owl-2': {}}

        # owl-3 was offline for all of it: it is told to stop when it returns
        ctrl.mqtt_client.publish.reset_mock()
        hears(ctrl, 'owl-3', {'detection_mode': 2})
        assert sent(ctrl, 'owl-3') == [SPOT_OFF]

    @pytest.mark.parametrize('value, command, stored', [
        ('false', SPOT_OFF, 'off'), ('off', SPOT_OFF, 'off'), (0, SPOT_OFF, 'off'),
        ('true', SPOT_ON, 'spot'), (1, SPOT_ON, 'spot')])
    def test_on_off_words_mean_what_they_say(self, rig3, sent, value, command, stored):
        """The string "false" used to count as on and started spraying."""
        client, ctrl = rig3
        resp = self._command(client, 'owl-1', 'toggle_detection', value=value)
        assert resp.status_code == 200
        assert sent(ctrl) == [('owl/owl-1/commands', command)]
        assert ctrl.desired_state == {'owl-1': {'spray': stored}}

    @pytest.mark.parametrize('action', ['toggle_detection', 'toggle_recording', 'set_recording_paused'])
    @pytest.mark.parametrize('value', ['banana', 2, [True]])
    def test_an_unclear_value_is_refused_with_400_and_changes_nothing(self, rig3, sent, action, value):
        client, ctrl = rig3
        resp = self._command(client, 'all', action, value=value)
        assert resp.status_code == 400
        assert resp.get_json()['refused'] is True
        assert sent(ctrl) == []
        assert ctrl.desired_state == {}

    def test_recording_on_for_all_is_sent_and_remembered(self, rig3, sent):
        client, ctrl = rig3
        self._command(client, 'all', 'toggle_recording', value=True)
        message = {'action': 'set_image_sample_enable', 'value': True}
        assert sent(ctrl) == [('owl/owl-1/commands', message), ('owl/owl-2/commands', message)]
        assert ctrl.desired_state == {'all': {'image_sample_enable': True}}

    def test_hold_to_spray_is_a_bounded_lease_and_never_remembered(self, rig3, sent):
        client, ctrl = rig3
        ctrl.owls_state['owl-1']['spray'] = {'request': 'off'}       # 3.27+: has leases
        resp = self._command(client, 'all', 'hold_spray', value=60000)
        assert resp.get_json()['targets'] == ['owl-1', 'owl-2']
        assert sent(ctrl) == [
            ('owl/owl-1/commands', {'action': 'hold_spray', 'value': 1500}),
            ('owl/owl-2/commands', BLANKET)]                          # older OWL: blanket on
        assert ctrl.desired_state == {}

    def test_broker_down_is_a_500_and_nothing_is_remembered(self, rig3, sent):
        client, ctrl = rig3
        ctrl.mqtt_connected = False
        resp = self._command(client, 'all', 'toggle_detection', value=True)
        assert resp.status_code == 500
        assert resp.get_json() == {'success': False, 'error': 'MQTT not connected'}
        assert sent(ctrl) == []
        assert ctrl.desired_state == {}

    def test_a_failed_publish_is_reported(self, rig3):
        client, ctrl = rig3
        ctrl.mqtt_client.publish.return_value = MagicMock(rc=4)
        resp = self._command(client, 'owl-1', 'toggle_detection', value=True)
        assert resp.status_code == 500
        assert resp.get_json()['success'] is False

    @pytest.mark.parametrize('body', [{'action': 'toggle_detection', 'value': True},
                                      {'device_id': 'owl-1', 'value': True}])
    def test_400_and_nothing_sent_without_a_target_and_an_action(self, rig3, sent, body):
        client, ctrl = rig3
        assert client.post('/api/command', json=body).status_code == 400
        assert sent(ctrl) == []
        assert ctrl.desired_state == {}


class TestCollectRecordingRoutes:
    """The collect screen's Record / Pause / Stop, on a real controller."""

    def test_record_stops_spraying_then_labels_then_starts_recording(self, rig3, sent):
        client, ctrl = rig3
        resp = client.post('/api/collect/start', json={'metadata': {'crop': 'wheat'}})
        assert resp.status_code == 200
        run_id = resp.get_json()['run_id']
        assert run_id

        to_owl_1 = sent(ctrl, 'owl-1')
        assert [m['action'] for m in to_owl_1] == [
            'set_detection_enable', 'set_capture_params', 'set_session_metadata',
            'set_image_sample_enable']
        assert to_owl_1[0] == SPOT_OFF
        assert (to_owl_1[2]['crop'], to_owl_1[2]['run_id']) == ('wheat', run_id)
        assert to_owl_1[3] == {'action': 'set_image_sample_enable', 'value': True}
        assert sent(ctrl, 'owl-2') == to_owl_1
        assert sent(ctrl, 'owl-3') == []

        desired = ctrl.desired_state['all']
        assert (desired['spray'], desired['image_sample_enable']) == ('off', True)
        assert desired['session_metadata']['run_id'] == run_id

    def test_pause_and_resume_reach_the_owls_and_a_rejoining_owl_stays_paused(self, rig3, sent, hears):
        client, ctrl = rig3
        assert client.post('/api/collect/pause').status_code == 409, 'nothing is recording yet'
        assert sent(ctrl) == []

        client.post('/api/collect/start', json={'metadata': {'crop': 'wheat'}})
        ctrl.owls_state['owl-1']['image_sample_enable'] = True       # owl-1 reports recording
        ctrl.mqtt_client.publish.reset_mock()

        assert client.post('/api/collect/pause').get_json() == {'success': True, 'paused': True}
        paused = {'action': 'set_recording_paused', 'value': True}
        assert sent(ctrl) == [('owl/owl-1/commands', paused), ('owl/owl-2/commands', paused)]

        ctrl.mqtt_client.publish.reset_mock()
        hears(ctrl, 'owl-3', {'image_sample_enable': False})          # rejoins mid-pause
        actions = [m['action'] for m in sent(ctrl, 'owl-3')]
        assert actions[-2:] == ['set_image_sample_enable', 'set_recording_paused']
        assert sent(ctrl, 'owl-3')[-1] == paused

        ctrl.mqtt_client.publish.reset_mock()
        assert client.post('/api/collect/resume').get_json() == {'success': True, 'paused': False}
        assert sent(ctrl, 'owl-1') == [{'action': 'set_recording_paused', 'value': False}]

    def test_stop_ends_recording_and_forgets_the_run(self, rig3, sent):
        client, ctrl = rig3
        client.post('/api/collect/start', json={'metadata': {'crop': 'wheat'}})
        ctrl.mqtt_client.publish.reset_mock()

        assert client.post('/api/collect/stop').get_json() == {'success': True}
        assert sent(ctrl, 'owl-1') == [{'action': 'set_image_sample_enable', 'value': False},
                                       {'action': 'list_data_sessions', 'value': None}]
        desired = ctrl.desired_state['all']
        assert desired['image_sample_enable'] is False
        assert 'session_metadata' not in desired
        assert desired['spray'] == 'off', 'stopping a recording never starts spraying'


class TestRestartRoute:
    """POST /api/owl/<device_id>/restart."""

    def test_restarts_that_owl_only(self, rig3, sent):
        client, ctrl = rig3
        resp = client.post('/api/owl/owl-2/restart')
        assert resp.get_json() == {'success': True, 'message': 'Sent to owl-2'}
        assert sent(ctrl) == [('owl/owl-2/commands', {'action': 'restart_service'})]


class TestSetActiveConfigRoute:
    """POST /api/config/<device_id>/set-active."""

    def test_names_the_config_the_owl_boots_with(self, rig3, sent):
        client, ctrl = rig3
        resp = client.post('/api/config/owl-1/set-active', json={'config': 'config/DAY.ini'})
        assert resp.get_json()['success'] is True
        assert sent(ctrl) == [('owl/owl-1/commands',
                               {'action': 'set_active_config', 'config': 'config/DAY.ini'})]

    def test_400_and_nothing_sent_without_a_config(self, rig3, sent):
        client, ctrl = rig3
        assert client.post('/api/config/owl-1/set-active', json={}).status_code == 400
        assert sent(ctrl) == []


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

class TestSystemShutdown:
    """POST /api/system/shutdown: every OWL is told, then the cab unit goes."""

    def test_tells_every_connected_owl_then_schedules_its_own_shutdown(self, rig3, sent):
        client, ctrl = rig3
        with patch('controller.networked.networked.threading') as mock_threading:
            with patch('controller.networked.networked.subprocess') as mock_subprocess:
                resp = client.post('/api/system/shutdown')

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['owls_notified'] == ['owl-1', 'owl-2']
        assert sent(ctrl) == [('owl/owl-1/commands', {'action': 'shutdown'}),
                              ('owl/owl-2/commands', {'action': 'shutdown'})]
        mock_threading.Thread.assert_called_once()
        mock_threading.Thread.return_value.start.assert_called_once()
        mock_subprocess.run.assert_not_called()      # nothing is run inside the request itself

    def test_still_shuts_the_cab_unit_down_with_no_owls(self, make_controller, client_for, sent):
        ctrl = make_controller(owls=())
        with patch('controller.networked.networked.threading') as mock_threading:
            with patch('controller.networked.networked.subprocess'):
                resp = client_for(ctrl).post('/api/system/shutdown')
        assert resp.get_json()['owls_notified'] == []
        assert sent(ctrl) == []
        mock_threading.Thread.return_value.start.assert_called_once()


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
