"""utils/output_manager.py: the nozzle scheduler (RelayController), the relay
board (RelayControl) and the status indicators.

Every spray test here asserts on WHEN the relay board was driven, with a bound
on both sides. A relay that switches late is the field failure (2026-11-16:
relays seconds late behind a GPS-derived delay), and a lower bound alone
passes it. The same paths on a whole Owl are in tests/owl/.
"""

import time
import threading
from unittest.mock import MagicMock, patch

import pytest

import utils.output_manager as om
from utils.output_manager import RelayController

# A consumer thread waking on its Condition and driving the pin. Measured at
# well under 10 ms; anything past this is a late nozzle, not jitter.
LATE_S = 0.1
# Slack on "not before": the event is stamped just after the drive call.
EARLY_S = 0.001


@pytest.fixture
def timed():
    """timed(relays=(0, 1)) -> (RelayController, events). events holds every
    drive of the relay board as (kind, relay, time.monotonic()), in order.
    Controllers are stopped at teardown."""
    built = []

    def _build(relays=(0, 1)):
        rc = RelayController({r: 13 + r for r in relays}, vis=False)
        events = []
        lock = threading.Lock()

        def on(relay, verbose=True):
            with lock:
                events.append(('on', relay, time.monotonic()))

        def off(relay, verbose=True):
            with lock:
                events.append(('off', relay, time.monotonic()))

        rc.relay.relay_on, rc.relay.relay_off = on, off
        built.append(rc)
        return rc, events

    yield _build
    for rc in built:
        rc.stop_test(notify=False)
        rc.stop()


def _at(events, kind, relay=0):
    return [at for k, r, at in events if k == kind and r == relay]


def _within(at, earliest, latest=None):
    """earliest <= at <= latest (default: earliest + LATE_S)."""
    latest = earliest + LATE_S if latest is None else latest
    return earliest - EARLY_S <= at <= latest


