"""
What the nozzles do, in every shape of install.

Each test runs a real Owl (harness.Rig) and asserts on the relay drives:
RelayControl.relay_on / relay_off, the last call before the GPIO pin. The
2026-11-16 audit seeded bugs into the detection-to-relay code and none of
the 108 tests in this folder noticed, because they stopped at
RelayController.receive() being called or at `relay_controller.mode`.

    spot      a weed switches its lane's relay, promptly, and lets go
    off       nothing ever switches
    blanket   every relay on until told otherwise
    stopping  a command, a switch, a fault or shutdown ends spray at the pin
    hold / nozzle test   on, and off again by themselves
    timing    configured, controller-sent, band, lane, late frames

Commands go in through the OWL's real MQTT handler (rig.command) and
switches through the real switch-panel controller (rig.switch).
"""

import time

import pytest

from harness import CONFIG_DURATION_S, MATRIX, PROMPT_S

ALL = [0, 1, 2, 3]            # relay_num = 4 in GENERAL_CONFIG.ini
DASHBOARDS = ['standalone', 'networked']


def no_on_since(rig, moment):
    """No relay was switched on after `moment` (monotonic)."""
    late = [(round(at - moment, 3), relay) for at, relay, on in rig.drives if on and at > moment]
    assert not late, f'relays switched on after they should have stopped: {late}'


# --------------------------------------------------------------------------
# Spot spraying
# --------------------------------------------------------------------------

@pytest.mark.parametrize('controller, dash', MATRIX)
def test_spot_a_weed_switches_a_relay_at_once_and_lets_go(rig, controller, dash):
    r = rig(controller=controller, dash=dash)
    assert not r.relays_on(), 'relays on before any weed was seen'

    showing = r.show_card()
    assert showing.fired, f'{controller}/{dash}: a weed in view switched nothing'
    assert showing.on < PROMPT_S, f'relay switched {showing.on:.2f} s after the weed appeared'
    assert showing.off < CONFIG_DURATION_S + PROMPT_S, \
        f'relay stayed on {showing.off:.2f} s after the weed was gone'

    time.sleep(0.4)
    assert not r.relays_on(), 'a relay is still on with nothing in view'


@pytest.mark.parametrize('controller, dash', MATRIX)
def test_spraying_off_never_switches_a_relay(rig, controller, dash):
    r = rig(controller=controller, dash=dash, spray=False)
    showing = r.show_card(settle=1.0)
    assert showing.fired == []
    assert not [d for d in r.drives if d[2]], 'a relay was switched on with spraying off'


@pytest.mark.parametrize('x, relay', [(0.12, 0), (0.38, 1), (0.62, 2), (0.88, 3)])
def test_a_weed_switches_the_relay_of_its_own_lane_only(rig, x, relay):
    r = rig()
    assert r.show_card(x=x).fired == [relay]


def test_continuous_weed_holds_the_relay_on_without_chatter(rig):
    """A patch seen on every frame is one long spray, not a click per frame."""
    r = rig()
    showing = r.show_card(seconds=1.5)
    ons = [d for d in showing.drives if d[2]]
    assert len(ons) == 1, f'relay cycled {len(ons)} times over one continuous weed'
    assert 1.3 <= showing.off - showing.on + (showing.gone - showing.seen) <= 1.5 + CONFIG_DURATION_S + PROMPT_S


@pytest.mark.parametrize('duration', [0.05, 0.4, 1.0])
def test_configured_spray_duration_is_how_long_the_relay_stays_on(rig, duration):
    r = rig(overrides=[('System', 'actuation_duration', str(duration))])
    showing = r.show_card(seconds=0.2)
    assert duration - 0.05 <= showing.off <= duration + PROMPT_S


@pytest.mark.parametrize('delay', [0.3, 0.8])
def test_configured_delay_is_how_long_the_relay_waits(rig, delay):
    r = rig(overrides=[('System', 'delay', str(delay))])
    showing = r.show_card(seconds=0.2)
    assert delay - 0.04 <= showing.on <= delay + PROMPT_S


# --------------------------------------------------------------------------
# The actuation band
# --------------------------------------------------------------------------

@pytest.mark.parametrize('top, bottom, y, fires', [
    (0.5, 1.0, 0.25, False), (0.5, 1.0, 0.75, True),
    (0.0, 0.5, 0.25, True), (0.0, 0.5, 0.75, False),
    (0.4, 0.6, 0.5, True), (0.4, 0.6, 0.15, False), (0.4, 0.6, 0.85, False)])
