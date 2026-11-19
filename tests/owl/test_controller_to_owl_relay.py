"""Controller GPS speed to an OWL's relay, with nothing in between left out.

    NMEA -> the real CentralController (GPSManager, SpeedAverager,
    ActuationCalculator, _actuation_broadcast_once) -> the exact message it
    publishes -> a real Owl's MQTT command handler -> the relay pin

The broker is the only stand-in: the message the controller publishes on the
OWL's command topic is handed to the OWL's handler, and the OWL's own state
is handed to the controller as its heartbeat, once a second, as on the rig.

The controller side alone (exact payloads, stale and void fixes) is
tests/controllers/networked/test_speed_actuation_integration.py; OWL-side
handling of a hand-written timing message is test_relay_matrix.py.
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from harness import PROMPT_S

KNOTS_TO_KMH = 1.852


def _nmea(body):
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f'${body}*{checksum:02X}'


def receiver_lines(speed_kmh):
    """One second's output of the cab unit's GPS receiver."""
    return [_nmea('GPGGA,120000.00,3351.8900,S,15110.5300,E,1,08,1.2,120.0,M,0.0,M,,'),
            _nmea(f'GPRMC,120000.00,A,3351.8900,S,15110.5300,E,{speed_kmh / KNOTS_TO_KMH:.2f},'
                  f'045.0,200226,,,A')]


def cab_unit(tmp_path, actuation):
    """A real CentralController with its own GPS and the [Actuation] given."""
    from controller.networked.networked import CentralController

    # not named CONTROLLER.ini: the rig ignores files of that name so a
    # developer's own never reaches a test OWL
    ini = tmp_path / 'cab_unit.ini'
    ini.write_text('[GPS]\nsource = tcp\nnmea_port = 8500\n'
                   f'track_save_directory = {tmp_path / "tracks"}\n\n[Actuation]\n{actuation}')
    ctrl = CentralController(config_file=str(ini))
    assert ctrl.gps_manager is not None      # never started: lines are fed to it directly
    ctrl.mqtt_connected = True
    ctrl.mqtt_client = MagicMock()
    return ctrl


def one_second(ctrl, r, speed_kmh):
    """The receiver reports, the OWL's heartbeat arrives, the controller
    broadcasts, and the OWL handles what was published on its command topic.
    Returns that message."""
    for line in receiver_lines(speed_kmh):
        ctrl.gps_manager._on_line(line)

    dash = r.owl.dash
    with dash.state_lock:
        heartbeat = json.dumps(dash.state, default=str).encode()
    ctrl._on_message(None, None, SimpleNamespace(topic=dash.topics['state'], payload=heartbeat))

    ctrl.mqtt_client.publish.reset_mock()
    ctrl._actuation_broadcast_once()
    [message] = [json.loads(c.args[1]) for c in ctrl.mqtt_client.publish.call_args_list
                 if c.args[0] == dash.topics['commands']]
    fields = dict(message)
    r.command(fields.pop('action'), **fields)
    return message


def test_parked_cab_gps_wandering_sprays_at_once_for_the_controllers_duration(rig, tmp_path):
    """Field bug, 2026-11-16, controller side: a parked receiver reading
    0.5-1.5 km/h held every spray back (40 cm at 0.9 km/h is 1.6 s)."""
    ctrl = cab_unit(tmp_path, 'offset_cm = 40\nactuation_duration = 0.5\n')
    r = rig(dash='networked')

    # Three broadcasts a second apart, as the loop sends them: long enough
    # for a wandering fix to be taken as travel if the threshold let it
    for speed in [0.6, 1.4, 0.9]:
        message = one_second(ctrl, r, speed)
        assert (message['actuation_duration'], message['delay'], message['source']) == (
            0.5, 0.0, 'config'), f'at {speed} km/h'
        if speed != 0.9:
            time.sleep(1.05)
    assert r.owl.actuation_source == 'controller'

    showing = r.show_card(seconds=0.2)
    assert showing.fired
    assert showing.on < PROMPT_S, f'relay waited {showing.on:.2f} s'
    assert 0.45 <= showing.off <= 0.5 + PROMPT_S, 'on for the controller\'s 0.5 s, not the OWL\'s 0.15 s'


def test_driving_at_10kmh_with_nozzles_40cm_back_delays_the_relay_by_the_travel_time(rig, tmp_path):
    """40 cm at 10 km/h = 0.144 s; a 50 cm patch at 10 km/h = 0.18 s."""
    ctrl = cab_unit(tmp_path, 'offset_cm = 40\nactuation_length_cm = 50\n')
    r = rig(dash='networked')

    message = one_second(ctrl, r, 10.0)
    assert (message['delay'], message['source']) == (0.0, 'config'), \
        'one reading is not yet travel: no delay'
    started = time.monotonic()
    while message['source'] != 'gps':
        assert time.monotonic() - started < 4.0, 'speed-adaptive timing never engaged'
        time.sleep(1.0)
        message = one_second(ctrl, r, 10.0)
    assert time.monotonic() - started >= 2.0, 'engaged before 2 s of sustained speed'

    assert message == {
        'action': 'set_actuation_params', 'actuation_duration': 0.18, 'delay': 0.144,
        'source': 'gps', 'actuation_length_cm': 50.0, 'offset_cm': 40.0,
        'offset_configured': True, 'speed_kmh': 10.0}
    assert (r.owl.actuation_duration, r.owl.delay) == (0.18, 0.144)

    showing = r.show_card(seconds=0.2)
    assert showing.fired
    assert 0.144 - 0.04 <= showing.on <= 0.144 + PROMPT_S
    assert 0.18 - 0.05 <= showing.off - 0.144 <= 0.18 + PROMPT_S
