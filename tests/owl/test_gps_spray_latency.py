"""
Weed in view -> relay switched, measured end to end with the OWL's own GPS.

Field bug, 2026-11-16: a unit standing still with its GPS antenna just
outside read about 0.5 km/h (a poor fix never reads zero). Speed-adaptive
timing took that as travel and held every spray back by the camera-to-nozzle
travel time at that speed: 30 cm / 0.5 km/h = 2.16 s. A green card held in
front of the camera fired the relay seconds late, and the relay stayed on
seconds after the card was gone. Unplugging the antenna cured it.

Nothing caught it because no test watched a relay while a GPS fix was
present. These do, with nothing between the receiver and the pin faked:

    fake serial port -> Owl._serial_gps_reader -> GPSState
        -> update_state thread -> Owl._update_spray_timing
        -> hoot() on real frames -> RelayController -> RelayControl.relay_on

Only the serial port and the camera are stand-ins. Every assertion is on the
time between the card appearing in a frame and the relay being driven.
"""

import random
import threading
import time

import pytest

from harness import CONFIG_DURATION_S, PROMPT_S

# The rig (real Owl + stand-in GPS receiver and camera + a log of every
# relay drive) is tests/owl/harness.Rig, built by the `rig` fixture in
# tests/owl/conftest.py.


# --------------------------------------------------------------------------
# Standing still
# --------------------------------------------------------------------------

def test_no_gps_sprays_at_once(rig):
    """The reference: no receiver at all."""
    r = rig()
    on, off = r.show_card()
    assert on is not None and on < PROMPT_S
    assert off < CONFIG_DURATION_S + PROMPT_S


@pytest.mark.parametrize('parked_kmh', [0.0, 0.3, 0.5, 0.9, 1.4])
def test_parked_with_a_gps_fix_sprays_at_once(rig, parked_kmh):
    """The field bug. A parked receiver reports up to a km/h or so; the relay
    must switch as promptly as it does with no GPS, and let go as promptly."""
    r = rig(gps=True, speed_kmh=parked_kmh)
    r.wait_for_speed(parked_kmh)

    for showing in range(3):
        on, off = r.show_card()
        assert on is not None, f'showing {showing}: card in view, no relay switched'
        assert on < PROMPT_S, (
            f'showing {showing}: relay switched {on:.2f} s after the card appeared '
            f'(GPS reads {parked_kmh} km/h, delay={r.owl.delay:.2f} s)')
        assert off < CONFIG_DURATION_S + PROMPT_S, (
            f'showing {showing}: relay stayed on {off:.2f} s after the card was gone')

    assert r.owl.delay == 0.0
    assert r.owl.actuation_duration == CONFIG_DURATION_S
    assert r.owl.actuation_source == 'config'


def test_wandering_fix_never_delays_a_spray(rig):
    """What the antenna just outside the shed did: a different small speed on
    every sample, for long enough that the average crosses 0.5 km/h both ways."""
    rng = random.Random(20261116)
    r = rig(gps=True, speed_kmh=lambda: rng.uniform(0.2, 1.5))
    r.wait_for(lambda: r.owl._device_timing, 'the GPS fix')

    worst_delay = 0.0
    for showing in range(6):
        on, off = r.show_card(seconds=0.4)
        worst_delay = max(worst_delay, r.owl.delay)
        assert on is not None and on < PROMPT_S, (
            f'showing {showing}: relay switched {on} s after the card appeared')
        assert off < CONFIG_DURATION_S + PROMPT_S
        time.sleep(0.6)
    assert worst_delay == 0.0


def test_speed_spikes_while_parked_never_delay_a_spray(rig):
    """A poor fix throws readings of several km/h for a moment, then 0 again.
    One second of 4 km/h in every three must not start travel timing."""
    start = time.monotonic()
    r = rig(gps=True, speed_kmh=lambda: 4.0 if (time.monotonic() - start) % 3.0 < 1.0 else 0.0)
    r.wait_for(lambda: r.owl._device_timing, 'the GPS fix')

    delays = []
    watching = True

    def watch():
        while watching:
            delays.append(r.owl.delay)
            time.sleep(0.02)
    threading.Thread(target=watch, daemon=True).start()

    try:
        for showing in range(6):
            on, off = r.show_card(seconds=0.4)
            assert on is not None and on < PROMPT_S, (
                f'showing {showing}: relay switched {on} s after the card appeared')
            assert off < CONFIG_DURATION_S + PROMPT_S
            time.sleep(0.5)   # showings drift across the 3 s spike cycle
    finally:
        watching = False
    assert max(delays) == 0.0, 'a spike started travel timing between showings'
    assert r.owl.actuation_source == 'config'


