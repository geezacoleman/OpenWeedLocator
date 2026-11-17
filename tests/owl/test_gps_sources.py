"""
Where a real Owl gets its position from, and what it does when a source dies.

    fake serial port -> Owl._serial_gps_reader -> GPSState --+
                                                             +-> Owl._get_best_gps_data
    owl/gps (phone, browser, controller) -> dash handler ----+      -> owl.gps_data (image EXIF,
                                                                       session track), the GPS LED
                                                                       and the dashboard's GPS state

Until now this was "tested" by tests/unit/test_serial_gps.py re-typing the
if/else of _get_best_gps_data and a copy of the reader loop inside the test
file; neither production function ran. Here both run on a real Owl. Only the
serial port is a stand-in (and the broker: the dashboard publisher runs
offline, its handler is called the way a message on owl/gps calls it).

The Owl is built but hoot() is not started: the state thread that calls
_get_best_gps_data ten times a second starts in the constructor, and no
frame is needed. Spray timing from GPS speed is tests/owl/test_gps_spray_latency.py.

The GPS LED is read as the state the Owl put it in; that each state drives
the pin its own way is tests/unit/test_serial_gps.py::TestGpsLedPatterns.
"""

import threading
import time
from types import SimpleNamespace

import pytest

import harness
from utils.output_manager import GPSLEDState

# harness.GpsReceiver's position: 3347.1234 S, 15101.5678 E
RECEIVER_LAT = -(33 + 47.1234 / 60)
RECEIVER_LON = 151 + 1.5678 / 60
PHONE = {'latitude': -31.5, 'longitude': 148.25, 'accuracy': 4.0, 'source': 'phone'}

XGPS_FIX = '#XGPS: -33.786234,151.026130,45.3,4.2,2.5,210.0,"2026-09-13 06:25:48"'


