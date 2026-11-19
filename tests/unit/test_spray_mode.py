"""Truth table for utils/spray_mode.resolve: every input combination.

The rules come from the product owner (v3.27 decisions): first-boot and a
hardware switch OFF always win; on_error = off turns the nozzles off on any
fault; on_error = on blanket-sprays only when detection is impossible and
spraying was asked for; link loss stops a blanket only under on_error = off.

The table below is the decision, written out row by row. It is NOT computed:
an earlier version derived its expectations from a second implementation of
the rules in this file, so a wrong rule copied into both passed. A failure
here means the code drifted from the decided rules (readthedocs
docs/software/spray-behaviour.md). Fix the code; do not edit a row to "fail
off" unless the product owner changed the rule and the page with it.

All 360 combinations (3 requests x 3 switch states x first-boot x 5 fault
sets x on_error x link) are still walked: 180 by the first-boot test, 60 by
the switch-off test and 120 by the 20 table rows (3 requests x 2 switch
states each).
"""

import itertools

import pytest

from utils import spray_mode as sm
from utils.spray_mode import OFF, SPOT, BLANKET, SprayInputs, resolve

# {code: detection still possible}
FAULTS = {
    'none': {},
    'relay stuck': {'relay_stuck': True},
    'control lost': {'control_lost': True},
    'camera lost': {'camera_lost': False},
    'camera lost + relay stuck': {'camera_lost': False, 'relay_stuck': True},
}

AS_ASKED = ((OFF, None), (SPOT, None), (BLANKET, None))


def _all_off(reason):
    return ((OFF, reason),) * 3


# No first-boot, switch absent or ON.
# (on_error, faults, link lost) -> what resolve returns for a request of (off, spot, blanket)
TABLE = [
    # on_error = off: any fault is an inhibit, whatever was asked for
    ('off', 'none',                      False, AS_ASKED),
    ('off', 'none',                      True,  ((OFF, None), (SPOT, None), (OFF, 'link_lost'))),
    ('off', 'relay stuck',               False, _all_off('fault:off:relay_stuck')),
    ('off', 'relay stuck',               True,  _all_off('fault:off:relay_stuck')),
    ('off', 'control lost',              False, _all_off('fault:off:control_lost')),
    ('off', 'control lost',              True,  _all_off('fault:off:control_lost')),
    ('off', 'camera lost',               False, _all_off('fault:off:camera_lost')),
    ('off', 'camera lost',               True,  _all_off('fault:off:camera_lost')),
    ('off', 'camera lost + relay stuck', False, _all_off('fault:off:relay_stuck')),
    ('off', 'camera lost + relay stuck', True,  _all_off('fault:off:relay_stuck')),
    # on_error = on: keep spraying. Link loss changes nothing; only a fault
    # that blinds detection matters, and then only if spraying was asked for
    ('on',  'none',                      False, AS_ASKED),
    ('on',  'none',                      True,  AS_ASKED),
    ('on',  'relay stuck',               False, AS_ASKED),
    ('on',  'relay stuck',               True,  AS_ASKED),
    ('on',  'control lost',              False, AS_ASKED),
    ('on',  'control lost',              True,  AS_ASKED),
    ('on',  'camera lost',               False, ((OFF, None), (BLANKET, 'fault:camera_lost'), (BLANKET, 'fault:camera_lost'))),
    ('on',  'camera lost',               True,  ((OFF, None), (BLANKET, 'fault:camera_lost'), (BLANKET, 'fault:camera_lost'))),
    ('on',  'camera lost + relay stuck', False, ((OFF, None), (BLANKET, 'fault:camera_lost'), (BLANKET, 'fault:camera_lost'))),
    ('on',  'camera lost + relay stuck', True,  ((OFF, None), (BLANKET, 'fault:camera_lost'), (BLANKET, 'fault:camera_lost'))),
]


def test_the_table_has_a_row_for_every_combination():
    rows = {(on_error, faults, link) for on_error, faults, link, _ in TABLE}
    assert rows == set(itertools.product(('off', 'on'), FAULTS, (False, True)))
    assert len(TABLE) == len(rows)


@pytest.mark.unit
@pytest.mark.parametrize('on_error, faults, link_lost, outcomes', TABLE,
                         ids=[f'on_error={o}, {f}, link {"lost" if l else "ok"}' for o, f, l, _ in TABLE])
