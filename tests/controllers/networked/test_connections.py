"""The controller's connection bookkeeping, on a real CentralController:

- check_connections (one real pass of the loop): a silent OWL goes offline,
  then off the screen; the pass re-sends a lost Stop; one failing pass does
  not end the checker.
- an OWL that (re)joins is put back to what the screen shows, through the
  real broker entry point _on_message.
- collect mode holds spraying off on every OWL that connects.
"""

import time

import pytest


class _EndOfTest(BaseException):
    """Not an Exception: the loop's own `except Exception` must not eat it."""


def run_checker(ctrl, monkeypatch, passes=1):
    """Run check_connections for `passes` passes. Returns the sleeps it asked for."""
    import controller.networked.networked as net_mod

    sleeps = []

    class Clock:
        def __getattr__(self, name):
            return getattr(time, name)

        def sleep(self, seconds):
            sleeps.append(seconds)
            if len(sleeps) >= passes:
                raise _EndOfTest

    with monkeypatch.context() as patched:
        patched.setattr(net_mod, 'time', Clock())
        with pytest.raises(_EndOfTest):
            ctrl.check_connections()
    return sleeps


def silent_for(ctrl, oid, seconds):
    ctrl.owls_state[oid]['last_seen'] = time.time() - seconds


def reports(ctrl, oid, request, source='dashboard'):
    ctrl.owls_state[oid]['spray'] = {'request': request, 'reason': None, 'source': source,
                                     'requested_at': 1.0}


def age_spray_sends(ctrl, seconds=10):
    for oid in list(ctrl._spray_sent_at):
        ctrl._spray_sent_at[oid] -= seconds


# --------------------------------------------------------------------------
# check_connections
# --------------------------------------------------------------------------

def test_a_silent_owl_goes_offline_after_15s_and_off_the_list_after_60s(make_controller, monkeypatch):
    ctrl = make_controller(owls=('fresh', 'quiet-14s', 'quiet-16s', 'quiet-59s', 'quiet-61s'))
    for oid, seconds in [('quiet-14s', 14), ('quiet-16s', 16), ('quiet-59s', 59), ('quiet-61s', 61)]:
        silent_for(ctrl, oid, seconds)

    assert run_checker(ctrl, monkeypatch) == [2], 'one pass, then a 2 s wait'

    connected = {oid: st['connected'] for oid, st in ctrl.owls_state.items()}
    assert connected == {'fresh': True, 'quiet-14s': True,
                         'quiet-16s': False, 'quiet-59s': False}, 'quiet-61s is removed'


def test_a_pass_resends_a_stop_the_owl_never_acted_on(make_controller, monkeypatch, sent):
    ctrl = make_controller(owls=('owl-1', 'owl-2', 'gone'))
    ctrl.send_command('all', 'toggle_detection', False)          # Stop all
    assert ctrl.desired_state['all']['spray'] == 'off'
    reports(ctrl, 'owl-1', 'spot')                               # never got it
    reports(ctrl, 'owl-2', 'off')                                # did
    reports(ctrl, 'gone', 'spot')
    silent_for(ctrl, 'gone', 16)
    age_spray_sends(ctrl)
    ctrl.mqtt_client.publish.reset_mock()

    run_checker(ctrl, monkeypatch)

    assert sent(ctrl, 'owl-1') == [{'action': 'set_detection_enable', 'value': False}]
    assert sent(ctrl, 'owl-2') == []
    assert sent(ctrl, 'gone') == [], 'an OWL that has gone quiet is not sent anything'
    assert ctrl.owls_state['gone']['connected'] is False


def test_a_pass_never_turns_spraying_on_for_an_owl_that_stopped_itself(make_controller, monkeypatch, sent):
    ctrl = make_controller()
    ctrl.send_command('all', 'toggle_detection', True)
    ctrl.owls_state['owl-1']['spray'] = {'request': 'off', 'reason': 'fault:off:camera_lost',
                                         'source': 'inhibit', 'requested_at': 2.0}
    age_spray_sends(ctrl)
    ctrl.mqtt_client.publish.reset_mock()

    run_checker(ctrl, monkeypatch)

    assert sent(ctrl, 'owl-1') == []
    assert ctrl.desired_state['owl-1']['spray'] == 'off'


def test_a_pass_with_the_broker_down_publishes_nothing_but_still_marks_offline(
        make_controller, monkeypatch):
    ctrl = make_controller(owls=('owl-1', 'quiet'))
    ctrl.send_command('all', 'toggle_detection', False)
    reports(ctrl, 'owl-1', 'spot')
    silent_for(ctrl, 'quiet', 16)
    age_spray_sends(ctrl)
    ctrl.mqtt_client.publish.reset_mock()
    ctrl.mqtt_connected = False

    run_checker(ctrl, monkeypatch)

    assert ctrl.mqtt_client.publish.call_args_list == []
    assert ctrl.owls_state['quiet']['connected'] is False


