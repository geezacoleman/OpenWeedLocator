"""
Speed-adaptive spray timing, shared by the networked controller and any OWL
timing itself from its own GPS (owl.py).

    duration = patch length / speed, stretched to cover the ground travelled
               between frames (at most MAX_STRETCH patches)
    delay    = camera-to-nozzle offset / speed

speed_warning() turns the same numbers into the farmer-facing message shown
when forward speed is causing late or missed sprays.
"""

import threading
import time
from collections import deque

# How long the last good GPS average may still be used after the fix drops.
# Past this the speed is unknown and actuation falls back to the configured
# values: a boom that keeps spraying for a speed the tractor left behind is
# worse than one that reverts to a fixed duration.
GPS_STALE_AFTER_S = 30


class SpeedAverager:
    """Time-windowed moving average of GPS speed."""

    def __init__(self, window_seconds=5.0):
        self.window_seconds = window_seconds
        self._samples = deque()  # (timestamp, speed_kmh)
        self._lock = threading.Lock()
        self._last_valid_avg = None
        self._last_sample_time = None

    def add_sample(self, speed_kmh):
        """Add a speed sample and return current average."""
        now = time.time()
        with self._lock:
            self._samples.append((now, speed_kmh))
            self._prune()
            avg = self._compute_avg()
            if avg is not None:
                self._last_valid_avg = avg
            self._last_sample_time = now
            return avg

    def get_average(self):
        """Current window average without adding a sample."""
        with self._lock:
            self._prune()
            return self._compute_avg()

    def get_fallback_speed(self):
        """Last known valid average for GPS dropout."""
        with self._lock:
            return self._last_valid_avg

    def seconds_since_update(self):
        """Time since last GPS sample."""
        with self._lock:
            if self._last_sample_time is None:
                return None
            return time.time() - self._last_sample_time

    def _prune(self):
        """Remove samples older than the window."""
        cutoff = time.time() - self.window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def _compute_avg(self):
        """Compute average from current window."""
        if not self._samples:
            return None
        total = sum(s[1] for s in self._samples)
        return total / len(self._samples)


class ActuationCalculator:
    """Pure math: converts speed + geometry into actuation timing."""

    MIN_SPEED = 0.5   # km/h — below this, use fallback
    MIN_DURATION = 0.01  # seconds floor
    MAX_DURATION = 5.0   # seconds cap
    # A patch shorter than the ground covered between frames leaves unsprayed
    # strips, so the spray stretches to cover the gap, up to this many patches.
    MAX_STRETCH = 3.0

    def __init__(self, actuation_length_cm=10.0, offset_cm=30.0,
                 fallback_duration=0.15, fallback_delay=0.0):
        self.actuation_length_cm = actuation_length_cm
        self.offset_cm = offset_cm
        self.fallback_duration = fallback_duration
        self.fallback_delay = fallback_delay

    def compute(self, speed_kmh, loop_time_ms=0.0):
        """Compute actuation params from speed and the detection loop time.

        Returns dict with actuation_duration, delay, source, speed_used,
        gap_cm (ground covered per frame), stretched and coverage_ok.
        """
        if speed_kmh is None or speed_kmh < self.MIN_SPEED:
            return {
                'actuation_duration': self.fallback_duration,
                'delay': self.fallback_delay,
                'source': 'config',
                'speed_used': 0.0,
                'gap_cm': 0.0,
                'stretched': False,
                'coverage_ok': True,
            }

        speed_m_s = speed_kmh / 3.6
        gap_cm = frame_gap_cm(speed_kmh, loop_time_ms)
        cap_cm = self.MAX_STRETCH * self.actuation_length_cm
        spray_cm = min(max(self.actuation_length_cm, gap_cm), cap_cm)

        duration = (spray_cm / 100.0) / speed_m_s
        delay = (self.offset_cm / 100.0) / speed_m_s

        # Clamp to safety bounds
        duration = max(self.MIN_DURATION, min(self.MAX_DURATION, duration))
        delay = max(0.0, min(self.MAX_DURATION, delay))

        return {
            'actuation_duration': round(duration, 4),
            'delay': round(delay, 4),
            'source': 'gps',
            'speed_used': round(speed_kmh, 2),
            'gap_cm': round(gap_cm, 1),
            'stretched': spray_cm > self.actuation_length_cm,
            'coverage_ok': gap_cm <= cap_cm,
        }

    def check_coverage(self, speed_kmh, avg_loop_time_ms):
        """Check whether the (stretched) spray still covers the gap between frames.

        Returns dict with coverage_ok, min_gap_cm, message.
        """
        if speed_kmh is None or speed_kmh < self.MIN_SPEED or avg_loop_time_ms <= 0:
            return {'coverage_ok': True, 'min_gap_cm': 0.0, 'message': ''}

        min_gap_cm = frame_gap_cm(speed_kmh, avg_loop_time_ms)
        coverage_ok = min_gap_cm <= self.MAX_STRETCH * self.actuation_length_cm
        message = '' if coverage_ok else speed_warning(
            speed_kmh, self.actuation_length_cm, self.offset_cm, avg_loop_time_ms)

        return {
            'coverage_ok': coverage_ok,
            'min_gap_cm': round(min_gap_cm, 1),
            'message': message
        }


def frame_gap_cm(speed_kmh, loop_time_ms):
    """Ground travelled between two detection frames, in cm."""
    return (speed_kmh / 3.6) * (max(loop_time_ms, 0.0) / 1000.0) * 100.0


def speed_warning(speed_kmh, actuation_length_cm, offset_cm,
                  loop_time_ms=0.0, lag_s=0.0, late_s=0.0):
    """Farmer-facing warning when forward speed causes missed or late sprays.

    speed_kmh: speed the timing uses. loop_time_ms: average detection loop.
    lag_s: average frame-capture-to-relay time. late_s: average amount a spray
    started after the weed reached the nozzle (lag beyond the travel delay).
    Returns '' when speed is not a problem.
    """
    if not speed_kmh or speed_kmh < ActuationCalculator.MIN_SPEED:
        return ''
    speed_m_s = speed_kmh / 3.6

    cap_cm = ActuationCalculator.MAX_STRETCH * actuation_length_cm
    loop_s = max(loop_time_ms, 0.0) / 1000.0
    if loop_s > 0 and frame_gap_cm(speed_kmh, loop_time_ms) > cap_cm:
        limit = (cap_cm / 100.0) / loop_s * 3.6
        return f"Moving too fast for the camera. Slow to {limit:.0f} km/h"

    late_cm = late_s * speed_m_s * 100.0
    if late_cm > actuation_length_cm / 2.0:
        if offset_cm > 0 and lag_s > 0:
            limit = (offset_cm / 100.0) / lag_s * 3.6
            return f"Sprays landing about {late_cm:.0f} cm late. Slow to {limit:.0f} km/h"
        return (f"Sprays landing about {late_cm:.0f} cm late. "
                f"Move the nozzles further back or slow down")
    return ''
