"""Integration tests for OWL Agent Runtime (Phase 6).

Verifies end-to-end flows across tool registry, LLM provider, agent engine,
and widget manager working together.
"""

import json
import os
import configparser
import pytest
from unittest.mock import MagicMock, patch

from agent.tool_registry import ToolRegistry
from agent.llm_provider import (
    LLMProvider,
    StreamChunk,
    ToolCall,
    RateLimitError,
)
from agent.engine import AgentEngine
from agent.widget_manager import WidgetManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config():
    """Create a minimal config for testing."""
    cfg = configparser.ConfigParser()
    cfg.add_section('GreenOnBrown')
    cfg.set('GreenOnBrown', 'exg_min', '25')
    cfg.set('GreenOnBrown', 'exg_max', '200')
    cfg.set('GreenOnBrown', 'hue_min', '39')
    cfg.set('GreenOnBrown', 'hue_max', '83')
    cfg.set('GreenOnBrown', 'saturation_min', '50')
    cfg.set('GreenOnBrown', 'saturation_max', '220')
    cfg.set('GreenOnBrown', 'brightness_min', '60')
    cfg.set('GreenOnBrown', 'brightness_max', '190')
    cfg.set('GreenOnBrown', 'min_detection_area', '10')
    cfg.add_section('System')
    cfg.set('System', 'algorithm', 'exhsv')
    return cfg


@pytest.fixture
def mqtt_client():
    """Mock MQTT client with current_state."""
    client = MagicMock()
    client.current_state = {
        'detection_enable': True,
        'algorithm': 'exhsv',
        'detection_mode': 0,
        'sensitivity_level': 'medium',
        'exg_min': 25,
        'exg_max': 200,
        'hue_min': 39,
        'hue_max': 83,
        'saturation_min': 50,
        'saturation_max': 220,
        'brightness_min': 60,
        'brightness_max': 190,
        'min_detection_area': 10,
        'cpu_percent': 45.2,
        'memory_percent': 62.1,
        'cpu_temp': 55.0,
        'tracking_enabled': False,
    }
    client.set_detection_enable.return_value = {'success': True, 'message': 'OK'}
    client.set_sensitivity_level.return_value = {'success': True, 'message': 'OK'}
    client._send_command.return_value = {'success': True, 'message': 'OK'}
    return client


@pytest.fixture
def widget_manager(tmp_path):
    """Real widget manager with temp directory."""
    return WidgetManager(str(tmp_path / 'widgets'))


@pytest.fixture
def registry():
    """Full registry with all discovered tools."""
    reg = ToolRegistry(developer_mode=False)
    reg.discover()
    return reg


@pytest.fixture
def context(mqtt_client, config, widget_manager):
    return {
        'mqtt_client': mqtt_client,
        'config': config,
        'widget_manager': widget_manager,
    }


def _make_provider(responses):
    """Create mock provider from list of response lists."""
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
            yield StreamChunk(type="text_delta", data="Done.")

    provider.stream_chat.side_effect = stream_chat_side_effect
    return provider


# ---------------------------------------------------------------------------
# Demo scenario: "What's my current algorithm?"
# ---------------------------------------------------------------------------

class TestQuerySystemStatus:
    def test_reads_and_reports_algorithm(self, registry, context):
        """Agent calls get_system_status, reports algorithm."""
        tc = ToolCall(id="tc1", name="get_system_status", arguments={})
        responses = [
            [StreamChunk(type="tool_call", data=tc)],
            [StreamChunk(type="text_delta", data="Your current algorithm is exhsv.")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "What's my current algorithm?"))
        tool_results = [c for c in chunks if c.type == "tool_result"]
        text_chunks = [c for c in chunks if c.type == "text_delta"]

        assert len(tool_results) == 1
        result = tool_results[0].data['result']
        assert 'status' in result
        assert result['status']['algorithm'] == 'exhsv'
        assert any('exhsv' in c.data for c in text_chunks)


# ---------------------------------------------------------------------------
# Demo scenario: "Lower exg_min to 15"
# ---------------------------------------------------------------------------