# ---------------------------------------------------------------------------
# Spray windows: when the relay switches
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSprayWindowTiming:
    """Each detection opens a window [time_stamp + delay, + duration].
    Overlapping windows merge: the ON never moves later and the OFF only
    extends, so a weed seen frame after frame holds the nozzle on once."""

    @pytest.mark.parametrize('delay', [0.0, 0.3])
    def test_relay_follows_the_window_on_both_edges(self, timed, delay):
        """delay 0 is the normal OWL setting: the relay is driven at once.
        With a delay it is driven at the delay, not before and not late."""
        rc, events = timed()
        t = time.monotonic()
        rc.receive(relay=0, time_stamp=t, delay=delay, duration=0.2)
        time.sleep(delay + 0.2 + 2 * LATE_S)

        ons, offs = _at(events, 'on'), _at(events, 'off')
        assert len(ons) == 1 and len(offs) == 1, events
        assert _within(ons[0], t + delay), f'ON {ons[0] - t:.3f} s after detection, delay {delay}'
        assert _within(offs[0], t + delay + 0.2), f'OFF {offs[0] - t:.3f} s after detection'
        assert not _at(events, 'on', 1) and not _at(events, 'off', 1), 'the other lane switched'

    @pytest.mark.parametrize('delay, duration, interval, frames', [
        (0.0, 0.3, 0.05, 8),     # a weed in view for 8 frames
        (0.2, 0.05, 0.1, 6),     # GPS delay, spray shorter than the frame gap
    ])
    def test_continuous_detection_is_one_on_at_the_first_window_and_one_off_after_the_last(
            self, timed, delay, duration, interval, frames):
        """Field bug repro (second row): the old queue model strobed the
        nozzle between frames. And the ON belongs to the FIRST detection:
        a merge that lets every new detection push it later (max instead of
        min on on_at) delays the spray for as long as the weed is in view."""
        rc, events = timed()
        first_t = last_t = None
        for _ in range(frames):
            last_t = time.monotonic()
            first_t = first_t if first_t is not None else last_t
            rc.receive(relay=0, time_stamp=last_t, delay=delay, duration=duration)
            time.sleep(interval)
        time.sleep(delay + duration + 2 * LATE_S)

        ons, offs = _at(events, 'on'), _at(events, 'off')
        assert len(ons) == 1, f'nozzle cycled during continuous detection: {events}'
        assert len(offs) == 1
        assert _within(ons[0], first_t + delay), (
            f'ON {ons[0] - first_t:.3f} s after the first detection, delay {delay}')
        # windows merge, they do not stack: 8 x 0.3 s queued would run 2.4 s
        assert _within(offs[0], last_t + delay + duration), (
            f'OFF {offs[0] - last_t:.3f} s after the last detection')

    def test_a_shorter_delay_brings_a_pending_on_forward_and_keeps_the_later_off(self, timed):
        """Speed rose between two frames of the same weed: the nozzle opens
        at the earlier of the two starts and closes at the later of the two
        ends."""
        rc, events = timed()
        t = time.monotonic()
        rc.receive(relay=0, time_stamp=t, delay=0.6, duration=0.2)   # [0.6, 0.8]
        rc.receive(relay=0, time_stamp=t, delay=0.2, duration=0.2)   # [0.2, 0.4]
        time.sleep(0.8 + 2 * LATE_S)
        ons, offs = _at(events, 'on'), _at(events, 'off')
        assert len(ons) == 1 and len(offs) == 1, events
        assert _within(ons[0], t + 0.2)
        assert _within(offs[0], t + 0.8)

    def test_a_duration_drop_does_not_cut_an_open_window_short(self, timed):
        """Field bug repro: the 1 Hz speed broadcast lowering
        actuation_duration mid-spray blipped an active nozzle off."""
        rc, events = timed()
        t = time.monotonic()
        rc.receive(relay=0, time_stamp=t, delay=0, duration=0.6)
        time.sleep(0.1)
        rc.receive(relay=0, time_stamp=time.monotonic(), delay=0, duration=0.05)
        time.sleep(0.5 + 2 * LATE_S)
        ons, offs = _at(events, 'on'), _at(events, 'off')
        assert len(ons) == 1 and len(offs) == 1, events
        assert _within(offs[0], t + 0.6), f'OFF {offs[0] - t:.3f} s into a 0.6 s window'

    def test_a_window_already_in_the_past_drives_nothing(self, timed):
        rc, events = timed()
        rc.receive(relay=0, time_stamp=time.monotonic() - 10, delay=0, duration=1)
        time.sleep(0.3)
        assert events == []

    def test_two_lanes_at_once_each_keep_their_own_window(self, timed):
        rc, events = timed()
        t = time.monotonic()
        rc.receive(relay=0, time_stamp=t, delay=0, duration=0.1)
        rc.receive(relay=1, time_stamp=t, delay=0, duration=0.3)
        time.sleep(0.3 + 2 * LATE_S)
        for relay, duration in ((0, 0.1), (1, 0.3)):
            ons, offs = _at(events, 'on', relay), _at(events, 'off', relay)
            assert len(ons) == 1 and len(offs) == 1, events
            assert _within(ons[0], t)
            assert _within(offs[0], t + duration)

    def test_stop_switches_an_open_window_off_at_once(self, timed):
        """stop() must never leave a spot nozzle on."""
        rc, events = timed()
        rc.receive(relay=0, time_stamp=time.monotonic(), delay=0, duration=5)
        time.sleep(0.1)
        assert [k for k, r, _ in events] == ['on']
        stopped = time.monotonic()
        rc.stop()
        time.sleep(2 * LATE_S)
        # stop() drives every relay off itself, and the consumer closes its
        # own window too: a second off is harmless, a later on is not
        offs = _at(events, 'off', 0)
        assert offs and _within(offs[0], stopped), events
        assert [k for k, r, at in events if at > stopped] == ['off'] * len(
            [e for e in events if e[2] > stopped]), events