def test_only_weeds_inside_the_band_spray(rig, top, bottom, y, fires):
    r = rig(overrides=[('System', 'actuation_top', str(top)),
                       ('System', 'actuation_bottom', str(bottom))])
    showing = r.show_card(y=y, settle=1.0)
    assert bool(showing.fired) is fires, f'weed at y={y}, band {top}-{bottom}: fired={showing.fired}'


# --------------------------------------------------------------------------
# Late frames
# --------------------------------------------------------------------------

def test_delay_counts_from_when_the_frame_was_taken_not_when_it_arrived(rig):
    """Each frame reaches the loop 80 ms after exposure. With a 0.3 s
    camera-to-nozzle delay the relay still switches 0.3 s after exposure:
    the time already spent is taken off, not added."""
    r = rig(lag_s=0.08, overrides=[('System', 'delay', '0.3')])
    showing = r.show_card(seconds=0.3)
    assert 0.27 <= showing.on <= 0.36, \
        f'relay switched {showing.on:.3f} s after exposure (0.38+ means the lag was added)'


def test_a_late_frame_with_no_delay_sprays_as_soon_as_it_arrives(rig):
    r = rig(lag_s=0.08)
    showing = r.show_card(seconds=0.3)
    assert 0.08 <= showing.on <= 0.08 + PROMPT_S


# --------------------------------------------------------------------------
# Blanket
# --------------------------------------------------------------------------

@pytest.mark.parametrize('dash', DASHBOARDS)
def test_blanket_from_the_dashboard_holds_every_relay_on(rig, dash):
    r = rig(dash=dash, spray=False)
    r.command('set_detection_mode', value=2)
    r.wait_all_on()

    mark = r.mark()
    r.show_card(settle=0.6)                  # a weed passing changes nothing
    time.sleep(0.5)
    assert r.relays_on() == set(ALL)
    assert not [d for d in r.drives_since(mark) if not d[2]], 'a relay dropped out during blanket'

    r.command('set_detection_mode', value=1)
    r.wait_all_off(timeout=PROMPT_S)
    asked = time.monotonic()
    r.show_card(settle=0.6)
    no_on_since(r, asked)


def test_blanket_from_the_advanced_switch(rig):
    r = rig(controller='advanced', spray=False)
    r.switch(detection=True, blanket=True)
    r.wait_all_on()
    r.switch(detection=False)
    r.wait_all_off(timeout=PROMPT_S)


def test_spot_after_blanket_sprays_the_first_weed(rig):
    """Leaving blanket switches everything off; the next weed must still
    switch its relay (it was missed once: the consumer thread believed the
    relay was already on)."""
    r = rig(dash='standalone', spray=False)
    r.command('set_detection_mode', value=2)
    r.wait_all_on()
    r.command('set_detection_mode', value=0)
    r.wait_all_off(timeout=PROMPT_S)

    showing = r.show_card(x=0.62)
    assert showing.fired == [2]
    assert showing.on < PROMPT_S


def test_ute_switch_off_ends_a_dashboard_blanket_at_the_relays(rig):
    r = rig(controller='ute', dash='standalone')
    r.command('set_detection_mode', value=2)
    r.wait_all_on()
    r.switch(detection=False)
    r.wait_all_off(timeout=PROMPT_S)
    asked = time.monotonic()
    r.command('set_detection_mode', value=2)       # the switch still says off
    time.sleep(0.5)
    no_on_since(r, asked)


# --------------------------------------------------------------------------
# Stopping while a nozzle is open
# --------------------------------------------------------------------------

def hold_a_weed_until_spraying(r):
    r.cam.card = True
    r.wait_for(lambda: r.relays_on(), 'the weed to switch a relay', timeout=3.0)


def stays_off_with_the_weed_still_there(r):
    r.wait_all_off(timeout=PROMPT_S)
    stopped = time.monotonic()
    time.sleep(0.8)                            # the weed is still in view
    no_on_since(r, stopped)
    r.cam.card = False


@pytest.mark.parametrize('dash', DASHBOARDS)
@pytest.mark.parametrize('value', [False, 'false', 0, 'off'])
def test_off_from_the_dashboard_closes_an_open_nozzle(rig, dash, value):
    """Including the spellings that used to mean ON (bool('false'))."""
    r = rig(dash=dash)
    hold_a_weed_until_spraying(r)
    r.command('set_detection_enable', value=value)
    stays_off_with_the_weed_still_there(r)


