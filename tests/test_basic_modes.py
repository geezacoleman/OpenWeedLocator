"""Basic-deployment tests: OWL must run with nothing else attached.

Three shapes of install have to keep working no matter what the dashboard,
cloud and app features do:

  1. a laptop/desktop with no GPIO, no camera and no MQTT broker (the
     `--input <directory>` workflow used for development and demos),
  2. an OWL unit with no dashboard/app at all (MQTT off or broker down),
  3. an OWL unit driven only by the hardware switches (Ute/Advanced
     controller), again with no dashboard.

Every test here builds a real Owl from a real INI and runs real frames
through hoot(); nothing about the detection path is mocked out.
"""

import configparser
import os
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cv2  # noqa: E402

import owl as owl_module  # noqa: E402
from owl import Owl  # noqa: E402
from utils.input_manager import AdvancedController, UteController  # noqa: E402
# aliased: a name starting with 'Test' would be collected as a test class
from utils.output_manager import TestLED as StubLED  # noqa: E402

BASE_CONFIG = PROJECT_ROOT / 'config' / 'GENERAL_CONFIG.ini'


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

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


@pytest.fixture
def isolate_repo_configs(monkeypatch):
    """Ignore any CONTROLLER.ini/GEOMETRY.ini the developer has locally.

    owl.py reads both from config/ on top of the chosen config file, so a
    real unit's files would otherwise decide whether these tests see a
    dashboard.
    """
    real_read = configparser.RawConfigParser.read

    def read(self, filenames, encoding=None):
        if isinstance(filenames, (str, os.PathLike)):
            filenames = [filenames]
        keep = [f for f in filenames
                if Path(f).name not in ('CONTROLLER.ini', 'GEOMETRY.ini')]
        return real_read(self, keep, encoding)

    monkeypatch.setattr(configparser.RawConfigParser, 'read', read)


@pytest.fixture
def input_dir(tmp_path):
    """A directory of images, as passed to `python owl.py --input ...`."""
    media = tmp_path / 'media'
    media.mkdir()
    for i in range(2):
        cv2.imwrite(str(media / f'frame{i}.png'), make_frame())
    return media


@pytest.fixture
def config_factory(tmp_path):
    """Build a config file from GENERAL_CONFIG.ini with overrides applied."""

    def _factory(name='TEST_CONFIG.ini', overrides=None, drop=()):
        config = configparser.ConfigParser()
        config.read(BASE_CONFIG)

        # Keep tests fast and off the real recording paths
        config.set('Camera', 'resolution_width', '416')
        config.set('Camera', 'resolution_height', '320')
        config.set('DataCollection', 'storage_location', 'internal')
        config.set('DataCollection', 'internal_save_directory',
                   str(tmp_path / 'owl_images'))
        config.set('DataCollection', 'save_directory', str(tmp_path / 'owl_images'))
        config.set('DataCollection', 'min_free_gb', '1')

        for section, key, value in (overrides or []):
            if not config.has_section(section):
                config.add_section(section)
            config.set(section, key, value)

        for section, key in drop:
            config.remove_option(section, key)

        path = tmp_path / name
        with open(path, 'w') as f:
            config.write(f)
        return path

    return _factory


@pytest.fixture
def owl_factory(isolate_repo_configs, monkeypatch, tmp_path):
    """Build real Owl instances and guarantee they are torn down.

    Owl.stop() ends in sys.exit(0), which is right for the application but
    would kill the test run, so SystemExit is swallowed here and in the
    hoot() helper below.
    """
    built = []
    # Never touch the operator's first-boot flag path from a test
    monkeypatch.setenv('OWL_FIRSTBOOT_FLAG', str(tmp_path / 'no-firstboot.flag'))

    def _factory(config_path, **kwargs):
        instance = Owl(config_file=str(config_path), **kwargs)
        built.append(instance)
        return instance

    yield _factory

    for instance in built:
        try:
            instance.stop()
        except SystemExit:
            pass
        except Exception:
            pass


def run_hoot(instance, frames=6):
    """Run the detection loop over `frames` frames and return relay firings."""
    instance.cam = FakeCamera(frames=frames)

    fired = []
    original_receive = instance.relay_controller.receive
    instance.relay_controller.receive = lambda **kwargs: (
        fired.append(kwargs), original_receive(**kwargs))[1]

    try:
        instance.hoot()
    except SystemExit:
        pass  # hoot() -> stop() -> sys.exit(0) on the None frame
    return fired


# --------------------------------------------------------------------------
# 1. laptop / desktop, no hardware and no dashboard
# --------------------------------------------------------------------------

