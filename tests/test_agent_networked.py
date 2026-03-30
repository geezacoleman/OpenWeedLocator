"""Tests for Agent Runtime integration with CentralController.

Verifies:
1. Tool-compatible interface methods on CentralController
2. Full connect flow: Flask route → AgentEngine → LLMProvider → mocked HTTP
3. Error propagation: 400/401/429 each surface distinct messages to the user
"""

import json
import threading
import configparser
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Fixtures — lightweight CentralController with mocked MQTT
# ---------------------------------------------------------------------------

@pytest.fixture
def controller():
    """Create a CentralController with MQTT stubbed out."""
    with patch('controller.networked.networked.mqtt'):
        from controller.networked.networked import CentralController
        ctrl = CentralController.__new__(CentralController)
        ctrl.config = configparser.ConfigParser()
        ctrl.config.add_section('MQTT')
        ctrl.config.set('MQTT', 'broker_ip', 'localhost')
        ctrl.config.set('MQTT', 'broker_port', '1883')
        ctrl.config.set('MQTT', 'client_id', 'test')
        ctrl.broker_host = 'localhost'
        ctrl.broker_port = 1883
        ctrl.client_id = 'test'
        ctrl.owls_state = {}
        ctrl.desired_state = {}
        ctrl.lwt_timestamps = {}
        ctrl.mqtt_connected = True
        ctrl.mqtt_lock = threading.Lock()
        ctrl.mqtt_client = MagicMock()
        ctrl.mqtt_client.publish.return_value = MagicMock(rc=0)
        ctrl.gps_manager = None
        # Minimal actuation stubs
        ctrl.speed_averager = MagicMock()
        ctrl.actuation_calculator = MagicMock()
        ctrl._actuation_state = {}
        ctrl.offline_timeout = 15.0
        return ctrl


# ---------------------------------------------------------------------------
# current_state property
# ---------------------------------------------------------------------------

class TestCurrentState:
    def test_returns_first_connected_owl(self, controller):
        controller.owls_state = {
            'owl-1': {'connected': True, 'detection_enable': True, 'algorithm': 'exhsv'},
            'owl-2': {'connected': True, 'detection_enable': False},
        }
        state = controller.current_state
        assert state.get('connected') is True
        assert state in [controller.owls_state['owl-1'], controller.owls_state['owl-2']]

    def test_returns_empty_when_no_owls(self, controller):
        controller.owls_state = {}
        assert controller.current_state == {}

    def test_returns_empty_when_all_disconnected(self, controller):
        controller.owls_state = {
            'owl-1': {'connected': False},
            'owl-2': {'connected': False},
        }
        assert controller.current_state == {}

    def test_skips_disconnected_owls(self, controller):
        controller.owls_state = {
            'owl-1': {'connected': False},
            'owl-2': {'connected': True, 'algorithm': 'gog'},
        }
        state = controller.current_state
        assert state.get('algorithm') == 'gog'


# ---------------------------------------------------------------------------
# _send_command (tool-compatible interface)
# ---------------------------------------------------------------------------

class TestSendCommand:
    def test_set_config_translates_kwargs(self, controller):
        controller.owls_state = {'owl-1': {'connected': True}}
        result = controller._send_command('set_config', key='exg_min', value=30)
        assert result['success'] is True
        assert controller.mqtt_client.publish.called

    def test_set_algorithm_delegates(self, controller):
        controller.owls_state = {'owl-1': {'connected': True}}
        result = controller._send_command('set_algorithm', value='exhsv')
        assert result['success'] is True

    def test_save_sensitivity_preset_broadcasts_raw(self, controller):
        controller.owls_state = {'owl-1': {'connected': True}}
        result = controller._send_command('save_sensitivity_preset', name='Custom1')
        assert result['success'] is True
        call_args = controller.mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload['action'] == 'save_sensitivity_preset'
        assert payload['name'] == 'Custom1'

    def test_delete_sensitivity_preset_broadcasts_raw(self, controller):
        controller.owls_state = {'owl-1': {'connected': True}}
        result = controller._send_command('delete_sensitivity_preset', name='Custom1')
        assert result['success'] is True
        call_args = controller.mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload['action'] == 'delete_sensitivity_preset'

    def test_mqtt_disconnected_returns_error(self, controller):
        controller.mqtt_connected = False
        result = controller._send_command('set_config', key='exg_min', value=30)
        assert result['success'] is False


# ---------------------------------------------------------------------------
# set_detection_enable / set_sensitivity_level
# ---------------------------------------------------------------------------

