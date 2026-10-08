"""Collect screen controller fixes (2026-10-08 review): rejoin labels,
not-recording / not-saving warnings, start/stop failures, remembered
record choices, kiosk links, rotation restart and upload retry."""

import configparser
import json
import re
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from controller.networked.collect import (CollectSession, collect_warnings, expected_interval_s,
                                          NOT_RECORDING_GRACE_S)

REPO = Path(__file__).resolve().parents[1]
JS = (REPO / 'controller/networked/static/js/collect/collect.js').read_text(encoding='utf-8')
I18N = (REPO / 'controller/networked/static/js/collect/i18n.js').read_text(encoding='utf-8')
HTML = (REPO / 'controller/networked/templates/collect.html').read_text(encoding='utf-8')


@pytest.fixture
def client_ctx(networked_test_client, tmp_path):
    client, ctrl = networked_test_client
    ctrl.collect = CollectSession()
    ctrl.config = configparser.ConfigParser()
    ctrl.config_file = tmp_path / 'CONTROLLER.ini'
    ctrl.config_file.write_text('[MQTT]\nbroker_ip = localhost\n\n[Collect]\n# keep me\nlanguage = en\n')
    ctrl.cloud_enable = True
    ctrl.cloud_connected = True
    ctrl.cloud_device_id = 'rig-1'
    ctrl.cloud_portal_url = ''
    ctrl.gps_manager = None
    ctrl.ui_mode = 'collect'
    ctrl.desired_state = {}
    ctrl.mqtt_lock = threading.Lock()
    ctrl.owls_state = {'owl-1': {'connected': True, 'image_sample_enable': False,
                                 'config': {'config': {'Camera': {'rotation': 'auto'}}},
                                 'data_sessions': [{'session_id': '20261008/session_090000',
                                                    'total_size': 100}]}}
    return client, ctrl


@pytest.fixture
def push_ctrl():
    with patch('controller.networked.networked.CentralController.__init__', lambda self, **kw: None):
        from controller.networked.networked import CentralController
        ctrl = CentralController.__new__(CentralController)
        ctrl.desired_state = {}
        ctrl.mqtt_client = MagicMock()
        return ctrl


def _pushed(ctrl):
    return [json.loads(c.args[1]) for c in ctrl.mqtt_client.publish.call_args_list]


# 1. Rejoining OWL keeps the run's labels -----------------------------------

@pytest.mark.unit
class TestRejoinLabels:
    def test_start_stores_and_stop_clears_metadata(self, client_ctx):
        client, ctrl = client_ctx
        run_id = client.post('/api/collect/start', json={'metadata': {'crop': 'barley'}}).get_json()['run_id']
        md = ctrl.desired_state['all']['session_metadata']
        assert md['crop'] == 'barley' and md['run_id'] == run_id
        client.post('/api/collect/stop', json={})
        assert 'session_metadata' not in ctrl.desired_state['all']

    def test_push_sends_labels_and_capture_before_recording(self, push_ctrl):
        push_ctrl.desired_state = {'all': {'detection_enable': False, 'image_sample_enable': True,
                                           'capture_params': {'sample_mode': 'distance'},
                                           'session_metadata': {'crop': 'rye', 'run_id': 'abc'}}}
        push_ctrl._push_desired_state('owl-1')
        sent = _pushed(push_ctrl)
        actions = [p['action'] for p in sent]
        assert actions.index('set_capture_params') < actions.index('set_image_sample_enable')
        assert actions.index('set_session_metadata') < actions.index('set_image_sample_enable')
        md = next(p for p in sent if p['action'] == 'set_session_metadata')
        assert md['crop'] == 'rye' and md['run_id'] == 'abc'

    def test_full_mode_push_without_metadata_unchanged(self, push_ctrl):
        push_ctrl.desired_state = {'all': {'detection_enable': True, 'image_sample_enable': False}}
        push_ctrl._push_desired_state('owl-1')
        assert [p['action'] for p in _pushed(push_ctrl)] == ['set_detection_enable', 'set_image_sample_enable']