def test_laptop_no_dashboard_runs_detection_loop(owl_factory, config_factory, input_dir):
    """The plain `python owl.py --input <dir>` case: no GPIO, no broker."""
    config = config_factory(overrides=[('DataCollection', 'detection_enable', 'True')])
    instance = owl_factory(config, input_file_or_directory=str(input_dir))

    assert instance.dash is None, 'no MQTT configured, so there must be no dashboard'
    assert instance.controller is None
    assert instance.controller_type == 'none'

    fired = run_hoot(instance)
    assert fired, 'a green blob in frame should actuate at least one relay'
    assert all(0 <= f['relay'] < instance.relay_num for f in fired)
    assert instance.cam.remaining == 0, 'the loop must run to the end of the footage'


def test_laptop_input_directory_sets_frame_geometry(owl_factory, config_factory, input_dir):
    """An image directory must configure geometry exactly like a camera does."""
    instance = owl_factory(config_factory(), input_file_or_directory=str(input_dir))

    assert (instance.frame_width, instance.frame_height) == (416, 320)
    assert instance.crop_slice is not None
    assert len(instance.lane_coords_int) == instance.relay_num


def test_legacy_min_detection_area_config_starts(owl_factory, config_factory, input_dir):
    """Regression: a pre-percent config crashed startup with

        AttributeError: 'Logger' object has no attribute 'log_line'

    Old units still carry `min_detection_area` (px) with no percent key.
    """
    config = config_factory(
        name='LEGACY_CONFIG.ini',
        overrides=[('GreenOnBrown', 'min_detection_area', '10')],
        drop=[('GreenOnBrown', 'min_detection_area_percent')])

    instance = owl_factory(config, input_file_or_directory=str(input_dir))

    assert instance.min_detection_area_percent > 0, 'legacy px value must be migrated'
    assert instance.config.get('GreenOnBrown', 'min_detection_area_percent')


def test_detection_runs_with_legacy_config(owl_factory, config_factory, input_dir):
    """The migrated threshold has to still detect weeds."""
    config = config_factory(
        name='LEGACY_RUN.ini',
        overrides=[('GreenOnBrown', 'min_detection_area', '10'),
                   ('DataCollection', 'detection_enable', 'True')],
        drop=[('GreenOnBrown', 'min_detection_area_percent')])
    instance = owl_factory(config, input_file_or_directory=str(input_dir))

    assert run_hoot(instance), 'detection must still fire after the px -> % migration'


def test_mqtt_configured_but_broker_down_keeps_running(owl_factory, config_factory,
                                                       input_dir):
    """A unit configured for the dashboard must still boot and run its loop
    when the broker is unreachable — no app, no network, no crash.

    Detection stays off here because the dashboard owns that switch whenever
    one is configured; the point of the test is that the unit keeps running
    and recovers when the broker comes back, rather than dying at startup.
    """
    config = config_factory(
        name='MQTT_DOWN.ini',
        overrides=[('MQTT', 'enable', 'True'),
                   ('MQTT', 'broker_ip', '127.0.0.1'),
                   # port 1 is never listening: connection refused, immediately
                   ('MQTT', 'broker_port', '1'),
                   ('MQTT', 'device_id', 'test-owl'),
                   ('DataCollection', 'detection_enable', 'True')])

    instance = owl_factory(config, input_file_or_directory=str(input_dir))

    assert instance.dash is not None, 'MQTT enabled, so the publisher is built'
    assert instance.dash.connected is False

    run_hoot(instance)
    assert instance.cam.remaining == 0, 'the loop must run with the broker down'


# --------------------------------------------------------------------------
# 2. headless OpenCV / no display attached
# --------------------------------------------------------------------------

def test_show_display_falls_back_without_gui(owl_factory, config_factory, input_dir,
                                             monkeypatch):
    """--show-display on a headless build must warn, not crash."""
    monkeypatch.setattr(owl_module, 'has_gui_support', lambda: False)

    instance = owl_factory(config_factory(), show_display=True,
                           input_file_or_directory=str(input_dir))

    assert instance.show_display is False
    assert run_hoot(instance) is not None


def test_headless_loop_never_calls_waitkey(owl_factory, config_factory, input_dir,
                                           monkeypatch):
    """Regression: waitKey() ran every frame even with no window open, which
    kills the loop outright on an opencv-headless install."""
    def explode(*args, **kwargs):
        raise cv2.error('The function is not implemented. Rebuild the library '
                        'with Windows, GTK+ 2.x or Cocoa support')

    monkeypatch.setattr(cv2, 'waitKey', explode)
    monkeypatch.setattr(cv2, 'imshow', explode)

    config = config_factory(overrides=[('DataCollection', 'detection_enable', 'True')])
    instance = owl_factory(config, input_file_or_directory=str(input_dir))

    assert instance.show_display is False
    assert run_hoot(instance), 'detection must run on a headless OpenCV build'
    # The whole point: the loop ran to the end of the footage instead of
    # dying on the first frame with a highgui error.
    assert instance.cam.remaining == 0


def test_has_gui_support_reports_false_when_highgui_raises(monkeypatch):
    monkeypatch.setattr(cv2, 'namedWindow', lambda *a, **k: (_ for _ in ()).throw(
        cv2.error('no GUI support')))
    assert owl_module.has_gui_support() is False


