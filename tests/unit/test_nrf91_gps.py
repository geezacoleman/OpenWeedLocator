"""
nRF9151 GPS source (Nordic Serial LTE Modem on the production CM5 carrier).

Covers the '#XGPS' parser, GPSState handling, the AT start sequence against a
scripted fake modem, and the owl.py / setup-script wiring (source inspection:
owl.py can't be imported on Windows).
"""

from pathlib import Path

import pytest

from utils.gps_manager import (
    GPSManager, GPSState, MS_TO_KMH, nrf91_command, nrf91_start_gps, parse_xgps,
)

REPO = Path(__file__).resolve().parents[2]
FIX = '#XGPS: -33.786234,151.026130,45.3,4.2,2.5,210.0,"2026-09-13 06:25:48"'


def _nmea(body):
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f"${body}*{checksum:02X}"


RMC = _nmea('GPRMC,123519,A,3347.1234,S,15101.5678,E,5.2,210.3,230394,003.1,W')


class FakeModem:
    """pyserial stand-in: records commands, replies per command (default OK)."""

    def __init__(self, replies=None, silent=False):
        self.replies = replies or {}
        self.silent = silent
        self.written = []
        self._queue = []

    def write(self, data):
        command = data.decode('ascii').strip()
        self.written.append(command)
        if not self.silent:
            self._queue.extend(self.replies.get(command, ['OK']))

    def readline(self):
        if self._queue:
            return (self._queue.pop(0) + '\r\n').encode('ascii')
        return b''


@pytest.mark.unit
class TestParseXgps:

    def test_fix_line(self):
        d = parse_xgps(FIX)
        assert d['type'] == 'XGPS'
        assert d['lat'] == pytest.approx(-33.786234)
        assert d['lon'] == pytest.approx(151.02613)
        assert d['altitude'] == pytest.approx(45.3)
        assert d['accuracy_m'] == pytest.approx(4.2)
        assert d['speed_kmh'] == pytest.approx(2.5 * MS_TO_KMH)
        assert d['heading'] == pytest.approx(210.0)
        assert d['time_utc'] == '062548'
        assert d['date'] == '130926'

    def test_serial_modem_addon_prefix(self):
        assert parse_xgps(FIX.replace('#XGPS:', '#XGNSSPOS:'))['type'] == 'XGPS'

    def test_status_line(self):
        assert parse_xgps('#XGPS: 1,1') == {'type': 'XGPS_STATUS', 'status': 1}

    def test_fractional_seconds_dropped(self):
        assert parse_xgps(FIX.replace('06:25:48', '06:25:48.250'))['time_utc'] == '062548'

    def test_bad_timestamp_keeps_position_omits_time(self):
        d = parse_xgps(FIX.replace('"2026-09-13 06:25:48"', '"garbage"'))
        assert d['lat'] is not None
        assert d['time_utc'] is None
        assert d['date'] is None

    @pytest.mark.parametrize('line', [
        '', 'OK', 'Ready', RMC, '#XGPS: 1', '#XGPS: a,b,c,d,e,f,g',
        '#XGPS: ,151.0,1,1,1,1,"2026-09-13 06:25:48"',
    ])
    def test_rejects(self, line):
        assert parse_xgps(line) is None


@pytest.mark.unit
class TestGPSStateLines:

    def test_xgps_fix_sets_state_without_inventing_dop(self):
        state = GPSState()
        assert state.update_from_line(FIX) == 'XGPS'
        d = state.get_dict()
        assert d['fix_valid'] is True
        assert d['accuracy_m'] == 4.2
        assert d['speed_kmh'] == 9.0
        assert d['utc_time'] == '062548'
        assert d['hdop'] is None
        assert d['satellites'] is None

    def test_status_line_is_not_a_fix(self):
        state = GPSState()
        assert state.update_from_line('#XGPS: 1,1') == 'XGPS_STATUS'
        assert state.get_dict()['fix_valid'] is False

    def test_nmea_still_routed(self):
        state = GPSState()
        assert state.update_from_line(RMC) == 'RMC'
        d = state.get_dict()
        assert d['fix_valid'] is True
        assert d['accuracy_m'] is None

    def test_unknown_line(self):
        assert GPSState().update_from_line('Ready') is None

    def test_gps_manager_accepts_xgps(self):
        mgr = GPSManager(source='tcp', port=18599)
        mgr._on_line(FIX)
        assert mgr.get_state()['fix']['fix_valid'] is True