class TestSetConfigParam:
    def test_reads_then_sets_param(self, registry, context, mqtt_client):
        """Agent reads config, then sets exg_min."""
        tc_get = ToolCall(id="tc1", name="get_config",
                          arguments={"section": "GreenOnBrown", "key": "exg_min"})
        tc_set = ToolCall(id="tc2", name="set_config_param",
                          arguments={"section": "GreenOnBrown", "key": "exg_min", "value": "15"})
        responses = [
            [StreamChunk(type="tool_call", data=tc_get)],
            [StreamChunk(type="tool_call", data=tc_set)],
            [StreamChunk(type="text_delta", data="Done, exg_min changed from 25 to 15.")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Lower exg_min to 15"))
        tool_results = [c for c in chunks if c.type == "tool_result"]

        # First tool: get_config returns current value
        assert tool_results[0].data['result']['value'] == '25'

        # Second tool: set_config_param calls MQTT
        mqtt_client._send_command.assert_called_once_with(
            'set_config', section='GreenOnBrown', key='exg_min', value='15'
        )


# ---------------------------------------------------------------------------
# Demo scenario: "Save as Rainy Day preset"
# ---------------------------------------------------------------------------

class TestCreatePreset:
    def test_creates_preset_via_mqtt(self, registry, context):
        """Agent calls create_preset tool which sends MQTT command."""
        tc = ToolCall(id="tc1", name="create_preset", arguments={"name": "rainy day"})
        responses = [
            [StreamChunk(type="tool_call", data=tc)],
            [StreamChunk(type="text_delta", data="Saved preset 'rainy day'.")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Save as Rainy Day"))
        tool_results = [c for c in chunks if c.type == "tool_result"]

        assert tool_results[0].data['result']['success'] is True
        # Verify MQTT command was sent
        mqtt = context['mqtt_client']
        mqtt._send_command.assert_called_with('save_sensitivity_preset', name='rainy day')


# ---------------------------------------------------------------------------
# Demo scenario: "Add a slider for min detection area on config tab"
# ---------------------------------------------------------------------------

class TestCreateWidget:
    def test_creates_slider_widget(self, registry, context, widget_manager):
        """Agent calls create_widget with slider spec."""
        spec = {
            "id": "min-area-slider",
            "name": "Min Area Dial",
            "type": "range_slider",
            "slot": "config_before_advanced",
            "builtin_config": {
                "param": "min_detection_area",
                "section": "GreenOnBrown",
                "min": 0,
                "max": 500,
                "step": 5,
                "label": "Min Detection Area",
                "unit": "px",
            },
        }
        tc = ToolCall(id="tc1", name="create_widget", arguments={"spec": spec})
        responses = [
            [StreamChunk(type="tool_call", data=tc)],
            [StreamChunk(type="text_delta", data="Created slider widget.")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Add a slider for min detection area"))
        tool_results = [c for c in chunks if c.type == "tool_result"]

        assert tool_results[0].data['result']['success'] is True

        # Verify widget was installed
        widgets = widget_manager.scan()
        assert len(widgets) == 1
        assert widgets[0]['id'] == 'min-area-slider'

        # Verify rendering works
        html = widget_manager.render('min-area-slider')
        assert html is not None
        assert 'min_detection_area' in html
        assert 'range' in html


# ---------------------------------------------------------------------------
# Token accumulation across messages
# ---------------------------------------------------------------------------

class TestTokenTracking:
    def test_tokens_accumulate_across_turns(self, registry, context):
        """Tokens from multiple messages accumulate in session."""
        resp1 = [[
            StreamChunk(type="text_delta", data="Hi"),
            StreamChunk(type="usage", data={"input_tokens": 100, "output_tokens": 20}),
        ]]
        resp2 = [[
            StreamChunk(type="text_delta", data="Sure"),
            StreamChunk(type="usage", data={"input_tokens": 150, "output_tokens": 30}),
        ]]

        provider1 = _make_provider(resp1)
        engine = AgentEngine(tool_registry=registry, provider=provider1, context=context)
        list(engine.chat("s1", "Hello"))

        provider2 = _make_provider(resp2)
        engine.provider = provider2
        list(engine.chat("s1", "Help me"))

        info = engine.get_session_info("s1")
        assert info['input_tokens'] == 250
        assert info['output_tokens'] == 50


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    def test_rate_limit_shows_clear_message(self, registry, context):
        """Rate limit produces user-friendly error."""
        provider = MagicMock(spec=LLMProvider)
        provider.stream_chat.side_effect = RateLimitError("Limit hit", retry_after=42)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Hello"))
        errors = [c for c in chunks if c.type == "error"]
        assert len(errors) == 1
        assert "42" in errors[0].data


# ---------------------------------------------------------------------------
# Widget error isolation
# ---------------------------------------------------------------------------

class TestWidgetErrorIsolation:
    def test_corrupted_widget_doesnt_crash_scan(self, widget_manager, tmp_path):
        """A malformed widget.json doesn't break scanning."""
        # Install a good widget
        good_spec = {
            "id": "good-widget",
            "name": "Good Widget",
            "type": "toggle",
            "slot": "dashboard_controls",
            "builtin_config": {
                "param": "detection_enable",
                "section": "System",
                "on_label": "ON",
                "off_label": "OFF",
            },
        }
        widget_manager.install("good-widget", good_spec)

        # Create a corrupted widget manually
        bad_dir = widget_manager.widgets_dir / "bad-widget"
        bad_dir.mkdir()
        (bad_dir / "widget.json").write_text("{invalid json")

        widgets = widget_manager.scan()
        # Good widget should still appear
        assert any(w['id'] == 'good-widget' for w in widgets)
        # Bad widget should be skipped (not crash)
        assert not any(w.get('id') == 'bad-widget' for w in widgets)


# ---------------------------------------------------------------------------
# Protected config keys enforcement
# ---------------------------------------------------------------------------

class TestProtectedKeys:
    def test_relay_section_blocked(self, registry, context):
        """Attempting to modify Relays section through agent fails."""
        tc = ToolCall(id="tc1", name="set_config_param",
                      arguments={"section": "Relays", "key": "0", "value": "99"})
        responses = [
            [StreamChunk(type="tool_call", data=tc)],
            [StreamChunk(type="text_delta", data="Cannot modify relay pins.")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Change relay pin"))
        tool_results = [c for c in chunks if c.type == "tool_result"]

        assert 'error' in tool_results[0].data['result']
        assert 'protected' in tool_results[0].data['result']['error'].lower()

    def test_mqtt_section_blocked(self, registry, context):
        tc = ToolCall(id="tc1", name="set_config_param",
                      arguments={"section": "MQTT", "key": "broker_host", "value": "evil"})
        responses = [
            [StreamChunk(type="tool_call", data=tc)],
            [StreamChunk(type="text_delta", data="Cannot modify MQTT settings.")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Change MQTT"))
        tool_results = [c for c in chunks if c.type == "tool_result"]
        assert 'error' in tool_results[0].data['result']


# ---------------------------------------------------------------------------
# Schema format matches provider
# ---------------------------------------------------------------------------

class TestSchemaProviderMatch:
    def test_anthropic_gets_anthropic_schemas(self, registry, context):
        """When using Anthropic provider, tools use input_schema format."""
        from agent.llm_provider import AnthropicProvider
        provider = MagicMock(spec=AnthropicProvider)
        provider.model = "claude-sonnet-4-20250514"
        provider.stream_chat.return_value = iter([
            StreamChunk(type="text_delta", data="Hello"),
        ])
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)
        list(engine.chat("s1", "Hi"))

        # Check that stream_chat was called with Anthropic-format tools
        call_args = provider.stream_chat.call_args
        tools = call_args.kwargs.get('tools') or call_args[1].get('tools', [])
        if tools:
            assert 'input_schema' in tools[0]

    def test_openai_gets_openai_schemas(self, registry, context):
        """When using OpenAI provider, tools use function.parameters format."""
        from agent.llm_provider import OpenAIProvider
        provider = MagicMock(spec=OpenAIProvider)
        provider.model = "gpt-4o-mini"
        provider.stream_chat.return_value = iter([
            StreamChunk(type="text_delta", data="Hello"),
        ])
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)
        list(engine.chat("s1", "Hi"))

        call_args = provider.stream_chat.call_args
        tools = call_args.kwargs.get('tools') or call_args[1].get('tools', [])
        if tools:
            assert tools[0].get('type') == 'function'
            assert 'function' in tools[0]


# ---------------------------------------------------------------------------
# Multi-step tool loop terminates
# ---------------------------------------------------------------------------

class TestMultiStepLoop:
    def test_tool_loop_terminates_with_text(self, registry, context):
        """Model uses 3 tools, then gives text — all results visible."""
        tc1 = ToolCall(id="tc1", name="get_system_status", arguments={})
        tc2 = ToolCall(id="tc2", name="get_config",
                       arguments={"section": "GreenOnBrown"})
        tc3 = ToolCall(id="tc3", name="list_presets", arguments={})

        responses = [
            [StreamChunk(type="tool_call", data=tc1)],
            [StreamChunk(type="tool_call", data=tc2)],
            [StreamChunk(type="tool_call", data=tc3)],
            [StreamChunk(type="text_delta", data="Here's a summary...")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Give me a full system overview"))
        tool_results = [c for c in chunks if c.type == "tool_result"]
        text_chunks = [c for c in chunks if c.type == "text_delta"]

        assert len(tool_results) == 3
        assert len(text_chunks) == 1

        # All tool names present
        names = {r.data['tool_name'] for r in tool_results}
        assert names == {'get_system_status', 'get_config', 'list_presets'}


# ---------------------------------------------------------------------------
# Algorithm validation
# ---------------------------------------------------------------------------

class TestAlgorithmValidation:
    def test_valid_algorithm_accepted(self, registry, context):
        tc = ToolCall(id="tc1", name="set_algorithm", arguments={"algorithm": "exg"})
        responses = [
            [StreamChunk(type="tool_call", data=tc)],
            [StreamChunk(type="text_delta", data="Algorithm set to exg.")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Switch to exg"))
        tool_results = [c for c in chunks if c.type == "tool_result"]
        assert tool_results[0].data['result'].get('success', True)

    def test_invalid_algorithm_rejected(self, registry, context):
        tc = ToolCall(id="tc1", name="set_algorithm", arguments={"algorithm": "magic"})
        responses = [
            [StreamChunk(type="tool_call", data=tc)],
            [StreamChunk(type="text_delta", data="Invalid algorithm.")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        chunks = list(engine.chat("s1", "Use magic algorithm"))
        tool_results = [c for c in chunks if c.type == "tool_result"]
        assert 'error' in tool_results[0].data['result']


# ---------------------------------------------------------------------------
# Widget remove
# ---------------------------------------------------------------------------

class TestWidgetRemove:
    def test_remove_installed_widget(self, registry, context, widget_manager):
        """Agent can remove a widget that was previously installed."""
        # Install first
        spec = {
            "id": "test-toggle",
            "name": "Test Toggle",
            "type": "toggle",
            "slot": "dashboard_controls",
            "builtin_config": {
                "param": "detection_enable",
                "section": "System",
                "on_label": "ON",
                "off_label": "OFF",
            },
        }
        widget_manager.install("test-toggle", spec)
        assert len(widget_manager.scan()) == 1

        # Remove via agent
        tc = ToolCall(id="tc1", name="remove_widget", arguments={"widget_id": "test-toggle"})
        responses = [
            [StreamChunk(type="tool_call", data=tc)],
            [StreamChunk(type="text_delta", data="Widget removed.")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        list(engine.chat("s1", "Remove the toggle"))
        assert len(widget_manager.scan()) == 0


# ---------------------------------------------------------------------------
# Detection mode control
# ---------------------------------------------------------------------------

class TestDetectionControl:
    def test_enable_detection(self, registry, context, mqtt_client):
        tc = ToolCall(id="tc1", name="set_detection", arguments={"enabled": True})
        responses = [
            [StreamChunk(type="tool_call", data=tc)],
            [StreamChunk(type="text_delta", data="Detection enabled.")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        list(engine.chat("s1", "Enable detection"))
        mqtt_client.set_detection_enable.assert_called_once_with(True)

    def test_blanket_mode(self, registry, context, mqtt_client):
        tc = ToolCall(id="tc1", name="set_detection", arguments={"enabled": True, "mode": 2})
        responses = [
            [StreamChunk(type="tool_call", data=tc)],
            [StreamChunk(type="text_delta", data="All nozzles on.")],
        ]
        provider = _make_provider(responses)
        engine = AgentEngine(tool_registry=registry, provider=provider, context=context)

        list(engine.chat("s1", "Turn all nozzles on"))
        mqtt_client._send_command.assert_called_with('set_detection_mode', value=2)


# ---------------------------------------------------------------------------
# Existing tests still pass (regression guard)
# ---------------------------------------------------------------------------

class TestNoRegressions:
    def test_tool_registry_imports(self):
        """Tool registry is importable and has all expected tools."""
        from agent.tool_registry import ToolRegistry
        reg = ToolRegistry()
        count = reg.discover()
        assert count >= 17  # 4 observe + 8 apply (developer stubs removed)

    def test_llm_provider_imports(self):
        """LLM provider classes are importable."""
        from agent.llm_provider import (
            AnthropicProvider, OpenAIProvider, create_provider,
            ModelResponse, StreamChunk, ToolCall,
        )
        assert AnthropicProvider.DEFAULT_MODEL == "claude-sonnet-4-6"
        assert OpenAIProvider.DEFAULT_MODEL == "gpt-4o-mini"

    def test_agent_engine_imports(self):
        """Agent engine is importable."""
        from agent.engine import AgentEngine, Session, MAX_ITERATIONS
        assert MAX_ITERATIONS == 10

    def test_widget_manager_imports(self):
        """Widget manager is importable."""
        from agent.widget_manager import WidgetManager
        assert 'range_slider' in WidgetManager.VALID_TYPES