# --------------------------------------------------------------------------
# Moving: the timing the fix must not break
# --------------------------------------------------------------------------

@pytest.mark.parametrize('kmh, travel_s', [(10.0, 0.108), (3.0, 0.36)])
def test_moving_delays_the_spray_by_the_travel_time(rig, kmh, travel_s):
    """Nozzle 30 cm behind the camera: the relay switches when the weed gets
    there, neither at once nor later."""
    r = rig(gps=True, speed_kmh=kmh)
    r.wait_for_moving(kmh)
    assert r.owl.delay == pytest.approx(travel_s, abs=0.01)

    on, _ = r.show_card()
    assert on is not None
    assert travel_s - 0.04 <= on <= travel_s + PROMPT_S, (
        f'relay switched {on:.3f} s after the card at {kmh} km/h, '
        f'expected about {travel_s} s')


def test_dropped_readings_at_speed_keep_travel_timing(rig):
    """The opposite fault: at 10 km/h a reading of 0 now and then must not
    hand timing back to the fixed values (delay 0 sprays 30 cm early)."""
    start = time.monotonic()
    r = rig(gps=True, speed_kmh=10.0)
    r.wait_for_moving(10.0)
    # 0.4 s of zeros in every 3 s: at most one of the once-a-second samples
    r.gps.speed_kmh = lambda: 0.0 if (time.monotonic() - start) % 3.0 < 0.4 else 10.0

    deadline = time.monotonic() + 7.0
    while time.monotonic() < deadline:
        assert r.owl.actuation_source == 'device-gps'
        assert r.owl.delay > 0.05
        time.sleep(0.05)
    on, _ = r.show_card()
    assert on is not None and on >= 0.06


def test_stopping_goes_back_to_spraying_at_once(rig):
    """Driving, then parked on a fix that still reads 0.6 km/h."""
    r = rig(gps=True, speed_kmh=8.0)
    r.wait_for_moving(8.0)
    assert r.owl.delay > 0.1

    r.gps.speed_kmh = 0.6
    r.wait_for_speed(0.6)
    on, off = r.show_card()
    assert on is not None and on < PROMPT_S
    assert off < CONFIG_DURATION_S + PROMPT_S
    assert (r.owl.actuation_duration, r.owl.delay) == (CONFIG_DURATION_S, 0.0)


def test_antenna_unplugged_while_moving_restores_config_timing(rig):
    r = rig(gps=True, speed_kmh=6.0)
    r.wait_for_moving(6.0)
    assert r.owl.delay == pytest.approx(0.18, abs=0.01)

    r.gps.fix = False
    r.wait_for(lambda: not r.owl._device_timing, 'the fix to be dropped')
    on, _ = r.show_card()
    assert on is not None and on < PROMPT_S
    assert r.owl.actuation_source == 'config'
    assert (r.owl.actuation_duration, r.owl.delay) == (CONFIG_DURATION_S, 0.0)


def test_configured_delay_is_used_when_parked(rig):
    """Standing still uses the timing the farmer set, not a hard-coded zero."""
    r = rig(gps=True, speed_kmh=0.7, overrides=[('System', 'delay', '0.3'),
                                      ('System', 'actuation_duration', '0.2')])
    r.wait_for_speed(0.7)
    on, _ = r.show_card()
    assert on is not None
    assert 0.26 <= on <= 0.3 + PROMPT_S


def test_live_timing_edit_survives_while_parked_on_a_fix(rig):
    """With a fix held, the timing is re-applied every second. A duration
    set from the app while parked must not be put back to the boot value."""
    r = rig(gps=True, speed_kmh=0.6)
    r.wait_for_speed(0.6)

    # what OWLMQTTPublisher's config handler does for [System] actuation_duration
    r.owl.actuation_duration = 0.4
    r.owl._config_actuation = (0.4, r.owl.delay)
    time.sleep(2.5)
    assert r.owl.actuation_duration == 0.4

    on, off = r.show_card()
    assert on is not None and on < PROMPT_S
    assert 0.3 <= off <= 0.4 + PROMPT_S


