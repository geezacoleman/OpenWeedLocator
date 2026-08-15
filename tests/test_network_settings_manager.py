"""Unit tests for controller/standalone/network_settings.py (R3).

Mirrors tests/test_setup_app_routes.py: the NetworkManager is a MagicMock
and timers fire only when the test says so, so the full join handoff
(including the revert-to-previous tail) runs synchronously anywhere.
"""

from unittest.mock import MagicMock, patch

import pytest

from controller.standalone.network_settings import NetworkSettingsManager


class CapturingTimer:
    """threading.Timer stand-in — collects callbacks; tests fire them."""

    pending = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.daemon = False
        self.cancelled = False

    def start(self):
        CapturingTimer.pending.append(self)

    def cancel(self):
        self.cancelled = True
        if self in CapturingTimer.pending:
            CapturingTimer.pending.remove(self)

    def fire(self):
        CapturingTimer.pending.remove(self)
        self.function()

    @classmethod
    def fire_all(cls):
        while cls.pending:
            cls.pending[0].fire()


@pytest.fixture(autouse=True)
def clear_timers():
    CapturingTimer.pending = []
    yield
    CapturingTimer.pending = []


@pytest.fixture(autouse=True)
def no_sleep():
    with patch('controller.standalone.network_settings.time.sleep'):
        yield


@pytest.fixture
def nm():
    manager = MagicMock()
    manager.is_supported.return_value = True
    manager.get_ip4.return_value = '192.168.1.50'
    manager.status.return_value = {'mode': 'hotspot', 'ssid': 'OWL-4b7d',
                                   'ip': '10.42.0.1', 'hotspot_profile': 'OWL-4b7d'}
    manager.scan.return_value = [
        {'ssid': 'HomeNet', 'signal': 80, 'security': 'WPA2'}]
    manager.activate_connection.return_value = True
    manager.last_error = None
    return manager


@pytest.fixture
def mgr(nm):
    return NetworkSettingsManager(nm, timer_factory=CapturingTimer)


@pytest.mark.unit
class TestScan:
    def test_client_scan_direct(self, mgr, nm):
        networks = mgr.scan_client(rescan=True)
        nm.scan.assert_called_once_with(rescan=True)
        assert networks[0]['ssid'] == 'HomeNet'
        assert mgr.scanned_at is not None
        assert mgr.scan_results()['networks'] == networks

    def test_hotspot_scan_cycles_ap_in_background(self, mgr, nm):
        mgr.start_hotspot_scan()
        assert mgr.scanning is True
        nm.hotspot_down.assert_not_called()  # deferred until the timer fires
        CapturingTimer.fire_all()
        nm.hotspot_down.assert_called_once()
        nm.scan.assert_called_once_with(rescan=True)
        nm.hotspot_up.assert_called_once()
        assert mgr.scanning is False
        assert mgr.scan_results()['networks'][0]['ssid'] == 'HomeNet'

    def test_hotspot_scan_failure_still_restores_ap(self, mgr, nm):
        nm.scan.side_effect = RuntimeError('radio busy')
        mgr.start_hotspot_scan()
        CapturingTimer.fire_all()
        nm.hotspot_up.assert_called_once()
        assert mgr.scanning is False

    def test_second_scan_request_is_noop_while_running(self, mgr, nm):
        mgr.start_hotspot_scan()
        mgr.start_hotspot_scan()
        assert len(CapturingTimer.pending) == 1


