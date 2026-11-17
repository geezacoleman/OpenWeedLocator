"""The agent's tools (agent/tool_registry.py).

Every tool that sends a command is called through ToolRegistry.call against
the two real clients it runs on: DashMQTTSubscriber (standalone dashboard)
and CentralController (in-cab controller). The assertion is the exact message
each one publishes. Until the 2026-11-16 audit these ran against a
hand-written MockMQTTClient, so a tool and its real client could disagree
(and did: see TestSprayOnTheCabController).
"""

import configparser
import sys
import types

import pytest

from agent.tool_registry import PROTECTED_SECTIONS, ToolRegistry, owl_tool
from agent.widget_manager import WidgetManager
from agent_fakes import central, commands, dash_subscriber, forget, published, as_the_cab_sends
from utils.config_manager import GREENONBROWN_PARAMS

TOOLS = {
    'observe': {'get_system_status', 'get_config', 'list_widgets', 'list_presets',
                'list_custom_algorithms'},
    'apply': {'set_config_param', 'set_sensitivity', 'create_preset', 'set_algorithm',
              'set_detection', 'create_widget', 'remove_widget', 'update_widget',
              'create_algorithm', 'run_algorithm_test', 'deploy_algorithm', 'delete_algorithm'},
}


def ctx(mqtt_client, **more):
    return dict({'mqtt_client': mqtt_client, 'config': None, 'widget_manager': None}, **more)


def is_cab(client):
    return hasattr(client, 'owls_state')


# --------------------------------------------------------------------------
# Registration, discovery, schemas
# --------------------------------------------------------------------------

class TestRegistration:
    def test_a_decorated_function_registers_with_its_required_parameters(self):
        @owl_tool(tier='apply', description='Set a thing', parameters={
            'name': {'type': 'string', 'description': 'which', 'required': True},
            'note': {'type': 'string', 'required': False}})
        def set_thing(name, note=None, **context):
            return {'name': name}

        tool = ToolRegistry().register(set_thing)
        assert (tool.name, tool.tier, tool.description, tool.required) == (
            'set_thing', 'apply', 'Set a thing', ['name'])

    def test_an_undecorated_function_is_refused(self):
        def plain():
            pass
        with pytest.raises(ValueError, match='not decorated'):
            ToolRegistry().register(plain)

    def test_an_unknown_tier_is_refused_at_decoration(self):
        with pytest.raises(ValueError, match='Invalid tier'):
            owl_tool(tier='superadmin', description='x')

    def test_discover_finds_exactly_the_shipped_tools_and_only_once(self, registry):
        by_tier = {}
        for tool in registry.list_tools(include_developer=True):
            by_tier.setdefault(tool['tier'], set()).add(tool['name'])
        assert by_tier == TOOLS, 'a new tool: add it here and test what it sends'
        assert registry.discover() == 0


class TestSchemas:
    def test_anthropic_schema(self, registry):
        schema = {s['name']: s for s in registry.get_schemas('anthropic')}['set_config_param']
        assert schema['input_schema'] == {
            'type': 'object',
            'properties': {
                'section': {'type': 'string', 'description': 'Config section name'},
                'key': {'type': 'string', 'description': 'Config key to set'},
                'value': {'type': 'string', 'description': 'Value to set'}},
            'required': ['section', 'key', 'value']}
        # the model is told what it may not touch, so it says so instead of trying
        for word in ('Relays', 'Controller', 'GPS', 'MQTT', 'Network', 'WebDashboard', 'Cloud',
                     'relay_num', 'on_error'):
            assert word in schema['description']

    def test_openai_schema_wraps_the_same_parameters(self, registry):
        anthropic = {s['name']: s for s in registry.get_schemas('anthropic')}
        for schema in registry.get_schemas('openai'):
            assert schema['type'] == 'function'
            function = schema['function']
            assert function['parameters'] == anthropic[function['name']]['input_schema']
            assert function['description'] == anthropic[function['name']]['description']
        assert len(registry.get_schemas('openai')) == len(anthropic) == 17

    def test_set_detection_is_described_with_no_required_field_and_typed_inputs(self, registry):
        schema = {s['name']: s for s in registry.get_schemas()}['set_detection']['input_schema']
        assert schema['required'] == []
        assert {name: prop['type'] for name, prop in schema['properties'].items()} == {
            'state': 'string', 'enabled': 'boolean', 'mode': 'integer'}

    def test_an_unknown_format_is_refused(self, registry):
        with pytest.raises(ValueError, match='Unknown schema format'):
            registry.get_schemas(format='gemini')


# --------------------------------------------------------------------------
# Tier enforcement and call validation
# --------------------------------------------------------------------------

def registry_with_a_developer_tool(developer_mode):
    ran = []

    @owl_tool(tier='developer', description='Run a shell command',
              parameters={'command': {'type': 'string', 'required': True}})
    def run_shell(command, **context):
        ran.append(command)
        return {'output': 'done'}

    reg = ToolRegistry(developer_mode=developer_mode)
    reg.discover()
    reg.register(run_shell)
    return reg, ran