# 2. Warnings: not recording / not saving -----------------------------------

def _owl(**kw):
    s = {'connected': True, 'image_sample_enable': True,
         'capture': {'record_mode': 'images', 'speed_kmh': 7.2}}
    s.update(kw)
    return s


@pytest.mark.unit
class TestRunWarnings:
    def test_not_recording_during_run_after_grace(self):
        owls = {'owl-1': _owl(image_sample_enable=False)}
        codes = lambda now, started: [w['code'] for w in collect_warnings(owls, now, run_started_at=started)]
        assert 'not_recording' in codes(100.0, 100.0 - NOT_RECORDING_GRACE_S - 1)
        assert 'not_recording' not in codes(100.0, 99.0)          # still starting
        assert 'not_recording' not in codes(100.0, None)          # no run

    def test_not_recording_skipped_when_blocked(self):
        owls = {'owl-1': _owl(image_sample_enable=False, recording_blocked_reason='no_storage')}
        codes = [w['code'] for w in collect_warnings(owls, 100.0, run_started_at=0.0)]
        assert 'recording_blocked' in codes and 'not_recording' not in codes

    def test_never_saved_owl_counts_from_run_start(self):
        owls = {'owl-1': _owl()}
        w = collect_warnings(owls, 100.0, run_started_at=80.0)
        assert [x['seconds'] for x in w if x['code'] == 'not_saving'] == [20]
        # Without a run and without any capture there is no baseline
        assert not [x for x in collect_warnings(owls, 100.0) if x['code'] == 'not_saving']

    def test_last_capture_after_start_is_the_baseline(self):
        owls = {'owl-1': _owl(last_capture_at=95.0)}
        assert not [x for x in collect_warnings(owls, 100.0, run_started_at=50.0) if x['code'] == 'not_saving']

    def test_threshold_scales_with_spacing(self):
        # 10 m at 7.2 km/h (2 m/s) = 5 s per image -> limit 15 s
        capture = {'sample_mode': 'distance', 'sample_distance_m': 10.0, 'sample_interval_s': 2.0}
        owls = {'owl-1': _owl(last_capture_at=88.0)}
        flagged = lambda now: any(x['code'] == 'not_saving' for x in collect_warnings(
            owls, now, run_started_at=0.0, capture_settings=capture))
        assert not flagged(88.0 + 14)
        assert flagged(88.0 + 16)

    def test_expected_interval(self):
        assert expected_interval_s({'sample_mode': 'time', 'sample_interval_s': 4}, None) == 4
        assert expected_interval_s({'sample_mode': 'distance', 'sample_distance_m': 1,
                                    'sample_interval_s': 2}, 3.6) == pytest.approx(1.0)
        assert expected_interval_s({'sample_mode': 'distance', 'sample_distance_m': 1,
                                    'sample_interval_s': 2}, None) == 2
        assert expected_interval_s({'sample_mode': 'frames'}, 5) is None
        assert expected_interval_s(None, 5) is None

    def test_state_route_flags_owl_not_recording(self, client_ctx):
        client, ctrl = client_ctx
        ctrl.collect.start({'crop': 'wheat'})
        ctrl.collect.started_mono -= NOT_RECORDING_GRACE_S + 1
        codes = [w['code'] for w in client.get('/api/collect/state').get_json()['warnings']]
        assert 'not_recording' in codes

    def test_warning_text_in_both_languages(self):
        assert I18N.count('w_not_recording:') == 2


# 3. Start / stop report failures --------------------------------------------

