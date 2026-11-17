"""Stand-ins for the two things the agent tests cannot have: the LLM
provider's HTTP API and the MQTT broker. Everything between them is real.

    LLMWire            replaces httpx.Client inside agent.llm_provider. Each
                       stream request gets the next scripted reply (SSE lines
                       as the provider's API sends them), so the real
                       AnthropicProvider / OpenAIProvider parse it.
    dash_subscriber()  a real DashMQTTSubscriber (the standalone dashboard's
                       client) whose paho client records publishes.
    central()          a real CentralController (the in-cab controller) whose
                       paho client records publishes.
"""

import copy
import json
import time
from unittest.mock import MagicMock


# --------------------------------------------------------------------------
# The LLM provider's HTTP API
# --------------------------------------------------------------------------

def says(text, input_tokens=0, output_tokens=0):
    """Anthropic SSE lines for a plain text reply."""
    lines = []
    if input_tokens:
        lines.append({'type': 'message_start', 'message': {'usage': {'input_tokens': input_tokens}}})
    lines += [{'type': 'content_block_start', 'content_block': {'type': 'text'}},
              {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': text}},
              {'type': 'content_block_stop'}]
    if output_tokens:
        lines.append({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'},
                      'usage': {'output_tokens': output_tokens}})
    return ['data: ' + json.dumps(line) for line in lines]


def calls(*tool_calls):
    """Anthropic SSE lines for a reply that calls tools:
    calls(('set_detection', {'state': 'off'}), ...). Ids are tc_1, tc_2, ..."""
    lines = []
    for n, (name, arguments) in enumerate(tool_calls, start=1):
        lines += [{'type': 'content_block_start',
                   'content_block': {'type': 'tool_use', 'id': f'tc_{n}', 'name': name}},
                  {'type': 'content_block_delta',
                   'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(arguments)}},
                  {'type': 'content_block_stop'}]
    return ['data: ' + json.dumps(line) for line in lines]


def openai_says(text):
    return ['data: ' + json.dumps({'choices': [{'delta': {'content': text}}]}), 'data: [DONE]']


class HTTPError:
    """A scripted non-200 reply."""

    def __init__(self, status, text=''):
        self.status, self.text = status, text


class _Reply:
    def __init__(self, scripted):
        failed = isinstance(scripted, HTTPError)
        self.status_code = scripted.status if failed else 200
        self.text = scripted.text if failed else ''
        self._lines = [] if failed else scripted

    def iter_lines(self):
        yield from self._lines

    def read(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class LLMWire:
    """wire.replies = [says('hi'), calls((...)), HTTPError(429, 'slow down')]
    wire.key_check  = 200 | 401 | HTTPError(400, 'credit balance too low')
    wire.requests   = every JSON body the provider posted, in order"""

    def __init__(self):
        self.replies = []
        self.key_check = 200
        self.requests = []
        self.headers = []

    # httpx.Client(timeout=...) -> context manager with .stream / .post
    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, json=None, headers=None):
        self.requests.append(copy.deepcopy(json))     # as sent: the session list changes later
        self.headers.append(headers)
        assert self.replies, f'the provider asked for a reply nobody scripted: {json}'
        return _Reply(self.replies.pop(0))

    def post(self, url, json=None, headers=None):
        self.requests.append(copy.deepcopy(json))     # as sent: the session list changes later
        self.headers.append(headers)
        check = self.key_check
        return _Reply(check if isinstance(check, HTTPError) else
                      (HTTPError(check) if check != 200 else []))


# --------------------------------------------------------------------------
# The broker
# --------------------------------------------------------------------------

def dash_subscriber(connected=True):
    """The standalone dashboard's MQTT client, never connected to a broker."""
    from utils.mqtt_manager import DashMQTTSubscriber

    sub = DashMQTTSubscriber()
    sub.client = MagicMock()
    sub.connected = connected
    return sub


def central(tmp_path, owls=('owl-1',), ini_text=''):
    """The in-cab controller with each OWL named connected and just heard from."""
    import controller.networked.networked as net_mod

    ini = tmp_path / 'cab_unit.ini'
    ini.write_text(ini_text)
    ctrl = net_mod.CentralController(config_file=str(ini))
    ctrl.mqtt_connected = True
    ctrl.mqtt_client = MagicMock()
    ctrl.mqtt_client.publish.return_value = MagicMock(rc=0)
    now = time.time()
    ctrl.owls_state = {oid: {'device_id': oid, 'connected': True, 'last_seen': now} for oid in owls}
    return ctrl


def published(client):
    """[(topic, payload dict), ...] for either client, in order."""
    paho = client.client if hasattr(client, 'client') else client.mqtt_client
    return [(c.args[0], json.loads(c.args[1])) for c in paho.publish.call_args_list]


def commands(client):
    """Only the payloads, when the topic is not the point."""
    return [payload for _, payload in published(client)]


def forget(client):
    paho = client.client if hasattr(client, 'client') else client.mqtt_client
    paho.publish.reset_mock()


def sse_events(response):
    """The events of a text/event-stream Flask response, as dicts."""
    return [json.loads(line[len('data: '):])
            for line in response.get_data(as_text=True).split('\n\n') if line.startswith('data: ')]
