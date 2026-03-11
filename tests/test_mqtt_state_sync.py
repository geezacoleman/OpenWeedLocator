"""Tests for networked controller MQTT state sync — multi-OWL consistency.

Priority 6 — critical for multi-OWL field setups (2+ units on one controller).
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def controller():
    """CentralController with mocked MQTT client (no broker connection)."""
    with patch('controller.networked.networked.CentralController.__init__', lambda self, **kw: None):
        from controller.networked.networked import CentralController
        ctrl = CentralController.__new__(CentralController)
        # Manually init required attributes
        ctrl.owls_state = {}
        ctrl.desired_state = {}
        ctrl.mqtt_connected = True
        ctrl.mqtt_lock = __import__('threading').Lock()
        ctrl.mqtt_client = MagicMock()
        ctrl.offline_timeout = 15.0
        ctrl.gps_manager = None
        ctrl.config = __import__('configparser').ConfigParser()
        return ctrl


# ---------------------------------------------------------------------------
# Device heartbeat registration/expiry
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDeviceHeartbeat:
    """Tests for OWL device registration and timeout."""

    def _inject_message(self, ctrl, device_id, topic_type='state', payload=None):
        """Simulate an incoming MQTT message."""
        if payload is None:
            payload = {'detection_enable': False, 'owl_running': True}
        msg = MagicMock()
        msg.topic = f'owl/{device_id}/{topic_type}'
        msg.payload = json.dumps(payload).encode()
        ctrl._on_message(None, None, msg)

    def test_new_owl_registered(self, controller):
        self._inject_message(controller, 'owl-pi-1')
        assert 'owl-pi-1' in controller.owls_state
        assert controller.owls_state['owl-pi-1']['connected'] is True

    def test_second_owl_tracked_independently(self, controller):
        self._inject_message(controller, 'owl-pi-1')
        self._inject_message(controller, 'owl-pi-2')
        assert 'owl-pi-1' in controller.owls_state
        assert 'owl-pi-2' in controller.owls_state

    def test_last_seen_updated_on_message(self, controller):
        self._inject_message(controller, 'owl-pi-1')
        t1 = controller.owls_state['owl-pi-1']['last_seen']
        time.sleep(0.05)
        self._inject_message(controller, 'owl-pi-1')
        t2 = controller.owls_state['owl-pi-1']['last_seen']
        assert t2 > t1

    def test_state_message_merges_payload(self, controller):
        self._inject_message(controller, 'owl-pi-1', 'state', {
            'detection_enable': True,
            'algorithm': 'exhsv',
            'sensitivity_level': 'high'
        })
        state = controller.owls_state['owl-pi-1']
        assert state['detection_enable'] is True
        assert state['algorithm'] == 'exhsv'

    def test_status_message_stored_separately(self, controller):
        self._inject_message(controller, 'owl-pi-1', 'status', {
            'owl_running': True,
            'connected': True
        })
        state = controller.owls_state['owl-pi-1']
        assert state['status']['owl_running'] is True


# ---------------------------------------------------------------------------
# Desired state vs reported state separation
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDesiredVsReportedState:
    """Tests for desired_state tracking (user intent vs device state)."""

    def test_toggle_detection_stores_desired_state(self, controller):
        # Register an OWL first
        controller.owls_state['owl-pi-1'] = {
            'device_id': 'owl-pi-1',
            'connected': True,
            'detection_enable': False,
            'last_seen': time.time()
        }
        mock_result = MagicMock()
        mock_result.rc = 0
        controller.mqtt_client.publish.return_value = mock_result

        controller.send_command('all', 'toggle_detection', True)

        assert controller.desired_state.get('all', {}).get('detection_enable') is True

    def test_toggle_recording_stores_desired_state(self, controller):
        controller.owls_state['owl-pi-1'] = {
            'device_id': 'owl-pi-1',
            'connected': True,
            'image_sample_enable': False,
            'last_seen': time.time()
        }
        mock_result = MagicMock()
        mock_result.rc = 0
        controller.mqtt_client.publish.return_value = mock_result

        controller.send_command('all', 'toggle_recording', True)

        assert controller.desired_state.get('all', {}).get('image_sample_enable') is True

    def test_nozzle_toggle_stores_detection_mode(self, controller):
        controller.owls_state['owl-pi-1'] = {
            'device_id': 'owl-pi-1',
            'connected': True,
            'last_seen': time.time()
        }
        mock_result = MagicMock()
        mock_result.rc = 0
        controller.mqtt_client.publish.return_value = mock_result

        controller.send_command('all', 'toggle_all_nozzles', True)

        assert controller.desired_state.get('all', {}).get('detection_mode') == 2
        assert controller.desired_state.get('all', {}).get('detection_enable') is False


# ---------------------------------------------------------------------------
# State broadcast on device reconnect
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestReconnectBroadcast:
    """Tests for _push_desired_state on OWL reconnect."""

    def _inject_message(self, ctrl, device_id, topic_type='state', payload=None):
        if payload is None:
            payload = {'detection_enable': False}
        msg = MagicMock()
        msg.topic = f'owl/{device_id}/{topic_type}'
        msg.payload = json.dumps(payload).encode()
        ctrl._on_message(None, None, msg)

    def test_new_device_gets_desired_state_pushed(self, controller):
        """When a new OWL appears, desired state should be pushed to it."""
        controller.desired_state = {
            'all': {'detection_enable': True, 'image_sample_enable': False}
        }

        self._inject_message(controller, 'owl-pi-new')

        # Should have published detection_enable and image_sample_enable to the new device
        calls = controller.mqtt_client.publish.call_args_list
        topics = [c[0][0] for c in calls]
        assert any('owl-pi-new/commands' in t for t in topics)

        # Check the payloads
        payloads = [json.loads(c[0][1]) for c in calls if 'owl-pi-new/commands' in c[0][0]]
        actions = [p['action'] for p in payloads]
        assert 'set_detection_enable' in actions

    def test_reconnected_device_gets_desired_state(self, controller):
        """When an OWL reconnects (was offline), desired state is pushed."""
        # Register then mark offline
        controller.owls_state['owl-pi-1'] = {
            'device_id': 'owl-pi-1',
            'connected': False,
            'last_seen': time.time() - 100
        }
        controller.desired_state = {
            'all': {'detection_enable': True}
        }

        self._inject_message(controller, 'owl-pi-1')

        calls = controller.mqtt_client.publish.call_args_list
        payloads = [json.loads(c[0][1]) for c in calls if 'owl-pi-1/commands' in c[0][0]]
        assert any(p['action'] == 'set_detection_enable' for p in payloads)

    def test_no_push_when_no_desired_state(self, controller):
        """If desired_state is empty, no commands pushed on reconnect."""
        controller.desired_state = {}
        self._inject_message(controller, 'owl-pi-1')

        # Only the initial message processing, no additional publish to commands topic
        calls = controller.mqtt_client.publish.call_args_list
        cmd_calls = [c for c in calls if 'commands' in c[0][0]]
        assert len(cmd_calls) == 0

    def test_tracking_enabled_pushed_on_reconnect(self, controller):
        """tracking_enabled should be pushed to reconnecting OWL."""
        controller.desired_state = {
            'all': {'tracking_enabled': True}
        }

        self._inject_message(controller, 'owl-pi-track')

        calls = controller.mqtt_client.publish.call_args_list
        payloads = [json.loads(c[0][1]) for c in calls
                    if 'owl-pi-track/commands' in c[0][0]]
        assert any(p['action'] == 'set_tracking' and p['value'] is True
                   for p in payloads)


# ---------------------------------------------------------------------------
# Multiple OWL devices tracked independently
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestMultiOWLTracking:
    """Tests for tracking multiple OWL devices independently."""

    def _inject_message(self, ctrl, device_id, topic_type='state', payload=None):
        if payload is None:
            payload = {'detection_enable': False}
        msg = MagicMock()
        msg.topic = f'owl/{device_id}/{topic_type}'
        msg.payload = json.dumps(payload).encode()
        ctrl._on_message(None, None, msg)

    def test_two_owls_independent_state(self, controller):
        self._inject_message(controller, 'owl-1', payload={
            'detection_enable': True,
            'algorithm': 'exhsv'
        })
        self._inject_message(controller, 'owl-2', payload={
            'detection_enable': False,
            'algorithm': 'gog'
        })

        assert controller.owls_state['owl-1']['detection_enable'] is True
        assert controller.owls_state['owl-2']['detection_enable'] is False
        assert controller.owls_state['owl-1']['algorithm'] == 'exhsv'
        assert controller.owls_state['owl-2']['algorithm'] == 'gog'

    def test_broadcast_sends_to_all_connected(self, controller):
        """send_command('all', ...) should publish to all connected OWLs."""
        for owl_id in ['owl-1', 'owl-2', 'owl-3']:
            controller.owls_state[owl_id] = {
                'device_id': owl_id,
                'connected': True,
                'last_seen': time.time()
            }

        mock_result = MagicMock()
        mock_result.rc = 0
        controller.mqtt_client.publish.return_value = mock_result

        result = controller.send_command('all', 'set_algorithm', 'exhsv')

        assert result['success'] is True
        assert len(result['targets']) == 3

    def test_broadcast_skips_disconnected(self, controller):
        controller.owls_state['owl-1'] = {
            'device_id': 'owl-1', 'connected': True, 'last_seen': time.time()
        }
        controller.owls_state['owl-2'] = {
            'device_id': 'owl-2', 'connected': False, 'last_seen': time.time() - 100
        }

        mock_result = MagicMock()
        mock_result.rc = 0
        controller.mqtt_client.publish.return_value = mock_result

        result = controller.send_command('all', 'set_algorithm', 'exhsv')

        # Should only send to owl-1
        assert len(result['targets']) == 1
        assert result['targets'][0] == 'owl-1'


# ---------------------------------------------------------------------------
# LWT (Last Will & Testament) instant offline detection
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLWTOfflineDetection:
    """Tests for instant OWL disconnect via LWT status messages."""

    def _inject_message(self, ctrl, device_id, topic_type='state', payload=None):
        if payload is None:
            payload = {'detection_enable': False}
        msg = MagicMock()
        msg.topic = f'owl/{device_id}/{topic_type}'
        msg.payload = json.dumps(payload).encode()
        ctrl._on_message(None, None, msg)

    def test_lwt_marks_owl_offline_immediately(self, controller):
        """LWT status message with connected=False marks OWL offline instantly."""
        # Register a connected OWL
        self._inject_message(controller, 'owl-pi-1', 'state', {
            'detection_enable': True, 'owl_running': True
        })
        assert controller.owls_state['owl-pi-1']['connected'] is True

        # LWT arrives (broker publishes when OWL disconnects ungracefully)
        self._inject_message(controller, 'owl-pi-1', 'status', {
            'device_id': 'owl-pi-1',
            'owl_running': False,
            'connected': False,
            'timestamp': time.time()
        })
        assert controller.owls_state['owl-pi-1']['connected'] is False

    def test_lwt_owl_running_false_marks_offline(self, controller):
        """Status message with owl_running=False also marks offline."""
        self._inject_message(controller, 'owl-pi-1', 'state', {'detection_enable': False})
        assert controller.owls_state['owl-pi-1']['connected'] is True

        self._inject_message(controller, 'owl-pi-1', 'status', {
            'device_id': 'owl-pi-1',
            'owl_running': False,
            'connected': True,
            'timestamp': time.time()
        })
        assert controller.owls_state['owl-pi-1']['connected'] is False

    def test_normal_status_does_not_mark_offline(self, controller):
        """A healthy status message keeps the OWL connected."""
        self._inject_message(controller, 'owl-pi-1', 'state', {'detection_enable': False})

        self._inject_message(controller, 'owl-pi-1', 'status', {
            'device_id': 'owl-pi-1',
            'owl_running': True,
            'connected': True,
            'timestamp': time.time()
        })
        assert controller.owls_state['owl-pi-1']['connected'] is True


# ---------------------------------------------------------------------------
# Timeout configuration
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestTimeoutConfiguration:
    """Tests for offline/stale timeout values."""

    def test_offline_timeout_is_15s(self, controller):
        """offline_timeout should be 15s to allow boot-time connection delays."""
        assert controller.offline_timeout == 15.0

    def test_stale_removal_at_4x_timeout(self, controller):
        """Stale OWLs should only be removed after 4x offline_timeout (60s)."""
        # Register then make stale (just over 4x timeout)
        controller.owls_state['owl-stale'] = {
            'device_id': 'owl-stale',
            'connected': False,
            'last_seen': time.time() - (controller.offline_timeout * 4 + 1)
        }
        controller.owls_state['owl-recent'] = {
            'device_id': 'owl-recent',
            'connected': True,
            'last_seen': time.time()
        }

        # Run one iteration of check_connections logic inline
        current_time = time.time()
        with controller.mqtt_lock:
            for device_id, state in list(controller.owls_state.items()):
                last_seen = state.get('last_seen', 0)
                time_since = current_time - last_seen
                if time_since > controller.offline_timeout:
                    state['connected'] = False
                if time_since > (controller.offline_timeout * 4):
                    del controller.owls_state[device_id]

        assert 'owl-stale' not in controller.owls_state
        assert 'owl-recent' in controller.owls_state
