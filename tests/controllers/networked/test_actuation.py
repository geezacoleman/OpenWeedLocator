"""
Tests for speed-adaptive actuation: SpeedAverager, ActuationCalculator,
MQTT handler, and API endpoints.
"""
import json
import time
import threading
import pytest

# ---------------------------------------------------------------------------
# SpeedAverager tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSpeedAverager:

    def _make(self, **kwargs):
        from controller.networked.networked import SpeedAverager
        return SpeedAverager(**kwargs)

    def test_empty_returns_none(self):
        sa = self._make()
        assert sa.get_average() is None
        assert sa.get_fallback_speed() is None

    def test_single_sample(self):
        sa = self._make()
        avg = sa.add_sample(10.0)
        assert avg == 10.0
        assert sa.get_average() == 10.0

    def test_multiple_samples_average(self):
        sa = self._make()
        sa.add_sample(10.0)
        sa.add_sample(20.0)
        avg = sa.add_sample(30.0)
        assert avg == pytest.approx(20.0)

    def test_window_pruning(self):
        sa = self._make(window_seconds=0.1)
        sa.add_sample(100.0)
        time.sleep(0.15)
        # After window expires, old sample pruned
        avg = sa.get_average()
        assert avg is None

    def test_fallback_after_gps_drop(self):
        sa = self._make(window_seconds=0.1)
        sa.add_sample(12.5)
        time.sleep(0.15)
        # Window empty but fallback should still be available
        assert sa.get_average() is None
        assert sa.get_fallback_speed() == 12.5

    def test_seconds_since_update_none_initially(self):
        sa = self._make()
        assert sa.seconds_since_update() is None

    def test_seconds_since_update_after_sample(self):
        sa = self._make()
        sa.add_sample(5.0)
        ssu = sa.seconds_since_update()
        assert ssu is not None
        assert ssu < 1.0

    def test_thread_safety(self):
        """Multiple threads adding samples shouldn't crash."""
        sa = self._make()
        errors = []

        def adder(speed):
            try:
                for _ in range(100):
                    sa.add_sample(speed)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=adder, args=(i * 5.0,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert sa.get_average() is not None


# ---------------------------------------------------------------------------
# ActuationCalculator tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestActuationCalculator:

    def _make(self, **kwargs):
        from controller.networked.networked import ActuationCalculator
        kwargs.setdefault('sustain_s', 0)   # the maths, not the moving/parked gate
        return ActuationCalculator(**kwargs)

    def test_basic_math_10kmh_10cm(self):
        calc = self._make(actuation_length_cm=10, offset_cm=30)
        result = calc.compute(10.0)
        # speed_m_s = 10/3.6 = 2.778
        # duration = 0.10 / 2.778 = 0.036s
        # delay = 0.30 / 2.778 = 0.108s
        assert result['actuation_duration'] == pytest.approx(0.036, abs=0.001)
        assert result['delay'] == pytest.approx(0.108, abs=0.001)
        assert result['source'] == 'gps'
        assert result['speed_used'] == 10.0

    def test_zero_speed_fallback(self):
        calc = self._make(fallback_duration=0.2, fallback_delay=0.05)
        result = calc.compute(0.0)
        assert result['actuation_duration'] == 0.2
        assert result['delay'] == 0.05
        assert result['source'] == 'config'

    def test_none_speed_fallback(self):
        calc = self._make(fallback_duration=0.15, fallback_delay=0.0)
        result = calc.compute(None)
        assert result['actuation_duration'] == 0.15
        assert result['source'] == 'config'

    def test_below_min_speed_fallback(self):
        calc = self._make()
        result = calc.compute(0.3)
        assert result['source'] == 'config'

    def test_safety_cap_slow_speed(self):
        """Very slow speed shouldn't produce duration > MAX_DURATION."""
        calc = self._make(actuation_length_cm=50, offset_cm=100, min_speed_kmh=0.1)
        result = calc.compute(0.2)
        assert result['source'] == 'gps'
        assert result['actuation_duration'] <= 5.0
        assert result['delay'] <= 5.0

    def test_high_speed_short_duration(self):
        calc = self._make(actuation_length_cm=10, offset_cm=0)
        result = calc.compute(30.0)
        # speed_m_s = 8.333, duration = 0.10/8.333 = 0.012s
        assert result['actuation_duration'] == pytest.approx(0.012, abs=0.001)
        assert result['actuation_duration'] >= 0.01  # MIN_DURATION floor

    def test_zero_offset_zero_delay(self):
        calc = self._make(offset_cm=0)
        result = calc.compute(10.0)
        assert result['delay'] == 0.0

    def test_coverage_ok(self):
        calc = self._make(actuation_length_cm=10)
        cov = calc.check_coverage(10.0, 30.0)
        # min_gap = (10/3.6) * 0.030 * 100 = 8.33cm < 10cm
        assert cov['coverage_ok'] is True

    def test_coverage_warning(self):
        calc = self._make(actuation_length_cm=2)
        cov = calc.check_coverage(15.0, 50.0)
        # min_gap = (15/3.6) * 0.050 * 100 = 20.8cm > 2cm
        assert cov['coverage_ok'] is False
        assert cov['min_gap_cm'] > 2
        assert 'Moving too fast for the camera' in cov['message']

    def test_coverage_zero_loop_time(self):
        calc = self._make()
        cov = calc.check_coverage(10.0, 0.0)
        assert cov['coverage_ok'] is True


# ---------------------------------------------------------------------------
# MQTT set_actuation_params handler tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSetActuationParams:

    def test_updates_owl_instance(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_actuation_params',
            'actuation_duration': 0.036,
            'delay': 0.108,
            'source': 'gps'
        })
        assert mock_owl.actuation_duration == pytest.approx(0.036)
        assert mock_owl.delay == pytest.approx(0.108)

    def test_clamps_negative_duration(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_actuation_params',
            'actuation_duration': -1.0,
            'delay': 0.0,
            'source': 'test'
        })
        # Should be clamped to MIN_DURATION (0.01)
        assert mock_owl.actuation_duration >= 0.01

    def test_clamps_excessive_duration(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_actuation_params',
            'actuation_duration': 99.0,
            'delay': 0.0,
            'source': 'test'
        })
        assert mock_owl.actuation_duration <= 5.0

    def test_state_includes_actuation_source(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({
            'action': 'set_actuation_params',
            'actuation_duration': 0.05,
            'delay': 0.1,
            'source': 'gps'
        })
        # actuation_source says who owns the timing, not the controller's own
        # speed source
        assert mqtt_publisher.state['actuation_source'] == 'controller'
        assert mqtt_publisher.state['actuation_duration'] == pytest.approx(0.05)

    def test_avg_loop_time_in_state(self, mqtt_publisher, mock_owl):
        """avg_loop_time_ms should appear in state after system stats update."""
        mqtt_publisher.update_system_stats({
            'cpu_percent': 50.0,
            'cpu_temp': 55.0,
            'memory_percent': 40.0,
            'memory_used': 1.0,
            'memory_total': 4.0,
            'disk_percent': 30.0,
            'disk_used': 5.0,
            'disk_total': 32.0,
            'owl_running': True,
            'avg_loop_time_ms': 35.2,
            'actuation_duration': 0.036,
            'delay': 0.108
        })
        assert mqtt_publisher.state['avg_loop_time_ms'] == 35.2


@pytest.mark.unit
class TestStretchAndSpeedWarning:
    """Duration stretches to cover the ground between frames (capped at
    3 patches) and the farmer-facing warning names a speed to slow to."""

    def _make(self, **kwargs):
        from utils.actuation import ActuationCalculator
        kwargs.setdefault('sustain_s', 0)
        return ActuationCalculator(**kwargs)

    def test_no_loop_time_behaves_as_before(self):
        result = self._make(actuation_length_cm=10, offset_cm=30).compute(10.0)
        assert result['actuation_duration'] == pytest.approx(0.036, abs=0.001)
        assert result['stretched'] is False
        assert result['coverage_ok'] is True

    def test_stretches_to_cover_the_frame_gap(self):
        # 10 km/h, 50 ms loop: 13.9 cm between frames > 10 cm patch
        result = self._make(actuation_length_cm=10, offset_cm=30).compute(10.0, 50.0)
        assert result['stretched'] is True
        assert result['coverage_ok'] is True
        assert result['actuation_duration'] == pytest.approx(0.05, abs=0.001)

    def test_stretch_capped_at_three_patches(self):
        # 20 km/h, 100 ms loop: 55.6 cm gap, capped at 30 cm
        result = self._make(actuation_length_cm=10, offset_cm=30).compute(20.0, 100.0)
        assert result['coverage_ok'] is False
        assert result['actuation_duration'] == pytest.approx(0.054, abs=0.001)

    def test_warning_for_frames_too_far_apart(self):
        from utils.actuation import speed_warning
        message = speed_warning(20.0, 10, 30, loop_time_ms=100.0)
        assert message == 'Moving too fast for the camera. Slow to 11 km/h'

    def test_warning_for_late_sprays(self):
        from utils.actuation import speed_warning
        # 10 km/h, 150 ms lag, 30 cm offset: 42 ms late, about 12 cm
        message = speed_warning(10.0, 10, 30, lag_s=0.15, late_s=0.042)
        assert message == 'Sprays landing about 12 cm late. Slow to 7 km/h'

    def test_late_sprays_with_no_offset_suggest_moving_nozzles(self):
        from utils.actuation import speed_warning
        message = speed_warning(10.0, 10, 0, lag_s=0.1, late_s=0.1)
        assert 'Move the nozzles further back' in message

    @pytest.mark.parametrize('speed', [None, 0.0, 0.3])
    def test_no_warning_without_speed(self, speed):
        from utils.actuation import speed_warning
        assert speed_warning(speed, 10, 30, loop_time_ms=500.0, lag_s=1, late_s=1) == ''

    def test_small_lateness_is_not_a_warning(self):
        from utils.actuation import speed_warning
        assert speed_warning(10.0, 10, 30, lag_s=0.12, late_s=0.01) == ''


@pytest.mark.unit
class TestStationary:
    """A parked GPS receiver never reads zero: on a poor fix it wanders around
    0.5-1.5 km/h. Timing from that speed held every spray back by up to
    30 cm / 0.5 km/h = 2.16 s (field bug, 2026-11-16). Below min_speed_kmh the
    configured timing applies. The end-to-end version of this, on a real Owl
    and relay, is tests/owl/test_gps_spray_latency.py."""

    def _make(self, **kwargs):
        from utils.actuation import ActuationCalculator
        kwargs.setdefault('fallback_duration', 0.15)
        kwargs.setdefault('fallback_delay', 0.0)
        kwargs.setdefault('sustain_s', 0)
        return ActuationCalculator(actuation_length_cm=10, offset_cm=30, **kwargs)

    @pytest.mark.parametrize('noise_kmh', [0.0, 0.1, 0.45, 0.5, 0.6, 0.9, 1.0, 1.5, 1.9])
    def test_receiver_noise_gets_configured_timing(self, noise_kmh):
        result = self._make().compute(noise_kmh)
        assert result['source'] == 'config'
        assert result['delay'] == 0.0
        assert result['actuation_duration'] == 0.15

    def test_configured_delay_is_what_comes_back(self):
        result = self._make(fallback_duration=0.3, fallback_delay=0.05).compute(0.8)
        assert (result['actuation_duration'], result['delay']) == (0.3, 0.05)

    def test_default_minimum_is_above_parked_receiver_noise(self):
        from utils.actuation import ActuationCalculator
        assert ActuationCalculator.MIN_SPEED >= 2.0

    def test_delay_never_exceeds_travel_time_at_the_minimum_speed(self):
        """The property the 0.5 km/h threshold broke: across every speed,
        fresh calculator or not, the delay is bounded by offset / (lowest
        speed that still counts as moving)."""
        from utils.actuation import ActuationCalculator
        calc = self._make()
        slowest = calc.min_speed_kmh * ActuationCalculator.RELEASE_RATIO
        bound = 0.30 / (slowest / 3.6)
        assert bound < 0.75
        sweep = [i / 20 for i in range(0, 601)]            # 0 to 30 km/h up...
        for speed in sweep + sweep[::-1]:                   # ...and back down
            assert calc.compute(speed)['delay'] <= bound + 1e-6, f'at {speed} km/h'
            assert self._make().compute(speed)['delay'] <= bound + 1e-6, f'fresh, at {speed} km/h'

    def test_speed_adaptive_from_the_minimum_up(self):
        result = self._make().compute(2.0)
        assert result['source'] == 'gps'
        assert result['delay'] == pytest.approx(0.54, abs=0.001)

    def test_hysteresis_holds_adaptive_timing_just_below_the_minimum(self):
        """Slowing through the threshold must not flip the delay between zero
        and its largest value on every one-second update."""
        calc = self._make()
        assert calc.compute(1.8)['source'] == 'config'      # not yet moving
        assert calc.compute(2.1)['source'] == 'gps'
        assert calc.compute(1.8)['source'] == 'gps'         # still moving
        assert calc.compute(1.6)['source'] == 'gps'
        assert calc.compute(1.4)['source'] == 'config'      # stopped
        assert calc.compute(1.8)['source'] == 'config'      # noise does not restart it

    def test_jitter_around_the_minimum_does_not_flap(self):
        calc = self._make()
        sources = [calc.compute(s)['source'] for s in [1.9, 2.05, 1.95, 2.0, 1.9, 2.1, 1.85]]
        assert sources == ['config'] + ['gps'] * 6

    def test_lost_speed_resets_to_stationary(self):
        calc = self._make()
        calc.compute(10.0)
        assert calc.compute(None)['source'] == 'config'
        assert calc.compute(1.8)['source'] == 'config'

    def test_minimum_is_configurable(self):
        calc = self._make(min_speed_kmh=1.0)
        assert calc.compute(1.2)['source'] == 'gps'
        assert calc.compute(0.9)['source'] == 'gps'          # release at 0.75
        assert calc.compute(0.7)['source'] == 'config'

    def test_zero_minimum_still_never_divides_by_zero(self):
        result = self._make(min_speed_kmh=0).compute(0.0)
        assert result['source'] == 'config'

    # --- single wild readings (default gate: ENGAGE_AFTER_S of every
    # reading at or above the minimum). The clock is stepped by hand, one
    # second per update, as both callers run. ---

    @pytest.fixture
    def clock(self, monkeypatch):
        import utils.actuation as actuation
        now = [1000.0]
        monkeypatch.setattr(actuation.time, 'monotonic', lambda: now[0])
        return now

    def _run(self, calc, clock, readings, window_s=1.5):
        """Feed one reading a second through the averager and calculator,
        as owl.py and the controller do. Returns the results. 1.5 s holds two
        readings, which is what the default 2 s window holds on a real clock
        (the third is a few ms too old)."""
        from utils.actuation import SpeedAverager
        averager = SpeedAverager(window_seconds=window_s)
        results = []
        for reading in readings:
            clock[0] += 1.0
            results.append(calc.compute(averager.add_sample(reading), 0.0, sample_kmh=reading))
        return results

    def test_default_gate_needs_sustained_speed(self):
        from utils.actuation import ActuationCalculator
        assert ActuationCalculator().sustain_s == ActuationCalculator.ENGAGE_AFTER_S >= 2.0

    @pytest.mark.parametrize('spike_kmh', [2.5, 4.0, 8.0, 25.0, 120.0])
    def test_one_wild_reading_while_parked_never_delays(self, clock, spike_kmh):
        """Parked, 0 km/h, one reading jumps and comes back."""
        calc = self._make(sustain_s=None)
        results = self._run(calc, clock, [0, 0, spike_kmh, 0, 0, 0])
        assert [r['delay'] for r in results] == [0.0] * 6
        assert {r['source'] for r in results} == {'config'}

    def test_repeated_spikes_while_parked_never_delay(self, clock):
        calc = self._make(sustain_s=None)
        readings = [0.4, 4.0, 0.0, 0.7, 6.0, 5.0, 0.2, 0.0, 9.0, 0.5, 4.0, 4.0, 0.0] * 3
        results = self._run(calc, clock, readings)
        assert max(r['delay'] for r in results) == 0.0

    def test_real_departure_engages_after_the_sustain_time(self, clock):
        calc = self._make(sustain_s=None)
        results = self._run(calc, clock, [0, 0, 3.0, 5.0, 8.0, 10.0, 10.0])
        assert [r['source'] for r in results] == ['config'] * 4 + ['gps'] * 3
        assert results[-1]['delay'] == pytest.approx(0.30 / (results[-1]['speed_used'] / 3.6), abs=0.001)

    def test_one_dropped_reading_at_speed_keeps_adaptive_timing(self, clock):
        """The opposite fault: 10 km/h with a single 0 must not hand timing
        back to the fixed values (delay 0 = spraying 30 cm early)."""
        calc = self._make(sustain_s=None)
        results = self._run(calc, clock, [10, 10, 10, 10, 0, 10, 10])
        assert [r['source'] for r in results[3:]] == ['gps'] * 4
        assert all(r['delay'] > 0 for r in results[3:])

    def test_genuine_stop_releases(self, clock):
        calc = self._make(sustain_s=None)
        results = self._run(calc, clock, [10, 10, 10, 10, 0.3, 0.2, 0.4, 0.3])
        assert results[3]['source'] == 'gps'
        assert results[-1]['source'] == 'config'
        assert results[-1]['delay'] == 0.0

    def test_no_speed_warning_or_coverage_alarm_while_parked(self):
        from utils.actuation import speed_warning
        calc = self._make()
        assert speed_warning(0.9, 10, 30, loop_time_ms=500.0, lag_s=1, late_s=1) == ''
        assert calc.check_coverage(0.9, 500.0) == {
            'coverage_ok': True, 'min_gap_cm': 0.0, 'message': ''}


@pytest.mark.unit
class TestControllerTimingOnDeviceGpsOwl:

    def test_device_timing_keeps_its_values_and_adopts_geometry(self, mqtt_publisher, mock_owl):
        from utils.actuation import ActuationCalculator
        mock_owl._device_timing = True
        mock_owl._actuation_calc = ActuationCalculator()
        mock_owl.actuation_duration = 0.2
        mqtt_publisher._handle_command({
            'action': 'set_actuation_params', 'actuation_duration': 0.05, 'delay': 0.1,
            'source': 'gps', 'actuation_length_cm': 25, 'offset_cm': 40, 'speed_kmh': 9.5,
        })
        assert mock_owl.actuation_duration == 0.2
        assert mock_owl._actuation_calc.actuation_length_cm == 25
        assert mock_owl._actuation_calc.offset_cm == 40
        assert mock_owl._controller_speed_kmh == 9.5

    def test_controller_timing_applies_and_remembers_config(self, mqtt_publisher, mock_owl):
        mock_owl._device_timing = False
        mock_owl.actuation_source = 'config'
        mock_owl.actuation_duration = 0.15
        mock_owl.delay = 0.0
        before = time.monotonic()
        mqtt_publisher._handle_command({
            'action': 'set_actuation_params', 'actuation_duration': 0.05, 'delay': 0.1,
            'source': 'gps',
        })
        assert mock_owl._config_actuation == (0.15, 0.0)
        assert mock_owl.actuation_duration == pytest.approx(0.05)
        assert mock_owl.actuation_source == 'controller'
        assert mock_owl._controller_timing_at >= before


class TestControllerBroadcast:
    """CentralController._actuation_broadcast_once on a real controller:
    controller GPS speed in, the set_actuation_params message each OWL gets
    out. (Until 3.27.2 this was checked by reading the source text.)"""

    @pytest.fixture
    def ctrl(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        import utils.actuation as actuation
        from controller.networked.networked import CentralController

        clock = [5000.0]
        monkeypatch.setattr(actuation.time, 'monotonic', lambda: clock[0])
        ctrl = CentralController(config_file=str(tmp_path / 'missing.ini'))
        ctrl.mqtt_connected = True
        ctrl.mqtt_client = MagicMock()
        ctrl.owls_state = {'owl-1': {'connected': True, 'avg_loop_time_ms': 30.0},
                           'owl-2': {'connected': False}}
        ctrl.gps_manager = MagicMock()
        ctrl.clock = clock
        return ctrl

    def _pass(self, ctrl, speed_kmh, fix_valid=True):
        """One second later, one broadcast pass. Returns what owl-1 was sent."""
        import json
        ctrl.clock[0] += 1.0
        ctrl.gps_manager.get_state.return_value = {
            'fix': {'speed_kmh': speed_kmh, 'fix_valid': fix_valid,
                    'latitude': -33.8, 'longitude': 151.0},
            'connection': {}}
        ctrl.mqtt_client.publish.reset_mock()
        ctrl._actuation_broadcast_once()
        sent = [json.loads(c.args[1]) for c in ctrl.mqtt_client.publish.call_args_list
                if c.args[0] == 'owl/owl-1/commands']
        assert len(sent) == 1, 'exactly one timing message per connected OWL per pass'
        assert not [c for c in ctrl.mqtt_client.publish.call_args_list
                    if c.args[0] == 'owl/owl-2/commands'], 'a disconnected OWL gets nothing'
        return sent[0]

    def test_parked_wandering_fix_broadcasts_configured_timing(self, ctrl):
        for speed in [0.5, 0.9, 0.6, 1.4, 0.5, 0.8, 1.1, 0.5]:
            sent = self._pass(ctrl, speed)
            assert sent['action'] == 'set_actuation_params'
            assert (sent['actuation_duration'], sent['delay']) == (0.15, 0.0), f'at {speed} km/h'
            assert sent['source'] == 'config'

    def test_spike_while_parked_broadcasts_configured_timing(self, ctrl):
        for speed in [0.0, 0.0, 4.0, 0.0, 0.0, 6.0, 5.0, 0.0]:
            assert self._pass(ctrl, speed)['delay'] == 0.0, f'at {speed} km/h'

    def test_driving_broadcasts_travel_time_geometry_and_speed(self, ctrl):
        ctrl.actuation_calculator.offset_cm = 40.0
        ctrl.actuation_calculator.actuation_length_cm = 20.0
        for _ in range(4):
            sent = self._pass(ctrl, 10.0)
        assert sent['source'] == 'gps'
        assert sent['delay'] == pytest.approx(0.144, abs=0.001)             # 40 cm at 10 km/h
        assert sent['actuation_duration'] == pytest.approx(0.072, abs=0.001)  # 20 cm at 10 km/h
        assert (sent['offset_cm'], sent['actuation_length_cm'], sent['speed_kmh']) == (40.0, 20.0, 10.0)

    def test_fix_lost_while_driving_reverts_after_the_stale_window(self, ctrl):
        from utils.actuation import GPS_STALE_AFTER_S
        for _ in range(4):
            self._pass(ctrl, 10.0)
        sent = self._pass(ctrl, None, fix_valid=False)
        assert sent['delay'] == pytest.approx(0.108, abs=0.001), 'last good speed, briefly'
        ctrl.clock[0] += GPS_STALE_AFTER_S + 1
        sent = self._pass(ctrl, None, fix_valid=False)
        assert (sent['actuation_duration'], sent['delay'], sent['source']) == (0.15, 0.0, 'config')
        assert sent['speed_kmh'] is None

    def test_speed_on_a_void_fix_never_times_the_boom(self, ctrl):
        """A source that keeps reporting 10 km/h after its fix has gone."""
        for _ in range(5):
            sent = self._pass(ctrl, 10.0, fix_valid=False)
            assert (sent['delay'], sent['source'], sent['speed_kmh']) == (0.0, 'config', None)

    def test_no_gps_manager_broadcasts_configured_timing(self, ctrl):
        ctrl.gps_manager = None
        import json
        ctrl._actuation_broadcast_once()
        sent = json.loads(ctrl.mqtt_client.publish.call_args_list[0].args[1])
        assert (sent['actuation_duration'], sent['delay']) == (0.15, 0.0)


class TestNozzleDistanceOnTheController:
    """Nozzle distance / spray length on a real CentralController: read from
    [Actuation], changed through /api/actuation/config, saved, and sent on."""

    def _controller(self, tmp_path, ini_text=''):
        from unittest.mock import MagicMock
        from controller.networked.networked import CentralController
        ini = tmp_path / 'CONTROLLER.ini'
        ini.write_text(ini_text)
        ctrl = CentralController(config_file=str(ini))
        ctrl.mqtt_connected = True
        ctrl.mqtt_client = MagicMock()
        ctrl.owls_state = {'owl-1': {'connected': True}}
        return ctrl, ini

    def _client(self, ctrl, monkeypatch):
        import controller.networked.networked as net_mod
        monkeypatch.setattr(net_mod, 'controller', ctrl)
        net_mod.app.config['TESTING'] = True
        return net_mod.app.test_client()

    def _broadcast(self, ctrl):
        import json
        ctrl.mqtt_client.publish.reset_mock()
        ctrl._actuation_broadcast_once()
        return json.loads(ctrl.mqtt_client.publish.call_args_list[0].args[1])

    def test_default_is_reported_as_never_set(self, tmp_path, monkeypatch):
        ctrl, _ = self._controller(tmp_path)
        data = self._client(ctrl, monkeypatch).get('/api/actuation').get_json()
        assert data['offset_configured'] is False
        assert data['offset_cm'] == 30.0
        assert self._broadcast(ctrl)['offset_configured'] is False

    def test_gps_enabled_tells_the_screen_whether_to_ask(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        ctrl, _ = self._controller(tmp_path)
        client = self._client(ctrl, monkeypatch)
        assert client.get('/api/actuation').get_json()['gps_enabled'] is False
        ctrl.gps_manager = MagicMock()
        ctrl.gps_manager.get_state.return_value = {'fix': {}, 'connection': {}}
        ctrl._actuation_broadcast_once()
        assert client.get('/api/actuation').get_json()['gps_enabled'] is True

    def test_ini_values_reach_the_calculator(self, tmp_path):
        ctrl, _ = self._controller(
            tmp_path, '[Actuation]\noffset_cm = 55\nactuation_length_cm = 15\n'
                      'min_speed_kmh = 3\nactuation_duration = 0.2\ndelay = 0.04\n')
        calc = ctrl.actuation_calculator
        assert (calc.offset_cm, calc.actuation_length_cm, calc.min_speed_kmh) == (55.0, 15.0, 3.0)
        assert (calc.fallback_duration, calc.fallback_delay) == (0.2, 0.04)
        assert ctrl.offset_configured is True

    def test_setting_it_applies_saves_and_reaches_the_owls(self, tmp_path, monkeypatch):
        import configparser
        ctrl, ini = self._controller(tmp_path, '# hand-written\n[MQTT]\nbroker_ip = 10.0.0.1\n')
        client = self._client(ctrl, monkeypatch)
        resp = client.post('/api/actuation/config', json={'offset_cm': 62})
        assert resp.get_json() == {'success': True, 'saved': True, 'actuation_length_cm': 10.0,
                                   'offset_cm': 62.0, 'offset_configured': True}
        assert client.get('/api/actuation').get_json()['offset_configured'] is True

        sent = self._broadcast(ctrl)
        assert (sent['offset_cm'], sent['offset_configured']) == (62.0, True)

        saved = configparser.ConfigParser()
        saved.read(ini)
        assert saved.get('Actuation', 'offset_cm') == '62'
        assert saved.get('MQTT', 'broker_ip') == '10.0.0.1'
        assert '# hand-written' in ini.read_text()

        # a restarted controller comes back with it
        from controller.networked.networked import CentralController
        again = CentralController(config_file=str(ini))
        assert again.actuation_calculator.offset_cm == 62.0
        assert again.offset_configured is True

    def test_spray_length_alone_does_not_mark_the_distance_as_set(self, tmp_path, monkeypatch):
        ctrl, _ = self._controller(tmp_path)
        resp = self._client(ctrl, monkeypatch).post('/api/actuation/config',
                                                    json={'actuation_length_cm': 18})
        assert resp.get_json()['offset_configured'] is False
        assert ctrl.actuation_calculator.actuation_length_cm == 18.0

    @pytest.mark.parametrize('body, key, expect', [
        ({'offset_cm': -10}, 'offset_cm', 0.0), ({'offset_cm': 9999}, 'offset_cm', 200.0),
        ({'actuation_length_cm': 0}, 'actuation_length_cm', 2.0),
        ({'actuation_length_cm': 999}, 'actuation_length_cm', 50.0)])
    def test_out_of_range_is_clamped(self, tmp_path, monkeypatch, body, key, expect):
        ctrl, _ = self._controller(tmp_path)
        resp = self._client(ctrl, monkeypatch).post('/api/actuation/config', json=body)
        assert resp.get_json()[key] == expect
        assert getattr(ctrl.actuation_calculator, key) == expect

    @pytest.mark.parametrize('body', [{'offset_cm': 'far'}, {'offset_cm': None},
                                      {'offset_cm': 'nan'}, {'actuation_length_cm': []}])
    def test_not_a_number_changes_nothing(self, tmp_path, monkeypatch, body):
        ctrl, ini = self._controller(tmp_path)
        resp = self._client(ctrl, monkeypatch).post('/api/actuation/config', json=body)
        assert resp.status_code == 400
        assert ctrl.actuation_calculator.offset_cm == 30.0
        assert ctrl.offset_configured is False
        assert ini.read_text() == ''


# ---------------------------------------------------------------------------
# Networked API endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestActuationAPI:

    def test_get_actuation(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        # The _actuation_state is on the real controller, but we have a mock.
        # We need to set it on the module-level controller.
        import controller.networked.networked as net_mod
        net_mod.controller._actuation_state = {
            'speed_kmh': 12.5,
            'actuation_duration': 0.036,
            'delay': 0.108,
            'source': 'gps',
            'gps_status': 'active',
            'coverage_ok': True,
            'min_gap_cm': 8.3,
            'coverage_message': '',
            'actuation_length_cm': 10,
            'offset_cm': 30,
            'avg_loop_time_ms': 35.0,
        }

        resp = client.get('/api/actuation')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['speed_kmh'] == 12.5
        assert data['source'] == 'gps'