def test_min_speed_is_configurable(rig):
    """A rig that really does creep at 1.2 km/h can lower the threshold."""
    r = rig(gps=True, speed_kmh=1.2, overrides=[('Actuation', 'min_speed_kmh', '1.0')])
    r.wait_for_moving(1.2)
    assert r.owl.delay == pytest.approx(0.9, abs=0.02)


# --------------------------------------------------------------------------
# Nozzle distance behind the camera ([Actuation] offset_cm)
# --------------------------------------------------------------------------

def test_default_nozzle_distance_is_reported_as_never_set(rig):
    """30 cm is a default nobody entered. The dashboards warn on this flag."""
    r = rig(gps=True, speed_kmh=0.0)
    stats = r.owl.get_system_stats()
    assert stats['offset_configured'] is False
    assert stats['offset_cm'] == 30.0
    assert stats['actuation_length_cm'] == 10.0


def test_nozzle_distance_from_the_ini_times_the_relay(rig):
    """60 cm at 6 km/h = 0.36 s between the card and the relay."""
    r = rig(gps=True, speed_kmh=6.0, overrides=[('Actuation', 'offset_cm', '60')])
    assert r.owl.get_system_stats()['offset_configured'] is True
    r.wait_for_moving(6.0)
    on, _ = r.show_card()
    assert on is not None
    assert 0.32 <= on <= 0.36 + PROMPT_S


def test_nozzle_distance_set_from_the_dashboard_times_the_relay_at_once(rig, mqtt_publisher):
    """The dashboard's command through the OWL's real handler: no restart,
    and the next spray waits the new distance of travel."""
    r = rig(gps=True, speed_kmh=6.0)
    r.wait_for_moving(6.0)
    on, _ = r.show_card()
    assert 0.14 <= on <= 0.18 + PROMPT_S                 # 30 cm default

    mqtt_publisher.set_owl_instance(r.owl)
    mqtt_publisher._handle_command({'action': 'set_config_section', 'section': 'Actuation',
                                    'params': {'offset_cm': '90', 'actuation_length_cm': '20'}})
    assert r.owl.get_system_stats()['offset_configured'] is True
    r.wait_for(lambda: r.owl.delay == pytest.approx(0.54, abs=0.01), 'the new travel time')
    assert r.owl.actuation_duration == pytest.approx(0.12, abs=0.005)   # 20 cm at 6 km/h
    assert r.owl.config.get('Actuation', 'offset_cm') == '90.0'

    on, _ = r.show_card()
    assert on is not None
    assert 0.50 <= on <= 0.54 + PROMPT_S


@pytest.mark.parametrize('sent, applied', [('-5', 0.0), ('5000', 200.0), ('abc', 30.0), ('nan', 30.0)])
def test_nozzle_distance_out_of_range_is_clamped_or_refused(rig, mqtt_publisher, sent, applied):
    r = rig(gps=True, speed_kmh=0.0)
    mqtt_publisher.set_owl_instance(r.owl)
    mqtt_publisher._handle_command({'action': 'set_config_section', 'section': 'Actuation',
                                    'params': {'offset_cm': sent}})
    assert r.owl._actuation_calc.offset_cm == applied
    assert r.owl.offset_configured is (applied != 30.0)


def test_unit_with_its_own_gps_reports_travel_timing_possible(rig):
    """The dashboards ask for the nozzle distance up front on these units."""
    assert rig(gps=True, speed_kmh=0.0).owl.get_system_stats()['travel_timing'] is True


def test_unit_without_gps_or_controller_does_not(rig):
    assert rig().owl.get_system_stats()['travel_timing'] is False


def test_controller_timing_counts_as_travel_timing_while_it_is_live(rig, mqtt_publisher):
    r = rig()
    mqtt_publisher.set_owl_instance(r.owl)
    mqtt_publisher._handle_command({'action': 'set_actuation_params', 'actuation_duration': 0.05,
                                    'delay': 0.1, 'source': 'gps', 'offset_cm': 30,
                                    'offset_configured': False})
    stats = r.owl.get_system_stats()
    assert stats['travel_timing'] is True
    assert stats['offset_configured'] is False
    r.owl._controller_timing_at -= r.owl._CONTROLLER_TIMING_TIMEOUT_S + 1    # controller went quiet
    assert r.owl.get_system_stats()['travel_timing'] is False
