"""v3.22 hardening on the MQTT side (OWLMQTTPublisher, DashMQTTSubscriber).

Each test names the field failure it guards. Uses the mock_owl /
mqtt_publisher fixtures from tests/conftest.py (MagicMock Owl, mock client).
"""

import json
from unittest.mock import MagicMock

import pytest


def published_states(publisher):
    return [json.loads(c.args[1]) for c in publisher.client.publish.call_args_list
            if c.args and c.args[0] == publisher.topics['state']]


class TestFaultState:
    def test_set_fault_publishes_and_clears(self, mqtt_publisher):
        fault = {'code': 'camera_lost', 'message': 'No frame', 'action': 'nozzles_off', 'since': 1.0}
        mqtt_publisher.set_fault(fault)
        assert mqtt_publisher.state['fault'] == fault
        assert published_states(mqtt_publisher)[-1]['fault']['code'] == 'camera_lost'

        mqtt_publisher.set_fault(None)
        assert mqtt_publisher.state['fault'] is None


class TestChangeGuards:
    """Ute/Advanced units call these every 0.1 s state-sync cycle; each call
    used to publish the full state (~20 publishes a second)."""

    def test_unchanged_detection_enable_does_not_publish(self, mqtt_publisher):
        mqtt_publisher.set_detection_enable(True)
        count = mqtt_publisher.client.publish.call_count
        mqtt_publisher.set_detection_enable(True)
        assert mqtt_publisher.client.publish.call_count == count

    def test_unchanged_image_sample_enable_does_not_publish(self, mqtt_publisher):
        mqtt_publisher.set_image_sample_enable(True)
        count = mqtt_publisher.client.publish.call_count
        mqtt_publisher.set_image_sample_enable(True)
        assert mqtt_publisher.client.publish.call_count == count


class TestBlanketMode:
    def test_detection_toggle_leaves_blanket_and_turns_nozzles_off(self, mqtt_publisher, mock_owl):
        """all_on bypasses the relay workers, so nothing switched the nozzles
        off when detection was toggled instead of the mode."""
        mqtt_publisher._handle_command({'action': 'set_detection_mode', 'value': 2})
        mock_owl.relay_controller.relay.all_off.reset_mock()

        mqtt_publisher._handle_command({'action': 'set_detection_enable', 'value': True})

        mock_owl.relay_controller.relay.all_off.assert_called_once()
        assert mqtt_publisher.state['detection_mode'] == 0

    def test_blanket_refused_while_first_boot_flag_armed(self, mqtt_publisher, mock_owl):
        mock_owl._firstboot_active = True
        mqtt_publisher._handle_command({'action': 'set_detection_mode', 'value': 2})
        mock_owl.relay_controller.relay.all_on.assert_not_called()


class TestGPSPayloadValidation:
    def test_non_numeric_field_is_rejected_not_stored(self, mqtt_publisher):
        """A stored string field raised on every state-sync cycle downstream."""
        mqtt_publisher._handle_gps_update({'latitude': 51.0, 'longitude': 4.0,
                                           'accuracy': 'high'})
        assert mqtt_publisher.state.get('gps_payload') is None

    def test_numeric_strings_are_stored_as_numbers(self, mqtt_publisher):
        mqtt_publisher._handle_gps_update({'latitude': '51.5', 'longitude': '4.25',
                                           'speed_kmh': '6'})
        payload = mqtt_publisher.state['gps_payload']
        assert payload['latitude'] == 51.5 and payload['speed_kmh'] == 6.0


class TestConfigSectionRuntimeObjects:
    def test_shared_values_are_not_replaced_by_strings(self, mqtt_publisher, mock_owl):
        shared = MagicMock(spec=['get_lock', 'value'])
        mock_owl.image_sample_enable = shared
        mqtt_publisher._handle_set_config_section('DataCollection',
                                                  {'image_sample_enable': 'False'})
        assert mock_owl.image_sample_enable is shared

    def test_save_directory_needs_a_restart(self, mqtt_publisher, mock_owl):
        mock_owl.save_directory = '/media/owl/usb'
        mqtt_publisher._handle_set_config_section('DataCollection',
                                                  {'save_directory': 'elsewhere'})
        assert mock_owl.save_directory == '/media/owl/usb'
        assert 'save_directory' in mqtt_publisher.state['restart_required']

    def test_greenongreen_model_change_flags_restart(self, mqtt_publisher):
        """Only confidence applies live; other keys used to log 'restart
        required' without telling the dashboard."""
        mqtt_publisher._handle_set_config_section('GreenOnGreen',
                                                  {'model_path': 'models/other'})
        assert 'model_path' in mqtt_publisher.state['restart_required']


class TestDashSubscriber:
    @pytest.fixture
    def subscriber(self):
        from utils.mqtt_manager import DashMQTTSubscriber
        sub = DashMQTTSubscriber(broker_host='localhost', broker_port=1883)
        sub.client = MagicMock()
        sub.connected = True
        return sub

    def test_stopped_owl_reports_detection_and_recording_off(self, subscriber):
        subscriber.current_state.update(detection_enable=True, image_sample_enable=True)
        subscriber.last_heartbeat = 0  # long ago
        state = subscriber.get_state()
        assert state['detection_enable'] is False and state['image_sample_enable'] is False

    def test_set_config_on_other_sections_becomes_set_config_section(self, subscriber):
        """The OWL's set_config ignores `section`: agent changes to [System]
        were dropped while the tool reported success."""
        subscriber._send_command('set_config', section='System', key='on_error', value='on')
        sent = json.loads(subscriber.client.publish.call_args.args[1])
        assert sent == {'action': 'set_config_section', 'section': 'System',
                        'params': {'on_error': 'on'}}