@pytest.mark.unit
class TestStaleAnchorCost:
    """What a timestamp taken earlier than the receive() call costs, at the
    relay.

    These pin down why owl.py stamps `actuation_time` AFTER detection rather
    than at frame capture. With delay = 0 the nozzle cannot open earlier than
    the call whatever the stamp says, so an earlier anchor only brings the
    OFF forward: every spray is shortened by the processing time and, at
    speed, dropped.
    """

    @pytest.mark.parametrize('lag_s, delay, duration, on_after, off_after', [
        (0.0, 0.0, 0.15, 0.0, 0.15),      # fresh anchor: the whole duration
        (0.04, 0.0, 0.15, 0.0, 0.11),     # 40 ms of lag costs 40 ms of spray
        (0.04, 0.2, 0.03, 0.16, 0.19),    # a real delay still lands in the future
    ])
    def test_spray_runs_from_the_anchor_not_from_the_call(self, timed, lag_s, delay, duration,
                                                          on_after, off_after):
        rc, events = timed(relays=(0,))
        called = time.monotonic()
        rc.receive(relay=0, time_stamp=called - lag_s, delay=delay, duration=duration)
        time.sleep(off_after + 2 * LATE_S)
        ons, offs = _at(events, 'on'), _at(events, 'off')
        assert len(ons) == 1 and len(offs) == 1, events
        assert _within(ons[0], called + on_after)
        assert _within(offs[0], called + off_after), (
            f'sprayed until {offs[0] - called:.3f} s after the call, wanted {off_after}')

    def test_a_stale_anchor_cancels_a_short_spray_completely(self, timed):
        """The failure that matters: at 12 km/h a 10 cm patch is a 30 ms
        spray. Anchored 40 ms back, the whole window is in the past and the
        OWL stops spraying with nothing logged."""
        rc, events = timed(relays=(0,))
        rc.receive(relay=0, time_stamp=time.monotonic() - 0.040, delay=0, duration=0.030)
        time.sleep(0.2)
        assert events == []


# ---------------------------------------------------------------------------
# HeadlessStatusIndicator
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestHeadlessStatusIndicator:
    """Tests for HeadlessStatusIndicator."""

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

        failures = [r for r in caplog.records if 'Recording drive unreadable' in r.message]
        recoveries = [r for r in caplog.records if 'Storage monitoring recovered' in r.message]
        assert len(failures) == 1
        assert len(recoveries) == 1
        indicator.stop()


@pytest.mark.unit
class TestNozzleSwitchFailure:
    """A switching failure must never leave a nozzle energised and unattended.

    The consumer thread had no exception handling: a raise from the GPIO call
    killed the thread, and if it happened while the nozzle was on it stayed on
    until the process ended, silently, with the main loop still running.
    """

    def _make_controller(self):
        from utils.output_manager import RelayController
        return RelayController({0: 13}, vis=False)

    def test_indicator_failure_does_not_strand_the_nozzle(self):
        """An LED/visualiser fault must not stop the nozzle switching off."""
        rc = self._make_controller()
        events = []
        rc.relay.relay_on = lambda relay, verbose=True: events.append(('on', relay))
        rc.relay.relay_off = lambda relay, verbose=True: events.append(('off', relay))

        class BrokenLED:
            def blink(self, **kwargs):
                raise RuntimeError('LED sysfs gone')

        rc.status_led = BrokenLED()
        rc.receive(relay=0, time_stamp=time.monotonic(), delay=0, duration=0.1)
        time.sleep(0.4)
        rc.stop()
        time.sleep(0.1)

        assert ('on', 0) in events, 'nozzle never fired'
        assert events[-1] == ('off', 0), f'nozzle not switched off: {events}'

    def test_consumer_survives_a_raising_switch_and_still_turns_off(self):
        """If switching ON raises, the thread must stay alive and force OFF."""
        rc = self._make_controller()
        events = []
        state = {'fail_on': True}

        def relay_on(relay, verbose=True):
            events.append(('on', relay))
            if state['fail_on']:
                raise RuntimeError('lgpio busy')

        rc.relay.relay_on = relay_on
        rc.relay.relay_off = lambda relay, verbose=True: events.append(('off', relay))

        rc.receive(relay=0, time_stamp=time.monotonic(), delay=0, duration=0.1)
        time.sleep(0.4)

        # the thread must still be running its window, not dead
        assert ('off', 0) in events, f'nozzle never turned off after a failure: {events}'
        rc.stop()
        time.sleep(0.1)

    def test_stop_still_turns_off_after_an_earlier_failure(self):
        rc = self._make_controller()
        events = []
        rc.relay.relay_on = lambda relay, verbose=True: events.append(('on', relay))
        rc.relay.relay_off = lambda relay, verbose=True: events.append(('off', relay))

        class BrokenVis:
            def update(self, **kwargs):
                raise RuntimeError('vis dead')

        rc.vis = True
        rc.relay_vis = BrokenVis()
        rc.receive(relay=0, time_stamp=time.monotonic(), delay=0, duration=5)
        time.sleep(0.2)
        rc.stop()
        time.sleep(0.2)
        assert events[-1] == ('off', 0), f'stop() left the nozzle on: {events}'

    def test_failed_off_is_retried_not_left_energised(self):
        """A failing off must be retried, not slept on with a live nozzle."""
        rc = self._make_controller()
        events = []
        state = {'fail': True}

        def relay_off(relay, verbose=True):
            events.append(('off', relay))
            if state['fail']:
                raise RuntimeError('gpio busy')

        rc.relay.relay_on = lambda relay, verbose=True: events.append(('on', relay))
        rc.relay.relay_off = relay_off

        rc.receive(relay=0, time_stamp=time.monotonic(), delay=0, duration=0.1)
        time.sleep(0.6)
        first_attempts = [e for e in events if e[0] == 'off']
        assert len(first_attempts) >= 2, (
            f'off should be retried while it keeps failing, got {events}')

        # once the hardware recovers, the nozzle actually goes off
        state['fail'] = False
        time.sleep(1.3)
        rc.stop()
        time.sleep(0.1)
        assert events[-1] == ('off', 0)