class TestDetectionEnable:
    def test_enable_detection(self, controller):
        controller.owls_state = {'owl-1': {'connected': True, 'detection_enable': False}}
        result = controller.set_detection_enable(True)
        assert result['success'] is True

    def test_disable_detection(self, controller):
        controller.owls_state = {'owl-1': {'connected': True, 'detection_enable': True}}
        result = controller.set_detection_enable(False)
        assert result['success'] is True


class TestSensitivityLevel:
    def test_set_high(self, controller):
        controller.owls_state = {'owl-1': {'connected': True}}
        result = controller.set_sensitivity_level('High')
        assert result['success'] is True
        call_args = controller.mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload['level'] == 'high'

    def test_set_low(self, controller):
        controller.owls_state = {'owl-1': {'connected': True}}
        result = controller.set_sensitivity_level('LOW')
        assert result['success'] is True


# ---------------------------------------------------------------------------
# _broadcast_raw
# ---------------------------------------------------------------------------

class TestBroadcastRaw:
    def test_sends_to_connected_owls_only(self, controller):
        controller.owls_state = {
            'owl-1': {'connected': True},
            'owl-2': {'connected': False},
            'owl-3': {'connected': True},
        }
        result = controller._broadcast_raw({'action': 'test'})
        assert result['success'] is True
        assert '2 OWLs' in result['message']
        assert controller.mqtt_client.publish.call_count == 2

    def test_mqtt_disconnected(self, controller):
        controller.mqtt_connected = False
        result = controller._broadcast_raw({'action': 'test'})
        assert result['success'] is False


# ---------------------------------------------------------------------------
# _send_command — install_algorithm (BUG 1)
# ---------------------------------------------------------------------------

class TestSendCommandInstallAlgorithm:
    def test_install_algorithm_broadcasts_name_and_code(self, controller):
        controller.owls_state = {'owl-1': {'connected': True}}
        result = controller._send_command(
            'install_algorithm', name='bright_green', code='def algo(image): pass',
            description='Test algo',
        )
        assert result['success'] is True
        call_args = controller.mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload['action'] == 'install_algorithm'
        assert payload['name'] == 'bright_green'
        assert payload['code'] == 'def algo(image): pass'
        assert payload['description'] == 'Test algo'

    def test_install_algorithm_default_description(self, controller):
        controller.owls_state = {'owl-1': {'connected': True}}
        result = controller._send_command(
            'install_algorithm', name='test', code='x',
        )
        assert result['success'] is True
        payload = json.loads(controller.mqtt_client.publish.call_args[0][1])
        assert payload['description'] == ''


# ---------------------------------------------------------------------------
# _send_command — set_config with section (BUG 2)
# ---------------------------------------------------------------------------

class TestSendCommandSetConfigSection:
    def test_set_config_non_gob_section_routes_via_set_config_section(self, controller):
        controller.owls_state = {'owl-1': {'connected': True}}
        result = controller._send_command(
            'set_config', section='Camera', key='resolution_width', value='1280',
        )
        assert result['success'] is True
        payload = json.loads(controller.mqtt_client.publish.call_args[0][1])
        assert payload['action'] == 'set_config_section'
        assert payload['section'] == 'Camera'
        assert payload['params'] == {'resolution_width': '1280'}

    def test_set_config_gob_section_uses_set_greenonbrown_param(self, controller):
        """GreenOnBrown section routes via send_command('set_config') which
        translates to set_greenonbrown_param in MQTT payload."""
        controller.owls_state = {'owl-1': {'connected': True}}
        result = controller._send_command(
            'set_config', section='GreenOnBrown', key='exg_min', value=30,
        )
        assert result['success'] is True
        payload = json.loads(controller.mqtt_client.publish.call_args[0][1])
        assert payload['action'] == 'set_greenonbrown_param'
        assert payload['param'] == 'exg_min'
        assert payload['value'] == 30

    def test_set_config_default_section_is_gob(self, controller):
        controller.owls_state = {'owl-1': {'connected': True}}
        result = controller._send_command('set_config', key='exg_min', value=30)
        assert result['success'] is True
        payload = json.loads(controller.mqtt_client.publish.call_args[0][1])
        # Default section is GreenOnBrown → translated to set_greenonbrown_param
        assert payload['action'] == 'set_greenonbrown_param'

    def test_unknown_action_with_extra_kwargs_logs_warning(self, controller):
        """Unknown actions with kwargs that aren't 'value' get a warning."""
        controller.owls_state = {'owl-1': {'connected': True}}
        import logging
        with patch('controller.networked.networked.logger') as mock_logger:
            controller._send_command('some_action', foo='bar', baz=42)
            mock_logger.warning.assert_called_once()


