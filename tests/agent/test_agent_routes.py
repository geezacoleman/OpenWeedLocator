"""The real /api/agent/* routes of both dashboards.

    HTTP request -> the route in networked.py / standalone.py -> the app's own
    AgentEngine and ToolRegistry -> the real AnthropicProvider / OpenAIProvider
    -> (scripted HTTP reply) -> the tool -> the dashboard's real MQTT client
    -> the exact message it publishes

Only the provider's HTTP API and the broker are stand-ins (agent_fakes.py).
Until the 2026-11-16 audit these routes were not called by any test: the
"route" tests ran copies of the route written inside the test file.
"""

import base64

import pytest

from agent_fakes import HTTPError, calls, openai_says, says

# Topic each dashboard publishes commands on
TOPIC = {'standalone': 'owl/commands', 'networked': 'owl/owl-1/commands'}


def of_type(events, kind):
    return [e['data'] for e in events if e['type'] == kind]


# --------------------------------------------------------------------------
# /api/agent/connect and /api/agent/status
# --------------------------------------------------------------------------

class TestConnect:
    def test_a_good_key_connects_and_status_says_so(self, agent_app, llm):
        assert agent_app.client.get('/api/agent/status').get_json() == {
            'connected': False, 'provider': None, 'model': None}

        response = agent_app.client.post('/api/agent/connect',
                                         json={'api_key': '  sk-ant-123  ', 'provider': 'anthropic'})

        assert (response.status_code, response.get_json()) == (
            200, {'status': 'connected', 'model': 'claude-sonnet-4-6'})
        assert llm.headers[0]['x-api-key'] == 'sk-ant-123', 'the key is sent stripped'
        assert agent_app.client.get('/api/agent/status').get_json() == {
            'connected': True, 'provider': 'anthropic', 'model': 'claude-sonnet-4-6'}

    def test_openai_is_the_other_provider(self, agent_app, llm):
        response = agent_app.client.post('/api/agent/connect',
                                         json={'api_key': 'sk-oai', 'provider': 'openai'})
        assert response.get_json() == {'status': 'connected', 'model': 'gpt-4o-mini'}
        assert llm.headers[0]['Authorization'] == 'Bearer sk-oai'
        assert agent_app.client.get('/api/agent/status').get_json()['provider'] == 'openai'

    def test_a_rejected_key_is_401_and_leaves_the_agent_disconnected(self, agent_app, llm):
        llm.key_check = 401
        response = agent_app.client.post('/api/agent/connect', json={'api_key': 'sk-bad'})
        assert (response.status_code, response.get_json()) == (401, {'error': 'Invalid API key'})
        assert agent_app.client.get('/api/agent/status').get_json()['connected'] is False

    @pytest.mark.parametrize('reply, shown', [
        (HTTPError(400, 'Your credit balance is too low'), 'Your credit balance is too low'),
        (HTTPError(429, 'slow down'), 'rate limit'),
        (HTTPError(500, 'overloaded'), 'HTTP 500'),
    ])
    def test_the_providers_own_reason_reaches_the_farmer(self, agent_app, llm, reply, shown):
        """A 400 from the provider (no credit) used to read 'Invalid API key'."""
        llm.key_check = reply
        response = agent_app.client.post('/api/agent/connect', json={'api_key': 'sk-ok'})
        assert response.status_code == 400
        assert shown.lower() in response.get_json()['error'].lower()
        assert agent_app.client.get('/api/agent/status').get_json()['connected'] is False

    @pytest.mark.parametrize('body, error', [
        ({'api_key': '   '}, 'API key is required'),
        ({}, 'No JSON body'),
        ({'api_key': 'sk-ok', 'provider': 'gemini'}, 'Unknown provider'),
    ])
    def test_bad_requests_are_400_and_never_reach_the_provider(self, agent_app, llm, body, error):
        response = agent_app.client.post('/api/agent/connect', json=body)
        assert response.status_code == 400
        assert error in response.get_json()['error']
        assert llm.requests == []

    def test_a_failed_reconnect_drops_the_working_provider(self, agent_app, llm):
        """What the code does: a second connect with a bad key disconnects."""
        agent_app.connect(llm)
        llm.key_check = 401
        agent_app.client.post('/api/agent/connect', json={'api_key': 'sk-bad'})
        assert agent_app.client.get('/api/agent/status').get_json()['connected'] is False