class TestTiers:
    def test_a_developer_tool_is_refused_and_never_runs_outside_developer_mode(self):
        reg, ran = registry_with_a_developer_tool(developer_mode=False)
        with pytest.raises(PermissionError, match="'run_shell' requires developer mode"):
            reg.call('run_shell', {'command': 'rm -rf /'}, {})
        assert ran == []

    def test_a_developer_tool_is_hidden_from_the_model_outside_developer_mode(self):
        reg, _ = registry_with_a_developer_tool(developer_mode=False)
        assert 'run_shell' not in {s['name'] for s in reg.get_schemas('anthropic')}
        assert 'run_shell' not in {s['function']['name'] for s in reg.get_schemas('openai')}
        assert 'run_shell' not in {t['name'] for t in reg.list_tools()}
        assert 'run_shell' in {t['name'] for t in reg.list_tools(include_developer=True)}

    def test_developer_mode_allows_and_shows_it(self):
        reg, ran = registry_with_a_developer_tool(developer_mode=True)
        assert reg.call('run_shell', {'command': 'uptime'}, {}) == {'output': 'done'}
        assert ran == ['uptime']
        assert 'run_shell' in {s['name'] for s in reg.get_schemas()}

    def test_both_dashboards_run_without_developer_mode(self):
        """A developer tool added later must not be callable from a dashboard."""
        import controller.networked.networked as net_mod
        assert net_mod.agent_engine.tool_registry.developer_mode is False

    def test_the_standalone_dashboard_runs_without_developer_mode(self, standalone_app):
        assert standalone_app.engine.tool_registry.developer_mode is False


class TestCallValidation:
    def test_a_missing_required_parameter_is_refused_before_the_tool_runs(self, registry, dash):
        with pytest.raises(TypeError, match="Missing required parameter 'key'"):
            registry.call('set_config_param', {'section': 'Camera', 'value': '1'}, ctx(dash))
        assert published(dash) == []

    def test_an_unknown_tool_is_a_key_error(self, registry):
        with pytest.raises(KeyError, match='Unknown tool: open_valves'):
            registry.call('open_valves', {}, {})

    def test_a_misspelt_parameter_is_dropped_not_taken_as_an_instruction(self, registry, dash):
        """Unknown names fall into the tool's **context and are ignored, so
        {'enable': false} reaches set_detection as no arguments: refused."""
        with pytest.raises(ValueError, match='Give state'):
            registry.call('set_detection', {'enable': False}, ctx(dash))
        assert published(dash) == []

    def test_a_tool_call_cannot_replace_what_the_dashboard_injects(self, registry, dash):
        with pytest.raises(TypeError, match='mqtt_client'):
            registry.call('set_detection', {'state': 'blanket', 'mqtt_client': 'mine'}, ctx(dash))
        assert published(dash) == []

    def test_a_result_that_is_not_a_dict_is_wrapped(self):
        @owl_tool(tier='observe', description='x')
        def answer(**context):
            return 42
        reg = ToolRegistry()
        reg.register(answer)
        assert reg.call('answer', {}, {}) == {'result': 42}


# --------------------------------------------------------------------------
# set_detection: the tool that opens and closes nozzles
# --------------------------------------------------------------------------

SPOT = {'action': 'set_detection_enable', 'value': True}
OFF = {'action': 'set_detection_enable', 'value': False}


def mode(code):
    return {'action': 'set_detection_mode', 'value': code}


class TestSetDetection:
    @pytest.mark.parametrize('arguments, payload', [
        ({'state': 'spot'}, mode(0)),
        ({'state': 'off'}, mode(1)),
        ({'state': 'blanket'}, mode(2)),
        ({'mode': 0}, mode(0)),
        ({'mode': 1}, mode(1)),
        ({'mode': 2}, mode(2)),
        ({'enabled': True, 'mode': 2}, mode(2)),
        ({'enabled': True}, SPOT),
        ({'enabled': 'true'}, SPOT),
        ({'enabled': 1}, SPOT),
        ({'enabled': 'ON'}, SPOT),
        ({'enabled': False}, OFF),
        ({'enabled': 'false'}, OFF),          # bool('false') is True: this sprayed
        ({'enabled': 'False'}, OFF),
        ({'enabled': 0}, OFF),
        ({'enabled': 'off'}, OFF),
        ({'enabled': False, 'mode': 2}, OFF),     # this blanket sprayed
        ({'enabled': 'false', 'mode': 2}, OFF),
        ({'enabled': 0, 'mode': 2}, OFF),
        ({'enabled': False, 'mode': 0}, OFF),
        ({'state': 'off', 'enabled': True, 'mode': 2}, mode(1)),   # state decides
    ])
    def test_each_form_publishes_exactly_one_command_with_this_meaning(
            self, registry, mqtt_client, arguments, payload):
        result = registry.call('set_detection', arguments, ctx(mqtt_client))
        if hasattr(mqtt_client, 'owls_state'):          # the in-cab controller
            payload = as_the_cab_sends(payload)
        assert commands(mqtt_client) == [payload]
        assert result['success'] is True

    @pytest.mark.parametrize('arguments, reason', [
        ({}, 'Give state'),
        ({'enabled': None}, 'Give state'),
        ({'enabled': 'maybe'}, 'Invalid enabled value'),
        ({'enabled': 2}, 'Invalid enabled value'),
        ({'enabled': ''}, 'Invalid enabled value'),
        ({'enabled': 'banana', 'mode': 2}, 'Invalid enabled value'),
        ({'enabled': 'banana', 'state': 'blanket'}, 'Invalid enabled value'),
        ({'state': 'on'}, 'Invalid spray state'),
        ({'state': 'Blanket'}, 'Invalid spray state'),
        ({'state': 2}, 'Invalid spray state'),
        ({'mode': 3}, 'Invalid detection mode'),
        ({'mode': -1}, 'Invalid detection mode'),
        ({'mode': '2'}, 'Invalid detection mode'),
        ({'enabled': True, 'mode': 5}, 'Invalid detection mode'),
    ])
    def test_anything_unclear_is_refused_and_nothing_is_published(
            self, registry, mqtt_client, arguments, reason):
        with pytest.raises(ValueError, match=reason):
            registry.call('set_detection', arguments, ctx(mqtt_client))
        assert published(mqtt_client) == []

    def test_no_client_is_an_error_result(self, registry):
        assert registry.call('set_detection', {'state': 'off'}, ctx(None)) == {
            'error': 'No MQTT client available'}

    def test_a_dashboard_with_no_broker_says_so_and_does_not_claim_success(self, registry, tmp_path):
        offline = dash_subscriber(connected=False)
        assert registry.call('set_detection', {'state': 'blanket'}, ctx(offline)) == {
            'success': False, 'error': 'Not connected to MQTT broker'}
        assert published(offline) == []

        cab = central(tmp_path)
        cab.mqtt_connected = False
        for arguments in ({'state': 'blanket'}, {'enabled': True}):
            assert registry.call('set_detection', arguments, ctx(cab)) == {
                'success': False, 'error': 'MQTT not connected'}
        assert published(cab) == []
        assert cab.desired_state == {}

    def test_the_standalone_dashboard_publishes_on_the_owls_command_topic(self, registry, dash):
        registry.call('set_detection', {'state': 'off'}, ctx(dash))
        assert published(dash) == [('owl/commands', mode(1))]

    def test_the_cab_controller_sends_to_every_connected_owl_and_no_other(self, registry, tmp_path):
        cab = central(tmp_path, owls=('owl-1', 'owl-2', 'owl-3'))
        cab.owls_state['owl-2']['connected'] = False
        registry.call('set_detection', {'state': 'off'}, ctx(cab))
        assert published(cab) == [('owl/owl-1/commands', OFF), ('owl/owl-3/commands', OFF)]


