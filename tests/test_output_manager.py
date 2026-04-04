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
    """Tests for RelayController — the thread-based actuation queue."""

    def _make_controller(self):
        from utils.output_manager import RelayController
        relay_dict = {0: 13, 1: 15}
        return RelayController(relay_dict, vis=False)

    def test_init_creates_consumer_threads(self):
        rc = self._make_controller()
        # Should have queue and condition for each relay
        assert 0 in rc.relay_queue_dict
        assert 1 in rc.relay_queue_dict
        assert 0 in rc.relay_condition_dict
        assert 1 in rc.relay_condition_dict
        rc.stop()

    def test_receive_enqueues_job(self):
        rc = self._make_controller()
        rc.receive(relay=0, time_stamp=time.time(), delay=0, duration=0.05)
        # Give consumer thread time to process
        time.sleep(0.2)
        rc.stop()

    def test_receive_multiple_relays(self):
        rc = self._make_controller()
        rc.receive(relay=0, time_stamp=time.time(), delay=0, duration=0.05)
        rc.receive(relay=1, time_stamp=time.time(), delay=0, duration=0.05)
        time.sleep(0.3)
        rc.stop()

    def test_concurrent_activations_no_interference(self):
        """Two relays activated simultaneously should not interfere."""
        rc = self._make_controller()
        t = time.time()
        rc.receive(relay=0, time_stamp=t, delay=0, duration=0.1)
        rc.receive(relay=1, time_stamp=t, delay=0, duration=0.1)
        time.sleep(0.3)
        rc.stop()


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
