"""What the agent's tools call on the in-cab controller.

A real CentralController (its own config file in tmp_path, its paho client a
recorder): current_state / owl_config, the tool-facing _send_command and
set_detection_enable, and the value coercion of /api/config/param. The
tools themselves, called through the registry on this controller, are in
test_tool_registry.py; the HTTP routes in test_agent_routes.py.
"""

import logging

import pytest

from agent_fakes import central, commands, published

SPOT = {'action': 'set_detection_enable', 'value': True}
OFF = {'action': 'set_detection_enable', 'value': False}


class TestWhatTheToolsRead:
    def test_current_state_is_the_first_connected_owl(self, tmp_path):
        cab = central(tmp_path, owls=('owl-1', 'owl-2', 'owl-3'))
        cab.owls_state['owl-1'].update(connected=False, algorithm='gog')
        cab.owls_state['owl-2'].update(algorithm='exhsv')
        cab.owls_state['owl-3'].update(algorithm='hsv')
        assert cab.current_state['algorithm'] == 'exhsv'

    def test_current_state_is_empty_with_nobody_connected(self, tmp_path):
        cab = central(tmp_path, owls=('owl-1',))
        cab.owls_state['owl-1']['connected'] = False
        assert cab.current_state == {}
        assert central(tmp_path, owls=()).current_state == {}

    def test_owl_config_is_the_first_connected_owl_that_published_one(self, tmp_path):
        cab = central(tmp_path, owls=('owl-1', 'owl-2', 'owl-3'))
        assert cab.owl_config is None
        cab.owls_state['owl-1'].update(connected=False, config={'System': {'algorithm': 'gog'}})
        cab.owls_state['owl-3']['config'] = {'System': {'algorithm': 'hsv'}}
        assert cab.owl_config == {'System': {'algorithm': 'hsv'}}


class TestDetectionEnable:
    """set_detection_enable: the audit flipped its polarity (bool -> not
    bool) and every test here still passed."""

    @pytest.mark.parametrize('value, payload, stored', [
        (True, SPOT, 'spot'), ('true', SPOT, 'spot'), (1, SPOT, 'spot'),
        (False, OFF, 'off'), ('false', OFF, 'off'), (0, OFF, 'off'), ('off', OFF, 'off'),
    ])
    def test_on_is_spot_off_is_off_and_the_screen_remembers_it(self, cab, value, payload, stored):
        result = cab.set_detection_enable(value)
        assert published(cab) == [('owl/owl-1/commands', payload)]
        assert cab.desired_state == {'all': {'spray': stored}}
        assert result == {'success': True, 'message': 'Sent to 1 OWLs', 'targets': ['owl-1']}

    @pytest.mark.parametrize('value', [None, 'maybe', 2, '', [], 'banana'])
    def test_an_unclear_value_is_refused_and_changes_nothing(self, cab, value):
        """None used to toggle, 'banana' used to switch spraying on."""
        cab.send_command('all', 'toggle_detection', False)
        before = published(cab)
        result = cab.set_detection_enable(value)
        assert (result['success'], result['refused']) == (False, True)
        assert published(cab) == before
        assert cab.desired_state == {'all': {'spray': 'off'}}