@pytest.mark.unit
class TestJoinFromHotspot:
    def test_join_success(self, mgr, nm):
        mgr.request_join('HomeNet', 'secret123')
        assert mgr.state == 'pending'
        assert mgr.previous == {'ssid': 'OWL-4b7d', 'mode': 'hotspot'}

        CapturingTimer.fire_all()
        nm.add_wifi_connection.assert_called_once_with('HomeNet', 'secret123')
        nm.hotspot_down.assert_called_once()
        result = mgr.result()
        assert result['state'] == 'connected'
        assert result['ip'] == '192.168.1.50'

    def test_join_failure_reverts_to_hotspot(self, mgr, nm):
        nm.activate_connection.return_value = False
        nm.last_error = 'auth'
        mgr.request_join('HomeNet', 'wrongpass99')
        CapturingTimer.fire_all()

        nm.delete_connection.assert_called_with('HomeNet')
        nm.hotspot_up.assert_called_once()
        result = mgr.result()
        assert result['state'] == 'failed'
        assert result['error'] == 'auth'
        assert result['warning'] is None

    def test_revert_exhaustion_flags_previous_unavailable(self, mgr, nm):
        nm.activate_connection.return_value = False
        nm.last_error = 'timeout'
        nm.hotspot_up.side_effect = RuntimeError('AP dead')
        mgr.request_join('HomeNet', 'secret123')
        CapturingTimer.fire_all()

        assert nm.hotspot_up.call_count == 3
        result = mgr.result()
        assert result['state'] == 'failed'
        assert result['warning'] == 'previous_unavailable'

    def test_concurrent_join_rejected(self, mgr, nm):
        mgr.request_join('HomeNet', 'secret123')
        with pytest.raises(RuntimeError):
            mgr.request_join('OtherNet', 'secret123')


@pytest.mark.unit
class TestJoinFromClient:
    """The unit is already on a client network; join switches in place and
    reverts to the PREVIOUS CLIENT network (not the hotspot) on failure."""

    @pytest.fixture(autouse=True)
    def client_mode(self, nm):
        nm.status.return_value = {'mode': 'client', 'ssid': 'OldNet',
                                  'ip': '192.168.0.9', 'hotspot_profile': 'OWL-4b7d'}

    def test_success_deletes_superseded_profile(self, mgr, nm):
        mgr.request_join('NewNet', 'secret123')
        CapturingTimer.fire_all()
        assert mgr.result()['state'] == 'connected'
        nm.hotspot_down.assert_not_called()
        nm.delete_connection.assert_called_once_with('OldNet')

    def test_rejoining_same_network_keeps_profile(self, mgr, nm):
        mgr.request_join('OldNet', 'newpass123')
        CapturingTimer.fire_all()
        assert mgr.result()['state'] == 'connected'
        nm.delete_connection.assert_not_called()

    def test_failure_reactivates_previous_client(self, mgr, nm):
        # First activate (NewNet) fails, revert activate (OldNet) succeeds
        nm.activate_connection.side_effect = [False, True]
        nm.last_error = 'not_found'
        mgr.request_join('NewNet', 'secret123')
        CapturingTimer.fire_all()

        nm.delete_connection.assert_called_once_with('NewNet')
        revert_call = nm.activate_connection.call_args_list[-1]
        assert revert_call.args[0] == 'OldNet'
        nm.hotspot_up.assert_not_called()
        result = mgr.result()
        assert result['state'] == 'failed'
        assert result['error'] == 'not_found'
        assert result['warning'] is None
        assert result['previous'] == {'ssid': 'OldNet', 'mode': 'client'}


@pytest.mark.unit
class TestCancel:
    def test_cancel_pending_kills_timer(self, mgr, nm):
        mgr.request_join('HomeNet', 'secret123')
        timer = CapturingTimer.pending[0]
        mgr.cancel()
        assert timer.cancelled is True
        assert CapturingTimer.pending == []
        assert mgr.result()['state'] == 'idle'
        # The switch never ran
        nm.add_wifi_connection.assert_not_called()

    def test_cancel_failed_returns_to_idle(self, mgr, nm):
        nm.activate_connection.return_value = False
        nm.last_error = 'auth'
        mgr.request_join('HomeNet', 'badpass99')
        CapturingTimer.fire_all()
        assert mgr.result()['state'] == 'failed'
        mgr.cancel()
        result = mgr.result()
        assert result['state'] == 'idle'
        assert result['error'] is None

    def test_cancel_during_switch_raises(self, mgr, nm):
        mgr.state = 'switching'
        with pytest.raises(RuntimeError):
            mgr.cancel()

    def test_cancel_idle_is_noop(self, mgr):
        mgr.cancel()
        assert mgr.result()['state'] == 'idle'


@pytest.mark.unit
class TestResultShape:
    def test_result_fields(self, mgr):
        result = mgr.result()
        assert set(result) == {'state', 'ssid', 'ip', 'error', 'warning',
                               'previous'}
        assert result['state'] == 'idle'
        assert result['previous'] is None
