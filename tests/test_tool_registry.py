"""Tests for the OWL Agent Runtime tool registry."""

import configparser

import pytest

from utils.config_manager import GREENONBROWN_PARAMS
from agent.tool_registry import (
    PROTECTED_SECTIONS,
    VALID_ALGORITHMS,
    ToolRegistry,
    owl_tool,
    get_system_status,
    get_config,
    list_presets,
    list_widgets,
    set_config_param,
    set_algorithm,
    set_detection,
    set_sensitivity,
    create_preset,
    create_widget,
    remove_widget,
    update_widget,
    list_custom_algorithms,
    create_algorithm,
    run_algorithm_test,
    deploy_algorithm,
    delete_algorithm,
)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

class MockMQTTClient:
    """Minimal stand-in for DashMQTTSubscriber / OWLMQTTPublisher."""

    def __init__(self, state=None):
        self.current_state = {
            'detection_enable': True,
            'algorithm': 'exhsv',
            'exg_min': 25,
            'exg_max': 200,
            'sensitivity_level': 'medium',
        } if state is None else state
        self.last_command = None

    def _send_command(self, action, **kwargs):
        self.last_command = {'action': action, **kwargs}
        return {'success': True, 'message': f'Command {action} sent'}

    def set_detection_enable(self, value):
        self.last_command = {'action': 'set_detection_enable', 'value': value}
        return {'success': True}

    def set_sensitivity_level(self, level):
        self.last_command = {'action': 'set_sensitivity_level', 'level': level}
        return {'success': True}

    def set_greenonbrown_param(self, param, value):
        self.last_command = {'action': 'set_greenonbrown_param', 'param': param, 'value': value}
        return {'success': True}


class MockSensitivityManager:
    """Minimal stand-in for SensitivityManager."""

    def __init__(self, presets=None):
        self._presets = presets or [
            {'name': 'low', 'values': {'exg_min': 25}, 'is_builtin': True},
            {'name': 'medium', 'values': {'exg_min': 25}, 'is_builtin': True},
            {'name': 'high', 'values': {'exg_min': 22}, 'is_builtin': True},
        ]
        self.last_save = None

    def list_presets(self):
        return self._presets

    def save_custom_preset(self, name, values=None, owl_instance=None):
        self.last_save = name
        return True


class MockWidgetManager:
    """Minimal stand-in for the widget manager."""

    def __init__(self):
        self.installed = []
        self.removed = []
        self.updated = []

    def scan(self):
        return [{'id': 'w1', 'type': 'gauge'}]

    def install(self, widget_id, spec, files=None):
        self.installed.append(spec)
        return (True, None)

    def remove(self, widget_id):
        self.removed.append(widget_id)
        return (True, None)

    def update(self, widget_id, updates):
        self.updated.append((widget_id, updates))
        return (True, None)


def _make_config():
    """Build a ConfigParser with a few sections for testing."""
    config = configparser.ConfigParser()
    config.read_dict({
        'System': {'algorithm': 'exhsv', 'relay_num': '4'},
        'Camera': {'resolution_width': '640', 'resolution_height': '480'},
        'GreenOnBrown': {'exg_min': '25', 'exg_max': '200'},
        'Relays': {'0': '13', '1': '15'},
    })
    return config


def _make_context(**overrides):
    """Return a standard context dict with mocks."""
    ctx = {
        'mqtt_client': MockMQTTClient(),
        'config': _make_config(),
        'sensitivity_manager': MockSensitivityManager(),
        'widget_manager': MockWidgetManager(),
    }
    ctx.update(overrides)
    return ctx


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    """@owl_tool decorator registers function with correct metadata."""

    def test_decorator_attaches_metadata(self):
        @owl_tool(tier='observe', description='test tool', parameters={
            'x': {'type': 'string', 'description': 'a param', 'required': True},
        })
        def my_tool(x, **context):
            return {'x': x}

        assert hasattr(my_tool, '_owl_tool_meta')
        meta = my_tool._owl_tool_meta
        assert meta['tier'] == 'observe'
        assert meta['description'] == 'test tool'
        assert 'x' in meta['parameters']

    def test_register_stores_tool_def(self):
        @owl_tool(tier='apply', description='setter', parameters={})
        def dummy_setter(**context):
            return {}

        reg = ToolRegistry()
        td = reg.register(dummy_setter)
        assert td.name == 'dummy_setter'
        assert td.tier == 'apply'
        assert 'dummy_setter' in reg._tools

    def test_register_rejects_undecorated(self):
        def plain_func():
            pass

        reg = ToolRegistry()
        with pytest.raises(ValueError, match='not decorated'):
            reg.register(plain_func)

    def test_invalid_tier_raises(self):
        with pytest.raises(ValueError, match='Invalid tier'):
            @owl_tool(tier='admin', description='bad', parameters={})
            def bad_tool(**context):
                pass


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    """discover() finds all @owl_tool decorated functions in the module."""

    def test_discover_finds_builtin_tools(self):
        reg = ToolRegistry(developer_mode=True)
        count = reg.discover()
        # observe (5) + apply (12) = 17 tools
        assert count == 17
        assert 'get_system_status' in reg._tools
        assert 'set_config_param' in reg._tools

    def test_discover_idempotent(self):
        reg = ToolRegistry(developer_mode=True)
        first = reg.discover()
        second = reg.discover()
        assert first == 17
        assert second == 0  # already registered