class TestSprayOnTheCabController:
    """The in-cab controller keeps what the screen asked for (desired_state)
    and puts every OWL back to it: on reconnect and every few seconds
    (_reconcile_spray). A spray change the controller does not record is
    undone by the controller itself."""

    @pytest.mark.parametrize('arguments, stored', [
        ({'enabled': True}, 'spot'),
        ({'enabled': 'false'}, 'off'),
        ({'enabled': False, 'mode': 2}, 'off'),
    ])
    def test_the_enabled_form_is_recorded_as_what_the_screen_wants(self, registry, cab, arguments, stored):
        cab.send_command('all', 'toggle_detection', stored != 'spot')     # the opposite, first
        registry.call('set_detection', arguments, ctx(cab))
        assert cab.desired_state == {'all': {'spray': stored}}

        forget(cab)
        cab._push_desired_state('owl-1')                                  # owl-1 reconnects
        assert commands(cab) == [SPOT if stored == 'spot' else OFF]

    @pytest.mark.parametrize('state', ['off', 'spot', 'blanket'])
    def test_the_state_form_is_recorded_too(self, registry, cab, state):
        cab.send_command('all', 'toggle_detection', state != 'spot')      # the opposite, first
        registry.call('set_detection', {'state': state}, ctx(cab))
        assert cab.desired_state['all']['spray'] == state

    def test_an_agent_off_is_not_undone_by_the_controller(self, registry, cab):
        cab.send_command('all', 'toggle_detection', True)                 # operator: spot
        registry.call('set_detection', {'state': 'off'}, ctx(cab))        # agent: off
        cab.owls_state['owl-1']['spray'] = {                              # the OWL obeyed
            'request': 'off', 'reason': None, 'source': 'dashboard', 'requested_at': 2.0}
        forget(cab)

        for oid in cab._spray_sent_at:                                    # 10 s later
            cab._spray_sent_at[oid] -= 10
        cab._reconcile_spray()
        cab._push_desired_state('owl-1')                                  # and a reconnect

        # The reconnect push re-asserts what is recorded, which is now off
        assert cab.desired_state['all']['spray'] == 'off'
        assert commands(cab) in ([], [OFF]), 'the controller switched spraying back on by itself'


# --------------------------------------------------------------------------
# The other tools that send a command
# --------------------------------------------------------------------------