# --------------------------------------------------------------------------
# /api/agent/chat
# --------------------------------------------------------------------------

class TestChat:
    def test_a_text_reply_streams_and_ends_with_the_session_totals(self, agent_app, llm):
        agent_app.connect(llm)
        llm.replies = [says('Hello farmer', input_tokens=50, output_tokens=7)]

        events = agent_app.ask('  Hi  ', session_id='session_1')

        assert events == [
            {'type': 'usage', 'data': {'input_tokens': 50}},
            {'type': 'text_delta', 'data': 'Hello farmer'},
            {'type': 'usage', 'data': {'output_tokens': 7}},
            {'type': 'done', 'data': {'input_tokens': 50, 'output_tokens': 7, 'message_count': 2}},
        ]
        [request] = llm.requests
        assert request['messages'] == [{'role': 'user', 'content': 'Hi'}]
        assert request['stream'] is True
        assert 'set_detection' in {tool['name'] for tool in request['tools']}
        assert 'OWL Assistant' in request['system']

    def test_without_a_provider_the_stream_says_to_connect(self, agent_app, llm):
        events = agent_app.ask('Hi')
        assert of_type(events, 'error') == ['No provider configured. Please connect first.']
        assert events[-1]['type'] == 'done'
        assert llm.requests == []

    @pytest.mark.parametrize('arguments, payload', [
        ({'state': 'blanket'}, {'action': 'set_detection_mode', 'value': 2}),
        ({'state': 'spot'}, {'action': 'set_detection_mode', 'value': 0}),
        ({'state': 'off'}, {'action': 'set_detection_mode', 'value': 1}),
        ({'enabled': 'false'}, {'action': 'set_detection_enable', 'value': False}),
        ({'enabled': False, 'mode': 2}, {'action': 'set_detection_enable', 'value': False}),
    ])
    def test_a_spray_tool_call_publishes_exactly_one_command(self, agent_app, llm, arguments, payload):
        """The whole chain, for the calls that open or close nozzles. "false"
        as a string used to switch spot spraying on."""
        agent_app.connect(llm)
        llm.replies = [calls(('set_detection', arguments)), says('Done.')]

        events = agent_app.ask('change spraying')

        assert agent_app.published() == [(TOPIC[agent_app.kind], payload)]
        [result] = of_type(events, 'tool_result')
        assert (result['tool_name'], result['tool_id']) == ('set_detection', 'tc_1')
        assert result['result']['success'] is True
        assert of_type(events, 'text_delta') == ['Done.']
        # the model is shown its own call and the tool's answer on the next turn
        followup = llm.requests[1]['messages']
        assert followup[1] == {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'tc_1', 'name': 'set_detection', 'input': arguments}]}
        assert followup[2]['content'][0]['type'] == 'tool_result'
        assert followup[2]['content'][0]['tool_use_id'] == 'tc_1'

    @pytest.mark.parametrize('name, arguments, reason', [
        ('set_detection', {}, "Give state: 'off', 'spot' or 'blanket'."),
        ('set_detection', {'enabled': 'maybe'}, 'Invalid enabled value'),
        ('set_detection', {'state': 'on'}, 'Invalid spray state'),
        ('set_detection', {'mode': 3}, 'Invalid detection mode'),
        ('set_config_param', {'section': 'Relays', 'key': '0', 'value': '7'}, 'protected'),
        ('set_config_param', {'section': 'System'}, "Missing required parameter 'key'"),
        ('open_all_valves', {}, 'Unknown tool'),
    ])
    def test_a_refused_tool_call_publishes_nothing_and_tells_the_model_why(
            self, agent_app, llm, name, arguments, reason):
        agent_app.connect(llm)
        llm.replies = [calls((name, arguments)), says('Sorry.')]

        events = agent_app.ask('do it')

        assert agent_app.published() == []
        [result] = of_type(events, 'tool_result')
        assert reason in result['result']['error']
        assert 'success' not in result['result']

    def test_tool_arguments_the_provider_cut_short_do_not_spray(self, agent_app, llm):
        """Half a JSON object parses to no arguments at all; set_detection
        with no arguments used to mean 'enable'."""
        agent_app.connect(llm)
        cut = calls(('set_detection', {'state': 'off'}))
        cut[1] = cut[1].replace('\\"off\\"}', '')           # '{"state": ' and nothing more
        llm.replies = [cut, says('Hm.')]

        events = agent_app.ask('stop')

        assert agent_app.published() == []
        assert 'Give state' in of_type(events, 'tool_result')[0]['result']['error']

    @pytest.mark.parametrize('reply, shown', [
        (HTTPError(429, 'busy'), 'Rate limit exceeded. Try again in 30 seconds.'),
        (HTTPError(401, 'revoked'), 'Authentication failed'),
        (HTTPError(500, 'boom'), 'Provider error: API error (HTTP 500): boom'),
    ])
    def test_a_provider_failure_is_an_error_event_not_a_broken_stream(self, agent_app, llm, reply, shown):
        agent_app.connect(llm)
        llm.replies = [reply]
        events = agent_app.ask('Hi')
        [error] = of_type(events, 'error')
        assert shown in error
        assert events[-1]['type'] == 'done'

    def test_an_exception_inside_the_stream_is_reported_in_the_stream(self, agent_app, llm, monkeypatch):
        agent_app.connect(llm)
        llm.replies = [says('Hello')]

        def broken(session_id):
            raise RuntimeError('disk full')
        monkeypatch.setattr(agent_app.engine, 'get_session_info', broken)

        events = agent_app.ask('Hi')
        assert events[-1] == {'type': 'error', 'data': 'disk full'}

    @pytest.mark.parametrize('body, error', [
        ({'message': '   '}, 'Message or image is required'),
        ({}, 'No JSON body'),
        ({'message': 'look', 'images': ['AAAA'] * 5}, 'Maximum 4 images per message'),
        ({'message': 'look', 'images': ['A' * 1_400_001]}, 'Invalid or oversized image'),
        ({'message': 'look', 'images': [{'data': 'AAAA'}]}, 'Invalid or oversized image'),
    ])
    def test_bad_chat_requests_are_400_before_anything_is_sent(self, agent_app, llm, body, error):
        agent_app.connect(llm)
        response = agent_app.client.post('/api/agent/chat', json=body)
        assert response.status_code == 400
        assert error in response.get_json()['error']
        assert llm.requests == []
        assert agent_app.engine._sessions == {}

    def test_four_images_of_the_largest_size_go_to_the_provider_before_the_text(self, agent_app, llm):
        agent_app.connect(llm)
        llm.replies = [says('I see weeds.')]
        big = 'A' * 1_400_000

        events = agent_app.ask('What is this?', images=[big, 'BBBB', 'CCCC', 'DDDD'])

        assert of_type(events, 'text_delta') == ['I see weeds.']
        content = llm.requests[0]['messages'][0]['content']
        assert [block['type'] for block in content] == ['image'] * 4 + ['text']
        assert content[0]['source'] == {'type': 'base64', 'media_type': 'image/jpeg', 'data': big}
        assert content[3]['source']['data'] == 'DDDD'
        assert content[4] == {'type': 'text', 'text': 'What is this?'}

    def test_an_image_alone_is_a_message(self, agent_app, llm):
        agent_app.connect(llm)
        llm.replies = [says('A paddock.')]
        agent_app.ask(images=['AAAA'])
        assert llm.requests[0]['messages'][0]['content'] == [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': 'AAAA'}}]

    def test_openai_gets_images_as_data_urls_and_tools_as_functions(self, agent_app, llm):
        agent_app.connect(llm, provider='openai')
        llm.replies = [openai_says('Ryegrass.')]

        events = agent_app.ask('What weed?', images=['AAAA'])

        assert of_type(events, 'text_delta') == ['Ryegrass.']
        request = llm.requests[0]
        assert request['messages'][0]['role'] == 'system'
        assert request['messages'][1] == {'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,AAAA'}},
            {'type': 'text', 'text': 'What weed?'}]}
        assert {tool['type'] for tool in request['tools']} == {'function'}
        assert 'set_detection' in {tool['function']['name'] for tool in request['tools']}

    @pytest.mark.xfail(strict=True, reason=(
        "session_id from the request body is used as a file name unchecked: "
        "agent/engine.py _save_session (filepath = sessions_dir / f'{session_id}.json') "
        "writes outside the sessions directory for '../x'"))
    def test_a_session_id_cannot_write_outside_the_sessions_directory(self, agent_app, llm):
        agent_app.connect(llm)
        llm.replies = [says('ok')]
        agent_app.ask('Hi', session_id='../escaped')
        outside = agent_app.engine.sessions_dir.parent / 'escaped.json'
        assert not outside.exists()


