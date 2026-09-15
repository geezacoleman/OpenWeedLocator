"""
Spray timing on the OWL itself (owl.py).

- An OWL with its own GPS (serial / nrf91) times sprays from its fix, and
  that always wins over the controller's broadcast.
- When the fix is lost, controller timing applies at once; without a
  controller the configured values come back. Phone GPS never drives timing.
- The delay handed to the relays has the frame's processing time taken off,
  and the capture time comes from the sensor timestamp when it is plausible.
- Annotation, indicators and system stats never run ahead of the relays.
"""

import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from owl import Owl
from utils.actuation import ActuationCalculator, SpeedAverager
from utils.gps_manager import GPSState

REPO = Path(__file__).parent.parent
# 2.7778 m/s = 10.0 km/h
FIX_10KMH = '#XGPS: -33.786234,151.026130,45.3,4.2,2.7778,210.0,"2026-09-13 06:25:48"'


def _owl(gps_state=None, **overrides):
    """Just the attributes Owl._update_spray_timing reads and writes."""
    fake = SimpleNamespace(
        _gps_state=gps_state,
        _device_timing=False,
        actuation_source='config',
        actuation_duration=0.15,
        delay=0.0,
        _config_actuation=(0.15, 0.0),
        _controller_timing_at=float('-inf'),
        _controller_speed_kmh=None,
        speed_kmh=None,
        speed_warning='',
        _avg_loop_time_ms=0.0,
        _speed_averager=SpeedAverager(window_seconds=2.0),
        _actuation_calc=ActuationCalculator(actuation_length_cm=10.0, offset_cm=30.0,
                                            fallback_duration=0.15, fallback_delay=0.0),
        _latency_s=deque(maxlen=30),
        _late_s=deque(maxlen=30),
        _CONTROLLER_TIMING_TIMEOUT_S=Owl._CONTROLLER_TIMING_TIMEOUT_S,
        logger=MagicMock(),
    )
    for key, value in overrides.items():
        setattr(fake, key, value)
    return fake


def _fix(line=FIX_10KMH):
    state = GPSState()
    state.update_from_line(line)
    return state


@pytest.mark.unit
class TestDeviceTiming:

    def test_fresh_fix_times_sprays_from_the_owl(self):
        fake = _owl(_fix())
        Owl._update_spray_timing(fake)
        assert fake.actuation_source == 'device-gps'
        assert fake._device_timing is True
        assert fake.speed_kmh == pytest.approx(10.0, abs=0.1)
        assert fake.actuation_duration == pytest.approx(0.036, abs=0.001)
        assert fake.delay == pytest.approx(0.108, abs=0.001)

    def test_config_timing_remembered_when_device_takes_over(self):
        fake = _owl(_fix(), actuation_duration=0.3, delay=0.02)
        Owl._update_spray_timing(fake)
        assert fake._config_actuation == (0.3, 0.02)

    def test_lost_fix_hands_over_to_a_live_controller(self):
        fake = _owl(None, _device_timing=True, actuation_source='device-gps',
                    actuation_duration=0.05, _controller_timing_at=time.monotonic(),
                    _controller_speed_kmh=8.0)
        Owl._update_spray_timing(fake)
        assert fake.actuation_source == 'controller'
        assert fake._device_timing is False
        assert fake.speed_kmh == 8.0

    def test_lost_fix_without_controller_restores_config(self):
        fake = _owl(None, _device_timing=True, actuation_source='device-gps',
                    actuation_duration=0.04, delay=0.1, _config_actuation=(0.2, 0.05))
        Owl._update_spray_timing(fake)
        assert fake.actuation_source == 'config'
        assert (fake.actuation_duration, fake.delay) == (0.2, 0.05)
        assert fake.speed_kmh is None

    def test_live_config_edits_survive_in_config_mode(self):
        fake = _owl(None, actuation_duration=0.3)
        Owl._update_spray_timing(fake)
        assert fake.actuation_duration == 0.3

    def test_phone_gps_never_drives_timing(self):
        # Phone/browser GPS reaches owl.py as gps_data, never as _gps_state
        fake = _owl(None, gps_data={'latitude': -33.8, 'longitude': 151.0,
                                    'speed_kmh': 12.0, 'source': 'dashboard'})
        Owl._update_spray_timing(fake)
        assert fake.actuation_source == 'config'
        assert fake._device_timing is False

    def test_late_sprays_raise_the_speed_warning(self):
        fake = _owl(_fix())
        fake._latency_s.extend([0.15] * 5)
        fake._late_s.extend([0.042] * 5)
        Owl._update_spray_timing(fake)
        assert 'late' in fake.speed_warning
        assert 'Slow to 7 km/h' in fake.speed_warning

    def test_no_warning_when_on_time(self):
        fake = _owl(_fix())
        fake._latency_s.extend([0.05] * 5)
        Owl._update_spray_timing(fake)
        assert fake.speed_warning == ''


@pytest.mark.unit
class TestCaptureTime:

    def test_plausible_sensor_timestamp_is_used(self):
        captured = time.monotonic() - 0.05
        result = Owl._frame_capture_time({'SensorTimestamp': int(captured * 1e9)})
        assert result == pytest.approx(captured, abs=0.005)

    def test_implausible_sensor_timestamp_falls_back_to_now(self):
        before = time.monotonic()
        result = Owl._frame_capture_time({'SensorTimestamp': int(time.time() * 1e9)})
        assert before <= result <= time.monotonic()

    @pytest.mark.parametrize('metadata', [None, {}])
    def test_no_metadata_uses_now(self, metadata):
        before = time.monotonic()
        assert before <= Owl._frame_capture_time(metadata) <= time.monotonic()


@pytest.mark.unit
class TestDetectionLoopOrder:

    @pytest.fixture
    def src(self):
        return (REPO / 'owl.py').read_text(encoding='utf-8')

    def _method(self, src, name):
        body = src.split(f'    def {name}(self', 1)[1]
        return body.split('\n    def ', 1)[0]

    def test_relays_scheduled_before_side_work(self, src):
        hoot = self._method(src, 'hoot')
        first_receive = hoot.index('self.relay_controller.receive(')
        assert hoot.index('weed_detect_indicator()') > first_receive
        assert hoot.index('draw_boxes(') > first_receive
        assert hoot.count('delay=delay_used') == 2
        assert 'time.time()\n                        for i in range(self.relay_num)' not in hoot

    def test_system_stats_gathered_off_the_detection_loop(self, src):
        assert 'get_system_stats()' not in self._method(src, 'hoot')
        assert 'update_system_stats(self.get_system_stats())' in self._method(src, 'update_state')
        assert 'cpu_percent(interval=None)' in self._method(src, 'get_system_stats')