@pytest.mark.unit
class TestStartStopFailures:
    def test_start_503_when_mqtt_down_and_no_run(self, client_ctx):
        client, ctrl = client_ctx
        ctrl.send_command.return_value = {'success': False, 'error': 'MQTT not connected'}
        resp = client.post('/api/collect/start', json={'metadata': {'crop': 'wheat'}})
        assert resp.status_code == 503 and resp.get_json()['error'] == 'MQTT not connected'
        assert ctrl.collect.run_id is None and ctrl.collect.started_at is None
        assert 'session_metadata' not in ctrl.desired_state.get('all', {})

    def test_start_503_when_recording_command_fails(self, client_ctx):
        client, ctrl = client_ctx
        ctrl.send_command.side_effect = lambda d, action, v=None: (
            {'success': False, 'error': 'gone'} if action == 'toggle_recording' else {'success': True})
        resp = client.post('/api/collect/start', json={'metadata': {'crop': 'wheat'}})
        assert resp.status_code == 503
        assert ctrl.collect.started_at is None
        assert 'session_metadata' not in ctrl.desired_state['all']

    def test_stop_503_keeps_run(self, client_ctx):
        client, ctrl = client_ctx
        ctrl.collect.start({'crop': 'wheat'})
        ctrl.send_command.return_value = {'success': False, 'error': 'MQTT not connected'}
        assert client.post('/api/collect/stop', json={}).status_code == 503
        assert ctrl.collect.started_at is not None


# 4. Record choices remembered on the controller -----------------------------

@pytest.mark.unit
class TestLastPick:
    def test_start_saves_last_keys_and_config_returns_them(self, client_ctx):
        client, ctrl = client_ctx
        assert client.get('/api/collect/config').get_json()['last_pick'] == {}
        client.post('/api/collect/start', json={'metadata': {
            'crop': 'barley', 'crop_season': 'spring', 'weather': 'wet',
            'lighting': 'natural_overcast', 'purpose': 'survey', 'notes': 'line one'}})
        text = ctrl.config_file.read_text()
        assert '# keep me' in text and 'last_crop = barley' in text and 'last_weather = wet' in text
        assert 'notes' not in text
        last = client.get('/api/collect/config').get_json()['last_pick']
        assert last == {'crop': 'barley', 'crop_season': 'spring', 'weather': 'wet',
                        'lighting': 'natural_overcast', 'purpose': 'survey'}

    def test_unsafe_value_not_written(self, client_ctx):
        client, ctrl = client_ctx
        resp = client.post('/api/collect/start', json={'metadata': {'crop': '100%\nrye', 'weather': 'dry'}})
        assert resp.status_code == 200
        text = ctrl.config_file.read_text()
        assert 'last_crop = \n' in text and '%' not in text and 'last_weather = dry' in text
        # And the in-memory config stays readable (no interpolation error)
        client.get('/api/collect/config')

    def test_ini_write_failure_does_not_block_start(self, client_ctx):
        client, ctrl = client_ctx
        ctrl.config_file = Path(ctrl.config_file.parent / 'missing-dir' / 'CONTROLLER.ini')
        assert client.post('/api/collect/start', json={'metadata': {'crop': 'rye'}}).status_code == 200

    def test_js_has_no_preselected_crop_and_gates_start(self):
        assert "crop: ''" in JS and "$('modal-start').disabled = !pick.crop" in JS
        assert "collect.pick" not in JS and 'data.last_pick' in JS


# 5. Kiosk dead ends -----------------------------------------------------------

@pytest.mark.unit
class TestKioskLinks:
    def test_full_dashboard_links_back_only_in_collect_mode(self, client_ctx):
        client, ctrl = client_ctx
        assert b'tab-collect' in client.get('/full').data
        ctrl.ui_mode = 'full'
        assert b'tab-collect' not in client.get('/full').data
        assert b'tab-collect' not in client.get('/').data

    def test_no_new_windows_from_collect_screen(self):
        assert 'target="_blank"' not in HTML
        assert I18N.count('cloud_portal:') == 2


# 6. Upload progress is coerced to a number ------------------------------------

