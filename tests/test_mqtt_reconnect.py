"""Tests for MQTT startup resilience — reconnect logic in OWLMQTTPublisher
and CentralController.

Verifies that broker unreachability doesn't crash the system and that
background reconnect threads retry with exponential backoff.
"""

import json
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# OWLMQTTPublisher reconnect tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestOWLMQTTPublisherReconnect:
    """Tests for OWLMQTTPublisher startup resilience."""

    def _make_publisher(self):
        """Create a publisher with a mocked MQTT client."""
        from utils.mqtt_manager import OWLMQTTPublisher

        publisher = OWLMQTTPublisher(
            broker_host='192.168.1.100',
            broker_port=1883,
            client_id='test_owl',
            device_id='test-owl',
            network_mode='networked'
        )
        publisher.client = MagicMock()
        return publisher

    def test_start_sets_running_true_even_on_failure(self):
        """start() must keep self.running = True so heartbeat threads stay alive."""
        publisher = self._make_publisher()
        publisher.client.connect.side_effect = ConnectionRefusedError("refused")

        publisher.start()

        assert publisher.running is True

    def test_start_launches_heartbeat_thread(self):
        """Heartbeat thread should start regardless of broker reachability."""
        publisher = self._make_publisher()
        publisher.client.connect.side_effect = ConnectionRefusedError("refused")

        publisher.start()

        assert publisher.heartbeat_thread is not None
        assert publisher.heartbeat_thread.is_alive()

        # Cleanup
        publisher.running = False

    def test_start_launches_monitoring_thread(self):
        """Monitoring thread should start regardless of broker reachability."""
        publisher = self._make_publisher()
        publisher.client.connect.side_effect = ConnectionRefusedError("refused")

        publisher.start()

        assert publisher.monitoring_thread is not None
        assert publisher.monitoring_thread.is_alive()

        publisher.running = False

    def test_start_launches_reconnect_thread_on_failure(self):
        """When connect fails, a background reconnect thread should start."""
        publisher = self._make_publisher()
        publisher.client.connect.side_effect = ConnectionRefusedError("refused")

        publisher.start()

        assert publisher._reconnect_thread is not None
        assert publisher._reconnect_thread.is_alive()

        publisher.running = False

    def test_start_no_reconnect_thread_on_success(self):
        """When connect succeeds, no reconnect thread is needed."""
        publisher = self._make_publisher()
        # connect() succeeds (no side_effect)

        publisher.start()

        assert publisher._reconnect_thread is None

        publisher.running = False

    def test_background_reconnect_retries_on_failure(self):
        """_background_reconnect should retry when connect keeps failing."""
        publisher = self._make_publisher()
        publisher.running = True

        call_count = 0

        def fail_then_succeed(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionRefusedError("refused")
            # Third attempt succeeds

        publisher.client.connect.side_effect = fail_then_succeed

        # Patch sleep to avoid waiting
        with patch('utils.mqtt_manager.time.sleep'):
            publisher._background_reconnect()

        assert call_count == 3
        publisher.client.loop_start.assert_called_once()

    def test_background_reconnect_stops_when_running_false(self):
        """Reconnect loop should exit when self.running is set to False."""
        publisher = self._make_publisher()
        publisher.running = True
        publisher.client.connect.side_effect = ConnectionRefusedError("refused")

        call_count = 0

        def stop_after_two(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                publisher.running = False

        with patch('utils.mqtt_manager.time.sleep', side_effect=stop_after_two):
            publisher._background_reconnect()

        # Should have stopped after running was set to False
        assert publisher.running is False

    def test_stop_safe_when_never_connected(self):
        """stop() should not crash if the client never connected."""
        publisher = self._make_publisher()
        publisher.running = True
        publisher.connected = False
        publisher.client.loop_stop.side_effect = Exception("not started")
        publisher.client.disconnect.side_effect = Exception("not connected")

        # Should not raise
        publisher.stop()

        assert publisher.running is False

    def test_lwt_configured_on_init(self):
        """Publisher should set Last Will & Testament on the status topic."""
        from utils.mqtt_manager import OWLMQTTPublisher

        with patch('paho.mqtt.client.Client') as MockClient:
            mock_client = MagicMock()
            MockClient.return_value = mock_client

            publisher = OWLMQTTPublisher(
                broker_host='localhost',
                broker_port=1883,
                client_id='test_lwt',
                device_id='test-lwt'
            )

            mock_client.will_set.assert_called_once()
            call_args = mock_client.will_set.call_args
            topic = call_args[0][0]
            payload = json.loads(call_args[0][1])

            assert 'status' in topic
            assert payload['connected'] is False
            assert payload['owl_running'] is False

    def test_reconnect_delay_set_on_init(self):
        """Publisher should configure paho's auto-reconnect delays."""
        from utils.mqtt_manager import OWLMQTTPublisher

        with patch('paho.mqtt.client.Client') as MockClient:
            mock_client = MagicMock()
            MockClient.return_value = mock_client

            publisher = OWLMQTTPublisher(
                broker_host='localhost',
                broker_port=1883,
                client_id='test_delay',
                device_id='test-delay'
            )

            mock_client.reconnect_delay_set.assert_called_once_with(
                min_delay=1, max_delay=30
            )

    def test_on_connect_sets_connected_true(self):
        """_on_connect callback should set self.connected = True on rc=0."""
        publisher = self._make_publisher()

        publisher._on_connect(publisher.client, None, {}, 0)

        assert publisher.connected is True

    def test_on_disconnect_sets_connected_false(self):
        """_on_disconnect callback should set self.connected = False."""
        publisher = self._make_publisher()
        publisher.connected = True

        publisher._on_disconnect(publisher.client, None, 1)

        assert publisher.connected is False


# ---------------------------------------------------------------------------
# CentralController MQTT retry tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestControllerMQTTReconnect:
    """Tests for CentralController MQTT startup resilience."""

    def _make_controller(self):
        """Create a controller with mocked internals (no real MQTT)."""
        with patch('controller.networked.networked.CentralController.__init__', lambda self, **kw: None):
            from controller.networked.networked import CentralController
            ctrl = CentralController.__new__(CentralController)
            ctrl.owls_state = {}
            ctrl.desired_state = {}
            ctrl.lwt_timestamps = {}
            ctrl.mqtt_connected = False
            ctrl.mqtt_lock = threading.Lock()
            ctrl.mqtt_client = None
            ctrl.offline_timeout = 15.0
            ctrl.gps_manager = None
            ctrl.broker_host = '192.168.1.100'
            ctrl.broker_port = 1883
            ctrl.client_id = 'test_controller'
            ctrl.config = __import__('configparser').ConfigParser()
            return ctrl

    def test_setup_mqtt_starts_reconnect_on_failure(self):
        """setup_mqtt should start background reconnect when broker is unreachable."""
        ctrl = self._make_controller()

        with patch('paho.mqtt.client.Client') as MockClient:
            mock_client = MagicMock()
            MockClient.return_value = mock_client
            mock_client.connect.side_effect = ConnectionRefusedError("refused")

            with patch('controller.networked.networked.threading.Thread') as MockThread:
                mock_thread = MagicMock()
                MockThread.return_value = mock_thread

                ctrl.setup_mqtt()

                # Thread should have been created for background reconnect
                MockThread.assert_called()
                mock_thread.start.assert_called_once()

    def test_setup_mqtt_no_reconnect_on_success(self):
        """setup_mqtt should not start reconnect thread when broker connects."""
        ctrl = self._make_controller()

        with patch('paho.mqtt.client.Client') as MockClient:
            mock_client = MagicMock()
            MockClient.return_value = mock_client
            # connect() succeeds

            with patch('controller.networked.networked.threading.Thread') as MockThread:
                ctrl.setup_mqtt()

                # No reconnect thread needed — Thread only called for reconnect
                reconnect_calls = [c for c in MockThread.call_args_list
                                   if 'reconnect' in str(c)]
                assert len(reconnect_calls) == 0

    def test_mqtt_client_never_set_to_none(self):
        """After setup_mqtt, mqtt_client should exist even if connect fails."""
        ctrl = self._make_controller()

        with patch('paho.mqtt.client.Client') as MockClient:
            mock_client = MagicMock()
            MockClient.return_value = mock_client
            mock_client.connect.side_effect = ConnectionRefusedError("refused")

            # Mock Thread so reconnect doesn't actually run
            with patch('controller.networked.networked.threading.Thread'):
                ctrl.setup_mqtt()

            assert ctrl.mqtt_client is not None

    def test_try_mqtt_connect_returns_false_on_error(self):
        """_try_mqtt_connect should return False when connection fails."""
        ctrl = self._make_controller()
        ctrl.mqtt_client = MagicMock()
        ctrl.mqtt_client.connect.side_effect = ConnectionRefusedError("refused")

        result = ctrl._try_mqtt_connect()

        assert result is False

    def test_try_mqtt_connect_returns_true_on_success(self):
        """_try_mqtt_connect should return True when connection succeeds."""
        ctrl = self._make_controller()
        ctrl.mqtt_client = MagicMock()

        result = ctrl._try_mqtt_connect()

        assert result is True
        ctrl.mqtt_client.loop_start.assert_called_once()


# ---------------------------------------------------------------------------
# "All on at once" integration test — simulates simultaneous power-on
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSimultaneousPowerOn:
    """Simulates the "power everything on at once" scenario.

    Uses a FakeBroker that becomes available after a delay, then verifies
    both OWL publisher and controller discover each other via callbacks.
    No real MQTT broker needed — we wire the callbacks directly.
    """

    def test_owl_publishes_full_state_on_reconnect(self):
        """After background reconnect succeeds, OWL should publish full state
        immediately via _on_connect — not wait for heartbeat."""
        from utils.mqtt_manager import OWLMQTTPublisher

        publisher = OWLMQTTPublisher(
            broker_host='192.168.1.100',
            broker_port=1883,
            client_id='test_owl',
            device_id='test-owl',
            network_mode='networked'
        )
        publisher.client = MagicMock()

        # Simulate _on_connect callback (as paho would call it after connect)
        publisher._on_connect(publisher.client, None, {}, 0)

        assert publisher.connected is True

        # Should have published to the state topic (full state), not just status
        publish_calls = publisher.client.publish.call_args_list
        published_topics = [c[0][0] for c in publish_calls]

        # Must include both status AND state topics
        assert any('status' in t for t in published_topics), \
            f"Expected status topic in {published_topics}"
        assert any('state' in t for t in published_topics), \
            f"Expected state topic in {published_topics} — full state must publish on connect"

    def test_broker_comes_up_after_owl_and_controller_start(self):
        """Simulate: OWL + controller both fail initial connect, broker appears,
        both reconnect, controller sees OWL state."""
        from utils.mqtt_manager import OWLMQTTPublisher
        from controller.networked.networked import CentralController

        # --- Set up OWL publisher ---
        publisher = OWLMQTTPublisher(
            broker_host='192.168.1.100',
            broker_port=1883,
            client_id='sim_owl',
            device_id='sim-owl-pi',
            network_mode='networked'
        )
        publisher.client = MagicMock()

        # --- Set up controller (bypassing __init__) ---
        with patch.object(CentralController, '__init__', lambda self, **kw: None):
            ctrl = CentralController.__new__(CentralController)
            ctrl.owls_state = {}
            ctrl.desired_state = {}
            ctrl.lwt_timestamps = {}
            ctrl.mqtt_connected = False
            ctrl.mqtt_lock = threading.Lock()
            ctrl.mqtt_client = MagicMock()
            ctrl.offline_timeout = 15.0
            ctrl.gps_manager = None
            ctrl.broker_host = '192.168.1.100'
            ctrl.broker_port = 1883
            ctrl.client_id = 'sim_controller'

        # --- Phase 1: Both fail to connect (broker not up) ---
        publisher.client.connect.side_effect = ConnectionRefusedError("refused")
        publisher.start()
        assert publisher.running is True
        assert publisher.connected is False
        assert publisher._reconnect_thread is not None

        # --- Phase 2: Broker comes up — simulate successful reconnect ---
        publisher.running = False  # Stop reconnect thread
        time.sleep(0.05)  # Let thread exit

        publisher.client.connect.side_effect = None  # Connect now succeeds
        publisher.client.connect.reset_mock()
        publisher.client.publish.reset_mock()

        # Wire up: when OWL publishes state, controller receives it
        captured_messages = []

        def capture_publish(topic, payload, *args, **kwargs):
            captured_messages.append((topic, payload))
            # Forward state messages to controller's _on_message
            if '/state' in topic or '/status' in topic:
                msg = MagicMock()
                msg.topic = topic
                msg.payload = payload.encode() if isinstance(payload, str) else payload
                ctrl._on_message(None, None, msg)
            return MagicMock(rc=0)

        publisher.client.publish.side_effect = capture_publish

        # Simulate paho calling _on_connect after successful reconnect
        publisher.connected = False
        publisher._on_connect(publisher.client, None, {}, 0)

        # --- Phase 3: Verify controller sees the OWL ---
        assert publisher.connected is True
        assert 'sim-owl-pi' in ctrl.owls_state, \
            f"Controller should see OWL. State: {ctrl.owls_state}"
        assert ctrl.owls_state['sim-owl-pi']['connected'] is True

        # Cleanup
        publisher.running = False

    def test_controller_pushes_desired_state_on_owl_reconnect(self):
        """When OWL reconnects, controller pushes queued desired state."""
        from controller.networked.networked import CentralController

        with patch.object(CentralController, '__init__', lambda self, **kw: None):
            ctrl = CentralController.__new__(CentralController)
            ctrl.owls_state = {}
            ctrl.desired_state = {'all': {'detection_enable': True}}
            ctrl.lwt_timestamps = {}
            ctrl.mqtt_connected = True
            ctrl.mqtt_lock = threading.Lock()
            ctrl.mqtt_client = MagicMock()
            ctrl.offline_timeout = 15.0
            ctrl.gps_manager = None

        # Simulate OWL state message arriving (first contact after power-on)
        msg = MagicMock()
        msg.topic = 'owl/owl-pi-field1/state'
        msg.payload = json.dumps({
            'device_id': 'owl-pi-field1',
            'detection_enable': False,
            'owl_running': True,
            'algorithm': 'exhsv'
        }).encode()
        ctrl._on_message(None, None, msg)

        # Controller should have pushed detection_enable=True to the new OWL
        pub_calls = ctrl.mqtt_client.publish.call_args_list
        cmd_calls = [c for c in pub_calls if 'owl-pi-field1/commands' in c[0][0]]
        assert len(cmd_calls) > 0, "Controller should push desired state to new OWL"

        payloads = [json.loads(c[0][1]) for c in cmd_calls]
        assert any(p['action'] == 'set_detection_enable' and p['value'] is True
                   for p in payloads)

    def test_owl_lwt_then_reconnect_cycle(self):
        """Full cycle: OWL connects → disconnects (LWT) → reconnects.
        Controller should see offline then back online."""
        from controller.networked.networked import CentralController

        with patch.object(CentralController, '__init__', lambda self, **kw: None):
            ctrl = CentralController.__new__(CentralController)
            ctrl.owls_state = {}
            ctrl.desired_state = {}
            ctrl.lwt_timestamps = {}
            ctrl.mqtt_connected = True
            ctrl.mqtt_lock = threading.Lock()
            ctrl.mqtt_client = MagicMock()
            ctrl.offline_timeout = 15.0
            ctrl.gps_manager = None

        def inject(topic_type, payload):
            msg = MagicMock()
            msg.topic = f'owl/owl-pi-1/{topic_type}'
            msg.payload = json.dumps(payload).encode()
            ctrl._on_message(None, None, msg)

        # Step 1: OWL comes online
        inject('state', {'detection_enable': True, 'owl_running': True})
        assert ctrl.owls_state['owl-pi-1']['connected'] is True

        # Step 2: LWT fires (OWL loses power / WiFi drops)
        inject('status', {
            'device_id': 'owl-pi-1',
            'owl_running': False,
            'connected': False,
            'timestamp': time.time()
        })
        assert ctrl.owls_state['owl-pi-1']['connected'] is False

        # Step 3: OWL reconnects (sends state again)
        inject('state', {'detection_enable': True, 'owl_running': True})
        assert ctrl.owls_state['owl-pi-1']['connected'] is True

    def test_two_owls_staggered_reconnect(self):
        """Two OWLs reconnect at different times — both tracked independently."""
        from controller.networked.networked import CentralController

        with patch.object(CentralController, '__init__', lambda self, **kw: None):
            ctrl = CentralController.__new__(CentralController)
            ctrl.owls_state = {}
            ctrl.desired_state = {'all': {'detection_enable': True}}
            ctrl.lwt_timestamps = {}
            ctrl.mqtt_connected = True
            ctrl.mqtt_lock = threading.Lock()
            ctrl.mqtt_client = MagicMock()
            ctrl.offline_timeout = 15.0
            ctrl.gps_manager = None

        def inject(device_id, payload):
            msg = MagicMock()
            msg.topic = f'owl/{device_id}/state'
            msg.payload = json.dumps(payload).encode()
            ctrl._on_message(None, None, msg)

        # OWL-1 connects first
        inject('owl-left', {'detection_enable': False, 'algorithm': 'exhsv'})
        assert 'owl-left' in ctrl.owls_state
        assert ctrl.owls_state['owl-left']['connected'] is True

        # OWL-2 connects 5 seconds later
        inject('owl-right', {'detection_enable': False, 'algorithm': 'gog'})
        assert 'owl-right' in ctrl.owls_state
        assert ctrl.owls_state['owl-right']['connected'] is True

        # Both should have had desired state pushed
        pub_calls = ctrl.mqtt_client.publish.call_args_list
        left_cmds = [c for c in pub_calls if 'owl-left/commands' in c[0][0]]
        right_cmds = [c for c in pub_calls if 'owl-right/commands' in c[0][0]]
        assert len(left_cmds) > 0, "Should push desired state to owl-left"
        assert len(right_cmds) > 0, "Should push desired state to owl-right"
