"""A widget the agent made, from the chat to the message its button sends.

    chat -> create_widget tool -> WidgetManager -> /api/widgets/<id>/template
    -> (the click, as controller/shared/js/widgets.js posts it)
    -> /api/config/param -> the dashboard's MQTT client -> published message

The click is the one part done by hand here: widgets.js reads data-command,
takes `action` out of it and posts {action, ...the rest} to /api/config/param.
`press` below does the same.

The assistant may only make buttons that tune detection. Spray, power,
relay and other protected commands are refused when the button is created
(agent/tool_registry.widget_spec_refusal), on both dashboards.
"""

import html as html_lib
import json
import re

import pytest

from agent_fakes import calls, says

TOPIC = {'standalone': 'owl/commands', 'networked': 'owl/owl-1/commands'}


def install_button(app, llm, command, widget_id='agent-button'):
    """The agent is asked for a button and creates it with its tool."""
    spec = {'id': widget_id, 'name': 'Agent button', 'type': 'button', 'slot': 'dashboard_controls',
            'label': 'Press me', 'mqtt_command': command}
    app.connect(llm)
    llm.replies = [calls(('create_widget', {'spec': spec})), says('Added.')]
    events = app.ask('add a button')
    [result] = [e['data'] for e in events if e['type'] == 'tool_result']
    assert result['result'] == {'success': True, 'widget_id': widget_id}


def press(app, widget_id='agent-button'):
    """What widgets.js does when the rendered button is clicked."""
    page = app.client.get(f'/api/widgets/{widget_id}/template')
    assert page.status_code == 200
    [attribute] = re.findall(r"data-command='([^']*)'", page.get_data(as_text=True))
    command = json.loads(html_lib.unescape(attribute))
    action = command.pop('action', 'unknown')
    return app.client.post('/api/config/param', json=dict(command, action=action))


def test_an_agent_made_button_sends_its_command_as_written(agent_app, llm):
    command = {'action': 'set_tracking', 'value': True}
    install_button(agent_app, llm, command)
    assert agent_app.published() == [], 'making the button sends nothing'

    response = press(agent_app)

    assert response.get_json()['success'] is True
    assert agent_app.published() == [(TOPIC[agent_app.kind], command)]


@pytest.mark.parametrize('command', [
    {'action': 'set_detection_mode', 'value': 2},                     # blanket
    {'action': 'set_detection_mode', 'value': 0},
    {'action': 'toggle_detection', 'value': True},
    {'action': 'toggle_all_nozzles', 'value': True},
    {'action': 'hold_spray', 'value': 1500},
    {'action': 'shutdown'}, {'action': 'reboot'}, {'action': 'restart_service'},
    {'action': 'set_config_section', 'section': 'Relays', 'params': {'0': '7'}},
    {'action': 'set_config_section', 'value': {'section': 'Relays', 'params': {'0': '7'}}},
    {'action': 'set_config_section', 'section': 'System', 'params': {'on_error': 'on'}},
    {'action': 'get_config'},                                         # not on the list: refused
])
def test_the_assistant_cannot_make_a_button_for_anything_else(agent_app, llm, command):
    """Spray, power, wiring: refused when the button is asked for. Nothing
    is installed, so there is nothing on the dashboard to press."""
    spec = {'id': 'agent-button', 'name': 'Agent button', 'type': 'button',
            'slot': 'dashboard_controls', 'label': 'Press me', 'mqtt_command': command}
    agent_app.connect(llm)
    llm.replies = [calls(('create_widget', {'spec': spec})), says('I cannot add that.')]

    events = agent_app.ask('add a button')

    [result] = [e['data'] for e in events if e['type'] == 'tool_result']
    assert result['result']['success'] is False
    assert agent_app.client.get('/api/widgets').get_json() == []
    assert agent_app.client.get('/api/widgets/agent-button/template').status_code == 404
    assert agent_app.published() == []


def test_a_sensitivity_button_reaches_the_owl_as_a_preset_name(agent_app, llm):
    install_button(agent_app, llm, {'action': 'set_sensitivity', 'value': 'high'})
    press(agent_app)
    assert agent_app.commands() == [{'action': 'set_sensitivity_level', 'level': 'high'}]