class TestCommandsSent:
    def test_set_sensitivity_sends_the_preset_name_lower_case(self, registry, mqtt_client):
        result = registry.call('set_sensitivity', {'level': 'High'}, ctx(mqtt_client))
        assert commands(mqtt_client) == [{'action': 'set_sensitivity_level', 'level': 'high'}]
        assert result['success'] is True

    def test_create_preset_asks_the_owl_to_save_its_current_values(self, registry, mqtt_client):
        registry.call('create_preset', {'name': 'rainy day'}, ctx(mqtt_client))
        assert commands(mqtt_client) == [{'action': 'save_sensitivity_preset', 'name': 'rainy day'}]

    def test_set_algorithm_sends_the_algorithm(self, registry, mqtt_client):
        registry.call('set_algorithm', {'algorithm': 'exhsv'}, ctx(mqtt_client))
        assert commands(mqtt_client) == [{'action': 'set_algorithm', 'value': 'exhsv'}]

    @pytest.mark.parametrize('algorithm', ['yolo', 'EXHSV', '', 'my_custom_algo'])
    def test_set_algorithm_refuses_a_name_the_owl_does_not_have(self, registry, mqtt_client, algorithm):
        with pytest.raises(ValueError, match='Invalid algorithm'):
            registry.call('set_algorithm', {'algorithm': algorithm}, ctx(mqtt_client))
        assert published(mqtt_client) == []

    def test_set_algorithm_takes_every_name_the_config_validator_does(self, registry, dash):
        from utils.config_manager import ConfigValidator
        valid = sorted(ConfigValidator.get_valid_algorithms())
        assert {'exg', 'exhsv', 'hsv', 'gog', 'gog-hybrid'} <= set(valid)
        for algorithm in valid:
            registry.call('set_algorithm', {'algorithm': algorithm}, ctx(dash))
        assert [c['value'] for c in commands(dash)] == valid

    def test_a_threshold_goes_to_greenonbrown_whatever_section_the_model_named(self, registry, mqtt_client):
        """The model often says section='Sensitivity' for hue_min."""
        registry.call('set_config_param', {'section': 'Sensitivity', 'key': 'hue_min', 'value': '60'},
                      ctx(mqtt_client))
        expected = ({'action': 'set_greenonbrown_param', 'param': 'hue_min', 'value': '60'}
                    if is_cab(mqtt_client) else
                    {'action': 'set_config', 'section': 'GreenOnBrown', 'key': 'hue_min', 'value': '60'})
        assert commands(mqtt_client) == [expected]

    def test_every_threshold_key_is_routed_as_a_threshold(self, registry, tmp_path):
        cab = central(tmp_path)
        for key in sorted(GREENONBROWN_PARAMS):
            registry.call('set_config_param', {'section': 'System', 'key': key, 'value': '42'}, ctx(cab))
        assert commands(cab) == [{'action': 'set_greenonbrown_param', 'param': key, 'value': '42'}
                                 for key in sorted(GREENONBROWN_PARAMS)]

    def test_any_other_key_is_a_section_edit_with_the_value_as_text(self, registry, mqtt_client):
        registry.call('set_config_param', {'section': 'Camera', 'key': 'exp_compensation', 'value': -2},
                      ctx(mqtt_client))
        assert commands(mqtt_client) == [{'action': 'set_config_section', 'section': 'Camera',
                                          'params': {'exp_compensation': '-2'}}]

    @pytest.mark.parametrize('name, arguments', [
        ('set_sensitivity', {'level': 'high'}),
        ('create_preset', {'name': 'x'}),
        ('set_algorithm', {'algorithm': 'exg'}),
        ('set_config_param', {'section': 'Camera', 'key': 'rotation', 'value': '180'}),
    ])
    def test_no_client_is_an_error_result(self, registry, name, arguments):
        assert registry.call(name, arguments, ctx(None)) == {'error': 'No MQTT client available'}


# --------------------------------------------------------------------------
# What set_config_param may not touch
# --------------------------------------------------------------------------

class TestProtectedSections:
    def test_what_the_assistant_may_never_change(self):
        """Wiring, how the unit is reached, and what a fault does to the
        nozzles (decided 2026-11-17). Adding to these lists is a decision,
        not a refactor: this test is the list."""
        from agent.tool_registry import PROTECTED_KEYS
        assert PROTECTED_SECTIONS == {'Relays', 'Controller', 'GPS',
                                      'MQTT', 'Network', 'WebDashboard', 'Cloud'}
        assert PROTECTED_KEYS == {'System': {'relay_num', 'on_error'}}

    @pytest.mark.parametrize('section, key', [
        ('Relays', '0'), ('MQTT', 'broker_ip'), ('Network', 'mode'), ('WebDashboard', 'ui'),
        ('Controller', 'switch_pin'), ('GPS', 'source'), ('GPS', 'port'), ('Cloud', 'broker_host')])
    def test_a_protected_section_is_refused_and_nothing_is_published(
            self, registry, mqtt_client, section, key):
        with pytest.raises(ValueError, match=rf'Section \[{section}\] is protected'):
            registry.call('set_config_param', {'section': section, 'key': key, 'value': '7'},
                          ctx(mqtt_client))
        assert published(mqtt_client) == []

    def test_a_threshold_key_does_not_get_a_protected_section_past_the_check(self, registry, dash):
        with pytest.raises(ValueError, match='protected'):
            registry.call('set_config_param', {'section': 'Relays', 'key': 'exg_min', 'value': '7'},
                          ctx(dash))
        assert published(dash) == []

    @pytest.mark.parametrize('section', ['relays', 'RELAYS', 'Relays ', ' Relays', 'mqtt', 'network',
                                         'webdashboard'])
    def test_a_protected_section_in_another_spelling_is_refused_too(self, registry, dash, section):
        try:
            registry.call('set_config_param', {'section': section, 'key': '0', 'value': '7'},
                          ctx(dash))
        except ValueError:
            pass
        assert published(dash) == []

    @pytest.mark.parametrize('section, key, value', [
        ('System', 'actuation_duration', '600'),
        ('System', 'delay', '2.5'),
        ('Actuation', 'offset_cm', '500'),
        ('System', 'algorithm', 'exhsv'),
    ])
    def test_spray_timing_keys_are_forwarded_unchanged_for_the_owl_to_clamp(
            self, registry, mqtt_client, section, key, value):
        """What the code does. The tool checks nothing here: the bounds are
        the OWL's (_handle_set_config_section clamps timing and geometry;
        tests/owl/test_mqtt_commands_at_the_relay.py proves that at the relay)."""
        registry.call('set_config_param', {'section': section, 'key': key, 'value': value},
                      ctx(mqtt_client))
        assert commands(mqtt_client) == [
            {'action': 'set_config_section', 'section': section, 'params': {key: value}}]

    @pytest.mark.parametrize('section, key, value', [
        ('System', 'relay_num', '8'),
        ('System', 'on_error', 'on'),
        ('Controller', 'controller_type', 'none'),
        ('Controller', 'switch_purpose', 'recording'),
        ('Controller', 'switch_pin', '13'),
        ('Controller', 'detection_mode_pin_up', '15'),
        ('system', 'RELAY_NUM', '8'), (' System ', 'on_error ', 'on'),
        ('GPS', 'baudrate', '9600'), ('Cloud', 'enable', 'False'),
    ])
    def test_hardware_and_fault_policy_keys_outside_relays_are_refused(
            self, registry, dash, section, key, value):
        try:
            registry.call('set_config_param', {'section': section, 'key': key, 'value': value},
                          ctx(dash))
        except ValueError:
            pass
        assert published(dash) == []


