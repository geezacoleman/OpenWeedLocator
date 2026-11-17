"""Shared by the networked-controller tests: a REAL CentralController with
only the MQTT client replaced, the Flask app pointed at it, and a reader for
what it published."""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def make_controller(tmp_path):
    """make_controller(ini_text='', owls=('owl-1',)) -> CentralController.

    The controller is built from its own CONTROLLER.ini in tmp_path. Its
    broker client is a mock that accepts every publish; each OWL named is
    connected and was heard from just now."""
    from controller.networked.networked import CentralController

    def build(ini_text='', owls=('owl-1',)):
        ini = tmp_path / 'CONTROLLER.ini'
        ini.write_text(ini_text)
        ctrl = CentralController(config_file=str(ini))
        ctrl.mqtt_connected = True
        ctrl.mqtt_client = MagicMock()
        ctrl.mqtt_client.publish.return_value = MagicMock(rc=0)
        now = time.time()
        ctrl.owls_state = {oid: {'device_id': oid, 'connected': True, 'last_seen': now}
                           for oid in owls}
        return ctrl

    return build


@pytest.fixture
def client_for(monkeypatch):
    """client_for(ctrl) -> Flask test client whose routes use that controller."""
    import controller.networked.networked as net_mod

    def build(ctrl):
        monkeypatch.setattr(net_mod, 'controller', ctrl)
        net_mod.app.config['TESTING'] = True
        return net_mod.app.test_client()

    return build


def published(ctrl, oid=None, kind='commands'):
    """What the controller published, in order.

    published(ctrl)            -> [(topic, payload dict), ...] for everything
    published(ctrl, 'owl-1')   -> [payload dict, ...] sent to owl/owl-1/commands
    """
    out = []
    for call in ctrl.mqtt_client.publish.call_args_list:
        topic, payload = call.args[0], json.loads(call.args[1])
        if oid is None:
            out.append((topic, payload))
        elif topic == f'owl/{oid}/{kind}':
            out.append(payload)
    return out


@pytest.fixture
def sent():
    return published


def mqtt_message(topic, payload):
    """An incoming broker message, as paho hands it to _on_message."""
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return SimpleNamespace(topic=topic, payload=raw)


@pytest.fixture
def hears():
    """hears(ctrl, 'owl-1', {...state}, kind='state'): the controller receives
    that message from the broker, through its real _on_message."""
    def deliver(ctrl, oid, payload, kind='state'):
        ctrl._on_message(None, None, mqtt_message(f'owl/{oid}/{kind}', payload))
    return deliver