@pytest.mark.unit
def test_progress_is_coerced_before_markup():
    assert 'Number(tr.progress)' in JS
    assert not re.search(r"tr\.progress \|\| 0", JS)


# 7. Rotation asks for a restart -------------------------------------------------

@pytest.mark.unit
class TestRotationRestart:
    def test_rotation_change_flags_restart_until_rejoin(self, client_ctx):
        client, ctrl = client_ctx
        resp = client.post('/api/collect/camera', json={'params': {'rotation': '180'}})
        assert resp.get_json()['restart_required'] == ['rotation']
        rows = client.get('/api/collect/state').get_json()['owls']
        assert rows[0]['restart_required'] == 'rotation'
        ctrl.collect.camera_restart.discard('owl-1')       # what a rejoin does
        assert client.get('/api/collect/state').get_json()['owls'][0]['restart_required'] == ''

    def test_same_rotation_does_not_flag(self, client_ctx):
        client, ctrl = client_ctx
        resp = client.post('/api/collect/camera', json={'params': {'rotation': 'auto'}})
        assert resp.get_json()['restart_required'] == []

    def test_camera_503_when_mqtt_down(self, client_ctx):
        client, ctrl = client_ctx
        ctrl.send_command.return_value = {'success': False, 'error': 'MQTT not connected'}
        assert client.post('/api/collect/camera', json={'params': {'awb_mode': 'auto'}}).status_code == 503

    def test_rejoin_clears_restart_flag(self):
        with patch('controller.networked.networked.CentralController.__init__', lambda self, **kw: None):
            from controller.networked.networked import CentralController
            ctrl = CentralController.__new__(CentralController)
        ctrl.owls_state = {'owl-1': {'device_id': 'owl-1', 'connected': False, 'last_seen': time.time() - 100}}
        ctrl.desired_state, ctrl.lwt_timestamps = {}, {}
        ctrl.mqtt_connected, ctrl.mqtt_lock, ctrl.mqtt_client = True, threading.Lock(), MagicMock()
        ctrl.offline_timeout, ctrl.gps_manager = 15.0, None
        ctrl.config = configparser.ConfigParser()
        ctrl.collect = CollectSession()
        ctrl.collect.camera_restart.add('owl-1')
        msg = MagicMock()
        msg.topic = 'owl/owl-1/state'
        msg.payload = json.dumps({'detection_enable': False}).encode()
        ctrl._on_message(None, None, msg)
        assert 'owl-1' not in ctrl.collect.camera_restart


# 8. Failed upload can be retried ----------------------------------------------

@pytest.mark.unit
class TestUploadRetry:
    def test_accepted_request_can_be_requested_again(self):
        c = CollectSession()
        item = {'owl': 'owl-1', 'session_id': '20261008/session_090000'}
        first = c.request_uploads([item])[0]
        assert c.request_uploads([item]) == []            # still waiting for Noktura
        c.ack_upload(first['id'], 'queued')
        assert c.request_uploads([item]) == []
        c.ack_upload(first['id'], 'accepted')
        assert len(c.request_uploads([item])) == 1        # retry after a failed transfer

    def test_route_reports_nothing_new(self, client_ctx):
        client, ctrl = client_ctx
        ok = {'items': [{'owl': 'owl-1', 'session_id': '20261008/session_090000'}]}
        assert len(client.post('/api/collect/upload', json=ok).get_json()['requested']) == 1
        again = client.post('/api/collect/upload', json=ok).get_json()
        assert again['requested'] == [] and again['reason'] == 'already_requested'

    def test_js_toasts_when_nothing_new(self):
        assert "t('upload_already')" in JS and I18N.count('upload_already:') == 2


# 9. The session being recorded cannot be ticked --------------------------------

@pytest.mark.unit
def test_current_session_not_pickable():
    assert 'isRecordingInto(owl, s)' in JS and "endsWith('/' + owl.session_id)" in JS