# --------------------------------------------------------------------------
# Tools that only read
# --------------------------------------------------------------------------

OWL_STATE = {'connected': True, 'detection_enable': True, 'algorithm': 'exhsv',
             'exg_min': 25, 'exg_max': 200, 'hue_min': 30, 'invert_hue': False,
             'sensitivity_level': 'medium'}


def local_config():
    config = configparser.ConfigParser()
    config.read_dict({'System': {'algorithm': 'exhsv', 'relay_num': '4'},
                      'GreenOnBrown': {'exg_min': '99'},
                      'Sensitivity_Dusty': {'exg_min': '20'}})
    return config


class TestGetSystemStatus:
    def test_the_standalone_dashboard_reports_the_owls_last_state(self, registry, dash):
        dash.current_state = {'detection_enable': True, 'algorithm': 'exhsv', 'exg_min': 25}
        result = registry.call('get_system_status', {}, ctx(dash))
        assert result == {'status': {'detection_enable': True, 'algorithm': 'exhsv', 'exg_min': 25}}
        result['status']['algorithm'] = 'changed'
        assert dash.current_state['algorithm'] == 'exhsv', 'the tool handed out the live dict'

    def test_a_section_filters_by_key_prefix(self, registry, dash):
        dash.current_state = {'exg_min': 25, 'exg_max': 200, 'hue_min': 30}
        assert registry.call('get_system_status', {'section': 'exg'}, ctx(dash)) == {
            'status': {'exg_min': 25, 'exg_max': 200}}

    def test_the_cab_controller_reports_the_first_connected_owl_and_who_is_connected(
            self, registry, tmp_path):
        cab = central(tmp_path, owls=('owl-1', 'owl-2', 'owl-3'))
        cab.owls_state['owl-1'] = {'connected': False, 'algorithm': 'gog'}
        cab.owls_state['owl-2'].update(OWL_STATE)
        status = registry.call('get_system_status', {}, ctx(cab))['status']
        assert (status['connected_owls'], status['owl_count']) == (['owl-2', 'owl-3'], 2)
        assert (status['algorithm'], status['exg_min'], status['invert_hue']) == ('exhsv', 25, False)

    def test_no_owl_connected_is_an_empty_fleet_not_an_error(self, registry, tmp_path):
        cab = central(tmp_path, owls=())
        assert registry.call('get_system_status', {}, ctx(cab)) == {
            'status': {'connected_owls': [], 'owl_count': 0}}

    def test_no_client(self, registry):
        assert registry.call('get_system_status', {}, ctx(None)) == {'error': 'No MQTT client available'}


class TestGetConfig:
    def test_everything_a_section_and_a_key(self, registry, dash):
        context = ctx(dash, config=local_config())
        assert registry.call('get_config', {}, context)['config']['System'] == {
            'algorithm': 'exhsv', 'relay_num': '4'}
        assert registry.call('get_config', {'section': 'System'}, context) == {
            'section': 'System', 'values': {'algorithm': 'exhsv', 'relay_num': '4'}}
        assert registry.call('get_config', {'section': 'System', 'key': 'relay_num'}, context) == {
            'section': 'System', 'key': 'relay_num', 'value': '4'}

    def test_what_is_missing_is_named(self, registry, dash):
        context = ctx(dash, config=local_config())
        assert registry.call('get_config', {'section': 'Nope'}, context) == {
            'error': 'Section [Nope] not found'}
        assert registry.call('get_config', {'section': 'System', 'key': 'nope'}, context) == {
            'error': 'Key "nope" not found in [System]'}
        assert registry.call('get_config', {}, ctx(dash)) == {'error': 'No config available'}

    def test_the_cab_controller_answers_from_the_owls_published_config(self, registry, cab):
        """CONTROLLER.ini has no detection sections: they live on the OWLs."""
        cab.owls_state['owl-1']['config'] = {'Camera': {'rotation': '180'},
                                             'System': {'algorithm': 'gog'}}
        context = ctx(cab, config=local_config())
        assert registry.call('get_config', {'section': 'Camera', 'key': 'rotation'}, context) == {
            'section': 'Camera', 'key': 'rotation', 'value': '180'}
        # the controller's own file wins where it has the section
        assert registry.call('get_config', {'section': 'System'}, context)['values']['algorithm'] == 'exhsv'
        assert registry.call('get_config', {}, context)['config']['Camera'] == {'rotation': '180'}

    def test_greenonbrown_is_built_from_the_owls_state_when_no_config_has_it(self, registry, cab):
        cab.owls_state['owl-1'].update(OWL_STATE)
        config = configparser.ConfigParser()
        result = registry.call('get_config', {'section': 'GreenOnBrown'}, ctx(cab, config=config))
        assert result == {'section': 'GreenOnBrown', 'values': {
            'exg_min': '25', 'exg_max': '200', 'hue_min': '30', 'invert_hue': 'False'}}