@pytest.mark.unit
class TestNrf91Commands:

    def test_ok(self):
        modem = FakeModem()
        assert nrf91_command(modem, 'AT', timeout=0.5) is True
        assert modem.written == ['AT']

    def test_error(self):
        modem = FakeModem({'AT#XGPS=1,0,1': ['ERROR']})
        assert nrf91_command(modem, 'AT#XGPS=1,0,1', timeout=0.5) is False

    def test_timeout(self):
        assert nrf91_command(FakeModem(silent=True), 'AT', timeout=0.1) is False

    def test_echo_ignored(self):
        assert nrf91_command(FakeModem({'AT': ['AT', 'OK']}), 'AT', timeout=0.5) is True

    def test_fix_arriving_mid_command_reaches_callback(self):
        seen = []
        modem = FakeModem({'AT': [FIX, 'OK']})
        assert nrf91_command(modem, 'AT', timeout=0.5, on_line=seen.append) is True
        assert seen == [FIX]

    def test_start_sequence(self):
        modem = FakeModem()
        assert nrf91_start_gps(modem, ping_timeout=0.2) == 'AT#XGPS'
        assert modem.written[0] == 'AT'
        mode = [c for c in modem.written
                if c in ('AT+CFUN=0', 'AT%XSYSTEMMODE=0,0,1,0', 'AT+CFUN=31')]
        assert mode == ['AT+CFUN=0', 'AT%XSYSTEMMODE=0,0,1,0', 'AT+CFUN=31']
        # GPS stopped before the mode change, started last
        assert modem.written.index('AT#XGPS=0') < modem.written.index('AT+CFUN=0')
        assert modem.written[-1] == 'AT#XGPS=1,0,1'

    def test_falls_back_to_serial_modem_addon_command(self):
        modem = FakeModem({'AT#XGPS=1,0,1': ['ERROR']})
        assert nrf91_start_gps(modem, ping_timeout=0.2) == 'AT#XGNSS'

    def test_silent_modem_gives_up_after_three_pings(self):
        modem = FakeModem(silent=True)
        assert nrf91_start_gps(modem, ping_timeout=0.05) is None
        assert modem.written == ['AT', 'AT', 'AT']


@pytest.mark.unit
class TestNrf91Wiring:

    @pytest.fixture
    def owl_src(self):
        return (REPO / 'owl.py').read_text(encoding='utf-8')

    def test_owl_reader_handles_nrf91(self, owl_src):
        assert "self.gps_source in ('serial', 'nrf91')" in owl_src
        assert "('/dev/ttyAMA3', 460800)" in owl_src
        assert 'nrf91_start_gps(' in owl_src
        assert 'self._gps_state.update_from_line(raw_line)' in owl_src

    def test_modem_restart_and_silence_trigger_restart(self, owl_src):
        reader = owl_src.split('def _serial_gps_reader(self):', 1)[1]
        assert "raw_line == 'Ready'" in reader
        assert "nrf91_command(self._gps_serial, 'AT'" in reader

    def test_metres_accuracy_preferred_over_hdop(self, owl_src):
        assert "snapshot['accuracy_m'] if snapshot.get('accuracy_m') is not None" in owl_src

    def test_boot_config_mode(self):
        script = (REPO / 'controller' / 'setup' / 'install_firstboot.sh').read_text(encoding='utf-8')
        fn = script.split('configure_gps_uart() {', 1)[1].split('\n}\n', 1)[0]
        assert '--gps-config) configure_gps_uart' in script
        assert 'GPS_UART_OVERLAY="uart3-pi5"' in script
        assert 'GPS_POWER_GPIO=26' in script
        assert 'echo "gpio=${GPS_POWER_GPIO}=op,dh"' in fn
        # CM5 carrier only, idempotent per line, exit 2 when config.txt changed
        assert '"Compute Module 5"' in fn
        assert 'grep -qE "^gpio=${GPS_POWER_GPIO}=op,dh' in fn
        assert 'grep -qE "^dtoverlay=${GPS_UART_OVERLAY}' in fn
        assert 'return 2' in fn
        assert 'echo "dtoverlay=${GPS_UART_OVERLAY}"' in fn
        # GPIO 10/11 are output channels on the carrier: ctsrts is removed, never written
        assert 'echo "dtoverlay=${GPS_UART_OVERLAY},ctsrts' not in fn

    @pytest.mark.parametrize('script', [
        'owl_setup.sh', 'controller/setup/install_firstboot.sh',
        'controller/shared/setup.sh', 'tools/nrf9151_probe.sh',
    ])
    def test_shell_scripts_are_clean_text(self, script):
        # A stray NUL (from a mangled '\0' escape) makes bash warn and git
        # treat the script as binary; CR line endings break it on the Pi
        raw = (REPO / script).read_bytes()
        assert b'\x00' not in raw
        assert b'\r' not in raw

    def test_board_model_read_strips_nul_escape(self):
        script = (REPO / 'controller' / 'setup' / 'install_firstboot.sh').read_text(encoding='utf-8')
        fn = script.split('configure_gps_uart() {', 1)[1].split('\n}\n', 1)[0]
        assert "tr -d '\\0' < /proc/device-tree/model" in fn

    def test_factory_gps_uses_bench_values_and_boot_config(self):
        script = (REPO / 'owl_setup.sh').read_text(encoding='utf-8')
        assert 'NRF_GPS_SOURCE="nrf91"' in script
        assert 'NRF_GPS_PORT="/dev/ttyAMA3"' in script
        assert 'NRF_GPS_BAUD="460800"' in script
        fn = script.split('setup_factory_gps() {', 1)[1].split('\n}\n', 1)[0]
        assert 'install_firstboot.sh" --gps-config' in fn
        assert '-crtscts' in fn

    def test_config_editor_offers_nrf91(self):
        js = (REPO / 'controller' / 'shared' / 'js' / 'config.js').read_text(encoding='utf-8')
        assert "'nrf91'" in js
