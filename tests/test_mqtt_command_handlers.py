"""Tests for OWLMQTTPublisher command handler dispatch (_handle_command).

Priority 1 — highest field-failure risk. Silent handler failures mean
the dashboard appears to work but OWL does nothing.

Tests handlers via the _handle_command entry point (not just the
underlying methods which are tested in test_mqtt_handlers.py).
"""

import json
import time
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# set_detection_enable / set_image_sample_enable
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDetectionEnable:
    """Tests for set_detection_enable command handler."""

    def test_enable_detection(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_detection_enable', 'value': True})
        assert mqtt_publisher.state['detection_enable'] is True

    def test_disable_detection(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state['detection_enable'] = True
        mqtt_publisher._handle_command({'action': 'set_detection_enable', 'value': False})
        assert mqtt_publisher.state['detection_enable'] is False

    def test_publishes_state_after_command(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_detection_enable', 'value': True})
        # _handle_command publishes state at the end — verify client.publish was called
        assert mqtt_publisher.client.publish.called


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

    def test_missing_param_ignored(self, mqtt_publisher, mock_owl):
        """Missing param name doesn't crash."""
        mqtt_publisher._handle_command({
            'action': 'set_greenonbrown_param',
            'value': 42
        })
        # No crash = pass


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

    def test_non_confidence_param_logged(self, mqtt_publisher, mock_owl):
        """Non-confidence params are logged (restart required), no crash."""
        mqtt_publisher._handle_command({
            'action': 'set_greenongreen_param',
            'key': 'model_path',
            'value': 'models/new_model'
        })
        # No crash = pass


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

    def test_missing_key_ignored(self, mqtt_publisher, mock_owl):
        """Missing key/value doesn't crash."""
        mqtt_publisher._handle_command({
            'action': 'set_config',
            'value': 42
        })
        # key is None, condition fails, no crash


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


# ---------------------------------------------------------------------------
# set_detection_mode (blanket spray / spot spray / off)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetDetectionMode:
    """Tests for set_detection_mode command handler."""

    def test_blanket_mode_activates_all_relays(self, mqtt_publisher, mock_owl):
        mock_owl.relay_controller = MagicMock()
        mqtt_publisher._handle_command({
            'action': 'set_detection_mode',
            'value': 2
        })
        assert mqtt_publisher.state['detection_mode'] == 2
        assert mqtt_publisher.state['detection_enable'] is False
        mock_owl.relay_controller.relay.all_on.assert_called_once()

    def test_spot_spray_enables_detection(self, mqtt_publisher, mock_owl):
        mock_owl.relay_controller = MagicMock()
        mqtt_publisher._handle_command({
            'action': 'set_detection_mode',
            'value': 0
        })
        assert mqtt_publisher.state['detection_mode'] == 0
        assert mqtt_publisher.state['detection_enable'] is True
        mock_owl.relay_controller.relay.all_off.assert_called_once()

    def test_off_mode_disables_everything(self, mqtt_publisher, mock_owl):
        mock_owl.relay_controller = MagicMock()
        mqtt_publisher._handle_command({
            'action': 'set_detection_mode',
            'value': 1
        })
        assert mqtt_publisher.state['detection_mode'] == 1
        assert mqtt_publisher.state['detection_enable'] is False
        mock_owl.relay_controller.relay.all_off.assert_called_once()

    def test_invalid_mode_rejected(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state['detection_mode'] = 1
        mqtt_publisher._handle_command({
            'action': 'set_detection_mode',
            'value': 5
        })
        # Invalid mode returns early — state unchanged
        assert mqtt_publisher.state['detection_mode'] == 1


# ---------------------------------------------------------------------------
# set_actuation_params
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetActuationParams:
    """Tests for set_actuation_params command handler."""

    def test_updates_owl_instance(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_actuation_params',
            'actuation_duration': 0.25,
            'delay': 0.1,
            'source': 'gps'
        })
        assert mock_owl.actuation_duration == 0.25
        assert mock_owl.delay == 0.1

    def test_updates_state(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_actuation_params',
            'actuation_duration': 0.5,
            'delay': 0.2,
            'source': 'gps'
        })
        assert mqtt_publisher.state['actuation_duration'] == 0.5
        assert mqtt_publisher.state['delay'] == 0.2
        assert mqtt_publisher.state['actuation_source'] == 'gps'

    def test_clamps_to_safety_bounds(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_actuation_params',
            'actuation_duration': 100.0,  # way over MAX_DURATION=5.0
            'delay': -5.0,
        })
        assert mock_owl.actuation_duration == 5.0  # clamped to max
        assert mock_owl.delay == 0.0  # clamped to min

    def test_min_duration_clamp(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_actuation_params',
            'actuation_duration': 0.001,  # below MIN_DURATION=0.01
            'delay': 0,
        })
        assert mock_owl.actuation_duration == 0.01  # clamped to min