# ---------------------------------------------------------------------------
# One spray owner (v3.27): set_mode, bounded tests, RelayControl.stop
# ---------------------------------------------------------------------------

def _recording_controller(relays=(0, 1)):
    from utils.output_manager import RelayController
    rc = RelayController({r: 13 + r for r in relays}, vis=False)
    events = []
    lock = threading.Lock()

    def on(relay, verbose=True):
        with lock:
            events.append(('on', relay))

    def off(relay, verbose=True):
        with lock:
            events.append(('off', relay))

    rc.relay.relay_on = on
    rc.relay.relay_off = off
    return rc, events


def _last(events, relay):
    for kind, r in reversed(events):
        if r == relay:
            return kind
    return None


@pytest.mark.unit
class TestSetMode:
    def test_blanket_turns_everything_on_and_off_turns_it_off(self):
        rc, events = _recording_controller()
        assert rc.set_mode('blanket') is True
        assert _last(events, 0) == 'on' and _last(events, 1) == 'on'
        rc.set_mode('off')
        assert _last(events, 0) == 'off' and _last(events, 1) == 'off'
        rc.stop()

    def test_change_guarded(self):
        rc, events = _recording_controller()
        rc.set_mode('off')
        before = len(events)
        assert rc.set_mode('off') is False
        assert len(events) == before
        rc.stop()

    def test_unknown_mode_raises(self):
        rc, _ = _recording_controller()
        with pytest.raises(ValueError):
            rc.set_mode('sideways')
        rc.stop()

    def test_receive_ignored_outside_spot(self):
        rc, events = _recording_controller()
        for mode in ('off', 'blanket'):
            rc.set_mode(mode)
            before = list(events)
            rc.receive(relay=0, time_stamp=time.monotonic(), delay=0, duration=0.2)
            time.sleep(0.35)
            assert events == before, f'{mode}: a spray window switched a relay: {events[len(before):]}'
        rc.stop()

    def test_pending_window_cannot_turn_a_blanket_lane_off(self):
        """The audit's bug: a spot window still pending when blanket started
        switched that lane off when it closed."""
        rc, events = _recording_controller()
        rc.receive(relay=0, time_stamp=time.monotonic(), delay=0, duration=0.2)
        time.sleep(0.05)
        rc.set_mode('blanket')
        time.sleep(0.4)
        assert _last(events, 0) == 'on', f'blanket lane switched off: {events}'
        rc.stop()

    def test_first_window_after_blanket_still_fires(self):
        """A consumer that thought its relay was on (from before the blanket)
        must still switch on for the first spot window afterwards."""
        rc, events = _recording_controller()
        rc.receive(relay=0, time_stamp=time.monotonic(), delay=0, duration=5)
        time.sleep(0.1)
        rc.set_mode('blanket')
        rc.set_mode('spot')
        assert _last(events, 0) == 'off'
        rc.receive(relay=0, time_stamp=time.monotonic(), delay=0, duration=0.3)
        time.sleep(0.1)
        assert _last(events, 0) == 'on', f'first weed after blanket missed: {events}'
        time.sleep(0.4)
        assert _last(events, 0) == 'off'
        rc.stop()