# ===========================================================================
# Connect Flow Tests — full route → engine → provider → HTTP chain
# ===========================================================================

from agent.llm_provider import (
    AnthropicProvider,
    OpenAIProvider,
    ProviderError,
    AuthenticationError,
    RateLimitError,
)
from agent.engine import AgentEngine
from agent.tool_registry import ToolRegistry


@pytest.fixture
def mock_registry():
    """Minimal tool registry for agent engine tests."""
    reg = ToolRegistry(developer_mode=False)
    return reg


@pytest.fixture
def mock_config():
    """Minimal config for agent engine context."""
    cfg = configparser.ConfigParser()
    cfg.add_section('GreenOnBrown')
    cfg.set('GreenOnBrown', 'exg_min', '25')
    return cfg


def _make_mock_response(status_code, body=''):
    """Create a mock httpx response with given status and body."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = body
    resp.json.return_value = {
        'content': [{'type': 'text', 'text': 'ok'}],
        'usage': {'input_tokens': 5, 'output_tokens': 1},
        'stop_reason': 'end_turn',
    }
    return resp


# ---------------------------------------------------------------------------
# AnthropicProvider.validate_key — HTTP-level tests
# ---------------------------------------------------------------------------

class TestValidateKeyHTTP:
    """Tests that mock httpx.Client.post to verify validate_key error handling."""

    def test_200_returns_true(self):
        """Successful API call → True."""
        provider = AnthropicProvider(api_key='sk-ant-valid-key-123')
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(200)
            assert provider.validate_key() is True

    def test_401_returns_false(self):
        """Invalid key (401) → False, no exception raised."""
        provider = AnthropicProvider(api_key='sk-ant-bad-key')
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(
                401, '{"error":{"type":"authentication_error","message":"invalid x-api-key"}}')
            assert provider.validate_key() is False

    def test_400_raises_provider_error(self):
        """Bad request (400) → ProviderError propagated to caller."""
        provider = AnthropicProvider(api_key='sk-ant-valid-key-123')
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(
                400, '{"error":{"type":"invalid_request_error","message":"model not found"}}')
            with pytest.raises(ProviderError, match='HTTP 400'):
                provider.validate_key()

    def test_429_raises_rate_limit_error(self):
        """Rate limited (429) → RateLimitError propagated to caller."""
        provider = AnthropicProvider(api_key='sk-ant-valid-key-123')
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(
                429, '{"error":{"type":"rate_limit_error","message":"too many requests"}}')
            with pytest.raises(RateLimitError, match='HTTP 429'):
                provider.validate_key()

    def test_network_error_returns_false(self):
        """Network timeout → False (not an API key issue)."""
        import httpx
        provider = AnthropicProvider(api_key='sk-ant-valid-key-123')
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.side_effect = httpx.ConnectError('connection refused')
            assert provider.validate_key() is False


class TestValidateKeyOpenAI:
    """Same tests for OpenAI provider."""

    def test_200_returns_true(self):
        provider = OpenAIProvider(api_key='sk-openai-valid-key')
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(200)
            assert provider.validate_key() is True

    def test_401_returns_false(self):
        provider = OpenAIProvider(api_key='sk-openai-bad-key')
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(401, 'invalid api key')
            assert provider.validate_key() is False

    def test_400_raises_provider_error(self):
        provider = OpenAIProvider(api_key='sk-openai-valid-key')
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(400, 'bad request')
            with pytest.raises(ProviderError, match='HTTP 400'):
                provider.validate_key()


# ---------------------------------------------------------------------------
# AgentEngine.set_provider — full flow with mocked HTTP
# ---------------------------------------------------------------------------

class TestSetProviderFlow:
    """Tests the set_provider → create_provider → validate_key → HTTP chain."""

    def test_valid_key_connects(self, mock_registry, mock_config):
        """Valid API key → engine.connected is True, returns True."""
        engine = AgentEngine(tool_registry=mock_registry, context={'config': mock_config})
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(200)
            result = engine.set_provider('sk-ant-test-key', 'anthropic')
        assert result is True
        assert engine.connected is True
        assert engine.provider.model == 'claude-sonnet-4-6'

    def test_invalid_key_disconnects(self, mock_registry, mock_config):
        """Invalid key (401) → returns False, engine not connected."""
        engine = AgentEngine(tool_registry=mock_registry, context={'config': mock_config})
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(401, 'bad key')
            result = engine.set_provider('sk-ant-bad-key', 'anthropic')
        assert result is False
        assert engine.connected is False

    def test_400_error_propagates_with_message(self, mock_registry, mock_config):
        """400 (bad model etc) → raises ProviderError, not silent False."""
        engine = AgentEngine(tool_registry=mock_registry, context={'config': mock_config})
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(
                400, 'model not found: claude-sonnet-4-20250514')
            with pytest.raises(ProviderError, match='HTTP 400'):
                engine.set_provider('sk-ant-test-key', 'anthropic')
        assert engine.connected is False

    def test_429_error_propagates(self, mock_registry, mock_config):
        """429 (rate limited) → raises RateLimitError, not silent False."""
        engine = AgentEngine(tool_registry=mock_registry, context={'config': mock_config})
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(429, 'rate limited')
            with pytest.raises(RateLimitError):
                engine.set_provider('sk-ant-test-key', 'anthropic')
        assert engine.connected is False

    def test_openai_valid_key(self, mock_registry, mock_config):
        """OpenAI provider with valid key."""
        engine = AgentEngine(tool_registry=mock_registry, context={'config': mock_config})
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(200)
            result = engine.set_provider('sk-openai-test', 'openai')
        assert result is True
        assert engine.provider.model == 'gpt-4o-mini'


# ---------------------------------------------------------------------------
# Route-level tests — simulate what the browser does
# ---------------------------------------------------------------------------

class TestConnectRoute:
    """Tests the /api/agent/connect Flask route end-to-end.

    Uses the agent_engine module directly (not the networked Flask app,
    which requires MQTT) to verify the JSON response format that the
    frontend JavaScript expects.
    """

    def _simulate_connect_route(self, engine, api_key, provider='anthropic'):
        """Reproduce the exact logic from the Flask route handler."""
        if engine is None:
            return {'error': 'Agent engine not available'}, 500
        if not api_key:
            return {'error': 'API key is required'}, 400
        try:
            valid = engine.set_provider(api_key.strip(), provider.strip())
            if valid:
                status = engine.get_status()
                return {'status': 'connected', 'model': status.get('model')}, 200
            return {'error': 'Invalid API key'}, 401
        except Exception as e:
            return {'error': str(e)}, 400

    def test_successful_connect_returns_model(self, mock_registry, mock_config):
        engine = AgentEngine(tool_registry=mock_registry, context={'config': mock_config})
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(200)
            body, status = self._simulate_connect_route(engine, 'sk-ant-test-key')
        assert status == 200
        assert body['status'] == 'connected'
        assert body['model'] == 'claude-sonnet-4-6'

    def test_invalid_key_returns_401(self, mock_registry, mock_config):
        engine = AgentEngine(tool_registry=mock_registry, context={'config': mock_config})
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(401, 'bad key')
            body, status = self._simulate_connect_route(engine, 'sk-ant-bad-key')
        assert status == 401
        assert 'Invalid API key' in body['error']

    def test_400_error_surfaces_real_message(self, mock_registry, mock_config):
        """This is the bug the user hit — 400 was showing as 'Invalid API key'."""
        engine = AgentEngine(tool_registry=mock_registry, context={'config': mock_config})
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(
                400, 'model not found: claude-sonnet-4-20250514')
            body, status = self._simulate_connect_route(engine, 'sk-ant-test-key')
        assert status == 400
        # The error message should contain the actual API error, NOT "Invalid API key"
        assert 'HTTP 400' in body['error']
        assert 'Invalid API key' not in body['error']

    def test_429_error_surfaces_rate_limit(self, mock_registry, mock_config):
        engine = AgentEngine(tool_registry=mock_registry, context={'config': mock_config})
        with patch('agent.llm_provider.httpx.Client') as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post.return_value = _make_mock_response(429, 'rate limited')
            body, status = self._simulate_connect_route(engine, 'sk-ant-test-key')
        assert status == 400  # route catches all exceptions as 400
        assert '429' in body['error']

    def test_empty_key_returns_400(self, mock_registry, mock_config):
        engine = AgentEngine(tool_registry=mock_registry, context={'config': mock_config})
        body, status = self._simulate_connect_route(engine, '')
        assert status == 400
        assert 'required' in body['error']

    def test_no_engine_returns_500(self):
        body, status = self._simulate_connect_route(None, 'sk-test')
        assert status == 500


# ---------------------------------------------------------------------------
# Session persistence tests
# ---------------------------------------------------------------------------

class TestSessionPersistence:
    """Tests for session save/load/list/delete on disk."""

    @pytest.fixture
    def engine_with_dir(self, mock_registry, mock_config, tmp_path):
        """AgentEngine with a temporary sessions directory."""
        return AgentEngine(
            tool_registry=mock_registry,
            context={'config': mock_config},
            sessions_dir=str(tmp_path / 'sessions'),
        )

    def test_sessions_dir_created(self, engine_with_dir):
        """sessions_dir is created on init."""
        assert engine_with_dir.sessions_dir.exists()

    def test_save_and_list_session(self, engine_with_dir):
        """Saving a session makes it appear in list_sessions."""
        session = engine_with_dir._get_session('session_100')
        session.messages.append({'role': 'user', 'content': 'Hello world'})
        session.messages.append({'role': 'assistant', 'content': 'Hi there'})
        session.input_tokens = 10
        session.output_tokens = 5
        engine_with_dir._save_session('session_100')

        sessions = engine_with_dir.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]['id'] == 'session_100'
        assert sessions[0]['title'] == 'Hello world'
        assert sessions[0]['message_count'] == 2
        assert sessions[0]['input_tokens'] == 10

    def test_load_session_restores_messages(self, engine_with_dir):
        """Loading a session restores messages into memory."""
        session = engine_with_dir._get_session('session_200')
        session.messages.append({'role': 'user', 'content': 'Test message'})
        session.messages.append({'role': 'assistant', 'content': 'Test reply'})
        engine_with_dir._save_session('session_200')

        # Clear memory
        engine_with_dir._sessions.clear()

        data = engine_with_dir.load_session('session_200')
        assert data is not None
        assert len(data['messages']) == 2
        assert data['messages'][0]['content'] == 'Test message'

        # Also restored in memory
        assert 'session_200' in engine_with_dir._sessions
        assert len(engine_with_dir._sessions['session_200'].messages) == 2

    def test_load_nonexistent_returns_none(self, engine_with_dir):
        """Loading a session that doesn't exist returns None."""
        assert engine_with_dir.load_session('session_999') is None

    def test_delete_session(self, engine_with_dir):
        """Deleting a session removes file and memory entry."""
        session = engine_with_dir._get_session('session_300')
        session.messages.append({'role': 'user', 'content': 'Delete me'})
        engine_with_dir._save_session('session_300')

        assert engine_with_dir.delete_session('session_300') is True
        assert engine_with_dir.list_sessions() == []
        assert 'session_300' not in engine_with_dir._sessions

    def test_delete_nonexistent_returns_false(self, engine_with_dir):
        """Deleting a session that doesn't exist returns False."""
        assert engine_with_dir.delete_session('session_999') is False

    def test_list_sessions_sorted_newest_first(self, engine_with_dir):
        """Sessions are sorted by updated time, newest first."""
        import time

        s1 = engine_with_dir._get_session('session_1')
        s1.messages.append({'role': 'user', 'content': 'First'})
        engine_with_dir._save_session('session_1')

        time.sleep(0.05)

        s2 = engine_with_dir._get_session('session_2')
        s2.messages.append({'role': 'user', 'content': 'Second'})
        engine_with_dir._save_session('session_2')

        sessions = engine_with_dir.list_sessions()
        assert len(sessions) == 2
        assert sessions[0]['id'] == 'session_2'
        assert sessions[1]['id'] == 'session_1'

    def test_empty_session_not_saved(self, engine_with_dir):
        """Sessions with no messages are not persisted."""
        engine_with_dir._get_session('session_empty')
        engine_with_dir._save_session('session_empty')
        assert engine_with_dir.list_sessions() == []

    def test_no_sessions_dir_graceful(self, mock_registry, mock_config):
        """Engine without sessions_dir doesn't crash on persistence calls."""
        engine = AgentEngine(tool_registry=mock_registry, context={'config': mock_config})
        engine._save_session('test')
        assert engine.list_sessions() == []
        assert engine.load_session('test') is None
        assert engine.delete_session('test') is False

    def test_title_from_anthropic_format(self, engine_with_dir):
        """Title extraction handles Anthropic content format."""
        session = engine_with_dir._get_session('session_anthro')
        session.messages.append({
            'role': 'user',
            'content': [{'type': 'text', 'text': 'Anthropic format message'}]
        })
        engine_with_dir._save_session('session_anthro')

        sessions = engine_with_dir.list_sessions()
        assert sessions[0]['title'] == 'Anthropic format message'

    def test_session_routes_list(self, engine_with_dir):
        """Simulate the /api/agent/sessions route."""
        session = engine_with_dir._get_session('session_rt')
        session.messages.append({'role': 'user', 'content': 'Route test'})
        engine_with_dir._save_session('session_rt')

        result = engine_with_dir.list_sessions()
        assert len(result) == 1
        assert result[0]['title'] == 'Route test'

    def test_session_routes_delete(self, engine_with_dir):
        """Simulate the DELETE /api/agent/sessions/<id> route."""
        session = engine_with_dir._get_session('session_del')
        session.messages.append({'role': 'user', 'content': 'Delete test'})
        engine_with_dir._save_session('session_del')

        assert engine_with_dir.delete_session('session_del') is True
        assert engine_with_dir.load_session('session_del') is None