class TestListPresets:
    def test_presets_come_from_the_owls_state_first(self, registry, dash):
        dash.current_state = {'sensitivity_presets': ['low', 'medium', 'high', 'dusty']}
        assert registry.call('list_presets', {}, ctx(dash, config=local_config())) == {
            'presets': ['low', 'medium', 'high', 'dusty']}

    def test_then_from_sensitivity_sections_of_the_owls_config(self, registry, cab):
        cab.owls_state['owl-1']['config'] = {'Sensitivity_Wet': {}, 'Sensitivity_Dry': {}, 'System': {}}
        assert registry.call('list_presets', {}, ctx(cab)) == {'presets': ['Wet', 'Dry']}

    def test_then_from_the_local_config(self, registry, dash):
        assert registry.call('list_presets', {}, ctx(dash, config=local_config())) == {'presets': ['Dusty']}

    def test_none_anywhere_is_an_empty_list(self, registry, dash):
        assert registry.call('list_presets', {}, ctx(dash, config=configparser.ConfigParser())) == {
            'presets': []}


# --------------------------------------------------------------------------
# Widget tools, on a real WidgetManager
# --------------------------------------------------------------------------

SLIDER = {'id': 'min-area', 'name': 'Min area', 'type': 'range_slider', 'slot': 'widgets_tab',
          'builtin_config': {'section': 'GreenOnBrown', 'param': 'min_detection_area',
                             'min': 0, 'max': 500, 'step': 5, 'label': 'Min area', 'unit': 'px'}}


class TestWidgetTools:
    @pytest.fixture
    def widgets(self, tmp_path):
        return WidgetManager(str(tmp_path / 'widgets'))

    def test_create_list_update_remove(self, registry, widgets):
        context = ctx(None, widget_manager=widgets)

        assert registry.call('create_widget', {'spec': SLIDER}, context) == {
            'success': True, 'widget_id': 'min-area'}
        assert registry.call('list_widgets', {}, context) == {'widgets': [SLIDER]}
        assert 'data-param="min_detection_area"' in widgets.render('min-area')

        assert registry.call('update_widget', {'widget_id': 'min-area', 'updates': {'name': 'Area'}},
                             context) == {'success': True, 'widget_id': 'min-area'}
        assert WidgetManager(str(widgets.widgets_dir)).get('min-area')['name'] == 'Area'

        assert registry.call('remove_widget', {'widget_id': 'min-area'}, context) == {
            'success': True, 'message': 'Widget "min-area" removed'}
        assert registry.call('list_widgets', {}, context) == {'widgets': []}
        assert not (widgets.widgets_dir / 'min-area').exists()

    @pytest.mark.parametrize('spec, reason', [
        ({'name': 'No id', 'type': 'toggle', 'slot': 'widgets_tab'}, 'must include an "id"'),
        ('not a dict', 'must include an "id"'),
        (dict(SLIDER, id='../../etc'), 'Invalid widget id'),
        (dict(SLIDER, type='iframe'), 'invalid type "iframe"'),
        (dict(SLIDER, slot='page_header'), 'invalid slot "page_header"'),
    ])
    def test_a_bad_spec_is_refused_with_the_reason_and_installs_nothing(self, registry, widgets, spec, reason):
        result = registry.call('create_widget', {'spec': spec}, ctx(None, widget_manager=widgets))
        assert result['success'] is False
        assert reason in result['error']
        assert list(widgets.widgets_dir.iterdir()) == []

    def test_a_second_widget_with_the_same_id_is_refused(self, registry, widgets):
        context = ctx(None, widget_manager=widgets)
        registry.call('create_widget', {'spec': SLIDER}, context)
        assert registry.call('create_widget', {'spec': dict(SLIDER, name='Other')}, context) == {
            'success': False, 'error': 'Widget "min-area" already exists'}
        assert widgets.get('min-area')['name'] == 'Min area'

    def test_updating_or_removing_a_widget_that_is_not_there(self, registry, widgets):
        context = ctx(None, widget_manager=widgets)
        assert registry.call('update_widget', {'widget_id': 'ghost', 'updates': {}}, context) == {
            'success': False, 'error': 'Widget "ghost" not found'}
        assert registry.call('remove_widget', {'widget_id': 'ghost'}, context) == {
            'success': False, 'error': 'Widget "ghost" not found'}

    def test_an_update_that_makes_the_widget_invalid_is_refused_and_not_saved(self, registry, widgets):
        context = ctx(None, widget_manager=widgets)
        registry.call('create_widget', {'spec': SLIDER}, context)
        result = registry.call('update_widget', {'widget_id': 'min-area', 'updates': {'type': 'iframe'}},
                               context)
        assert result['success'] is False
        assert WidgetManager(str(widgets.widgets_dir)).get('min-area')['type'] == 'range_slider'

    def test_no_widget_manager(self, registry):
        context = ctx(None)
        assert registry.call('list_widgets', {}, context) == {'widgets': []}
        for name, arguments in [('create_widget', {'spec': SLIDER}),
                                ('remove_widget', {'widget_id': 'x'}),
                                ('update_widget', {'widget_id': 'x', 'updates': {}})]:
            assert registry.call(name, arguments, context) == {'error': 'No widget manager available'}

    @pytest.mark.parametrize('command', [
        {'action': 'set_config_section', 'value': {'section': 'Relays', 'params': {'0': '7'}}},
        {'action': 'set_config_section', 'section': 'Relays', 'params': {'0': '7'}},
        {'action': 'set_detection_mode', 'value': 2},
        {'action': 'set_detection_enable', 'value': True},
        {'action': 'toggle_detection', 'value': True},
        {'action': 'hold_spray', 'value': 1500},
        {'action': 'test_nozzles', 'value': {'relays': [0], 'duration_ms': 10000}},
        {'action': 'shutdown'}, {'action': 'reboot'}, {'action': 'restart_service'},
        {'action': 'install_algorithm', 'name': 'x', 'code': 'import os'},
        {'action': 'set_config_section', 'section': 'System', 'params': {'on_error': 'on'}},
        {'action': 'set_config', 'section': 'System', 'key': 'relay_num', 'value': '8'},
        {'action': 'set_config_section', 'section': 'gps', 'params': {'source': 'none'}},
        {}, 'not json', None,
    ])
    def test_a_button_cannot_carry_a_command_the_agent_itself_may_not_send(self, registry, widgets, command):
        spec = {'id': 'handy', 'name': 'Handy', 'type': 'button', 'slot': 'dashboard_controls',
                'label': 'Tidy up', 'mqtt_command': command}
        result = registry.call('create_widget', {'spec': spec}, ctx(None, widget_manager=widgets))
        assert result['success'] is False
        assert widgets.scan() == []

    @pytest.mark.parametrize('wtype, fields', [
        ('range_slider', {'config_section': 'System', 'config_key': 'relay_num'}),
        ('range_slider', {'builtin_config': {'section': 'Relays', 'param': '0'}}),
        ('toggle', {'action_params': {'section': 'Controller', 'param': 'controller_type'}}),
        ('select', {'config_section': 'System', 'config_key': 'on_error', 'options': ['on', 'off']}),
        ('select', {'action': 'shutdown', 'options': ['now']}),
        ('dual_range_slider', {'config_section': 'GPS', 'action_params': {'key_low': 'port', 'key_high': 'baudrate'}}),
        ('preset_button', {'builtin_config': {'preset_name': 'p', 'values': {'relay_num': 8}}}),
    ])
    def test_no_other_widget_type_reaches_a_protected_setting_either(self, registry, widgets, wtype, fields):
        spec = dict({'id': 'w', 'name': 'W', 'type': wtype, 'slot': 'widgets_tab'}, **fields)
        result = registry.call('create_widget', {'spec': spec}, ctx(None, widget_manager=widgets))
        assert result['success'] is False and 'protected' in result['error'] or 'cannot send' in result['error']
        assert widgets.scan() == []

    def test_an_update_cannot_turn_a_harmless_widget_into_a_forbidden_one(self, registry, widgets):
        spec = {'id': 'ok', 'name': 'OK', 'type': 'button', 'slot': 'widgets_tab',
                'mqtt_command': {'action': 'set_sensitivity', 'value': 'high'}}
        assert registry.call('create_widget', {'spec': spec}, ctx(None, widget_manager=widgets))['success']
        result = registry.call('update_widget', {'widget_id': 'ok', 'updates': {
            'mqtt_command': {'action': 'set_detection_mode', 'value': 2}}}, ctx(None, widget_manager=widgets))
        assert result['success'] is False
        assert widgets.get('ok')['mqtt_command'] == {'action': 'set_sensitivity', 'value': 'high'}

    @pytest.mark.parametrize('command', [
        {'action': 'set_sensitivity', 'value': 'high'}, {'action': 'set_algorithm', 'value': 'exhsv'},
        {'action': 'set_tracking', 'value': True},
        {'action': 'set_config_section', 'section': 'GreenOnBrown', 'params': {'exg_min': '30'}},
    ])
    def test_a_detection_tuning_button_is_allowed(self, registry, widgets, command):
        spec = {'id': 'tune', 'name': 'Tune', 'type': 'button', 'slot': 'widgets_tab', 'mqtt_command': command}
        assert registry.call('create_widget', {'spec': spec}, ctx(None, widget_manager=widgets)) == {
            'success': True, 'widget_id': 'tune'}