def _wait(condition, what, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    pytest.fail(f'timed out waiting for {what}')


def _led(instance):
    return instance.gps_status_led._state


def _record_led(instance):
    """Every state the Owl puts its GPS LED in from now on, in order."""
    states = []
    real = instance.gps_status_led.set_state

    def set_state(state):
        states.append(state)
        real(state)

    instance.gps_status_led.set_state = set_state
    return states


class ScriptedPort:
    """A serial port a test drives.

    As an nRF9151 (modem=True) it answers every AT command with OK unless
    `silent`; `stream` is the line it repeats five times a second (None = a
    quiet receiver); push() queues one line; `broken` makes the next read
    raise, as a yanked USB receiver does.
    """

    def __init__(self, stream=None, modem=False):
        self.stream = stream
        self.modem = modem
        self.silent = False
        self.broken = False
        self.is_open = True
        self.written = []
        self._queue = []
        self._lock = threading.Lock()

    def push(self, line):
        with self._lock:
            self._queue.append(line)

    def write(self, data):
        command = data.decode('ascii').strip()
        self.written.append(command)
        if self.modem and not self.silent:
            self.push('OK')
        return len(data)

    def readline(self):
        if self.broken:
            raise OSError('device disconnected')
        with self._lock:
            line = self._queue.pop(0) if self._queue else None
        if line is None:
            time.sleep(0.05)
            line = self.stream if self.stream and not self.silent else ''
            if line:
                time.sleep(0.15)
        return (line + '\r\n').encode('ascii') if line else b''

    def close(self):
        self.is_open = False


class ReaderClock:
    """owl.py's `time` module as the GPS reader thread sees it: its 5 s
    reconnect pause is cut to 50 ms and its monotonic clock can be moved
    forward (the 30 s of silence before the modem is pinged). Every other
    thread gets the real thing."""

    def __init__(self):
        self.skip = 0.0

    @staticmethod
    def _reader():
        target = getattr(threading.current_thread(), '_target', None)
        return getattr(target, '__name__', '') == '_serial_gps_reader'

    def monotonic(self):
        return time.monotonic() + (self.skip if self._reader() else 0.0)

    def sleep(self, seconds):
        time.sleep(0.05 if self._reader() and seconds >= 5 else seconds)

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def gps_owl(owl_factory, config_factory, input_dir, monkeypatch):
    """gps_owl(source, ports=[...], dash='none', controller='none') -> (Owl, opened)

    `ports` are handed to the reader one per connect; `opened` lists the ones
    it has taken so far. The fixture stops the reader thread at teardown.
    """
    import owl as owl_module

    monkeypatch.setattr(owl_module, 'Process', harness.ThreadProcess)
    clock = ReaderClock()
    monkeypatch.setattr(owl_module, 'time', clock)
    built, connects = [], []      # connects: the arguments of every serial.Serial(...) call

    def _build(source, ports=(), dash='none', controller='none', port='/dev/fake-gps'):
        waiting, opened = list(ports), []

        def connect(**kwargs):
            connects.append(kwargs)
            if not waiting:
                raise OSError('no such device')
            opened.append(waiting.pop(0))
            return opened[-1]

        monkeypatch.setattr(owl_module, 'serial',
                            SimpleNamespace(Serial=connect, SerialException=OSError))
        config = config_factory(overrides=[
            ('Controller', 'controller_type', controller),
            ('DataCollection', 'detection_enable', 'False'),
            ('GPS', 'source', source), *([('GPS', 'port', port)] if port else []),
            *harness.DASH[dash]])
        instance = owl_factory(config, input_file_or_directory=str(input_dir))
        instance.reader_clock = clock
        instance.serial_connects = connects
        built.append(instance)
        return instance, opened

    yield _build
    for instance in built:
        instance._gps_running = False


# --------------------------------------------------------------------------
# Which source wins
# --------------------------------------------------------------------------

def test_a_receiver_with_no_fix_yields_to_the_phone_and_takes_over_once_it_has_one(gps_owl):
    """Serial beats dashboard, but only with a valid fix: a receiver still
    searching must not blank out a good phone position, and a phone position
    must not override the unit's own receiver once it has a fix."""
    receiver = harness.GpsReceiver(speed_kmh=7.0, fix=False)
    instance, opened = gps_owl('serial', ports=[receiver], dash='standalone')
    try:
        _wait(lambda: opened, 'the reader to open the port')
        time.sleep(0.8)                                    # several GGA + RMC, none with a fix
        assert instance.gps_data is None, 'a position was reported without a fix'
        assert instance.dash.state['gps_fix'] is False

        instance.dash._handle_gps_update(dict(PHONE))
        _wait(lambda: instance.gps_data is not None, 'the phone position to be used')
        assert instance.gps_data['source'] == 'phone'
        assert (instance.gps_data['latitude'], instance.gps_data['longitude']) == (-31.5, 148.25)
        assert instance.gps_data['accuracy'] == 4.0

        receiver.fix = True
        _wait(lambda: (instance.gps_data or {}).get('source') == 'serial',
              'the serial fix to take over from a fresh phone position')
        instance.dash._handle_gps_update(dict(PHONE))        # the phone is still talking
        time.sleep(0.5)
        gps = instance.gps_data
        assert gps['source'] == 'serial'
        assert gps['latitude'] == pytest.approx(RECEIVER_LAT, abs=1e-5)
        assert gps['longitude'] == pytest.approx(RECEIVER_LON, abs=1e-5)
        # the receiver's own numbers, passed through for EXIF and the track
        assert gps['speed_kmh'] == pytest.approx(7.0, abs=0.1)
        assert gps['satellites'] == 8
        assert gps['hdop'] == 4.5 and gps['accuracy'] == 4.5      # no metres from NMEA: HDOP
        assert gps['altitude'] == 45.3
        assert gps['heading'] == 210.3
        assert (gps['utc_time'], gps['utc_date']) == ('123519', '230394')
        assert abs(gps['timestamp'] - time.time()) < 1.0
        assert _led(instance) == GPSLEDState.FIX
        # what the dashboard shows
        assert instance.dash.state['gps_fix'] is True
        assert instance.dash.state['gps_source'] == 'serial'
        assert instance.dash.state['gps_best_lat'] == pytest.approx(RECEIVER_LAT, abs=1e-5)
    finally:
        receiver.close()


def test_a_phone_position_is_dropped_three_seconds_after_the_phone_goes_quiet(gps_owl):
    """A stale position must not be written into image EXIF. Fresh within
    the threshold, gone after it, back with the next update; the LED follows
    (off: nothing ever heard; solid: position; error flash: lost)."""
    instance, _ = gps_owl('none', dash='standalone', controller='ute')
    time.sleep(0.3)
    assert instance.gps_data is None
    assert _led(instance) == GPSLEDState.OFF

    instance.dash._handle_gps_update(dict(PHONE))
    received = time.monotonic()
    _wait(lambda: instance.gps_data is not None, 'the phone position to be used', timeout=1.0)
    assert _led(instance) == GPSLEDState.FIX
    assert instance.dash.state['gps_fix'] is True and instance.dash.state['gps_source'] == 'phone'

    time.sleep(max(0.0, received + 2.5 - time.monotonic()))
    assert instance.gps_data is not None and instance.gps_data['latitude'] == -31.5, (
        'dropped a 2.5 s old position: the threshold is 3 s')

    time.sleep(max(0.0, received + 3.5 - time.monotonic()))
    assert instance.gps_data is None, 'a position 3.5 s old is still being used'
    assert _led(instance) == GPSLEDState.ERROR
    assert instance.dash.state['gps_fix'] is False

    # a position with no source tag (the controller's broadcast) is 'dashboard'
    instance.dash._handle_gps_update({'latitude': -31.6, 'longitude': 148.3})
    _wait(lambda: instance.gps_data is not None, 'the new position to be used', timeout=1.0)
    assert instance.gps_data['source'] == 'dashboard'
    assert instance.gps_data['latitude'] == -31.6
    assert _led(instance) == GPSLEDState.FIX


def test_the_senders_clock_does_not_decide_staleness(gps_owl):
    """A phone whose clock is minutes out still gives a usable position:
    age is measured from when this OWL received it."""
    instance, _ = gps_owl('none', dash='standalone')
    instance.dash._handle_gps_update(dict(PHONE, timestamp=time.time() - 600))
    _wait(lambda: instance.gps_data is not None, 'a position stamped by a slow clock', timeout=1.0)
    assert instance.gps_data['latitude'] == -31.5


# --------------------------------------------------------------------------
# The serial reader when the receiver misbehaves
# --------------------------------------------------------------------------

def test_a_read_error_shows_on_the_led_and_the_reader_reconnects(gps_owl):
    """A USB receiver knocked out mid-field: the port is closed, the LED is
    told, and the reader opens the port again by itself."""
    first = harness.GpsReceiver(speed_kmh=3.0)
    second = harness.GpsReceiver(speed_kmh=3.0)
    instance, opened = gps_owl('serial', ports=[first, second])
    try:
        _wait(lambda: (instance.gps_data or {}).get('source') == 'serial', 'the first fix')
        assert opened == [first]
        led_states = _record_led(instance)

        def unplugged():
            raise OSError('device reports readiness to read but returned no data')

        first.readline = unplugged
        _wait(lambda: len(opened) == 2, 'the reader to reconnect')
        assert first.is_open is False, 'the dead port was left open'
        assert GPSLEDState.ERROR in led_states, 'the read error never reached the LED'
        _wait(lambda: second.is_open and _led(instance) == GPSLEDState.FIX, 'the fix to come back')
        time.sleep(0.5)
        assert instance.gps_data['source'] == 'serial'
        assert opened == [first, second], 'reconnected again with a working receiver'
    finally:
        first.close()
        second.close()


@pytest.mark.xfail(strict=True, reason=(
    "With [GPS] source = serial the GPS LED is written by two threads that disagree: the "
    "reader sets ACQUIRING (no fix) or ERROR (no receiver), and 100 ms later the state "
    "thread's _get_best_gps_data sets OFF because no source has a position (owl.py "
    "_get_best_gps_data: 'No GPS source available' -> GPSLEDState.OFF, with no check that a "
    "serial reader owns the LED). The LED shows 'GPS disabled' on a unit that is searching "
    "or has lost its receiver."))
@pytest.mark.parametrize('receiver, shows', [('searching', GPSLEDState.ACQUIRING),
                                             ('absent', GPSLEDState.ERROR)])
def test_the_led_of_a_serial_gps_unit_without_a_fix_never_reads_gps_disabled(
        gps_owl, receiver, shows):
    port = harness.GpsReceiver(fix=False) if receiver == 'searching' else None
    instance, opened = gps_owl('serial', ports=[port] if port else [])
    try:
        time.sleep(0.6)
        led_states = _record_led(instance)
        time.sleep(1.0)
        assert GPSLEDState.OFF not in led_states, (
            f'LED set to OFF {led_states.count(GPSLEDState.OFF)} times in 1 s')
        assert _led(instance) == shows
    finally:
        if port:
            port.close()


def test_nrf91_is_started_with_at_commands_and_started_again_when_the_modem_restarts(gps_owl):
    """The nRF9151 only reports fixes after GPS is started over AT, and
    forgets that when it resets (a brown-out on the carrier). Its 'Ready'
    banner must make the reader reconnect and run the start sequence again:
    without that the unit has no position until the OWL is restarted."""
    first = ScriptedPort(stream=XGPS_FIX, modem=True)
    second = ScriptedPort(stream=XGPS_FIX, modem=True)
    instance, opened = gps_owl('nrf91', ports=[first, second], port=None)

    _wait(lambda: (instance.gps_data or {}).get('source') == 'serial', 'the first nRF91 fix')
    # no [GPS] port or baudrate set: the production carrier's UART3
    assert instance.serial_connects[0] == {'port': '/dev/ttyAMA3', 'baudrate': 460800, 'timeout': 2}
    assert first.written[0] == 'AT' and first.written[-1] == 'AT#XGPS=1,0,1'
    gps = instance.gps_data
    assert gps['latitude'] == pytest.approx(-33.786234)
    assert gps['accuracy'] == 4.2, 'metres from the modem, not HDOP'
    assert gps['hdop'] is None
    assert gps['speed_kmh'] == 9.0                      # 2.5 m/s
    assert _led(instance) == GPSLEDState.FIX

    first.push('Ready')
    _wait(lambda: len(opened) == 2, 'the reader to reconnect after the Ready banner')
    assert first.is_open is False
    _wait(lambda: second.written[-1:] == ['AT#XGPS=1,0,1'], 'GPS to be started again')
    assert second.written[0] == 'AT'
    assert 'AT+CFUN=31' in second.written               # GNSS-only mode, set again


def test_a_quiet_nrf91_is_pinged_and_restarted_only_if_it_does_not_answer(gps_owl):
    """Silence is normal while the modem searches for satellites, so after
    30 s the reader asks 'AT'. An answer means carry on on the same port; no
    answer means the modem is gone: reconnect and start GPS again."""
    first = ScriptedPort(stream=None, modem=True)        # started, no fix yet: quiet
    second = ScriptedPort(stream=XGPS_FIX, modem=True)
    instance, opened = gps_owl('nrf91', ports=[first, second])

    _wait(lambda: first.written[-1:] == ['AT#XGPS=1,0,1'], 'GPS to be started')
    time.sleep(0.3)
    started = len(first.written)

    instance.reader_clock.skip += 28.0
    time.sleep(0.4)
    assert len(first.written) == started, 'pinged before 30 s of silence'

    instance.reader_clock.skip += 3.0                    # past 30 s of silence
    _wait(lambda: len(first.written) == started + 1, 'the modem to be pinged')
    assert first.written[-1] == 'AT'
    time.sleep(0.4)
    assert opened == [first] and first.is_open, 'a modem that answered was dropped'
    assert len(first.written) == started + 1, 'pinged again straight after an answer'

    first.silent = True                                  # now it stops answering
    instance.reader_clock.skip += 31.0
    _wait(lambda: len(opened) == 2, 'the reader to give up on a silent modem', timeout=8.0)
    assert first.written[-1] == 'AT' and first.is_open is False
    _wait(lambda: (instance.gps_data or {}).get('source') == 'serial',
          'a fix from the restarted modem')


# --------------------------------------------------------------------------
# An nRF91 unit whose config puts devices on the modem's pins
# --------------------------------------------------------------------------

class _Button:
    """gpiozero.Button on a Pi: records the pin it claimed."""
    made = []

    def __init__(self, pin, bounce_time=None):
        _Button.made.append(pin)
        self.is_pressed = False
        self.when_pressed = self.when_released = None


@pytest.mark.allow_errors          # each disabled device is logged as an ERROR, on purpose
@pytest.mark.parametrize('controller, on_claimed_pins, disabled_relays, status_led, gps_led, buttons', [
    ('ute', [('Relays', '1', '37'), ('Controller', 'status_led_pin', '24'),
             ('Controller', 'switch_pin', '21')],
     {1: 37}, 'disabled', 'BOARD38', []),
    ('advanced', [('Controller', 'gps_led_pin', '37'), ('Controller', 'recording_pin', '21'),
                  ('Controller', 'detection_mode_pin_up', '24')],
     {}, 'BOARD40', None, ['BOARD32', 'BOARD35']),      # sensitivity and mode-down keep theirs
])
def test_an_nrf91_unit_boots_with_every_device_on_a_claimed_pin_left_out(
        owl_factory, config_factory, input_dir, monkeypatch, controller, on_claimed_pins,
        disabled_relays, status_led, gps_led, buttons):
    """[GPS] source = nrf91 claims BOARD 37 (modem power) and 21/24 (its
    UART). A relay, LED or switch configured there is left out and the OWL
    still starts; a GPIO device on 37 would switch the modem off. This runs
    the switch-panel code as on a Pi (Button is a recorder), so a pin that
    reaches gpiozero without going through the check shows up in `made`."""
    import owl as owl_module
    import utils.input_manager as input_manager

    monkeypatch.setattr(owl_module, 'Process', harness.ThreadProcess)
    modem = ScriptedPort(stream=XGPS_FIX, modem=True)
    monkeypatch.setattr(owl_module, 'serial',
                        SimpleNamespace(Serial=lambda **kwargs: modem, SerialException=OSError))
    monkeypatch.setattr(input_manager, 'testing', False)
    monkeypatch.setattr(input_manager, 'Button', _Button, raising=False)
    _Button.made = []

    config = config_factory(overrides=[
        ('Controller', 'controller_type', controller), ('GPS', 'source', 'nrf91'),
        ('GPS', 'port', '/dev/fake-gps'), *on_claimed_pins])
    instance = owl_factory(config, input_file_or_directory=str(input_dir))
    try:
        assert instance.relay_controller.relay.disabled_relays == disabled_relays
        assert set(instance.relay_controller.relay_schedule_dict) == {0, 1, 2, 3}, 'a lane was lost'
        assert instance.status_indicator.led.pin == status_led
        assert (instance.gps_status_led.led.pin if instance.gps_status_led else None) == gps_led
        assert _Button.made == buttons
        _wait(lambda: (instance.gps_data or {}).get('source') == 'serial',
              'the modem to give a fix: the unit is otherwise working')
    finally:
        instance._gps_running = False
