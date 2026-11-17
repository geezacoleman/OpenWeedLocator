"""
MQTT commands, judged by what the nozzles do.

tests/mqtt used to check these handlers against a MagicMock Owl: the handler
set an attribute, the test read the attribute back. The 2026-11-16 audit
removed the line that turns the nozzles off before a reboot, and the line
that keeps controller timing alive between broadcasts, and that suite stayed
green. Here each command goes in through the OWL's real handler
(rig.command, or dash._on_message for the networked controller's topic) and
the assertion is on RelayControl.relay_on / relay_off of a running Owl.

tests/owl/test_relay_matrix.py already covers spot/off/blanket per install,
off in every spelling, the hold lease, nozzle-test timing and cap, and
controller timing. This file adds what was left:

    live config edit      [System] actuation_duration / delay, band, geometry
    power commands        restart_service / shutdown / reboot close the nozzles
    bad input             set_detection_mode, missing values, nozzle-test relays, hold lease
    networked controller  timing between broadcasts, link stamp to blanket
"""

import configparser
import json
import subprocess
import time

import pytest

from harness import CONFIG_DURATION_S, PROMPT_S

ALL = {0, 1, 2, 3}            # relay_num = 4 in GENERAL_CONFIG.ini


def switched_on_since(rig, moment):
    return [(round(at - moment, 3), relay) for at, relay, on in rig.drives if on and at > moment]


def no_on_since(rig, moment):
    late = switched_on_since(rig, moment)
    assert not late, f'relays switched on after they should have stayed off: {late}'


def edit(rig, section, **params):
    rig.command('set_config_section', section=section, params=params)


# --------------------------------------------------------------------------
# Live config edits: set_config_section / save_geometry
# --------------------------------------------------------------------------

def test_a_live_timing_edit_is_what_the_next_spray_does(rig):
    """Values arrive as strings from the config editor. The OWL's delay must
    take 0.4 (a delay held as an int would silently keep 0)."""
    r = rig(dash='standalone')
    edit(r, 'System', actuation_duration='0.5', delay='0.4')

    showing = r.show_card(seconds=0.2)
    assert 0.36 <= showing.on <= 0.4 + PROMPT_S, f'relay waited {showing.on:.2f} s on a 0.4 s delay'
    assert 0.45 <= showing.off - 0.4 <= 0.5 + PROMPT_S, \
        f'relay let go {showing.off - 0.4:.2f} s after the delayed weed on a 0.5 s duration'
    assert r.owl.actuation_source == 'config'


def test_a_live_timing_edit_is_clamped_before_it_reaches_a_relay(rig):
    """A misplaced decimal point used to spray for minutes."""
    r = rig(dash='standalone')
    edit(r, 'System', actuation_duration='600', delay='-3')
    assert (r.owl.actuation_duration, r.owl.delay) == (5.0, 0.0)
    assert r.owl._config_actuation == (5.0, 0.0)
    # what a later save writes is the clamped value too
    assert (r.owl.config.getfloat('System', 'actuation_duration'),
            r.owl.config.getfloat('System', 'delay')) == (5.0, 0.0)

    showing = r.show_card(seconds=0.2, settle=0.6)       # do not wait out the 5 s
    assert showing.on < PROMPT_S, 'a negative delay must spray at once'
    assert r.relays_on(), 'the relay let go early: the 5 s cap is not what ran'
    r.command('set_detection_enable', value=False)
    r.wait_all_off(timeout=PROMPT_S)


def test_a_duration_edit_while_gps_times_the_sprays_survives_the_rig_stopping(rig):
    """Moving on a fix, owl.delay is the travel time at that speed. Editing
    the duration then used to save that travel time as the configured delay,
    and it came back as a permanent delay once the rig stopped."""
    r = rig(dash='standalone', gps=True, speed_kmh=6.0,
            overrides=[('Actuation', 'offset_cm', '90')])
    r.wait_for_moving(6.0)
    assert r.owl.delay == pytest.approx(0.54, abs=0.01)          # 90 cm at 6 km/h

    edit(r, 'System', actuation_duration='0.4')
    r.gps.speed_kmh = 0.0
    r.wait_for_speed(0.0)
    r.wait_for(lambda: r.owl.actuation_source == 'config', 'the configured timing')
    assert (r.owl.actuation_duration, r.owl.delay) == (0.4, 0.0)

    showing = r.show_card(seconds=0.2)
    assert showing.on < PROMPT_S, f'parked, the relay still waited {showing.on:.2f} s'
    assert 0.35 <= showing.off <= 0.4 + PROMPT_S, \
        f'the edited 0.4 s duration is not what ran once parked ({showing.off:.2f} s)'