@pytest.mark.unit
class TestBoundedTests:
    def test_test_runs_in_off_mode_and_ends_by_itself(self):
        rc, events = _recording_controller()
        rc.set_mode('off')
        ended = threading.Event()
        rc.on_test_end = ended.set
        assert rc.start_test([0], 0.2) == [0]
        assert _last(events, 0) == 'on'
        assert rc.test_relays == [0]
        assert ended.wait(1.0)
        assert _last(events, 0) == 'off'
        assert rc.test_relays == []
        rc.stop()

    def test_renew_extends_but_never_past_the_cap(self):
        rc, events = _recording_controller()
        rc.set_mode('off')
        rc.start_test([0, 1], 0.2, max_s=0.5)
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline and rc.test_relays:
            rc.renew_test(0.2)
            time.sleep(0.05)
        assert rc.test_relays == [], 'renewing kept the test past its cap'
        assert _last(events, 0) == 'off' and _last(events, 1) == 'off'
        rc.stop()

    def test_uncapped_hold_stays_on_while_renewed(self):
        """Hold to spray (max_s=None) has no maximum on-time: renewed, it
        never drops out; it ends only once the renews stop. A 10 s cap
        switched the nozzles off under a finger still holding."""
        rc, events = _recording_controller()
        rc.set_mode('off')
        rc.start_test([0, 1], 0.2, max_s=None)
        assert rc.test_capped is False
        since = len(events)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            assert rc.renew_test(0.2) is True
            assert rc.test_relays == [0, 1], 'hold dropped out while renewed'
            time.sleep(0.05)
        held = events[since:]
        assert ('off', 0) not in held and ('off', 1) not in held, held
        time.sleep(0.4)   # renews stopped: the lease runs out
        assert rc.test_relays == []
        assert _last(events, 0) == 'off' and _last(events, 1) == 'off'
        rc.stop()

    def test_renew_without_a_test_is_refused(self):
        rc, _ = _recording_controller()
        assert rc.renew_test(1.0) is False
        rc.stop()

    def test_test_end_restores_blanket(self):
        rc, events = _recording_controller()
        rc.set_mode('blanket')
        rc.start_test([0], 5)
        rc.stop_test()
        assert _last(events, 0) == 'on'
        rc.stop()

    def test_mode_change_leaves_test_relays_alone(self):
        rc, events = _recording_controller()
        rc.set_mode('blanket')
        rc.start_test([0], 5)
        rc.set_mode('off')
        assert _last(events, 0) == 'on', 'mode change cut a running test short'
        assert _last(events, 1) == 'off'
        rc.stop_test()
        assert _last(events, 0) == 'off'
        rc.stop()

    def test_spot_consumer_does_not_fight_a_test(self):
        rc, events = _recording_controller()
        rc.start_test([0], 5)
        rc.receive(relay=0, time_stamp=time.monotonic(), delay=0, duration=0.1)
        time.sleep(0.3)
        assert _last(events, 0) == 'on', 'spray window end switched a tested relay off'
        rc.stop_test()
        rc.stop()


@pytest.mark.unit
class TestStuckCallbackNoDeadlock:
    def test_on_stuck_runs_without_blocking_set_mode(self):
        """on_stuck is called on a consumer thread holding its Condition; a
        handler that only records the message keeps set_mode working."""
        rc, events = _recording_controller(relays=(0,))
        seen = []
        rc.on_stuck = seen.append

        def broken_off(relay, verbose=True):
            raise RuntimeError('gpio busy')

        rc.relay.relay_off = broken_off
        rc.receive(relay=0, time_stamp=time.monotonic(), delay=0, duration=0.05)
        time.sleep(0.3)
        assert seen, 'stuck relay never reported'
        done = threading.Event()
        threading.Thread(target=lambda: (rc.set_mode('off'), done.set()), daemon=True).start()
        assert done.wait(3.0), 'set_mode deadlocked behind the stuck relay'
        rc.stop()


@pytest.mark.unit
def test_relay_control_stop_turns_relays_off_before_clearing():
    from utils.output_manager import RelayControl
    rc = RelayControl({0: 13, 1: 15})
    offs = []
    for number, relay in rc.relay_dict.items():
        relay.off = (lambda n: (lambda: offs.append(n)))(number)
    rc.stop()
    assert sorted(offs) == [0, 1]
    assert rc.relay_dict == {}