def test_the_cab_controllers_agent_is_for_the_cab_screen_only(networked_app, llm):
    """Everyone else on the rig network gets /demo: the agent can spray."""
    networked_app.connect(llm)
    response = networked_app.client.post(
        '/api/agent/chat', json={'message': 'blanket spray'},
        environ_base={'REMOTE_ADDR': '192.168.1.77'})
    assert response.status_code == 403
    assert llm.requests == []


# --------------------------------------------------------------------------
# Missing engine (the agent package failed to import at startup)
# --------------------------------------------------------------------------

@pytest.mark.parametrize('method, path, status, body', [
    ('post', '/api/agent/connect', 500, {'error': 'Agent engine not available'}),
    ('post', '/api/agent/chat', 500, {'error': 'Agent engine not available'}),
    ('get', '/api/agent/status', 200, {'connected': False, 'provider': None, 'model': None}),
    ('get', '/api/agent/sessions', 200, []),
    ('get', '/api/agent/sessions/session_1', 500, {'error': 'Agent engine not available'}),
    ('delete', '/api/agent/sessions/session_1', 500, {'error': 'Agent engine not available'}),
])
def test_every_route_answers_when_the_engine_is_missing(agent_app, monkeypatch, method, path, status, body):
    if agent_app.kind == 'networked':
        import controller.networked.networked as net_mod
        monkeypatch.setattr(net_mod, 'agent_engine', None)
    else:
        monkeypatch.setattr(agent_app.dashboard, 'agent_engine', None)
    kwargs = {'json': {'api_key': 'k', 'message': 'hi'}} if method == 'post' else {}
    response = getattr(agent_app.client, method)(path, **kwargs)
    assert (response.status_code, response.get_json()) == (status, body)