# --------------------------------------------------------------------------
# Custom algorithm tools
# --------------------------------------------------------------------------
# custom_algorithms/ is not in the repository (gitignored on purpose: its
# validator can be bypassed and install_algorithm accepts code over MQTT), so
# on an installed OWL and in CI these five tools find no module. That is the
# first class. The second gives them a stand-in module over a temp directory,
# to prove what the tools do with it: the 13 tests here and the 51 in
# test_custom_algorithms.py that needed the real module never ran anywhere.

ALGORITHM_TOOLS = [('list_custom_algorithms', {}),
                   ('create_algorithm', {'name': 'purple', 'code': 'def purple(image): return image'}),
                   ('run_algorithm_test', {'name': 'purple'}),
                   ('deploy_algorithm', {'name': 'purple'}),
                   ('delete_algorithm', {'name': 'purple'})]


class TestCustomAlgorithmToolsAsShipped:
    @pytest.mark.parametrize('name, arguments', ALGORITHM_TOOLS)
    def test_without_the_module_each_tool_fails_cleanly_and_sends_nothing(
            self, registry, mqtt_client, monkeypatch, name, arguments):
        monkeypatch.setitem(sys.modules, 'custom_algorithms', None)      # import raises
        result = registry.call(name, arguments, ctx(mqtt_client))
        assert 'custom_algorithms' in result['error']
        assert result.get('success', False) is False
        assert result.get('algorithms', []) == []
        assert published(mqtt_client) == []


GREEN_CODE = ('import cv2\n'
              'import numpy as np\n'
              'def purple(image):\n'
              '    b, g, r = cv2.split(image.astype(np.int16))\n'
              '    return np.clip(2 * g - r - b, 0, 255).astype(np.uint8)\n')


