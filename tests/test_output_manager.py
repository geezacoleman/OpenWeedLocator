"""Tests for output_manager.py — relay control, test mocks, status indicators.

Priority 3 — nozzles must fire correctly in the field.
"""

import time
import threading
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# TestRelay / TestBuzzer / TestLED mock classes
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestTestRelay:
    """Tests for the TestRelay mock used on non-Pi platforms."""

    def test_on_off_no_crash(self):
        from utils.output_manager import TestRelay
        relay = TestRelay(0)
        relay.on()
        relay.off()

    def test_verbose_prints(self, capsys):
        from utils.output_manager import TestRelay
        relay = TestRelay(3, verbose=True)
        relay.on()
        relay.off()
        captured = capsys.readouterr()
        assert 'Relay 3 ON' in captured.out
        assert 'Relay 3 OFF' in captured.out

    def test_non_verbose_silent(self, capsys):
        from utils.output_manager import TestRelay
        relay = TestRelay(0, verbose=False)
        relay.on()
        relay.off()
        captured = capsys.readouterr()
        assert captured.out == ''


@pytest.mark.unit
class TestTestBuzzer:
    """Tests for the TestBuzzer mock."""

    def test_beep_no_crash(self):
        from utils.output_manager import TestBuzzer
        buzzer = TestBuzzer()
        buzzer.beep(on_time=0.1, off_time=0.1, n=2)

    def test_beep_verbose(self, capsys):
        from utils.output_manager import TestBuzzer
        buzzer = TestBuzzer()
        buzzer.beep(on_time=0.1, off_time=0.1, n=3, verbose=True)
        captured = capsys.readouterr()
        assert captured.out.count('BEEP') == 3


@pytest.mark.unit
class TestTestLED:
    """Tests for the TestLED mock."""

    def test_blink_no_crash(self):
        from utils.output_manager import TestLED
        led = TestLED(pin='BOARD37')
        led.blink(on_time=0.1, off_time=0.1, n=1)

    def test_on_off(self, capsys):
        from utils.output_manager import TestLED
        led = TestLED(pin='BOARD37')
        led.on()
        led.off()
        captured = capsys.readouterr()
        assert 'ON' in captured.out
        assert 'OFF' in captured.out

    def test_blink_none_n(self):
        """n=None should not crash (converted to n=1 internally)."""
        from utils.output_manager import TestLED
        led = TestLED(pin='BOARD37')
        led.blink(on_time=0.1, off_time=0.1, n=None)


# ---------------------------------------------------------------------------
# RelayControl (uses TestRelay on non-Pi)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRelayControl:
    """Tests for RelayControl class (test mode on Windows)."""

    def _make_relay_control(self):
        from utils.output_manager import RelayControl
        relay_dict = {0: 13, 1: 15, 2: 16, 3: 18}
        return RelayControl(relay_dict)

    def test_init_creates_test_relays(self):
        rc = self._make_relay_control()
        assert rc.testing is True
        # All 4 relays should be TestRelay instances
        from utils.output_manager import TestRelay
        for relay in rc.relay_dict.values():
            assert isinstance(relay, TestRelay)

    def test_relay_on_off(self):
        rc = self._make_relay_control()
        rc.relay_on(0, verbose=False)
        rc.relay_off(0, verbose=False)

    def test_all_on(self):
        rc = self._make_relay_control()
        rc.all_on(verbose=False)
        # No crash = pass

    def test_all_off(self):
        rc = self._make_relay_control()
        rc.all_off(verbose=False)

    def test_beep(self):
        rc = self._make_relay_control()
        rc.beep(duration=0.1, repeats=1)

    def test_remove_relay(self):
        rc = self._make_relay_control()
        rc.remove(2)
        assert 2 not in rc.relay_dict
        assert len(rc.relay_dict) == 3

    def test_clear_relays(self):
        rc = self._make_relay_control()
        rc.clear()
        assert len(rc.relay_dict) == 0