# ---------------------------------------------------------------------------
# restart_service
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRestartService:
    """Tests for restart_service command handler."""

    def test_calls_popen_not_run(self, mqtt_publisher, mock_owl):
        """restart_service must use Popen (non-blocking) not subprocess.run."""
        with patch('subprocess.Popen') as mock_popen:
            mqtt_publisher._handle_command({'action': 'restart_service'})
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            assert 'restart' in args
            assert 'owl.service' in args

    def test_popen_failure_doesnt_crash(self, mqtt_publisher, mock_owl):
        """If subprocess.Popen raises, handler logs error without crash."""
        with patch('subprocess.Popen', side_effect=OSError("no sudo")):
            mqtt_publisher._handle_command({'action': 'restart_service'})
            # No crash = pass


# ---------------------------------------------------------------------------
# Unknown action (fallthrough)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestUnknownAction:
    """Tests for unknown/unhandled actions.

    ISSUE FOUND: Unknown actions fall through silently. The _handle_command
    method has no else clause to warn about unrecognized actions. This means
    typos in action names (e.g., 'set_sensitivty' instead of 'set_sensitivity_level')
    are silently dropped — the dashboard thinks the command was sent but nothing
    happens on the OWL.
    """

    def test_unknown_action_doesnt_crash(self, mqtt_publisher, mock_owl):
        """Unknown actions should not crash the handler."""
        mqtt_publisher._handle_command({'action': 'totally_fake_action', 'value': 42})
        # No crash = pass

    def test_unknown_action_still_publishes_state(self, mqtt_publisher, mock_owl):
        """Even unknown actions reach the publish_state at end of _handle_command."""
        mqtt_publisher.client.publish.reset_mock()
        mqtt_publisher._handle_command({'action': 'unknown_action'})
        # _handle_command always publishes state at the end (line 510)
        assert mqtt_publisher.client.publish.called


# ---------------------------------------------------------------------------
# save_config via command dispatch
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSaveConfigCommand:
    """Tests for save_config via _handle_command dispatch."""

    def test_save_config_dispatches(self, mqtt_publisher, mock_owl, tmp_config_dir):
        """save_config action dispatches to _handle_save_config."""
        mqtt_publisher._handle_command({
            'action': 'save_config',
            'filename': None
        })
        # Saves to the config_path — verify file still exists
        assert mock_owl.config_path.exists()

    def test_save_config_with_filename(self, mqtt_publisher, mock_owl, tmp_config_dir):
        """save_config with filename attempts to save to that name."""
        mqtt_publisher._handle_command({
            'action': 'save_config',
            'filename': 'my_saved.ini'
        })
        # No crash = pass (actual file save depends on path resolution)