def test_a_spray_button_put_on_the_unit_by_hand_obeys_the_on_off_rules(networked_app):
    """A person can still install any widget on the unit (files on disk);
    its command goes through the controller like the screen's own buttons:
    'false' is off, and the screen remembers it."""
    spec = {'id': 'agent-button', 'name': 'Stop', 'type': 'button', 'slot': 'dashboard_controls',
            'mqtt_command': {'action': 'toggle_detection', 'value': 'false'}}
    assert networked_app.widgets.install('agent-button', spec) == (True, None)
    press(networked_app)
    assert networked_app.commands() == [{'action': 'set_detection_enable', 'value': False}]
    assert networked_app.mqtt.desired_state == {'all': {'spray': 'off'}}


def test_a_widget_can_be_deleted_from_the_dashboard(agent_app, llm):
    install_button(agent_app, llm, {'action': 'set_sensitivity', 'value': 'high'})
    assert [w['id'] for w in agent_app.client.get('/api/widgets').get_json()] == ['agent-button']

    assert agent_app.client.delete('/api/widgets/agent-button').get_json() == {'status': 'ok'}

    assert agent_app.client.get('/api/widgets').get_json() == []
    assert agent_app.client.get('/api/widgets/agent-button/template').status_code == 404
    assert agent_app.client.delete('/api/widgets/agent-button').status_code == 404


def test_a_slider_the_agent_made_sets_the_parameter_it_names(agent_app, llm):
    spec = {'id': 'area', 'name': 'Min area', 'type': 'range_slider', 'slot': 'widgets_tab',
            'config_section': 'GreenOnBrown', 'config_key': 'min_detection_area', 'min': 0, 'max': 500}
    agent_app.connect(llm)
    llm.replies = [calls(('create_widget', {'spec': spec})), says('Added.')]
    agent_app.ask('add a slider')

    page = agent_app.client.get('/api/widgets/area/template').get_data(as_text=True)
    [(section, param)] = re.findall(r'data-section="([^"]*)" data-param="([^"]*)"', page)
    assert (section, param) == ('GreenOnBrown', 'min_detection_area')
    # widgets.js setParam: {param, value, section}
    response = agent_app.client.post('/api/config/param',
                                     json={'section': section, 'param': param, 'value': '120'})

    assert response.get_json()['success'] is True
    expected = ({'action': 'set_config_section', 'section': 'GreenOnBrown',
                 'params': {'min_detection_area': '120'}} if agent_app.kind == 'networked' else
                {'action': 'set_config', 'key': 'min_detection_area', 'value': 120})
    assert agent_app.commands() == [expected]


def test_a_custom_widgets_style_is_scoped_to_its_own_container(agent_app):
    """Files put on the unit by hand (the agent's tool cannot write them)."""
    spec = {'id': 'styled', 'name': 'Styled', 'type': 'custom', 'slot': 'widgets_tab'}
    assert agent_app.widgets.install('styled', spec, files={
        'template.html': '<div class="foo">Hello</div>',
        'style.css': '.foo { color: red; }',
        'script.js': 'OWLWidget.onStateUpdate(function() {});'}) == (True, None)

    style = agent_app.client.get('/api/widgets/styled/style')
    script = agent_app.client.get('/api/widgets/styled/script')

    assert style.get_data(as_text=True) == (
        '.widget-container[data-widget-id="styled"] {\n.foo { color: red; }\n}')
    assert style.mimetype == 'text/css'
    assert script.get_data(as_text=True) == (
        '(function(OWLWidget) {\nOWLWidget.onStateUpdate(function() {});\n})(window.OWLWidget);')
    assert agent_app.client.get('/api/widgets/styled/template').get_data(as_text=True).count(
        '<div class="foo">Hello</div>') == 1


def test_style_and_script_are_for_custom_widgets_only(agent_app, llm):
    install_button(agent_app, llm, {'action': 'set_sensitivity', 'value': 'high'})
    assert agent_app.client.get('/api/widgets/agent-button/style').status_code == 404
    assert agent_app.client.get('/api/widgets/agent-button/script').status_code == 404
    assert agent_app.client.get('/api/widgets/nope/style').status_code == 404
