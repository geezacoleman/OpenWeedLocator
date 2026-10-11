"""Producer <-> consumer contract: every command a controller, the agent or
the browser sends must have a handler on the OWL.

A command with no handler is the silent kind of failure: the dashboard says
"sent", the OWL logs "unknown action" (or nothing) and nothing happens.
This reads the senders' source for action names and checks each against
the OWL dispatch in utils/mqtt_manager.py.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SENDER_DIRS = ['controller', 'agent']
SENDER_PATTERNS = [
    r"""['"]action['"]\s*:\s*['"]([a-z_]+)['"]""",       # {'action': 'x'}
    r"""\baction\s*:\s*['"]([a-z_]+)['"]""",              # JS {action: 'x'}
    r"""_send_command\(\s*['"]([a-z_]+)['"]""",           # client._send_command('x', ...)
    r"""send_?[cC]ommand\([^,()]+,\s*['"]([a-z_]+)['"]""",  # send_command(id, 'x') / sendCommand(id, 'x')
]

# Translated by the controller before anything reaches the OWL
# (networked.send_command / _send_command, DashMQTTSubscriber._send_command)
CONTROLLER_ALIASES = {'toggle_detection', 'toggle_recording', 'toggle_all_nozzles',
                      'set_sensitivity'}
# Handled by a controller itself, never forwarded to an OWL
CONTROLLER_HANDLED = {'upload_request_ack'}


def owl_handled_actions():
    src = (ROOT / 'utils' / 'mqtt_manager.py').read_text(encoding='utf-8')
    return set(re.findall(r"action == '([a-z_]+)'", src))


def sent_actions():
    found = {}
    for folder in SENDER_DIRS:
        for path in (ROOT / folder).rglob('*'):
            if path.suffix not in ('.py', '.js') or 'node_modules' in path.parts:
                continue
            text = path.read_text(encoding='utf-8', errors='ignore')
            for pattern in SENDER_PATTERNS:
                for action in re.findall(pattern, text):
                    found.setdefault(action, path.relative_to(ROOT).as_posix())
    return found


def test_every_sent_command_has_an_owl_handler():
    handled = owl_handled_actions() | CONTROLLER_ALIASES | CONTROLLER_HANDLED
    missing = {action: where for action, where in sent_actions().items()
               if action not in handled}
    assert not missing, f'commands sent with no OWL handler: {missing}'


def test_the_scan_finds_the_main_commands():
    """Guard the guard: if the patterns stop matching, the test above would
    pass vacuously."""
    assert {'set_config_section', 'toggle_detection', 'set_detection_mode',
            'calibrate_awb', 'save_config'} <= set(sent_actions())