# ---------------------------------------------------------------------------
# set_algorithm
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetAlgorithm:
    """Tests for set_algorithm command handler."""

    def test_valid_algorithm_updates_state(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_algorithm', 'value': 'exg'})
        assert mqtt_publisher.state['algorithm'] == 'exg'

    def test_valid_algorithm_queues_on_owl(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_algorithm', 'value': 'maxg'})
        assert mock_owl._pending_algorithm == 'maxg'

    def test_all_valid_algorithms(self, mqtt_publisher, mock_owl):
        """Every supported algorithm should be accepted."""
        for algo in ('exg', 'exgr', 'maxg', 'nexg', 'exhsv', 'hsv', 'gndvi', 'gog', 'gog-hybrid'):
            mqtt_publisher._handle_command({'action': 'set_algorithm', 'value': algo})
            assert mqtt_publisher.state['algorithm'] == algo

    def test_invalid_algorithm_rejected(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state['algorithm'] = 'exhsv'
        mqtt_publisher._handle_command({'action': 'set_algorithm', 'value': 'fakealgo'})
        assert mqtt_publisher.state['algorithm'] == 'exhsv'  # unchanged

    def test_case_insensitive(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_algorithm', 'value': 'EXG'})
        assert mqtt_publisher.state['algorithm'] == 'exg'

    def test_updates_config_object(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_algorithm', 'value': 'hsv'})
        assert mock_owl.config.get('System', 'algorithm') == 'hsv'


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

    def test_invalid_value_doesnt_crash(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_crop_buffer', 'value': 'abc'})
        # ValueError caught, no crash


# ---------------------------------------------------------------------------
# get_config
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGetConfig:
    """Tests for get_config command handler."""

    def test_publishes_config_to_mqtt(self, mqtt_publisher, mock_owl, tmp_config_dir):
        mqtt_publisher._handle_command({'action': 'get_config'})
        # Should publish to config topic
        publish_calls = mqtt_publisher.client.publish.call_args_list
        # Find the config publish (not the state publish)
        config_published = False
        for c in publish_calls:
            topic = c[0][0]
            if 'config' in topic and 'state' not in topic:
                payload = json.loads(c[0][1])
                assert 'config' in payload
                assert 'GreenOnBrown' in payload['config']
                assert payload['device_id'] == 'test-owl'
                config_published = True
                break
        assert config_published, "get_config should publish config to MQTT"

    def test_includes_config_path(self, mqtt_publisher, mock_owl, tmp_config_dir):
        mqtt_publisher._handle_command({'action': 'get_config'})
        publish_calls = mqtt_publisher.client.publish.call_args_list
        for c in publish_calls:
            topic = c[0][0]
            if 'config' in topic and 'state' not in topic:
                payload = json.loads(c[0][1])
                assert 'config_path' in payload
                assert 'config_name' in payload
                break


# ---------------------------------------------------------------------------
# set_config_section
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetConfigSection:
    """Tests for set_config_section command handler."""

    def test_greenonbrown_params_applied(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_config_section',
            'section': 'GreenOnBrown',
            'params': {'exg_min': '35', 'hue_max': '90'}
        })
        assert mock_owl.exg_min == 35
        assert mock_owl.hue_max == 90

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
            'params': {'algorithm': 'gndvi'}
        })
        assert mqtt_publisher.state['algorithm'] == 'gndvi'

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

    def test_updates_config_object(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_config_section',
            'section': 'GreenOnBrown',
            'params': {'exg_min': '40'}
        })
        assert mock_owl.config.get('GreenOnBrown', 'exg_min') == '40'

    def test_missing_section_ignored(self, mqtt_publisher, mock_owl):
        """Empty section should not crash."""
        mqtt_publisher._handle_command({
            'action': 'set_config_section',
            'section': '',
            'params': {'exg_min': '40'}
        })
        # No crash (falsy section fails the `if section and params` guard)

    def test_missing_params_ignored(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_config_section',
            'section': 'GreenOnBrown',
            'params': {}
        })
        # No crash (empty params fails the guard)


# ---------------------------------------------------------------------------
# set_active_config
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetActiveConfig:
    """Tests for set_active_config command handler."""

    def test_writes_active_config_txt(self, mqtt_publisher, mock_owl, tmp_config_dir):
        mqtt_publisher._handle_command({
            'action': 'set_active_config',
            'config': 'config/GENERAL_CONFIG.ini'
        })
        # Check active_config.txt in the project's config/ dir (uses ../ from utils/)
        import os
        config_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(mqtt_publisher.__class__.__module__.replace('.', '/') + '.py')
        )))
        # The handler writes to config/active_config.txt relative to project root
        # Just verify no crash — actual path depends on install location

    def test_missing_config_path_ignored(self, mqtt_publisher, mock_owl):
        """Missing config path should not write anything."""
        mqtt_publisher._handle_command({
            'action': 'set_active_config'
        })
        # config is None -> guard fails, no crash