# ---------------------------------------------------------------------------
# RelayController (thread-based job queue system)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRelayController:
    """Tests for RelayController — spray-window (deadline) actuation.

    Each detection opens a window [time_stamp + delay, time_stamp + delay + duration].
    Overlapping windows merge: off_at only ever extends, so continuous detection
    holds a nozzle on without off/on cycling, and a live duration drop (GPS
    speed-adaptive update) can never cut short spray already promised.
    """

    def _make_controller(self):
        from utils.output_manager import RelayController
        relay_dict = {0: 13, 1: 15}
        return RelayController(relay_dict, vis=False)

    def _record_switching(self, rc):
        """Wrap the relay board calls so tests can assert real switching events."""
        events = []
        rc.relay.relay_on = lambda relay, verbose=True: events.append(('on', relay, time.time()))
        rc.relay.relay_off = lambda relay, verbose=True: events.append(('off', relay, time.time()))
        return events

    def test_init_creates_consumer_threads(self):
        rc = self._make_controller()
        # Should have a schedule and condition for each relay
        assert 0 in rc.relay_schedule_dict
        assert 1 in rc.relay_schedule_dict
        assert 0 in rc.relay_condition_dict
        assert 1 in rc.relay_condition_dict
        rc.stop()

    def test_receive_opens_window(self):
        rc = self._make_controller()
        t = time.time()
        rc.receive(relay=0, time_stamp=t, delay=0, duration=5)
        schedule = rc.relay_schedule_dict[0]
        assert schedule['on_at'] == pytest.approx(t, abs=0.001)
        assert schedule['off_at'] == pytest.approx(t + 5, abs=0.001)
        rc.stop()

    def test_overlapping_windows_merge_and_extend(self):
        rc = self._make_controller()
        t = time.time()
        rc.receive(relay=0, time_stamp=t, delay=0, duration=5)
        rc.receive(relay=0, time_stamp=t + 0.5, delay=0, duration=5)
        schedule = rc.relay_schedule_dict[0]
        assert schedule['on_at'] == pytest.approx(t, abs=0.001)
        assert schedule['off_at'] == pytest.approx(t + 5.5, abs=0.001)
        rc.stop()

    def test_duration_drop_never_shrinks_open_window(self):
        """A mid-stream GPS speed update lowering duration must not cut short
        spray that was already promised (the field off-blip bug)."""
        rc = self._make_controller()
        t = time.time()
        rc.receive(relay=0, time_stamp=t, delay=0, duration=5)
        rc.receive(relay=0, time_stamp=t + 0.1, delay=0, duration=0.05)
        schedule = rc.relay_schedule_dict[0]
        assert schedule['off_at'] == pytest.approx(t + 5, abs=0.001)
        rc.stop()

    def test_stale_window_ignored(self):
        """A detection whose whole window is already in the past schedules nothing."""
        rc = self._make_controller()
        rc.receive(relay=0, time_stamp=time.time() - 10, delay=0, duration=1)
        schedule = rc.relay_schedule_dict[0]
        assert schedule['off_at'] is None
        rc.stop()

    def test_continuous_detection_single_on_off(self):
        """Continuous detections must hold the nozzle on: exactly one ON at the
        start and one OFF after the last window closes — no cycling between."""
        rc = self._make_controller()
        events = self._record_switching(rc)
        last_t = None
        for _ in range(8):
            last_t = time.time()
            rc.receive(relay=0, time_stamp=last_t, delay=0, duration=0.3)
            time.sleep(0.05)
        time.sleep(0.6)  # let the final window close

        ons = [e for e in events if e[0] == 'on' and e[1] == 0]
        offs = [e for e in events if e[0] == 'off' and e[1] == 0]
        assert len(ons) == 1
        assert len(offs) == 1
        # Off must come only after the final window's deadline...
        assert offs[0][2] >= last_t + 0.3 - 0.02
        # ...but windows must MERGE, not accumulate: off no later than the last
        # detection + one duration (+ margin). 8 stacked 0.3s jobs would run
        # ~2.4s — the trailing over-spray seen in the field.
        assert offs[0][2] <= last_t + 0.3 + 0.2
        rc.stop()

    def test_continuous_detection_with_delay_no_cycling(self):
        """Field bug repro: with a GPS-derived delay and a duration shorter than
        the frame interval, the old queue model strobed the nozzle (off + full
        delay re-applied between every frame) despite continuous detection.
        Windows [ts+delay, ts+delay+duration] overlap the next frame's arrival,
        so they must merge into one continuous activation."""
        rc = self._make_controller()
        events = self._record_switching(rc)
        last_t = None
        for _ in range(6):
            last_t = time.time()
            rc.receive(relay=0, time_stamp=last_t, delay=0.2, duration=0.05)
            time.sleep(0.1)
        time.sleep(0.6)  # let the final window close

        ons = [e for e in events if e[0] == 'on' and e[1] == 0]
        offs = [e for e in events if e[0] == 'off' and e[1] == 0]
        assert len(ons) == 1, f"nozzle cycled during continuous detection: {events}"
        assert len(offs) == 1
        assert offs[0][2] >= last_t + 0.25 - 0.02
        rc.stop()

    def test_duration_drop_does_not_blip_relay(self):
        """Field bug repro: the 1 Hz speed broadcast lowering actuation_duration
        mid-spray must not switch an active nozzle off before the spray already
        promised has finished (the visible off-blip on the sprayer)."""
        rc = self._make_controller()
        events = self._record_switching(rc)
        t = time.time()
        rc.receive(relay=0, time_stamp=t, delay=0, duration=0.6)
        time.sleep(0.1)
        # New detection arrives carrying a much smaller duration (speed rose)
        rc.receive(relay=0, time_stamp=time.time(), delay=0, duration=0.05)
        time.sleep(0.2)  # t+0.3: well inside the original 0.6s window
        assert not any(e[0] == 'off' and e[1] == 0 for e in events), (
            "duration drop cut short an active spray window"
        )
        time.sleep(0.5)  # let the window close
        offs = [e for e in events if e[0] == 'off' and e[1] == 0]
        assert len(offs) == 1
        assert offs[0][2] >= t + 0.6 - 0.02
        rc.stop()

    def test_relay_turns_off_after_window(self):
        rc = self._make_controller()
        events = self._record_switching(rc)
        rc.receive(relay=0, time_stamp=time.time(), delay=0, duration=0.15)
        time.sleep(0.5)
        assert events, "relay never switched"
        assert events[-1][0] == 'off'
        assert rc.relay_schedule_dict[0]['off_at'] is None  # schedule reset
        rc.stop()

    def test_delay_defers_activation(self):
        rc = self._make_controller()
        events = self._record_switching(rc)
        t = time.time()
        rc.receive(relay=0, time_stamp=t, delay=0.3, duration=0.2)
        time.sleep(0.1)
        assert not any(e[0] == 'on' for e in events), "fired before delay elapsed"
        time.sleep(0.5)
        ons = [e for e in events if e[0] == 'on' and e[1] == 0]
        assert len(ons) == 1
        assert ons[0][2] >= t + 0.3 - 0.02
        rc.stop()

    def test_concurrent_activations_no_interference(self):
        """Two relays activated simultaneously should not interfere."""
        rc = self._make_controller()
        events = self._record_switching(rc)
        t = time.time()
        rc.receive(relay=0, time_stamp=t, delay=0, duration=0.1)
        rc.receive(relay=1, time_stamp=t, delay=0, duration=0.1)
        time.sleep(0.4)
        for relay in (0, 1):
            assert any(e[0] == 'on' and e[1] == relay for e in events)
            assert any(e[0] == 'off' and e[1] == relay for e in events)
        rc.stop()

    def test_stop_turns_off_active_relay(self):
        """stop() must never leave a nozzle on."""
        rc = self._make_controller()
        events = self._record_switching(rc)
        rc.receive(relay=0, time_stamp=time.time(), delay=0, duration=5)
        time.sleep(0.1)
        assert any(e[0] == 'on' for e in events)
        rc.stop()
        time.sleep(0.2)
        assert events[-1][0] == 'off'


