"""Shutter cap and gain limit for data collection at speed.

Auto exposure picks the shutter time for brightness only. At 10 km/h a 2 ms
shutter smears the image by 5.6 mm on the ground, which blurs small weeds.
The governor caps the shutter so ground movement during the exposure stays
under `smear_limit_mm`, and lets the sensor gain rise instead.

Ground smear is speed x exposure time, so no camera height or lens data is
needed (boom height changes in the field anyway).

The governor only decides. It reads the camera's own per-frame metadata
(ExposureTime, AnalogueGain), and returns an override for the camera:

    {'exposure_us': int | None, 'gain': float | None}

None means "automatic" for that control. The camera layer turns this into
libcamera controls (see Picamera2Stream.set_exposure_override).

Fail-safe rules:
  * no fresh GPS speed, or speed below MIN_SPEED_KMH -> release to auto
  * the cap never goes below MIN_EXPOSURE_US (1/8000 s)
  * the cap is released when the sensor has sat at minimum gain under the
    cap for RELEASE_HOLD_S, i.e. the scene is bright enough that auto
    exposure would choose a shorter shutter anyway; re-applied with
    hysteresis when auto drifts over it
  * the caller passes cap_allowed=False whenever the OWL is not collecting
    (detecting, or not recording): the cap releases, the gain limit stays

libcamera applies a new control 2-3 frames late, so the frames straight
after the cap is set still report the old long exposure at unity gain, and
the AGC then needs a few more frames to raise the gain. Releasing on those
frames made the cap flap on and off every few frames in daylight, hence
"the cap has taken effect" and the hold time.
"""

import math
import time

MIN_EXPOSURE_US = 125           # 1/8000 s
MIN_SPEED_KMH = 0.5
UNITY_GAIN = 1.05               # "at minimum gain" with a little headroom
HYSTERESIS = 1.15               # re-cap only when auto exposure is 15 % over
REAPPLY_CHANGE = 0.10           # re-send the cap when it moves more than 10 %
GAIN_RECHECK_S = 15.0           # let AGC re-measure a clamped gain this often
CAP_EFFECTIVE = 1.05            # metadata exposure within 5 % of the cap = cap applied
RELEASE_HOLD_S = 1.0            # unity gain under the cap this long before release


def ground_smear_mm(speed_kmh, exposure_us):
    """Ground distance travelled during one exposure, in mm."""
    if speed_kmh is None or exposure_us is None:
        return None
    return round(speed_kmh / 3.6 * exposure_us / 1000.0, 2)


def _finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"not a finite number: {value}")
    return value


def parse_max_gain(value):
    """[Camera] max_gain -> float, or None for automatic ('auto', 'none',
    'off' or blank). Raises ValueError for anything else."""
    text = str(value if value is not None else '').strip().lower()
    if text in ('', 'auto', 'none', 'off'):
        return None
    return min(64.0, max(1.0, _finite(text)))


def shutter_cap_us(speed_kmh, smear_limit_mm):
    """Longest exposure (µs) that keeps ground smear under the limit, or
    None when there is no meaningful speed."""
    if speed_kmh is None or speed_kmh < MIN_SPEED_KMH or not smear_limit_mm or smear_limit_mm <= 0:
        return None
    cap = smear_limit_mm / (speed_kmh / 3.6) * 1000.0
    return max(MIN_EXPOSURE_US, int(cap))


class ExposureGovernor:
    def __init__(self, smear_limit_mm=2.0, cap_enabled=True, max_gain=None):
        self.smear_limit_mm = smear_limit_mm
        self.cap_enabled = cap_enabled
        self.max_gain = max_gain
        self.exposure_us = None   # current override, None = auto
        self.gain = None
        self._gain_clamped_at = None
        self._unity_since = None

    def configure(self, smear_limit_mm=None, cap_enabled=None, max_gain=None, clear_max_gain=False):
        # Same bounds as the config validator; inf/nan rejected outright.
        if smear_limit_mm is not None:
            self.smear_limit_mm = min(20.0, max(0.1, _finite(smear_limit_mm)))
        if cap_enabled is not None:
            self.cap_enabled = bool(cap_enabled)
        if clear_max_gain:
            self.max_gain = None
        elif max_gain is not None:
            self.max_gain = min(64.0, max(1.0, _finite(max_gain)))

    @property
    def active(self):
        return self.exposure_us is not None or self.gain is not None

    def update(self, metadata, speed_kmh, now=None, cap_allowed=True):
        """Feed one frame's metadata and the fresh GPS speed (None if stale).
        cap_allowed=False releases the shutter cap but keeps the gain limit.

        Returns the new override dict when it changed, else None.
        """
        now = time.monotonic() if now is None else now
        exposure = (metadata or {}).get('ExposureTime')
        gain = (metadata or {}).get('AnalogueGain')
        before = (self.exposure_us, self.gain)

        cap = (shutter_cap_us(speed_kmh, self.smear_limit_mm)
               if self.cap_enabled and cap_allowed else None)
        if cap is None:
            self.exposure_us = None
        elif self.exposure_us is None:
            if exposure is not None and exposure > cap * HYSTERESIS:
                self.exposure_us = cap
        elif abs(cap - self.exposure_us) > self.exposure_us * REAPPLY_CHANGE:
            self.exposure_us = cap
            self._unity_since = None
        elif (exposure is not None and exposure <= self.exposure_us * CAP_EFFECTIVE
              and gain is not None and gain <= UNITY_GAIN and self.gain is None):
            # Running at the cap and still at minimum gain: bright enough that
            # auto would pick a shorter shutter. Hand back once it has held.
            if self._unity_since is None:
                self._unity_since = now
            elif now - self._unity_since >= RELEASE_HOLD_S:
                self.exposure_us = None
        else:
            self._unity_since = None
        if self.exposure_us is None:
            self._unity_since = None

        if self.max_gain is None:
            self.gain = None
            self._gain_clamped_at = None
        elif self.gain is None:
            if gain is not None and gain > self.max_gain:
                self.gain = self.max_gain
                self._gain_clamped_at = now
        elif now - self._gain_clamped_at >= GAIN_RECHECK_S:
            # Release so the AGC can say whether it still wants more gain.
            self.gain = None
            self._gain_clamped_at = None

        if (self.exposure_us, self.gain) != before:
            return {'exposure_us': self.exposure_us, 'gain': self.gain}
        return None

    def release(self):
        """Back to full auto. Returns the override when anything was held."""
        if not self.active:
            return None
        self.exposure_us = None
        self.gain = None
        self._gain_clamped_at = None
        self._unity_since = None
        return {'exposure_us': None, 'gain': None}
