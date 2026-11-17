"""The LUT profile MQTT command handlers (set_lut_profile,
set_lut_sensitivity, delete_lut_profile): validation, state and what is
queued for the detection loop.

What the detection loop does with what is queued (profile and algorithm
arriving together, re-bake on a sensitivity change) is asserted on a real
Owl in tests/owl/test_mqtt_commands_at_the_relay.py. Eight tests here used
to assert it by searching owl.py for string literals."""

import logging
from unittest.mock import MagicMock

import pytest


@pytest.mark.unit
class TestSetLutProfile:
    def test_queues_pending_and_updates_state(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_lut_profile', 'name': 'Paddock_One'})
        assert mqtt_publisher.state['lut_profile'] == 'paddock_one'
        assert mock_owl._pending_lut_profile == 'paddock_one'

    def test_mirrors_into_config(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_lut_profile', 'name': 'padd'})
        assert mock_owl.config.get('GreenOnBrown', 'lut_profile') == 'padd'

    def test_missing_name_ignored(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state['lut_profile'] = 'keep'
        mqtt_publisher._handle_command({'action': 'set_lut_profile', 'name': ''})
        assert mqtt_publisher.state['lut_profile'] == 'keep'


@pytest.mark.unit
class TestSetLutSensitivity:
    def test_queues_pending_and_updates_state(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_lut_sensitivity', 'value': 72})
        assert mqtt_publisher.state['lut_sensitivity'] == 72
        assert mock_owl._pending_lut_sensitivity == 72

    def test_clamped_to_range(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_lut_sensitivity', 'value': 400})
        assert mqtt_publisher.state['lut_sensitivity'] == 100
        mqtt_publisher._handle_command({'action': 'set_lut_sensitivity', 'value': -5})
        assert mqtt_publisher.state['lut_sensitivity'] == 0

    def test_invalid_value_ignored(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state['lut_sensitivity'] = 33
        mqtt_publisher._handle_command({'action': 'set_lut_sensitivity', 'value': 'abc'})
        assert mqtt_publisher.state['lut_sensitivity'] == 33


@pytest.mark.unit
class TestDeleteLutProfile:
    def test_delete_calls_manager(self, mqtt_publisher, mock_owl):
        mock_owl.lut_manager = MagicMock()
        mqtt_publisher._handle_command({'action': 'delete_lut_profile', 'name': 'gone'})
        mock_owl.lut_manager.delete.assert_called_once_with('gone')

    def test_a_failed_delete_is_logged_and_the_selection_kept(self, mqtt_publisher, mock_owl, caplog):
        from utils.lut_manager import LUTProfileError
        mock_owl.lut_manager = MagicMock()
        mock_owl.lut_manager.delete.side_effect = LUTProfileError('missing')
        mqtt_publisher.state['lut_profile'] = 'ghost'
        with caplog.at_level(logging.ERROR):
            mqtt_publisher._handle_command({'action': 'delete_lut_profile', 'name': 'Ghost'})
        mock_owl.lut_manager.delete.assert_called_once_with('ghost')
        assert any("Failed to delete LUT profile 'ghost': missing" in r.message
                   for r in caplog.records)
        assert mqtt_publisher.state['lut_profile'] == 'ghost'

    def test_delete_without_a_name_deletes_nothing(self, mqtt_publisher, mock_owl):
        mock_owl.lut_manager = MagicMock()
        mqtt_publisher._handle_command({'action': 'delete_lut_profile', 'name': '  '})
        mock_owl.lut_manager.delete.assert_not_called()


@pytest.mark.unit
class TestLutSensitivityViaGenericParamPath:
    """lut_sensitivity rides the generic slider path (set_config /
    set_config_section) and must queue a re-bake, not a plain setattr."""

    def test_set_config_routes_to_pending(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command(
            {'action': 'set_config', 'key': 'lut_sensitivity', 'value': '64'})
        assert mock_owl._pending_lut_sensitivity == 64
        assert mqtt_publisher.state['lut_sensitivity'] == 64

    def test_clamped(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command(
            {'action': 'set_config', 'key': 'lut_sensitivity', 'value': '400'})
        assert mock_owl._pending_lut_sensitivity == 100


@pytest.mark.unit
class TestMinDetectionAreaPercent:
    def test_set_config_updates_float(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command(
            {'action': 'set_config', 'key': 'min_detection_area_percent',
             'value': '0.0123'})
        assert mock_owl.min_detection_area_percent == pytest.approx(0.0123)

    def test_clamped_to_range(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command(
            {'action': 'set_config', 'key': 'min_detection_area_percent',
             'value': '99'})
        assert mock_owl.min_detection_area_percent == 5.0


@pytest.mark.unit
class TestSetAlgorithmLut:
    def test_lut_is_a_valid_algorithm(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_algorithm', 'value': 'lut'})
        assert mqtt_publisher.state['algorithm_pending'] == 'lut'
        assert mock_owl._pending_algorithm == 'lut'
