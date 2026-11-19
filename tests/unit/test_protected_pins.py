"""
Protected GPIO pins, only while on-device GPS hardware uses them.

[GPS] source = nrf91 claims BOARD 37 (GPIO26 modem power) and BOARD 21/24
(the modem UART). A serial GPS on the Pi UART claims BOARD 8/10. A relay, LED
or switch on a claimed pin is disabled at startup (the OWL keeps running) and
refused by the standalone dashboard (and phone app), the networked controller
and the OWL's own MQTT handler. Nothing is protected without that hardware.
"""

import configparser
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from utils.config_manager import ConfigValidator

REPO = Path(__file__).resolve().parents[2]


def _gps(source='none', port=''):
    cfg = configparser.ConfigParser()
    cfg.read_dict({'GPS': {'source': source, 'port': port}})
    return cfg


NRF91 = ConfigValidator.protected_pins(_gps('nrf91'))

@pytest.mark.unit
class TestProtectedPinRules:

    def test_nrf91_claims_modem_power_and_uart(self):
        assert set(NRF91) == {37, 21, 24}
        assert 'GPIO26' in NRF91[37]

    @pytest.mark.parametrize('port', ['/dev/ttyAMA0', '/dev/serial0'])
    def test_serial_gps_on_pi_uart_claims_uart0(self, port):
        assert set(ConfigValidator.protected_pins(_gps('serial', port))) == {8, 10}

    @pytest.mark.parametrize('source,port', [
        ('none', ''), ('serial', '/dev/ttyUSB0'), ('serial', '/dev/ttyACM0'),
        ('tcp', ''), ('gpsd', '/dev/ttyAMA0'),
    ])
    def test_nothing_protected_without_that_hardware(self, source, port):
        assert ConfigValidator.protected_pins(_gps(source, port)) == {}

    def test_no_gps_section(self):
        assert ConfigValidator.protected_pins(configparser.ConfigParser()) == {}