# ---------------------------------------------------------------------------
# HeadlessStatusIndicator
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestHeadlessStatusIndicator:
    """Tests for HeadlessStatusIndicator."""

    def test_init_no_crash(self, tmp_path):
        from utils.output_manager import HeadlessStatusIndicator
        indicator = HeadlessStatusIndicator(save_directory=str(tmp_path))
        indicator.stop()

    def test_drive_full_at_90_percent(self, tmp_path):
        from utils.output_manager import HeadlessStatusIndicator
        indicator = HeadlessStatusIndicator(save_directory=str(tmp_path))
        indicator._update_storage_indicator(0.91)
        assert indicator.DRIVE_FULL is True
        indicator.stop()

    def test_drive_not_full_below_90(self, tmp_path):
        from utils.output_manager import HeadlessStatusIndicator
        indicator = HeadlessStatusIndicator(save_directory=str(tmp_path))
        indicator._update_storage_indicator(0.5)
        assert indicator.DRIVE_FULL is False
        indicator.stop()

    def test_no_save_mode(self):
        from utils.output_manager import HeadlessStatusIndicator
        indicator = HeadlessStatusIndicator(save_directory=None, no_save=True)
        indicator.stop()

    def test_drive_full_clears_when_space_freed(self, tmp_path):
        """Deleting sessions below 90% must unlatch DRIVE_FULL so recording
        can resume without a restart (R2 eMMC recovery path)."""
        from utils.output_manager import HeadlessStatusIndicator
        indicator = HeadlessStatusIndicator(save_directory=str(tmp_path))
        indicator._update_storage_indicator(0.91)
        assert indicator.DRIVE_FULL is True
        indicator._update_storage_indicator(0.5)
        assert indicator.DRIVE_FULL is False
        indicator.stop()