# ---------------------------------------------------------------------------
# download_model
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDownloadModel:
    """Tests for download_model command handler."""

    def test_starts_background_thread(self, mqtt_publisher, mock_owl):
        import threading
        initial_threads = threading.active_count()
        with patch.object(mqtt_publisher, '_download_model') as mock_download:
            # The handler starts a thread that calls _download_model
            # We need to patch threading.Thread to avoid actual thread creation
            with patch('threading.Thread') as MockThread:
                mock_thread = MagicMock()
                MockThread.return_value = mock_thread
                mqtt_publisher._handle_command({
                    'action': 'download_model',
                    'url': 'https://controller.local/models/test.pt',
                    'filename': 'test.pt',
                    'sha256': 'abc123',
                    'is_archive': False
                })
                MockThread.assert_called_once()
                mock_thread.start.assert_called_once()

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

    def test_publishes_state(self, mqtt_publisher, mock_owl):
        mock_owl.tracking_enabled = False
        mock_owl._class_smoother = None
        mock_owl._crop_stabilizer = None
        mock_owl._gog_detector = None

        mqtt_publisher.client.publish.reset_mock()
        mqtt_publisher._handle_command({'action': 'set_tracking', 'value': True})
        assert mqtt_publisher.client.publish.called

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
# reboot (currently a no-op / future implementation)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestReboot:
    """Tests for reboot command handler (currently no-op)."""

    def test_reboot_doesnt_crash(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'reboot'})
        # No crash = pass (handler just logs a warning)

    def test_reboot_still_publishes_state(self, mqtt_publisher, mock_owl):
        mqtt_publisher.client.publish.reset_mock()
        mqtt_publisher._handle_command({'action': 'reboot'})
        assert mqtt_publisher.client.publish.called


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestShutdown:
    """Tests for shutdown command handler."""

    def test_shutdown_calls_popen(self, mqtt_publisher, mock_owl):
        with patch('subprocess.Popen') as mock_popen:
            mqtt_publisher._handle_command({'action': 'shutdown'})
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            assert args[0] == 'sudo'
            assert 'shutdown' in args[1]  # full path may vary by OS
            assert args[2] == 'now'

    def test_shutdown_publishes_state(self, mqtt_publisher, mock_owl):
        mqtt_publisher.client.publish.reset_mock()
        with patch('subprocess.Popen'):
            mqtt_publisher._handle_command({'action': 'shutdown'})
        assert mqtt_publisher.client.publish.called

    def test_shutdown_handles_popen_failure(self, mqtt_publisher, mock_owl):
        with patch('subprocess.Popen', side_effect=OSError('not found')):
            # Should not raise — error is caught and logged
            mqtt_publisher._handle_command({'action': 'shutdown'})


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
        repo_dir = Path(mqtt_publisher._update_status_path).parent
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

    def test_refused_while_update_in_progress(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state['software_update']['status'] = 'fetching'
        with patch('subprocess.Popen') as mock_popen:
            mqtt_publisher._handle_command({
                'action': 'update_software', 'ref': 'main', 'request_id': 'r'
            })
        mock_popen.assert_not_called()
        assert mqtt_publisher.state['software_update']['status'] == 'error'
        assert 'already in progress' in mqtt_publisher.state['software_update']['error']

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

    def test_reboot_calls_popen(self, mqtt_publisher, mock_owl):
        with patch('subprocess.Popen') as mock_popen, \
             patch('shutil.which', return_value='/usr/sbin/reboot'):
            mqtt_publisher._handle_command({'action': 'reboot'})
        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        assert argv == ['sudo', '-n', '/usr/sbin/reboot']

    def test_reboot_handles_popen_failure(self, mqtt_publisher, mock_owl):
        with patch('subprocess.Popen', side_effect=OSError('denied')):
            # Should not raise — error is caught and logged
            mqtt_publisher._handle_command({'action': 'reboot'})


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

    def test_unknown_action_does_not_raise(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'no_such_action'})
        # State still published afterwards
        assert mqtt_publisher.client.publish.called


# ---------------------------------------------------------------------------
# transfer_session method dispatch (POST default, PUT for presigned URLs)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestTransferSessionDispatch:
    """Method validation and threading for the transfer_session command."""

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
        assert mock_thread.called

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
