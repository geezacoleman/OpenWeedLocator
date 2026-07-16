"""
Unit tests for utils/network_manager.py (nmcli wrapper).

No real nmcli is ever invoked: a FakeRunner records every argv and
returns canned CompletedProcess results, so these tests run anywhere
(including Windows CI).
"""

import subprocess
from unittest.mock import patch

import pytest

from utils.network_manager import (
    NetworkManager,
    NetworkManagerError,
    classify_nmcli_error,
    _split_terse,
    CLIENT_CONNECTION_PRIORITY,
)


class FakeRunner:
    """Records nmcli argv calls; replies from a queue or a matcher map."""

    def __init__(self):
        self.calls = []
        self.responses = []          # FIFO queue of CompletedProcess
        self.matchers = []           # (predicate, CompletedProcess) pairs
        self.default = subprocess.CompletedProcess([], 0, stdout='', stderr='')

    def queue(self, stdout='', returncode=0, stderr=''):
        self.responses.append(
            subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr))

    def when(self, predicate, stdout='', returncode=0, stderr=''):
        self.matchers.append(
            (predicate,
             subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)))

    def __call__(self, argv, timeout=30):
        self.calls.append(argv)
        for predicate, response in self.matchers:
            if predicate(argv):
                return response
        if self.responses:
            return self.responses.pop(0)
        return self.default

    def argv_history(self):
        return [' '.join(call) for call in self.calls]


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def manager(runner):
    return NetworkManager(runner=runner)


@pytest.mark.unit
class TestTerseParsing:
    def test_plain_fields(self):
        assert _split_terse('MyNet:78:WPA2') == ['MyNet', '78', 'WPA2']

    def test_escaped_colon_in_ssid(self):
        assert _split_terse('Cafe\\: Free:55:WPA2') == ['Cafe: Free', '55', 'WPA2']

    def test_escaped_backslash(self):
        assert _split_terse('A\\\\B:10:WPA2') == ['A\\B', '10', 'WPA2']

    def test_empty_fields(self):
        assert _split_terse(':42:') == ['', '42', '']


@pytest.mark.unit
class TestErrorClassification:
    def test_auth_failure(self):
        assert classify_nmcli_error(
            'Error: Connection activation failed: Secrets were required, '
            'but not provided.') == 'auth'

    def test_not_found(self):
        assert classify_nmcli_error('Error: No network with SSID Foo') == 'not_found'

    def test_timeout(self):
        assert classify_nmcli_error('Error: Timeout expired') == 'timeout'

    def test_generic(self):
        assert classify_nmcli_error('Error: something else') == 'error'

    def test_none_stderr(self):
        assert classify_nmcli_error(None) == 'error'


@pytest.mark.unit
class TestScan:
    def test_parses_and_sorts_by_signal(self, manager, runner):
        runner.queue(stdout='HomeNet:55:WPA2\nShed:90:WPA1 WPA2\n')
        networks = manager.scan()
        assert networks == [
            {'ssid': 'Shed', 'signal': 90, 'security': 'WPA1 WPA2'},
            {'ssid': 'HomeNet', 'signal': 55, 'security': 'WPA2'},
        ]

    def test_dedupes_keeping_strongest(self, manager, runner):
        runner.queue(stdout='HomeNet:55:WPA2\nHomeNet:80:WPA2\n')
        networks = manager.scan()
        assert len(networks) == 1
        assert networks[0]['signal'] == 80

    def test_drops_hidden_ssids(self, manager, runner):
        runner.queue(stdout=':70:WPA2\nVisible:60:WPA2\n')
        networks = manager.scan()
        assert [n['ssid'] for n in networks] == ['Visible']

    def test_open_network_security_label(self, manager, runner):
        runner.queue(stdout='OpenNet:40:\n')
        assert manager.scan()[0]['security'] == 'open'

    def test_escaped_colon_ssid(self, manager, runner):
        runner.queue(stdout='Cafe\\: Free:55:WPA2\n')
        assert manager.scan()[0]['ssid'] == 'Cafe: Free'

    def test_rescan_flag_mapping(self, manager, runner):
        runner.queue(stdout='')
        manager.scan(rescan=True)
        assert runner.calls[0][-2:] == ['--rescan', 'yes']

        runner.queue(stdout='')
        manager.scan(rescan=False)
        assert runner.calls[1][-2:] == ['--rescan', 'no']

    def test_scan_failure_raises(self, manager, runner):
        runner.queue(returncode=1, stderr='Error: device busy')
        with pytest.raises(NetworkManagerError):
            manager.scan()


