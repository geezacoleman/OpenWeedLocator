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
    manager.get_gateway4.return_value = '10.134.76.213'
    manager.status.return_value = {'mode': 'hotspot', 'ssid': 'OWL-4b7d',
                                   'ip': '10.42.0.1', 'hotspot_profile': 'OWL-4b7d'}
    manager.scan.return_value = [
        {'ssid': 'HomeNet', 'signal': 80, 'security': 'WPA2'}]
    manager.activate_connection.return_value = True
    manager.last_error = None
    return manager


@pytest.fixture
def sender():
    """Stand-in for the UDP beacon so no test ever opens a real socket."""
    return MagicMock()


@pytest.fixture
def mgr(nm, sender, tmp_path):
    return NetworkSettingsManager(nm, timer_factory=CapturingTimer,
                                  beacon_sender=sender,
                                  switch_marker=str(tmp_path / 'net-switching'))


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
        # No autoconnect until confirmed (v3.27)
        nm.add_wifi_connection.assert_called_once_with('HomeNet', 'secret123', autoconnect=False)
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

    def test_success_deletes_superseded_profile_once_confirmed(self, mgr, nm):
        mgr.request_join('NewNet', 'secret123')
        CapturingTimer.fire_all()
        assert mgr.result()['state'] == 'connected'
        nm.hotspot_down.assert_not_called()
        nm.delete_connection.assert_not_called()   # kept until the join is confirmed
        assert mgr.confirm() is True
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
                               'previous', 'temporary', 'expires_at',
                               'ended_reason', 'con_name', 'confirmed'}
        assert result['state'] == 'idle'
        assert result['previous'] is None
        assert result['temporary'] is False
        assert result['con_name'] is None


# ---------------------------------------------------------------------------
# v3.27: a permanent join reverts unless confirmed
# ---------------------------------------------------------------------------

@pytest.fixture
def confirm_mgr(nm, sender, tmp_path):
    return NetworkSettingsManager(nm, timer_factory=CapturingTimer, beacon_sender=sender,
                                  switch_marker=str(tmp_path / 'net-switching'),
                                  confirm_timer_factory=ConfirmTimer,
                                  confirm_record=str(tmp_path / 'confirm.json'))


class ConfirmTimer(CapturingTimer):
    """The 120 s confirm timer, fired only when a test says so."""
    armed = []

    def start(self):
        ConfirmTimer.armed.append(self)

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function()


@pytest.fixture(autouse=True)
def clear_confirm_timers():
    ConfirmTimer.armed = []
    yield


