"""The controller's own presence (owl-controller), which the cloud bridge
forwards so Noktura shows the cab unit online with its version.

Run on a real CentralController with a mock broker client. (Until 3.27.3
these were substring checks of networked.py.)
"""

import json
from unittest.mock import MagicMock

import pytest

STATUS_TOPIC = 'owl/owl-controller/status'
# Outside owl/ so the slim cloud state is not bridged as a full OWL state
STATE_TOPIC = 'cloud_out/owl-controller/state'


def publishes(client, topic):
    """[(payload dict, kwargs)] for each publish on `topic`, in order."""
    return [(json.loads(c.args[1]), c.kwargs) for c in client.publish.call_args_list
            if c.args[0] == topic]


@pytest.fixture
def connected(make_controller):
    """A controller that has just connected to the broker."""
    ctrl = make_controller(owls=())
    ctrl.mqtt_connected = False
    ctrl._on_connect(ctrl.mqtt_client, None, {}, 0)
    return ctrl


def test_topics_are_the_ones_the_bridge_forwards():
    import controller.networked.networked as net_mod
    assert net_mod.CONTROLLER_STATUS_TOPIC == STATUS_TOPIC
    assert net_mod.CONTROLLER_STATE_TOPIC == STATE_TOPIC


def test_last_will_is_registered_before_connecting(make_controller, monkeypatch):
    """If the cab unit loses power the broker announces it offline: the will
    has to be set before connect() or paho never sends it."""
    import controller.networked.networked as net_mod

    client = MagicMock()
    monkeypatch.setattr(net_mod.mqtt, 'Client', MagicMock(return_value=client))
    ctrl = make_controller(owls=())
    ctrl.mqtt_connected = False
    ctrl.setup_mqtt()

    order = [call[0] for call in client.method_calls]
    assert order.index('will_set') < order.index('connect')
    will = client.will_set.call_args
    assert will.args[0] == STATUS_TOPIC
    assert will.kwargs == {'qos': 1, 'retain': True}
    payload = json.loads(will.args[1])
    assert (payload['device_id'], payload['connected']) == ('owl-controller', False)
    client.connect.assert_called_once_with('localhost', 1883, 60)
    assert client.on_message == ctrl._on_message


def test_connecting_announces_online_retained_and_sends_the_state(connected):
    from version import VERSION
    client = connected.mqtt_client
    assert connected.mqtt_connected is True

    [(status, how)] = publishes(client, STATUS_TOPIC)
    assert (status['device_id'], status['connected']) == ('owl-controller', True)
    assert how == {'qos': 1, 'retain': True}

    [(state, how)] = publishes(client, STATE_TOPIC)
    assert how == {'qos': 1, 'retain': False}
    assert {k: state[k] for k in ('device_id', 'type', 'version', 'connected', 'owl_count')} == {
        'device_id': 'owl-controller', 'type': 'controller', 'version': str(VERSION),
        'connected': True, 'owl_count': 0}
    assert state['upload_requests'] == []


def test_connecting_subscribes_to_every_owl(connected):
    subscribed = connected.mqtt_client.subscribe.call_args_list[0].args[0]
    assert ('owl/+/state', 0) in subscribed
    assert ('owl/+/status', 0) in subscribed
    assert ('owl/+/config', 0) in subscribed
    assert ('owl/cloud/owl-controller/commands', 1) in subscribed


def test_a_refused_connection_announces_nothing(make_controller):
    ctrl = make_controller(owls=())
    ctrl._on_connect(ctrl.mqtt_client, None, {}, 5)
    assert ctrl.mqtt_connected is False
    assert ctrl.mqtt_client.publish.call_args_list == []
    assert ctrl.mqtt_client.subscribe.call_args_list == []


def test_the_2s_refresh_sends_nothing_while_nothing_changed(connected):
    """The status is retained and bridged at QoS 1: republishing it (or an
    unchanged state) every 2 s cost 4G data."""
    client = connected.mqtt_client
    client.publish.reset_mock()
    for _ in range(5):
        connected._publish_controller_presence()
    assert client.publish.call_args_list == []


def test_a_change_goes_out_at_once_as_state_only(connected, hears):
    client = connected.mqtt_client
    client.publish.reset_mock()
    hears(connected, 'owl-1', {'detection_enable': False})
    connected._publish_controller_presence()

    assert publishes(client, STATUS_TOPIC) == []
    [(state, how)] = publishes(client, STATE_TOPIC)
    assert state['owl_count'] == 1
    assert how == {'qos': 1, 'retain': False}


def test_keepalive_after_20s_of_no_change_is_qos_0(connected):
    client = connected.mqtt_client
    client.publish.reset_mock()
    connected._cloud_feed._sent_at -= 19
    connected._publish_controller_presence()
    assert client.publish.call_args_list == []

    connected._cloud_feed._sent_at -= 2
    connected._publish_controller_presence()
    assert publishes(client, STATUS_TOPIC) == []
    [(state, how)] = publishes(client, STATE_TOPIC)
    assert how == {'qos': 0, 'retain': False}
    assert state['type'] == 'controller'


def test_reconnecting_announces_again_in_full(connected):
    client = connected.mqtt_client
    connected._on_disconnect(client, None, 1)
    assert connected.mqtt_connected is False
    client.publish.reset_mock()

    connected._on_connect(client, None, {}, 0)
    assert len(publishes(client, STATUS_TOPIC)) == 1
    assert len(publishes(client, STATE_TOPIC)) == 1


@pytest.mark.parametrize('kind', ['state', 'status'])
def test_its_own_presence_is_never_listed_as_an_owl(connected, hears, kind):
    """It subscribes to owl/+/state and owl/+/status, which match its own."""
    hears(connected, 'owl-controller', {'device_id': 'owl-controller', 'connected': True}, kind=kind)
    assert connected.owls_state == {}

