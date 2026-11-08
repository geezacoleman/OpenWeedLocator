"""Networked controller, v3.27: the OWLs follow the screen.

- Stop all is respected: no stale per-OWL override survives it.
- Commands are absolute; a bare toggle uses what the screen shows.
- One spray command per OWL on reconnect (old OWLs undid a blanket when sent
  both detection_enable and detection_mode).
- Reconciliation re-sends a lost command, adopts an OWL's own stop
  (first-boot, switch off, fault) instead of fighting it, follows a fresh
  flip of the OWL's own switch, and flags an OWL that never answers.
- Hold to spray is a lease; OWLs older than 3.27 get the old blanket on/off.
"""

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def ctrl():
    with patch('controller.networked.networked.CentralController.__init__', lambda self, **kw: None):
        from controller.networked.networked import CentralController
        c = CentralController.__new__(CentralController)
        c.owls_state = {}
        c.desired_state = {}
        c.lwt_timestamps = {}
        c.mqtt_connected = True
        c.mqtt_lock = threading.Lock()
        c.mqtt_client = MagicMock()
        c.mqtt_client.publish.return_value = MagicMock(rc=0)
        c.offline_timeout = 15.0
        c.gps_manager = None
        return c


def owl(c, oid, **state):
    c.owls_state[oid] = dict({'connected': True, 'last_seen': time.time()}, **state)


def sent(c, oid=None):
    out = []
    for call in c.mqtt_client.publish.call_args_list:
        topic, payload = call.args[0], json.loads(call.args[1])
        if oid is None or topic == f'owl/{oid}/commands':
            out.append(payload)
    return out


def spray_state(request, reason=None, source='dashboard', token=1.0):
    return {'spray': {'request': request, 'reason': reason, 'source': source,
                      'requested_at': token}}


def age_sends(c):
    for oid in list(c._spray_sent_at):
        c._spray_sent_at[oid] -= 10


@pytest.mark.unit
class TestStopAll:
    def test_stop_all_clears_a_stale_override_from_an_offline_owl(self, ctrl):
        owl(ctrl, 'owl-1')
        ctrl.send_command('owl-2', 'toggle_all_nozzles', True)   # owl-2 alone on blanket
        assert ctrl.desired_state['owl-2']['spray'] == 'blanket'
        ctrl.send_command('all', 'toggle_detection', False)      # Stop all, owl-2 offline
        assert 'spray' not in ctrl.desired_state['owl-2']
        ctrl.mqtt_client.publish.reset_mock()
        ctrl._push_desired_state('owl-2')                       # owl-2 comes back
        assert sent(ctrl, 'owl-2') == [{'action': 'set_detection_enable', 'value': False}]

    def test_set_all_reaches_owls_not_listed(self, ctrl):
        ctrl.desired_state = {'owl-9': {'tracking_enabled': False}}
        ctrl.send_command('all', 'set_tracking', True)
        assert 'tracking_enabled' not in ctrl.desired_state['owl-9']


@pytest.mark.unit
class TestAbsoluteCommands:
    def test_toggle_detection_with_value_is_absolute(self, ctrl):
        owl(ctrl, 'owl-1', detection_enable=True)   # reported on
        ctrl.send_command('owl-1', 'toggle_detection', True)
        assert sent(ctrl) == [{'action': 'set_detection_enable', 'value': True}]

    def test_bare_toggle_uses_the_screen_not_the_report(self, ctrl):
        owl(ctrl, 'owl-1', detection_enable=True)   # stale report says on
        ctrl.desired_state = {'all': {'spray': 'off'}}
        ctrl.send_command('owl-1', 'toggle_detection')
        assert sent(ctrl) == [{'action': 'set_detection_enable', 'value': True}]

    def test_all_nozzles_for_one_owl_stays_with_that_owl(self, ctrl):
        owl(ctrl, 'owl-1')
        owl(ctrl, 'owl-2')
        ctrl.send_command('owl-1', 'toggle_all_nozzles', True)
        assert ctrl.desired_state['owl-1']['spray'] == 'blanket'
        assert 'spray' not in ctrl.desired_state.get('all', {})
        assert sent(ctrl, 'owl-2') == []


@pytest.mark.unit
class TestPush:
    @pytest.mark.parametrize('spray, command', [
        ('blanket', {'action': 'set_detection_mode', 'value': 2}),
        ('spot', {'action': 'set_detection_enable', 'value': True}),
        ('off', {'action': 'set_detection_enable', 'value': False})])
    def test_exactly_one_spray_command(self, ctrl, spray, command):
        ctrl.desired_state = {'all': {'spray': spray}}
        ctrl._push_desired_state('owl-1')
        spray_cmds = [p for p in sent(ctrl) if p['action'] in ('set_detection_mode',
                                                              'set_detection_enable')]
        assert spray_cmds == [command]

    def test_legacy_desired_keys_still_push(self, ctrl):
        # collect mode and older code write detection_enable
        ctrl.desired_state = {'all': {'detection_enable': False}}
        ctrl._push_desired_state('owl-1')
        assert {'action': 'set_detection_enable', 'value': False} in sent(ctrl)


