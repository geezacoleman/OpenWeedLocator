"""Tests for the Noktura cloud connectivity indicator (both controllers).

Covers the device-side signal: the dashboards subscribe to the retained
$SYS/broker/connection/owl-bridge-<id>/state topic the mosquitto bridge
publishes on the local broker, and derive a not-linked / connecting /
disconnected / connected state from [Cloud] config + that retained payload.
"""

import types

import pytest

from utils.mqtt_manager import DashMQTTSubscriber


def _msg(topic, payload):
    """Minimal stand-in for a paho MQTTMessage."""
    return types.SimpleNamespace(topic=topic, payload=payload)


# ---------------------------------------------------------------------------
# Standalone — DashMQTTSubscriber
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDashSubscriberCloudState:

    def test_no_cloud_device_id_means_no_topic(self):
        sub = DashMQTTSubscriber(client_id='t')
        assert sub.cloud_state_topic is None
        assert sub.get_cloud_connected() is None

    def test_cloud_topic_built_from_device_id(self):
        sub = DashMQTTSubscriber(client_id='t', cloud_device_id='paddock-north')
        assert sub.cloud_state_topic == '$SYS/broker/connection/owl-bridge-paddock-north/state'

    def test_retained_one_marks_connected(self):
        sub = DashMQTTSubscriber(client_id='t', cloud_device_id='paddock-north')
        sub._on_message(None, None, _msg(sub.cloud_state_topic, b'1'))
        assert sub.get_cloud_connected() is True

    def test_retained_zero_marks_disconnected(self):
        sub = DashMQTTSubscriber(client_id='t', cloud_device_id='paddock-north')
        sub._on_message(None, None, _msg(sub.cloud_state_topic, b'0'))
        assert sub.get_cloud_connected() is False

    def test_sys_message_does_not_touch_owl_state(self):
        """The $SYS payload is '1'/'0', not JSON — it must be intercepted
        before the state-topic handling and never land in current_state."""
        sub = DashMQTTSubscriber(client_id='t', cloud_device_id='paddock-north')
        sub._on_message(None, None, _msg(sub.cloud_state_topic, b'1'))
        assert sub.current_state == {}


# ---------------------------------------------------------------------------
# Networked — CentralController
# ---------------------------------------------------------------------------

def _write_controller_ini(tmp_path, *, enable, device_id='', portal_url=''):
    ini = tmp_path / 'CONTROLLER.ini'
    lines = [
        '[MQTT]',
        'broker_ip = localhost',
        'broker_port = 1883',
        'client_id = test_controller',
        '',
        '[Cloud]',
        f'enable = {enable}',
        f'device_id = {device_id}',
        f'portal_url = {portal_url}',
    ]
    ini.write_text('\n'.join(lines), encoding='utf-8')
    return ini


@pytest.mark.unit
class TestControllerCloudState:

    def _controller(self, ini):
        from controller.networked.networked import CentralController
        return CentralController(config_file=str(ini))

    def test_enabled_builds_state_topic(self, tmp_path):
        ini = _write_controller_ini(tmp_path, enable='True', device_id='east-farm',
                                    portal_url='https://app.noktura.tech')
        ctrl = self._controller(ini)
        assert ctrl.cloud_enable is True
        assert ctrl.cloud_device_id == 'east-farm'
        assert ctrl.cloud_portal_url == 'https://app.noktura.tech'
        assert ctrl.cloud_state_topic == '$SYS/broker/connection/owl-bridge-east-farm/state'
        assert ctrl.cloud_connected is None

    def test_disabled_means_no_topic(self, tmp_path):
        ini = _write_controller_ini(tmp_path, enable='False', device_id='east-farm')
        ctrl = self._controller(ini)
        assert ctrl.cloud_enable is False
        assert ctrl.cloud_state_topic is None

    def test_portal_url_trailing_slash_stripped(self, tmp_path):
        ini = _write_controller_ini(tmp_path, enable='True', device_id='east-farm',
                                    portal_url='https://app.noktura.tech/')
        ctrl = self._controller(ini)
        assert ctrl.cloud_portal_url == 'https://app.noktura.tech'

    def test_bridge_state_message_sets_connected(self, tmp_path):
        ini = _write_controller_ini(tmp_path, enable='True', device_id='east-farm')
        ctrl = self._controller(ini)
        ctrl._on_message(None, None, _msg(ctrl.cloud_state_topic, b'1'))
        assert ctrl.cloud_connected is True
        ctrl._on_message(None, None, _msg(ctrl.cloud_state_topic, b'0'))
        assert ctrl.cloud_connected is False

    def test_bridge_state_message_creates_no_phantom_owl(self, tmp_path):
        """$SYS topic splits to ['$SYS','broker',...] — must be handled before
        the owl/<id>/<type> parser so it never registers a fake OWL."""
        ini = _write_controller_ini(tmp_path, enable='True', device_id='east-farm')
        ctrl = self._controller(ini)
        ctrl._on_message(None, None, _msg(ctrl.cloud_state_topic, b'1'))
        assert ctrl.owls_state == {}