@pytest.mark.unit
class TestHotspotDetection:
    def _wire_hotspot(self, runner, name='OWL-4'):
        runner.when(lambda a: a[-2:] == ['con', 'show'] and '-t' in a,
                    stdout=f'{name}:802-11-wireless\npreconfigured:802-11-wireless\n'
                           'Wired connection 1:802-3-ethernet\n')
        runner.when(lambda a: '802-11-wireless.mode' in a and a[-1] == name,
                    stdout='802-11-wireless.mode:ap\n')
        runner.when(lambda a: '802-11-wireless.mode' in a and a[-1] == 'preconfigured',
                    stdout='802-11-wireless.mode:infrastructure\n')

    def test_finds_ap_mode_connection(self, manager, runner):
        self._wire_hotspot(runner)
        assert manager.get_hotspot_connection() == 'OWL-4'

    def test_result_is_cached(self, manager, runner):
        self._wire_hotspot(runner)
        manager.get_hotspot_connection()
        first_call_count = len(runner.calls)
        manager.get_hotspot_connection()
        assert len(runner.calls) == first_call_count

    def test_returns_none_when_no_ap(self, manager, runner):
        runner.when(lambda a: a[-2:] == ['con', 'show'] and '-t' in a,
                    stdout='preconfigured:802-11-wireless\n')
        runner.when(lambda a: '802-11-wireless.mode' in a,
                    stdout='802-11-wireless.mode:infrastructure\n')
        assert manager.get_hotspot_connection() is None

    def test_hotspot_up_without_profile_raises(self, manager, runner):
        runner.when(lambda a: a[-2:] == ['con', 'show'] and '-t' in a, stdout='')
        with pytest.raises(NetworkManagerError):
            manager.hotspot_up()


@pytest.mark.unit
class TestHotspotUpDown:
    @pytest.fixture
    def hotspot_manager(self, runner):
        manager = NetworkManager(runner=runner)
        manager._hotspot_name = 'OWL-4'
        return manager

    def test_up_issues_con_up(self, hotspot_manager, runner):
        hotspot_manager.hotspot_up()
        assert runner.calls[-1] == ['nmcli', 'con', 'up', 'OWL-4']

    def test_down_issues_con_down(self, hotspot_manager, runner):
        hotspot_manager.hotspot_down()
        assert runner.calls[-1] == ['nmcli', 'con', 'down', 'OWL-4']

    def test_down_tolerates_inactive(self, hotspot_manager, runner):
        runner.queue(returncode=10, stderr="Error: 'OWL-4' is not an active connection.")
        hotspot_manager.hotspot_down()  # must not raise

    def test_explicit_name_overrides_lookup(self, hotspot_manager, runner):
        hotspot_manager.hotspot_up(name='OWL-CUSTOM')
        assert runner.calls[-1] == ['nmcli', 'con', 'up', 'OWL-CUSTOM']


@pytest.mark.unit
class TestAddWifiConnection:
    def test_full_command_sequence(self, manager, runner):
        manager.add_wifi_connection('HomeNet', 'secret123')
        history = runner.argv_history()
        assert history[0] == 'nmcli con delete HomeNet'
        assert history[1] == ('nmcli con add type wifi ifname wlan0 '
                              'con-name HomeNet ssid HomeNet')
        assert history[2] == ('nmcli con modify HomeNet wifi-sec.key-mgmt wpa-psk '
                              'wifi-sec.psk secret123')
        assert history[3] == ('nmcli con modify HomeNet connection.autoconnect yes '
                              f'connection.autoconnect-priority {CLIENT_CONNECTION_PRIORITY}')

    def test_open_network_skips_security(self, manager, runner):
        manager.add_wifi_connection('OpenNet', password=None)
        assert not any('wifi-sec' in call for call in runner.argv_history())

    def test_custom_priority(self, manager, runner):
        manager.add_wifi_connection('HomeNet', 'secret123', priority=50)
        assert 'connection.autoconnect-priority 50' in runner.argv_history()[-1]


