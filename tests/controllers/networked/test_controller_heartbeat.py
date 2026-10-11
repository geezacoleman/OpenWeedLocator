"""Controller Noktura presence (heartbeat) wiring checks.

networked.py instantiates CentralController at import time (so the route tests
mock the class). We therefore verify the heartbeat wiring at the source/AST
level — the same approach the suite uses for owl.py, which also can't be
imported directly.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
NET = (ROOT / 'controller' / 'networked' / 'networked.py').read_text(encoding='utf-8')
TREE = ast.parse(NET)


@pytest.mark.unit
class TestControllerHeartbeat:
    def test_device_id_and_topics(self):
        assert "CONTROLLER_DEVICE_ID = 'owl-controller'" in NET
        assert "CONTROLLER_STATUS_TOPIC = f'owl/{CONTROLLER_DEVICE_ID}/status'" in NET
        # Slim cloud state lives outside owl/ (cloud data budget, v3.21.0)
        assert "CONTROLLER_STATE_TOPIC = cloud_state_out_topic(True, CONTROLLER_DEVICE_ID)" in NET

    def test_lwt_set_before_connect(self):
        # Last-Will on the controller status topic (connected:False).
        assert re.search(r"will_set\(\s*CONTROLLER_STATUS_TOPIC", NET)

    def test_presence_method_defined(self):
        names = {n.name for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)}
        assert '_publish_controller_presence' in names

    def test_presence_publishes_status_and_state(self):
        # Status retained + connected; state carries type=controller + version.
        assert 'CONTROLLER_STATUS_TOPIC' in NET and 'CONTROLLER_STATE_TOPIC' in NET
        assert "'type': 'controller'" in NET
        assert "'version': CONTROLLER_VERSION" in NET
        assert 'retain=True' in NET

    def test_status_not_republished_every_cycle(self):
        # Retained status goes out once per connect (bridged at QoS 1, so a
        # 2 s republish cost 4G data); the periodic presence is state only.
        fn = next(n for n in ast.walk(TREE)
                  if isinstance(n, ast.FunctionDef) and n.name == '_publish_controller_presence')
        assert 'CONTROLLER_STATUS_TOPIC' not in ast.get_source_segment(NET, fn)
        on_connect = next(n for n in ast.walk(TREE)
                          if isinstance(n, ast.FunctionDef) and n.name == '_on_connect')
        assert 'CONTROLLER_STATUS_TOPIC' in ast.get_source_segment(NET, on_connect)

    def test_on_connect_publishes_presence(self):
        assert 'self._publish_controller_presence()' in NET

    def test_periodic_refresh(self):
        # Refreshed in the connection-checker loop while connected.
        assert re.search(r"if self\.mqtt_connected:\s*\n\s*self\._publish_controller_presence\(\)", NET)

    def test_self_presence_not_treated_as_owl(self):
        # The controller subscribes to owl/+/state|status and must ignore its own.
        assert 'if device_id == CONTROLLER_DEVICE_ID:' in NET

    def test_version_resolves(self):
        from version import VERSION
        assert str(VERSION)
