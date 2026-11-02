"""Temporary join (internet via the phone's own hotspot) — the Maintenance
card path in controller/standalone/network_settings.py.

Reuses the MagicMock NetworkManager + CapturingTimer fixtures from
test_network_settings_manager.py so the retry loop, watchdog and revert
tail all run synchronously.
"""

import json
from unittest.mock import patch

import pytest

from controller.standalone import network_settings as ns
from test_network_settings_manager import (   # noqa: F401 (fixtures)
    CapturingTimer, clear_timers, no_sleep, nm, sender, mgr,
)


def fire_one():
    """Fire only the oldest pending timer (fire_all would also run the
    watchdog the switch arms on success)."""
    CapturingTimer.pending[0].fire()


@pytest.fixture
def clock():
    state = {'now': 1000.0}
    with patch('controller.standalone.network_settings.time.monotonic',
               side_effect=lambda: state['now']):
        yield state


def start(mgr, wait_s=None):
    mgr.request_join('PhoneAP', 'phonepass1', temporary=True, wait_s=wait_s)
    assert mgr.state == 'pending'
    result = mgr.result()
    assert result['temporary'] is True
    assert result['con_name'] == ns.TEMP_CON_NAME


def connect(mgr):
    start(mgr)
    fire_one()
    assert mgr.state == 'connected'