@pytest.mark.parametrize('value', ['banana', None, 2])
def test_an_unclear_value_changes_nothing_either_way(rig, value):
    r = rig(dash='standalone')
    r.command('set_detection_enable', value=value)       # spraying: stays spraying
    assert r.show_card().fired

    r.command('set_detection_enable', value=False)
    r.wait_for(lambda: r.owl.relay_controller.mode == 'off', 'spraying off')
    asked = time.monotonic()
    r.command('set_detection_enable', value=value)       # off: stays off
    r.show_card(settle=0.6)
    no_on_since(r, asked)


@pytest.mark.parametrize('controller', ['ute', 'advanced'])
def test_switch_off_closes_an_open_nozzle(rig, controller):
    r = rig(controller=controller)
    hold_a_weed_until_spraying(r)
    r.switch(detection=False)
    stays_off_with_the_weed_still_there(r)


@pytest.mark.allow_errors
def test_a_fault_under_on_error_off_closes_an_open_nozzle_and_stays_off(rig):
    r = rig(dash='standalone', overrides=[('System', 'on_error', 'off')])
    hold_a_weed_until_spraying(r)
    r.owl._fault('control_lost', 'test fault', detection_possible=True)
    stays_off_with_the_weed_still_there(r)

    r.owl._clear_fault('control_lost')            # clearing it does not resume
    asked = time.monotonic()
    r.show_card(settle=0.6)
    no_on_since(r, asked)


@pytest.mark.allow_errors
def test_a_fault_under_on_error_on_keeps_spot_spraying(rig):
    r = rig(dash='standalone', overrides=[('System', 'on_error', 'on')])
    r.owl._fault('control_lost', 'test fault', detection_possible=True)
    r.owl._update_state_once()
    assert r.show_card().fired


def test_spraying_off_before_a_delayed_spray_starts_cancels_it(rig):
    """The weed was seen, the nozzle is 1 s of travel away, and the operator
    switches off in between: that spray must never happen."""
    r = rig(dash='standalone', overrides=[('System', 'delay', '1.0')])
    r.cam.card = True
    time.sleep(0.3)
    r.cam.card = False
    r.command('set_detection_enable', value=False)
    asked = time.monotonic()
    time.sleep(1.6)
    no_on_since(r, asked)


@pytest.mark.parametrize('state', ['spot', 'blanket'])
def test_shutdown_leaves_every_relay_off(rig, state):
    r = rig(dash='standalone')
    if state == 'blanket':
        r.command('set_detection_mode', value=2)
        r.wait_all_on()
    else:
        hold_a_weed_until_spraying(r)
    try:
        r.owl.stop()
    except SystemExit:
        pass
    assert not r.relays_on(), f'relays left on after stop(): {sorted(r.relays_on())}'


# --------------------------------------------------------------------------
# Hold to spray and the nozzle test: on, and off again by themselves
# --------------------------------------------------------------------------

@pytest.mark.parametrize('dash', DASHBOARDS)
def test_hold_to_spray_lets_go_by_itself_when_the_dashboard_goes_quiet(rig, dash):
    r = rig(dash=dash, spray=False)
    r.command('hold_spray', value=1500)
    r.wait_all_on(timeout=PROMPT_S)
    started = time.monotonic()
    r.wait_all_off(timeout=2.2)                      # nothing renews it
    held = time.monotonic() - started
    assert 1.3 <= held <= 2.0, f'held for {held:.2f} s on a 1.5 s lease'


def test_hold_to_spray_stays_on_while_renewed_and_stops_on_release(rig):
    r = rig(dash='standalone', spray=False)
    for _ in range(4):
        r.command('hold_spray', value=1500)
        time.sleep(0.6)
        assert r.relays_on() == set(ALL)
    r.command('hold_spray', value=0)
    r.wait_all_off(timeout=PROMPT_S)


@pytest.mark.allow_errors
@pytest.mark.parametrize('what', ['hold', 'nozzle test'])
def test_a_fault_under_on_error_off_ends_a_hold_or_nozzle_test(rig, what):
    r = rig(dash='standalone', spray=False, overrides=[('System', 'on_error', 'off')])
    if what == 'hold':
        r.command('hold_spray', value=1500)
        r.wait_all_on(timeout=PROMPT_S)
    else:
        r.command('test_nozzles', value={'relays': [1, 2], 'duration_ms': 8000})
        r.wait_for(lambda: r.relays_on() == {1, 2}, 'the test relays on', timeout=PROMPT_S)
    r.owl._fault('control_lost', 'test fault', detection_possible=True)
    r.owl._update_state_once()
    r.wait_all_off(timeout=PROMPT_S)


def test_hold_to_spray_returns_to_spot_not_off(rig):
    r = rig(dash='standalone')
    r.command('hold_spray', value=300)
    r.wait_all_on(timeout=PROMPT_S)
    r.wait_all_off(timeout=1.0)
    assert r.show_card(x=0.62).fired == [2]