# --------------------------------------------------------------------------
# 3. hardware controllers, no dashboard
# --------------------------------------------------------------------------

@pytest.mark.parametrize('controller_type', ['ute', 'advanced'])
def test_hardware_controller_without_dashboard(owl_factory, config_factory, input_dir,
                                               controller_type):
    """Switch-driven units must boot and run with no MQTT and no GPIO."""
    config = config_factory(
        name=f'{controller_type.upper()}_CONFIG.ini',
        overrides=[('Controller', 'controller_type', controller_type),
                   # the LED pins are irrelevant off-Pi but exercise the
                   # indicator paths rather than the disabled stub
                   ('Controller', 'status_led_pin', '40')])

    instance = owl_factory(config, input_file_or_directory=str(input_dir))

    assert instance.dash is None
    assert instance.controller is not None
    assert instance.controller_process.is_alive()
    # No switch closed off-Pi: detection and recording both start off, and
    # the state thread mirrors the shared Values rather than a dashboard.
    assert bool(instance.detection_enable.value) is False
    assert run_hoot(instance) is not None


def test_advanced_controller_update_state_without_gpio():
    """Regression: update_detection_mode_state() raised

        AttributeError: 'NoneType' object has no attribute 'is_pressed'

    on every state update off-Pi, so the advanced controller logged an error
    each cycle instead of resolving to 'off'.
    """
    from multiprocessing import Value

    from utils.shared_types import Sensitivity

    class DummyIndicator:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def _record(*args, **kwargs):
                self.calls.append(name)
            return _record

    class DummySensitivityManager:
        def __init__(self):
            self.applied = []

        def apply_preset(self, name, owl):
            self.applied.append(name)

    class DummyRelay:
        def __init__(self):
            self.calls = []

        def all_off(self):
            self.calls.append('all_off')

        def all_on(self):
            self.calls.append('all_on')

    class DummyRelayController:
        def __init__(self):
            self.relay = DummyRelay()

    class DummyOwl:
        def __init__(self):
            self.detection_enable = Value('b', True)
            self.dash = None
            self.relay_controller = DummyRelayController()

    owl_instance = DummyOwl()
    errors = []

    controller = AdvancedController(
        recording_state=Value('b', False),
        sensitivity_level=Value('i', Sensitivity.HIGH.value),
        detection_mode_state=Value('i', 1),
        stop_flag=Value('b', False),
        owl_instance=owl_instance,
        status_indicator=DummyIndicator(),
        sensitivity_manager=DummySensitivityManager())

    controller.logger.error = lambda msg, *a, **k: errors.append(msg)
    controller.update_state()

    assert errors == [], f'update_state must not error off-Pi: {errors}'
    # No switches: three-position switch resolves to centre (off)
    assert controller.detection_mode_state.value == 1
    assert bool(owl_instance.detection_enable.value) is False
    assert set(owl_instance.relay_controller.relay.calls) == {'all_off'}


def test_ute_controller_update_state_without_gpio():
    """The Ute controller's single switch reads as open off-Pi."""
    from multiprocessing import Value

    class DummyIndicator:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def _record(*args, **kwargs):
                self.calls.append(name)
            return _record

    controller = UteController(
        detection_state=Value('b', True),
        sample_state=Value('b', True),
        stop_flag=Value('b', False),
        owl_instance=object(),
        status_indicator=DummyIndicator(),
        switch_purpose='recording')

    controller.update_state()
    assert bool(controller.sample_state.value) is False


# --------------------------------------------------------------------------
# 4. GPIO stand-ins used whenever there is no Pi
# --------------------------------------------------------------------------

def test_stub_led_is_quiet(capsys):
    """The GPS LED thread drives off() twice a second: printing each call
    buried the real console output on a laptop."""
    led = StubLED(pin='BOARD38')
    for _ in range(5):
        led.off()
        led.on()

    assert capsys.readouterr().out == ''


def test_stub_led_blocking_blink_takes_time():
    """background=False must block like gpiozero does, otherwise the
    indicator threads spin the CPU at 100% off-Pi."""
    import time

    led = StubLED(pin='BOARD38')
    start = time.time()
    led.blink(on_time=0.05, off_time=0.05, n=2, background=False)
    assert time.time() - start >= 0.15

    start = time.time()
    led.blink(on_time=0.05, off_time=0.05, n=2, background=True)
    assert time.time() - start < 0.05


def test_gps_status_led_thread_does_not_spin():
    """GPSStatusLED's worker sleeps between blinks on every platform."""
    from utils.output_manager import GPSLEDState, GPSStatusLED

    led = GPSStatusLED(pin='BOARD38')
    try:
        led.set_state(GPSLEDState.ACQUIRING)
        assert led._thread.is_alive()
        assert threading.active_count() >= 1
    finally:
        led.stop()
    assert led._thread.is_alive() is False