@pytest.fixture
def algorithm_store(tmp_path, monkeypatch):
    """A stand-in custom_algorithms module: plain files in tmp_path, the
    functions the tools import, no validation of its own."""
    store = tmp_path / 'custom_algorithms'
    store.mkdir()
    module = types.ModuleType('custom_algorithms')

    def save_algorithm(name, code, description=''):
        (store / f'{name}.py').write_text(code)
        (store / f'{name}.txt').write_text(description)
        return {'success': True, 'name': name}

    def get_algorithm_code(name):
        path = store / f'{name}.py'
        return path.read_text() if path.exists() else None

    def load_custom_algorithm(name):
        code = get_algorithm_code(name)
        if code is None:
            return None
        scope = {}
        exec(compile(code, name, 'exec'), scope)
        return scope[name]

    def list_algorithms():
        return [{'name': p.stem, 'description': p.with_suffix('.txt').read_text()}
                for p in sorted(store.glob('*.py'))]

    def delete_algorithm(name):
        path = store / f'{name}.py'
        if not path.exists():
            return {'success': False, 'error': f'Algorithm "{name}" not found'}
        path.unlink()
        return {'success': True}

    for function in (save_algorithm, get_algorithm_code, load_custom_algorithm, list_algorithms,
                     delete_algorithm):
        setattr(module, function.__name__, function)
    monkeypatch.setitem(sys.modules, 'custom_algorithms', module)
    return store


class TestCustomAlgorithmToolsWithAStore:
    def test_create_list_delete(self, registry, algorithm_store):
        assert registry.call('create_algorithm',
                             {'name': 'purple', 'code': GREEN_CODE, 'description': 'finds purple'},
                             ctx(None)) == {'success': True, 'name': 'purple'}
        assert (algorithm_store / 'purple.py').read_text() == GREEN_CODE
        assert registry.call('list_custom_algorithms', {}, ctx(None)) == {
            'algorithms': [{'name': 'purple', 'description': 'finds purple'}]}
        assert registry.call('delete_algorithm', {'name': 'purple'}, ctx(None)) == {'success': True}
        assert registry.call('list_custom_algorithms', {}, ctx(None)) == {'algorithms': []}
        assert registry.call('delete_algorithm', {'name': 'purple'}, ctx(None))['success'] is False

    def test_the_test_run_counts_the_four_weeds_on_its_own_image(self, registry, algorithm_store):
        registry.call('create_algorithm', {'name': 'purple', 'code': GREEN_CODE}, ctx(None))
        result = registry.call('run_algorithm_test', {'name': 'purple'}, ctx(None))
        assert (result['success'], result['name'], result['output_shape'], result['detection_count'],
                result['pre_thresholded']) == (True, 'purple', [480, 640], 4, False)
        assert 0 < result['timing_ms'] < 2000

    @pytest.mark.parametrize('body, error', [
        ('    return "not an image"\n', 'must return a numpy array'),
        ('    return image\n', 'Expected 2D output, got 3D'),
        ('    raise RuntimeError("bad maths")\n', 'bad maths'),
    ])
    def test_a_test_run_reports_what_is_wrong_with_the_algorithm(self, registry, algorithm_store, body, error):
        registry.call('create_algorithm', {'name': 'purple', 'code': 'def purple(image):\n' + body},
                      ctx(None))
        result = registry.call('run_algorithm_test', {'name': 'purple'}, ctx(None))
        assert result['success'] is False
        assert error in result['error']

    def test_a_pre_thresholded_result_is_counted_as_it_is(self, registry, algorithm_store):
        code = GREEN_CODE.replace('def purple(image):', 'def purple(image, params):').replace(
            'return np.clip(2 * g - r - b, 0, 255).astype(np.uint8)',
            "return (np.where(2 * g - r - b > params['exg_min'], 255, 0).astype(np.uint8), True)")
        registry.call('create_algorithm', {'name': 'purple', 'code': code}, ctx(None))
        result = registry.call('run_algorithm_test', {'name': 'purple'}, ctx(None))
        assert (result['detection_count'], result['pre_thresholded']) == (4, True)

    def test_deploy_sends_the_stored_code_to_the_owls(self, registry, algorithm_store, mqtt_client):
        registry.call('create_algorithm', {'name': 'purple', 'code': GREEN_CODE}, ctx(None))
        result = registry.call('deploy_algorithm', {'name': 'purple'}, ctx(mqtt_client))
        assert result['success'] is True
        expected = {'action': 'install_algorithm', 'name': 'purple', 'code': GREEN_CODE}
        if is_cab(mqtt_client):
            expected['description'] = ''
        assert commands(mqtt_client) == [expected]

    def test_what_is_not_stored_cannot_be_tested_or_deployed(self, registry, algorithm_store, mqtt_client):
        assert registry.call('run_algorithm_test', {'name': 'ghost'}, ctx(None)) == {
            'success': False, 'error': 'Algorithm "ghost" not found or failed to load'}
        assert registry.call('deploy_algorithm', {'name': 'ghost'}, ctx(mqtt_client)) == {
            'success': False, 'error': 'Algorithm "ghost" not found'}
        assert published(mqtt_client) == []

    def test_deploy_without_a_client(self, registry, algorithm_store):
        registry.call('create_algorithm', {'name': 'purple', 'code': GREEN_CODE}, ctx(None))
        assert registry.call('deploy_algorithm', {'name': 'purple'}, ctx(None)) == {
            'error': 'No MQTT client available'}
