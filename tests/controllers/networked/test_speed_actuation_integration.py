"""GPS speed -> spray timing on the networked controller, end to end:

    NMEA (as a receiver sends it) -> GPSManager / GPSState -> SpeedAverager
      -> ActuationCalculator -> the set_actuation_params message each OWL gets

Every pipeline test drives a real CentralController with a real GPSManager and
reads what CentralController._actuation_broadcast_once() publishes. Nothing
here re-implements the loop or the formula. What an OWL's relay then does
with that message is tests/owl/test_controller_to_owl_relay.py.
"""

import json
import socket
import time

import pytest

from utils.gps_manager import GPSState, KNOTS_TO_KMH, parse_sentence


# ---------------------------------------------------------------------------
# NMEA as a receiver sends it (valid checksums)
# ---------------------------------------------------------------------------

def _nmea(body):
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f'${body}*{checksum:02X}\r\n'


def rmc(speed_kmh, status='A'):
    knots = speed_kmh / KNOTS_TO_KMH
    return _nmea(f'GPRMC,120000.00,{status},3351.8900,S,15110.5300,E,{knots:.2f},045.0,200226,,,A')


def vtg(speed_kmh):
    return _nmea(f'GPVTG,045.0,T,,M,{speed_kmh / KNOTS_TO_KMH:.2f},N,{speed_kmh:.1f},K,A')


def gga(fix_quality=1):
    return _nmea(f'GPGGA,120000.00,3351.8900,S,15110.5300,E,{fix_quality},08,1.2,120.0,M,0.0,M,,')


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('speed_kmh', [0.9, 10.0, 25.0])
def test_rmc_speed_is_parsed_in_knots(speed_kmh):
    parsed = parse_sentence(rmc(speed_kmh))
    assert parsed['type'] == 'RMC'
    assert parsed['status'] == 'A'
    assert parsed['speed_knots'] * KNOTS_TO_KMH == pytest.approx(speed_kmh, abs=0.01)


def test_vtg_speed_is_parsed_in_kmh():
    parsed = parse_sentence(vtg(18.5))
    assert (parsed['type'], parsed['speed_kmh']) == ('VTG', 18.5)


def test_gga_carries_the_fix_and_position():
    parsed = parse_sentence(gga())
    assert (parsed['type'], parsed['fix_quality']) == ('GGA', 1)
    assert parsed['lat'] == pytest.approx(-33.8648, abs=0.0001)
    assert parsed['lon'] == pytest.approx(151.1755, abs=0.0001)


def test_a_corrupted_sentence_is_dropped():
    good = rmc(10.0)
    assert parse_sentence(good.replace('S,151', 'N,151')) is None, 'checksum no longer matches'


# ---------------------------------------------------------------------------
# GPSState: which speeds are allowed out (the controller times every nozzle
# on the rig from this value)
# ---------------------------------------------------------------------------

class TestGPSStateSpeed:

    def _state(self, *sentences):
        state = GPSState()
        for sentence in sentences:
            state.update_from_line(sentence.strip())
        return state

    def test_rmc_speed_with_a_fix_is_reported(self):
        snap = self._state(gga(), rmc(12.0)).get_dict()
        assert snap['fix_valid'] is True
        assert snap['speed_kmh'] == 12.0

    def test_vtg_speed_with_a_fix_is_reported(self):
        assert self._state(gga(), vtg(18.5)).get_dict()['speed_kmh'] == 18.5

    def test_speed_without_a_fix_is_not_reported(self):
        # A speed with no valid fix used to reach the controller, which timed
        # every nozzle on the rig from it
        snap = self._state(vtg(18.5)).get_dict()
        assert (snap['fix_valid'], snap['speed_kmh']) == (False, None)

    def test_void_rmc_does_not_set_speed(self):
        """A void RMC (status V) can still carry the receiver's last speed."""
        assert self._state(gga(), rmc(30.0, status='V')).get_dict()['speed_kmh'] is None

    def test_void_rmc_does_not_replace_a_good_speed(self):
        assert self._state(gga(), rmc(8.0), rmc(30.0, status='V')).get_dict()['speed_kmh'] == 8.0

    @pytest.mark.parametrize('age_s, reported', [(2.5, 18.5), (3.5, None)])
    def test_a_speed_older_than_3s_is_not_reported(self, age_s, reported):
        state = self._state(gga(), vtg(18.5))
        state.last_speed_time = time.monotonic() - age_s
        assert state.get_dict()['speed_kmh'] == reported

    @pytest.mark.parametrize('age_s, valid', [(9.0, True), (11.0, False)])
    def test_a_fix_older_than_10s_is_no_fix_and_carries_no_speed(self, age_s, valid):
        state = self._state(gga(), rmc(10.0))
        state.last_fix_time = time.monotonic() - age_s
        state.last_speed_time = time.monotonic()      # speed alone still arriving
        snap = state.get_dict()
        assert snap['fix_valid'] is valid
        assert snap['speed_kmh'] == (10.0 if valid else None)