@pytest.mark.unit
class TestStorageRecoveryKeepsFaultCodes:
    """Drive-full recovery used to call clear_error(), wiping a spraying
    fault's LED code (on a Ute the LED is the only signal)."""

    def test_recovery_keeps_a_fault_code(self):
        from utils.output_manager import HeadlessStatusIndicator
        ind = HeadlessStatusIndicator(save_directory=None, no_save=True)
        ind.DRIVE_FULL = True
        ind.error(7)                    # relay stuck
        ind._update_storage_indicator(0.5)
        assert ind.DRIVE_FULL is False
        assert ind.error_code == 7

    def test_recovery_clears_a_storage_code(self):
        from utils.output_manager import HeadlessStatusIndicator
        ind = HeadlessStatusIndicator(save_directory=None, no_save=True)
        ind.DRIVE_FULL = True
        ind.error(5)
        ind._update_storage_indicator(0.5)
        assert ind.error_code is None


# ---------------------------------------------------------------------------
# A relay that will not switch off, outside the consumer threads
# ---------------------------------------------------------------------------

def _refuse_off(rc, failures=None):
    """Make the board's relay_off raise (`failures` times, default always).
    Returns the list of attempts."""
    attempts = []

    def relay_off(relay, verbose=True):
        attempts.append(relay)
        if failures is None or len(attempts) <= failures:
            raise RuntimeError('gpio busy')

    rc.relay.relay_off = relay_off
    return attempts


@pytest.mark.unit
class TestStuckRelayOutsideTheConsumer:
    """_drive() switches relays for a mode change and a test end. An OFF that
    fails there is retried once, and a relay that still will not release is
    reported through on_stuck (owl.py raises the relay_stuck fault from it).
    Before this only the consumer path was checked: a nozzle left on by
    'spray off' or by a nozzle test ending was silent."""

    def _blanket(self, timed):
        rc, events = timed(relays=(0,))
        stuck = []
        rc.on_stuck = stuck.append
        return rc, events, stuck

    def test_mode_change_to_off(self, timed):
        rc, events, stuck = self._blanket(timed)
        rc.set_mode('blanket')
        attempts = _refuse_off(rc)
        rc.set_mode('off')
        assert attempts == [0, 0], 'the failed OFF was not retried exactly once'
        assert len(stuck) == 1 and 'Relay 0 is stuck on' in stuck[0], stuck

    def test_stopping_a_nozzle_test(self, timed):
        rc, events, stuck = self._blanket(timed)
        rc.set_mode('off')
        rc.start_test([0], 5)
        attempts = _refuse_off(rc)
        assert rc.stop_test() is True
        assert attempts == [0, 0]
        assert len(stuck) == 1 and 'Relay 0 is stuck on' in stuck[0], stuck

    def test_a_nozzle_test_running_out(self, timed):
        rc, events, stuck = self._blanket(timed)
        rc.set_mode('off')
        ended = threading.Event()
        rc.on_test_end = ended.set
        rc.start_test([0], 0.15)
        attempts = _refuse_off(rc)
        assert ended.wait(1.0), 'the test never ended'
        assert attempts == [0, 0]
        assert len(stuck) == 1 and 'Relay 0 is stuck on' in stuck[0], stuck

    def test_an_off_that_works_on_the_retry_is_not_reported(self, timed):
        rc, events, stuck = self._blanket(timed)
        rc.set_mode('blanket')
        attempts = _refuse_off(rc, failures=1)
        rc.set_mode('off')
        assert attempts == [0, 0]
        assert stuck == []

    def test_a_failed_on_is_not_a_stuck_relay(self, timed):
        rc, events, stuck = self._blanket(timed)

        def broken_on(relay, verbose=True):
            raise RuntimeError('gpio busy')

        rc.relay.relay_on = broken_on
        assert rc.set_mode('blanket') is True
        assert stuck == []
        assert _at(events, 'off') == [], 'a failed ON drove the relay off'


# ---------------------------------------------------------------------------
# RelayController.stop() while it holds relays on itself
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestStopWhileHoldingRelaysOn:
    """stop() switches off a spot window (TestSprayWindowTiming). Relays held
    on by blanket mode or by a nozzle test are not a consumer's, and stop()
    does nothing about them. On a whole Owl this is masked: Owl.stop() calls
    _relays_off_now() first (tests/owl/test_relay_matrix.py
    ::test_shutdown_leaves_every_relay_off)."""

    def test_stop_during_blanket_switches_every_relay_off(self, timed):
        rc, events = timed()
        rc.set_mode('blanket')
        assert len(_at(events, 'on', 0)) == 1 and len(_at(events, 'on', 1)) == 1
        stopped = time.monotonic()
        rc.stop()
        time.sleep(2 * LATE_S)
        for relay in (0, 1):
            offs = _at(events, 'off', relay)
            assert offs and _within(offs[-1], stopped), f'relay {relay} left on: {events}'

    def test_stop_during_a_nozzle_test_switches_the_relay_off(self, timed):
        rc, events = timed()
        rc.set_mode('off')
        rc.start_test([0], 0.3)
        events.clear()
        rc.stop()
        time.sleep(0.3 + 2 * LATE_S)       # past where the test would have ended
        assert [(k, r) for k, r, _ in events] == [('off', 0)], events
        assert rc.test_relays == []


