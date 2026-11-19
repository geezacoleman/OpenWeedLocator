"""The agent loop (agent/engine.py): one user message in, provider turns and
tool calls until the model answers in text.

The registry and its tools are the real ones, on a real DashMQTTSubscriber
(agent_fakes.dash_subscriber). The provider is scripted one level above HTTP:
a real AnthropicProvider / OpenAIProvider whose stream_chat hands out the
chunks listed, so the engine picks its message format from the provider's
type as it does in production. The same loop through the real HTTP parsing
and the Flask routes is test_agent_routes.py.
"""

import copy
import json

import pytest

from agent.engine import MAX_HISTORY, MAX_ITERATIONS, AgentEngine
from agent.llm_provider import (
    AnthropicProvider,
    AuthenticationError,
    OpenAIProvider,
    ProviderError,
    RateLimitError,
    StreamChunk,
    ToolCall,
)
from agent.tool_registry import owl_tool
from agent_fakes import commands, dash_subscriber


def text(words):
    return StreamChunk(type='text_delta', data=words)


def usage(**tokens):
    return StreamChunk(type='usage', data=tokens)


def call(name, arguments=None, id='tc1'):
    return StreamChunk(type='tool_call', data=ToolCall(id=id, name=name, arguments=arguments or {}))


def scripted(turns, kind=AnthropicProvider):
    """A provider that answers turn by turn. A turn is a list of chunks, or
    an exception to raise. `seen` is what each turn was given."""

    class Scripted(kind):
        def __init__(self):
            super().__init__(api_key='sk-test')
            self.turns = list(turns)
            self.seen = []

        def stream_chat(self, messages, tools=None, system=None):
            self.seen.append({'messages': copy.deepcopy(messages), 'tools': tools, 'system': system})
            turn = self.turns.pop(0)
            if isinstance(turn, Exception):
                raise turn
            yield from turn

    return Scripted()


@pytest.fixture
def owl_client():
    client = dash_subscriber()
    client.current_state = {'algorithm': 'exhsv', 'detection_enable': True, 'exg_min': 25}
    return client


@pytest.fixture
def engine(registry, owl_client, tmp_path):
    def build(turns, kind=AnthropicProvider, **kwargs):
        return AgentEngine(tool_registry=registry, provider=scripted(turns, kind),
                           context={'mqtt_client': owl_client, 'config': None, 'widget_manager': None},
                           sessions_dir=str(tmp_path / 'sessions'), **kwargs)
    return build


def results(chunks):
    return [c.data for c in chunks if c.type == 'tool_result']


def errors(chunks):
    return [c.data for c in chunks if c.type == 'error']


# --------------------------------------------------------------------------
# A plain answer
# --------------------------------------------------------------------------

