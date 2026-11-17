"""Fixtures for the agent tests. See agent_fakes.py for what is faked (the
LLM provider's HTTP API and the broker) and what is real (everything else)."""

import pytest

import agent_fakes


@pytest.fixture
def registry():
    """The registry as both dashboards build it."""
    from agent.tool_registry import ToolRegistry

    reg = ToolRegistry(developer_mode=False)
    reg.discover()
    return reg


@pytest.fixture
def llm(monkeypatch):
    """Script what the LLM provider's API answers (agent_fakes.LLMWire)."""
    import agent.llm_provider as provider_mod

    wire = agent_fakes.LLMWire()
    monkeypatch.setattr(provider_mod.httpx, 'Client', wire)
    return wire


@pytest.fixture
def dash():
    """The standalone dashboard's real MQTT client, connected."""
    return agent_fakes.dash_subscriber()


@pytest.fixture
def cab(tmp_path):
    """The in-cab controller: a real CentralController with owl-1 connected."""
    return agent_fakes.central(tmp_path)


@pytest.fixture(params=['standalone', 'networked'])
def mqtt_client(request, tmp_path):
    """Each tool-facing client in turn: what a tool does must hold on both."""
    if request.param == 'standalone':
        return agent_fakes.dash_subscriber()
    return agent_fakes.central(tmp_path)


def _fresh_engine(monkeypatch, engine, tmp_path):
    """The app's own engine, with nothing left from another test: no
    provider, no sessions, its files under tmp_path."""
    from agent.widget_manager import WidgetManager

    assert engine is not None, 'the dashboard came up without its agent engine'
    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    widgets = WidgetManager(str(tmp_path / 'widgets'))
    monkeypatch.setattr(engine, 'provider', None)
    monkeypatch.setattr(engine, '_sessions', {})
    monkeypatch.setattr(engine, 'sessions_dir', sessions)
    monkeypatch.setitem(engine.context, 'widget_manager', widgets)
    return widgets


class AgentApp:
    """A dashboard with its real agent routes, engine, registry and MQTT
    client. `client` is the Flask test client, `mqtt` the dashboard's client
    (commands(app.mqtt) is what it published), `widgets` its WidgetManager."""

    def __init__(self, kind, client, engine, mqtt, widgets, dashboard=None):
        self.kind, self.client, self.engine, self.mqtt, self.widgets = kind, client, engine, mqtt, widgets
        self.dashboard = dashboard          # standalone only: the OWLDashboard

    def connect(self, llm, provider='anthropic'):
        llm.key_check = 200
        response = self.client.post('/api/agent/connect', json={'api_key': 'sk-test', 'provider': provider})
        assert response.status_code == 200, response.get_json()
        llm.requests.clear()

    def chat(self, message='', **body):
        return self.client.post('/api/agent/chat', json=dict(body, message=message))

    def ask(self, message='', **body):
        """The SSE events of one chat request."""
        response = self.chat(message, **body)
        assert response.status_code == 200, response.get_json()
        return agent_fakes.sse_events(response)

    def published(self):
        return agent_fakes.published(self.mqtt)

    def commands(self):
        return agent_fakes.commands(self.mqtt)


@pytest.fixture
def networked_app(monkeypatch, tmp_path):
    """controller/networked/networked.py: its Flask app, its module-level
    agent engine, and a real CentralController with owl-1 connected."""
    import controller.networked.networked as net_mod

    ctrl = agent_fakes.central(tmp_path)
    monkeypatch.setattr(net_mod, 'controller', ctrl)
    engine = net_mod.agent_engine
    widgets = _fresh_engine(monkeypatch, engine, tmp_path)
    monkeypatch.setattr(net_mod, 'widget_manager', widgets)
    monkeypatch.setitem(engine.context, 'mqtt_client', ctrl)
    monkeypatch.setitem(engine.context, 'config', ctrl.config)
    net_mod.app.config['TESTING'] = True
    return AgentApp('networked', net_mod.app.test_client(), engine, ctrl, widgets)


@pytest.fixture
def standalone_app(monkeypatch, tmp_path, standalone_test_client):
    """controller/standalone/standalone.py: a real OWLDashboard (the root
    conftest's, whose config files are in a temp directory) with its own
    agent engine and its real DashMQTTSubscriber (connected, no broker)."""
    client, dashboard, _ = standalone_test_client
    dashboard.mqtt_client.connected = True
    engine = dashboard.agent_engine
    widgets = _fresh_engine(monkeypatch, engine, tmp_path)
    dashboard.widget_manager = widgets
    assert engine.context['mqtt_client'] is dashboard.mqtt_client
    return AgentApp('standalone', client, engine, dashboard.mqtt_client, widgets, dashboard=dashboard)


@pytest.fixture(params=['standalone', 'networked'])
def agent_app(request):
    """Both dashboards in turn: the agent routes are written twice."""
    return request.getfixturevalue(f'{request.param}_app')
