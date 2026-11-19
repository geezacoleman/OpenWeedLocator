"""On/off values in commands (utils.shared_types.parse_switch).

bool('false') is True. Until 3.27.3 the OWL's set_detection_enable handler,
the networked controller and the AI agent's set_detection tool all coerced
with bool(), so a client that sent the string "false" (or "0", or "off")
switched spot spraying ON, and the agent tool with enabled="false", mode=2
blanket sprayed. Anything that is not clearly on or off is now refused and
changes nothing.
"""

import json
from unittest.mock import MagicMock

import pytest

from utils.shared_types import parse_switch

OFF_VALUES = [False, 0, 0.0, 'false', 'False', 'FALSE', ' false ', '0', 'off', 'Off', 'no']
ON_VALUES = [True, 1, 1.0, 'true', 'True', ' TRUE', '1', 'on', 'yes']
NOT_A_SWITCH = [None, 2, -1, 0.5, '', 'banana', 'enabled', '2', [], {}, [False], 'nan']


@pytest.mark.parametrize('value', OFF_VALUES)
def test_off_values(value):
    assert parse_switch(value) is False


@pytest.mark.parametrize('value', ON_VALUES)
def test_on_values(value):
    assert parse_switch(value) is True


@pytest.mark.parametrize('value', NOT_A_SWITCH)
def test_anything_else_is_refused(value):
    with pytest.raises(ValueError):
        parse_switch(value)


# --------------------------------------------------------------------------
# OWL command handler: what the spray owner is asked for
# --------------------------------------------------------------------------

@pytest.fixture
def owl_handler(mqtt_publisher):
    asked = []
    mqtt_publisher._request_spray = lambda mode, **kw: asked.append(mode)
    return mqtt_publisher, asked


@pytest.mark.parametrize('value', OFF_VALUES)
def test_owl_off_value_asks_for_off(owl_handler, value):
    publisher, asked = owl_handler
    publisher._handle_command({'action': 'set_detection_enable', 'value': value})
    assert asked == ['off']


@pytest.mark.parametrize('value', ON_VALUES)
def test_owl_on_value_asks_for_spot(owl_handler, value):
    publisher, asked = owl_handler
    publisher._handle_command({'action': 'set_detection_enable', 'value': value})
    assert asked == ['spot']


@pytest.mark.parametrize('value', NOT_A_SWITCH)
def test_owl_unclear_value_changes_nothing(owl_handler, value):
    publisher, asked = owl_handler
    publisher._handle_command({'action': 'set_detection_enable', 'value': value})
    assert asked == []


def test_owl_missing_value_changes_nothing(owl_handler):
    publisher, asked = owl_handler
    publisher._handle_command({'action': 'set_detection_enable'})
    assert asked == []


@pytest.mark.parametrize('action, key', [('set_image_sample_enable', 'image_sample_enable'),
                                         ('set_recording_paused', 'recording_paused')])
@pytest.mark.parametrize('value', ['false', '0', 'off'])
def test_owl_recording_commands_read_false_strings_as_off(mqtt_publisher, action, key, value):
    mqtt_publisher.state['image_sample_enable'] = True
    mqtt_publisher.state['recording_paused'] = True
    mqtt_publisher._handle_command({'action': action, 'value': value})
    assert mqtt_publisher.state[key] is False


@pytest.mark.parametrize('action', ['set_image_sample_enable', 'set_recording_paused'])
def test_owl_recording_commands_refuse_unclear_values(mqtt_publisher, action):
    mqtt_publisher.state['image_sample_enable'] = True
    mqtt_publisher.state['recording_paused'] = False
    mqtt_publisher._handle_command({'action': action, 'value': 'banana'})
    assert mqtt_publisher.state['image_sample_enable'] is True
    assert mqtt_publisher.state['recording_paused'] is False


# --------------------------------------------------------------------------
# Networked controller: what goes out to the OWLs
# --------------------------------------------------------------------------

