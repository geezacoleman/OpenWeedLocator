"""
The GPS status LED and the single status LED of the switch-panel indicators.

What this file used to "cover" and where it is really tested now:

- Owl._get_best_gps_data (serial beats dashboard, the 3 s stale threshold)
  and Owl._serial_gps_reader (read error, nRF91 restart, silence): on a real
  Owl in tests/owl/test_gps_sources.py. The old tests here re-typed both
  functions inside the test file and never ran the production code.
- NMEA parsing: tests/unit/test_nmea_parser.py. GPSState (fix, staleness,
  speed gates): tests/unit/test_gps_manager.py.
"""

import configparser
import time
from pathlib import Path

import pytest

from utils.config_manager import ConfigValidator
from utils.error_manager import ConfigValueError
from utils.gps_manager import GPSState
from utils.output_manager import (
    AdvancedIndicatorState, AdvancedStatusIndicator, GPSLEDState, GPSStatusLED,
    UteStatusIndicator,
)

REPO = Path(__file__).resolve().parents[2]


def _nmea(body):
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f'${body}*{checksum:02X}'


# ---------------------------------------------------------------------------
# GPS LED: what the pin does in each state
# ---------------------------------------------------------------------------

class _Pin:
    """Stands in for gpiozero.LED: records every call the LED thread makes."""

    def __init__(self):
        self.calls = []

    def on(self):
        self.calls.append('on')

    def off(self):
        self.calls.append('off')

    def blink(self, on_time, off_time, n, background):
        self.calls.append(('blink', on_time, off_time, n, background))
        time.sleep(0.02)


class TestGpsLedPatterns:
    """The farmer reads GPS health off this LED: off = GPS disabled, solid =
    fix, one flash a second = searching, double flash = receiver lost. The
    old tests set the state and read the same attribute back, so a FIX that
    blinked like an ERROR passed."""

    @pytest.fixture
    def led(self):
        indicator = GPSStatusLED(pin='BOARD38')
        indicator.led = _Pin()
        yield indicator
        indicator.stop()

    def _calls_in(self, led, state):
        led.set_state(state)
        time.sleep(0.7)                       # the OFF loop re-reads the state every 0.5 s
        calls = led.led.calls
        while calls and calls[0] == 'off':    # the initial OFF loop(s)
            calls = calls[1:]
        return calls

    def test_starts_off(self, led):
        time.sleep(0.7)
        assert led.led.calls and set(led.led.calls) == {'off'}

    def test_fix_is_solid_on(self, led):
        calls = self._calls_in(led, GPSLEDState.FIX)
        assert calls and set(calls) == {'on'}, calls

    def test_acquiring_is_one_flash_a_second(self, led):
        calls = self._calls_in(led, GPSLEDState.ACQUIRING)
        assert calls and set(calls) == {('blink', 0.15, 0.85, 1, False)}, calls

    def test_error_is_a_double_short_flash(self, led):
        calls = self._calls_in(led, GPSLEDState.ERROR)
        assert calls == [('blink', 0.1, 0.1, 2, False)], calls   # then a 1.5 s pause

    def test_stop_ends_the_thread_and_leaves_the_led_off(self):
        indicator = GPSStatusLED(pin='BOARD38')
        indicator.led = pin = _Pin()
        indicator.set_state(GPSLEDState.FIX)
        time.sleep(0.7)
        assert pin.calls[-1] == 'on'
        indicator.stop()
        assert pin.calls[-1] == 'off'
        assert not indicator._thread.is_alive()

    def test_default_pin_is_board38(self):
        """BOARD37 (GPIO26) is the Sixfab HAT's / the nRF9151's power pin."""
        indicator = GPSStatusLED()
        try:
            assert indicator.led.pin == 'BOARD38'
        finally:
            indicator.stop()


# ---------------------------------------------------------------------------
# GPSState: one case the other GPS files do not have
# ---------------------------------------------------------------------------