# ---------------------------------------------------------------------------
# The cap on a timed nozzle test, on its own
# ---------------------------------------------------------------------------

class _Clock:
    """utils.output_manager's `time`, with a monotonic clock a test can move
    forward. Everything else is the real module."""

    def __init__(self):
        self.offset = 0.0

    def monotonic(self):
        return time.monotonic() + self.offset

    def __getattr__(self, name):
        return getattr(time, name)


class _Timers:
    """Stands in for threading.Timer in utils.output_manager: records what
    was armed and lets the test fire it."""

    def __init__(self):
        self.armed = []      # [delay, function, cancelled]

    def __call__(self, delay, function):
        entry = [delay, function, False]
        self.armed.append(entry)

        class _Timer:
            daemon = True

            def start(self):
                pass

            def cancel(self):
                entry[2] = True

        return _Timer()

    def live(self):
        return [entry for entry in self.armed if not entry[2]]


@pytest.mark.unit
class TestNozzleTestCap:
    """start_test() called with no max_s (as nothing but the MQTT handler's
    own min() protected until now) ends at TEST_MAX_S however long was asked
    for and however often it is renewed. The clock is moved, not waited on."""

    @pytest.fixture
    def capped(self, timed, monkeypatch):
        clock, timers = _Clock(), _Timers()
        rc, events = timed()
        rc.set_mode('off')
        events.clear()
        monkeypatch.setattr(om, 'time', clock)
        monkeypatch.setattr(om, 'Timer', timers)
        ended = []
        rc.on_test_end = lambda: ended.append(True)
        return rc, events, clock, timers, ended

    def test_a_long_test_is_armed_for_the_cap_and_ends_there(self, capped):
        rc, events, clock, timers, ended = capped
        assert RelayController.TEST_MAX_S == 10.0
        assert rc.start_test([0, 1], 3600) == [0, 1]
        assert rc.test_capped is True
        (delay, expire, _), = timers.live()
        assert delay == pytest.approx(10.0, abs=0.05)

        clock.offset = 9.9
        expire()                                    # a timer firing early re-arms
        assert rc.test_relays == [0, 1] and not ended
        assert _at(events, 'off', 0) == [] and _at(events, 'off', 1) == []
        (delay, expire, _), = timers.live()
        assert delay == pytest.approx(0.1, abs=0.05)

        clock.offset = 10.01
        expire()
        assert rc.test_relays == [] and ended == [True]
        assert [(k, r) for k, r, _ in events][-2:] == [('off', 0), ('off', 1)]

    def test_renewing_cannot_push_a_capped_test_past_the_cap(self, capped):
        rc, events, clock, timers, ended = capped
        rc.start_test([0], 2)
        clock.offset = 9.0
        assert rc.renew_test(60) is True
        (delay, expire, _), = timers.live()
        assert delay == pytest.approx(1.0, abs=0.05), 'a renew moved the end past TEST_MAX_S'
        clock.offset = 10.01
        expire()
        assert rc.test_relays == [] and ended == [True]

    def test_a_short_test_ends_at_its_own_duration(self, capped):
        rc, events, clock, timers, ended = capped
        rc.start_test([0], 2)
        (delay, expire, _), = timers.live()
        assert delay == pytest.approx(2.0, abs=0.05)
        clock.offset = 2.01
        expire()
        assert rc.test_relays == [] and ended == [True]

    def test_hold_to_spray_has_no_cap(self, capped):
        rc, events, clock, timers, ended = capped
        rc.start_test([0], 1, max_s=None)
        assert rc.test_capped is False
        clock.offset = 3600.0
        assert rc.renew_test(1) is True
        (delay, expire, _), = timers.live()
        assert delay == pytest.approx(1.0, abs=0.05)
        assert rc.test_relays == [0] and not ended
