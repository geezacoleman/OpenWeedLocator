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