# ---------------------------------------------------------------------------
# The timing maths: one table, worked by hand
# ---------------------------------------------------------------------------

# speed km/h, spray length cm, nozzle offset cm -> duration s, delay s
#   duration = length / speed, delay = offset / speed, both clamped to
#   0.01 .. 5 s (delay 0 .. 5 s). 10 km/h = 2.778 m/s; 7.2 km/h = 2 m/s;
#   18 km/h = 5 m/s; 36 km/h = 10 m/s.
WORKED = [
    (10.0, 10, 30, 0.036, 0.108),    # the default geometry at a typical speed
    (2.0, 10, 30, 0.18, 0.54),       # the slowest speed that counts as moving
    (7.2, 50, 100, 0.25, 0.5),
    (20.0, 20, 40, 0.036, 0.072),
    (18.0, 10, 0, 0.02, 0.0),        # nozzle under the camera: no delay
    (36.0, 5, 200, 0.01, 0.2),       # 5 ms needed: held at the 10 ms a solenoid needs
    (18.0, 2, 150, 0.01, 0.3),       # 4 ms needed: same floor
]


@pytest.mark.parametrize('speed, length_cm, offset_cm, duration, delay', WORKED)
def test_timing_for_a_speed_and_geometry(speed, length_cm, offset_cm, duration, delay):
    from utils.actuation import ActuationCalculator
    calc = ActuationCalculator(actuation_length_cm=length_cm, offset_cm=offset_cm, sustain_s=0)
    result = calc.compute(speed)
    assert result['source'] == 'gps'
    assert result['actuation_duration'] == pytest.approx(duration, abs=0.0005)
    assert result['delay'] == pytest.approx(delay, abs=0.0005)
    assert result['speed_used'] == speed


def test_duration_and_delay_never_exceed_5s_however_slow():
    """Only reachable with the moving threshold lowered: 0.18 km/h = 0.05 m/s
    would be a 10 s spray, 20 s late."""
    from utils.actuation import ActuationCalculator
    calc = ActuationCalculator(actuation_length_cm=50, offset_cm=100, min_speed_kmh=0.1, sustain_s=0)
    result = calc.compute(0.18)
    assert (result['actuation_duration'], result['delay'], result['source']) == (5.0, 5.0, 'gps')


# ---------------------------------------------------------------------------
# The pipeline, through a real controller
# ---------------------------------------------------------------------------

def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


@pytest.fixture
def clock(monkeypatch):
    """Monotonic time stepped by hand: GPS staleness, the speed window and the
    2 s moving gate all read it."""
    now = [9000.0]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    return now


@pytest.fixture
def ctrl(make_controller, tmp_path, clock):
    """A controller with its own TCP GPS (not listening until a test starts
    it) and two OWLs, one of them offline."""
    ctrl = make_controller(
        f'[GPS]\nsource = tcp\nnmea_port = {free_port()}\n'
        f'track_save_directory = {tmp_path / "tracks"}\n', owls=('owl-1', 'owl-2'))
    ctrl.owls_state['owl-2']['connected'] = False
    assert ctrl.gps_manager is not None
    yield ctrl
    ctrl.gps_manager.stop()


def broadcast(ctrl):
    """One pass of the broadcast loop. Returns what owl-1 was sent."""
    ctrl.mqtt_client.publish.reset_mock()
    ctrl._actuation_broadcast_once()
    sent = [json.loads(c.args[1]) for c in ctrl.mqtt_client.publish.call_args_list
            if c.args[0] == 'owl/owl-1/commands']
    assert len(sent) == 1
    assert not [c for c in ctrl.mqtt_client.publish.call_args_list
                if c.args[0].startswith('owl/owl-2/')], 'an offline OWL is sent nothing'
    return sent[0]


def receiver_says(ctrl, clock, *sentences):
    """A second passes and the receiver delivers these lines."""
    clock[0] += 1.0
    for sentence in sentences:
        ctrl.gps_manager._on_line(sentence.strip())


CONFIGURED = {'actuation_duration': 0.15, 'delay': 0.0, 'source': 'config'}


