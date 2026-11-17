"""The AI agent's spray tool, judged by what the nozzles do.

    ToolRegistry.call('set_detection', ...)  (what the model's tool call runs)
      -> the dashboard's real MQTT client: DashMQTTSubscriber (standalone)
         or CentralController (in-cab controller)
      -> the exact message it publishes, on the topic it publishes it
      -> a real Owl's MQTT command handler -> the relay pins

The broker is the only stand-in: a message published on the OWL's command
topic is handed to the OWL's handler. tests/agent proves the message each
client publishes and the HTTP route in front of the tool; this is the one
place the agent is tied to actuation.
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness import PROMPT_S


def agent_for(r, dash, tmp_path):
    """call(**arguments) runs the agent's set_detection tool on the client
    that dashboard uses, wired to this OWL."""
    from agent.tool_registry import ToolRegistry

    def broker(topic, payload, *args, **kwargs):
        assert topic == r.owl.dash.topics['commands'], \
            f'published on {topic}; this OWL listens on {r.owl.dash.topics["commands"]}'
        fields = json.loads(payload)
        r.command(fields.pop('action'), **fields)
        return SimpleNamespace(rc=0)

    if dash == 'standalone':
        from utils.mqtt_manager import DashMQTTSubscriber
        client = DashMQTTSubscriber()
        client.connected = True
        client.client = MagicMock()
        client.client.publish.side_effect = broker
    else:
        from controller.networked.networked import CentralController
        # not named CONTROLLER.ini: the rig ignores files of that name
        ini = tmp_path / 'cab_unit.ini'
        ini.write_text('')
        client = CentralController(config_file=str(ini))
        client.mqtt_connected = True
        client.mqtt_client = MagicMock()
        client.mqtt_client.publish.side_effect = broker
        device_id = r.owl.dash.device_id
        client.owls_state = {device_id: {'device_id': device_id, 'connected': True,
                                         'last_seen': time.time()}}

    registry = ToolRegistry(developer_mode=False)
    registry.discover()

    def call(**arguments):
        result = registry.call('set_detection', arguments, {'mqtt_client': client})
        assert result['success'] is True, result
        return time.monotonic()

    return call


def no_relay_switched_on_since(r, moment):
    late = [(round(at - moment, 3), relay) for at, relay, on in r.drives if on and at > moment]
    assert not late, f'relays switched on after the agent turned spraying off: {late}'


@pytest.mark.parametrize('dash', ['standalone', 'networked'])
def test_the_agents_spray_tool_is_what_the_nozzles_do(rig, dash, tmp_path):
    r = rig(dash=dash)                         # spot spraying, as the operator left it
    agent = agent_for(r, dash, tmp_path)

    # "false" as a string is OFF (bool("false") is True: it used to switch
    # spot spraying on). A weed is under the camera and its nozzle is open
    # when the agent says stop.
    r.cam.card = True
    r.wait_for(lambda: r.relays_on(), 'the weed to switch a relay', timeout=3.0)
    asked = agent(enabled='false')
    r.wait_all_off(timeout=PROMPT_S)
    time.sleep(0.8)                            # the weed is still in view
    no_relay_switched_on_since(r, asked)
    r.cam.card = False

    # blanket: every nozzle on, with no weed in view
    agent(state='blanket')
    r.wait_all_on(timeout=PROMPT_S)
    assert r.owl.relay_controller.mode == 'blanket'

    # off: everything closes and a weed does not open it again
    asked = agent(state='off')
    r.wait_all_off(timeout=PROMPT_S)
    r.show_card(settle=0.6)
    no_relay_switched_on_since(r, asked)
    assert r.owl.relay_controller.mode == 'off'

    # spot: the next weed switches its own relay at once, and it lets go
    agent(state='spot')
    showing = r.show_card(x=0.62)
    assert showing.fired == [2]
    assert showing.on < PROMPT_S, f'relay waited {showing.on:.2f} s'
    assert not r.relays_on()