# ---------------------------------------------------------------------------
# Schema generation
# ---------------------------------------------------------------------------

class TestSchemaGeneration:
    def _registry(self):
        reg = ToolRegistry(developer_mode=True)
        reg.discover()
        return reg

    def test_anthropic_format(self):
        reg = self._registry()
        schemas = reg.get_schemas(format='anthropic')
        assert len(schemas) > 0
        for s in schemas:
            assert 'name' in s
            assert 'description' in s
            assert 'input_schema' in s
            assert s['input_schema']['type'] == 'object'
            assert 'properties' in s['input_schema']
            assert 'required' in s['input_schema']

    def test_openai_format(self):
        reg = self._registry()
        schemas = reg.get_schemas(format='openai')
        assert len(schemas) > 0
        for s in schemas:
            assert s['type'] == 'function'
            assert 'function' in s
            fn = s['function']
            assert 'name' in fn
            assert 'description' in fn
            assert 'parameters' in fn
            assert fn['parameters']['type'] == 'object'

    def test_anthropic_required_params(self):
        reg = self._registry()
        schemas = reg.get_schemas(format='anthropic')
        by_name = {s['name']: s for s in schemas}

        sc = by_name['set_config_param']
        assert 'section' in sc['input_schema']['required']
        assert 'key' in sc['input_schema']['required']
        assert 'value' in sc['input_schema']['required']

    def test_openai_set_algorithm_no_enum(self):
        """set_algorithm no longer has a static enum (dynamic validation)."""
        reg = self._registry()
        schemas = reg.get_schemas(format='openai')
        by_name = {s['function']['name']: s for s in schemas}

        sa = by_name['set_algorithm']
        algo_prop = sa['function']['parameters']['properties']['algorithm']
        assert 'enum' not in algo_prop

    def test_no_developer_tools_in_registry(self):
        """No developer-tier tools exist after stub removal."""
        reg = ToolRegistry(developer_mode=True)
        reg.discover()
        developer_tools = [n for n, t in reg._tools.items() if t.tier == 'developer']
        assert developer_tools == []

    def test_invalid_format_raises(self):
        reg = self._registry()
        with pytest.raises(ValueError, match='Unknown schema format'):
            reg.get_schemas(format='gemini')


# ---------------------------------------------------------------------------
# Tier enforcement
# ---------------------------------------------------------------------------

class TestTierEnforcement:
    def test_observe_tool_always_allowed(self):
        reg = ToolRegistry(developer_mode=False)
        reg.discover()
        ctx = _make_context()
        result = reg.call('get_system_status', {}, ctx)
        assert 'status' in result

    def test_apply_tool_always_allowed(self):
        reg = ToolRegistry(developer_mode=False)
        reg.discover()
        ctx = _make_context()
        result = reg.call('set_sensitivity', {'level': 'high'}, ctx)
        assert result['success'] is True


# ---------------------------------------------------------------------------
# Protected config keys
# ---------------------------------------------------------------------------