def test_a_live_band_edit_changes_which_weeds_spray(rig):
    r = rig(dash='standalone')
    edit(r, 'System', actuation_top='0.5', actuation_bottom='1.0')
    assert r.show_card(y=0.25, settle=1.0).fired == [], 'a weed above the new band sprayed'
    assert r.show_card(y=0.75).fired, 'a weed inside the new band did not spray'


def test_save_geometry_moves_the_band_at_once_and_writes_only_geometry(rig, owl_home):
    """The geometry editor's Done button: applied to the running OWL and
    kept in GEOMETRY.ini (this unit's mount), nowhere else."""
    r = rig(dash='standalone')
    r.command('save_geometry', params={'actuation_top': '0.0', 'actuation_bottom': '0.5',
                                       'relay_num': '8'})
    assert r.owl.dash.io_flush()

    assert r.show_card(y=0.75, settle=1.0).fired == [], 'a weed below the saved band sprayed'
    assert r.show_card(y=0.25).fired, 'a weed inside the saved band did not spray'

    written = configparser.ConfigParser()
    # read_string: the rig makes ConfigParser.read() skip any GEOMETRY.ini
    written.read_string((owl_home / 'config' / 'GEOMETRY.ini').read_text())
    assert {s: dict(written[s]) for s in written.sections()} == {
        'System': {'actuation_top': '0.0', 'actuation_bottom': '0.5'}}
    assert sorted(p.name for p in (owl_home / 'config').iterdir()) == ['GEOMETRY.ini']


# --------------------------------------------------------------------------
# Power commands: nozzles closed before the machine goes down
# --------------------------------------------------------------------------

@pytest.mark.parametrize('action, argv_tail', [
    ('restart_service', ['systemctl', 'restart', 'owl.service']),
    ('shutdown', ['now']),
    ('reboot', [])])
def test_power_commands_close_every_nozzle_first(rig, monkeypatch, action, argv_tail):
    r = rig(dash='standalone')
    r.command('set_detection_mode', value=2)
    r.wait_all_on()

    ran = []        # (argv, spray mode the relay controller was in when it ran)
    real_run, real_popen = subprocess.run, subprocess.Popen

    def power(real):
        def call(args, *a, **kw):
            if isinstance(args, (list, tuple)) and args and args[0] == 'sudo':
                ran.append((list(args), r.owl.relay_controller.mode))
                return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
            return real(args, *a, **kw)
        return call

    monkeypatch.setattr(subprocess, 'run', power(real_run))
    monkeypatch.setattr(subprocess, 'Popen', power(real_popen))

    r.command(action)
    asked = time.monotonic()
    r.wait_all_off(timeout=PROMPT_S)
    assert r.owl.dash.io_flush()

    assert len(ran) == 1, f'{action} ran {len(ran)} system commands: {ran}'
    argv, mode_when_run = ran[0]
    assert argv[:1] == ['sudo'] and argv[len(argv) - len(argv_tail):] == argv_tail
    assert mode_when_run == 'off', f'{action} ran while the OWL was still in {mode_when_run}'

    r.show_card(settle=0.6)                  # a weed passing while it goes down
    no_on_since(r, asked)


# --------------------------------------------------------------------------
# Bad input never starts spraying, and never stops a blanket by accident
# --------------------------------------------------------------------------

BAD_MODES = [5, -1, 'x', '', None, [2], {'mode': 2}]


@pytest.mark.allow_errors      # each refusal is logged as an error
def test_an_invalid_spray_mode_changes_nothing_either_way(rig):
    r = rig(dash='standalone', spray=False)
    start = time.monotonic()
    for bad in BAD_MODES:
        r.command('set_detection_mode', value=bad)
    r.show_card(settle=0.6)
    no_on_since(r, start)

    r.command('set_detection_mode', value=2)
    r.wait_all_on()
    mark = r.mark()
    for bad in BAD_MODES:
        r.command('set_detection_mode', value=bad)
    time.sleep(0.4)
    assert r.relays_on() == ALL
    assert not [d for d in r.drives_since(mark) if not d[2]], 'a bad value dropped a relay out of blanket'


