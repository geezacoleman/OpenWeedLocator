"""
First-boot setup — software integration tests.

Unlike tests/test_setup_app_routes.py (in-process test_client, MagicMock
NetworkManager, manually-fired timers), this suite runs the REAL stack the
phone app talks to:

    real HTTP socket -> real Flask app -> real FirstBootState with real
    threading.Timer handoffs -> real NetworkManager argv building/parsing
    -> stateful FakeNmcli (the only fake: the nmcli binary itself)

so the 202-then-switch timing, the failed-join revert, the scan-requires-
AP-down constraint, and the finish/teardown sequencing are all exercised
end to end. Timing constants are shortened via monkeypatch; nothing else
is stubbed.

Windows-safe: FakeNmcli emulates nmcli, FakeSystem emulates systemctl/
hostnamectl/ufw; all files live in tmp_path.
"""

import configparser
import threading
import time
from types import SimpleNamespace

import pytest
import requests
from werkzeug.serving import make_server

from controller.setup import firstboot_state as fbs
from controller.setup.setup_app import create_app
from controller.setup.firstboot_state import FirstBootState, SETUP_HOTSPOT_PSK
from utils.network_manager import NetworkManager

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).parent))  # fakes.py lives beside us
from fakes import HOTSPOT, FakeNmcli, FakeSystem  # noqa: E402

POLL_TIMEOUT = 8