@pytest.mark.parametrize('relays, ms', [([1], 300), ([0, 3], 500)])
def test_nozzle_test_switches_only_the_named_relays_for_the_time_asked(rig, relays, ms):
    r = rig(dash='standalone', spray=False)
    r.command('test_nozzles', value={'relays': relays, 'duration_ms': ms})
    r.wait_for(lambda: r.relays_on() == set(relays), f'relays {relays} on', timeout=PROMPT_S)
    started = time.monotonic()
    r.wait_all_off(timeout=ms / 1000 + 1.0)
    lasted = time.monotonic() - started
    assert ms / 1000 - 0.1 <= lasted <= ms / 1000 + PROMPT_S
    assert {relay for _, relay, on in r.drives if on} == set(relays)


def test_nozzle_test_is_capped_at_ten_seconds_whatever_is_asked(rig):
    r = rig(dash='standalone', spray=False)
    r.command('test_nozzles', value={'relays': [2], 'duration_ms': 3_600_000})
    r.wait_for(lambda: r.relays_on() == {2}, 'relay 2 on', timeout=PROMPT_S)
    remaining = r.owl.relay_controller._test_until - time.monotonic()
    assert 9.0 <= remaining <= 10.0


def test_an_empty_nozzle_test_stops_the_running_one(rig):
    r = rig(dash='standalone', spray=False)
    r.command('test_nozzles', value={'relays': [0, 1], 'duration_ms': 5000})
    r.wait_for(lambda: r.relays_on() == {0, 1}, 'relays on', timeout=PROMPT_S)
    r.command('test_nozzles', value={'relays': []})
    r.wait_all_off(timeout=PROMPT_S)


# --------------------------------------------------------------------------
# Timing sent by the networked controller
# --------------------------------------------------------------------------

def controller_timing(r, duration, delay, **extra):
    r.command('set_actuation_params', actuation_duration=duration, delay=delay,
              source='gps', **extra)


def test_controller_timing_is_what_the_relay_does(rig):
    r = rig(dash='networked')
    controller_timing(r, 0.4, 0.3, speed_kmh=8.0)
    assert r.owl.actuation_source == 'controller'
    showing = r.show_card(seconds=0.2)
    assert 0.26 <= showing.on <= 0.3 + PROMPT_S
    assert 0.35 <= showing.off - 0.3 <= 0.4 + PROMPT_S


def test_controller_gone_quiet_puts_the_configured_timing_back(rig):
    """The controller broadcasts once a second. Three seconds of silence
    (cab unit off, link down) and the OWL is back on its own settings."""
    r = rig(dash='networked')
    controller_timing(r, 0.4, 0.6)
    assert r.show_card(seconds=0.2).on >= 0.5

    r.wait_for(lambda: r.owl.actuation_source == 'config', 'the configured timing', timeout=6.0)
    showing = r.show_card(seconds=0.2)
    assert showing.on < PROMPT_S
    assert showing.off < CONFIG_DURATION_S + PROMPT_S


def test_controller_timing_is_clamped_before_it_reaches_a_relay(rig):
    r = rig(dash='networked')
    controller_timing(r, 600, -5)
    assert (r.owl.actuation_duration, r.owl.delay) == (5.0, 0.0)


def test_this_units_own_gps_beats_the_controller_and_parked_means_at_once(rig):
    """Networked OWL with its own receiver, parked on a wandering fix, while
    the controller keeps sending a 0.5 s travel delay."""
    r = rig(dash='networked', gps=True, speed_kmh=0.7)
    r.wait_for_speed(0.7)
    for _ in range(3):
        controller_timing(r, 0.05, 0.5, speed_kmh=9.0)
        showing = r.show_card(seconds=0.3)
        assert showing.on < PROMPT_S, f'relay waited {showing.on:.2f} s on the controller delay'
    assert r.owl.actuation_source == 'config'


def test_controller_geometry_is_used_by_this_units_own_gps_timing(rig):
    """The controller's nozzle distance applies even when the OWL times
    itself: 60 cm at 6 km/h is 0.36 s."""
    r = rig(dash='networked', gps=True, speed_kmh=6.0)
    r.wait_for_moving(6.0)
    controller_timing(r, 0.05, 0.1, offset_cm=60, actuation_length_cm=10, offset_configured=True)
    r.wait_for(lambda: r.owl.delay == pytest.approx(0.36, abs=0.01), 'the 60 cm travel time')
    showing = r.show_card(seconds=0.3)
    assert 0.32 <= showing.on <= 0.36 + PROMPT_S