@pytest.mark.unit
class TestStaticIpConnection:
    def test_static_ip_adds_manual_ipv4_modify(self, manager, runner):
        manager.add_wifi_connection('RigNet', 'secret123',
                                    static_ip='192.168.1.13',
                                    gateway='192.168.1.1')
        history = runner.argv_history()
        assert ('nmcli con modify RigNet ipv4.addresses 192.168.1.13/24 '
                'ipv4.gateway 192.168.1.1 ipv4.dns 192.168.1.1 '
                'ipv4.method manual') in history

    def test_dns_defaults_to_gateway_and_is_overridable(self, manager, runner):
        manager.add_wifi_connection('RigNet', 'secret123',
                                    static_ip='192.168.1.13',
                                    gateway='192.168.1.1', dns='8.8.8.8')
        assert any('ipv4.dns 8.8.8.8' in call for call in runner.argv_history())

    def test_custom_prefix(self, manager, runner):
        manager.add_wifi_connection('RigNet', 'secret123',
                                    static_ip='10.0.0.13', gateway='10.0.0.1',
                                    prefix=16)
        assert any('ipv4.addresses 10.0.0.13/16' in call
                   for call in runner.argv_history())

    def test_static_without_gateway_raises(self, manager):
        with pytest.raises(NetworkManagerError):
            manager.add_wifi_connection('RigNet', 'secret123',
                                        static_ip='192.168.1.13')

    def test_no_static_args_keeps_dhcp_behavior(self, manager, runner):
        manager.add_wifi_connection('HomeNet', 'secret123')
        assert not any('ipv4.method' in call for call in runner.argv_history())


@pytest.mark.unit
class TestSetHostname:
    def _runner(self, returncode=0, stderr=''):
        calls = []

        def run(argv, timeout=15):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, returncode, stdout='',
                                               stderr=stderr)
        run.calls = calls
        return run

    def test_full_sequence(self, tmp_path):
        hosts = tmp_path / 'hosts'
        hosts.write_text('127.0.0.1\tlocalhost\n127.0.1.1\towl-1\n'
                         '192.168.1.2\tcontroller\n')
        runner = self._runner()
        NetworkManager.set_hostname('owl-3', runner, hosts_path=str(hosts))
        assert runner.calls[0] == ['hostnamectl', 'set-hostname', 'owl-3']
        assert runner.calls[1] == ['systemctl', 'restart', 'avahi-daemon']
        content = hosts.read_text()
        assert '127.0.1.1\towl-3' in content
        assert 'owl-1' not in content
        assert '192.168.1.2\tcontroller' in content  # unrelated lines kept

    def test_invalid_hostname_rejected(self):
        runner = self._runner()
        for bad in ('Owl_3!', '-owl', 'owl 3', ''):
            with pytest.raises(NetworkManagerError):
                NetworkManager.set_hostname(bad, runner)
        assert runner.calls == []  # nothing executed

    def test_hostnamectl_failure_raises(self, tmp_path):
        runner = self._runner(returncode=1, stderr='not permitted')
        with pytest.raises(NetworkManagerError):
            NetworkManager.set_hostname('owl-3', runner,
                                        hosts_path=str(tmp_path / 'hosts'))