def test_truth_table(on_error, faults, link_lost, outcomes):
    for switch in (None, 'on'):
        for request, outcome in zip((OFF, SPOT, BLANKET), outcomes):
            got = resolve(SprayInputs(request=request, switch=switch, firstboot=False,
                                      faults=dict(FAULTS[faults]), on_error=on_error,
                                      link_lost=link_lost))
            assert got == outcome, f'request={request}, switch={switch}'


def _everything_else():
    return itertools.product((OFF, SPOT, BLANKET), FAULTS.values(), ('off', 'on'), (False, True))


@pytest.mark.unit
def test_first_boot_is_off_whatever_else_is_true():
    for switch in (None, 'off', 'on'):
        for request, faults, on_error, link_lost in _everything_else():
            got = resolve(SprayInputs(request=request, switch=switch, firstboot=True,
                                      faults=dict(faults), on_error=on_error, link_lost=link_lost))
            assert got == (OFF, 'firstboot'), (request, switch, faults, on_error, link_lost)


@pytest.mark.unit
def test_a_switch_that_is_off_is_off_whatever_else_is_true():
    """Including on_error = on with the camera lost: the switch is a hard kill."""
    for request, faults, on_error, link_lost in _everything_else():
        got = resolve(SprayInputs(request=request, switch='off', firstboot=False,
                                  faults=dict(faults), on_error=on_error, link_lost=link_lost))
        assert got == (OFF, 'switch_off'), (request, faults, on_error, link_lost)


@pytest.mark.unit
class TestRules:
    def test_switch_off_beats_dashboard_blanket(self):
        assert resolve(SprayInputs(request=BLANKET, switch='off')) == (OFF, 'switch_off')

    def test_switch_on_follows_request(self):
        assert resolve(SprayInputs(request=BLANKET, switch='on')) == (BLANKET, None)

    def test_link_loss_never_stops_spot(self):
        for on_error in ('off', 'on'):
            assert resolve(SprayInputs(request=SPOT, link_lost=True, on_error=on_error)) == (SPOT, None)

    def test_link_loss_blanket_keeps_going_under_on_error_on(self):
        assert resolve(SprayInputs(request=BLANKET, link_lost=True, on_error='on')) == (BLANKET, None)

    def test_on_error_on_blanket_only_when_spraying_was_asked_for(self):
        assert resolve(SprayInputs(request=OFF, faults={'camera_lost': False}, on_error='on')) == (OFF, None)

    def test_on_error_on_keeps_spot_when_detection_possible(self):
        assert resolve(SprayInputs(request=SPOT, faults={'relay_stuck': True}, on_error='on')) == (SPOT, None)

    def test_unknown_request_is_off(self):
        assert resolve(SprayInputs(request='sideways')) == (OFF, None)

    def test_on_error_is_case_insensitive(self):
        assert resolve(SprayInputs(request=SPOT, faults={'camera_lost': False}, on_error=' ON ')) == \
            (BLANKET, 'fault:camera_lost')


@pytest.mark.unit
class TestHelpers:
    @pytest.mark.parametrize('reason, inhibit', [
        ('firstboot', True), ('switch_off', True), ('link_lost', True),
        ('fault:off:camera_lost', True), ('fault:camera_lost', False), (None, False), ('', False),
    ])
    def test_is_inhibit(self, reason, inhibit):
        assert sm.is_inhibit(reason) is inhibit

    def test_reason_text(self):
        assert sm.reason_text('fault:off:camera_lost') == 'Camera fault'
        assert sm.reason_text('fault:camera_lost') == 'Camera fault'
        assert sm.reason_text('switch_off') == 'Switch is off'
        assert sm.reason_text('fault:off:mystery_thing') == 'Mystery thing fault'
        assert sm.reason_text(None) == ''

    def test_fault_code(self):
        assert sm.fault_code('fault:off:relay_stuck') == 'relay_stuck'
        assert sm.fault_code('fault:camera_lost') == 'camera_lost'
        assert sm.fault_code('link_lost') is None

    def test_top_fault(self):
        assert sm.top_fault(['link_lost', 'camera_lost']) == 'camera_lost'
        assert sm.top_fault(['relay_stuck', 'camera_lost']) == 'relay_stuck'
        assert sm.top_fault([]) is None

    def test_mode_codes_round_trip(self):
        for mode in sm.MODES:
            assert sm.CODE_TO_MODE[sm.MODE_TO_CODE[mode]] == mode