class TestProtectedKeys:
    def test_reject_relays_section(self):
        reg = ToolRegistry()
        reg.discover()
        ctx = _make_context()
        with pytest.raises(ValueError, match='protected'):
            reg.call('set_config_param', {
                'section': 'Relays', 'key': '0', 'value': '99'
            }, ctx)

    def test_reject_mqtt_section(self):
        reg = ToolRegistry()
        reg.discover()
        ctx = _make_context()
        with pytest.raises(ValueError, match='protected'):
            reg.call('set_config_param', {
                'section': 'MQTT', 'key': 'broker', 'value': 'evil.host'
            }, ctx)

    def test_reject_network_section(self):
        reg = ToolRegistry()
        reg.discover()
        ctx = _make_context()
        with pytest.raises(ValueError, match='protected'):
            reg.call('set_config_param', {
                'section': 'Network', 'key': 'port', 'value': '9999'
            }, ctx)

    def test_reject_webdashboard_section(self):
        reg = ToolRegistry()
        reg.discover()
        ctx = _make_context()
        with pytest.raises(ValueError, match='protected'):
            reg.call('set_config_param', {
                'section': 'WebDashboard', 'key': 'host', 'value': '0.0.0.0'
            }, ctx)

    def test_allowed_section_succeeds(self):
        reg = ToolRegistry()
        reg.discover()
        ctx = _make_context()
        result = reg.call('set_config_param', {
            'section': 'GreenOnBrown', 'key': 'exg_min', 'value': '30'
        }, ctx)
        assert result['success'] is True


# ---------------------------------------------------------------------------
# Tool call validation
# ---------------------------------------------------------------------------

class TestToolCallValidation:
    def test_missing_required_param(self):
        reg = ToolRegistry()
        reg.discover()
        ctx = _make_context()
        with pytest.raises(TypeError, match='Missing required parameter'):
            reg.call('set_config_param', {'section': 'System'}, ctx)

    def test_unknown_tool_raises(self):
        reg = ToolRegistry()
        with pytest.raises(KeyError, match='Unknown tool'):
            reg.call('nonexistent_tool', {}, {})

    def test_valid_call_returns_dict(self):
        reg = ToolRegistry()
        reg.discover()
        ctx = _make_context()
        result = reg.call('get_system_status', {}, ctx)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Individual tool tests
# ---------------------------------------------------------------------------

class TestGetSystemStatus:
    def test_returns_state(self):
        ctx = _make_context()
        result = get_system_status(**ctx)
        assert result['status']['algorithm'] == 'exhsv'
        assert result['status']['detection_enable'] is True

    def test_no_mqtt_client(self):
        result = get_system_status(mqtt_client=None)
        assert 'error' in result

    def test_section_filter(self):
        ctx = _make_context()
        ctx['mqtt_client'].current_state = {
            'exg_min': 25, 'exg_max': 200, 'algorithm': 'exhsv',
        }
        result = get_system_status(section='exg', **ctx)
        assert 'exg_min' in result['status']
        assert 'exg_max' in result['status']
        assert 'algorithm' not in result['status']


class TestGetConfig:
    def test_full_config(self):
        ctx = _make_context()
        result = get_config(**ctx)
        assert 'config' in result
        assert 'System' in result['config']

    def test_section_only(self):
        ctx = _make_context()
        result = get_config(section='Camera', **ctx)
        assert result['section'] == 'Camera'
        assert result['values']['resolution_width'] == '640'

    def test_section_and_key(self):
        ctx = _make_context()
        result = get_config(section='System', key='algorithm', **ctx)
        assert result['value'] == 'exhsv'

    def test_missing_section(self):
        ctx = _make_context()
        result = get_config(section='NonExistent', **ctx)
        assert 'error' in result

    def test_missing_key(self):
        ctx = _make_context()
        result = get_config(section='System', key='nonexistent_key', **ctx)
        assert 'error' in result

    def test_no_config(self):
        result = get_config(config=None)
        assert 'error' in result


class TestListPresets:
    def test_returns_presets(self):
        ctx = _make_context()
        result = list_presets(**ctx)
        assert len(result['presets']) == 3
        names = {p['name'] for p in result['presets']}
        assert 'low' in names

    def test_no_manager(self):
        result = list_presets(sensitivity_manager=None)
        assert result['presets'] == []

    def test_local_config_fallback(self):
        """BUG 7: list_presets reads Sensitivity_* sections from local config."""
        config = configparser.ConfigParser()
        config.read_dict({
            'Sensitivity_Low': {'exg_min': '30'},
            'Sensitivity_Medium': {'exg_min': '25'},
            'Sensitivity_High': {'exg_min': '20'},
            'System': {'algorithm': 'exhsv'},
        })
        # No MQTT state, no owl_config, no sensitivity_manager
        result = list_presets(
            mqtt_client=None,
            config=config,
            sensitivity_manager=None,
        )
        assert set(result['presets']) == {'Low', 'Medium', 'High'}

    def test_local_config_fallback_empty(self):
        """Local config with no Sensitivity_ sections returns empty."""
        config = configparser.ConfigParser()
        config.read_dict({'System': {'algorithm': 'exhsv'}})
        result = list_presets(
            mqtt_client=None,
            config=config,
            sensitivity_manager=None,
        )
        assert result['presets'] == []


