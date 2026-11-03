"""utils/net_watchdog.py: the Wi-Fi fallback thread in owl.py.

Driven with explicit `now` values and a MagicMock NetworkManager; the
first-boot flag, the dashboard's switch marker and /proc live in tmp_path.
Never os.kill(pid, 0) for liveness: on Windows it terminates the process.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from utils import net_watchdog as nw

HOTSPOT = 'OWL-4b7d'


def completed(stdout='', returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr='')


@pytest.fixture
def proc(tmp_path):
    folder = tmp_path / 'proc'
    folder.mkdir()
    with patch.object(nw, 'PROC_DIR', str(folder)):
        yield folder


@pytest.fixture
def nm():
    manager = MagicMock()
    manager.get_hotspot_connection.return_value = HOTSPOT
    manager.device_state.return_value = (30, None)
    manager.wifi_profiles.return_value = [
        {'name': HOTSPOT, 'autoconnect': True, 'priority': 10},
        {'name': 'RigNet', 'autoconnect': True, 'priority': 200},
    ]
    manager.activate_connection.return_value = True
    return manager


@pytest.fixture
def runner():
    return MagicMock(return_value=completed(''))   # nobody on the hotspot


@pytest.fixture
def dog(nm, runner, tmp_path, proc):
    return nw.NetWatchdog(nm, runner, uptime=lambda: 1000,
                          flag_path=str(tmp_path / 'owl-firstboot.flag'),
                          marker_path=str(tmp_path / 'net-switching'))


def on_hotspot(nm):
    nm.device_state.return_value = (100, HOTSPOT)


@pytest.mark.unit
class TestPaused:
    def test_armed_unit_is_left_alone(self, dog, nm, tmp_path):
        (tmp_path / 'owl-firstboot.flag').touch()
        for now in (0, 60, 120):
            assert dog.tick(now) == 'paused'
        nm.hotspot_up.assert_not_called()

    def test_boot_grace(self, dog, nm):
        dog.uptime = lambda: 30
        assert dog.tick(0) == 'paused'
        assert dog.tick(60) == 'paused'
        nm.hotspot_up.assert_not_called()

    def test_live_dashboard_marker_pauses(self, dog, tmp_path, proc):
        (tmp_path / 'net-switching').write_text('4321')
        (proc / '4321').mkdir()
        assert dog.tick(0) == 'paused'

    def test_dead_pid_marker_is_ignored(self, dog, tmp_path):
        # The dashboard crashed mid-switch: the fallback must still happen
        (tmp_path / 'net-switching').write_text('4321')
        assert dog.tick(0) == 'down'

    def test_old_marker_is_ignored(self, dog, tmp_path, proc):
        (tmp_path / 'net-switching').write_text('4321')
        (proc / '4321').mkdir()
        with patch.object(nw.time, 'time', return_value=10**10):
            assert dog.tick(0) == 'down'

    def test_garbage_marker_is_ignored(self, dog, tmp_path):
        (tmp_path / 'net-switching').write_text('not a pid')
        assert dog.tick(0) == 'down'


@pytest.mark.unit
class TestNothingToDo:
    def test_networked_unit_without_hotspot(self, dog, nm):
        nm.get_hotspot_connection.return_value = None
        for now in (0, 100):
            assert dog.tick(now) == 'no_hotspot'
        nm.hotspot_up.assert_not_called()
        # Looking means listing every profile through sudo: rarely
        assert nm.get_hotspot_connection.call_count == 1
        dog.tick(nw.NO_HOTSPOT_RECHECK_S)
        assert nm.get_hotspot_connection.call_count == 2

    @pytest.mark.parametrize('code', [0, 10, 20])
    def test_radio_off_or_unmanaged(self, dog, nm, code):
        nm.device_state.return_value = (code, None)
        assert dog.tick(0) == 'hands_off'
        assert dog.tick(100) == 'hands_off'
        nm.hotspot_up.assert_not_called()

    def test_client_connected(self, dog, nm):
        nm.device_state.return_value = (100, 'RigNet')
        assert dog.tick(0) == 'client'
        nm.hotspot_up.assert_not_called()


@pytest.mark.unit
class TestFallback:
    def test_thirty_seconds_without_network_raises_hotspot_once(self, dog, nm):
        assert dog.tick(0) == 'down'
        assert dog.tick(29) == 'down'
        nm.hotspot_up.assert_not_called()
        assert dog.tick(30) == 'fallback'
        nm.hotspot_up.assert_called_once_with(HOTSPOT)

    def test_connecting_counts_as_down(self, dog, nm):
        nm.device_state.return_value = (70, 'RigNet')
        dog.tick(0)
        assert dog.tick(30) == 'fallback'

    def test_a_connection_in_between_restarts_the_clock(self, dog, nm):
        dog.tick(0)
        nm.device_state.return_value = (100, 'RigNet')
        dog.tick(20)
        nm.device_state.return_value = (30, None)
        assert dog.tick(25) == 'down'
        assert dog.tick(50) == 'down'
        assert dog.tick(55) == 'fallback'

    def test_still_down_retries_after_another_thirty(self, dog, nm):
        dog.tick(0)
        dog.tick(30)
        assert dog.tick(40) == 'down'
        assert dog.tick(70) == 'fallback'
        assert nm.hotspot_up.call_count == 2

    def test_hotspot_error_propagates_and_next_tick_retries(self, dog, nm):
        nm.hotspot_up.side_effect = RuntimeError('radio busy')
        dog.tick(0)
        with pytest.raises(RuntimeError):
            dog.tick(30)
        nm.hotspot_up.side_effect = None
        dog.tick(40)
        assert dog.tick(70) == 'fallback'


@pytest.mark.unit
class TestPrivileges:
    def test_changes_go_through_the_admin_runner(self, nm, runner, tmp_path, proc):
        admin = MagicMock()
        admin.activate_connection.return_value = False
        dog = nw.NetWatchdog(nm, runner, uptime=lambda: 1000,
                             flag_path=str(tmp_path / 'flag'),
                             marker_path=str(tmp_path / 'marker'), nm_admin=admin)
        dog.tick(0)
        dog.tick(30)                                   # fallback
        on_hotspot(nm)
        dog.tick(40)
        dog.tick(30 + nw.CLIENT_RETRY_S)               # retry, fails
        assert admin.hotspot_up.call_count == 2
        admin.activate_connection.assert_called_once()
        nm.hotspot_up.assert_not_called()
        nm.activate_connection.assert_not_called()


@pytest.mark.unit
class TestClientRetry:
    def test_first_try_waits_the_retry_interval(self, dog, nm):
        on_hotspot(nm)
        dog.tick(0)
        dog.tick(nw.CLIENT_RETRY_S - 1)
        nm.activate_connection.assert_not_called()
        dog.tick(nw.CLIENT_RETRY_S)
        nm.activate_connection.assert_called_once_with(
            'RigNet', timeout=nw.CLIENT_TRY_TIMEOUT_S)
        nm.hotspot_up.assert_not_called()

    def test_failed_try_brings_hotspot_straight_back(self, dog, nm):
        on_hotspot(nm)
        nm.activate_connection.return_value = False
        dog.tick(0)
        dog.tick(nw.CLIENT_RETRY_S)
        nm.hotspot_up.assert_called_once_with(HOTSPOT)
        dog.tick(nw.CLIENT_RETRY_S * 2 - 1)
        assert nm.activate_connection.call_count == 1
        dog.tick(nw.CLIENT_RETRY_S * 2)
        assert nm.activate_connection.call_count == 2

    def test_never_while_a_phone_is_on_the_hotspot(self, dog, nm, runner):
        on_hotspot(nm)
        runner.return_value = completed('Station aa:bb:cc:dd:ee:ff (on wlan0)\n')
        for now in range(0, 1000, 10):
            dog.tick(now)
        nm.activate_connection.assert_not_called()

    def test_waits_for_the_hotspot_to_be_quiet(self, dog, nm, runner):
        on_hotspot(nm)
        dog.tick(0)
        runner.return_value = completed('Station aa:bb:cc:dd:ee:ff (on wlan0)\n')
        dog.tick(nw.CLIENT_RETRY_S - 10)
        runner.return_value = completed('')
        dog.tick(nw.CLIENT_RETRY_S)
        nm.activate_connection.assert_not_called()       # phone left 10 s ago
        dog.tick(nw.CLIENT_RETRY_S - 10 + nw.STATION_QUIET_S)
        nm.activate_connection.assert_called_once()

    @pytest.mark.parametrize('reply', [completed('', returncode=1), OSError('no iw')])
    def test_unreadable_station_list_counts_as_a_phone(self, dog, nm, runner, reply):
        on_hotspot(nm)
        if isinstance(reply, Exception):
            runner.side_effect = reply
        else:
            runner.return_value = reply
        for now in range(0, 1000, 10):
            dog.tick(now)
        nm.activate_connection.assert_not_called()

    def test_profile_choice(self, dog, nm):
        on_hotspot(nm)
        nm.wifi_profiles.return_value = [
            {'name': HOTSPOT, 'autoconnect': True, 'priority': 10},
            {'name': 'owl-phone-internet', 'autoconnect': True, 'priority': 300},
            {'name': 'NoAuto', 'autoconnect': False, 'priority': 250},
            {'name': 'preconfigured', 'autoconnect': True, 'priority': 0},
            {'name': 'FarmNet', 'autoconnect': True, 'priority': 100},
            {'name': 'RigNet', 'autoconnect': True, 'priority': 200},
        ]
        dog.tick(0)
        dog.tick(nw.CLIENT_RETRY_S)
        assert nm.activate_connection.call_args[0][0] == 'RigNet'

    def test_nothing_saved_nothing_tried(self, dog, nm):
        on_hotspot(nm)
        nm.wifi_profiles.return_value = [
            {'name': HOTSPOT, 'autoconnect': True, 'priority': 10},
            {'name': 'owl-phone-internet', 'autoconnect': False, 'priority': 200},
            {'name': 'preconfigured', 'autoconnect': True, 'priority': 0},
        ]
        dog.tick(0)
        dog.tick(nw.CLIENT_RETRY_S)
        nm.activate_connection.assert_not_called()
        nm.hotspot_up.assert_not_called()


@pytest.mark.unit
def test_main_loop_survives_a_failing_tick():
    watchdog = MagicMock()
    watchdog.tick.side_effect = [RuntimeError('nmcli gone'), RuntimeError('nmcli gone'), 'down']
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        nw.run(watchdog, sleep=sleep)
    assert watchdog.tick.call_count == 3
    assert sleeps == [nw.TICK_S] * 3


@pytest.mark.unit
class TestStart:
    def test_nothing_to_run_without_networkmanager(self):
        with patch.object(nw.NetworkManager, 'is_supported', return_value=False):
            assert nw.start() is None

    def test_reads_without_sudo_changes_with_sudo(self):
        # Every sudo call logs to the journal: a check per 10 s must not use it
        with patch.object(nw.NetworkManager, 'is_supported', return_value=True), \
                patch.object(nw, 'run_command', return_value=completed('GENERAL.STATE:100\n')), \
                patch.object(nw.threading, 'Thread') as thread:
            nw.start()
        kwargs = thread.call_args.kwargs
        assert kwargs['daemon'] is True
        watchdog = kwargs['args'][0]
        assert watchdog.nm._runner is not nw.run_sudo
        assert watchdog.nm_admin._runner is nw.run_sudo
        thread.return_value.start.assert_called_once()

    def test_reads_fall_back_to_sudo_when_refused(self):
        # A refused read would look like 'radio off' and the watchdog would never act
        with patch.object(nw, 'run_command', return_value=completed('', returncode=8)):
            assert nw.read_runner() is nw.run_sudo

    def test_sudo_prefix(self):
        with patch.object(nw, 'run_command') as run:
            nw.run_sudo(['nmcli', 'con', 'up', 'OWL-4b7d'], timeout=60)
        run.assert_called_once_with(['/usr/bin/sudo', '-n', 'nmcli', 'con', 'up', 'OWL-4b7d'],
                                    timeout=60)

    def test_owl_starts_it_before_the_camera(self):
        source = (Path(__file__).resolve().parents[2] / 'owl.py').read_text(encoding='utf-8')
        main = source[source.index('if __name__ == "__main__":'):]
        assert main.index('net_watchdog.start()') < main.index('owl = Owl(')
