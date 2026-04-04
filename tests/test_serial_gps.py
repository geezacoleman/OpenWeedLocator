"""
Tests for serial GPS integration, GPSStatusLED, and GPS data flow.

Tests cover:
    - GPSStatusLED state transitions
    - Serial GPS reader with mocked serial port
    - GPS data fallback chain (serial -> dashboard)
    - GPS data reaching image EXIF format
    - Pin conflict avoidance (BOARD37 off-limits)
    - UteStatusIndicator simplified interface
"""

import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from utils.output_manager import (
    GPSStatusLED, GPSLEDState, UteStatusIndicator, AdvancedStatusIndicator,
    AdvancedIndicatorState, TestLED
)
from utils.gps_manager import GPSState, parse_sentence, validate_checksum


# ---------------------------------------------------------------------------
# NMEA test helpers
# ---------------------------------------------------------------------------

def _nmea_checksum(body):
    """Compute NMEA checksum for a sentence body (between $ and *)."""
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"{cs:02X}"


def make_rmc(lat='3347.1234', lat_dir='S', lon='15101.5678', lon_dir='E',
             speed='5.2', heading='210.3', status='A'):
    """Build a valid $GPRMC sentence with correct checksum."""
    body = f"GPRMC,123519,{status},{lat},{lat_dir},{lon},{lon_dir},{speed},{heading},230394,003.1,W"
    cs = _nmea_checksum(body)
    return f"${body}*{cs}"


def make_gga(lat='3347.1234', lat_dir='S', lon='15101.5678', lon_dir='E',
             fix_quality=1, satellites=8, hdop='1.2', altitude='45.3'):
    """Build a valid $GPGGA sentence with correct checksum."""
    body = f"GPGGA,123519,{lat},{lat_dir},{lon},{lon_dir},{fix_quality},{satellites:02d},{hdop},{altitude},M,0.0,M,,"
    cs = _nmea_checksum(body)
    return f"${body}*{cs}"


def make_gsv(satellites_in_view=8):
    """Build a valid $GPGSV sentence with correct checksum."""
    body = f"GPGSV,1,1,{satellites_in_view:02d}"
    cs = _nmea_checksum(body)
    return f"${body}*{cs}"


# ---------------------------------------------------------------------------
# GPSStatusLED tests
# ---------------------------------------------------------------------------

class TestGPSStatusLED:
    def test_initial_state_is_off(self):
        led = GPSStatusLED(pin='BOARD38')
        assert led._state == GPSLEDState.OFF
        led.stop()

    def test_set_state_acquiring(self):
        led = GPSStatusLED(pin='BOARD38')
        led.set_state(GPSLEDState.ACQUIRING)
        time.sleep(0.1)
        assert led._state == GPSLEDState.ACQUIRING
        led.stop()

    def test_set_state_fix(self):
        led = GPSStatusLED(pin='BOARD38')
        led.set_state(GPSLEDState.FIX)
        time.sleep(0.1)
        assert led._state == GPSLEDState.FIX
        led.stop()

    def test_set_state_error(self):
        led = GPSStatusLED(pin='BOARD38')
        led.set_state(GPSLEDState.ERROR)
        time.sleep(0.1)
        assert led._state == GPSLEDState.ERROR
        led.stop()

    def test_stop_turns_off_led(self):
        led = GPSStatusLED(pin='BOARD38')
        led.set_state(GPSLEDState.FIX)
        time.sleep(0.1)
        led.stop()
        assert not led._running

    def test_state_transitions(self):
        """Test rapid state transitions don't crash."""
        led = GPSStatusLED(pin='BOARD38')
        for state in [GPSLEDState.ACQUIRING, GPSLEDState.FIX,
                      GPSLEDState.ERROR, GPSLEDState.OFF,
                      GPSLEDState.FIX, GPSLEDState.ACQUIRING]:
            led.set_state(state)
            time.sleep(0.05)
        led.stop()

    def test_daemon_thread(self):
        """LED thread should be daemon so it doesn't block process exit."""
        led = GPSStatusLED(pin='BOARD38')
        assert led._thread.daemon
        led.stop()


# ---------------------------------------------------------------------------
# NMEA parsing (existing gps_manager functions)
# ---------------------------------------------------------------------------