def timing(message):
    return {k: message[k] for k in ('actuation_duration', 'delay', 'source')}


def test_driving_at_10kmh_sends_the_travel_time_once_it_is_sustained(ctrl, clock):
    receiver_says(ctrl, clock, gga(), rmc(10.0))
    assert timing(broadcast(ctrl)) == CONFIGURED, 'first reading: not yet trusted as travel'
    receiver_says(ctrl, clock, gga(), rmc(10.0))
    assert timing(broadcast(ctrl)) == CONFIGURED
    receiver_says(ctrl, clock, gga(), rmc(10.0))

    message = broadcast(ctrl)
    assert message == {
        'action': 'set_actuation_params',
        'actuation_duration': 0.036,        # 10 cm at 10 km/h
        'delay': 0.108,                     # 30 cm at 10 km/h
        'source': 'gps',
        'actuation_length_cm': 10.0, 'offset_cm': 30.0, 'offset_configured': False,
        'speed_kmh': 10.0}
    assert ctrl._actuation_state['gps_status'] == 'active'


def test_speed_is_averaged_over_the_window_not_taken_from_the_last_reading(ctrl, clock):
    for _ in range(3):
        receiver_says(ctrl, clock, gga(), rmc(10.0))
        broadcast(ctrl)
    receiver_says(ctrl, clock, gga(), rmc(16.0))
    message = broadcast(ctrl)
    # 2 s window holds the readings at t, t-1 and t-2: (10 + 10 + 16) / 3 = 12
    assert message['speed_kmh'] == 12.0
    assert message['delay'] == pytest.approx(0.09, abs=0.0005)     # 30 cm at 12 km/h


def test_a_parked_receiver_wandering_below_2kmh_sends_the_configured_timing(ctrl, clock):
    for speed in [0.5, 0.9, 1.4, 0.6, 1.9, 0.8]:
        receiver_says(ctrl, clock, gga(), rmc(speed))
        message = broadcast(ctrl)
        assert timing(message) == CONFIGURED, f'at {speed} km/h'


def test_speed_on_a_void_fix_never_times_the_boom(ctrl, clock):
    """Antenna unplugged: the receiver keeps repeating its last speed in void
    RMC sentences and reports no fix."""
    for _ in range(5):
        receiver_says(ctrl, clock, gga(fix_quality=0), rmc(10.0, status='V'))
        message = broadcast(ctrl)
        assert timing(message) == CONFIGURED
        assert message['speed_kmh'] is None
    assert ctrl._actuation_state['gps_status'] == 'no_gps'


def test_receiver_gone_quiet_while_driving_reverts_to_configured_timing(ctrl, clock):
    from utils.actuation import GPS_STALE_AFTER_S
    for _ in range(3):
        receiver_says(ctrl, clock, gga(), rmc(10.0))
        broadcast(ctrl)

    clock[0] += 4.0                                   # nothing for 4 s: the speed is stale
    assert ctrl.gps_manager.get_state()['fix']['speed_kmh'] is None
    message = broadcast(ctrl)
    assert (message['delay'], message['source']) == (0.108, 'gps'), 'last good average, briefly'
    assert ctrl._actuation_state['gps_status'] == 'stale'

    clock[0] += GPS_STALE_AFTER_S - 5.0               # 29 s since the last reading
    assert broadcast(ctrl)['delay'] == 0.108

    clock[0] += 2.0                                   # 31 s
    message = broadcast(ctrl)
    assert timing(message) == CONFIGURED
    assert message['speed_kmh'] is None
    assert ctrl._actuation_state['gps_status'] == 'no_gps'


def test_stopping_hands_timing_back_to_the_configured_values(ctrl, clock):
    for _ in range(3):
        receiver_says(ctrl, clock, gga(), rmc(10.0))
        message = broadcast(ctrl)
    assert message['source'] == 'gps'
    for speed in [0.4, 0.3, 0.6, 0.2]:
        receiver_says(ctrl, clock, gga(), rmc(speed))
        message = broadcast(ctrl)
    assert timing(message) == CONFIGURED


def test_loop_time_reported_by_the_owls_stretches_the_spray(ctrl, clock, hears):
    """10 km/h with a 50 ms detection loop covers 13.9 cm between frames: a
    10 cm patch would leave strips unsprayed, so the spray is 50 ms long."""
    hears(ctrl, 'owl-1', {'avg_loop_time_ms': 50.0})
    for _ in range(3):
        receiver_says(ctrl, clock, gga(), rmc(10.0))
        broadcast(ctrl)
    message = broadcast(ctrl)
    assert message['actuation_duration'] == pytest.approx(0.05, abs=0.0005)
    assert message['delay'] == 0.108


