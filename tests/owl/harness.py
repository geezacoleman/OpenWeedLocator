"""Helpers for tests that build a real Owl and run real frames through hoot().

Fixtures that use these (owl_factory, config_factory, input_dir) live in
tests/owl/conftest.py. Import from a test with `from harness import ...`.
"""

import time
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = PROJECT_ROOT / 'config' / 'GENERAL_CONFIG.ini'


def make_frame(width=416, height=320):
    """A brown frame with a green blob — reliably detected by exhsv."""
    frame = np.full((height, width, 3), (40, 60, 90), dtype=np.uint8)
    cv2.rectangle(frame, (width // 2 - 40, height // 2 - 40),
                  (width // 2 + 40, height // 2 + 40), (60, 180, 60), -1)
    return frame


class FakeCamera:
    """Yields a fixed number of frames, then None so hoot() shuts down."""

    def __init__(self, frames=6, width=416, height=320):
        self.remaining = frames
        self.frame = make_frame(width, height)
        self.stopped = False

    def read_with_metadata(self):
        if self.remaining <= 0:
            return None, None
        self.remaining -= 1
        return self.frame.copy(), None

    def read(self):
        return self.read_with_metadata()[0]

    def stop(self):
        self.stopped = True


def run_hoot(instance, frames=6):
    """Run the detection loop over `frames` frames and return relay firings."""
    instance.cam = FakeCamera(frames=frames)

    fired = []
    original_receive = instance.relay_controller.receive

    def record(**kwargs):
        # called_at lets tests check the window against the moment the relay
        # controller was actually asked to spray, not just the arguments
        fired.append(dict(kwargs, called_at=time.monotonic()))
        return original_receive(**kwargs)

    instance.relay_controller.receive = record

    try:
        instance.hoot()
    except SystemExit:
        pass  # hoot() -> stop() -> sys.exit(0) on the None frame
    return fired


# ==========================================================================
# The rig: a running Owl whose relays are watched
# ==========================================================================
#
# What a spray test is for is the nozzle. Everything here asserts on
# RelayControl.relay_on / relay_off (the last call before the GPIO pin), with
# the real Owl, real threads and real RelayController in between. Only the
# camera, the serial port, the broker and the switch-panel process are
# stand-ins. Build one with the `rig` fixture (tests/owl/conftest.py).

import queue          # noqa: E402
import threading      # noqa: E402

import pytest         # noqa: E402

# A frame at 30 fps plus detection. Used as "at once".
PROMPT_S = 0.25
# [System] actuation_duration in GENERAL_CONFIG.ini
CONFIG_DURATION_S = 0.15

# [MQTT] settings that give the Owl a dashboard publisher with no broker
# (port 1 refuses, so it runs offline, as on a unit whose broker is down)
DASH = {
    'none': [],
    'standalone': [('MQTT', 'enable', 'True'), ('MQTT', 'broker_ip', '127.0.0.1'),
                   ('MQTT', 'broker_port', '1'), ('MQTT', 'device_id', 'test-owl')],
}
DASH['networked'] = DASH['standalone'] + [('Network', 'mode', 'networked')]

# controller type x dashboard: every shape of install
MATRIX = [('none', 'none'), ('none', 'standalone'), ('none', 'networked'),
          ('ute', 'none'), ('ute', 'standalone'),
          ('advanced', 'none'), ('advanced', 'standalone')]


def _nmea(body):
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f'${body}*{checksum:02X}\r\n'.encode('ascii')


class GpsReceiver:
    """A receiver on a serial port, streaming GGA + RMC five times a second.

    speed_kmh: a number, or a callable returning one per sample (a wandering
    fix). fix=False sends what a receiver sends with the antenna unplugged:
    GGA quality 0 and a void RMC.
    """

    def __init__(self, speed_kmh=0.0, fix=True):
        self.speed_kmh = speed_kmh
        self.fix = fix
        self.is_open = True
        self._lines = queue.Queue()
        self._running = True
        threading.Thread(target=self._stream, daemon=True).start()

    def _stream(self):
        while self._running:
            speed = self.speed_kmh() if callable(self.speed_kmh) else self.speed_kmh
            knots = speed / 1.852
            quality, status = (1, 'A') if self.fix else (0, 'V')
            self._lines.put(_nmea(
                f'GPGGA,123519,3347.1234,S,15101.5678,E,{quality},08,4.5,45.3,M,0.0,M,,'))
            self._lines.put(_nmea(
                f'GPRMC,123519,{status},3347.1234,S,15101.5678,E,{knots:.3f},210.3,230394,003.1,W'))
            time.sleep(0.2)

    # the parts of serial.Serial the reader uses
    def readline(self):
        try:
            return self._lines.get(timeout=0.2)
        except queue.Empty:
            return b''

    def write(self, data):
        return len(data)

    def close(self):
        self.is_open = False
        self._running = False


def card_frame(width=416, height=320, x=0.5, y=0.5, size=80):
    """Bare soil with a green card centred at (x, y) as fractions of the
    frame. x picks the lane, y decides whether it is inside the spray band."""
    frame = np.full((height, width, 3), (40, 60, 90), dtype=np.uint8)
    cx, cy, half = int(width * x), int(height * y), size // 2
    cv2.rectangle(frame, (max(cx - half, 0), max(cy - half, 0)),
                  (min(cx + half, width - 1), min(cy + half, height - 1)), (60, 180, 60), -1)
    return frame


class CardCamera:
    """30 fps of bare soil, with a green card in view while `card` is set.

    card_x / card_y place the card (fractions of the frame). lag_s makes
    every frame arrive that long after it was exposed, with a picamera2-style
    SensorTimestamp, as a real sensor and ISP do (the frame rate is unchanged).
    """

    def __init__(self, width=416, height=320, fps=30, lag_s=0.0):
        self.width, self.height, self.fps, self.lag_s = width, height, fps, lag_s
        self._bare = np.full((height, width, 3), (40, 60, 90), dtype=np.uint8)
        self.card = False
        self.card_x = 0.5
        self.card_y = 0.5
        self.running = True
        self.frames = 0
        self.card_first_seen = None   # exposure time of the first card frame
        self.card_last_seen = None

    def read_with_metadata(self):
        if not self.running:
            return None, None
        time.sleep(1 / self.fps)
        # The sensor and ISP hold each frame for lag_s without slowing the
        # frame rate: what arrives now was exposed lag_s ago
        exposed = time.monotonic() - self.lag_s
        metadata = {'SensorTimestamp': int(exposed * 1e9)} if self.lag_s else None
        self.frames += 1
        if self.card:
            if self.card_first_seen is None:
                self.card_first_seen = exposed
            self.card_last_seen = exposed
            return card_frame(self.width, self.height, self.card_x, self.card_y), metadata
        return self._bare.copy(), metadata

    def read(self):
        return self.read_with_metadata()[0]

    def stop(self):
        self.running = False


class Switch:
    """A switch-panel toggle (off-Pi the controllers have none)."""

    def __init__(self, pressed):
        self.is_pressed = pressed


class ThreadProcess(threading.Thread):
    """Stands in for multiprocessing.Process for the switch-panel controller:
    under 'spawn' the real one pickles the whole Owl, and a forked one
    outlives the test holding pytest's stdout."""

    def __init__(self, target, daemon=True):
        super().__init__(target=target, daemon=daemon)

    def terminate(self):
        pass  # controller.stop() sets its stop flag; run() then returns


class Rig:
    """A running Owl, its camera and GPS stand-ins, and every relay drive."""

    def __init__(self, instance, cam, gps=None):
        self.owl = instance
        self.cam = cam
        self.gps = gps
        self.drives = []  # (monotonic, relay, on), in the order the pins were driven
        self._lock = threading.Lock()

        board = instance.relay_controller.relay
        real_on, real_off = board.relay_on, board.relay_off

        def relay_on(relay, verbose=True):
            with self._lock:
                self.drives.append((time.monotonic(), relay, True))
            return real_on(relay, verbose=verbose)

        def relay_off(relay, verbose=True):
            with self._lock:
                self.drives.append((time.monotonic(), relay, False))
            return real_off(relay, verbose=verbose)

        board.relay_on, board.relay_off = relay_on, relay_off
        # all_on / all_off loop over the relays through these two
        instance.cam = cam
        self._loop = threading.Thread(target=self._hoot, daemon=True)
        self._loop.start()
        self.wait_for(lambda: cam.frames >= 3, 'the detection loop to start')

    def _hoot(self):
        try:
            self.owl.hoot()
        except SystemExit:
            pass

    # ---- waiting -------------------------------------------------------

    def wait_for(self, condition, what, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        pytest.fail(f'timed out waiting for {what}: spray={self.owl.relay_controller.mode} '
                    f'source={self.owl.actuation_source} speed={self.owl.speed_kmh} '
                    f'delay={self.owl.delay} duration={self.owl.actuation_duration} '
                    f'relays on={sorted(self.relays_on())}')

    def wait_for_speed(self, kmh, tolerance=0.15):
        """Until the OWL's own averaged GPS speed has settled on kmh."""
        self.wait_for(lambda: self.owl._device_timing and self.owl.speed_kmh is not None
                      and abs(self.owl.speed_kmh - kmh) <= tolerance,
                      f'device GPS speed of {kmh} km/h')
        time.sleep(1.2)  # one more timing tick on the settled average

    def wait_for_moving(self, kmh):
        """Until the OWL has accepted kmh as real travel and times from it."""
        self.wait_for_speed(kmh)
        self.wait_for(lambda: self.owl.actuation_source == 'device-gps',
                      'speed-adaptive timing to start')

    # ---- the pins ------------------------------------------------------

    def relays_on(self, upto=None):
        """Relays whose last drive was ON."""
        state = {}
        with self._lock:
            drives = list(self.drives)
        for at, relay, on in drives:
            if upto is None or at <= upto:
                state[relay] = on
        return {relay for relay, on in state.items() if on}

    def drives_since(self, mark):
        with self._lock:
            return self.drives[mark:]

    def mark(self):
        with self._lock:
            return len(self.drives)

    def wait_all_off(self, timeout=8.0):
        self.wait_for(lambda: not self.relays_on(), 'every relay to be off', timeout)

    def wait_all_on(self, timeout=3.0):
        everything = set(range(self.owl.relay_num))
        self.wait_for(lambda: self.relays_on() == everything, 'every relay to be on', timeout)

    # ---- the operator --------------------------------------------------

    def command(self, action, **fields):
        """A command as it arrives from a dashboard, the app or the
        controller: through the OWL's real MQTT command handler."""
        assert self.owl.dash is not None, 'this rig has no dashboard publisher'
        self.owl.dash._handle_command(dict(fields, action=action))
        self.owl._update_state_once()

    def switch(self, detection=None, recording=None, blanket=False):
        """Flip the hardware switch panel (Ute: its one switch; Advanced: the
        three-position spray switch and the recording switch)."""
        controller = self.owl.controller
        if self.owl.controller_type == 'ute':
            wanted = recording if controller.switch_purpose == 'recording' else detection
            controller.switch = Switch(bool(wanted))
        elif self.owl.controller_type == 'advanced':
            if recording is not None:
                controller.recording_switch = Switch(recording)
            controller.detection_mode_switch_down = Switch(bool(detection) and not blanket)
            controller.detection_mode_switch_up = Switch(bool(blanket))
        else:
            raise AssertionError('this rig has no switch panel')
        controller.update_state()
        self.owl._update_state_once()

    def spray_on(self):
        """Turn spot spraying on the way this deployment does it."""
        if self.owl.controller_type != 'none':
            self.switch(detection=True)
        if self.owl.dash:
            self.command('set_detection_enable', value=True)
        self.wait_for(lambda: self.owl.relay_controller.mode == 'spot', 'spot spraying')

    # ---- weeds ---------------------------------------------------------

    def show_card(self, seconds=0.5, x=0.5, y=0.5, settle=8.0):
        """Hold a green card in view, take it away, and report what the
        relays did: a Showing. Waits until everything it switched on is off
        again (or `settle` seconds)."""
        time.sleep(0.1)   # let anything still spraying from before finish
        self.cam.card_first_seen = self.cam.card_last_seen = None
        self.cam.card_x, self.cam.card_y = x, y
        mark = self.mark()
        self.cam.card = True
        time.sleep(seconds)
        self.cam.card = False
        time.sleep(2 / self.cam.fps)       # the last card frame is out
        gone = self.cam.card_last_seen

        deadline = time.monotonic() + settle
        while time.monotonic() < deadline:
            time.sleep(0.05)
            state = {}
            for _, relay, on in self.drives_since(mark):
                state[relay] = on
            if state and not any(state.values()):
                break
            if not state and time.monotonic() - gone > 1.0 + self.owl.delay:
                break
        return Showing(self.drives_since(mark), self.cam.card_first_seen, gone)

    def stop(self):
        self.cam.running = False
        self._loop.join(timeout=5)


class Showing:
    """What the relays did for one card."""

    def __init__(self, drives, seen, gone):
        self.drives = drives
        self.seen = seen          # exposure time of the first frame with the card
        self.gone = gone          # exposure time of the last frame with the card
        ons = [at for at, _, on in drives if on]
        offs = [at for at, _, on in drives if not on]
        self.fired = sorted({relay for _, relay, on in drives if on})
        # card seen -> first relay on; card gone -> last relay off (seconds)
        self.on = ons[0] - seen if ons and seen is not None else None
        self.off = max(offs) - gone if ons and offs and gone is not None else None

    def __iter__(self):           # on, off = rig.show_card()
        return iter((self.on, self.off))

    def __repr__(self):
        return f'Showing(fired={self.fired}, on={self.on}, off={self.off})'