def test_the_gps_track_stops_when_the_only_spraying_owl_goes_silent(make_controller, monkeypatch):
    from unittest.mock import create_autospec
    from utils.gps_manager import GPSManager

    ctrl = make_controller()
    ctrl.owls_state['owl-1']['detection_enable'] = True
    gps = create_autospec(GPSManager, instance=True)
    gps.session_active = False
    ctrl.gps_manager = gps

    run_checker(ctrl, monkeypatch)
    gps.start_session.assert_called_once_with()
    gps.stop_session.assert_not_called()

    gps.session_active = True
    gps.start_session.reset_mock()
    silent_for(ctrl, 'owl-1', 16)
    run_checker(ctrl, monkeypatch)
    gps.stop_session.assert_called_once_with()
    gps.start_session.assert_not_called()


def test_one_failing_pass_does_not_end_the_checker(make_controller, monkeypatch, sent):
    ctrl = make_controller()
    ctrl.send_command('all', 'toggle_detection', False)
    reports(ctrl, 'owl-1', 'spot')
    age_spray_sends(ctrl)
    ctrl.mqtt_client.publish.reset_mock()

    real = ctrl._update_session_state
    calls = []

    def fails_once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('GPS track directory vanished')
        return real()

    ctrl._update_session_state = fails_once

    assert run_checker(ctrl, monkeypatch, passes=2) == [5, 2], 'back off 5 s, then carry on'
    assert sent(ctrl, 'owl-1') == [{'action': 'set_detection_enable', 'value': False}], \
        'the pass after the failure still re-sent the Stop'


# --------------------------------------------------------------------------
# (Re)joining OWLs: _on_message -> _push_desired_state
# --------------------------------------------------------------------------

def test_an_owl_that_joins_after_stop_all_is_told_to_stop(make_controller, hears, sent):
    ctrl = make_controller(owls=())
    ctrl.send_command('all', 'toggle_detection', False)
    assert ctrl.mqtt_client.publish.call_args_list == [], 'nobody connected yet'

    hears(ctrl, 'owl-7', {'detection_enable': True, 'detection_mode': 0})
    assert sent(ctrl, 'owl-7') == [{'action': 'set_detection_enable', 'value': False}]

    hears(ctrl, 'owl-7', {'detection_enable': True, 'detection_mode': 0})
    assert len(sent(ctrl, 'owl-7')) == 1, 'pushed once on joining, not on every heartbeat'


@pytest.mark.parametrize('fleet, own, command', [
    ('spot', 'off', {'action': 'set_detection_enable', 'value': False}),
    ('off', 'blanket', {'action': 'set_detection_mode', 'value': 2}),
    ('blanket', None, {'action': 'set_detection_mode', 'value': 2}),
])
def test_a_rejoining_owl_gets_its_own_spray_state_over_the_fleets(
        make_controller, monkeypatch, hears, sent, fleet, own, command):
    ctrl = make_controller(owls=('owl-1', 'owl-2'))
    if fleet == 'blanket':
        ctrl.send_command('all', 'toggle_all_nozzles', True)
    else:
        ctrl.send_command('all', 'toggle_detection', fleet == 'spot')
    if own == 'off':
        ctrl.send_command('owl-2', 'toggle_detection', False)
    elif own == 'blanket':
        ctrl.send_command('owl-2', 'toggle_all_nozzles', True)

    silent_for(ctrl, 'owl-2', 16)
    run_checker(ctrl, monkeypatch)                 # owl-2 times out
    assert ctrl.owls_state['owl-2']['connected'] is False
    ctrl.mqtt_client.publish.reset_mock()

    hears(ctrl, 'owl-2', {'detection_enable': False})
    assert sent(ctrl, 'owl-2') == [command]
    assert sent(ctrl, 'owl-1') == []
    assert ctrl.owls_state['owl-2']['connected'] is True


def test_a_rejoining_owl_gets_spray_then_recording_in_that_order(make_controller, hears, sent):
    ctrl = make_controller(owls=())
    ctrl.send_command('all', 'toggle_detection', False)
    ctrl.send_command('all', 'toggle_recording', True)
    ctrl.send_command('all', 'set_tracking', False)

    hears(ctrl, 'owl-3', {'detection_enable': True})
    assert sent(ctrl, 'owl-3') == [
        {'action': 'set_detection_enable', 'value': False},
        {'action': 'set_image_sample_enable', 'value': True},
        {'action': 'set_tracking', 'value': False}]


def test_nothing_asked_for_means_nothing_pushed(make_controller, hears):
    ctrl = make_controller(owls=())
    hears(ctrl, 'owl-1', {'detection_enable': True})
    assert ctrl.mqtt_client.publish.call_args_list == []
    assert ctrl.owls_state['owl-1']['connected'] is True