@pytest.mark.unit
class TestReconcile:
    def test_lost_command_is_re_sent(self, ctrl):
        owl(ctrl, 'owl-1', **spray_state('spot'))
        ctrl.send_command('all', 'toggle_detection', False)
        ctrl.mqtt_client.publish.reset_mock()
        ctrl._reconcile_spray()
        assert sent(ctrl) == [], 'its answer may still be on the way'
        age_sends(ctrl)
        ctrl._reconcile_spray()
        assert sent(ctrl, 'owl-1') == [{'action': 'set_detection_enable', 'value': False}]

    def test_old_owl_is_reconciled_from_legacy_fields(self, ctrl):
        owl(ctrl, 'owl-1', detection_enable=False, detection_mode=1)
        ctrl.desired_state = {'all': {'spray': 'blanket'}}
        ctrl._reconcile_spray()
        assert sent(ctrl, 'owl-1') == [{'action': 'set_detection_mode', 'value': 2}]

    @pytest.mark.parametrize('reason', ['switch_off', 'firstboot', 'fault:off:camera_lost',
                                        'setup_finished', 'link_lost'])
    def test_an_owl_that_stopped_itself_is_adopted_not_restarted(self, ctrl, reason):
        owl(ctrl, 'owl-1', **spray_state('off', reason=reason, source='inhibit'))
        ctrl.desired_state = {'all': {'spray': 'spot'}}
        ctrl._reconcile_spray()
        assert sent(ctrl) == [], 'spraying must never resume by itself'
        assert ctrl.desired_state['owl-1']['spray'] == 'off'

    def test_a_fresh_switch_flip_on_the_owl_is_followed(self, ctrl):
        owl(ctrl, 'owl-1', **spray_state('off', reason='switch_off', source='inhibit', token=1.0))
        ctrl.desired_state = {'all': {'spray': 'spot'}}
        ctrl._reconcile_spray()                       # adopts off
        ctrl.owls_state['owl-1'].update(spray_state('spot', source='switch', token=2.0))
        ctrl._reconcile_spray()
        assert sent(ctrl) == [], 'the operator switched it on: do not switch it off again'
        assert ctrl.desired_state['owl-1']['spray'] == 'spot'

    def test_an_old_switch_state_does_not_overrule_a_stop(self, ctrl):
        owl(ctrl, 'owl-1', **spray_state('spot', source='switch', token=1.0))
        ctrl._reconcile_spray()                       # nothing desired yet; token seen
        ctrl.send_command('all', 'toggle_detection', False)   # Stop all, lost
        ctrl.mqtt_client.publish.reset_mock()
        age_sends(ctrl)
        ctrl._reconcile_spray()
        assert sent(ctrl, 'owl-1') == [{'action': 'set_detection_enable', 'value': False}]

    def test_not_responding_after_three_unanswered_sends(self, ctrl):
        owl(ctrl, 'owl-1', **spray_state('off'))
        ctrl.desired_state = {'all': {'spray': 'spot'}}
        for _ in range(3):
            ctrl._reconcile_spray()
            age_sends(ctrl)
        assert ctrl.owls_state['owl-1']['controller_notice'] == 'Not responding to commands'
        ctrl.owls_state['owl-1'].update(spray_state('spot'))
        ctrl._reconcile_spray()
        assert ctrl.owls_state['owl-1']['controller_notice'] is None

    def test_offline_or_silent_owls_are_left_alone(self, ctrl):
        owl(ctrl, 'owl-1', **spray_state('off'))
        ctrl.owls_state['owl-1']['last_seen'] = time.time() - 10
        ctrl.desired_state = {'all': {'spray': 'spot'}}
        ctrl._reconcile_spray()
        assert sent(ctrl) == []


@pytest.mark.unit
class TestHold:
    def test_hold_is_a_lease_for_new_owls(self, ctrl):
        owl(ctrl, 'owl-1', spray={'request': 'off'})
        ctrl.send_command('all', 'hold_spray', 1500)
        assert sent(ctrl) == [{'action': 'hold_spray', 'value': 1500}]
        assert 'spray' not in ctrl.desired_state.get('all', {}), 'a hold is never restored'

    def test_old_owls_get_blanket_on_and_off(self, ctrl):
        owl(ctrl, 'owl-1', detection_enable=False)
        ctrl.send_command('all', 'hold_spray', 1500)
        ctrl.send_command('all', 'hold_spray', 0)
        assert sent(ctrl) == [{'action': 'set_detection_mode', 'value': 2},
                              {'action': 'set_detection_mode', 'value': 1}]

    def test_lease_is_bounded(self, ctrl):
        owl(ctrl, 'owl-1', spray={'request': 'off'})
        ctrl.send_command('owl-1', 'hold_spray', 99999)
        assert sent(ctrl) == [{'action': 'hold_spray', 'value': 1500}]