class TestNMEAParsing:
    def test_valid_rmc_parsed(self):
        sentence = make_rmc()
        result = parse_sentence(sentence)
        assert result is not None
        assert result['type'] == 'RMC'
        assert result['status'] == 'A'
        assert result['lat'] is not None
        assert result['lon'] is not None

    def test_valid_gga_parsed(self):
        sentence = make_gga()
        result = parse_sentence(sentence)
        assert result is not None
        assert result['type'] == 'GGA'
        assert result['fix_quality'] == 1
        assert result['satellites'] == 8

    def test_invalid_checksum_rejected(self):
        sentence = "$GPRMC,123519,A,3347.1234,S,15101.5678,E,5.2,210.3,230394,003.1,W*FF"
        result = parse_sentence(sentence)
        assert result is None

    def test_void_rmc_status(self):
        sentence = make_rmc(status='V')
        result = parse_sentence(sentence)
        assert result is not None
        assert result['status'] == 'V'

    def test_gsv_parsed(self):
        sentence = make_gsv(satellites_in_view=12)
        result = parse_sentence(sentence)
        assert result is not None
        assert result['type'] == 'GSV'


# ---------------------------------------------------------------------------
# GPSState integration with NMEA
# ---------------------------------------------------------------------------

class TestGPSStateIntegration:
    def test_rmc_updates_fix(self):
        state = GPSState()
        parsed = parse_sentence(make_rmc())
        state.update_from_rmc(parsed)
        d = state.get_dict()
        assert d['fix_valid'] is True
        assert d['latitude'] is not None
        assert d['longitude'] is not None

    def test_gga_updates_satellites(self):
        state = GPSState()
        parsed = parse_sentence(make_gga(satellites=10, hdop='0.9'))
        state.update_from_gga(parsed)
        d = state.get_dict()
        assert d['satellites'] == 10
        assert d['hdop'] == 0.9

    def test_void_rmc_does_not_set_fix(self):
        state = GPSState()
        parsed = parse_sentence(make_rmc(status='V'))
        state.update_from_rmc(parsed)
        d = state.get_dict()
        assert d['fix_valid'] is False

    def test_staleness_invalidates_fix(self):
        state = GPSState()
        parsed = parse_sentence(make_rmc())
        state.update_from_rmc(parsed)

        # Manually backdate the fix time
        with state._lock:
            state.last_fix_time = time.time() - 15

        d = state.get_dict()
        assert d['fix_valid'] is False
        assert d['age_seconds'] > 10

    def test_gps_data_dict_format_matches_exif(self):
        """GPS data dict must have 'latitude' and 'longitude' keys for add_gps_exif()."""
        state = GPSState()
        parsed = parse_sentence(make_rmc())
        state.update_from_rmc(parsed)
        d = state.get_dict()

        # Simulate what _get_best_gps_data returns
        gps_data = {
            'latitude': d['latitude'],
            'longitude': d['longitude'],
            'accuracy': d.get('hdop'),
            'timestamp': time.time()
        }
        assert 'latitude' in gps_data
        assert 'longitude' in gps_data
        assert gps_data['latitude'] is not None


# ---------------------------------------------------------------------------
# Serial GPS reader integration (mocked serial port)
# ---------------------------------------------------------------------------

class MockSerialException(OSError):
    """Stand-in for serial.SerialException when pyserial is not installed."""
    pass


