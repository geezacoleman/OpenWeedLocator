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
        result = calc.compute(0.3)  # Below MIN_SPEED of 0.5
        assert result['source'] == 'config'

    def test_safety_cap_slow_speed(self):
        """Very slow speed shouldn't produce duration > MAX_DURATION."""
        calc = self._make(actuation_length_cm=50, offset_cm=100)
        result = calc.compute(0.6)  # Just above MIN_SPEED
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

    def test_broadcast_carries_geometry_and_speed(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[3] / 'controller' / 'networked'
               / 'networked.py').read_text(encoding='utf-8')
        loop = src.split('def _actuation_broadcast_loop', 1)[1].split('\n    def ', 1)[0]
        for field in ("'actuation_length_cm'", "'offset_cm'", "'speed_kmh'"):
            assert field in loop
        assert 'compute(speed_kmh, avg_loop_time)' in loop


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

    def test_post_actuation_config(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        import controller.networked.networked as net_mod

        # Create a real ActuationCalculator for the test
        from controller.networked.networked import ActuationCalculator
        net_mod.controller.actuation_calculator = ActuationCalculator()

        resp = client.post('/api/actuation/config', json={
            'actuation_length_cm': 15,
            'offset_cm': 40
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['actuation_length_cm'] == 15
        assert data['offset_cm'] == 40

    def test_post_actuation_config_bounds_clamping(self, networked_test_client):
        client, mock_ctrl = networked_test_client
        import controller.networked.networked as net_mod
        from controller.networked.networked import ActuationCalculator
        net_mod.controller.actuation_calculator = ActuationCalculator()

        resp = client.post('/api/actuation/config', json={
            'actuation_length_cm': 999,  # Should clamp to 50
            'offset_cm': -5              # Should clamp to 0
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['actuation_length_cm'] == 50.0
        assert data['offset_cm'] == 0.0