@pytest.mark.parametrize('status', [{'connected': False}, {'connected': True, 'owl_running': False}])
def test_a_last_will_takes_the_owl_offline_at_once(make_controller, hears, status):
    ctrl = make_controller()
    hears(ctrl, 'owl-1', status, kind='status')
    assert ctrl.owls_state['owl-1']['connected'] is False


def test_a_heartbeat_still_in_flight_after_the_last_will_is_not_a_rejoin(
        make_controller, monkeypatch, hears, sent):
    ctrl = make_controller()
    ctrl.send_command('all', 'toggle_detection', False)
    ctrl.mqtt_client.publish.reset_mock()

    hears(ctrl, 'owl-1', {'connected': False}, kind='status')
    hears(ctrl, 'owl-1', {'detection_enable': True})            # sent before it died
    assert sent(ctrl, 'owl-1') == []

    # It stays silent, times out, and boots half a minute later
    silent_for(ctrl, 'owl-1', 16)
    ctrl.lwt_timestamps['owl-1'] -= 30
    run_checker(ctrl, monkeypatch)
    ctrl.mqtt_client.publish.reset_mock()
    hears(ctrl, 'owl-1', {'detection_enable': True})
    assert sent(ctrl, 'owl-1') == [{'action': 'set_detection_enable', 'value': False}]


@pytest.mark.xfail(strict=True, reason=(
    "controller/networked/networked.py _on_message: a heartbeat arriving within 5 s of an "
    "OWL's last will marks it connected again and uses up the rejoin (the push is "
    "suppressed, `is_reconnect` branch near 'Suppressed reconnect push'). If the OWL is "
    "back before the 15 s offline timeout, its first real heartbeat is not a rejoin, so "
    "recording / model / tracking are never pushed. (Spray is: _reconcile_spray re-sends it.)"))
def test_an_owl_that_restarts_within_15s_of_its_last_will_still_gets_recording_back(
        make_controller, hears, sent):
    ctrl = make_controller()
    ctrl.send_command('all', 'toggle_recording', True)
    ctrl.mqtt_client.publish.reset_mock()

    hears(ctrl, 'owl-1', {'connected': False}, kind='status')
    hears(ctrl, 'owl-1', {'image_sample_enable': True})         # in flight when it died
    ctrl.lwt_timestamps['owl-1'] -= 10                          # ten seconds later it is back
    hears(ctrl, 'owl-1', {'image_sample_enable': False})

    assert sent(ctrl, 'owl-1') == [{'action': 'set_image_sample_enable', 'value': True}]


def test_a_message_that_is_not_json_creates_no_owl(make_controller, hears):
    ctrl = make_controller(owls=())
    hears(ctrl, 'owl-1', b'{ not json')
    assert ctrl.owls_state == {}


# --------------------------------------------------------------------------
# Collect mode
# --------------------------------------------------------------------------

def start_up(ctrl, monkeypatch):
    """init_app() on this controller, with the broker connection and the two
    endless loops replaced. Returns the loops it started."""
    import controller.networked.networked as net_mod
    from unittest.mock import MagicMock

    started = []
    ctrl.setup_mqtt = MagicMock()
    monkeypatch.setattr(ctrl, 'check_connections', lambda: started.append('checker'))
    monkeypatch.setattr(ctrl, '_actuation_broadcast_loop', lambda: started.append('timing'))
    monkeypatch.setattr(net_mod, 'controller', ctrl)
    net_mod.init_app()
    deadline = time.monotonic() + 2
    while len(started) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    ctrl.setup_mqtt.assert_called_once_with()
    return sorted(started)


def test_collect_mode_holds_spraying_off_on_every_owl_that_connects(
        make_controller, monkeypatch, hears, sent):
    import controller.networked.networked as net_mod

    ctrl = make_controller('[WebDashboard]\nui = collect\n', owls=())
    assert start_up(ctrl, monkeypatch) == ['checker', 'timing']

    hears(ctrl, 'owl-1', {'detection_enable': True})
    pushed = sent(ctrl, 'owl-1')
    assert pushed[0] == {'action': 'set_detection_enable', 'value': False}
    assert [p['action'] for p in pushed] == ['set_detection_enable', 'set_capture_params']
    assert pushed[1]['params'] == net_mod._collect_capture_settings()
    assert pushed[1]['params'], 'the capture settings are not empty'


def test_full_mode_start_up_asks_for_nothing(make_controller, monkeypatch, hears):
    ctrl = make_controller(owls=())
    assert start_up(ctrl, monkeypatch) == ['checker', 'timing']
    assert ctrl.desired_state == {}
    hears(ctrl, 'owl-1', {'detection_enable': True})
    assert ctrl.mqtt_client.publish.call_args_list == []