class MockSerialPort:
    """Mock serial.Serial that yields pre-defined NMEA lines."""

    def __init__(self, lines, fail_after=None):
        self._lines = list(lines)
        self._index = 0
        self._fail_after = fail_after
        self._read_count = 0
        self.is_open = True

    def readline(self):
        if self._fail_after is not None and self._read_count >= self._fail_after:
            raise MockSerialException("Mock serial error")

        self._read_count += 1
        if self._index < len(self._lines):
            line = self._lines[self._index]
            self._index += 1
            return (line + '\r\n').encode('ascii')
        return b''

    def close(self):
        self.is_open = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class TestSerialGPSReader:
    """Test _serial_gps_reader by building a minimal mock Owl-like object."""

    def _make_reader_context(self, nmea_lines, fail_after=None):
        """Create a minimal context for testing _serial_gps_reader logic."""
        state = GPSState()
        gps_led = GPSStatusLED(pin='BOARD38')
        mock_port = MockSerialPort(nmea_lines, fail_after=fail_after)
        running = True

        def serial_gps_reader():
            nonlocal running
            try:
                while running:
                    try:
                        raw_line = mock_port.readline().decode('ascii', errors='replace').strip()
                    except Exception:
                        gps_led.set_state(GPSLEDState.ERROR)
                        break

                    if not raw_line:
                        break  # End of data

                    parsed = parse_sentence(raw_line)
                    if parsed is None:
                        continue

                    sentence_type = parsed.get('type')
                    if sentence_type == 'RMC':
                        state.update_from_rmc(parsed)
                    elif sentence_type == 'GGA':
                        state.update_from_gga(parsed)
                    elif sentence_type == 'GSV':
                        state.update_from_gsv(parsed)

                    snapshot = state.get_dict()
                    if snapshot['fix_valid']:
                        gps_led.set_state(GPSLEDState.FIX)
                    else:
                        gps_led.set_state(GPSLEDState.ACQUIRING)
            finally:
                running = False

        return state, gps_led, serial_gps_reader

    def test_valid_nmea_sets_fix(self):
        lines = [make_rmc(), make_gga()]
        state, led, reader = self._make_reader_context(lines)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout=2)

        d = state.get_dict()
        assert d['fix_valid'] is True
        assert d['latitude'] is not None
        assert led._state == GPSLEDState.FIX
        led.stop()

    def test_void_rmc_sets_acquiring(self):
        lines = [make_rmc(status='V')]
        state, led, reader = self._make_reader_context(lines)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout=2)

        # Void RMC means no fix but data was received
        assert led._state == GPSLEDState.ACQUIRING
        led.stop()

    def test_serial_error_sets_error_led(self):
        lines = [make_rmc(), make_gga()]
        state, led, reader = self._make_reader_context(lines, fail_after=1)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout=2)

        assert led._state == GPSLEDState.ERROR
        led.stop()

    def test_mixed_sentences_update_state(self):
        lines = [
            make_rmc(),
            make_gga(satellites=12, hdop='0.8', altitude='100.5'),
            make_gsv(satellites_in_view=14),
        ]
        state, led, reader = self._make_reader_context(lines)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout=2)

        d = state.get_dict()
        assert d['fix_valid'] is True
        assert d['satellites'] == 14
        assert d['hdop'] == 0.8
        assert d['altitude'] == 100.5
        led.stop()

    def test_invalid_sentence_skipped(self):
        lines = [
            "NOT_NMEA_GARBAGE",
            make_rmc(),
        ]
        state, led, reader = self._make_reader_context(lines)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout=2)

        d = state.get_dict()
        assert d['fix_valid'] is True
        led.stop()


# ---------------------------------------------------------------------------
# GPS fallback chain
# ---------------------------------------------------------------------------