class TestTextReply:
    def test_the_reply_is_streamed_and_both_sides_are_kept(self, engine):
        agent = engine([[text('Hello'), text(' farmer!'), usage(input_tokens=50, output_tokens=10)]])

        chunks = list(agent.chat('s1', 'Hi there'))

        assert [(c.type, c.data) for c in chunks] == [
            ('text_delta', 'Hello'), ('text_delta', ' farmer!'),
            ('usage', {'input_tokens': 50, 'output_tokens': 10})]
        assert agent._get_session('s1').messages == [
            {'role': 'user', 'content': 'Hi there'},
            {'role': 'assistant', 'content': 'Hello farmer!'}]
        assert agent.get_session_info('s1') == {'input_tokens': 50, 'output_tokens': 10, 'message_count': 2}

    def test_tokens_add_up_over_a_conversation_and_sessions_are_separate(self, engine):
        agent = engine([[text('a'), usage(input_tokens=100, output_tokens=20)],
                        [text('b'), usage(input_tokens=80), usage(output_tokens=15)],
                        [text('c'), usage(input_tokens=7, output_tokens=1)]])
        list(agent.chat('s1', 'one'))
        list(agent.chat('s1', 'two'))
        list(agent.chat('s2', 'other'))
        assert agent.get_session_info('s1') == {'input_tokens': 180, 'output_tokens': 35, 'message_count': 4}
        assert agent.get_session_info('s2') == {'input_tokens': 7, 'output_tokens': 1, 'message_count': 2}
        # the second turn of s1 was given the first exchange, s2 was not
        assert [m['content'] for m in agent.provider.seen[1]['messages']] == ['one', 'a', 'two']
        assert [m['content'] for m in agent.provider.seen[2]['messages']] == ['other']

    def test_without_a_provider_nothing_is_recorded(self, registry):
        agent = AgentEngine(tool_registry=registry)
        chunks = list(agent.chat('s1', 'Hi'))
        assert [(c.type, c.data) for c in chunks] == [
            ('error', 'No provider configured. Please connect first.')]
        assert agent.get_session_info('s1')['message_count'] == 0

    def test_the_model_is_told_the_owls_current_state(self, engine):
        agent = engine([[text('ok')]])
        list(agent.chat('s1', 'Hi'))
        system = agent.provider.seen[0]['system']
        assert 'OWL Assistant' in system
        assert 'Current OWL state:\n  detection_enable: True\n  algorithm: exhsv\n  exg_min: 25' in system

    def test_no_state_no_state_block(self, engine, owl_client):
        owl_client.current_state = {}
        agent = engine([[text('ok')]])
        list(agent.chat('s1', 'Hi'))
        assert 'Current OWL state' not in agent.provider.seen[0]['system']

    @pytest.mark.parametrize('kind, name_of', [
        (AnthropicProvider, lambda tool: tool['name']),
        (OpenAIProvider, lambda tool: tool['function']['name']),
    ])
    def test_the_provider_gets_every_tool_in_its_own_schema_format(self, engine, registry, kind, name_of):
        agent = engine([[text('ok')]], kind=kind)
        list(agent.chat('s1', 'Hi'))
        offered = agent.provider.seen[0]['tools']
        assert sorted(name_of(tool) for tool in offered) == sorted(
            t['name'] for t in registry.list_tools())
        assert len(offered) == 17


# --------------------------------------------------------------------------
# Tool calls
# --------------------------------------------------------------------------

class TestToolLoop:
    def test_a_tool_runs_its_result_goes_back_and_the_model_answers(self, engine, owl_client):
        agent = engine([[text('Let me check...'), call('get_system_status')],
                        [text('Your algorithm is exhsv.')]])

        chunks = list(agent.chat('s1', 'What algorithm?'))

        status = {'status': {'algorithm': 'exhsv', 'detection_enable': True, 'exg_min': 25}}
        assert results(chunks) == [{'tool_name': 'get_system_status', 'tool_id': 'tc1', 'result': status}]
        assert [c.data for c in chunks if c.type == 'text_delta'] == [
            'Let me check...', 'Your algorithm is exhsv.']
        assert agent._get_session('s1').messages == [
            {'role': 'user', 'content': 'What algorithm?'},
            {'role': 'assistant', 'content': [
                {'type': 'text', 'text': 'Let me check...'},
                {'type': 'tool_use', 'id': 'tc1', 'name': 'get_system_status', 'input': {}}]},
            {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'tc1', 'content': json.dumps(status)}]},
            {'role': 'assistant', 'content': 'Your algorithm is exhsv.'}]
        # the second turn saw the tool's answer
        assert agent.provider.seen[1]['messages'][2]['content'][0]['type'] == 'tool_result'

    def test_openai_history_uses_tool_calls_and_tool_messages(self, engine):
        agent = engine([[call('set_sensitivity', {'level': 'high'})], [text('Done.')]], kind=OpenAIProvider)
        list(agent.chat('s1', 'More sensitive'))
        messages = agent._get_session('s1').messages
        assert messages[1] == {'role': 'assistant', 'tool_calls': [{
            'id': 'tc1', 'type': 'function',
            'function': {'name': 'set_sensitivity', 'arguments': '{"level": "high"}'}}]}
        assert (messages[2]['role'], messages[2]['tool_call_id']) == ('tool', 'tc1')
        assert json.loads(messages[2]['content'])['success'] is True

    def test_openai_keeps_text_said_alongside_a_tool_call(self, engine):
        agent = engine([[text('Checking.'), call('get_system_status')], [text('Done.')]],
                       kind=OpenAIProvider)
        list(agent.chat('s1', 'status?'))
        assert agent._get_session('s1').messages[1]['content'] == 'Checking.'

    def test_several_tool_calls_in_one_turn_all_run_in_order(self, engine, owl_client):
        agent = engine([[call('set_sensitivity', {'level': 'low'}, id='a'),
                         call('set_detection', {'state': 'off'}, id='b'),
                         call('set_config_param', {'section': 'Relays', 'key': '0', 'value': '1'}, id='c')],
                        [text('Done.')]])

        chunks = list(agent.chat('s1', 'Wind is up: back off and stop'))

        assert commands(owl_client) == [{'action': 'set_sensitivity_level', 'level': 'low'},
                                        {'action': 'set_detection_mode', 'value': 1}]
        assert [r['tool_id'] for r in results(chunks)] == ['a', 'b', 'c']
        assert 'protected' in results(chunks)[2]['result']['error']
        messages = agent._get_session('s1').messages
        assert [block['id'] for block in messages[1]['content']] == ['a', 'b', 'c']
        assert [m['content'][0]['tool_use_id'] for m in messages[2:5]] == ['a', 'b', 'c']

    def test_step_by_step_tool_use_ends_with_the_text_answer(self, engine, owl_client):
        agent = engine([[call('get_system_status')],
                        [call('set_config_param', {'section': 'GreenOnBrown', 'key': 'exg_min', 'value': '15'},
                              id='tc2')],
                        [text('exg_min is now 15.')]])
        chunks = list(agent.chat('s1', 'Lower exg_min to 15'))
        assert [r['tool_name'] for r in results(chunks)] == ['get_system_status', 'set_config_param']
        assert commands(owl_client) == [
            {'action': 'set_config', 'section': 'GreenOnBrown', 'key': 'exg_min', 'value': '15'}]
        assert chunks[-1].data == 'exg_min is now 15.'
        assert agent.provider.turns == []

    def test_a_model_that_never_stops_calling_tools_is_stopped(self, engine, owl_client):
        agent = engine([[call('set_sensitivity', {'level': 'high'})] for _ in range(MAX_ITERATIONS + 5)])

        chunks = list(agent.chat('session_loop', 'Loop forever'))

        assert MAX_ITERATIONS == 10
        assert len(results(chunks)) == len(commands(owl_client)) == 10
        assert errors(chunks) == ['Reached maximum tool iterations. Please try a simpler request.']
        assert chunks[-1].type == 'error'
        assert [s['id'] for s in agent.list_sessions()] == ['session_loop'], 'what it did is kept'