@pytest.mark.unit
class TestProtectedPinErrors:

    def test_relay_and_led_on_claimed_pins(self):
        errs = ConfigValidator.protected_pin_errors({
            'Relays': {'0': '13', '1': '37'},
            'Controller': {'status_led_pin': '24', 'switch_purpose': '37'},
        }, NRF91)
        assert list(errs['Relays']) == ['1']
        assert list(errs['Controller']) == ['status_led_pin']
        assert 'BOARD 37 is in use' in errs['Relays']['1']
        assert 'GPIO26' in ConfigValidator.format_protected_pin_errors(errs)

    def test_the_shipped_config_is_free_of_claimed_pins_on_an_nrf91_unit(self):
        """Every production CM5 runs GENERAL_CONFIG.ini with [GPS] source =
        nrf91: a default on BOARD 37/21/24 would be disabled on first boot."""
        shipped = configparser.ConfigParser()
        assert shipped.read(REPO / 'config' / 'GENERAL_CONFIG.ini')
        pins = {key: shipped.get('Controller', key) for key in ConfigValidator.CONTROLLER_PIN_KEYS
                if shipped.has_option('Controller', key)}
        assert len(pins) >= 6 and len(shipped['Relays']) >= 4, 'the shipped config lost its pins'
        assert ConfigValidator.protected_pin_errors(shipped, NRF91) == {}
        # and the check does bite on that same file
        shipped.set('Relays', '0', '37')
        assert list(ConfigValidator.protected_pin_errors(shipped, NRF91)) == ['Relays']

    def test_nothing_refused_when_nothing_protected(self):
        assert ConfigValidator.protected_pin_errors({'Relays': {'0': '37'}}, {}) == {}

    def test_none_and_quoted_values(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({'Controller': {'gps_led_pin': "'37'", 'status_led_pin': 'none'}})
        errs = ConfigValidator.protected_pin_errors(cfg, NRF91)
        assert list(errs['Controller']) == ['gps_led_pin']

    def test_config_load_never_refuses_a_protected_pin(self, tmp_path):
        """A unit whose saved config has a relay on a claimed pin must still
        boot (the relay is disabled at startup): refusing the file would
        leave it with no detection at all."""
        config = configparser.ConfigParser()
        assert config.read(REPO / 'config' / 'GENERAL_CONFIG.ini')
        config.read_dict({'GPS': {'source': 'nrf91'}, 'Relays': {'1': '37'},
                          'Controller': {'status_led_pin': '24'}})
        path = tmp_path / 'claimed.ini'
        with open(path, 'w') as f:
            config.write(f)
        loaded = ConfigValidator.load_and_validate_config(path)
        assert loaded.get('Relays', '1') == '37'
        assert set(ConfigValidator.protected_pin_errors(loaded, ConfigValidator.protected_pins(loaded))) \
            == {'Relays', 'Controller'}


class _Gpio:
    """utils.output_manager as it is on a Pi: `testing` off and gpiozero's
    OutputDevice / Buzzer replaced by recorders, so the branch that claims
    real pins runs. Off a Pi every relay is a TestRelay whatever the code
    does, which is how a disabled relay that still got a GPIO device passed."""

    BCM = {13: 27, 15: 22, 16: 23, 18: 24, 37: 26, 7: 4}

    def __init__(self, monkeypatch, tmp_path):
        import utils.output_manager as om
        self.claimed = []        # 'BOARDn' for every OutputDevice built
        self.driven = []         # (board pin, on)
        self.buzzers = []
        self.pins_file = tmp_path / 'owl-relay-pins'
        gpio = self

        class OutputDevice:
            def __init__(self, pin):
                gpio.claimed.append(pin)
                self.board = int(pin.replace('BOARD', ''))
                self.pin = f'GPIO{gpio.BCM[self.board]}'

            def on(self):
                gpio.driven.append((self.board, True))

            def off(self):
                gpio.driven.append((self.board, False))

        class Buzzer:
            def __init__(self, pin):
                gpio.buzzers.append(pin)

            def beep(self, **kwargs):
                pass

        monkeypatch.setattr(om, 'testing', False)
        monkeypatch.setattr(om, 'OutputDevice', OutputDevice, raising=False)
        monkeypatch.setattr(om, 'Buzzer', Buzzer, raising=False)
        monkeypatch.setenv('OWL_RELAY_PINS_FILE', str(self.pins_file))


@pytest.fixture
def gpio(monkeypatch, tmp_path):
    return _Gpio(monkeypatch, tmp_path)


@pytest.mark.unit
class TestStartupDisablesClaimedDevices:

    def test_a_relay_on_a_claimed_pin_keeps_its_slot_and_gets_no_gpio_device(self, gpio):
        from utils.output_manager import RelayControl
        rc = RelayControl({0: 13, 1: 37, 2: 16}, disabled_pins=NRF91)
        assert gpio.claimed == ['BOARD13', 'BOARD16'], 'the modem power pin was claimed as a relay'
        assert list(rc.relay_dict) == [0, 1, 2]
        assert rc.disabled_relays == {1: 37}
        rc.all_on()
        rc.all_off()
        assert gpio.driven == [(13, True), (16, True), (13, False), (16, False)]

    def test_without_gps_hardware_the_same_pin_is_an_ordinary_relay(self, gpio):
        from utils.output_manager import RelayControl
        rc = RelayControl({0: 13, 1: 37}, disabled_pins={})
        assert gpio.claimed == ['BOARD13', 'BOARD37']
        assert rc.disabled_relays == {}
        rc.relay_on(1, verbose=False)
        assert gpio.driven == [(37, True)]

    def test_the_pins_file_for_relays_off_lists_only_pins_this_process_drives(self, gpio):
        """tools/relays_off.py (owl.service ExecStopPost) drives every BCM
        pin in this file low after any exit. The modem's power pin in it
        would switch the GPS off on every restart."""
        from utils.output_manager import RelayControl
        RelayControl({0: 13, 1: 37, 2: 16, 3: 18}, disabled_pins=NRF91)
        assert gpio.pins_file.read_text() == '23 24 27\n'      # BCM, sorted; no 26

    def test_a_relay_on_the_buzzer_pin_takes_it_from_the_buzzer(self, gpio):
        from utils.output_manager import RelayControl
        RelayControl({0: 13}, disabled_pins={})
        assert gpio.buzzers == ['BOARD7']
        gpio.buzzers.clear()
        rc = RelayControl({0: 7}, disabled_pins={})
        assert gpio.buzzers == [], 'the buzzer and a relay both claimed BOARD7'
        rc.beep(duration=0.01, repeats=1)                      # a stub: nothing to drive

    def test_a_disabled_lane_takes_spray_windows_and_drives_nothing(self, gpio):
        """The lane keeps its consumer thread (lane indexing depends on keys
        0..n-1), so a weed in it must be a no-op, not an exception that
        kills the thread, while the lane next to it sprays."""
        from utils.output_manager import RelayController
        rc = RelayController({0: 13, 1: 37}, disabled_pins=NRF91)
        try:
            assert set(rc.relay_schedule_dict) == {0, 1}
            now = time.monotonic()
            rc.receive(relay=1, time_stamp=now, delay=0, duration=0.1)
            rc.receive(relay=0, time_stamp=now, delay=0, duration=0.1)
            time.sleep(0.4)
            assert gpio.driven == [(13, True), (13, False)]
            rc.set_mode('blanket')
            assert gpio.driven[2:] == [(13, True)]
        finally:
            rc.set_mode('off')
            rc.stop()


@pytest.mark.unit
class TestMqttRejectsClaimedPins:

    def test_whole_section_refused_on_nrf91_unit(self, mqtt_publisher, mock_owl):
        mock_owl.config.read_dict({'GPS': {'source': 'nrf91'}})
        mqtt_publisher._handle_command({
            'action': 'set_config_section',
            'section': 'Controller',
            'params': {'status_led_pin': '37', 'switch_purpose': 'detection'},
        })
        assert mock_owl.config.get('Controller', 'status_led_pin', fallback=None) != '37'
        assert mock_owl.config.get('Controller', 'switch_purpose', fallback=None) != 'detection'

    def test_allowed_without_gps_hardware(self, mqtt_publisher, mock_owl):
        mock_owl.config.read_dict({'GPS': {'source': 'none'}})
        mqtt_publisher._handle_command({
            'action': 'set_config_section',
            'section': 'Controller',
            'params': {'status_led_pin': '37'},
        })
        assert mock_owl.config.get('Controller', 'status_led_pin') == '37'

    def test_claimed_pins_published_in_state(self, mqtt_publisher, mock_owl):
        mock_owl.config.read_dict({'GPS': {'source': 'nrf91'}})
        mqtt_publisher.set_owl_instance(mock_owl)
        assert set(mqtt_publisher.state['protected_pins']) == {'37', '21', '24'}


@pytest.mark.unit
class TestStandaloneRejectsClaimedPins:

    def test_section_route_on_nrf91_unit(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = MagicMock()
        dashboard._device_protected_pins = lambda: NRF91
        resp = client.post('/api/config/section',
                           json={'section': 'Relays', 'params': {'0': '37'}})
        assert resp.status_code == 400
        assert 'GPIO26' in resp.get_json()['error']
        dashboard.mqtt_client._send_command.assert_not_called()

    def test_section_route_allows_pin_without_gps_hardware(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = MagicMock()
        dashboard._device_protected_pins = lambda: {}
        resp = client.post('/api/config/section',
                           json={'section': 'Relays', 'params': {'0': '37'}})
        assert resp.status_code == 200
        dashboard.mqtt_client._send_command.assert_called_once()

    def test_save_route_writes_nothing(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard._device_protected_pins = lambda: NRF91
        resp = client.post('/api/config', json={
            'name': 'bad pins',
            'config': {'System': {'relay_num': '1'}, 'Relays': {'0': '24'}},
        })
        assert resp.status_code == 400
        assert 'rejected' in resp.get_json()
        assert not list(Path(tmp_dir).rglob('*bad*pins*'))


@pytest.mark.unit
class TestNetworkedRejectsClaimedPins:

    @staticmethod
    def _claim(mock_ctrl, pins):
        mock_ctrl.mqtt_lock = threading.Lock()
        mock_ctrl.owls_state = {'test-owl': {
            'connected': True,
            'protected_pins': {str(pin): why for pin, why in pins.items()},
        }}
        mock_ctrl.send_command.reset_mock()

    def test_push_device_config(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        self._claim(mock_ctrl, NRF91)
        resp = client.post('/api/config/test-owl',
                           json={'section': 'Controller', 'params': {'status_led_pin': '37'}})
        assert resp.status_code == 400
        assert 'GPIO26' in resp.get_json()['error']
        mock_ctrl.send_command.assert_not_called()

    def test_param_route_checks_every_connected_owl(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        self._claim(mock_ctrl, NRF91)
        resp = client.post('/api/config/param',
                           json={'section': 'Relays', 'param': '0', 'value': '21'})
        assert resp.status_code == 400
        mock_ctrl.send_command.assert_not_called()

    def test_owl_without_gps_hardware_accepts_the_pin(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        self._claim(mock_ctrl, {})
        resp = client.post('/api/config/test-owl',
                           json={'section': 'Controller', 'params': {'status_led_pin': '37'}})
        assert resp.status_code == 200
        mock_ctrl.send_command.assert_called_once()