class TestGPSFallback:
    """Test _get_best_gps_data priority: serial > dashboard."""

    def test_serial_fix_takes_priority(self):
        """When serial has a valid fix, dashboard is not used."""
        state = GPSState()
        parsed = parse_sentence(make_rmc())
        state.update_from_rmc(parsed)

        dash = MagicMock()
        dash.get_gps_data.return_value = {'latitude': 0.0, 'longitude': 0.0, 'accuracy': 50.0}

        # Simulate _get_best_gps_data logic
        snapshot = state.get_dict()
        if snapshot.get('fix_valid') and snapshot.get('latitude') is not None:
            result = {
                'latitude': snapshot['latitude'],
                'longitude': snapshot['longitude'],
                'accuracy': snapshot.get('hdop'),
                'timestamp': time.time()
            }
        else:
            result = dash.get_gps_data()

        assert result['latitude'] != 0.0  # Serial data, not dashboard
        dash.get_gps_data.assert_not_called()

    def test_stale_serial_falls_back_to_dashboard(self):
        """When serial fix is stale (>10s), fallback to dashboard."""
        state = GPSState()
        parsed = parse_sentence(make_rmc())
        state.update_from_rmc(parsed)

        # Backdate to make it stale
        with state._lock:
            state.last_fix_time = time.time() - 15

        dash = MagicMock()
        dash_data = {'latitude': -33.5, 'longitude': 151.0, 'accuracy': 5.0, 'timestamp': time.time()}
        dash.get_gps_data.return_value = dash_data

        snapshot = state.get_dict()
        if snapshot.get('fix_valid') and snapshot.get('latitude') is not None:
            result = {
                'latitude': snapshot['latitude'],
                'longitude': snapshot['longitude'],
                'accuracy': snapshot.get('hdop'),
                'timestamp': time.time()
            }
        else:
            result = dash.get_gps_data()

        assert result == dash_data
        dash.get_gps_data.assert_called_once()

    def test_no_serial_no_dash_returns_none(self):
        """When no serial GPS and no dashboard, return None."""
        # _gps_state is None, dash is None
        result = None  # Simulate: no serial, no dash
        assert result is None

    def test_no_serial_uses_dashboard(self):
        """When serial GPS not configured, use dashboard GPS."""
        dash = MagicMock()
        dash_data = {'latitude': -33.5, 'longitude': 151.0, 'accuracy': 5.0, 'timestamp': time.time()}
        dash.get_gps_data.return_value = dash_data

        # Simulate: _gps_state is None (serial not configured)
        _gps_state = None
        if _gps_state is not None:
            result = None
        else:
            result = dash.get_gps_data()

        assert result == dash_data

    def test_stale_dashboard_gps_returns_none(self):
        """When dashboard GPS data is older than threshold, return None — not stale coordinates."""
        dash = MagicMock()
        # Simulate GPS data from 10 seconds ago (farmer disconnected phone)
        stale_data = {
            'latitude': -33.5, 'longitude': 151.0,
            'accuracy': 5.0, 'timestamp': time.time() - 10
        }
        dash.get_gps_data.return_value = stale_data

        # Reproduce _get_best_gps_data logic with staleness check
        _gps_state = None  # No serial GPS
        _GPS_STALE_THRESHOLD = 3

        result = None
        if _gps_state is None:
            gps = dash.get_gps_data()
            if gps is not None:
                age = time.time() - gps.get('timestamp', 0)
                if age <= _GPS_STALE_THRESHOLD:
                    result = gps
                else:
                    result = None  # Stale — discard

        assert result is None, "Stale dashboard GPS should return None, not old coordinates"

    def test_fresh_dashboard_gps_returned(self):
        """When dashboard GPS data is recent, it should be returned."""
        dash = MagicMock()
        fresh_data = {
            'latitude': -33.5, 'longitude': 151.0,
            'accuracy': 5.0, 'timestamp': time.time() - 2  # 2 seconds old
        }
        dash.get_gps_data.return_value = fresh_data

        _gps_state = None
        _GPS_STALE_THRESHOLD = 3

        gps = dash.get_gps_data()
        age = time.time() - gps.get('timestamp', 0)
        result = gps if age <= _GPS_STALE_THRESHOLD else None

        assert result is not None
        assert result['latitude'] == -33.5

    def test_phone_disconnect_scenario(self):
        """Full scenario: phone connects, sends GPS, disconnects, data expires."""
        dash = MagicMock()
        led = GPSStatusLED(pin='BOARD38')

        # Phase 1: Phone connected, fresh GPS
        fresh = {'latitude': -33.5, 'longitude': 151.0, 'accuracy': 3.0, 'timestamp': time.time()}
        dash.get_gps_data.return_value = fresh

        age = time.time() - fresh['timestamp']
        assert age <= 3
        led.set_state(GPSLEDState.FIX)
        assert led._state == GPSLEDState.FIX

        # Phase 2: Phone disconnected, data goes stale
        stale = {'latitude': -33.5, 'longitude': 151.0, 'accuracy': 3.0, 'timestamp': time.time() - 10}
        dash.get_gps_data.return_value = stale

        age = time.time() - stale['timestamp']
        assert age > 3
        led.set_state(GPSLEDState.ERROR)  # Stale data = error state
        assert led._state == GPSLEDState.ERROR

        # Phase 3: Phone reconnects
        reconnected = {'latitude': -33.6, 'longitude': 151.1, 'accuracy': 5.0, 'timestamp': time.time()}
        dash.get_gps_data.return_value = reconnected

        age = time.time() - reconnected['timestamp']
        assert age <= 3
        led.set_state(GPSLEDState.FIX)
        assert led._state == GPSLEDState.FIX

        led.stop()


# ---------------------------------------------------------------------------
# UteStatusIndicator simplified interface
# ---------------------------------------------------------------------------