@pytest.mark.unit
class TestTemporaryJoin:
    def test_rejected_unless_on_hotspot(self, mgr, nm):
        nm.status.return_value = {'mode': 'client', 'ssid': 'FarmNet',
                                  'ip': '192.168.1.50'}
        with pytest.raises(RuntimeError):
            mgr.request_join('PhoneAP', 'phonepass1', temporary=True)
        assert mgr.state == 'idle'
        assert CapturingTimer.pending == []

    def test_wait_is_clamped(self, mgr):
        start(mgr, wait_s=999)
        assert mgr.wait_s == ns.TEMP_WAIT_MAX_S
        mgr.cancel()
        start(mgr, wait_s=1)
        assert mgr.wait_s == ns.TEMP_WAIT_MIN_S
        mgr.cancel()
        start(mgr)
        assert mgr.wait_s == ns.TEMP_WAIT_DEFAULT_S

    def test_success_first_attempt_arms_watchdog(self, mgr, nm, clock):
        start(mgr)
        fire_one()
        nm.add_wifi_connection.assert_called_once_with(
            'PhoneAP', 'phonepass1', con_name=ns.TEMP_CON_NAME, autoconnect=False)
        nm.hotspot_down.assert_called_once()
        nm.activate_connection.assert_called_once_with(
            ns.TEMP_CON_NAME, timeout=ns.TEMP_ACTIVATE_TIMEOUT_S)
        result = mgr.result()
        assert result['state'] == 'connected'
        assert result['ip'] == '192.168.1.50'
        assert result['temporary'] is True
        assert result['expires_at'] is not None
        assert len(CapturingTimer.pending) == 1
        assert CapturingTimer.pending[0].interval == ns.TEMP_WATCHDOG_S

    def test_retries_until_the_hotspot_appears(self, mgr, nm, clock):
        nm.activate_connection.side_effect = [False, False, True]
        nm.last_error = 'not_found'
        start(mgr)
        fire_one()
        assert nm.activate_connection.call_count == 3
        assert mgr.result()['state'] == 'connected'

    def test_deadline_reverts_to_hotspot(self, mgr, nm, clock):
        def attempt(*args, **kwargs):
            clock['now'] += 50
            return False
        nm.activate_connection.side_effect = attempt
        nm.last_error = 'not_found'
        start(mgr, wait_s=90)
        fire_one()
        nm.delete_connection.assert_called_with(ns.TEMP_CON_NAME)
        nm.hotspot_up.assert_called_once()
        result = mgr.result()
        assert result['state'] == 'failed'
        assert result['error'] == 'timeout'
        assert result['ended_reason'] == 'timeout'
        assert result['warning'] is None
        assert CapturingTimer.pending == []   # no watchdog

    def test_two_auth_failures_abort_early(self, mgr, nm, clock):
        nm.activate_connection.return_value = False
        nm.last_error = 'auth'
        start(mgr)
        fire_one()
        assert nm.activate_connection.call_count == ns.TEMP_AUTH_FAILURES_TO_ABORT
        assert mgr.result()['error'] == 'auth'
        nm.hotspot_up.assert_called_once()

    def test_watchdog_ends_session_when_link_drops(self, mgr, nm, clock):
        connect(mgr)
        temp_states = ['activated', 'inactive']
        nm.get_hotspot_connection.return_value = 'OWL-4b7d'
        nm.connection_state.side_effect = (
            lambda name: temp_states.pop(0) if name == ns.TEMP_CON_NAME
            else 'inactive')
        fire_one()                                   # alive: re-armed
        assert mgr.state == 'connected'
        assert len(CapturingTimer.pending) == 1
        fire_one()                                   # dropped
        nm.delete_connection.assert_called_with(ns.TEMP_CON_NAME)
        nm.hotspot_up.assert_called_once()
        result = mgr.result()
        assert result['state'] == 'failed'
        assert result['error'] == 'link_lost'
        assert result['ended_reason'] == 'link_lost'
        assert result['ip'] is None
        assert CapturingTimer.pending == []

    def test_watchdog_does_not_kick_an_ap_that_is_already_up(self, mgr, nm, clock):
        connect(mgr)
        nm.get_hotspot_connection.return_value = 'OWL-4b7d'
        nm.connection_state.side_effect = (
            lambda name: 'activated' if name == 'OWL-4b7d' else 'inactive')
        fire_one()
        nm.hotspot_up.assert_not_called()
        assert mgr.result()['warning'] is None

    def test_watchdog_ttl_expiry(self, mgr, nm, clock):
        connect(mgr)
        nm.connection_state.return_value = 'activated'
        clock['now'] += ns.TEMP_TTL_S + 1
        fire_one()
        assert mgr.result()['error'] == 'ttl_expired'
        nm.delete_connection.assert_called_with(ns.TEMP_CON_NAME)

    def test_revert_from_connected(self, mgr, nm, clock):
        connect(mgr)
        nm.connection_state.return_value = 'inactive'
        assert mgr.revert() == 'reverting'
        revert_timer = [t for t in CapturingTimer.pending
                        if t.interval == ns.TEMP_REVERT_DELAY_S]
        assert len(revert_timer) == 1
        revert_timer[0].fire()
        nm.delete_connection.assert_called_with(ns.TEMP_CON_NAME)
        nm.hotspot_up.assert_called_once()
        result = mgr.result()
        assert result['state'] == 'idle'
        assert result['error'] is None
        assert result['ended_reason'] == 'revert'
        assert CapturingTimer.pending == []      # watchdog cancelled

    def test_revert_while_pending_cancels(self, mgr, nm):
        start(mgr)
        assert mgr.revert() == 'cancelled'
        assert mgr.state == 'idle'
        assert mgr.result()['temporary'] is False
        nm.add_wifi_connection.assert_not_called()
        assert CapturingTimer.pending == []

    def test_revert_while_switching_stops_the_retry_loop(self, mgr, nm, clock):
        def attempt(*args, **kwargs):
            assert mgr.revert() == 'cancelling'
            return False
        nm.activate_connection.side_effect = attempt
        nm.last_error = 'not_found'
        start(mgr)
        fire_one()
        assert nm.activate_connection.call_count == 1
        assert mgr.result()['error'] == 'cancelled'
        nm.hotspot_up.assert_called_once()

    def test_revert_rejected_outside_a_temporary_session(self, mgr, nm, clock):
        with pytest.raises(RuntimeError):
            mgr.revert()
        mgr.request_join('HomeNet', 'secret123')     # ordinary join
        with pytest.raises(RuntimeError):
            mgr.revert()
        mgr.cancel()
        connect(mgr)
        nm.connection_state.return_value = 'inactive'
        mgr.revert()
        CapturingTimer.fire_all()
        with pytest.raises(RuntimeError):            # already ended
            mgr.revert()

    def test_ordinary_join_call_shape_unchanged(self, mgr, nm):
        mgr.request_join('HomeNet', 'secret123')
        CapturingTimer.fire_all()
        nm.add_wifi_connection.assert_called_once_with('HomeNet', 'secret123')
        result = mgr.result()
        assert result['temporary'] is False
        assert result['con_name'] is None

    def test_cancel_tears_a_connected_session_down(self, mgr, nm, clock):
        # Left alone, nothing would ever bring the hotspot back
        connect(mgr)
        mgr.cancel()
        result = mgr.result()
        assert result['temporary'] is False
        assert result['expires_at'] is None
        assert [t.interval for t in CapturingTimer.pending] == [ns.TEMP_REVERT_DELAY_S]
        fire_one()
        nm.delete_connection.assert_called_with(ns.TEMP_CON_NAME)
        nm.hotspot_up.assert_called_once()
        assert mgr.state == 'idle'

    def test_cancel_leaves_a_permanent_join_on_its_network(self, mgr, nm):
        mgr.request_join('HomeNet', 'secret123')
        CapturingTimer.fire_all()
        assert mgr.state == 'connected'
        mgr.cancel()
        assert CapturingTimer.pending == []
        nm.hotspot_up.assert_not_called()

    def test_cleanup_stale_temporary_after_a_restart(self, mgr, nm, tmp_path):
        # v3.26.0: an update restarted the dashboard mid-session and the
        # hotspot never came back
        (tmp_path / 'net-switching').write_text('999999')
        nm.status.return_value = {'mode': 'none', 'ssid': None, 'ip': None}
        mgr.cleanup_stale_temporary()
        nm.delete_connection.assert_called_once_with(ns.TEMP_CON_NAME)
        nm.allow_hotspot_autoconnect.assert_called_once()
        # Unblocked first: the delete frees the radio and NetworkManager
        # chooses the next profile right then
        names = [c[0] for c in nm.mock_calls]
        assert names.index('allow_hotspot_autoconnect') < names.index('delete_connection')
        assert not (tmp_path / 'net-switching').exists()
        assert [t.interval for t in CapturingTimer.pending] == [ns.HOTSPOT_RETRY_S]
        fire_one()
        nm.hotspot_up.assert_called_once()

    def test_cleanup_stale_temporary_leaves_a_working_network(self, mgr, nm):
        nm.status.return_value = {'mode': 'client', 'ssid': 'FarmNet', 'ip': '192.168.1.50'}
        mgr.cleanup_stale_temporary()
        fire_one()
        nm.hotspot_up.assert_not_called()
        assert CapturingTimer.pending == []