class TestToolFailures:
    """Whatever a tool does wrong, the model is told and the loop goes on."""

    @pytest.fixture
    def agent(self, engine, registry):
        @owl_tool(tier='developer', description='Read a file',
                  parameters={'path': {'type': 'string', 'required': True}})
        def read_file(path, **context):
            return {'content': 'secret'}

        @owl_tool(tier='observe', description='Breaks')
        def broken(**context):
            raise RuntimeError('sensor exploded')

        registry.register(read_file)
        registry.register(broken)
        return lambda name, arguments: engine([[call(name, arguments)], [text('Sorry.')]])

    @pytest.mark.parametrize('name, arguments, error', [
        ('read_file', {'path': '/etc/passwd'}, "Tool 'read_file' requires developer mode"),
        ('set_detection', {'state': 'sideways'},
         "Invalid spray state 'sideways'. Must be 'off', 'spot' or 'blanket'."),
        ('set_config_param', {'section': 'Camera'},
         "Missing required parameter 'key' for tool 'set_config_param'"),
        ('make_coffee', {}, "Unknown tool: 'Unknown tool: make_coffee'"),
        ('broken', {}, 'Tool execution failed: sensor exploded'),
    ])
    def test_the_error_is_the_tools_result(self, agent, owl_client, name, arguments, error):
        engine = agent(name, arguments)
        chunks = list(engine.chat('s1', 'do it'))
        [result] = results(chunks)
        assert result['result'] == {'error': error}
        assert commands(owl_client) == []
        assert chunks[-1].data == 'Sorry.', 'the model got its turn to explain'
        assert json.loads(engine._get_session('s1').messages[2]['content'][0]['content']) == result['result']


# --------------------------------------------------------------------------
# Provider failures
# --------------------------------------------------------------------------