@pytest.mark.unit
class TestJoinConfirm:
    def join(self, mgr, ssid='HomeNet'):
        mgr.request_join(ssid, 'secret123')
        CapturingTimer.fire_all()
        assert mgr.result()['state'] == 'connected'

    def test_unconfirmed_join_reverts_to_the_hotspot(self, confirm_mgr, nm):
        self.join(confirm_mgr)
        assert confirm_mgr.result()['confirmed'] is False
        nm.set_autoconnect.assert_not_called()
        ConfirmTimer.armed[-1].fire()
        nm.delete_connection.assert_called_with('HomeNet')
        nm.hotspot_up.assert_called()
        result = confirm_mgr.result()
        assert (result['state'], result['error']) == ('failed', 'not_confirmed')

    def test_confirm_keeps_it_and_turns_autoconnect_on(self, confirm_mgr, nm, tmp_path):
        self.join(confirm_mgr)
        assert (tmp_path / 'confirm.json').exists()
        assert confirm_mgr.confirm() is True
        nm.set_autoconnect.assert_called_once_with('HomeNet', True)
        assert not (tmp_path / 'confirm.json').exists()
        ConfirmTimer.armed[-1].fire()           # too late to matter
        nm.hotspot_up.assert_not_called()
        assert confirm_mgr.result()['confirmed'] is True

    def test_request_from_the_new_subnet_confirms(self, confirm_mgr, nm):
        self.join(confirm_mgr)                 # OWL is 192.168.1.50 now
        assert confirm_mgr.confirm_from('10.42.0.7') is False   # old hotspot subnet
        assert confirm_mgr.confirm_from('192.168.1.23') is True
        nm.set_autoconnect.assert_called_once()

    def test_kiosk_join_confirms_at_once(self, confirm_mgr, nm):
        confirm_mgr.request_join('HomeNet', 'secret123', confirm_now=True)
        CapturingTimer.fire_all()
        nm.add_wifi_connection.assert_called_once_with('HomeNet', 'secret123', autoconnect=True)
        assert confirm_mgr.result()['confirmed'] is True
        assert ConfirmTimer.armed == []

    def test_dashboard_restart_inside_the_window_keeps_waiting(self, nm, sender, tmp_path):
        record = tmp_path / 'confirm.json'
        first = NetworkSettingsManager(nm, timer_factory=CapturingTimer, beacon_sender=sender,
                                       switch_marker=str(tmp_path / 'm'),
                                       confirm_timer_factory=ConfirmTimer,
                                       confirm_record=str(record))
        self.join(first)
        second = NetworkSettingsManager(nm, timer_factory=CapturingTimer, beacon_sender=sender,
                                        switch_marker=str(tmp_path / 'm'),
                                        confirm_timer_factory=ConfirmTimer,
                                        confirm_record=str(record))
        second.resume_pending_confirm()
        assert second.result()['confirmed'] is False
        ConfirmTimer.armed[-1].fire()
        assert second.result()['error'] == 'not_confirmed'

    def test_dashboard_restart_past_the_deadline_reverts(self, nm, sender, tmp_path):
        import json as _json
        record = tmp_path / 'confirm.json'
        record.write_text(_json.dumps({'ssid': 'HomeNet', 'deadline': 1,
                                       'previous': {'mode': 'hotspot', 'ssid': 'OWL-4b7d'}}))
        mgr = NetworkSettingsManager(nm, timer_factory=CapturingTimer, beacon_sender=sender,
                                     switch_marker=str(tmp_path / 'm'),
                                     confirm_timer_factory=ConfirmTimer,
                                     confirm_record=str(record))
        mgr.resume_pending_confirm()
        assert ConfirmTimer.armed[-1].interval == 0.0
        ConfirmTimer.armed[-1].fire()
        nm.hotspot_up.assert_called()

    def test_rejoining_the_same_network_needs_no_confirm(self, confirm_mgr, nm):
        nm.status.return_value = {'mode': 'client', 'ssid': 'HomeNet', 'ip': '192.168.1.50',
                                  'hotspot_profile': 'OWL-4b7d'}
        self.join(confirm_mgr)
        assert confirm_mgr.result()['confirmed'] is True


@pytest.mark.unit
class TestRadioOwner:
    """Item 15 of the 2026-11-07 audit: scan, join, phone session and the
    hotspot retries each drove the radio with no single owner."""

    def test_join_refused_while_a_scan_holds_the_radio(self, mgr):
        mgr.start_hotspot_scan()
        with pytest.raises(RuntimeError, match='scan'):
            mgr.request_join('HomeNet', 'secret123')

    def test_scan_is_a_no_op_while_a_join_owns_the_radio(self, mgr, nm):
        mgr.request_join('HomeNet', 'secret123')
        mgr.start_hotspot_scan()
        assert mgr.scanning is False
        CapturingTimer.fire_all()
        nm.hotspot_down.assert_called_once()     # the join's, not a scan's

    def test_marker_stays_while_any_operation_holds_it(self, mgr, tmp_path):
        marker = tmp_path / 'net-switching'
        mgr._mark_switching()                    # e.g. a join
        mgr._mark_switching()                    # and a revert
        mgr._clear_switching()
        assert marker.exists(), 'the first one finishing un-paused the watchdog'
        mgr._clear_switching()
        assert not marker.exists()

    def test_only_one_hotspot_retry_chain(self, mgr):
        mgr._schedule_hotspot_retry()
        mgr._schedule_hotspot_retry()
        mgr._schedule_hotspot_retry()
        assert len([t for t in CapturingTimer.pending if t.function == mgr._hotspot_retry]) == 1