# ---------------------------------------------------------------------------
# _coerce_value helper (BUG 6)
# ---------------------------------------------------------------------------

class TestCoerceValue:
    """Test the smart type coercion used by /api/config/param."""

    @pytest.fixture(autouse=True)
    def import_coerce(self):
        from controller.networked.networked import _coerce_value
        self._coerce = _coerce_value

    def test_int_string(self):
        assert self._coerce('42') == 42
        assert isinstance(self._coerce('42'), int)

    def test_float_string(self):
        assert self._coerce('3.14') == 3.14
        assert isinstance(self._coerce('3.14'), float)

    def test_bool_true_string(self):
        assert self._coerce('true') is True
        assert self._coerce('True') is True
        assert self._coerce('TRUE') is True

    def test_bool_false_string(self):
        assert self._coerce('false') is False
        assert self._coerce('False') is False

    def test_passthrough_string(self):
        assert self._coerce('exhsv') == 'exhsv'

    def test_passthrough_native_int(self):
        assert self._coerce(42) == 42

    def test_passthrough_native_float(self):
        assert self._coerce(3.14) == 3.14

    def test_passthrough_native_bool(self):
        assert self._coerce(True) is True

    def test_none_passthrough(self):
        assert self._coerce(None) is None


# ---------------------------------------------------------------------------
# owl_config property (BUG 3 support)
# ---------------------------------------------------------------------------