@pytest.fixture
def ctrl(tmp_path):
    from controller.networked.networked import CentralController
    controller = CentralController(config_file=str(tmp_path / 'missing.ini'))
    controller.mqtt_connected = True
    controller.mqtt_client = MagicMock()
    controller.mqtt_client.publish.return_value = MagicMock(rc=0)
    controller.owls_state = {'owl-1': {'connected': True}}
    return controller


def _sent(controller):
    return [json.loads(c.args[1]) for c in controller.mqtt_client.publish.call_args_list
            if c.args[0] == 'owl/owl-1/commands']


@pytest.mark.parametrize('value', ['false', '0', 'off', False, 0])
def test_controller_off_value_sends_spraying_off(ctrl, value):
    result = ctrl.send_command('all', 'toggle_detection', value)
    assert result['success'] is True
    assert _sent(ctrl) == [{'action': 'set_detection_enable', 'value': False}]
    assert ctrl.desired_state['all'].get('spray') == 'off'


@pytest.mark.parametrize('value', ['true', '1', True])
def test_controller_on_value_sends_spot(ctrl, value):
    ctrl.send_command('all', 'toggle_detection', value)
    assert _sent(ctrl) == [{'action': 'set_detection_enable', 'value': True}]


@pytest.mark.parametrize('action', ['toggle_detection', 'toggle_recording', 'set_recording_paused'])
@pytest.mark.parametrize('value', ['banana', 2, [], {}])
def test_controller_refuses_unclear_values_and_sends_nothing(ctrl, action, value):
    before = json.dumps(ctrl.desired_state, sort_keys=True, default=str)
    result = ctrl.send_command('all', action, value)
    assert result['success'] is False and result['refused'] is True
    assert _sent(ctrl) == []
    assert json.dumps(ctrl.desired_state, sort_keys=True, default=str) == before


@pytest.mark.parametrize('value, sent', [('false', False), (0, False), ('true', True)])
def test_controller_tool_entry_point(ctrl, value, sent):
    """CentralController.set_detection_enable is what the AI agent calls."""
    ctrl.set_detection_enable(value)
    assert _sent(ctrl) == [{'action': 'set_detection_enable', 'value': sent}]


def test_controller_tool_entry_point_never_toggles_on_none(ctrl):
    result = ctrl.set_detection_enable(None)
    assert result['success'] is False
    assert _sent(ctrl) == []


def test_controller_http_route_refuses_with_400(ctrl, monkeypatch):
    import controller.networked.networked as net_mod
    monkeypatch.setattr(net_mod, 'controller', ctrl)
    net_mod.app.config['TESTING'] = True
    resp = net_mod.app.test_client().post(
        '/api/command', json={'device_id': 'all', 'action': 'toggle_detection', 'value': 'banana'})
    assert resp.status_code == 400
    assert _sent(ctrl) == []


# --------------------------------------------------------------------------
# AI agent tool
# --------------------------------------------------------------------------

def _tool(**kwargs):
    from agent import tool_registry
    client = MagicMock()
    func = getattr(tool_registry.set_detection, '__wrapped__', tool_registry.set_detection)
    func(mqtt_client=client, **kwargs)
    return client.mock_calls


@pytest.mark.parametrize('enabled', ['false', 'False', 0, '0', 'off', False])
def test_agent_enabled_false_in_any_spelling_is_off(enabled):
    from unittest.mock import call
    assert _tool(enabled=enabled) == [call.set_detection_enable(False)]


@pytest.mark.parametrize('enabled', ['false', 0, False])
@pytest.mark.parametrize('mode', [0, 2])
def test_agent_enabled_false_beats_any_mode(enabled, mode):
    """enabled="false" with mode=2 used to blanket spray."""
    from unittest.mock import call
    assert _tool(enabled=enabled, mode=mode) == [call.set_detection_enable(False)]


@pytest.mark.parametrize('kwargs', [{'enabled': 'banana'}, {'enabled': 2}, {}])
def test_agent_unclear_request_sends_nothing(kwargs):
    from agent import tool_registry
    client = MagicMock()
    func = getattr(tool_registry.set_detection, '__wrapped__', tool_registry.set_detection)
    with pytest.raises(ValueError):
        func(mqtt_client=client, **kwargs)
    assert client.mock_calls == []