class TestListWidgets:
    def test_returns_widgets(self):
        ctx = _make_context()
        result = list_widgets(**ctx)
        assert len(result['widgets']) == 1
        assert result['widgets'][0]['id'] == 'w1'

    def test_no_manager(self):
        result = list_widgets(widget_manager=None)
        assert result['widgets'] == []


class TestSetAlgorithm:
    def test_valid_algorithm(self):
        ctx = _make_context()
        result = set_algorithm(algorithm='exg', **ctx)
        assert result['success'] is True
        assert ctx['mqtt_client'].last_command['action'] == 'set_algorithm'
        assert ctx['mqtt_client'].last_command['value'] == 'exg'

    def test_invalid_algorithm(self):
        ctx = _make_context()
        with pytest.raises(ValueError, match='Invalid algorithm'):
            set_algorithm(algorithm='invalid_algo', **ctx)

    def test_all_valid_algorithms_accepted(self):
        for algo in VALID_ALGORITHMS:
            ctx = _make_context()
            result = set_algorithm(algorithm=algo, **ctx)
            assert result['success'] is True

    def test_no_mqtt_client(self):
        result = set_algorithm(algorithm='exg', mqtt_client=None)
        assert 'error' in result


class TestSetDetection:
    def test_enable(self):
        ctx = _make_context()
        result = set_detection(enabled=True, **ctx)
        assert result['success'] is True
        assert ctx['mqtt_client'].last_command['value'] is True

    def test_disable(self):
        ctx = _make_context()
        result = set_detection(enabled=False, **ctx)
        assert result['success'] is True
        assert ctx['mqtt_client'].last_command['value'] is False

    def test_mode_spot_spray(self):
        ctx = _make_context()
        result = set_detection(enabled=True, mode=0, **ctx)
        assert result['success'] is True
        assert ctx['mqtt_client'].last_command['action'] == 'set_detection_mode'
        assert ctx['mqtt_client'].last_command['value'] == 0

    def test_mode_blanket(self):
        ctx = _make_context()
        result = set_detection(enabled=True, mode=2, **ctx)
        assert result['success'] is True
        assert ctx['mqtt_client'].last_command['value'] == 2

    def test_invalid_mode(self):
        ctx = _make_context()
        with pytest.raises(ValueError, match='Invalid detection mode'):
            set_detection(enabled=True, mode=5, **ctx)

    def test_no_mqtt_client(self):
        result = set_detection(enabled=True, mqtt_client=None)
        assert 'error' in result


class TestSetSensitivity:
    def test_sends_command(self):
        ctx = _make_context()
        result = set_sensitivity(level='high', **ctx)
        assert result['success'] is True
        assert ctx['mqtt_client'].last_command['level'] == 'high'


class TestCreatePreset:
    def test_saves_preset(self):
        ctx = _make_context()
        result = create_preset(name='custom1', **ctx)
        assert result['success'] is True
        assert ctx['mqtt_client'].last_command['action'] == 'save_sensitivity_preset'
        assert ctx['mqtt_client'].last_command['name'] == 'custom1'

    def test_no_mqtt_client(self):
        result = create_preset(name='x', mqtt_client=None)
        assert 'error' in result


class TestWidgetTools:
    def test_create_widget(self):
        ctx = _make_context()
        spec = {'id': 'speed-gauge', 'type': 'gauge', 'label': 'Speed'}
        result = create_widget(spec=spec, **ctx)
        assert result['success'] is True
        assert ctx['widget_manager'].installed == [spec]

    def test_remove_widget(self):
        ctx = _make_context()
        result = remove_widget(widget_id='w1', **ctx)
        assert result['success'] is True
        assert 'w1' in ctx['widget_manager'].removed

    def test_update_widget(self):
        ctx = _make_context()
        updates = {'label': 'New Label'}
        result = update_widget(widget_id='w1', updates=updates, **ctx)
        assert result['success'] is True
        assert ctx['widget_manager'].updated == [('w1', updates)]

    def test_create_widget_no_manager(self):
        result = create_widget(spec={}, widget_manager=None)
        assert 'error' in result

    def test_remove_widget_no_manager(self):
        result = remove_widget(widget_id='w1', widget_manager=None)
        assert 'error' in result

    def test_update_widget_no_manager(self):
        result = update_widget(widget_id='w1', updates={}, widget_manager=None)
        assert 'error' in result


