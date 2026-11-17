"""
The time a frame was taken (Owl._frame_capture_time).

The camera-to-nozzle delay counts from exposure. picamera2's SensorTimestamp
is used when it is plausible; webcams, replayed files and anything implausible
fall back to the hand-over time.

Everything else about spray timing on the OWL is measured at the relay on a
real Owl: tests/owl/test_gps_spray_latency.py (own GPS, controller hand-over,
phone GPS, speed warning) and tests/owl/test_relay_matrix.py (configured and
controller timing, late frames). Until 3.27.5 this file tested
_update_spray_timing on a hand-built SimpleNamespace and grepped owl.py for
the order of two lines.
"""

import time

import pytest

from owl import Owl


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
