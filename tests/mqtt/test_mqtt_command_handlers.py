"""OWLMQTTPublisher command dispatch (_handle_command): validation, state
and the payloads/argv each command produces.

Nothing about spraying is tested here. A MagicMock Owl accepts any call, so
"the handler asked the OWL" proves nothing about a nozzle: spray on/off/
blanket, hold, nozzle test, actuation timing and the power commands closing
the nozzles are asserted at the relay of a real Owl in
tests/owl/test_relay_matrix.py and tests/owl/test_mqtt_commands_at_the_relay.py.
What is saved to config/ is in test_mqtt_handlers.py.
"""

import configparser
import json
import time
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Spray commands before an OWL is attached (the publisher starts first)
# ---------------------------------------------------------------------------

@pytest.fixture
def bare_publisher(mqtt_publisher):
    """The publisher as it is before owl.py attaches itself: no Owl at all,
    so nothing here can be satisfied by a mock inventing a method."""
    mqtt_publisher.owl_instance = None
    return mqtt_publisher


@pytest.mark.unit
class TestSprayCommandsWithNoOwl:
    @pytest.mark.parametrize('command, enable, mode', [
        ({'action': 'set_detection_enable', 'value': True}, True, 0),
        ({'action': 'set_detection_enable', 'value': 'off'}, False, 1),
        ({'action': 'set_detection_mode', 'value': 2}, False, 2),
        ({'action': 'set_detection_mode', 'value': '0'}, True, 0),
    ])
    def test_state_follows_the_request(self, bare_publisher, command, enable, mode):
        bare_publisher.state.update(detection_enable=not enable, detection_mode=3)
        bare_publisher._handle_command(command)
        assert bare_publisher.state['detection_enable'] is enable
        assert bare_publisher.state['detection_mode'] == mode

    @pytest.mark.parametrize('command', [
        {'action': 'set_detection_enable', 'value': 'banana'},
        {'action': 'set_detection_enable'},
        {'action': 'set_detection_mode', 'value': 5},
        {'action': 'set_detection_mode', 'value': 'x'},
        {'action': 'set_detection_mode', 'value': None},
    ])
    def test_an_unclear_request_changes_no_state_and_publishes_nothing(self, bare_publisher, command):
        bare_publisher.state.update(detection_enable=True, detection_mode=0)
        bare_publisher.client.publish.reset_mock()
        bare_publisher._handle_command(command)
        assert bare_publisher.state['detection_enable'] is True
        assert bare_publisher.state['detection_mode'] == 0
        bare_publisher.client.publish.assert_not_called()

    @pytest.mark.parametrize('command, said', [
        ({'action': 'test_nozzles', 'value': {'relays': [0]}}, 'Nozzle test refused'),
        ({'action': 'hold_spray', 'value': 1500}, 'hold_spray refused'),
    ])
    def test_nozzle_commands_are_refused_out_loud(self, bare_publisher, command, said, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            bare_publisher._handle_command(command)
        assert [r.message for r in caplog.records if said in r.message] == \
            [f'{said}: no relay controller']

    def test_controller_timing_is_clamped_into_state(self, bare_publisher):
        bare_publisher._handle_command({'action': 'set_actuation_params',
                                        'actuation_duration': 100.0, 'delay': -5.0})
        assert (bare_publisher.state['actuation_duration'], bare_publisher.state['delay']) == (5.0, 0.0)
        assert bare_publisher.state['actuation_source'] == 'controller'

    @pytest.mark.parametrize('field', ['actuation_duration', 'delay', 'speed_kmh'])
    def test_non_numeric_controller_timing_is_refused_whole(self, bare_publisher, field, caplog):
        import logging
        before = {k: bare_publisher.state.get(k)
                  for k in ('actuation_duration', 'delay', 'actuation_source')}
        command = {'action': 'set_actuation_params', 'actuation_duration': 0.3, 'delay': 0.1,
                   field: 'fast'}
        with caplog.at_level(logging.ERROR):
            bare_publisher._handle_command(command)
        assert {k: bare_publisher.state.get(k) for k in before} == before
        assert any('Invalid actuation params' in r.message for r in caplog.records)


@pytest.mark.unit
class TestImageSampleEnable:
    """Tests for set_image_sample_enable command handler."""

    def test_enable_recording(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_image_sample_enable', 'value': True})
        assert mqtt_publisher.state['image_sample_enable'] is True

    def test_disable_recording(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state['image_sample_enable'] = True
        mqtt_publisher._handle_command({'action': 'set_image_sample_enable', 'value': False})
        assert mqtt_publisher.state['image_sample_enable'] is False


@pytest.mark.unit
class TestStopMetadataOffTheMqttLoop:
    """Pi 5 field test 2026-10-29: Stop wrote session_metadata.json under
    state_lock on the MQTT loop. A stalled drive then froze every later
    command while the video kept streaming."""

    def test_stalled_metadata_write_does_not_block_commands(self, mqtt_publisher, mock_owl):
        import threading
        started, release = threading.Event(), threading.Event()

        def stalled_write(metadata):
            started.set()
            release.wait(5)

        mqtt_publisher.state['image_sample_enable'] = True
        mqtt_publisher.state['session_metadata'] = {'crop': 'wheat'}
        with patch.object(mqtt_publisher, '_write_session_metadata', side_effect=stalled_write):
            mqtt_publisher._handle_command({'action': 'set_image_sample_enable', 'value': False})
            assert started.wait(2), "Stop never wrote the metadata"
            # The writer is stuck on the drive; commands still go through
            mqtt_publisher._handle_command({'action': 'set_crop_buffer', 'value': 33})
            assert mqtt_publisher.state['crop_buffer_px'] == 33
            assert mqtt_publisher.state_lock.acquire(timeout=1)
            mqtt_publisher.state_lock.release()
            release.set()

    def test_stop_still_writes_the_metadata(self, mqtt_publisher, mock_owl):
        import threading
        written = threading.Event()
        mqtt_publisher.state['image_sample_enable'] = True
        mqtt_publisher.state['session_metadata'] = {'crop': 'wheat'}
        with patch.object(mqtt_publisher, '_write_session_metadata',
                          side_effect=lambda metadata: written.set()) as write:
            mqtt_publisher._handle_command({'action': 'set_image_sample_enable', 'value': False})
            assert written.wait(2)
        assert write.call_args.args[0] == {'crop': 'wheat'}


@pytest.mark.unit
class TestGpsSourceAndSpeed:
    """The app's Speed cell: display speed from any fresh fix, and where the
    position comes from. Spray-timing speed is not touched here."""

    def test_known_push_source_kept(self, mqtt_publisher):
        mqtt_publisher._handle_gps_update({'latitude': -31.5, 'longitude': 150.2,
                                           'source': 'phone'})
        assert mqtt_publisher.state['gps_payload']['source'] == 'phone'

    @pytest.mark.parametrize('source', ['serial', 'dashboard', 'x', 3])
    def test_unknown_push_source_dropped(self, mqtt_publisher, source):
        """Nothing pushed over MQTT may pose as the OWL's own receiver."""
        mqtt_publisher._handle_gps_update({'latitude': -31.5, 'longitude': 150.2,
                                           'source': source})
        assert 'source' not in mqtt_publisher.state['gps_payload']

    def test_speed_published_and_quantized(self, mqtt_publisher):
        mqtt_publisher.set_gps_status(True, 'phone', -31.5, 150.2, 4.0, speed_kmh=7.32)
        assert mqtt_publisher.state['gps_speed_kmh'] == 7.5
        assert mqtt_publisher.state['gps_source'] == 'phone'

    def test_speed_jitter_does_not_republish(self, mqtt_publisher):
        mqtt_publisher.set_gps_status(True, 'phone', -31.5, 150.2, 4.0, speed_kmh=7.4)
        mqtt_publisher.client.publish.reset_mock()
        mqtt_publisher.set_gps_status(True, 'phone', -31.5, 150.2, 4.0, speed_kmh=7.6)
        assert not mqtt_publisher.client.publish.called

    def test_fix_detail_stored(self, mqtt_publisher):
        mqtt_publisher.set_gps_status(True, 'serial', -31.5, 150.2, None, satellites=11,
                                      hdop=0.84, fix_quality=4, age_s=0.31)
        state = mqtt_publisher.state
        assert state['gps_best_accuracy'] is None
        assert (state['gps_satellites'], state['gps_hdop']) == (11, 0.8)
        assert (state['gps_fix_quality'], state['gps_fix_age_s']) == (4, 0.3)

    def test_fix_detail_jitter_does_not_republish(self, mqtt_publisher):
        """Satellite count, HDOP and age wobble every second on a parked
        rig; they ride the 2 s heartbeat. A change of fix type publishes."""
        mqtt_publisher.set_gps_status(True, 'serial', -31.5, 150.2, None, satellites=11,
                                      hdop=0.8, fix_quality=1, age_s=0.2)
        mqtt_publisher.client.publish.reset_mock()
        mqtt_publisher.set_gps_status(True, 'serial', -31.5, 150.2, None, satellites=12,
                                      hdop=0.9, fix_quality=1, age_s=0.7)
        assert not mqtt_publisher.client.publish.called
        assert mqtt_publisher.state['gps_satellites'] == 12
        mqtt_publisher.set_gps_status(True, 'serial', -31.5, 150.2, None, satellites=12,
                                      hdop=0.9, fix_quality=4, age_s=0.7)
        assert mqtt_publisher.client.publish.called

    def test_fix_detail_cleared_without_fix(self, mqtt_publisher):
        mqtt_publisher.set_gps_status(True, 'serial', -31.5, 150.2, None, satellites=11,
                                      hdop=0.8, fix_quality=1, age_s=0.2)
        mqtt_publisher.set_gps_status(False, None, None, None, None)
        state = mqtt_publisher.state
        assert state['gps_satellites'] is None and state['gps_hdop'] is None
        assert state['gps_fix_quality'] is None and state['gps_fix_age_s'] is None

    def test_no_speed_is_none_not_zero(self, mqtt_publisher):
        mqtt_publisher.set_gps_status(True, 'serial', -31.5, 150.2, 2.0)
        assert mqtt_publisher.state['gps_speed_kmh'] is None


@pytest.mark.unit
class TestRecordingPaused:
    """set_recording_paused: headland pause, same session (2026-10-15)."""

    def test_pause_and_resume_while_recording(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state['image_sample_enable'] = True
        mqtt_publisher._handle_command({'action': 'set_recording_paused', 'value': True})
        assert mqtt_publisher.get_recording_paused() is True
        mqtt_publisher._handle_command({'action': 'set_recording_paused', 'value': False})
        assert mqtt_publisher.get_recording_paused() is False

    def test_cannot_pause_when_not_recording(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_recording_paused', 'value': True})
        assert mqtt_publisher.get_recording_paused() is False

    @pytest.mark.parametrize('value', [False, True])
    def test_stop_or_start_clears_the_pause(self, mqtt_publisher, mock_owl, value):
        mqtt_publisher.state['image_sample_enable'] = not value
        mqtt_publisher.state['recording_paused'] = True
        mqtt_publisher._handle_command({'action': 'set_image_sample_enable', 'value': value})
        assert mqtt_publisher.get_recording_paused() is False

    def test_internal_stop_clears_the_pause(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state['image_sample_enable'] = True
        mqtt_publisher.state['recording_paused'] = True
        mqtt_publisher.set_image_sample_enable(False)
        assert mqtt_publisher.get_recording_paused() is False

    def test_not_a_cloud_command(self):
        from utils.mqtt_manager import CLOUD_ALLOWED_ACTIONS
        assert 'set_recording_paused' not in CLOUD_ALLOWED_ACTIONS


# ---------------------------------------------------------------------------
# set_sensitivity_level
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetSensitivityLevel:
    """Tests for set_sensitivity_level command handler."""

    def test_valid_level_updates_state(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_sensitivity_level', 'level': 'low'})
        assert mqtt_publisher.state['sensitivity_level'] == 'low'

    def test_valid_level_high(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_sensitivity_level', 'level': 'high'})
        assert mqtt_publisher.state['sensitivity_level'] == 'high'

    def test_invalid_level_rejected(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state['sensitivity_level'] = 'medium'
        mqtt_publisher._handle_command({'action': 'set_sensitivity_level', 'level': 'extreme'})
        # State should remain unchanged — invalid level returns early
        assert mqtt_publisher.state['sensitivity_level'] == 'medium'

    def test_case_insensitive(self, mqtt_publisher, mock_owl):
        """Level is lowercased by the handler."""
        mqtt_publisher._handle_command({'action': 'set_sensitivity_level', 'level': 'LOW'})
        # The handler lowercases the level before checking
        assert mqtt_publisher.state['sensitivity_level'] == 'low'


# ---------------------------------------------------------------------------
# set_greenonbrown_param (command dispatch)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetGreenOnBrownParamCommand:
    """Tests for set_greenonbrown_param command dispatch."""

    def test_updates_instance_attribute(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_greenonbrown_param',
            'param': 'exg_min',
            'value': 42
        })
        assert mock_owl.exg_min == 42

    def test_updates_state(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_greenonbrown_param',
            'param': 'hue_max',
            'value': 100
        })
        assert mqtt_publisher.state['hue_max'] == 100

    def test_rejects_invalid_param(self, mqtt_publisher, mock_owl):
        """Invalid param names should be rejected without crash."""
        original = mock_owl.exg_min
        mqtt_publisher._handle_command({
            'action': 'set_greenonbrown_param',
            'param': 'nonexistent_param',
            'value': 99
        })
        assert mock_owl.exg_min == original  # unchanged

    def test_missing_param_changes_nothing(self, mqtt_publisher, mock_owl):
        before = dict(mock_owl.config['GreenOnBrown'])
        mqtt_publisher._handle_command({
            'action': 'set_greenonbrown_param',
            'value': 42
        })
        assert dict(mock_owl.config['GreenOnBrown']) == before
        assert (mock_owl.exg_min, mock_owl.hue_max) == (25, 83)


# ---------------------------------------------------------------------------
# set_greenongreen_param (command dispatch)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetGreenOnGreenParamCommand:
    """Tests for set_greenongreen_param command dispatch."""

    def test_confidence_via_command(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_greenongreen_param',
            'key': 'confidence',
            'value': 0.85
        })
        assert mock_owl._gog_confidence == 0.85
        assert mqtt_publisher.state['confidence'] == 0.85

    def test_only_confidence_applies_live(self, mqtt_publisher, mock_owl):
        """model_path over this command changes nothing on the running OWL
        (set_model is the command that switches models)."""
        mqtt_publisher._handle_command({
            'action': 'set_greenongreen_param',
            'key': 'model_path',
            'value': 'models/new_model'
        })
        assert mock_owl._model_path == 'models'
        assert mock_owl._pending_model is None
        assert mock_owl.config.get('GreenOnGreen', 'model_path') == 'models'
        assert mock_owl._gog_confidence == 0.5


# ---------------------------------------------------------------------------
# set_config (command dispatch — routes to _update_greenonbrown_param)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetConfigCommand:
    """Tests for set_config command dispatch."""

    def test_routes_to_greenonbrown_update(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_config',
            'key': 'saturation_min',
            'value': 75
        })
        assert mock_owl.saturation_min == 75

    def test_missing_key_changes_nothing(self, mqtt_publisher, mock_owl):
        before = dict(mock_owl.config['GreenOnBrown'])
        mqtt_publisher._handle_command({
            'action': 'set_config',
            'value': 42
        })
        assert dict(mock_owl.config['GreenOnBrown']) == before
        assert mock_owl.saturation_min == 50


# ---------------------------------------------------------------------------
# set_detect_classes
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetDetectClasses:
    """Tests for set_detect_classes command handler."""

    def test_list_input(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_detect_classes',
            'value': ['weed', 'crop']
        })
        assert mock_owl._pending_detect_classes == ['weed', 'crop']
        assert mqtt_publisher.state['detect_classes'] == ['weed', 'crop']

    def test_string_input_comma_separated(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_detect_classes',
            'value': 'weed,crop,grass'
        })
        assert mock_owl._pending_detect_classes == ['weed', 'crop', 'grass']

    def test_empty_list_clears_filter(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_detect_classes',
            'value': []
        })
        assert mock_owl._pending_detect_classes == []
        assert mqtt_publisher.state['detect_classes'] == []


# ---------------------------------------------------------------------------
# set_model
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetModelCommand:
    """Tests for set_model command handler."""

    def test_queues_model_with_models_prefix(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_model',
            'value': 'yolo26n-seg.pt'
        })
        import os
        assert mock_owl._pending_model == os.path.join('models', 'yolo26n-seg.pt')

    def test_empty_model_ignored(self, mqtt_publisher, mock_owl):
        mock_owl._pending_model = None
        mqtt_publisher._handle_command({
            'action': 'set_model',
            'value': ''
        })
        assert mock_owl._pending_model is None


@pytest.mark.unit
class TestNozzleCountState:
    def test_state_reports_nozzle_count(self, mqtt_publisher, mock_owl):
        mock_owl.relay_num = 10
        mqtt_publisher._sync_parameters_to_state()
        assert mqtt_publisher.state['relay_num'] == 10
        json.dumps(mqtt_publisher.state)


# ---------------------------------------------------------------------------
# restart_service
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRestartService:
    """What runs. That the nozzles close first: tests/owl/
    test_mqtt_commands_at_the_relay.py::test_power_commands_close_every_nozzle_first."""

    def test_restarts_the_service_without_blocking(self, mqtt_publisher, mock_owl):
        """Popen, not run: the handler must return before systemd kills us."""
        with patch('subprocess.Popen') as mock_popen, patch('subprocess.run') as mock_run:
            mqtt_publisher._handle_command({'action': 'restart_service'})
            assert mqtt_publisher.io_flush()
        mock_popen.assert_called_once()
        assert mock_popen.call_args[0][0] == ['sudo', 'systemctl', 'restart', 'owl.service']
        mock_run.assert_not_called()

    def test_a_failed_restart_is_logged(self, mqtt_publisher, mock_owl, caplog):
        import logging
        with patch('subprocess.Popen', side_effect=OSError("no sudo")), \
                caplog.at_level(logging.ERROR):
            mqtt_publisher._handle_command({'action': 'restart_service'})
            assert mqtt_publisher.io_flush()
        assert any('Error restarting service: no sudo' in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# set_algorithm
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetAlgorithm:
    """Tests for set_algorithm command handler."""

    def test_valid_algorithm_is_pending_until_loaded(self, mqtt_publisher, mock_owl):
        """'algorithm' is what runs; a request is only pending until owl.py
        has loaded the detector."""
        mqtt_publisher.state['algorithm'] = 'exhsv'
        mqtt_publisher._handle_command({'action': 'set_algorithm', 'value': 'exg'})
        assert mqtt_publisher.state['algorithm_pending'] == 'exg'
        assert mqtt_publisher.state['algorithm'] == 'exhsv'

    def test_valid_algorithm_queues_on_owl(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_algorithm', 'value': 'hsv'})
        assert mock_owl._pending_algorithm == 'hsv'

    def test_all_valid_algorithms(self, mqtt_publisher, mock_owl):
        """Every supported algorithm should be accepted.

        Driven off ConfigValidator rather than a hand-written list: the handler
        itself calls get_valid_algorithms(), so a literal here would silently
        drift the moment an algorithm is added or retired.
        """
        from utils.config_manager import ConfigValidator
        algorithms = ConfigValidator.get_valid_algorithms()
        assert algorithms, 'no valid algorithms reported'
        for algo in algorithms:
            mqtt_publisher._handle_command({'action': 'set_algorithm', 'value': algo})
            assert mqtt_publisher.state['algorithm_pending'] == algo

    def test_retired_algorithms_rejected(self, mqtt_publisher, mock_owl):
        """Algorithms deleted in v3.16 must not be selectable.

        gndvi drew a window inside its per-frame maths (crashed headless units)
        and maxg divided by the per-frame max with no guard. Both are gone, so
        accepting the name would leave the OWL with no detector.
        """
        mqtt_publisher.state['algorithm'] = 'exhsv'
        for algo in ('maxg', 'gndvi', 'exgr', 'nexg'):
            mqtt_publisher._handle_command({'action': 'set_algorithm', 'value': algo})
            assert mqtt_publisher.state['algorithm'] == 'exhsv'
            assert mqtt_publisher.state['algorithm_pending'] is None

    def test_invalid_algorithm_rejected(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state['algorithm'] = 'exhsv'
        mqtt_publisher._handle_command({'action': 'set_algorithm', 'value': 'fakealgo'})
        assert mqtt_publisher.state['algorithm'] == 'exhsv'  # unchanged

    def test_case_insensitive(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_algorithm', 'value': 'EXG'})
        assert mqtt_publisher.state['algorithm_pending'] == 'exg'

    def test_does_not_touch_config_before_the_detector_loads(self, mqtt_publisher, mock_owl):
        """owl.py sets [System] algorithm only after a successful load; the
        handler setting it first is how owl-1 kept gog without ultralytics."""
        before = mock_owl.config.get('System', 'algorithm')
        mqtt_publisher._handle_command({'action': 'set_algorithm', 'value': 'gog'})
        assert mock_owl.config.get('System', 'algorithm') == before


# ---------------------------------------------------------------------------
# set_crop_buffer
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetCropBuffer:
    """Tests for set_crop_buffer command handler."""

    def test_valid_value_updates_state(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_crop_buffer', 'value': 30})
        assert mqtt_publisher.state['crop_buffer_px'] == 30

    def test_updates_owl_instance(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_crop_buffer', 'value': 15})
        assert mock_owl.crop_buffer_px == 15

    def test_clamps_to_max_50(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_crop_buffer', 'value': 100})
        assert mqtt_publisher.state['crop_buffer_px'] == 50

    def test_clamps_to_min_0(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_crop_buffer', 'value': -10})
        assert mqtt_publisher.state['crop_buffer_px'] == 0

    def test_string_value_converted(self, mqtt_publisher, mock_owl):
        """INI values arrive as strings — handler should int-convert."""
        mqtt_publisher._handle_command({'action': 'set_crop_buffer', 'value': '25'})
        assert mqtt_publisher.state['crop_buffer_px'] == 25

    def test_invalid_value_keeps_the_current_buffer(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_crop_buffer', 'value': 'abc'})
        assert mqtt_publisher.state['crop_buffer_px'] == 20
        assert mock_owl.crop_buffer_px == 20


# ---------------------------------------------------------------------------
# get_config
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGetConfig:
    def test_command_publishes_the_config_once_on_the_config_topic(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'get_config'})
        sent = [json.loads(c[0][1]) for c in mqtt_publisher.client.publish.call_args_list
                if c[0][0] == mqtt_publisher.topics['config']]
        assert len(sent) == 1
        assert sent[0]['device_id'] == 'test-owl'
        assert sent[0]['config_name'] == 'test_config.ini'
        assert sent[0]['config_path'] == str(mock_owl.config_path)
        assert sent[0]['config']['GreenOnBrown']['hue_max'] == '83'


# ---------------------------------------------------------------------------
# set_config_section
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetConfigSection:
    """Tests for set_config_section command handler."""

    def test_greenonbrown_params_applied_and_kept(self, mqtt_publisher, mock_owl, owl_home):
        mqtt_publisher._handle_command({
            'action': 'set_config_section',
            'section': 'GreenOnBrown',
            'params': {'exg_min': '35', 'hue_max': '90'}
        })
        assert mqtt_publisher.io_flush()
        assert (mock_owl.exg_min, mock_owl.hue_max) == (35, 90)
        assert mock_owl.config.get('GreenOnBrown', 'exg_min') == '35'
        # thresholds have no Save button: written to the working file at once
        saved = configparser.ConfigParser()
        saved.read_string((owl_home / 'config' / 'config_autosave.ini').read_text())
        assert (saved.get('GreenOnBrown', 'exg_min'), saved.get('GreenOnBrown', 'hue_max')) == ('35', '90')

    def test_greenongreen_params_routed(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_config_section',
            'section': 'GreenOnGreen',
            'params': {'confidence': '0.75'}
        })
        assert mock_owl._gog_confidence == 0.75

    def test_system_algorithm_routes_correctly(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_config_section',
            'section': 'System',
            'params': {'algorithm': 'hsv'}
        })
        assert mqtt_publisher.state['algorithm_pending'] == 'hsv'
        assert mock_owl._pending_algorithm == 'hsv'

    def test_generic_attribute_type_conversion(self, mqtt_publisher, mock_owl):
        """Generic section params with int/float types auto-convert from strings."""
        # Use a section other than GreenOnBrown/GreenOnGreen/System.algorithm
        # to hit the generic hasattr/setattr path with type conversion
        mqtt_publisher._handle_command({
            'action': 'set_config_section',
            'section': 'Camera',
            'params': {'crop_buffer_px': '35'}
        })
        # crop_buffer_px is int(20) on mock_owl, so string '35' should become int
        assert mock_owl.crop_buffer_px == 35
        assert isinstance(mock_owl.crop_buffer_px, int)

    @pytest.mark.parametrize('section, params', [('', {'exg_min': '40'}), ('GreenOnBrown', {}),
                                                 (None, {'exg_min': '40'})])
    def test_no_section_or_no_params_changes_nothing(self, mqtt_publisher, mock_owl, section, params):
        before = {s: dict(mock_owl.config[s]) for s in mock_owl.config.sections()}
        mqtt_publisher._handle_command({'action': 'set_config_section',
                                        'section': section, 'params': params})
        assert {s: dict(mock_owl.config[s]) for s in mock_owl.config.sections()} == before
        assert mock_owl.exg_min == 25


# ---------------------------------------------------------------------------
# download_model
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDownloadModel:
    """Tests for download_model command handler."""

    def test_starts_the_download_in_a_background_thread(self, mqtt_publisher, mock_owl):
        with patch('threading.Thread') as MockThread:
            mqtt_publisher._handle_command({
                'action': 'download_model',
                'url': 'https://controller.local/models/test.pt',
                'filename': 'test.pt',
                'sha256': 'abc123',
                'is_archive': False
            })
        MockThread.assert_called_once_with(
            target=mqtt_publisher._download_model,
            args=('https://controller.local/models/test.pt', 'test.pt', 'abc123', False),
            daemon=True)
        MockThread.return_value.start.assert_called_once_with()

    def test_missing_url_logs_error(self, mqtt_publisher, mock_owl):
        """Missing URL should not start download thread."""
        with patch('threading.Thread') as MockThread:
            mqtt_publisher._handle_command({
                'action': 'download_model',
                'filename': 'test.pt'
            })
            MockThread.assert_not_called()

    def test_missing_filename_logs_error(self, mqtt_publisher, mock_owl):
        with patch('threading.Thread') as MockThread:
            mqtt_publisher._handle_command({
                'action': 'download_model',
                'url': 'https://controller.local/models/test.pt'
            })
            MockThread.assert_not_called()

    def test_is_archive_flag_passed(self, mqtt_publisher, mock_owl):
        with patch('threading.Thread') as MockThread:
            mock_thread = MagicMock()
            MockThread.return_value = mock_thread
            mqtt_publisher._handle_command({
                'action': 'download_model',
                'url': 'https://controller.local/models/test.zip',
                'filename': 'test.zip',
                'sha256': '',
                'is_archive': True
            })
            # Verify is_archive=True was passed to _download_model args
            thread_args = MockThread.call_args
            assert thread_args[1]['args'][3] is True  # 4th arg = is_archive


# ---------------------------------------------------------------------------
# set_tracking
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetTracking:
    """Tests for set_tracking command handler."""

    def test_enable_tracking(self, mqtt_publisher, mock_owl):
        mock_owl.tracking_enabled = False
        mock_owl._class_smoother = None
        mock_owl._crop_stabilizer = None
        mock_owl._track_class_window = 5
        mock_owl._track_crop_persist = 3
        mock_owl._gog_detector = None

        mqtt_publisher._handle_command({'action': 'set_tracking', 'value': True})

        assert mqtt_publisher.state['tracking_enabled'] is True
        assert mock_owl.tracking_enabled is True

    def test_disable_tracking(self, mqtt_publisher, mock_owl):
        from utils.tracker import ClassSmoother, CropMaskStabilizer
        mock_owl.tracking_enabled = True
        mock_owl._class_smoother = ClassSmoother(window=5)
        mock_owl._crop_stabilizer = CropMaskStabilizer(max_age=3)
        mock_owl._gog_detector = None

        mqtt_publisher._handle_command({'action': 'set_tracking', 'value': False})

        assert mqtt_publisher.state['tracking_enabled'] is False
        assert mock_owl.tracking_enabled is False

    def test_enable_creates_smoother_and_stabilizer(self, mqtt_publisher, mock_owl):
        mock_owl.tracking_enabled = False
        mock_owl._class_smoother = None
        mock_owl._crop_stabilizer = None
        mock_owl._track_class_window = 7
        mock_owl._track_crop_persist = 4
        mock_owl._gog_detector = None

        mqtt_publisher._handle_command({'action': 'set_tracking', 'value': True})

        from utils.tracker import ClassSmoother, CropMaskStabilizer
        assert isinstance(mock_owl._class_smoother, ClassSmoother)
        assert isinstance(mock_owl._crop_stabilizer, CropMaskStabilizer)
        assert mock_owl._class_smoother.window == 7
        assert mock_owl._crop_stabilizer.max_age == 4

    def test_disable_resets_detector_tracker(self, mqtt_publisher, mock_owl):
        from unittest.mock import MagicMock
        from utils.tracker import ClassSmoother, CropMaskStabilizer

        mock_owl.tracking_enabled = True
        mock_owl._class_smoother = ClassSmoother(window=5)
        mock_owl._crop_stabilizer = CropMaskStabilizer(max_age=3)

        mock_gog = MagicMock()
        mock_gog.tracking_enabled = True
        mock_owl._gog_detector = mock_gog

        mqtt_publisher._handle_command({'action': 'set_tracking', 'value': False})

        assert mock_gog.tracking_enabled is False
        mock_gog.reset_tracker.assert_called_once()

    def test_enable_passes_stabilizer_to_detector(self, mqtt_publisher, mock_owl):
        from unittest.mock import MagicMock

        mock_owl.tracking_enabled = False
        mock_owl._class_smoother = None
        mock_owl._crop_stabilizer = None
        mock_owl._track_class_window = 5
        mock_owl._track_crop_persist = 3

        mock_gog = MagicMock()
        mock_gog.tracking_enabled = False
        mock_owl._gog_detector = mock_gog

        mqtt_publisher._handle_command({'action': 'set_tracking', 'value': True})

        assert mock_gog.tracking_enabled is True
        assert mock_gog._crop_stabilizer is mock_owl._crop_stabilizer

    def test_disable_resets_smoother_and_stabilizer(self, mqtt_publisher, mock_owl):
        from utils.tracker import ClassSmoother, CropMaskStabilizer
        smoother = ClassSmoother(window=5)
        stabilizer = CropMaskStabilizer(max_age=3)
        # Seed some state into them
        smoother.update([1], [0], [0.9], frame_count=1)
        stabilizer.update([1], [[10, 20, 50, 60]])

        mock_owl.tracking_enabled = True
        mock_owl._class_smoother = smoother
        mock_owl._crop_stabilizer = stabilizer
        mock_owl._gog_detector = None

        mqtt_publisher._handle_command({'action': 'set_tracking', 'value': False})

        # Smoother and stabilizer should be reset (empty internal state)
        assert len(smoother._history) == 0
        assert stabilizer.active_count == 0

    def test_enable_idempotent_does_not_recreate_smoother(self, mqtt_publisher, mock_owl):
        """Re-enabling tracking when smoother already exists should keep the same object."""
        from utils.tracker import ClassSmoother, CropMaskStabilizer
        existing_smoother = ClassSmoother(window=5)
        existing_stabilizer = CropMaskStabilizer(max_age=3)

        mock_owl.tracking_enabled = True
        mock_owl._class_smoother = existing_smoother
        mock_owl._crop_stabilizer = existing_stabilizer
        mock_owl._gog_detector = None

        mqtt_publisher._handle_command({'action': 'set_tracking', 'value': True})

        # Should keep the existing objects, not create new ones
        assert mock_owl._class_smoother is existing_smoother
        assert mock_owl._crop_stabilizer is existing_stabilizer

    def test_string_false_disables_tracking(self, mqtt_publisher, mock_owl):
        """Sending value='false' as string must disable tracking (not bool('false') = True)."""
        mock_owl.tracking_enabled = True
        mock_owl._class_smoother = None
        mock_owl._crop_stabilizer = None
        mock_owl._gog_detector = None

        mqtt_publisher._handle_command({'action': 'set_tracking', 'value': 'false'})

        assert mqtt_publisher.state['tracking_enabled'] is False
        assert mock_owl.tracking_enabled is False

    def test_string_true_enables_tracking(self, mqtt_publisher, mock_owl):
        """Sending value='true' as string must enable tracking."""
        mock_owl.tracking_enabled = False
        mock_owl._class_smoother = None
        mock_owl._crop_stabilizer = None
        mock_owl._track_class_window = 5
        mock_owl._track_crop_persist = 3
        mock_owl._gog_detector = None

        mqtt_publisher._handle_command({'action': 'set_tracking', 'value': 'true'})

        assert mqtt_publisher.state['tracking_enabled'] is True
        assert mock_owl.tracking_enabled is True


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestShutdown:
    """What runs. Nozzles closed first: tests/owl/test_mqtt_commands_at_the_relay.py."""

    def test_shutdown_runs_sudo_n_shutdown_now(self, mqtt_publisher, mock_owl):
        with patch('subprocess.run') as mock_run, \
             patch('shutil.which', return_value='/usr/sbin/shutdown'):
            mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
            mqtt_publisher._handle_command({'action': 'shutdown'})
            mqtt_publisher.io_flush()
        mock_run.assert_called_once()
        argv = mock_run.call_args[0][0]
        # -n matters: without it a sudoers miss hangs on a password prompt
        assert argv == ['sudo', '-n', '/usr/sbin/shutdown', 'now']

    def test_shutdown_that_cannot_run_is_logged(self, mqtt_publisher, mock_owl, caplog):
        import logging
        with patch('subprocess.run', side_effect=OSError('not found')), \
                caplog.at_level(logging.ERROR):
            mqtt_publisher._handle_command({'action': 'shutdown'})
            assert mqtt_publisher.io_flush()
        assert any('Error running shutdown: not found' in r.message for r in caplog.records)

    def test_shutdown_logs_nonzero_returncode(self, mqtt_publisher, mock_owl,
                                              caplog):
        """A sudoers miss must be LOUD — Popen with DEVNULL used to swallow
        it and the box just never went down."""
        import logging
        result = MagicMock(returncode=1, stdout='',
                           stderr='sudo: a password is required')
        with patch('subprocess.run', return_value=result), \
             caplog.at_level(logging.ERROR):
            mqtt_publisher._handle_command({'action': 'shutdown'})
            mqtt_publisher.io_flush()
        assert any('password is required' in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# update_software
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestUpdateSoftware:
    """Tests for the update_software command handler (remote git update)."""

    def _expected_argv(self, mqtt_publisher, ref, request_id):
        import getpass
        from pathlib import Path
        user = getpass.getuser()
        # the update script is code (the checkout); only its status file
        # lives in the OWL home
        repo_dir = Path(__file__).resolve().parents[2]
        return [
            'sudo', '-n', '/usr/bin/systemd-run',
            '--unit=owl-update', '--collect',
            '--property=RuntimeMaxSec=1800',
            f'--uid={user}', f'--gid={user}',
            '/bin/bash', str(repo_dir / 'owl_update.sh'),
            '--unattended', '--ref', ref,
            '--request-id', request_id,
            '--status-file', str(mqtt_publisher._update_status_path),
        ]

    def test_valid_ref_launches_systemd_run(self, mqtt_publisher, mock_owl):
        with patch('subprocess.Popen') as mock_popen:
            mqtt_publisher._handle_command({
                'action': 'update_software', 'ref': 'main', 'request_id': 'req-1'
            })
        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        assert argv == self._expected_argv(mqtt_publisher, 'main', 'req-1')

    def test_with_ai_adds_the_flag(self, mqtt_publisher, mock_owl):
        """Install AI from the networked controller or the cloud."""
        with patch('subprocess.Popen') as mock_popen:
            mqtt_publisher._handle_command({
                'action': 'update_software', 'ref': 'main', 'request_id': 'req-ai',
                'with_ai': True,
            })
        argv = mock_popen.call_args[0][0]
        assert argv == self._expected_argv(mqtt_publisher, 'main', 'req-ai') + ['--with-ai']

    def test_with_ai_must_be_true_not_truthy(self, mqtt_publisher, mock_owl):
        with patch('subprocess.Popen') as mock_popen:
            mqtt_publisher._handle_command({
                'action': 'update_software', 'ref': 'main', 'request_id': 'r',
                'with_ai': 'yes',
            })
        assert '--with-ai' not in mock_popen.call_args[0][0]

    def test_seeds_starting_state_with_request_id(self, mqtt_publisher, mock_owl):
        with patch('subprocess.Popen'):
            mqtt_publisher._handle_command({
                'action': 'update_software', 'ref': 'main', 'request_id': 'req-2'
            })
        su = mqtt_publisher.state['software_update']
        assert su['status'] == 'starting'
        assert su['request_id'] == 'req-2'
        assert su['ref'] == 'main'

    @pytest.mark.parametrize('bad_ref', [
        '../etc/passwd', 'a..b', 'https://evil.com/repo', 'main; rm -rf x',
        '-rf', '', 'branch name with spaces',
    ])
    def test_invalid_ref_rejected(self, mqtt_publisher, mock_owl, bad_ref, caplog):
        import logging
        with patch('subprocess.Popen') as mock_popen, caplog.at_level(logging.WARNING):
            mqtt_publisher._handle_command({
                'action': 'update_software', 'ref': bad_ref, 'request_id': 'r'
            })
        mock_popen.assert_not_called()
        assert mqtt_publisher.state['software_update']['status'] == 'error'
        assert any('update_software refused' in r.message for r in caplog.records)

    def test_refused_while_update_in_progress(self, mqtt_publisher, mock_owl, tmp_path):
        mqtt_publisher._update_status_path = tmp_path / 'absent.json'
        mqtt_publisher.state['software_update']['status'] = 'fetching'
        mqtt_publisher.state['software_update']['updated_at'] = int(time.time())
        with patch('subprocess.Popen') as mock_popen:
            mqtt_publisher._handle_command({
                'action': 'update_software', 'ref': 'main', 'request_id': 'r'
            })
        mock_popen.assert_not_called()
        assert mqtt_publisher.state['software_update']['status'] == 'error'
        assert 'already in progress' in mqtt_publisher.state['software_update']['error']

    def test_a_cut_off_run_no_longer_blocks_updates(self, mqtt_publisher, mock_owl, tmp_path):
        # v3.27: a status nobody has written for 30 min is not an update in
        # progress (a power cut mid-update used to block this path for good)
        mqtt_publisher._update_status_path = tmp_path / 'absent.json'
        mqtt_publisher.state['software_update']['status'] = 'installing_deps'
        mqtt_publisher.state['software_update']['updated_at'] = int(time.time()) - 4000
        with patch('subprocess.Popen') as mock_popen:
            mqtt_publisher._handle_command({
                'action': 'update_software', 'ref': 'main', 'request_id': 'r'
            })
        mock_popen.assert_called_once()

    def test_refused_while_transfer_in_progress(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state['data_transfer']['status'] = 'uploading'
        with patch('subprocess.Popen') as mock_popen:
            mqtt_publisher._handle_command({
                'action': 'update_software', 'ref': 'main', 'request_id': 'r'
            })
        mock_popen.assert_not_called()
        assert 'transfer in progress' in mqtt_publisher.state['software_update']['error']

    def test_popen_failure_reports_error(self, mqtt_publisher, mock_owl):
        with patch('subprocess.Popen', side_effect=OSError('sudo denied')):
            mqtt_publisher._handle_command({
                'action': 'update_software', 'ref': 'main', 'request_id': 'r'
            })
        assert mqtt_publisher.state['software_update']['status'] == 'error'
        assert 'failed to launch' in mqtt_publisher.state['software_update']['error']

    def test_terminal_statuses_allow_new_update(self, mqtt_publisher, mock_owl):
        for terminal in ('idle', 'complete', 'rolled_back', 'error'):
            mqtt_publisher.state['software_update'] = {
                'request_id': 'old', 'status': terminal, 'ref': 'main',
                'from': '', 'to': '', 'error': '', 'rollback_failed': False,
                'updated_at': 0
            }
            with patch('subprocess.Popen') as mock_popen:
                mqtt_publisher._handle_command({
                    'action': 'update_software', 'ref': 'main', 'request_id': 'new'
                })
            mock_popen.assert_called_once()


# ---------------------------------------------------------------------------
# reboot
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRebootCommand:
    """Tests for the reboot command handler."""

    def test_reboot_runs_sudo_n(self, mqtt_publisher, mock_owl):
        with patch('subprocess.run') as mock_run, \
             patch('shutil.which', return_value='/usr/sbin/reboot'):
            mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
            mqtt_publisher._handle_command({'action': 'reboot'})
            mqtt_publisher.io_flush()
        mock_run.assert_called_once()
        argv = mock_run.call_args[0][0]
        assert argv == ['sudo', '-n', '/usr/sbin/reboot']

    def test_reboot_that_cannot_run_is_logged(self, mqtt_publisher, mock_owl, caplog):
        import logging
        with patch('subprocess.run', side_effect=OSError('denied')), \
                caplog.at_level(logging.ERROR):
            mqtt_publisher._handle_command({'action': 'reboot'})
            assert mqtt_publisher.io_flush()
        assert any('Error running reboot: denied' in r.message for r in caplog.records)

    def test_reboot_logs_nonzero_returncode(self, mqtt_publisher, mock_owl,
                                            caplog):
        import logging
        result = MagicMock(returncode=1, stdout='',
                           stderr='sudo: a password is required')
        with patch('subprocess.run', return_value=result), \
             caplog.at_level(logging.ERROR):
            mqtt_publisher._handle_command({'action': 'reboot'})
            mqtt_publisher.io_flush()
        assert any('password is required' in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# unknown action fallthrough
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestUnknownAction:
    """Unknown actions must warn loudly — silent no-ops are field failures."""

    def test_unknown_action_logs_warning(self, mqtt_publisher, mock_owl, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            mqtt_publisher._handle_command({'action': 'frobnicate_the_sprayer'})
        assert any('frobnicate_the_sprayer' in r.message for r in caplog.records)

    def test_unknown_action_changes_no_state(self, mqtt_publisher, mock_owl):
        before = json.dumps({k: v for k, v in mqtt_publisher.state.items() if k != 'last_update'},
                            sort_keys=True, default=str)
        mqtt_publisher._handle_command({'action': 'no_such_action', 'value': True})
        after = json.dumps({k: v for k, v in mqtt_publisher.state.items() if k != 'last_update'},
                           sort_keys=True, default=str)
        assert after == before


# ---------------------------------------------------------------------------
# transfer_session method dispatch (POST default, PUT for presigned URLs)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestTransferSessionMethod:
    """Method validation for the transfer_session command. (This class was
    named TestTransferSessionDispatch, as is the one further down, so Python
    kept only the second and these three never ran.)"""

    def test_default_method_is_post(self, mqtt_publisher, mock_owl):
        with patch('threading.Thread') as mock_thread:
            mqtt_publisher._handle_command({
                'action': 'transfer_session',
                'session_id': '20260611/session_083015',
                'upload_url': 'https://controller/api/receive',
            })
        mock_thread.assert_called_once()
        args = mock_thread.call_args.kwargs['args']
        assert args[3] == 'POST'

    def test_put_method_passed_through(self, mqtt_publisher, mock_owl):
        with patch('threading.Thread') as mock_thread:
            mqtt_publisher._handle_command({
                'action': 'transfer_session',
                'session_id': '20260611/session_083015',
                'upload_url': 'https://storage.example.com/presigned',
                'method': 'put',
            })
        mock_thread.assert_called_once()
        args = mock_thread.call_args.kwargs['args']
        assert args[3] == 'PUT'

    def test_invalid_method_rejected(self, mqtt_publisher, mock_owl):
        with patch('threading.Thread') as mock_thread:
            mqtt_publisher._handle_command({
                'action': 'transfer_session',
                'session_id': '20260611/session_083015',
                'upload_url': 'https://x/y',
                'method': 'DELETE',
            })
        mock_thread.assert_not_called()


# ---------------------------------------------------------------------------
# GPS update handler (owl/{id}/gps topic) — fail-safe + clock-skew immunity
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGPSUpdateHandler:
    """_handle_gps_update must store payloads as-received and stamp local time."""

    def test_minimal_payload_invents_nothing(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_gps_update({'latitude': -33.7, 'longitude': 151.1})

        gps = mqtt_publisher.get_gps_data()
        assert gps['latitude'] == -33.7
        assert gps['longitude'] == 151.1
        # No invented accuracy/timestamp keys in the stored payload
        assert 'accuracy' not in mqtt_publisher.state['gps_payload']
        assert 'timestamp' not in mqtt_publisher.state['gps_payload']

    def test_payload_without_coordinates_ignored(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_gps_update({'accuracy': 5.0})
        assert mqtt_publisher.state['gps_available'] is False
        assert mqtt_publisher.get_gps_data() is None

    def test_received_at_uses_local_clock(self, mqtt_publisher, mock_owl):
        """A sender timestamp 100s in the past (clock skew) must not poison staleness."""
        before = time.time()
        mqtt_publisher._handle_gps_update({
            'latitude': -33.7, 'longitude': 151.1,
            'timestamp': time.time() - 100,  # skewed sender clock
        })
        gps = mqtt_publisher.get_gps_data()
        assert gps['received_at'] >= before
        # Original sender timestamp preserved for EXIF use
        assert gps['timestamp'] < before - 90

    def test_full_controller_payload_passes_through(self, mqtt_publisher, mock_owl):
        payload = {
            'latitude': -33.7853, 'longitude': 151.1234, 'accuracy': 0.8,
            'hdop': 0.8, 'altitude': 51.2, 'speed_kmh': 7.2, 'heading': 90.0,
            'satellites': 10, 'utc_time': '012345.00', 'utc_date': '110626',
            'timestamp': time.time(),
        }
        mqtt_publisher._handle_gps_update(payload)
        gps = mqtt_publisher.get_gps_data()
        for key, value in payload.items():
            assert gps[key] == value

    def test_legacy_flat_keys_updated(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_gps_update({
            'latitude': -33.7, 'longitude': 151.1, 'accuracy': 2.5,
        })
        assert mqtt_publisher.state['gps_latitude'] == -33.7
        assert mqtt_publisher.state['gps_longitude'] == 151.1
        assert mqtt_publisher.state['gps_accuracy'] == 2.5


@pytest.mark.unit
class TestGetSessionMetadata:

    def test_returns_copy(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state['session_metadata'] = {'field_name': 'North', 'crop': 'wheat',
                                                    'weather': '', 'vehicle': ''}
        metadata = mqtt_publisher.get_session_metadata()
        assert metadata['field_name'] == 'North'
        metadata['field_name'] = 'mutated'
        assert mqtt_publisher.state['session_metadata']['field_name'] == 'North'


# ---------------------------------------------------------------------------
# transfer_session multipart + upload_previews dispatch (Noktura CR-2/CR-3)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestTransferSessionDispatch:

    def test_dispatches_with_upload_object_and_request_id(self, mqtt_publisher):
        upload = {'upload_id': 'uid-1', 'part_size': 104857600,
                  'parts': [{'part_number': 1, 'url': 'https://s3/p1'}]}
        with patch('utils.mqtt_manager.threading.Thread') as mock_thread:
            mqtt_publisher._handle_command({
                'action': 'transfer_session',
                'session_id': '20260611/session_083015',
                'data_types': ['images'],
                'request_id': 'req-1',
                'upload': upload,
            })
        kwargs = mock_thread.call_args.kwargs
        assert kwargs['target'] == mqtt_publisher._upload_session
        assert kwargs['args'][0] == '20260611/session_083015'
        assert kwargs['kwargs'] == {'request_id': 'req-1', 'upload': upload}

    def test_multipart_without_upload_url_is_accepted(self, mqtt_publisher):
        """Multipart commands carry part URLs instead of a single upload_url."""
        upload = {'upload_id': 'uid-1', 'part_size': 1000,
                  'parts': [{'part_number': 1, 'url': 'https://s3/p1'}]}
        with patch('utils.mqtt_manager.threading.Thread') as mock_thread:
            mqtt_publisher._handle_command({
                'action': 'transfer_session',
                'session_id': '20260611',
                'method': 'PUT',
                'upload': upload,
            })
        mock_thread.assert_called_once()
        assert mock_thread.call_args.kwargs['kwargs'] == {'request_id': '', 'upload': upload}

    def test_invalid_upload_object_rejected(self, mqtt_publisher):
        """Missing parts list means no thread is spawned."""
        with patch('utils.mqtt_manager.threading.Thread') as mock_thread:
            mqtt_publisher._handle_command({
                'action': 'transfer_session',
                'session_id': '20260611',
                'upload': {'upload_id': 'uid-1', 'part_size': 1000, 'parts': []},
            })
        assert not mock_thread.called

    def test_plain_transfer_still_dispatches(self, mqtt_publisher):
        """Legacy single-URL command shape keeps working."""
        with patch('utils.mqtt_manager.threading.Thread') as mock_thread:
            mqtt_publisher._handle_command({
                'action': 'transfer_session',
                'session_id': '20260611',
                'upload_url': 'https://example.com/upload',
                'method': 'PUT',
            })
        kwargs = mock_thread.call_args.kwargs
        assert kwargs['target'] == mqtt_publisher._upload_session
        assert kwargs['kwargs'] == {'request_id': '', 'upload': None}


@pytest.mark.unit
class TestUploadPreviewsDispatch:

    def test_dispatches_with_args(self, mqtt_publisher):
        urls = ['https://s3/p1', 'https://s3/p2']
        with patch('utils.mqtt_manager.threading.Thread') as mock_thread:
            mqtt_publisher._handle_command({
                'action': 'upload_previews',
                'request_id': 'req-1',
                'session_id': '20260611/session_083015',
                'count': 2,
                'max_dimension': 800,
                'upload_urls': urls,
            })
        kwargs = mock_thread.call_args.kwargs
        assert kwargs['target'] == mqtt_publisher._upload_previews
        assert kwargs['args'] == ('req-1', '20260611/session_083015', 2, 800, urls)

    def test_missing_urls_rejected(self, mqtt_publisher):
        with patch('utils.mqtt_manager.threading.Thread') as mock_thread:
            mqtt_publisher._handle_command({
                'action': 'upload_previews',
                'request_id': 'req-1',
                'session_id': '20260611/session_083015',
                'count': 2,
            })
        assert not mock_thread.called

    def test_invalid_count_rejected(self, mqtt_publisher):
        with patch('utils.mqtt_manager.threading.Thread') as mock_thread:
            mqtt_publisher._handle_command({
                'action': 'upload_previews',
                'session_id': '20260611/session_083015',
                'count': 'lots',
                'upload_urls': ['https://s3/p1'],
            })
        assert not mock_thread.called


# ---------------------------------------------------------------------------
# set_config_section - live camera controls (white balance + exposure)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCameraLiveControls:
    """CAMERA_LIVE_KEYS hot-apply branch in _handle_set_config_section.

    The branch must run BEFORE the generic setattr fallback: that ordering
    is what makes exp_compensation live (previously a silent no-op until
    restart)."""

    def _camera_cmd(self, mqtt_publisher, params):
        mqtt_publisher._handle_command({
            'action': 'set_config_section',
            'section': 'Camera',
            'params': params,
        })

    def test_manual_mode_applies_coerced_types(self, mqtt_publisher, mock_owl):
        self._camera_cmd(mqtt_publisher, {
            'awb_mode': 'Manual', 'awb_red_gain': '1.5', 'awb_blue_gain': 3})
        mock_owl.cam.set_camera_controls.assert_called_once_with(
            {'awb_mode': 'manual', 'awb_red_gain': 1.5, 'awb_blue_gain': 3.0})
        assert mock_owl.awb_mode == 'manual'
        assert mock_owl.awb_red_gain == 1.5
        assert mock_owl.config.get('Camera', 'awb_mode') == 'manual'
        assert mock_owl.config.get('Camera', 'awb_red_gain') == '1.5'
        # Live keys never raise the restart-required notice
        assert not mqtt_publisher.state.get('restart_required')

    def test_exp_compensation_applies_live(self, mqtt_publisher, mock_owl):
        """Regression: exp_compensation used to fall into the generic
        setattr branch and silently do nothing until restart."""
        self._camera_cmd(mqtt_publisher, {'exp_compensation': '3'})
        mock_owl.cam.set_camera_controls.assert_called_once_with(
            {'exp_compensation': 3})

    def test_invalid_awb_mode_rejected_not_persisted(self, mqtt_publisher, mock_owl):
        self._camera_cmd(mqtt_publisher, {'awb_mode': 'sunset'})
        mock_owl.cam.set_camera_controls.assert_not_called()
        # A value that fails validation must never reach the config object -
        # it would crash the next boot's load_and_validate_config.
        assert not mock_owl.config.has_option('Camera', 'awb_mode') or \
            mock_owl.config.get('Camera', 'awb_mode') != 'sunset'

    def test_gains_clamped_to_valid_range(self, mqtt_publisher, mock_owl):
        self._camera_cmd(mqtt_publisher, {'awb_red_gain': '99', 'awb_blue_gain': '0'})
        mock_owl.cam.set_camera_controls.assert_called_once_with(
            {'awb_red_gain': 8.0, 'awb_blue_gain': 0.1})
        # The CLAMPED values persist, so the file stays loadable next boot
        assert mock_owl.config.get('Camera', 'awb_red_gain') == '8.0'
        assert mock_owl.config.get('Camera', 'awb_blue_gain') == '0.1'

    def test_cam_without_live_support_no_exception(self, mqtt_publisher, mock_owl):
        del mock_owl.cam.set_camera_controls  # MagicMock: removes the attr
        self._camera_cmd(mqtt_publisher, {'awb_mode': 'auto'})
        # No exception; value still set on the instance + config
        assert mock_owl.awb_mode == 'auto'
        assert mock_owl.config.get('Camera', 'awb_mode') == 'auto'

    def test_set_controls_error_never_kills_handler(self, mqtt_publisher, mock_owl):
        mock_owl.cam.set_camera_controls.side_effect = RuntimeError('camera gone')
        self._camera_cmd(mqtt_publisher, {'awb_mode': 'auto'})
        assert mock_owl.config.get('Camera', 'awb_mode') == 'auto'

    def test_gains_while_preset_persist_and_apply(self, mqtt_publisher, mock_owl):
        """Gains sent while a preset is active are persisted and forwarded;
        build_awb_controls ignores them until awb_mode = manual."""
        self._camera_cmd(mqtt_publisher, {'awb_mode': 'daylight',
                                          'awb_red_gain': '2.5'})
        mock_owl.cam.set_camera_controls.assert_called_once_with(
            {'awb_mode': 'daylight', 'awb_red_gain': 2.5})
        assert mock_owl.config.get('Camera', 'awb_red_gain') == '2.5'


@pytest.mark.unit
class TestCalibrateAwb:
    """calibrate_awb: sample auto white balance, lock as manual gains.

    Runs in a worker thread (dispatch holds state_lock); the worker is
    called directly here with time.sleep patched out. Gains come from the
    Owl-held frame metadata, never from a consuming read_with_metadata()."""

    def _run(self, mqtt_publisher):
        with patch('utils.mqtt_manager.time.sleep'), \
                patch.object(mqtt_publisher, '_handle_save_config') as save:
            mqtt_publisher._calibrate_awb()
        return save

    def _seed(self, mock_owl, mode='auto'):
        mock_owl.awb_mode = mode
        mock_owl.awb_red_gain = 2.0
        mock_owl.awb_blue_gain = 2.0

    def test_locks_sampled_gains_as_manual(self, mqtt_publisher, mock_owl):
        self._seed(mock_owl)
        mock_owl._last_camera_metadata = {
            'ColourGains': (1.4321, 3.2), 'ColourTemperature': 5200, 'Lux': 800}
        save = self._run(mqtt_publisher)

        calls = mock_owl.cam.set_camera_controls.call_args_list
        assert calls[0].args[0] == {'awb_mode': 'auto'}
        assert calls[-1].args[0] == {
            'awb_mode': 'manual', 'awb_red_gain': 1.432, 'awb_blue_gain': 3.2}
        assert mock_owl.config.get('Camera', 'awb_mode') == 'manual'
        assert mock_owl.config.get('Camera', 'awb_red_gain') == '1.432'
        assert mock_owl.config.get('Camera', 'awb_blue_gain') == '3.2'
        # Camera keys are not auto-saved by set_config_section: the lock
        # must be written so it survives a restart.
        save.assert_called_once()
        st = mqtt_publisher.state['awb_calibration']
        assert st['status'] == 'complete'
        assert st['red_gain'] == 1.432
        assert st['blue_gain'] == 3.2
        assert st['colour_temperature'] == 5200
        assert st['updated_at'] > 0
        assert st['error'] == ''

    def test_no_metadata_restores_previous_wb(self, mqtt_publisher, mock_owl):
        self._seed(mock_owl, mode='daylight')
        mock_owl._last_camera_metadata = None
        save = self._run(mqtt_publisher)

        st = mqtt_publisher.state['awb_calibration']
        assert st['status'] == 'error'
        assert 'ColourGains' in st['error']
        # Previous mode is put back on the running camera
        assert mock_owl.cam.set_camera_controls.call_args_list[-1].args[0] == {
            'awb_mode': 'daylight', 'awb_red_gain': 2.0, 'awb_blue_gain': 2.0}
        save.assert_not_called()
        assert mock_owl.config.get('Camera', 'awb_mode', fallback='') != 'manual'

    def test_backend_without_live_controls(self, mqtt_publisher, mock_owl):
        self._seed(mock_owl)
        del mock_owl.cam.set_camera_controls
        save = self._run(mqtt_publisher)
        assert mqtt_publisher.state['awb_calibration']['status'] == 'error'
        save.assert_not_called()

    def test_command_dispatch_spawns_worker(self, mqtt_publisher):
        with patch('utils.mqtt_manager.threading.Thread') as thread:
            mqtt_publisher._handle_command({'action': 'calibrate_awb'})
        thread.assert_called_once()
        assert thread.call_args.kwargs['target'] == mqtt_publisher._calibrate_awb
        assert mqtt_publisher.state['awb_calibration']['status'] == 'running'

    def test_command_ignored_while_running(self, mqtt_publisher):
        mqtt_publisher.state['awb_calibration']['status'] = 'running'
        with patch('utils.mqtt_manager.threading.Thread') as thread:
            mqtt_publisher._handle_command({'action': 'calibrate_awb'})
        thread.assert_not_called()

    def test_state_seeds_idle(self, mqtt_publisher):
        st = mqtt_publisher.state['awb_calibration']
        assert st['status'] == 'idle'
        assert st['red_gain'] is None
        json.dumps(mqtt_publisher.state)


@pytest.mark.unit
class TestRefreshCameraState:
    """state['camera'] from configured WB + latest frame metadata. Metadata
    keys are omitted, never defaulted, when unavailable (webcams)."""

    def test_with_metadata(self, mqtt_publisher, mock_owl):
        mock_owl.awb_mode = 'manual'
        mock_owl.awb_red_gain = 1.5
        mock_owl.awb_blue_gain = 2.5
        mock_owl._last_camera_metadata = {
            'ColourGains': (1.4, 3.2), 'ColourTemperature': 5200, 'Lux': 812.34}
        mqtt_publisher._refresh_camera_state()
        cam = mqtt_publisher.state['camera']
        assert cam == {'awb_mode': 'manual', 'awb_red_gain': 1.5,
                       'awb_blue_gain': 2.5, 'colour_gains': [1.4, 3.2],
                       'colour_temperature': 5200.0, 'lux': 812.3}
        json.dumps(mqtt_publisher.state)

    def test_without_metadata_omits_measured_keys(self, mqtt_publisher, mock_owl):
        mock_owl.awb_mode = 'auto'
        mock_owl.awb_red_gain = 2.0
        mock_owl.awb_blue_gain = 2.0
        mock_owl._last_camera_metadata = None
        mqtt_publisher._refresh_camera_state()
        cam = mqtt_publisher.state['camera']
        assert cam['awb_mode'] == 'auto'
        assert 'colour_gains' not in cam
        assert 'colour_temperature' not in cam
        assert 'lux' not in cam

    def test_an_owl_without_white_balance_values_publishes_none_of_them(self, mqtt_publisher, mock_owl):
        """mock_owl has no awb_* set (MagicMock attributes): nothing that is
        not a real value may reach the published JSON."""
        mqtt_publisher._refresh_camera_state()
        published = json.loads(json.dumps(mqtt_publisher.state))['camera']
        assert published == {'awb_mode': None, 'awb_red_gain': None, 'awb_blue_gain': None}
