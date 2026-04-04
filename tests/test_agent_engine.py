"""Tests for utils/agent_engine.py."""

import json
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from agent.engine import (
    AgentEngine,
    Session,
    MAX_ITERATIONS,
    MAX_HISTORY,
    SYSTEM_PROMPT_STATIC,
)
from agent.tool_registry import ToolRegistry, owl_tool
from agent.llm_provider import (
    LLMProvider,
    StreamChunk,
    ToolCall,
    ModelResponse,
    ProviderError,
    AuthenticationError,
    RateLimitError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_registry():
    """Create a registry with a couple of test tools."""
    registry = ToolRegistry(developer_mode=False)

    @owl_tool(tier='observe', description='Get status', parameters={})
    def get_system_status(**context):
        return {'status': {'algorithm': 'exhsv', 'detection_enable': True}}

    @owl_tool(
        tier='apply', description='Set param',
        parameters={
            'section': {'type': 'string', 'required': True},
            'key': {'type': 'string', 'required': True},
            'value': {'type': 'string', 'required': True},
        },
    )
    def set_config_param(section, key, value, **context):
        return {'success': True, 'message': f'Set [{section}] {key} = {value}'}

    @owl_tool(tier='developer', description='Read file',
              parameters={'path': {'type': 'string', 'required': True}})
    def read_file(path, **context):
        return {'content': 'file contents'}

    registry.register(get_system_status)
    registry.register(set_config_param)
    registry.register(read_file)
    return registry


def _make_provider(responses):
    """Create a mock provider that yields pre-defined stream responses.

    Parameters
    ----------
    responses : list of list of StreamChunk
        Each inner list is the chunks yielded for one stream_chat() call.
    """
    provider = MagicMock(spec=LLMProvider)
    provider.model = "test-model"
    provider.validate_key.return_value = True

    call_idx = [0]

    def stream_chat_side_effect(messages, tools=None, system=None):
        idx = call_idx[0]
        call_idx[0] += 1
        if idx < len(responses):
            yield from responses[idx]
        else:
            # Default: just return empty text
            yield StreamChunk(type="text_delta", data="Done.")

    provider.stream_chat.side_effect = stream_chat_side_effect
    return provider


@pytest.fixture
def registry():
    return _make_registry()


@pytest.fixture
def context():
    mqtt = MagicMock()
    mqtt.current_state = {
        'algorithm': 'exhsv',
        'detection_enable': True,
        'exg_min': 25,
    }
    return {'mqtt_client': mqtt}


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_defaults(self, registry):
        engine = AgentEngine(tool_registry=registry)
        assert engine.provider is None
        assert engine.connected is False
        assert engine.context == {}

    def test_with_provider(self, registry):
        provider = MagicMock(spec=LLMProvider)
        engine = AgentEngine(tool_registry=registry, provider=provider)
        assert engine.connected is True

    def test_with_context(self, registry, context):
        engine = AgentEngine(tool_registry=registry, context=context)
        assert engine.context is context


# ---------------------------------------------------------------------------
# set_provider
# ---------------------------------------------------------------------------

class TestSetProvider:
    def test_success(self, registry):
        engine = AgentEngine(tool_registry=registry)
        with patch('agent.engine.create_provider') as mock_create:
            mock_provider = MagicMock(spec=LLMProvider)
            mock_provider.validate_key.return_value = True
            mock_provider.model = "test-model"
            mock_create.return_value = mock_provider

            result = engine.set_provider("sk-test", "anthropic")
            assert result is True
            assert engine.connected is True

    def test_invalid_key(self, registry):
        engine = AgentEngine(tool_registry=registry)
        with patch('agent.engine.create_provider') as mock_create:
            mock_provider = MagicMock(spec=LLMProvider)
            mock_provider.validate_key.return_value = False
            mock_create.return_value = mock_provider

            result = engine.set_provider("bad-key", "anthropic")
            assert result is False
            assert engine.connected is False

    def test_creation_error(self, registry):
        engine = AgentEngine(tool_registry=registry)
        with patch('agent.engine.create_provider') as mock_create:
            mock_create.side_effect = ValueError("Unknown provider")
            with pytest.raises(ValueError):
                engine.set_provider("sk-test", "invalid")


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

class TestSessionManagement:
    def test_new_session_created(self, registry):
        engine = AgentEngine(tool_registry=registry)
        session = engine._get_session("s1")
        assert isinstance(session, Session)
        assert session.messages == []
        assert session.input_tokens == 0

    def test_same_session_returned(self, registry):
        engine = AgentEngine(tool_registry=registry)
        s1 = engine._get_session("s1")
        s2 = engine._get_session("s1")
        assert s1 is s2

    def test_different_sessions(self, registry):
        engine = AgentEngine(tool_registry=registry)
        s1 = engine._get_session("s1")
        s2 = engine._get_session("s2")
        assert s1 is not s2

    def test_trim_history(self, registry):
        engine = AgentEngine(tool_registry=registry)
        session = Session()
        session.messages = [{"role": "user", "content": f"msg{i}"} for i in range(60)]
        engine._trim_history(session)
        assert len(session.messages) == MAX_HISTORY

    def test_get_session_info(self, registry):
        engine = AgentEngine(tool_registry=registry)
        session = engine._get_session("s1")
        session.input_tokens = 100
        session.output_tokens = 50
        session.messages = [{"role": "user", "content": "hi"}]
        info = engine.get_session_info("s1")
        assert info == {
            'input_tokens': 100,
            'output_tokens': 50,
            'message_count': 1,
        }


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

class TestSystemPrompt:
    def test_static_content(self, registry):
        engine = AgentEngine(tool_registry=registry)
        prompt = engine._build_system_prompt()
        assert "OWL Assistant" in prompt
        assert "exg:" in prompt

    def test_dynamic_state_included(self, registry, context):
        engine = AgentEngine(tool_registry=registry, context=context)
        prompt = engine._build_system_prompt()
        assert "Current OWL state:" in prompt
        assert "algorithm: exhsv" in prompt

    def test_no_mqtt_client(self, registry):
        engine = AgentEngine(tool_registry=registry, context={})
        prompt = engine._build_system_prompt()
        assert "Current OWL state:" not in prompt


# ---------------------------------------------------------------------------
# Simple text chat (no tool calls)
# ---------------------------------------------------------------------------

class TestSimpleChat:
    def test_text_response(self, registry, context):
        responses = [[
            StreamChunk(type="text_delta", data="Hello"),
            StreamChunk(type="text_delta", data=" farmer!"),
            StreamChunk(type="usage", data={"input_tokens": 50, "output_tokens": 10}),
        ]]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Hi there"))
        text_chunks = [c for c in chunks if c.type == "text_delta"]
        assert len(text_chunks) == 2
        assert text_chunks[0].data == "Hello"
        assert text_chunks[1].data == " farmer!"

    def test_user_message_added_to_history(self, registry, context):
        responses = [[StreamChunk(type="text_delta", data="OK")]]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        list(engine.chat("s1", "Hello"))
        session = engine._get_session("s1")
        assert session.messages[0] == {"role": "user", "content": "Hello"}

    def test_assistant_response_added_to_history(self, registry, context):
        responses = [[StreamChunk(type="text_delta", data="Sure thing!")]]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        list(engine.chat("s1", "Hello"))
        session = engine._get_session("s1")
        assert session.messages[-1] == {"role": "assistant", "content": "Sure thing!"}

    def test_no_provider_yields_error(self, registry):
        engine = AgentEngine(tool_registry=registry)
        chunks = list(engine.chat("s1", "Hi"))
        assert len(chunks) == 1
        assert chunks[0].type == "error"
        assert "No provider" in chunks[0].data

    def test_token_accumulation(self, registry, context):
        responses = [[
            StreamChunk(type="text_delta", data="Hi"),
            StreamChunk(type="usage", data={"input_tokens": 100, "output_tokens": 20}),
        ]]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        list(engine.chat("s1", "Hello"))
        info = engine.get_session_info("s1")
        assert info['input_tokens'] == 100
        assert info['output_tokens'] == 20

        # Second message accumulates
        responses2 = [[
            StreamChunk(type="text_delta", data="Again"),
            StreamChunk(type="usage", data={"input_tokens": 80, "output_tokens": 15}),
        ]]
        engine.provider = _make_provider(responses2)
        list(engine.chat("s1", "More"))
        info = engine.get_session_info("s1")
        assert info['input_tokens'] == 180
        assert info['output_tokens'] == 35


# ---------------------------------------------------------------------------
# Tool calling
# ---------------------------------------------------------------------------

class TestToolCalling:
    def test_single_tool_call(self, registry, context):
        """Model calls a tool, gets result, then responds with text."""
        # Iteration 1: model calls get_system_status
        tool_call = ToolCall(id="tc1", name="get_system_status", arguments={})
        iter1 = [
            StreamChunk(type="text_delta", data="Let me check..."),
            StreamChunk(type="tool_call", data=tool_call),
        ]
        # Iteration 2: model responds with text after seeing result
        iter2 = [
            StreamChunk(type="text_delta", data="Your algorithm is exhsv."),
        ]
        provider = _make_provider([iter1, iter2])
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "What algorithm?"))

        # Should have text deltas + tool result + more text
        text_chunks = [c for c in chunks if c.type == "text_delta"]
        tool_results = [c for c in chunks if c.type == "tool_result"]

        assert len(text_chunks) >= 2
        assert len(tool_results) == 1
        assert tool_results[0].data['tool_name'] == "get_system_status"
        assert 'status' in tool_results[0].data['result']

    def test_multi_step_tool_use(self, registry, context):
        """Model calls tool, sees result, calls another tool, then responds."""
        # Step 1: get config
        tc1 = ToolCall(id="tc1", name="get_system_status", arguments={})
        # Step 2: set config
        tc2 = ToolCall(id="tc2", name="set_config_param",
                       arguments={"section": "GreenOnBrown", "key": "exg_min", "value": "15"})
        # Step 3: final text
        responses = [
            [StreamChunk(type="tool_call", data=tc1)],
            [StreamChunk(type="tool_call", data=tc2)],
            [StreamChunk(type="text_delta", data="Done, exg_min set to 15.")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Set exg_min to 15"))
        tool_results = [c for c in chunks if c.type == "tool_result"]
        assert len(tool_results) == 2

    def test_tool_error_returned_gracefully(self, registry, context):
        """Tool raises error → result contains error message, loop continues."""
        # Call a developer tool (blocked in farmer mode)
        tc = ToolCall(id="tc1", name="read_file", arguments={"path": "/etc/passwd"})
        responses = [
            [StreamChunk(type="tool_call", data=tc)],
            [StreamChunk(type="text_delta", data="Sorry, that tool is restricted.")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Read /etc/passwd"))
        tool_results = [c for c in chunks if c.type == "tool_result"]
        assert len(tool_results) == 1
        assert 'error' in tool_results[0].data['result']

    def test_unknown_tool_handled(self, registry, context):
        tc = ToolCall(id="tc1", name="nonexistent_tool", arguments={})
        responses = [
            [StreamChunk(type="tool_call", data=tc)],
            [StreamChunk(type="text_delta", data="That tool doesn't exist.")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Do something"))
        tool_results = [c for c in chunks if c.type == "tool_result"]
        assert 'error' in tool_results[0].data['result']


# ---------------------------------------------------------------------------
# Iteration limit
# ---------------------------------------------------------------------------

class TestIterationLimit:
    def test_max_iterations(self, registry, context):
        """If model keeps calling tools, engine stops after MAX_ITERATIONS."""
        tc = ToolCall(id="tc1", name="get_system_status", arguments={})
        # Every iteration returns a tool call, never text
        responses = [
            [StreamChunk(type="tool_call", data=tc)]
            for _ in range(MAX_ITERATIONS + 5)
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Loop forever"))
        error_chunks = [c for c in chunks if c.type == "error"]
        assert len(error_chunks) == 1
        assert "maximum tool iterations" in error_chunks[0].data.lower()

        # Verify it stopped at MAX_ITERATIONS
        tool_results = [c for c in chunks if c.type == "tool_result"]
        assert len(tool_results) == MAX_ITERATIONS


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_rate_limit_error(self, registry, context):
        provider = MagicMock(spec=LLMProvider)
        provider.stream_chat.side_effect = RateLimitError("Too fast", retry_after=30)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Hi"))
        assert chunks[-1].type == "error"
        assert "Rate limit" in chunks[-1].data

    def test_auth_error(self, registry, context):
        provider = MagicMock(spec=LLMProvider)
        provider.stream_chat.side_effect = AuthenticationError("Bad key")
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Hi"))
        assert chunks[-1].type == "error"
        assert "Authentication" in chunks[-1].data

    def test_provider_error(self, registry, context):
        provider = MagicMock(spec=LLMProvider)
        provider.stream_chat.side_effect = ProviderError("Server down")
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Hi"))
        assert chunks[-1].type == "error"
        assert "Provider error" in chunks[-1].data

    def test_unexpected_error(self, registry, context):
        provider = MagicMock(spec=LLMProvider)
        provider.stream_chat.side_effect = RuntimeError("Something broke")
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Hi"))
        assert chunks[-1].type == "error"
        assert "Unexpected" in chunks[-1].data


# ---------------------------------------------------------------------------
# History management
# ---------------------------------------------------------------------------

class TestHistoryManagement:
    def test_tool_results_in_history(self, registry, context):
        """Tool call + results should appear in conversation history."""
        tc = ToolCall(id="tc1", name="get_system_status", arguments={})
        responses = [
            [StreamChunk(type="tool_call", data=tc)],
            [StreamChunk(type="text_delta", data="Algorithm is exhsv.")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        list(engine.chat("s1", "What algorithm?"))
        session = engine._get_session("s1")

        # Messages: user, assistant (tool_use), user (tool_result), assistant (text)
        assert len(session.messages) == 4
        assert session.messages[0]['role'] == 'user'
        assert session.messages[1]['role'] == 'assistant'
        assert session.messages[2]['role'] == 'user'
        assert session.messages[3]['role'] == 'assistant'

        # Assistant content should have tool_use block
        assistant_content = session.messages[1]['content']
        assert isinstance(assistant_content, list)
        assert assistant_content[0]['type'] == 'tool_use'

        # User content should have tool_result
        tool_result_content = session.messages[2]['content']
        assert isinstance(tool_result_content, list)
        assert tool_result_content[0]['type'] == 'tool_result'


# ---------------------------------------------------------------------------
# Schema format selection
# ---------------------------------------------------------------------------

class TestSchemaFormat:
    def test_anthropic_provider(self, registry):
        from agent.llm_provider import AnthropicProvider
        provider = MagicMock(spec=AnthropicProvider)
        engine = AgentEngine(tool_registry=registry, provider=provider)
        assert engine._schema_format() == "anthropic"

    def test_openai_provider(self, registry):
        from agent.llm_provider import OpenAIProvider
        provider = MagicMock(spec=OpenAIProvider)
        engine = AgentEngine(tool_registry=registry, provider=provider)
        assert engine._schema_format() == "openai"

    def test_no_provider(self, registry):
        engine = AgentEngine(tool_registry=registry)
        assert engine._schema_format() == "anthropic"


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

class TestGetStatus:
    def test_not_connected(self, registry):
        engine = AgentEngine(tool_registry=registry)
        status = engine.get_status()
        assert status['connected'] is False
        assert status['provider'] is None
        assert status['model'] is None

    def test_anthropic_connected(self, registry):
        from agent.llm_provider import AnthropicProvider
        provider = MagicMock(spec=AnthropicProvider)
        provider.model = "claude-sonnet-4-20250514"
        engine = AgentEngine(tool_registry=registry, provider=provider)
        status = engine.get_status()
        assert status['connected'] is True
        assert status['provider'] == "anthropic"
        assert status['model'] == "claude-sonnet-4-20250514"

    def test_openai_connected(self, registry):
        from agent.llm_provider import OpenAIProvider
        provider = MagicMock(spec=OpenAIProvider)
        provider.model = "gpt-4o-mini"
        engine = AgentEngine(tool_registry=registry, provider=provider)
        status = engine.get_status()
        assert status['connected'] is True
        assert status['provider'] == "openai"
        assert status['model'] == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# _execute_tool
# ---------------------------------------------------------------------------

class TestExecuteTool:
    def test_success(self, registry, context):
        engine = AgentEngine(tool_registry=registry, context=context)
        tc = ToolCall(id="tc1", name="get_system_status", arguments={})
        result = engine._execute_tool(tc)
        assert 'status' in result

    def test_permission_error(self, registry, context):
        engine = AgentEngine(tool_registry=registry, context=context)
        tc = ToolCall(id="tc1", name="read_file", arguments={"path": "/etc/passwd"})
        result = engine._execute_tool(tc)
        assert 'error' in result
        assert 'developer mode' in result['error']

    def test_unknown_tool(self, registry, context):
        engine = AgentEngine(tool_registry=registry, context=context)
        tc = ToolCall(id="tc1", name="nonexistent", arguments={})
        result = engine._execute_tool(tc)
        assert 'error' in result

    def test_missing_required_param(self, registry, context):
        engine = AgentEngine(tool_registry=registry, context=context)
        tc = ToolCall(id="tc1", name="set_config_param", arguments={"section": "GreenOnBrown"})
        result = engine._execute_tool(tc)
        assert 'error' in result


# ---------------------------------------------------------------------------
# _append_assistant_tool_use / _append_tool_result (format-aware)
# ---------------------------------------------------------------------------

class TestAppendAssistantToolUse:
    def test_anthropic_format(self, registry):
        engine = AgentEngine(tool_registry=registry)
        session = Session()
        tc = ToolCall(id="tc1", name="get_system_status", arguments={"section": "cpu"})
        engine._append_assistant_tool_use(session, ["Checking..."], [tc], "anthropic")
        msg = session.messages[-1]
        assert msg['role'] == 'assistant'
        assert isinstance(msg['content'], list)
        assert msg['content'][0] == {'type': 'text', 'text': 'Checking...'}
        assert msg['content'][1]['type'] == 'tool_use'
        assert msg['content'][1]['id'] == 'tc1'
        assert msg['content'][1]['input'] == {"section": "cpu"}

    def test_openai_format(self, registry):
        engine = AgentEngine(tool_registry=registry)
        session = Session()
        tc = ToolCall(id="tc1", name="get_system_status", arguments={"section": "cpu"})
        engine._append_assistant_tool_use(session, ["Checking..."], [tc], "openai")
        msg = session.messages[-1]
        assert msg['role'] == 'assistant'
        assert msg['content'] == 'Checking...'
        assert len(msg['tool_calls']) == 1
        assert msg['tool_calls'][0]['id'] == 'tc1'
        assert msg['tool_calls'][0]['type'] == 'function'
        assert msg['tool_calls'][0]['function']['name'] == 'get_system_status'

    def test_openai_no_text(self, registry):
        engine = AgentEngine(tool_registry=registry)
        session = Session()
        tc = ToolCall(id="tc1", name="get_system_status", arguments={})
        engine._append_assistant_tool_use(session, [], [tc], "openai")
        msg = session.messages[-1]
        assert 'content' not in msg  # no text means no content key


class TestAppendToolResult:
    def test_anthropic_format(self, registry):
        engine = AgentEngine(tool_registry=registry)
        session = Session()
        tc = ToolCall(id="tc1", name="get_system_status", arguments={})
        result = {"status": {"cpu": 50}}
        engine._append_tool_result(session, tc, result, "anthropic")
        msg = session.messages[-1]
        assert msg['role'] == 'user'
        assert msg['content'][0]['type'] == 'tool_result'
        assert msg['content'][0]['tool_use_id'] == 'tc1'

    def test_openai_format(self, registry):
        engine = AgentEngine(tool_registry=registry)
        session = Session()
        tc = ToolCall(id="tc1", name="get_system_status", arguments={})
        result = {"status": {"cpu": 50}}
        engine._append_tool_result(session, tc, result, "openai")
        msg = session.messages[-1]
        assert msg['role'] == 'tool'
        assert msg['tool_call_id'] == 'tc1'