# ---------------------------------------------------------------------------
# LED capability probe (R1 journal quiet)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLEDCapabilityProbe:
    """LED signalling degrades to a single warning on units without
    passwordless sudo or LED sysfs paths (sealed CM5, desktops)."""

    def _no_save_indicator(self):
        from utils.output_manager import HeadlessStatusIndicator
        return HeadlessStatusIndicator(save_directory=None, no_save=True)

    def test_disabled_in_test_mode(self):
        indicator = self._no_save_indicator()
        assert indicator.leds_enabled is False
        indicator.stop()

    def test_missing_sysfs_warns_once(self, caplog):
        import logging
        indicator = self._no_save_indicator()
        indicator.testing = False
        with patch('utils.output_manager.os.path.exists', return_value=False), \
                caplog.at_level(logging.WARNING, logger='utils.output_manager'):
            assert indicator._probe_led_control() is False
        warnings = [r for r in caplog.records if 'LED signalling disabled' in r.message]
        assert len(warnings) == 1
        indicator.stop()

    def test_no_passwordless_sudo_disables(self, caplog):
        import logging
        indicator = self._no_save_indicator()
        indicator.testing = False
        failed = MagicMock(returncode=1)
        with patch('utils.output_manager.os.path.exists', return_value=True), \
                patch('utils.output_manager.subprocess.run', return_value=failed), \
                caplog.at_level(logging.WARNING, logger='utils.output_manager'):
            assert indicator._probe_led_control() is False
        warnings = [r for r in caplog.records if 'LED signalling disabled' in r.message]
        assert len(warnings) == 1
        indicator.stop()

    def test_sudo_missing_disables(self):
        indicator = self._no_save_indicator()
        indicator.testing = False
        with patch('utils.output_manager.os.path.exists', return_value=True), \
                patch('utils.output_manager.subprocess.run', side_effect=OSError('no sudo')):
            assert indicator._probe_led_control() is False
        indicator.stop()

    def test_probe_success_enables(self):
        indicator = self._no_save_indicator()
        indicator.testing = False
        ok = MagicMock(returncode=0)
        with patch('utils.output_manager.os.path.exists', return_value=True), \
                patch('utils.output_manager.subprocess.run', return_value=ok):
            assert indicator._probe_led_control() is True
        indicator.stop()

    def test_set_led_state_noop_when_disabled(self):
        indicator = self._no_save_indicator()
        indicator.testing = False
        indicator.leds_enabled = False
        with patch('utils.output_manager.subprocess.run') as run:
            indicator._set_led_state('ACT', 1)
            indicator._set_led_trigger('ACT', 'none')
        run.assert_not_called()
        indicator.stop()