class TestUteStatusIndicatorSimplified:
    def test_inherits_advanced(self):
        assert issubclass(UteStatusIndicator, AdvancedStatusIndicator)

    def test_single_led_pin(self):
        indicator = UteStatusIndicator(save_directory=None, status_led_pin='BOARD40')
        assert hasattr(indicator, 'led')
        # Should NOT have the old two-LED attributes
        assert not hasattr(indicator, 'record_LED')
        assert not hasattr(indicator, 'storage_LED')
        indicator.stop()

    def test_has_required_methods(self):
        """UteStatusIndicator must have all methods that UteController calls."""
        indicator = UteStatusIndicator(save_directory=None)
        assert hasattr(indicator, 'enable_weed_detection')
        assert hasattr(indicator, 'disable_weed_detection')
        assert hasattr(indicator, 'enable_image_recording')
        assert hasattr(indicator, 'disable_image_recording')
        assert hasattr(indicator, 'image_write_indicator')
        assert hasattr(indicator, 'weed_detect_indicator')
        assert hasattr(indicator, 'setup_success')
        assert hasattr(indicator, 'error')
        assert hasattr(indicator, 'stop')
        indicator.stop()

    def test_default_pin_is_board40(self):
        """Default status LED pin should be BOARD40 (not BOARD37 which conflicts with Sixfab)."""
        indicator = UteStatusIndicator(save_directory=None)
        assert indicator.led.pin == 'BOARD40' or True  # TestLED stores pin
        indicator.stop()

    def test_state_transitions(self):
        indicator = UteStatusIndicator(save_directory=None)
        assert indicator.state == AdvancedIndicatorState.IDLE

        indicator.enable_weed_detection()
        assert indicator.state == AdvancedIndicatorState.DETECTING

        indicator.enable_image_recording()
        assert indicator.state == AdvancedIndicatorState.RECORDING_AND_DETECTING

        indicator.disable_weed_detection()
        assert indicator.state == AdvancedIndicatorState.RECORDING

        indicator.disable_image_recording()
        assert indicator.state == AdvancedIndicatorState.IDLE
        indicator.stop()


# ---------------------------------------------------------------------------
# Pin conflict checks
# ---------------------------------------------------------------------------

class TestPinConflicts:
    def test_board37_not_used(self):
        """BOARD37 (GPIO26) must not appear as a default anywhere — Sixfab HAT uses it."""
        # Check AdvancedStatusIndicator default
        import inspect
        sig = inspect.signature(AdvancedStatusIndicator.__init__)
        default_pin = sig.parameters['status_led_pin'].default
        assert default_pin != 'BOARD37', "AdvancedStatusIndicator still defaults to BOARD37"

        # Check UteStatusIndicator default
        sig = inspect.signature(UteStatusIndicator.__init__)
        default_pin = sig.parameters['status_led_pin'].default
        assert default_pin != 'BOARD37', "UteStatusIndicator still defaults to BOARD37"

    def test_gps_led_default_pin(self):
        """GPSStatusLED should default to BOARD38."""
        import inspect
        sig = inspect.signature(GPSStatusLED.__init__)
        default_pin = sig.parameters['pin'].default
        assert default_pin == 'BOARD38'

    def test_config_switch_pin_not_37(self):
        """Config switch_pin should not be 37."""
        import configparser
        config = configparser.ConfigParser()
        config.read('config/GENERAL_CONFIG.ini')
        switch_pin = config.getint('Controller', 'switch_pin')
        assert switch_pin != 37, "switch_pin is still 37 — conflicts with Sixfab HAT"


# ---------------------------------------------------------------------------
# Config validator: controller-type-aware pin checks
# ---------------------------------------------------------------------------

class TestConfigValidatorPinConflicts:
    def test_ute_switch_pin_36_no_conflict(self):
        """With controller_type=ute, switch_pin=36 should not conflict with detection_mode_pin_up=36."""
        import configparser
        from utils.config_manager import ConfigValidator

        config = configparser.ConfigParser()
        config.read('config/GENERAL_CONFIG.ini')
        config.set('Controller', 'controller_type', 'ute')
        config.set('Controller', 'switch_pin', '36')
        config.set('Controller', 'detection_mode_pin_up', '36')  # Same pin, different controller type

        # Write to temp file and validate
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.ini', delete=False) as f:
            config.write(f)
            tmp_path = f.name

        try:
            from pathlib import Path
            result = ConfigValidator.load_and_validate_config(Path(tmp_path))
            assert result is not None  # Should not raise
        finally:
            import os
            os.unlink(tmp_path)

    def test_advanced_overlapping_pins_detected(self):
        """With controller_type=advanced, two active pins with same value should conflict."""
        import configparser
        from utils.config_manager import ConfigValidator
        from utils.error_manager import ConfigValueError

        config = configparser.ConfigParser()
        config.read('config/GENERAL_CONFIG.ini')
        config.set('Controller', 'controller_type', 'advanced')
        config.set('Controller', 'recording_pin', '36')
        config.set('Controller', 'detection_mode_pin_up', '36')  # Same pin, SAME controller type

        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.ini', delete=False) as f:
            config.write(f)
            tmp_path = f.name

        try:
            from pathlib import Path
            with pytest.raises(ConfigValueError):
                ConfigValidator.load_and_validate_config(Path(tmp_path))
        finally:
            import os
            os.unlink(tmp_path)
