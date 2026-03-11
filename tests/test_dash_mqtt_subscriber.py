"""Tests for DashMQTTSubscriber — standalone dashboard MQTT client.

Priority 4 — dashboard-to-OWL communication link.
"""

import json
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from utils.mqtt_manager import DashMQTTSubscriber


@pytest.fixture
def subscriber():
    """DashMQTTSubscriber with mocked MQTT client (no broker connection)."""
    with patch('utils.mqtt_manager.mqtt.Client') as MockClient:
        mock_client = MagicMock()
        MockClient.return_value = mock_client
        sub = DashMQTTSubscriber(
            broker_host='localhost',
            broker_port=1883,
            client_id='test_dashboard'
        )
        sub.connected = True
        return sub


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCommandDispatch:
    """Tests for DashMQTTSubscriber command methods."""

    def test_send_command_publishes_json(self, subscriber):
        result = subscriber._send_command('set_detection_enable', value=True)
        assert result['success'] is True
        subscriber.client.publish.assert_called_once()

        # Verify published payload
        topic, payload_str = subscriber.client.publish.call_args[0]
        payload = json.loads(payload_str)
        assert payload['action'] == 'set_detection_enable'
        assert payload['value'] is True

    def test_send_command_uses_correct_topic(self, subscriber):
        subscriber._send_command('set_algorithm', value='exhsv')
        topic = subscriber.client.publish.call_args[0][0]
        assert topic == 'owl/commands'  # standalone mode

    def test_send_command_not_connected(self, subscriber):
        subscriber.connected = False
        result = subscriber._send_command('set_detection_enable', value=True)
        assert result['success'] is False
        assert 'Not connected' in result['error']

    def test_set_detection_enable(self, subscriber):
        result = subscriber.set_detection_enable(True)
        assert result['success'] is True
        payload = json.loads(subscriber.client.publish.call_args[0][1])
        assert payload['action'] == 'set_detection_enable'

    def test_set_image_sample_enable(self, subscriber):
        result = subscriber.set_image_sample_enable(False)
        assert result['success'] is True
        payload = json.loads(subscriber.client.publish.call_args[0][1])
        assert payload['action'] == 'set_image_sample_enable'
        assert payload['value'] is False

    def test_set_sensitivity_level_valid(self, subscriber):
        result = subscriber.set_sensitivity_level('high')
        assert result['success'] is True
        payload = json.loads(subscriber.client.publish.call_args[0][1])
        assert payload['level'] == 'high'

    def test_set_sensitivity_level_custom_name(self, subscriber):
        """Custom preset names are accepted (validation happens on OWL side)."""
        result = subscriber.set_sensitivity_level('extreme')
        assert result['success'] is True
        payload = json.loads(subscriber.client.publish.call_args[0][1])
        assert payload['level'] == 'extreme'

    def test_set_greenonbrown_param_valid(self, subscriber):
        result = subscriber.set_greenonbrown_param('exg_min', 30)
        assert result['success'] is True
        payload = json.loads(subscriber.client.publish.call_args[0][1])
        assert payload['param'] == 'exg_min'
        assert payload['value'] == 30

    def test_set_greenonbrown_param_invalid(self, subscriber):
        result = subscriber.set_greenonbrown_param('invalid_param', 42)
        assert result['success'] is False
        subscriber.client.publish.assert_not_called()

    def test_set_detection_mode(self, subscriber):
        result = subscriber.set_detection_mode(2)
        assert result['success'] is True
        payload = json.loads(subscriber.client.publish.call_args[0][1])
        assert payload['action'] == 'set_detection_mode'
        assert payload['value'] == 2