# --------------------------------------------------------------------------
# /api/agent/sessions
# --------------------------------------------------------------------------

class TestSessions:
    def test_a_chat_is_saved_listed_loaded_and_deleted(self, agent_app, llm):
        agent_app.connect(llm)
        llm.replies = [says('Hello', input_tokens=12, output_tokens=3)]
        agent_app.ask('How is the boom?', session_id='session_42')

        [listed] = agent_app.client.get('/api/agent/sessions').get_json()
        assert (listed['id'], listed['title'], listed['message_count'],
                listed['input_tokens'], listed['output_tokens']) == (
            'session_42', 'How is the boom?', 2, 12, 3)

        loaded = agent_app.client.get('/api/agent/sessions/session_42').get_json()
        assert loaded['messages'] == [{'role': 'user', 'content': 'How is the boom?'},
                                      {'role': 'assistant', 'content': 'Hello'}]

        deleted = agent_app.client.delete('/api/agent/sessions/session_42')
        assert (deleted.status_code, deleted.get_json()) == (200, {'status': 'deleted'})
        assert agent_app.client.get('/api/agent/sessions').get_json() == []
        assert agent_app.client.get('/api/agent/sessions/session_42').status_code == 404
        assert agent_app.client.delete('/api/agent/sessions/session_42').status_code == 404

    def test_a_loaded_session_is_what_the_next_message_continues(self, agent_app, llm):
        agent_app.connect(llm)
        llm.replies = [says('First answer')]
        agent_app.ask('First question', session_id='session_7')
        agent_app.engine._sessions.clear()                     # the dashboard restarted

        agent_app.client.get('/api/agent/sessions/session_7')
        llm.replies = [says('Second answer')]
        agent_app.ask('Second question', session_id='session_7')

        assert [m['content'] for m in llm.requests[-1]['messages']] == [
            'First question', 'First answer', 'Second question']