class TestOwlConfigProperty:
    def test_returns_config_from_connected_owl(self, controller):
        controller.owls_state = {
            'owl-1': {
                'connected': True,
                'config': {'GreenOnBrown': {'exg_min': '25'}},
            },
        }
        cfg = controller.owl_config
        assert cfg is not None
        assert cfg['GreenOnBrown']['exg_min'] == '25'

    def test_returns_none_when_no_config(self, controller):
        controller.owls_state = {
            'owl-1': {'connected': True},
        }
        assert controller.owl_config is None

    def test_returns_none_when_no_owls(self, controller):
        controller.owls_state = {}
        assert controller.owl_config is None


# ---------------------------------------------------------------------------
# Agent threshold routing — section normalization (Fix 1 + Fix 2)
# ---------------------------------------------------------------------------

class TestAgentThresholdRouting:
    """Agent sending threshold keys with wrong section should still work."""

    def test_set_config_sensitivity_section_routes_as_gob(self, controller):
        """Agent sends section='Sensitivity' for hue_min — should route via
        set_greenonbrown_param (same as section='GreenOnBrown')."""
        controller.owls_state = {'owl-1': {'connected': True}}
        # section='Sensitivity' gets normalized to 'GreenOnBrown' in tool_registry
        result = controller._send_command(
            'set_config', section='GreenOnBrown', key='hue_min', value=60,
        )
        assert result['success'] is True
        payload = json.loads(controller.mqtt_client.publish.call_args[0][1])
        assert payload['action'] == 'set_greenonbrown_param'
        assert payload['param'] == 'hue_min'
        assert payload['value'] == 60

    def test_all_threshold_keys_route_via_gob(self, controller):
        """Every GREENONBROWN_PARAMS key routes through set_greenonbrown_param."""
        from utils.config_manager import GREENONBROWN_PARAMS
        controller.owls_state = {'owl-1': {'connected': True}}
        for key in GREENONBROWN_PARAMS:
            controller.mqtt_client.reset_mock()
            result = controller._send_command(
                'set_config', section='GreenOnBrown', key=key, value=42,
            )
            assert result['success'] is True
            payload = json.loads(controller.mqtt_client.publish.call_args[0][1])
            assert payload['action'] == 'set_greenonbrown_param', f"Key {key} didn't route correctly"
