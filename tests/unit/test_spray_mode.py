"""Truth table for utils/spray_mode.resolve: every input combination.

The rules come from the product owner (v3.27 decisions): first-boot and a
hardware switch OFF always win; on_error = off turns the nozzles off on any
fault; on_error = on blanket-sprays only when detection is impossible and
spraying was asked for; link loss stops a blanket only under on_error = off.

A failure here means the code drifted from the decided rules (readthedocs
docs/software/spray-behaviour.md). Fix the code; do not edit expected() to
"fail off" unless the product owner changed the rule and the page with it.
"""

import itertools

import pytest

from utils import spray_mode as sm
from utils.spray_mode import OFF, SPOT, BLANKET, SprayInputs, resolve

FAULT_SETS = [
    {},
    {'relay_stuck': True},          # detection still possible
    {'control_lost': True},
    {'camera_lost': False},         # detection impossible
    {'camera_lost': False, 'relay_stuck': True},
]


def expected(request, switch, firstboot, faults, on_error, link_lost):
    """The rules written out a second, independent way."""
    if firstboot:
        return OFF, 'firstboot'
    if switch == 'off':
        return OFF, 'switch_off'
    if on_error == 'off':
        if faults:
            code = 'relay_stuck' if 'relay_stuck' in faults else next(iter(faults))
            return OFF, 'fault:off:' + code
        if link_lost and request == BLANKET:
            return OFF, 'link_lost'
        return request, None
    # on_error = on
    if request != OFF and any(not possible for possible in faults.values()):
        return BLANKET, 'fault:camera_lost'
    return request, None


CASES = list(itertools.product(
    (OFF, SPOT, BLANKET),
    (None, 'off', 'on'),
    (False, True),
    range(len(FAULT_SETS)),
    ('off', 'on'),
    (False, True),
))


@pytest.mark.unit
@pytest.mark.parametrize('request_, switch, firstboot, faults_i, on_error, link_lost', CASES)
def test_truth_table(request_, switch, firstboot, faults_i, on_error, link_lost):
    faults = FAULT_SETS[faults_i]
    got = resolve(SprayInputs(request=request_, switch=switch, firstboot=firstboot,
                              faults=dict(faults), on_error=on_error, link_lost=link_lost))
    assert got == expected(request_, switch, firstboot, faults, on_error, link_lost)


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