# ---------------------------------------------------------------------------
# State message parsing
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestStateMessageParsing:
    """Tests for _on_message state handling."""

    def _make_msg(self, topic, payload_dict):
        msg = MagicMock()
        msg.topic = topic
        msg.payload = json.dumps(payload_dict).encode()
        return msg

    def test_state_message_updates_current_state(self, subscriber):
        state_data = {
            'detection_enable': True,
            'algorithm': 'exhsv',
            'sensitivity_level': 'high'
        }
        msg = self._make_msg('owl/state', state_data)
        subscriber._on_message(None, None, msg)

        state = subscriber.get_state()
        assert state['detection_enable'] is True
        assert state['algorithm'] == 'exhsv'

    def test_status_message_updates_owl_running(self, subscriber):
        msg = self._make_msg('owl/status', {
            'owl_running': True,
            'connected': True
        })
        subscriber._on_message(None, None, msg)

        state = subscriber.get_state()
        assert state.get('owl_running') is True
        assert state.get('connected') is True

    def test_any_message_updates_heartbeat(self, subscriber):
        subscriber.last_heartbeat = 0
        msg = self._make_msg('owl/status', {'owl_running': True})
        subscriber._on_message(None, None, msg)
        assert subscriber.last_heartbeat > 0

    def test_heartbeat_timeout_marks_owl_stopped(self, subscriber):
        subscriber.last_heartbeat = time.time() - 10  # 10s ago
        subscriber.current_state = {'owl_running': True}
        state = subscriber.get_state()
        # Heartbeat timeout (5s) exceeded — should report owl_running=False
        assert state['owl_running'] is False

    def test_indicator_weed_detected(self, subscriber):
        msg = self._make_msg('owl/indicators', {
            'type': 'weed_detected',
            'timestamp': time.time()
        })
        subscriber._on_message(None, None, msg)
        assert subscriber.get_weed_detect_indicator() is True

    def test_indicator_image_written(self, subscriber):
        msg = self._make_msg('owl/indicators', {
            'type': 'image_written',
            'timestamp': time.time()
        })
        subscriber._on_message(None, None, msg)
        assert subscriber.get_image_write_indicator() is True

    def test_error_message_appended_to_log(self, subscriber):
        msg = self._make_msg('owl/errors', {
            'message': 'Camera USB timeout',
            'timestamp': time.time()
        })
        subscriber._on_message(None, None, msg)
        errors = subscriber.get_and_clear_errors()
        assert len(errors) == 1
        assert errors[0]['message'] == 'Camera USB timeout'

    def test_get_and_clear_errors_clears(self, subscriber):
        msg = self._make_msg('owl/errors', {'message': 'test error'})
        subscriber._on_message(None, None, msg)
        subscriber.get_and_clear_errors()
        # Second call should return empty
        assert len(subscriber.get_and_clear_errors()) == 0

    def test_malformed_json_ignored(self, subscriber):
        msg = MagicMock()
        msg.topic = 'owl/state'
        msg.payload = b'not valid json {{{}'
        subscriber._on_message(None, None, msg)
        # No crash, state unchanged


# ---------------------------------------------------------------------------
# Connection callbacks
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestConnectionCallbacks:
    """Tests for _on_connect and _on_disconnect callbacks."""

    def test_on_connect_success_subscribes(self, subscriber):
        subscriber._on_connect(subscriber.client, None, None, 0)
        assert subscriber.connected is True
        # Should subscribe to 6 topics (state, status, detection, config, indicators, errors)
        assert subscriber.client.subscribe.call_count == 6

    def test_on_connect_failure(self, subscriber):
        # NOTE: _on_connect does not explicitly set connected=False on failure.
        # It only sets connected=True inside `if rc == 0`. This means if a
        # reconnection attempt fails, the subscriber still thinks it's connected.
        # Potential bug to address in production code.
        subscriber.connected = False  # start from disconnected state
        subscriber._on_connect(subscriber.client, None, None, 5)  # rc=5 = not authorized
        assert subscriber.connected is False  # should remain False

    def test_on_disconnect_unexpected(self, subscriber):
        subscriber.connected = True
        subscriber._on_disconnect(subscriber.client, None, 1)
        assert subscriber.connected is False

    def test_on_disconnect_clean(self, subscriber):
        subscriber.connected = True
        subscriber._on_disconnect(subscriber.client, None, 0)
        assert subscriber.connected is False


# ---------------------------------------------------------------------------
# Networked mode topics
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestNetworkedMode:
    """Tests for DashMQTTSubscriber in networked mode."""

    def test_networked_topics_include_device_id(self):
        with patch('utils.mqtt_manager.mqtt.Client'):
            sub = DashMQTTSubscriber(
                broker_host='10.42.0.1',
                broker_port=1883,
                device_id='owl-unit-1'
            )
            assert sub.networked_mode is True
            assert sub.topics['commands'] == 'owl/owl-unit-1/commands'
            assert sub.topics['state'] == 'owl/owl-unit-1/state'

    def test_standalone_topics_no_device_id(self):
        with patch('utils.mqtt_manager.mqtt.Client'):
            sub = DashMQTTSubscriber(
                broker_host='localhost',
                broker_port=1883
            )
            assert sub.networked_mode is False
            assert sub.topics['commands'] == 'owl/commands'