# ---------------------------------------------------------------------------
# list_tools
# ---------------------------------------------------------------------------

class TestListTools:
    def test_excludes_developer_by_default(self):
        reg = ToolRegistry()
        reg.discover()
        tools = reg.list_tools(include_developer=False)
        tiers = {t['tier'] for t in tools}
        assert 'developer' not in tiers
        assert 'observe' in tiers
        assert 'apply' in tiers

    def test_no_developer_tools_to_include(self):
        reg = ToolRegistry()
        reg.discover()
        tools = reg.list_tools(include_developer=True)
        tiers = {t['tier'] for t in tools}
        assert 'developer' not in tiers

    def test_metadata_shape(self):
        reg = ToolRegistry()
        reg.discover()
        tools = reg.list_tools()
        for t in tools:
            assert 'name' in t
            assert 'tier' in t
            assert 'description' in t
            assert 'parameters' in t


# ---------------------------------------------------------------------------
# Custom algorithm tools
# ---------------------------------------------------------------------------

class TestCustomAlgorithmTools:
    """Tests for list/create/test/deploy/delete algorithm tools."""

    @pytest.fixture(autouse=True)
    def _temp_algo_dir(self, tmp_path, monkeypatch):
        import custom_algorithms
        monkeypatch.setattr(custom_algorithms, 'CUSTOM_ALGO_DIR', tmp_path)
        self.algo_dir = tmp_path

    VALID_CODE = (
        'import numpy as np\n'
        'import cv2\n'
        'def bright(image):\n'
        '    b, g, r = cv2.split(image)\n'
        '    return (2.0 * g.astype(np.float32) - r.astype(np.float32) - b.astype(np.float32))'
        '.clip(0, 255).astype("uint8")\n'
    )

    def test_list_custom_algorithms_empty(self):
        result = list_custom_algorithms()
        assert result['algorithms'] == []

    def test_create_algorithm_valid(self):
        result = create_algorithm(name='test_algo', code=self.VALID_CODE,
                                  description='Test algo')
        assert result['success'] is True
        assert (self.algo_dir / 'test_algo.py').exists()

    def test_create_algorithm_invalid_code(self):
        bad_code = 'import os\ndef algo(image): pass'
        result = create_algorithm(name='bad', code=bad_code)
        assert result['success'] is False
        assert 'validation' in result['error'].lower()

    def test_create_algorithm_invalid_name(self):
        result = create_algorithm(name='../bad', code=self.VALID_CODE)
        assert result['success'] is False

    def test_list_after_create(self):
        create_algorithm(name='algo1', code=self.VALID_CODE, description='First')
        result = list_custom_algorithms()
        assert len(result['algorithms']) == 1
        assert result['algorithms'][0]['name'] == 'algo1'

    def test_test_algorithm(self):
        create_algorithm(name='testable', code=self.VALID_CODE)
        result = run_algorithm_test(name='testable')
        assert result['success'] is True
        assert result['timing_ms'] >= 0
        assert result['output_shape'] == [480, 640]
        assert isinstance(result['detection_count'], int)

    def test_test_algorithm_not_found(self):
        result = run_algorithm_test(name='nonexistent')
        assert result['success'] is False
        assert 'not found' in result['error'].lower()

    def test_deploy_algorithm(self):
        ctx = _make_context()
        create_algorithm(name='deployable', code=self.VALID_CODE)
        result = deploy_algorithm(name='deployable', **ctx)
        assert result['success'] is True
        assert ctx['mqtt_client'].last_command['action'] == 'install_algorithm'

    def test_deploy_nonexistent(self):
        ctx = _make_context()
        result = deploy_algorithm(name='nonexistent', **ctx)
        assert result['success'] is False

    def test_delete_algorithm(self):
        create_algorithm(name='to_delete', code=self.VALID_CODE)
        result = delete_algorithm(name='to_delete')
        assert result['success'] is True
        assert not (self.algo_dir / 'to_delete.py').exists()

    def test_delete_nonexistent(self):
        result = delete_algorithm(name='nonexistent')
        assert result['success'] is False

    def test_set_algorithm_accepts_custom(self):
        """set_algorithm accepts a custom algorithm name after creation."""
        ctx = _make_context()
        create_algorithm(name='custom_exg', code=self.VALID_CODE)
        result = set_algorithm(algorithm='custom_exg', **ctx)
        assert result['success'] is True

    def test_set_algorithm_rejects_unknown(self):
        ctx = _make_context()
        with pytest.raises(ValueError, match='Invalid algorithm'):
            set_algorithm(algorithm='totally_fake', **ctx)