class TestSendCommand:
    """The tool-facing _send_command(action, **kwargs)."""

    @pytest.mark.parametrize('action, kwargs, payload', [
        # spray: the one command that means the same on every OWL version
        ('set_detection_mode', {'value': 2}, {'action': 'set_detection_mode', 'value': 2}),
        ('set_detection_mode', {'value': 0}, {'action': 'set_detection_enable', 'value': True}),
        ('set_detection_mode', {'value': 1}, {'action': 'set_detection_enable', 'value': False}),
        # the generic branch passes value through untouched (audit M3: dropped, unnoticed)
        ('set_algorithm', {'value': 'exhsv'}, {'action': 'set_algorithm', 'value': 'exhsv'}),
        ('set_crop_buffer', {'value': 20}, {'action': 'set_crop_buffer', 'value': 20}),
        ('get_config', {}, {'action': 'get_config', 'value': None}),
        # thresholds
        ('set_config', {'section': 'GreenOnBrown', 'key': 'exg_min', 'value': 30},
         {'action': 'set_greenonbrown_param', 'param': 'exg_min', 'value': 30}),
        ('set_config', {'key': 'hue_max', 'value': '90'},
         {'action': 'set_greenonbrown_param', 'param': 'hue_max', 'value': '90'}),
        # any other section
        ('set_config', {'section': 'Camera', 'key': 'rotation', 'value': 180},
         {'action': 'set_config_section', 'section': 'Camera', 'params': {'rotation': '180'}}),
        # raw broadcasts keep their own field names
        ('save_sensitivity_preset', {'name': 'dusty'}, {'action': 'save_sensitivity_preset', 'name': 'dusty'}),
        ('delete_sensitivity_preset', {'name': 'dusty'},
         {'action': 'delete_sensitivity_preset', 'name': 'dusty'}),
        ('install_algorithm', {'name': 'purple', 'code': 'def purple(image): ...'},
         {'action': 'install_algorithm', 'name': 'purple', 'code': 'def purple(image): ...',
          'description': ''}),
        ('install_algorithm', {'name': 'purple', 'code': 'x', 'description': 'finds purple'},
         {'action': 'install_algorithm', 'name': 'purple', 'code': 'x', 'description': 'finds purple'}),
    ])
    def test_the_message_each_owl_gets(self, tmp_path, action, kwargs, payload):
        cab = central(tmp_path, owls=('owl-1', 'owl-2', 'owl-3'))
        cab.owls_state['owl-2']['connected'] = False
        result = cab._send_command(action, **kwargs)
        assert published(cab) == [('owl/owl-1/commands', payload), ('owl/owl-3/commands', payload)]
        assert result['success'] is True
        assert result['message'] == 'Sent to 2 OWLs'

    def test_set_sensitivity_level_is_lower_cased(self, cab):
        cab.set_sensitivity_level('HIGH')
        assert commands(cab) == [{'action': 'set_sensitivity_level', 'level': 'high'}]

    @pytest.mark.parametrize('send', [
        lambda cab: cab._send_command('set_detection_mode', value=2),
        lambda cab: cab._send_command('save_sensitivity_preset', name='x'),
        lambda cab: cab._send_command('install_algorithm', name='x', code='y'),
        lambda cab: cab.set_detection_enable(True),
        lambda cab: cab.set_sensitivity_level('high'),
    ])
    def test_with_the_broker_down_nothing_is_sent_or_remembered(self, cab, send):
        cab.mqtt_connected = False
        assert send(cab) == {'success': False, 'error': 'MQTT not connected'}
        assert published(cab) == []
        assert cab.desired_state == {}

    def test_arguments_the_generic_branch_would_drop_are_logged(self, cab, caplog):
        """Only `value` survives the generic branch: say so, do not guess."""
        with caplog.at_level(logging.WARNING, logger='controller.networked.networked'):
            cab._send_command('set_crop_buffer', px=20)
        assert commands(cab) == [{'action': 'set_crop_buffer', 'value': None}]
        assert "action 'set_crop_buffer' has kwargs ['px'] that may be dropped" in caplog.text

    @pytest.mark.parametrize('value', [None, 3, -1, 'blanket', 2.9, True])
    def test_a_spray_mode_that_is_not_0_1_or_2_is_refused(self, cab, value):
        """Including the misspelt keyword above (mode=2 arrives as value None):
        refused, nothing sent, nothing recorded."""
        result = cab._send_command('set_detection_mode', value=value)
        assert result['success'] is False and result['refused'] is True
        assert commands(cab) == []
        assert cab.desired_state == {}

    def test_an_algorithm_one_owl_cannot_run_is_refused_for_the_whole_boom(self, tmp_path):
        """owl-2 has no colour table loaded: nobody is switched to lut."""
        cab = central(tmp_path, owls=('owl-1', 'owl-2'))
        cab.owls_state['owl-1']['lut_profile'] = 'ryegrass'
        result = cab._send_command('set_algorithm', value='lut')
        assert (result['success'], result['refused']) == (False, True)
        assert result['error'].startswith('owl-2: ')
        assert published(cab) == []

        cab.owls_state['owl-2']['lut_profile'] = 'ryegrass'
        assert cab._send_command('set_algorithm', value='lut')['success'] is True
        assert commands(cab) == [{'action': 'set_algorithm', 'value': 'lut'}] * 2


class TestCoerceValue:
    """/api/config/param: slider and toggle values arrive as text."""

    @pytest.mark.parametrize('given, expected', [
        ('42', 42), ('-3', -3), ('3.14', 3.14), ('true', True), ('TRUE', True), ('false', False),
        ('False', False), ('exhsv', 'exhsv'), ('', ''), (42, 42), (3.14, 3.14), (True, True),
        (False, False), (None, None),
    ])
    def test_coerce(self, given, expected):
        from controller.networked.networked import _coerce_value
        result = _coerce_value(given)
        assert result == expected
        assert type(result) is type(expected)