def test_actuation_section_of_the_ini_sets_the_window_and_fallbacks(make_controller, clock):
    ctrl = make_controller(
        '[Actuation]\nspeed_avg_window = 6\nactuation_duration = 0.3\ndelay = 0.05\n'
        'min_speed_kmh = 4\noffset_cm = 60\nactuation_length_cm = 20\n')
    assert ctrl.speed_averager.window_seconds == 6.0
    ctrl.mqtt_client.publish.reset_mock()
    ctrl._actuation_broadcast_once()
    message = json.loads(ctrl.mqtt_client.publish.call_args_list[0].args[1])
    assert message == {'action': 'set_actuation_params', 'actuation_duration': 0.3, 'delay': 0.05,
                       'source': 'config', 'actuation_length_cm': 20.0, 'offset_cm': 60.0,
                       'offset_configured': True, 'speed_kmh': None}
    assert make_controller('').speed_averager.window_seconds == 2.0


def test_broker_down_computes_but_publishes_nothing(ctrl, clock):
    ctrl.mqtt_connected = False
    receiver_says(ctrl, clock, gga(), rmc(10.0))
    ctrl._actuation_broadcast_once()
    assert ctrl.mqtt_client.publish.call_args_list == []
    assert ctrl._actuation_state['speed_kmh'] == 10.0


def test_the_loop_survives_a_gps_error_and_keeps_broadcasting(ctrl, monkeypatch):
    """_actuation_broadcast_loop: a failing pass backs off 2 s and the next
    pass still reaches the OWLs."""
    import controller.networked.networked as net_mod

    class Done(BaseException):
        pass

    sleeps = []

    class Clock:
        def __getattr__(self, name):
            return getattr(time, name)

        def sleep(self, seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise Done

    real = ctrl.gps_manager.get_state
    calls = []

    def fails_once():
        calls.append(1)
        if len(calls) == 1:
            raise OSError('serial port vanished')
        return real()

    monkeypatch.setattr(ctrl.gps_manager, 'get_state', fails_once)
    monkeypatch.setattr(net_mod, 'time', Clock())
    with pytest.raises(Done):
        ctrl._actuation_broadcast_loop()

    assert sleeps == [2.0, 1.0]
    sent = [json.loads(c.args[1]) for c in ctrl.mqtt_client.publish.call_args_list
            if c.args[0] == 'owl/owl-1/commands']
    assert [timing(m) for m in sent] == [CONFIGURED]


# ---------------------------------------------------------------------------
# The same, with the NMEA arriving over TCP as the Teltonika sends it
# ---------------------------------------------------------------------------

def test_nmea_over_tcp_reaches_the_owls_as_spray_timing(ctrl, clock):
    gps = ctrl.gps_manager
    gps.start()

    deadline = time.time() + 5
    while True:
        try:
            feed = socket.create_connection(('127.0.0.1', gps.port), timeout=1)
            break
        except OSError:
            assert time.time() < deadline, 'the GPS TCP server never started listening'
            time.sleep(0.02)

    def router_sends(speed_kmh):
        clock[0] += 1.0
        feed.sendall((gga() + rmc(speed_kmh)).encode('ascii'))
        deadline = time.time() + 5
        while gps.state.last_speed_time != clock[0]:
            assert time.time() < deadline, 'the sentence never reached GPSState'
            time.sleep(0.005)

    try:
        router_sends(10.0)
        assert gps.get_state()['connection'] == {
            'gps_connected': True, 'gps_enabled': True, 'source': 'tcp'}
        assert timing(broadcast(ctrl)) == CONFIGURED
        router_sends(10.0)
        broadcast(ctrl)
        router_sends(10.0)
        message = broadcast(ctrl)
        assert (message['actuation_duration'], message['delay'], message['source'],
                message['speed_kmh']) == (0.036, 0.108, 'gps', 10.0)

        # the position goes out too, for image EXIF
        [position] = [json.loads(c.args[1]) for c in ctrl.mqtt_client.publish.call_args_list
                      if c.args[0] == 'owl/owl-1/gps']
        assert position['latitude'] == pytest.approx(-33.8648, abs=0.0001)
        assert position['speed_kmh'] == 10.0

        router_sends(20.0)
        router_sends(20.0)
        router_sends(20.0)
        message = broadcast(ctrl)
        assert (message['delay'], message['speed_kmh']) == (0.054, 20.0)   # 30 cm at 20 km/h
    finally:
        feed.close()