# ---------------------------------------------------------------------------
# Error flash lifecycle: bounded loop + clear_error() (R1 journal quiet)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestErrorFlashLifecycle:

    def _led_indicator(self):
        """Indicator with LED signalling forced on. testing=True still
        short-circuits _set_led_state, so no subprocess is ever run."""
        from utils.output_manager import HeadlessStatusIndicator
        indicator = HeadlessStatusIndicator(save_directory=None, no_save=True)
        indicator.leds_enabled = True
        return indicator

    def test_error_starts_flash_thread(self):
        indicator = self._led_indicator()
        indicator.error(3)
        assert indicator.flashing_thread is not None
        assert indicator.flashing_thread.is_alive()
        indicator.stop()

    def test_clear_error_stops_flash_thread(self):
        indicator = self._led_indicator()
        indicator.error(3)
        indicator.clear_error()
        indicator.flashing_thread.join(timeout=3)
        assert not indicator.flashing_thread.is_alive()
        assert indicator.error_code is None
        indicator.stop()

    def test_error_without_leds_starts_no_thread(self):
        """Sealed units: error code still latches for the dashboard/app, but
        no flash thread (and so no sudo subprocess loop) ever starts."""
        indicator = self._led_indicator()
        indicator.leds_enabled = False
        indicator.error(4)
        assert indicator.flashing_thread is None
        assert indicator.error_code == 4
        indicator.stop()

    def test_update_clears_no_drive_error_when_directory_returns(self, tmp_path):
        from utils.output_manager import HeadlessStatusIndicator
        indicator = HeadlessStatusIndicator(save_directory=None)
        indicator.update()
        assert indicator.error_code == 6
        indicator.save_directory = str(tmp_path)
        indicator.update()
        assert indicator.error_code is None
        indicator.stop()

    def test_stop_ends_flash_thread_promptly(self):
        indicator = self._led_indicator()
        indicator.error(6)
        indicator.stop()
        indicator.flashing_thread.join(timeout=3)
        assert not indicator.flashing_thread.is_alive()


@pytest.mark.unit
class TestAdvancedIndicatorErrorRecovery:

    def _advanced(self):
        from utils.output_manager import AdvancedStatusIndicator
        return AdvancedStatusIndicator(save_directory=None, status_led_pin=None)

    def test_clear_error_leaves_error_state(self):
        from utils.output_manager import AdvancedIndicatorState
        indicator = self._advanced()
        indicator.error(2)
        assert indicator.state == AdvancedIndicatorState.ERROR
        indicator.clear_error()
        assert indicator.state == AdvancedIndicatorState.IDLE
        assert indicator.error_code is None
        indicator.stop()

    def test_clear_error_restores_activity_state(self):
        from utils.output_manager import AdvancedIndicatorState
        indicator = self._advanced()
        indicator.enable_weed_detection()
        indicator.error(1)
        indicator.clear_error()
        assert indicator.state == AdvancedIndicatorState.DETECTING
        indicator.stop()

    def test_drive_full_recovery(self):
        indicator = self._advanced()
        indicator._update_storage_indicator(0.95)
        assert indicator.DRIVE_FULL is True
        indicator._update_storage_indicator(0.5)
        assert indicator.DRIVE_FULL is False
        assert indicator.error_code is None
        indicator.stop()


# ---------------------------------------------------------------------------
# Storage watchdog thread survives disk_usage failures (R1; R2 prerequisite)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestStorageWatchdogResilience:

    def test_thread_survives_oserror_and_logs_once(self, tmp_path, caplog):
        """A yanked USB drive raises OSError from disk_usage; the watchdog
        thread must survive and log once per failure transition, then log
        recovery once when the drive is back."""
        import logging
        from utils.output_manager import HeadlessStatusIndicator
        indicator = HeadlessStatusIndicator(save_directory=str(tmp_path))

        with caplog.at_level(logging.INFO, logger='utils.output_manager'):
            with patch('utils.output_manager.shutil.disk_usage',
                       side_effect=OSError('drive yanked')):
                indicator.start_storage_indicator()
                time.sleep(0.2)
                assert indicator.thread.is_alive()
                # second watchdog cycle, still failing
                indicator.update_event.set()
                time.sleep(0.2)
                assert indicator.thread.is_alive()
                assert indicator._update_failed is True

            # drive is back: next cycle recovers
            indicator.update_event.set()
            time.sleep(0.2)
            assert indicator._update_failed is False

        failures = [r for r in caplog.records if 'Storage monitoring error' in r.message]
        recoveries = [r for r in caplog.records if 'Storage monitoring recovered' in r.message]
        assert len(failures) == 1
        assert len(recoveries) == 1
        indicator.stop()