def wait_until(predicate, timeout=POLL_TIMEOUT, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError('condition not met within %ss' % timeout)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Real Flask app on a real socket, real state machine, fake nmcli."""
    monkeypatch.setattr(fbs, 'SWITCH_DELAY_S', 0.05)
    monkeypatch.setattr(fbs, 'EXIT_DELAY_S', 0.05)
    monkeypatch.setattr(fbs, 'HOTSPOT_RETRY_S', 0.05)
    monkeypatch.setattr(fbs, 'REVERT_HOTSPOT_ATTEMPTS', 1)

    flag = tmp_path / 'owl-firstboot.flag'
    flag.touch()
    controller_ini = tmp_path / 'CONTROLLER.ini'
    controller_ini.write_text(
        '[MQTT]\nenable = False\nbroker_ip = localhost\n'
        'broker_port = 1883\ndevice_id = owl-fresh\n\n'
        '[WebDashboard]\nport = 8000\n\n'
        '[GPS]\nsource = none\n')

    fake = FakeNmcli()
    system = FakeSystem()
    nm = NetworkManager(runner=fake)
    nm.is_supported = lambda: True  # instance override: full radio path

    exits = []
    fb = FirstBootState(nm, flag_path=flag, state_file=tmp_path / 'state.json',
                        system_runner=system, exit_fn=lambda: exits.append(True),
                        controller_ini=controller_ini,
                        hostname_getter=lambda: system.hostname)
    # Keep teardown's avahi-file handling inside tmp_path
    monkeypatch.setattr(fbs, 'AVAHI_SERVICE_FILE',
                        str(tmp_path / 'owl-setup.service'))

    app = create_app(network_manager=nm, state=fb)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield SimpleNamespace(
        base=f'http://127.0.0.1:{server.server_port}/setup/api',
        fake=fake, system=system, fb=fb, nm=nm, flag=flag,
        controller_ini=controller_ini, exits=exits)

    server.shutdown()
    thread.join(timeout=5)


def controller_join_payload(**overrides):
    payload = {'ssid': 'RigNet', 'password': 'rigpass99',
               'device_id': 'owl-3', 'static_ip': '192.168.1.13',
               'gateway': '192.168.1.1', 'broker_ip': '192.168.1.2',
               'subnet_prefix': 24, 'broker_port': 1883}
    payload.update(overrides)
    return payload


class TestStartupAndScan:
    def test_startup_scans_ap_down_and_raises_hotspot(self, env):
        env.fb.startup()
        # Scan happened (AP had to come down for it) and the cache is served
        assert [n['ssid'] for n in env.fb.scan_cache] == ['FarmWiFi', 'Shed']
        # Hotspot back up with the fixed setup PSK
        assert env.fake.hotspot_active()
        assert env.fake.profiles[HOTSPOT]['psk'] == SETUP_HOTSPOT_PSK
        response = requests.get(f'{env.base}/wifi/scan', timeout=5)
        assert response.json()['networks'][0]['ssid'] == 'FarmWiFi'

    def test_startup_without_hotspot_profile_does_not_crash_loop(self, env):
        # The S1 regression: a missing hotspot profile used to raise out of
        # startup() -> systemd restart loop with no AP, phone locked out.
        del env.fake.profiles[HOTSPOT]
        env.fb.startup()   # must not raise
        # Recovery: profile appears (e.g. re-imaged) and the retry timer
        # brings the AP up without a service restart
        env.fake.add_hotspot()
        env.fake.profiles[HOTSPOT]['active'] = False
        env.nm._hotspot_name = None
        wait_until(env.fake.hotspot_active)
        assert env.fake.profiles[HOTSPOT]['psk'] == SETUP_HOTSPOT_PSK

    def test_reboot_onto_client_network_resumes(self, env):
        env.fake.profiles[HOTSPOT]['active'] = False
        env.fake.profiles['FarmWiFi'] = {'type': '802-11-wireless',
                                         'mode': 'infrastructure',
                                         'active': True, 'psk': 'x',
                                         'props': {}}
        env.fb.startup()
        assert env.fb.wifi_state == 'connected'
        assert env.fb.wifi_ssid == 'FarmWiFi'


class TestWifiJoinHandoff:
    def test_join_success_over_real_timers(self, env):
        env.fb.startup()
        response = requests.post(f'{env.base}/wifi/join',
                                 json={'ssid': 'FarmWiFi',
                                       'password': 'sunflower1'}, timeout=5)
        assert response.status_code == 202
        assert response.json()['verify']['service_type'] == '_owl-setup._tcp'

        def connected():
            result = requests.get(f'{env.base}/wifi/result', timeout=5).json()
            return result if result['state'] == 'connected' else None
        result = wait_until(connected)
        assert result['ssid'] == 'FarmWiFi'
        assert result['ip'] == '192.168.1.77'
        assert result['warning'] is None
        # Hotspot stayed as the priority-0 fallback profile; client outranks it
        assert HOTSPOT in env.fake.profiles
        assert not env.fake.hotspot_active()
        assert env.fake.profile_priority('FarmWiFi') == '200'

    def test_auth_failure_reverts_to_hotspot(self, env):
        env.fb.startup()
        env.fake.join_outcome['FarmWiFi'] = 'auth'
        requests.post(f'{env.base}/wifi/join',
                      json={'ssid': 'FarmWiFi', 'password': 'wrongpass1'},
                      timeout=5)

        def failed():
            result = requests.get(f'{env.base}/wifi/result', timeout=5).json()
            return result if result['state'] == 'failed' else None
        result = wait_until(failed)
        assert result['error'] == 'auth'
        # Failed profile deleted, AP restored — the phone can reconnect
        assert 'FarmWiFi' not in env.fake.profiles
        assert env.fake.hotspot_active()

    def test_revert_with_dead_hotspot_reports_and_self_heals(self, env):
        # The S2 regression: hotspot_up failing during revert used to leave
        # no AP and no client, with the state still claiming plain 'failed'.
        env.fb.startup()
        env.fake.join_outcome['FarmWiFi'] = 'auth'
        env.fake.hotspot_up_failures = 3
        requests.post(f'{env.base}/wifi/join',
                      json={'ssid': 'FarmWiFi', 'password': 'wrongpass1'},
                      timeout=5)

        def failed_no_ap():
            result = requests.get(f'{env.base}/wifi/result', timeout=5).json()
            return result if result.get('warning') == 'hotspot_unavailable' \
                else None
        wait_until(failed_no_ap)
        # Background retry keeps trying and eventually restores the AP
        wait_until(env.fake.hotspot_active)

    def test_second_join_while_pending_is_409(self, env):
        env.fb.startup()
        first = requests.post(f'{env.base}/wifi/join',
                              json={'ssid': 'FarmWiFi',
                                    'password': 'sunflower1'}, timeout=5)
        second = requests.post(f'{env.base}/wifi/join',
                               json={'ssid': 'Shed', 'password': 'sunflower1'},
                               timeout=5)
        assert first.status_code == 202
        assert second.status_code == 409


class TestControllerJoin:
    def test_full_rig_join(self, env):
        env.fb.startup()
        response = requests.post(f'{env.base}/controller/join',
                                 json=controller_join_payload(), timeout=5)
        assert response.status_code == 202
        assert response.json()['verify']['static_ip'] == '192.168.1.13'

        def connected():
            result = requests.get(f'{env.base}/wifi/result', timeout=5).json()
            return result if result['state'] == 'connected' else None
        result = wait_until(connected)
        assert result['device_id'] == 'owl-3'
        assert result['warning'] is None

        # Identity applied
        assert env.system.hostname == 'owl-3'
        assert ['systemctl', 'restart', 'owl.service'] in env.system.calls
        config = configparser.ConfigParser()
        config.read(env.controller_ini)
        assert config.get('MQTT', 'broker_ip') == '192.168.1.2'
        assert config.get('MQTT', 'device_id') == 'owl-3'
        assert config.get('Network', 'mode') == 'networked'
        # Pre-existing sections preserved
        assert config.get('WebDashboard', 'port') == '8000'
        # Static config on the radio
        props = env.fake.profiles['RigNet']['props']
        assert props['ipv4.addresses'] == '192.168.1.13/24'
        assert props['ipv4.method'] == 'manual'
        # Join committed -> pre-join backup cleaned up
        assert not env.fb._controller_ini_backup.exists()

    def test_owl_restart_failure_surfaces_warning(self, env):
        # The F8 regression: a swallowed owl.service restart failure used to
        # report a clean success while the rig never saw a heartbeat.
        env.fb.startup()
        env.system.fail_owl_restart = True
        requests.post(f'{env.base}/controller/join',
                      json=controller_join_payload(), timeout=5)

        def connected():
            result = requests.get(f'{env.base}/wifi/result', timeout=5).json()
            return result if result['state'] == 'connected' else None
        result = wait_until(connected)
        assert result['warning'] == 'owl_restart_failed'

    def test_failed_radio_keeps_target_config_and_retry_succeeds(self, env):
        env.fb.startup()
        env.fake.join_outcome['RigNet'] = 'auth'
        requests.post(f'{env.base}/controller/join',
                      json=controller_join_payload(), timeout=5)
        wait_until(lambda: env.fb.wifi_state == 'failed')

        # Target state kept for retry: hostname + CONTROLLER.ini stay
        assert env.system.hostname == 'owl-3'
        config = configparser.ConfigParser()
        config.read(env.controller_ini)
        assert config.get('Network', 'mode') == 'networked'

        env.fake.join_outcome['RigNet'] = 'ok'
        response = requests.post(f'{env.base}/controller/join',
                                 json=controller_join_payload(), timeout=5)
        assert response.status_code == 202
        wait_until(lambda: env.fb.wifi_state == 'connected')

    def test_abandoned_join_restores_identity(self, env):
        # The F7 regression: an abandoned wizard used to leave the OWL with
        # mode=networked + the rig broker + an owl-N hostname forever.
        env.fb.startup()
        env.fake.join_outcome['RigNet'] = 'auth'
        requests.post(f'{env.base}/controller/join',
                      json=controller_join_payload(), timeout=5)
        wait_until(lambda: env.fb.wifi_state == 'failed')

        response = requests.post(f'{env.base}/hotspot/restore', timeout=5)
        assert response.status_code == 200
        assert env.system.hostname == 'owl-fresh'
        config = configparser.ConfigParser()
        config.read(env.controller_ini)
        assert config.get('MQTT', 'device_id') == 'owl-fresh'
        assert not config.has_option('Network', 'mode') or \
            config.get('Network', 'mode') != 'networked'
        assert env.fake.hotspot_active()


class TestFinish:
    def test_standalone_finish_applies_password_and_tears_down(self, env):
        env.fb.startup()
        response = requests.post(f'{env.base}/finish',
                                 json={'mode': 'standalone',
                                       'new_password': 'paddock99'}, timeout=5)
        assert response.status_code == 200
        assert env.fake.profiles[HOTSPOT]['psk'] == 'paddock99'
        assert not env.flag.exists()
        wait_until(lambda: env.exits)

    def test_wifi_finish_requires_connected(self, env):
        env.fb.startup()
        response = requests.post(f'{env.base}/finish',
                                 json={'mode': 'wifi'}, timeout=5)
        assert response.status_code == 409
        assert env.flag.exists()

    def test_rearm_after_finish(self, env):
        env.fb.startup()
        requests.post(f'{env.base}/finish',
                      json={'mode': 'standalone', 'new_password': 'paddock99'},
                      timeout=5)
        wait_until(lambda: env.exits)
        # Factory-reset path: touching the flag + startup() is a full re-arm
        env.flag.touch()
        env.fb.startup()
        assert env.fake.hotspot_active()
        assert env.fake.profiles[HOTSPOT]['psk'] == SETUP_HOTSPOT_PSK