class TestProviderFailures:
    @pytest.mark.parametrize('failure, shown', [
        (RateLimitError('Too fast', retry_after=42), 'Rate limit exceeded. Try again in 42 seconds.'),
        (RateLimitError('Too fast'), 'Rate limit exceeded. Try again in 30 seconds.'),
        (RateLimitError('Too fast', retry_after=0.4), 'Rate limit exceeded. Try again in 0 seconds.'),
        (AuthenticationError('Bad key'), 'Authentication failed: Bad key'),
        (ProviderError('Server down'), 'Provider error: Server down'),
        (RuntimeError('Something broke'), 'Unexpected error: Something broke'),
    ])
    def test_each_failure_is_one_plain_error(self, engine, failure, shown):
        agent = engine([failure])
        chunks = list(agent.chat('s1', 'Hi'))
        assert [(c.type, c.data) for c in chunks] == [('error', shown)]

    def test_a_failure_after_a_tool_ran_keeps_what_the_tool_did_in_the_history(self, engine, owl_client):
        agent = engine([[call('set_detection', {'state': 'off'})], ProviderError('gone')])
        chunks = list(agent.chat('s1', 'stop'))
        assert commands(owl_client) == [{'action': 'set_detection_mode', 'value': 1}]
        assert errors(chunks) == ['Provider error: gone']
        assert [m['role'] for m in agent._get_session('s1').messages] == ['user', 'assistant', 'user']

    def test_an_error_chunk_in_the_stream_is_passed_on_and_no_answer_is_invented(self, engine):
        agent = engine([[text('Half an ans'), StreamChunk(type='error', data='stream cut')]])
        chunks = list(agent.chat('s1', 'Hi'))
        assert [(c.type, c.data) for c in chunks] == [
            ('text_delta', 'Half an ans'), ('error', 'stream cut')]


# --------------------------------------------------------------------------
# History length
# --------------------------------------------------------------------------

def long_history(n):
    return [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': f'm{i}'} for i in range(n)]


class TestHistory:
    def test_only_the_last_fifty_messages_are_kept_and_sent(self, engine):
        agent = engine([[text('ok')]])
        agent._get_session('s1').messages = long_history(60)
        list(agent.chat('s1', 'newest'))
        sent = agent.provider.seen[0]['messages']
        assert MAX_HISTORY == 50
        # the cut lands on an assistant message (m11): the history sent
        # starts at the next question, as the provider requires
        assert len(sent) == 49
        assert (sent[0]['role'], sent[0]['content'], sent[-1]['content']) == ('user', 'm12', 'newest')

    def test_trimming_never_separates_a_tool_result_from_its_tool_call(self, engine):
        agent = engine([[text('ok')]])
        agent._get_session('s1').messages = [
            {'role': 'user', 'content': 'q'},
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'tc1', 'name': 'get_config',
                                              'input': {}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'tc1', 'content': '{}'}]},
            {'role': 'assistant', 'content': 'a'},
        ] + long_history(47)

        list(agent.chat('s1', 'newest'))                 # 52 messages -> the first two are cut

        sent = agent.provider.seen[0]['messages']
        called = {block['id'] for m in sent if isinstance(m['content'], list)
                  for block in m['content'] if block.get('type') == 'tool_use'}
        answered = {block['tool_use_id'] for m in sent if isinstance(m['content'], list)
                    for block in m['content'] if block.get('type') == 'tool_result'}
        assert answered <= called, 'a tool_result was sent without the tool_use it answers'


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------

IMAGE = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAAA'}}
QUESTION = {'type': 'text', 'text': 'What weed is this?'}