# --------------------------------------------------------------------------
# /api/agent/grab_frame
# --------------------------------------------------------------------------

JPEG = b'\xff\xd8 not really a jpeg \xff\xd9'


class TestGrabFrameNetworked:
    @pytest.fixture
    def fetched(self, monkeypatch):
        """What the controller asked the OWL for, and what the OWL answers."""
        import controller.networked.networked as net_mod
        from types import SimpleNamespace

        asked = SimpleNamespace(urls=[], reply=SimpleNamespace(status_code=200, content=JPEG))

        def get(url, **kwargs):
            asked.urls.append(url)
            if isinstance(asked.reply, Exception):
                raise asked.reply
            return asked.reply
        monkeypatch.setattr(net_mod.requests, 'get', get)
        return asked

    def test_the_owls_frame_comes_back_as_base64(self, networked_app, fetched):
        response = networked_app.client.get('/api/agent/grab_frame/owl_1')
        assert response.get_json() == {'image': base64.b64encode(JPEG).decode(), 'device_id': 'owl-1'}
        assert fetched.urls == ['https://owl-1.local/latest_frame.jpg']

    def test_the_owls_reported_address_is_used_when_it_has_one(self, networked_app, fetched):
        networked_app.mqtt.owls_state['owl-1']['static_ip'] = '10.42.0.11'
        networked_app.client.get('/api/agent/grab_frame/owl-1')
        assert fetched.urls == ['https://10.42.0.11/latest_frame.jpg']

    def test_an_owl_that_cannot_give_a_frame_is_502(self, networked_app, fetched):
        import requests
        fetched.reply.status_code = 503
        response = networked_app.client.get('/api/agent/grab_frame/owl-1')
        assert (response.status_code, response.get_json()) == (
            502, {'error': 'Could not get frame from owl-1'})

        fetched.reply = requests.exceptions.ConnectionError('no route')
        response = networked_app.client.get('/api/agent/grab_frame/owl-1')
        assert (response.status_code, response.get_json()) == (502, {'error': 'owl-1 is offline'})


class TestGrabFrameStandalone:
    def test_the_local_frame_comes_back_as_base64(self, standalone_app, monkeypatch):
        import io
        import controller.standalone.standalone as sa_mod

        asked = []

        def urlopen(url, timeout=None):
            asked.append((url, timeout))
            return io.BytesIO(JPEG)
        monkeypatch.setattr(sa_mod.urllib.request, 'urlopen', urlopen)

        response = standalone_app.client.get('/api/agent/grab_frame')
        assert response.get_json() == {'image': base64.b64encode(JPEG).decode()}
        assert asked == [('http://127.0.0.1:8001/latest_frame.jpg', 2)]

    def test_no_frame_is_502(self, standalone_app, monkeypatch):
        import controller.standalone.standalone as sa_mod

        def urlopen(url, timeout=None):
            raise OSError('connection refused')
        monkeypatch.setattr(sa_mod.urllib.request, 'urlopen', urlopen)

        response = standalone_app.client.get('/api/agent/grab_frame')
        assert response.status_code == 502
        assert 'connection refused' in response.get_json()['error']