def test_gsv_satellites_in_view_do_not_replace_satellites_used():
    """Multi-GNSS receivers interleave per-constellation GSV sentences; a
    trailing BeiDou '0 in view' used to clobber the displayed count to 0."""
    state = GPSState()
    assert state.update_from_line(_nmea(
        'GPGGA,123519,3347.1234,S,15101.5678,E,1,12,0.8,100.5,M,0.0,M,,')) == 'GGA'
    assert state.update_from_line(_nmea('GPGSV,1,1,00')) == 'GSV'
    snapshot = state.get_dict()
    assert snapshot['satellites'] == 12
    assert snapshot['fix_valid'] is True
    assert (snapshot['hdop'], snapshot['altitude']) == (0.8, 100.5)
    assert snapshot['fix_quality'] == 1


def test_fix_quality_follows_gga():
    """The app's GPS sheet names the fix (GPS, DGPS, RTK) from GGA field 6."""
    state = GPSState()
    assert state.get_dict()['fix_quality'] is None
    state.update_from_line(_nmea(
        'GPGGA,123519,3347.1234,S,15101.5678,E,4,14,0.6,100.5,M,0.0,M,,'))
    assert state.get_dict()['fix_quality'] == 4
    state.update_from_line(_nmea(
        'GPGGA,123520,3347.1234,S,15101.5678,E,0,03,9.9,100.5,M,0.0,M,,'))
    assert state.get_dict()['fix_quality'] == 0


# ---------------------------------------------------------------------------
# Ute status indicator: one LED, the Advanced state machine
# ---------------------------------------------------------------------------

class TestUteStatusIndicator:

    @pytest.fixture
    def indicator(self):
        built = UteStatusIndicator(save_directory=None)
        yield built
        built.stop()

    def test_one_led_on_board40_by_default(self, indicator):
        """Not BOARD37, and not the two LEDs of the old Ute indicator."""
        assert indicator.led.pin == 'BOARD40'
        assert not hasattr(indicator, 'record_LED') and not hasattr(indicator, 'storage_LED')

    def test_advanced_indicator_default_is_board40_too(self):
        built = AdvancedStatusIndicator(save_directory=None)
        try:
            assert built.led.pin == 'BOARD40'
        finally:
            built.stop()

    def test_state_follows_detection_and_recording(self, indicator):
        assert indicator.state == AdvancedIndicatorState.IDLE
        indicator.enable_weed_detection()
        assert indicator.state == AdvancedIndicatorState.DETECTING
        indicator.enable_image_recording()
        assert indicator.state == AdvancedIndicatorState.RECORDING_AND_DETECTING
        indicator.disable_weed_detection()
        assert indicator.state == AdvancedIndicatorState.RECORDING
        indicator.disable_image_recording()
        assert indicator.state == AdvancedIndicatorState.IDLE


# ---------------------------------------------------------------------------
# Config validator: a pin shared by two switches only matters for the
# controller type that uses both
# ---------------------------------------------------------------------------

class TestConfigValidatorPinConflicts:

    def _config(self, tmp_path, **controller):
        config = configparser.ConfigParser()
        assert config.read(REPO / 'config' / 'GENERAL_CONFIG.ini')
        for key, value in controller.items():
            config.set('Controller', key, value)
        path = tmp_path / 'pins.ini'
        with open(path, 'w') as f:
            config.write(f)
        return path

    def test_ute_ignores_the_advanced_controllers_pins(self, tmp_path):
        path = self._config(tmp_path, controller_type='ute', switch_pin='36',
                            detection_mode_pin_up='36')
        loaded = ConfigValidator.load_and_validate_config(path)
        assert loaded.get('Controller', 'switch_pin') == '36'

    def test_advanced_with_two_switches_on_one_pin_is_refused(self, tmp_path):
        path = self._config(tmp_path, controller_type='advanced', recording_pin='36',
                            detection_mode_pin_up='36')
        with pytest.raises(ConfigValueError) as refused:
            ConfigValidator.load_and_validate_config(path)
        assert '36' in str(refused.value)