@pytest.mark.unit
class TestHotspotRecovery:
    def test_join_unblocks_hotspot_autoconnect(self, mgr, nm, clock):
        connect(mgr)
        nm.allow_hotspot_autoconnect.assert_called_once()

    def test_unblock_failure_does_not_fail_the_join(self, mgr, nm, clock):
        nm.allow_hotspot_autoconnect.side_effect = RuntimeError('nmcli')
        connect(mgr)
        assert mgr.state == 'connected'

    def test_marker_held_only_while_switching(self, mgr, nm, clock, tmp_path):
        marker = tmp_path / 'net-switching'
        seen = []
        nm.activate_connection.side_effect = (
            lambda *a, **k: seen.append(marker.read_text()) or True)
        connect(mgr)
        assert seen == [str(ns.os.getpid())]
        assert not marker.exists()

    def test_marker_cleared_when_the_scan_fails(self, mgr, nm, tmp_path):
        nm.scan.side_effect = RuntimeError('scan failed')
        mgr.start_hotspot_scan()
        CapturingTimer.fire_all()
        assert not (tmp_path / 'net-switching').exists()

    def test_three_unreadable_link_states_end_the_session(self, mgr, nm, clock):
        connect(mgr)
        nm.connection_state.side_effect = RuntimeError('nmcli hung')
        fire_one()
        fire_one()
        assert mgr.state == 'connected'
        fire_one()
        assert mgr.result()['error'] == 'link_lost'

    def test_one_bad_read_does_not(self, mgr, nm, clock):
        connect(mgr)
        reads = [RuntimeError('blip'), RuntimeError('blip'), 'activated', RuntimeError('blip')]

        def state(name):
            value = reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        nm.connection_state.side_effect = state
        for _ in range(4):
            fire_one()
        assert mgr.state == 'connected'

    def test_activating_hotspot_is_not_restarted(self, mgr, nm, clock):
        connect(mgr)
        nm.get_hotspot_connection.return_value = 'OWL-4b7d'
        nm.connection_state.side_effect = (
            lambda name: 'activating' if name == 'OWL-4b7d' else 'inactive')
        fire_one()
        nm.hotspot_up.assert_not_called()
        assert mgr.result()['warning'] is None

    def test_failed_restore_keeps_retrying(self, mgr, nm, clock):
        connect(mgr)
        nm.connection_state.return_value = 'inactive'
        nm.hotspot_up.side_effect = RuntimeError('radio busy')
        fire_one()                                   # link lost, 3 tries fail
        assert nm.hotspot_up.call_count == ns.REVERT_ATTEMPTS
        assert mgr.result()['warning'] == 'previous_unavailable'
        assert [t.interval for t in CapturingTimer.pending] == [ns.HOTSPOT_RETRY_S]
        nm.status.return_value = {'mode': 'none', 'ssid': None, 'ip': None}
        fire_one()                                   # still failing: again
        assert len(CapturingTimer.pending) == 1
        nm.hotspot_up.side_effect = None
        fire_one()
        assert nm.hotspot_up.call_count == ns.REVERT_ATTEMPTS + 2
        assert CapturingTimer.pending == []

    def test_retry_stops_without_a_hotspot_profile(self, mgr, nm):
        nm.status.return_value = {'mode': 'none', 'ssid': None, 'ip': None}
        nm.hotspot_up.side_effect = ns.NetworkManagerError('none', kind='not_found')
        mgr._schedule_hotspot_retry()
        fire_one()
        assert CapturingTimer.pending == []

    def test_retry_stays_out_of_a_new_switch(self, mgr, nm):
        nm.status.return_value = {'mode': 'none', 'ssid': None, 'ip': None}
        mgr._schedule_hotspot_retry()
        mgr.state = 'switching'
        fire_one()
        nm.hotspot_up.assert_not_called()