@pytest.mark.unit
class TestActivateConnection:
    def test_success_returns_true(self, manager, runner):
        runner.queue(returncode=0)
        assert manager.activate_connection('HomeNet') is True
        assert runner.calls[0][:4] == ['nmcli', '--wait', '45', 'con']

    def test_failure_returns_false_and_classifies(self, manager, runner):
        runner.queue(returncode=4,
                     stderr='Error: Connection activation failed: Secrets were '
                            'required, but not provided.')
        assert manager.activate_connection('HomeNet') is False
        assert manager.last_error == 'auth'

    def test_custom_timeout_propagates(self, manager, runner):
        runner.queue(returncode=0)
        manager.activate_connection('HomeNet', timeout=90)
        assert runner.calls[0][1:3] == ['--wait', '90']


@pytest.mark.unit
class TestConnectionState:
    def test_activated(self, manager, runner):
        runner.queue(stdout='GENERAL.STATE:activated\n')
        assert manager.connection_state('HomeNet') == 'activated'

    def test_inactive_profile(self, manager, runner):
        runner.queue(stdout='')  # profile exists, no GENERAL section
        assert manager.connection_state('HomeNet') == 'inactive'

    def test_missing_profile(self, manager, runner):
        runner.queue(returncode=10, stderr='Error: unknown connection')
        assert manager.connection_state('Nope') == 'missing'


@pytest.mark.unit
class TestHotspotPassword:
    def test_rejects_short_password(self, manager):
        with pytest.raises(NetworkManagerError):
            manager.set_hotspot_password('short')

    def test_modify_and_reapply(self, manager, runner):
        manager._hotspot_name = 'OWL-4'
        manager.set_hotspot_password('newpassword')
        history = runner.argv_history()
        assert 'nmcli con modify OWL-4 wifi-sec.psk newpassword' in history
        assert history[-1] == 'nmcli con up OWL-4'


@pytest.mark.unit
class TestStatus:
    def test_hotspot_mode(self, manager, runner):
        manager._hotspot_name = 'OWL-4'
        runner.when(lambda a: '--active' in a,
                    stdout='OWL-4:802-11-wireless:wlan0\n')
        runner.when(lambda a: 'IP4.ADDRESS' in a,
                    stdout='IP4.ADDRESS[1]:10.42.0.1/24\n')
        status = manager.status()
        assert status['mode'] == 'hotspot'
        assert status['ssid'] == 'OWL-4'
        assert status['ip'] == '10.42.0.1'

    def test_client_mode(self, manager, runner):
        manager._hotspot_name = 'OWL-4'
        runner.when(lambda a: '--active' in a,
                    stdout='HomeNet:802-11-wireless:wlan0\n')
        runner.when(lambda a: 'IP4.ADDRESS' in a,
                    stdout='IP4.ADDRESS[1]:192.168.1.57/24\n')
        status = manager.status()
        assert status['mode'] == 'client'
        assert status['ssid'] == 'HomeNet'
        assert status['ip'] == '192.168.1.57'

    def test_no_network(self, manager, runner):
        manager._hotspot_name = 'OWL-4'
        runner.when(lambda a: '--active' in a, stdout='')
        runner.when(lambda a: 'IP4.ADDRESS' in a, returncode=10, stderr='no device')
        status = manager.status()
        assert status['mode'] == 'none'
        assert status['ip'] is None


@pytest.mark.unit
class TestPlatformSupport:
    def test_not_supported_on_windows(self):
        with patch('utils.network_manager.platform.system', return_value='Windows'):
            assert NetworkManager.is_supported() is False

    def test_not_supported_without_nmcli(self):
        with patch('utils.network_manager.platform.system', return_value='Linux'), \
                patch('utils.network_manager.shutil.which', return_value=None):
            assert NetworkManager.is_supported() is False

    def test_unsupported_platform_raises_without_runner(self):
        manager = NetworkManager()  # no injected runner
        with patch.object(NetworkManager, 'is_supported', return_value=False):
            with pytest.raises(NetworkManagerError):
                manager.scan()

    def test_timeout_expired_maps_to_error(self, manager, runner):
        def boom(argv, timeout=30):
            raise subprocess.TimeoutExpired(argv, timeout)
        manager._runner = boom
        with pytest.raises(NetworkManagerError) as excinfo:
            manager.scan()
        assert excinfo.value.kind == 'timeout'