def test_a_command_with_no_value_never_starts_spraying(rig):
    """The field is missing altogether (a truncated or hand-built message).
    set_detection_enable refuses it; set_detection_mode reads it as off."""
    r = rig(dash='standalone', spray=False)
    start = time.monotonic()
    r.command('set_detection_enable')
    r.command('set_detection_mode')
    assert r.owl.relay_controller.mode == 'off'
    r.show_card(settle=0.6)
    no_on_since(r, start)

    r.command('set_detection_mode', value=2)
    r.wait_all_on()
    r.command('set_detection_enable')            # refused: the blanket stays
    time.sleep(0.3)
    assert r.relays_on() == ALL
    r.command('set_detection_mode')              # no value is off
    r.wait_all_off(timeout=PROMPT_S)


def test_a_blanket_refused_by_the_switch_is_reported_as_off(rig):
    """Ute switch off: the dashboard's blanket is refused, and the state the
    dashboard reads back says off, not the blanket it asked for."""
    r = rig(controller='ute', dash='standalone')
    r.switch(detection=False)
    r.wait_for(lambda: r.owl.relay_controller.mode == 'off', 'the switch to stop spraying')
    start = time.monotonic()
    r.command('set_detection_mode', value=2)
    time.sleep(0.4)
    no_on_since(r, start)
    state = r.owl.dash.state
    assert (state['detection_mode'], state['detection_enable']) == (1, False)


def test_nozzle_test_ignores_relays_this_owl_does_not_have(rig):
    """True is 1 to Python, '1' and 1.0 look like 1: none of them may open
    relay 1. Only the real relay number 2 in this list is switched."""
    r = rig(dash='standalone', spray=False)
    r.command('test_nozzles', value={'relays': [4, -1, True, '1', 1.0, 2], 'duration_ms': 400})
    r.wait_for(lambda: r.relays_on() == {2}, 'relay 2 on', timeout=PROMPT_S)
    r.wait_all_off(timeout=1.0)
    assert {relay for _, relay, on in r.drives if on} == {2}

    start = time.monotonic()
    r.command('test_nozzles', value=[0, 1])                     # not the documented shape
    r.command('test_nozzles', value={'relays': [7, 'all']})
    time.sleep(0.4)
    no_on_since(r, start)


@pytest.mark.parametrize('release', [None, 'x', -50])
def test_an_unreadable_hold_lease_lets_go(rig, release):
    r = rig(dash='standalone', spray=False)
    r.command('hold_spray', value=1500)
    r.wait_all_on(timeout=PROMPT_S)
    r.command('hold_spray', value=release)
    r.wait_all_off(timeout=PROMPT_S)


def test_a_huge_hold_lease_still_lets_go_after_a_second_and_a_half(rig):
    """The handler passes the lease as sent; the OWL caps it. A dashboard
    that asked for an hour and then vanished must not leave the boom on."""
    r = rig(dash='standalone', spray=False)
    r.command('hold_spray', value=3_600_000)
    r.wait_all_on(timeout=PROMPT_S)
    started = time.monotonic()
    r.wait_all_off(timeout=2.2)
    held = time.monotonic() - started
    assert 1.3 <= held <= 2.0, f'held for {held:.2f} s on a lease capped at 1.5 s'


# --------------------------------------------------------------------------
# The networked controller
# --------------------------------------------------------------------------

def controller_timing(r, duration, delay, **extra):
    r.command('set_actuation_params', actuation_duration=duration, delay=delay,
              source='gps', **extra)


def test_controller_timing_holds_between_broadcasts(rig):
    """The controller broadcasts once a second and the OWL re-checks its
    timing once a second. In between, the controller's delay must still be
    what the relay does (it used to depend on a timestamp no test read)."""
    r = rig(dash='networked')
    controller_timing(r, 0.4, 0.5, speed_kmh=8.0)
    time.sleep(1.6)                               # at least one timing check, under the 3 s timeout
    showing = r.show_card(seconds=0.2)
    assert 0.46 <= showing.on <= 0.5 + PROMPT_S, \
        f'relay switched after {showing.on:.2f} s: the 0.5 s controller delay was dropped'
    assert r.owl.actuation_source == 'controller'


@pytest.mark.xfail(strict=True, reason=(
    "utils/mqtt_manager.py:2390 _handle_set_actuation_params: a broadcast with no "
    "actuation_duration is read as 0 and clamped up to 0.01 s, so every spray becomes "
    "10 ms until the controller goes quiet. A missing field should be refused, as a "
    "non-numeric one already is."))
