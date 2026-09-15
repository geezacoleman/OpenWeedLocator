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

REPO = Path(__file__).parent.parent


def _gps(source='none', port=''):
    cfg = configparser.ConfigParser()
    cfg.read_dict({'GPS': {'source': source, 'port': port}})
    return cfg


NRF91 = ConfigValidator.protected_pins(_gps('nrf91'))

DEFAULT_PINS = {
    'Relays': {'0': '13', '1': '15', '2': '16', '3': '18'},
    'Controller': {
        'status_led_pin': '40', 'gps_led_pin': '38', 'switch_pin': '36',
        'detection_mode_pin_up': '36', 'detection_mode_pin_down': '35',
        'recording_pin': '33', 'sensitivity_pin': '32',
    },
}


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

    def test_default_pins_are_free_on_an_nrf91_unit(self):
        assert ConfigValidator.protected_pin_errors(DEFAULT_PINS, NRF91) == {}

    def test_nothing_refused_when_nothing_protected(self):
        assert ConfigValidator.protected_pin_errors({'Relays': {'0': '37'}}, {}) == {}

    def test_none_and_quoted_values(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({'Controller': {'gps_led_pin': "'37'", 'status_led_pin': 'none'}})
        errs = ConfigValidator.protected_pin_errors(cfg, NRF91)
        assert list(errs['Controller']) == ['gps_led_pin']

    def test_config_load_never_refuses_a_protected_pin(self):
        src = (REPO / 'utils' / 'config_manager.py').read_text(encoding='utf-8')
        body = src.split('def load_and_validate_config', 1)[1].split('\n    @classmethod', 1)[0]
        assert 'protected_pin' not in body


@pytest.mark.unit
class TestStartupDisablesClaimedDevices:

    def test_relay_slot_kept_but_driving_nothing(self):
        from utils.output_manager import RelayControl
        rc = RelayControl({0: 13, 1: 37, 2: 16}, disabled_pins=NRF91)
        assert list(rc.relay_dict) == [0, 1, 2]
        assert rc.disabled_relays == {1: 37}
        rc.relay_on(1, verbose=False)
        rc.relay_off(1, verbose=False)

    def test_relay_controller_keeps_every_lane(self):
        from utils.output_manager import RelayController
        rc = RelayController({0: 13, 1: 37}, disabled_pins=NRF91)
        try:
            assert set(rc.relay_schedule_dict) == {0, 1}
            rc.receive(relay=1, time_stamp=time.monotonic(), delay=0, duration=0.05)
        finally:
            rc.stop()

    def test_owl_builds_every_gpio_device_through_the_pin_check(self):
        src = (REPO / 'owl.py').read_text(encoding='utf-8')
        assert 'self.protected_pins = ConfigValidator.protected_pins(self.config)' in src
        assert 'disabled_pins=self.protected_pins' in src
        for key in ('status_led_pin', 'gps_led_pin', 'switch_pin', 'recording_pin',
                    'sensitivity_pin', 'detection_mode_pin_up', 'detection_mode_pin_down'):
            assert f"_allowed_board_pin('{key}'" in src, key

    def test_switches_accept_a_missing_pin(self):
        src = (REPO / 'utils' / 'input_manager.py').read_text(encoding='utf-8')
        assert 'if not testing and switch_board_pin else None' in src
        assert 'if not testing and pin else None' in src
        assert "switch_board_pin='BOARD37'" not in src


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