@pytest.mark.unit
class TestBeacon:
    def test_sent_to_gateway_on_connect(self, mgr, nm, sender, clock):
        connect(mgr)
        sender.assert_called_once()
        source_ip, gateway, port, payload = sender.call_args[0]
        assert source_ip == '192.168.1.50'
        assert gateway == '10.134.76.213'
        assert port == ns.BEACON_PORT == 47931
        assert json.loads(payload) == {'owl': 'phone-internet',
                                       'ip': '192.168.1.50'}
        assert mgr.gateway == '10.134.76.213'

    def test_sent_again_on_alive_watchdog_tick(self, mgr, nm, sender, clock):
        connect(mgr)
        nm.connection_state.return_value = 'activated'
        fire_one()
        assert sender.call_count == 2
        assert len(CapturingTimer.pending) == 1

    def test_not_sent_after_session_ends(self, mgr, nm, sender, clock):
        connect(mgr)
        nm.connection_state.return_value = 'inactive'
        fire_one()                                   # link lost
        assert mgr.state == 'failed'
        assert mgr.gateway is None
        mgr._send_beacon()
        assert sender.call_count == 1

    def test_not_sent_after_revert(self, mgr, nm, sender, clock):
        connect(mgr)
        nm.connection_state.return_value = 'inactive'
        mgr.revert()
        CapturingTimer.fire_all()
        mgr._send_beacon()
        assert sender.call_count == 1

    def test_sender_error_does_not_break_watchdog(self, mgr, nm, sender, clock):
        sender.side_effect = OSError('network unreachable')
        connect(mgr)
        nm.connection_state.return_value = 'activated'
        fire_one()
        assert mgr.state == 'connected'
        assert len(CapturingTimer.pending) == 1
        assert sender.call_count == 2

    def test_no_gateway_no_send(self, mgr, nm, sender, clock):
        nm.get_gateway4.return_value = None
        connect(mgr)
        nm.connection_state.return_value = 'activated'
        fire_one()
        sender.assert_not_called()

    def test_gateway_read_error_still_connects(self, mgr, nm, sender, clock):
        nm.get_gateway4.side_effect = RuntimeError('nmcli gone')
        connect(mgr)
        assert mgr.gateway is None
        sender.assert_not_called()