def test_a_broadcast_with_no_duration_does_not_cut_sprays_to_ten_milliseconds(rig):
    r = rig(dash='networked')
    r.command('set_actuation_params', delay=0.0, source='gps')
    showing = r.show_card(seconds=0.2)
    assert showing.off >= CONFIG_DURATION_S - 0.05, \
        f'relay let go {showing.off:.3f} s after the weed (configured {CONFIG_DURATION_S} s)'


@pytest.mark.allow_errors      # link_lost is logged as an error
def test_controller_messages_keep_a_blanket_alive_and_silence_ends_it(rig):
    """Networked OWL, on_error = off, blanket spraying. Every message on the
    controller's command topic counts as a sign of life; five seconds
    without one and the boom must close. Messages go in through the real
    paho callback, as the broker delivers them."""
    import owl as owl_module

    r = rig(dash='networked', spray=False, overrides=[('System', 'on_error', 'off')])
    dash = r.owl.dash
    dash.connected = True          # the broker is up; only the controller will go quiet

    class Message:
        topic = dash.topics['commands']

        def __init__(self, **command):
            self.payload = json.dumps(command).encode()

    dash._on_message(None, None, Message(action='set_detection_mode', value=2))
    r.wait_all_on()

    mark = r.mark()
    broadcasting_until = time.monotonic() + owl_module.LINK_LOST_S + 1.5
    while time.monotonic() < broadcasting_until:
        dash._on_message(None, None, Message(action='set_actuation_params', source='gps',
                                             actuation_duration=0.15, delay=0.0))
        last_heard = time.monotonic()
        time.sleep(1.0)
    assert r.relays_on() == ALL, 'the blanket stopped while the controller was still talking'
    assert not [d for d in r.drives_since(mark) if not d[2]], 'a relay dropped out during blanket'

    r.wait_all_off(timeout=owl_module.LINK_LOST_S + 2.0)
    closed = time.monotonic()
    quiet_for = closed - last_heard
    assert owl_module.LINK_LOST_S - 0.1 <= quiet_for <= owl_module.LINK_LOST_S + 1.5, \
        f'the boom closed {quiet_for:.1f} s after the last message (limit {owl_module.LINK_LOST_S} s)'

    r.show_card(settle=0.6)
    no_on_since(r, closed)


# --------------------------------------------------------------------------
# Painted LUT profiles: what was queued by the handler is what detects
# --------------------------------------------------------------------------

def test_a_lut_profile_and_the_lut_algorithm_sent_together_detect_with_that_profile(rig):
    """The weed painter's Apply sends set_lut_profile then set_algorithm.
    Both wait for the detection loop; the detector it builds must be from
    the profile just sent (a config with no profile yet has nothing else to
    build from). 'bluecard' calls blue the weed and green the background,
    so a green card stops spraying; 'starter' (greens) brings it back."""
    import numpy as np
    from utils.lut_manager import generate_starter_pixels

    r = rig(dash='standalone')
    assert r.show_card().fired, 'exhsv: the green card sprays'

    greens, soil = generate_starter_pixels()
    blue, green_red = np.arange(150, 250, 4), np.arange(20, 80, 6)
    blues = np.array([(b, g, red) for b in blue for g in green_red for red in green_red],
                     dtype=np.uint8)
    r.owl.lut_manager.save('bluecard', blues, np.concatenate([greens, soil]))

    # straight into the handler, back to back: both pending for the same frame
    r.owl.dash._handle_command({'action': 'set_lut_profile', 'name': 'bluecard'})
    r.owl.dash._handle_command({'action': 'set_algorithm', 'value': 'lut'})
    r.wait_for(lambda: r.owl.running_algorithm == 'lut', 'the LUT detector to load')
    assert r.owl.lut_profile == 'bluecard'
    assert r.owl.dash.state['algorithm_error'] is None

    switched = time.monotonic()
    assert r.show_card(settle=1.0).fired == [], 'green is background in this profile'
    no_on_since(r, switched)

    r.command('set_lut_profile', name='starter')
    r.wait_for(lambda: r.owl.lut_profile == 'starter', 'the starter profile')
    showing = r.show_card(x=0.62)
    assert showing.fired == [2]
    assert showing.on < PROMPT_S