# ---------------------------------------------------------------------------
# Section normalization for threshold keys (agent config fix)
# ---------------------------------------------------------------------------

class TestSetConfigParamNormalization:
    """set_config_param normalizes threshold keys to GreenOnBrown section."""

    def test_sensitivity_section_normalized_to_gob(self):
        """Agent sending section='Sensitivity' for hue_min gets normalized."""
        ctx = _make_context()
        result = set_config_param(section='Sensitivity', key='hue_min', value='60', **ctx)
        assert result['success'] is True
        cmd = ctx['mqtt_client'].last_command
        assert cmd['section'] == 'GreenOnBrown'

    def test_all_threshold_keys_normalized(self):
        """Every GREENONBROWN_PARAMS key gets normalized regardless of section."""
        for key in GREENONBROWN_PARAMS:
            ctx = _make_context()
            result = set_config_param(section='WrongSection', key=key, value='42', **ctx)
            assert result['success'] is True
            assert ctx['mqtt_client'].last_command['section'] == 'GreenOnBrown'

    def test_non_threshold_key_keeps_section(self):
        """Non-threshold keys keep their original section."""
        ctx = _make_context()
        result = set_config_param(section='Camera', key='resolution_width', value='1280', **ctx)
        assert result['success'] is True
        assert ctx['mqtt_client'].last_command['section'] == 'Camera'

    def test_gob_section_stays_gob(self):
        """GreenOnBrown section for threshold keys stays unchanged."""
        ctx = _make_context()
        result = set_config_param(section='GreenOnBrown', key='exg_min', value='30', **ctx)
        assert result['success'] is True
        assert ctx['mqtt_client'].last_command['section'] == 'GreenOnBrown'


# ---------------------------------------------------------------------------
# get_system_status threshold pull-through (networked controller)
# ---------------------------------------------------------------------------

class TestGetSystemStatusThresholds:
    """get_system_status pulls threshold values from connected OWL state."""

    def test_threshold_values_pulled_from_owl(self):
        mqtt = MockMQTTClient(state={})
        mqtt.owls_state = {
            'owl-1': {
                'connected': True,
                'detection_enable': True,
                'exg_min': 25, 'exg_max': 200,
                'hue_min': 30, 'hue_max': 90,
                'saturation_min': 30, 'saturation_max': 255,
                'brightness_min': 5, 'brightness_max': 200,
                'min_detection_area': 10, 'invert_hue': False,
            },
        }
        result = get_system_status(mqtt_client=mqtt)
        status = result['status']
        assert status['exg_min'] == 25
        assert status['hue_min'] == 30
        assert status['invert_hue'] is False


# ---------------------------------------------------------------------------
# get_config GreenOnBrown fallback from OWL MQTT state
# ---------------------------------------------------------------------------

class TestGetConfigGoBFallback:
    """get_config constructs GreenOnBrown from OWL published state."""

    def test_gob_from_owl_state(self):
        config = configparser.ConfigParser()
        config.read_dict({'System': {'algorithm': 'exhsv'}})
        mqtt = MockMQTTClient()
        mqtt.owls_state = {
            'owl-1': {
                'connected': True,
                'exg_min': 25, 'exg_max': 200,
                'hue_min': 30, 'hue_max': 90,
                'saturation_min': 30, 'saturation_max': 255,
                'brightness_min': 5, 'brightness_max': 200,
                'min_detection_area': 10, 'invert_hue': False,
            },
        }
        result = get_config(section='GreenOnBrown', config=config, mqtt_client=mqtt)
        assert 'error' not in result
        assert result['values']['exg_min'] == '25'
        assert result['values']['hue_min'] == '30'

    def test_gob_local_config_takes_priority(self):
        """Local config GreenOnBrown section takes priority over OWL state."""
        config = configparser.ConfigParser()
        config.read_dict({'GreenOnBrown': {'exg_min': '99'}})
        mqtt = MockMQTTClient()
        mqtt.owls_state = {
            'owl-1': {'connected': True, 'exg_min': 25},
        }
        result = get_config(section='GreenOnBrown', config=config, mqtt_client=mqtt)
        assert result['values']['exg_min'] == '99'