class TestImages:
    def test_anthropic_gets_the_content_array_as_stored(self, engine):
        agent = engine([[text('Ryegrass.')]])
        list(agent.chat('s1', [IMAGE, QUESTION]))
        assert agent.provider.seen[0]['messages'] == [{'role': 'user', 'content': [IMAGE, QUESTION]}]
        assert agent._get_session('s1').messages[0]['content'] == [IMAGE, QUESTION]

    def test_openai_gets_a_data_url_and_the_stored_history_stays_as_it_was(self, engine):
        agent = engine([[text('Ryegrass.')], [text('Yes.')]], kind=OpenAIProvider)
        list(agent.chat('s1', [IMAGE, QUESTION]))
        list(agent.chat('s1', 'Sure?'))
        for turn in agent.provider.seen:
            assert turn['messages'][0] == {'role': 'user', 'content': [
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}, QUESTION]}
        assert agent.provider.seen[1]['messages'][1:] == [
            {'role': 'assistant', 'content': 'Ryegrass.'}, {'role': 'user', 'content': 'Sure?'}]
        assert agent._get_session('s1').messages[0]['content'] == [IMAGE, QUESTION]

    def test_an_image_without_a_media_type_is_sent_as_jpeg(self, engine):
        agent = engine([[text('ok')]], kind=OpenAIProvider)
        list(agent.chat('s1', [{'type': 'image', 'source': {'data': 'BBBB'}}]))
        assert agent.provider.seen[0]['messages'][0]['content'] == [
            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,BBBB'}}]


# --------------------------------------------------------------------------
# Saved sessions
# --------------------------------------------------------------------------

class TestSavedSessions:
    def test_a_conversation_with_an_image_comes_back_from_disk_as_it_was(self, engine):
        agent = engine([[text('Ryegrass.'), usage(input_tokens=9, output_tokens=2)]])
        list(agent.chat('session_img', [IMAGE, QUESTION]))
        agent._sessions.clear()

        data = agent.load_session('session_img')

        assert data['title'] == 'What weed is this?'
        assert data['messages'] == [{'role': 'user', 'content': [IMAGE, QUESTION]},
                                    {'role': 'assistant', 'content': 'Ryegrass.'}]
        assert agent.get_session_info('session_img') == {
            'input_tokens': 9, 'output_tokens': 2, 'message_count': 2}

    @pytest.mark.parametrize('first_message, title', [
        ('x' * 200, 'x' * 80),
        ([IMAGE], 'New conversation'),
        ([IMAGE, {'type': 'text', 'text': 'y' * 100}], 'y' * 80),
    ])
    def test_the_title_is_the_start_of_the_first_thing_the_user_typed(self, engine, first_message, title):
        agent = engine([[text('ok')]])
        list(agent.chat('session_t', first_message))
        assert agent.list_sessions()[0]['title'] == title

    def test_sessions_are_listed_newest_first(self, engine):
        import os
        agent = engine([[text('a')], [text('b')], [text('c')]])
        for name in ('session_1', 'session_2', 'session_3'):
            list(agent.chat(name, f'hello from {name}'))
        # make the order unmistakable: files carry their own 'updated' stamp
        for name, stamp in (('session_1', 300.0), ('session_2', 100.0), ('session_3', 200.0)):
            path = agent.sessions_dir / f'{name}.json'
            data = json.loads(path.read_text())
            data['updated'] = stamp
            path.write_text(json.dumps(data))
        assert [s['id'] for s in agent.list_sessions()] == ['session_1', 'session_3', 'session_2']
        assert sorted(os.listdir(agent.sessions_dir)) == ['session_1.json', 'session_2.json',
                                                         'session_3.json'], 'no .tmp left behind'

    def test_a_conversation_that_got_no_answer_is_not_saved(self, engine):
        agent = engine([ProviderError('down')])
        list(agent.chat('session_x', 'Hi'))
        assert agent.list_sessions() == []

    def test_a_damaged_session_file_is_skipped_not_fatal(self, engine):
        agent = engine([[text('ok')]])
        list(agent.chat('session_good', 'Hi'))
        (agent.sessions_dir / 'session_bad.json').write_text('{half a file')
        assert [s['id'] for s in agent.list_sessions()] == ['session_good']
        assert agent.load_session('session_bad') is None
        assert agent.load_session('session_missing') is None

    def test_deleting_forgets_it_in_memory_too(self, engine):
        agent = engine([[text('ok')], [text('fresh')]])
        list(agent.chat('session_d', 'remember this'))
        assert agent.delete_session('session_d') is True
        assert agent.delete_session('session_d') is False
        list(agent.chat('session_d', 'again'))
        assert [m['content'] for m in agent.provider.seen[1]['messages']] == ['again']

    def test_an_engine_without_a_sessions_directory_keeps_working(self, registry):
        agent = AgentEngine(tool_registry=registry, provider=scripted([[text('ok')]]))
        chunks = list(agent.chat('s1', 'Hi'))
        assert chunks[-1].data == 'ok'
        assert (agent.list_sessions(), agent.load_session('s1'), agent.delete_session('s1')) == (
            [], None, False)
