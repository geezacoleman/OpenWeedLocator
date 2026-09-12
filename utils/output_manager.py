from threading import Thread, Event, Condition, Lock
from utils.vis_manager import RelayVis
from utils.error_manager import OWLAlreadyRunningError
from utils.log_manager import LogManager
from enum import Enum
from collections import deque
from typing import Optional

import os
import subprocess
import shutil
import time
import logging
import platform

logger = logging.getLogger(__name__)

def get_platform_config() -> tuple[bool, Optional[Exception]]:
    """Determine platform and return testing status and lgpio error type"""
    system_platform = platform.platform().lower()
    is_raspberry_pi = 'rpi' in system_platform or 'aarch' in system_platform

    if is_raspberry_pi:
        from gpiozero import Buzzer, OutputDevice, LED
        import lgpio
        return False, lgpio.error

    is_windows = platform.system() == "Windows"
    system_name = "Windows" if is_windows else "unrecognized"
    logger.warning(
        f"The system is running on a {system_name} platform. GPIO disabled. Test mode active."
    )
    return True, None

testing, lgpioERROR = get_platform_config()

# Import GPIO components only if needed
if not testing:
    from gpiozero import Buzzer, OutputDevice, LED

# two test classes to run the analysis on a desktop computer if a "win32" platform is detected
class TestRelay:
    def __init__(self, relay_number, verbose=False):
        self.relay_number = relay_number
        self.verbose = verbose

    def on(self):
        if self.verbose:
            print(f"[TEST] Relay {self.relay_number} ON")

    def off(self):
        if self.verbose:
            print(f"[TEST] Relay {self.relay_number} OFF")

class TestBuzzer:
    def beep(self, on_time, off_time, n=1, verbose=False):
        for i in range(n):
            if verbose:
                print('BEEP')

class TestLED:
    """Stand-in for gpiozero.LED when no GPIO is available.

    Mirrors the gpiozero API closely enough for the status/GPS indicator
    threads to run unchanged on a laptop:
      * state changes are logged at DEBUG, never printed - the GPS LED thread
        drives off() twice a second and printing each call buried the real
        console output;
      * background=False blinks block for the blink duration, as gpiozero
        does, so indicator threads sleep instead of spinning the CPU.
    """

    def __init__(self, pin):
        self.pin = pin
        self.is_lit = False

    def blink(self, on_time=0.1, off_time=0.1, n=1, verbose=False, background=True):
        if n is None:
            n = 1

        if verbose:
            for _ in range(n):
                print(f'BLINK {self.pin}')

        if not background:
            time.sleep(n * ((on_time or 0) + (off_time or 0)))

    def on(self):
        if not self.is_lit:
            self.is_lit = True
            logger.debug(f'[TEST] LED {self.pin} ON')

    def off(self):
        if self.is_lit:
            self.is_lit = False
            logger.debug(f'[TEST] LED {self.pin} OFF')


class BaseStatusIndicator:
    GB = 1024 ** 3
    LED_PATHS = {
        "ACT": "/sys/class/leds/ACT/brightness",
        "PWR": "/sys/class/leds/PWR/brightness"
    }
    LED_TRIGGER_PATHS = {
        "ACT": "/sys/class/leds/ACT/trigger",
        "PWR": "/sys/class/leds/PWR/trigger"
    }

    def __init__(self, save_directory, no_save=False):
        self.logger = LogManager.get_logger(__name__)

        self.save_directory = save_directory
        self.no_save = no_save
        self.testing = True if testing else False
        self.storage_used = None
        self.storage_total = None
        self.storage_free = None
        # usb: percent rule (full at >=90%). internal: absolute free-space
        # floor (min_free_gb) — a 90% rule on a 16 GB eMMC would allow free
        # space below the floor. owl.py assigns these after config load.
        self.storage_location = 'usb'
        self.min_free_gb = 4
        self.storage_warning = 'ok'   # ok | low | full
        self.update_event = Event()
        self.running = True
        self.thread = None
        self.DRIVE_FULL = False

        self.error_code = None
        self.flashing_thread = None
        self.flash_event = Event()
        self._update_failed = False
        self.leds_enabled = self._probe_led_control()
        self._set_led_trigger("ACT", "none")
        self._set_led_trigger("PWR", "none")

    def _probe_led_control(self):
        """One-time capability probe for board LED control (sysfs + passwordless sudo).

        Units without either — sealed CM5 enclosures, desktops, Pis without a
        sudoers entry — run with LED signalling disabled as a healthy state:
        errors still reach the dashboard/app via MQTT state, and exactly one
        warning is logged instead of a failing subprocess per blink.
        """
        if self.testing:
            return False
        if not all(os.path.exists(path) for path in self.LED_PATHS.values()):
            self.logger.warning("LED signalling disabled: status LED sysfs paths not found.")
            return False
        try:
            result = subprocess.run(['sudo', '-n', 'true'], capture_output=True)
        except OSError:
            self.logger.warning("LED signalling disabled: sudo not available.")
            return False
        if result.returncode != 0:
            self.logger.warning("LED signalling disabled: passwordless sudo unavailable.")
            return False
        return True

    def start_storage_indicator(self):
        self.thread = Thread(target=self.run_update)
        self.thread.start()

    def run_update(self):
        while self.running:
            try:
                self.update()
                if self._update_failed:
                    self._update_failed = False
                    self.logger.info("Storage monitoring recovered.")
            except OSError as e:
                # A yanked drive must never kill this thread — it is the
                # storage watchdog. Log once per failure transition only.
                if not self._update_failed:
                    self._update_failed = True
                    self.logger.warning(f"Storage monitoring error (drive removed?): {e}")
            self.update_event.wait(10.5)
            self.update_event.clear()

    def update(self):
        if self.save_directory is not None:
            self.storage_total, self.storage_used, self.storage_free = \
                shutil.disk_usage(self.save_directory)
            if self.storage_location == 'internal':
                self._update_internal_storage(self.storage_free)
            else:
                percent_full = (self.storage_used / self.storage_total)
                self._update_storage_indicator(percent_full)
                self._set_storage_warning(
                    'full' if self.DRIVE_FULL else
                    'low' if percent_full >= 0.85 else 'ok')
            if self.error_code == 6:
                # a writable drive is back — the no-drive error no longer applies
                self.clear_error()

        elif self.no_save:
            pass

        else:
            # No storage resolved. Error 6 is the "insert a USB drive" signal —
            # only honest in usb mode. internal/auto landing here means the
            # internal path itself failed (floor/permissions): a storage
            # error (code 5), not a missing drive.
            self.error(6 if self.storage_location == 'usb' else 5)

    def _update_internal_storage(self, free_bytes):
        """Internal (eMMC) mode: absolute free-space floor, not a percent.
        Breach latches DRIVE_FULL — the main loop's snap-off machinery does
        the rest. Recovery (sessions deleted) clears via clear_error()."""
        floor = self.min_free_gb * self.GB
        if free_bytes < floor:
            self.DRIVE_FULL = True
            self._set_storage_warning('full')
        else:
            if self.DRIVE_FULL:
                self.clear_error()
            self._set_storage_warning(
                'low' if free_bytes < floor + 2 * self.GB else 'ok')

    def _set_storage_warning(self, level):
        """Edge-triggered: one log line per state transition only."""
        if level == self.storage_warning:
            return
        previous = self.storage_warning
        self.storage_warning = level
        free_gb = (self.storage_free or 0) / self.GB
        if level == 'full':
            self.logger.warning(
                f"Storage full ({free_gb:.1f} GB free): recording unavailable "
                f"until space is freed.")
        elif level == 'low':
            self.logger.warning(f"Storage low: {free_gb:.1f} GB free.")
        elif previous != 'ok':
            self.logger.info(f"Storage recovered: {free_gb:.1f} GB free.")

    def error(self, error_code):
        self.error_code = error_code
        if not self.leds_enabled:
            return
        self.flash_event.clear()
        if self.flashing_thread is None or not self.flashing_thread.is_alive():
            self.flashing_thread = Thread(target=self._flash_error_code)
            self.flashing_thread.start()

    def clear_error(self):
        """Stop error flashing and unlatch DRIVE_FULL once the condition recovers."""
        self.error_code = None
        self.DRIVE_FULL = False
        self.flash_event.set()

    def _flash_error_code(self):
        while self.running and self.error_code is not None:
            code = self.error_code
            for _ in range(code):
                if not self.running or self.error_code is None:
                    return
                self._blink_leds()
                self.flash_event.wait(0.2)  # Interval between flashes
            self.flash_event.wait(2)  # Pause after each sequence

    def _blink_leds(self):
        self._set_led_state("ACT", 1)
        self._set_led_state("PWR", 1)
        time.sleep(0.2)
        self._set_led_state("ACT", 0)
        self._set_led_state("PWR", 0)

    def _set_led_state(self, led, state):
        if self.testing or not self.leds_enabled:
            return
        try:
            subprocess.run(
                ['sudo', 'sh', '-c', f'echo {1 if state else 0} > {self.LED_PATHS[led]}'],
                check=True
            )
        except subprocess.CalledProcessError as e:
            self.logger.error(msg=f"Error: Could not set {led} LED. {e}", exc_info=True)

    # Method to set LED trigger to 'none' to ensure manual control.
    # Based on: https://howtoraspberrypi.com/controler-led-verte-raspberry-pi-2/
    def _set_led_trigger(self, led, trigger):
        if self.testing or not self.leds_enabled:
            return
        try:
            subprocess.run(
                ['sudo', 'sh', '-c', f'echo {trigger} > {self.LED_TRIGGER_PATHS[led]}'],
                check=True
            )
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Error: Could not set {led} trigger to {trigger}.", exc_info=True)

    def _update_storage_indicator(self, percent_full):
        self.logger.warning("Called _update_storage_indicator() but it's not implemented.")
        raise NotImplementedError("This method should be implemented by subclasses")

    def stop(self):
        """Stop all threads and ensure resources are cleaned up."""
        self.running = False
        self.update_event.set()  # Wake up storage indicator thread
        self.flash_event.set()  # Wake up flashing thread so it exits promptly

        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1)  # Ensure thread stops

        if self.flashing_thread and self.flashing_thread.is_alive():
            self.flashing_thread.join(timeout=1)  # Ensure flashing thread stops

        self._cleanup_leds()
        logger.info("[INFO] StatusIndicator stopped.")

    def _cleanup_leds(self):
        """Turn off LEDs and reset their states."""
        try:
            self._set_led_state("ACT", 0)
            self._set_led_state("PWR", 0)
        except Exception as e:
            logger.error(f"Failed to clean up LEDs: {e}")


class HeadlessStatusIndicator(BaseStatusIndicator):
    def __init__(self, save_directory=None, no_save=False):
        super().__init__(save_directory, no_save)

    def _update_storage_indicator(self, percent_full):
        if percent_full >= 0.90:
            self.DRIVE_FULL = True
        elif self.DRIVE_FULL:
            # space was freed (e.g. sessions deleted) — recording may resume
            self.clear_error()


# UteStatusIndicator is defined after AdvancedStatusIndicator (below)
# as it inherits from it. See line ~343.


class AdvancedIndicatorState(Enum):
    IDLE = 0
    RECORDING = 1
    DETECTING = 2
    NOTIFICATION = 3
    RECORDING_AND_DETECTING = 4
    ERROR = 5


class AdvancedStatusIndicator(BaseStatusIndicator):
    def __init__(self, save_directory, status_led_pin='BOARD40'):
        super().__init__(save_directory)
        if status_led_pin is None:
            # No physical LED — use no-op stub
            self.led = TestLED(pin='disabled')
        else:
            LED_class = LED if not testing else TestLED
            self.led = LED_class(pin=status_led_pin)
        self.state = AdvancedIndicatorState.IDLE
        self.error_queue = deque()
        self.state_lock = Lock()
        self.weed_detection_enabled = False
        self.image_recording_enabled = False
        self.flashing_thread = None

    def _update_storage_indicator(self, percent_full):
        if percent_full >= 0.90:
            self.DRIVE_FULL = True
            self.error(1)  # Use error code 1 for drive full
        elif self.DRIVE_FULL:
            # space was freed (e.g. sessions deleted) — recording may resume
            self.clear_error()

    def setup_success(self):
        self.led.blink(on_time=0.1, off_time=0.1, n=2)

    def _update_state(self):
        if self.state != AdvancedIndicatorState.ERROR:
            if self.weed_detection_enabled and self.image_recording_enabled:
                self.state = AdvancedIndicatorState.RECORDING_AND_DETECTING
            elif self.weed_detection_enabled:
                self.state = AdvancedIndicatorState.DETECTING
            elif self.image_recording_enabled:
                self.state = AdvancedIndicatorState.RECORDING
            else:
                self.state = AdvancedIndicatorState.IDLE

    def enable_weed_detection(self):
        with self.state_lock:
            self.weed_detection_enabled = True
            self._update_state()

    def disable_weed_detection(self):
        with self.state_lock:
            self.weed_detection_enabled = False
            self._update_state()

    def enable_image_recording(self):
        with self.state_lock:
            self.image_recording_enabled = True
            self._update_state()

    def disable_image_recording(self):
        with self.state_lock:
            self.image_recording_enabled = False
            self._update_state()

    def image_write_indicator(self):
        with self.state_lock:
            if self.state not in [AdvancedIndicatorState.ERROR, AdvancedIndicatorState.DETECTING,
                                  AdvancedIndicatorState.RECORDING_AND_DETECTING]:
                try:
                    self.led.blink(on_time=0.1, off_time=0.1, n=1, background=True)
                except KeyboardInterrupt:
                    logger.info("[INFO] KeyboardInterrupt received during image_write_indicator. Turning off LED.")
                    self.led.off()
                    raise
                except Exception as e:
                    logger.error(f"Error in image_write_indicator: {e}", exc_info=True)

    def weed_detect_indicator(self):
        with self.state_lock:
            if self.state in [AdvancedIndicatorState.DETECTING, AdvancedIndicatorState.RECORDING_AND_DETECTING]:
                try:
                    self.led.blink(on_time=0.05, off_time=0.05, n=1, background=True)
                except KeyboardInterrupt:
                    logger.info("[INFO] KeyboardInterrupt received during weed_detect_indicator. Turning off LED.")
                    self.led.off()
                    raise
                except Exception as e:
                    logger.error(f"Error in weed_detect_indicator: {e}", exc_info=True)

    def generic_notification(self):
        try:
            with self.state_lock:
                init_state = self.state
                self.state = AdvancedIndicatorState.NOTIFICATION
                self.led.off()  # Reset LED state before notification

                self.led.blink(on_time=0.1, off_time=0.1, n=2, background=False)
                self.state = init_state
        except KeyboardInterrupt:
            logger.info("[INFO] KeyboardInterrupt received during generic_notification. Turning off LED.")
            self.led.off()
            raise
        except Exception as e:
            logger.error(f"Error in generic_notification: {e}", exc_info=True)

    def error(self, error_code):
        self.error_code = error_code
        with self.state_lock:
            self.state = AdvancedIndicatorState.ERROR
        if not self.leds_enabled:
            return
        self.flash_event.clear()
        if self.flashing_thread is None or not self.flashing_thread.is_alive():
            self.flashing_thread = Thread(target=self._flash_error_code)
            self.flashing_thread.start()

    def clear_error(self):
        super().clear_error()
        with self.state_lock:
            if self.state == AdvancedIndicatorState.ERROR:
                self.state = AdvancedIndicatorState.IDLE
                self._update_state()

    def _flash_error_code(self):
        try:
            while self.running and self.error_code is not None:
                code = self.error_code
                for _ in range(code):
                    if not self.running or self.error_code is None:
                        return
                    self._blink_leds()
                    self.flash_event.wait(0.2)
                self.flash_event.wait(2)
        except KeyboardInterrupt:
            logger.info("[INFO] KeyboardInterrupt received in _flash_error_code. Exiting.")
        except Exception as e:
            logger.error(f"Error in _flash_error_code: {e}", exc_info=True)
        finally:
            self._cleanup_leds()

    def stop(self):
        super().stop()
        if self.flashing_thread and self.flashing_thread.is_alive():
            self.flashing_thread.join(timeout=3)
        self.led.off()


# Re-define UteStatusIndicator as a thin wrapper around AdvancedStatusIndicator.
# Both now use a single status LED — UteStatusIndicator just changes the default pin.
class UteStatusIndicator(AdvancedStatusIndicator):
    def __init__(self, save_directory, status_led_pin='BOARD40'):
        super().__init__(save_directory, status_led_pin=status_led_pin)


class GPSLEDState(Enum):
    OFF = 0
    ACQUIRING = 1
    FIX = 2
    ERROR = 3


class GPSStatusLED:
    """Standalone GPS status LED indicator with its own daemon thread.

    LED patterns:
        OFF       — GPS disabled
        ACQUIRING — regular flashing (data received but no fix)
        FIX       — solid ON (valid GPS lock)
        ERROR     — double short flash + pause (serial failed / no data)
    """

    def __init__(self, pin='BOARD38'):
        LED_class = LED if not testing else TestLED
        self.led = LED_class(pin=pin)
        self._state = GPSLEDState.OFF
        self._lock = Lock()
        self._running = True
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_state(self, state):
        with self._lock:
            self._state = state

    def _run(self):
        while self._running:
            with self._lock:
                state = self._state

            if state == GPSLEDState.OFF:
                self.led.off()
                time.sleep(0.5)
            elif state == GPSLEDState.FIX:
                self.led.on()
                time.sleep(0.5)
            elif state == GPSLEDState.ACQUIRING:
                self.led.blink(on_time=0.15, off_time=0.85, n=1, background=False)
            elif state == GPSLEDState.ERROR:
                # Double short flash then pause
                self.led.blink(on_time=0.1, off_time=0.1, n=2, background=False)
                time.sleep(1.5)

    def stop(self):
        self._running = False
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        self.led.off()


# control class for the relay board
class RelayControl:
    def __init__(self, relay_dict):
        self.logger = LogManager.get_logger(__name__)

        self.testing = True if testing else False
        self.relay_dict = relay_dict
        self.on = False

        # used to toggle activation of GPIO pins for LEDs
        self.field_data_recording = False

        if not self.testing:
            # Skip buzzer if BOARD7 (GPIO4) is needed by a relay
            buzzer_pin_used = 7 in self.relay_dict.values()
            if buzzer_pin_used:
                logger.warning("Buzzer pin (BOARD7/GPIO4) allocated to relay; buzzer disabled")
                self.buzzer = TestBuzzer()
            else:
                try:
                    self.buzzer = Buzzer(pin='BOARD7')
                except Exception as e:
                    if isinstance(e, lgpioERROR) and 'GPIO busy' in str(e):
                        raise OWLAlreadyRunningError("OWL instance may already be running.") from e
                    else:
                        raise

            for relay, board_pin in self.relay_dict.items():
                self.relay_dict[relay] = OutputDevice(pin=f'BOARD{board_pin}')

        else:
            self.buzzer = TestBuzzer()
            for relay, board_pin in self.relay_dict.items():
                self.relay_dict[relay] = TestRelay(board_pin)

    def relay_on(self, relay_number, verbose=True):
        relay = self.relay_dict[relay_number]
        relay.on()

        if verbose:
            print(f"Relay {relay_number} ON")

    def relay_off(self, relay_number, verbose=True):
        relay = self.relay_dict[relay_number]
        relay.off()

        if verbose:
            print(f"Relay {relay_number} OFF")

    def beep(self, duration=0.2, repeats=2):
        self.buzzer.beep(on_time=duration, off_time=(duration / 2), n=repeats)

    def all_on(self, verbose=False):
        for relay in self.relay_dict.keys():
            self.relay_on(relay, verbose=verbose)

    def all_off(self, verbose=False):
        for relay in self.relay_dict.keys():
            self.relay_off(relay, verbose=verbose)

    def remove(self, relay_number):
        self.relay_dict.pop(relay_number, None)

    def clear(self):
        self.relay_dict = {}

    def stop(self):
        self.clear()
        self.all_off()

# this class does the hard work of receiving detection 'jobs' and scheduling the actuation. Each detection opens a
# spray window from its detection timestamp; overlapping windows from repeated detections of the same weed/patch merge
# into one continuous activation, so the nozzle is only switched at the true start and end of spraying.
class RelayController:
    def __init__(self, relay_dict, vis=False, status_led=None):
        self.logger = LogManager.get_logger(__name__)

        self.relay_dict = relay_dict
        self.vis = vis
        self.status_led = status_led
        # instantiate relay control with supplied relay dictionary to map to correct board pins
        try:
            self.relay = RelayControl(self.relay_dict)
        except OWLAlreadyRunningError:
            self.logger.error("Failed to initialize RelayControl: OWL is already running and using GPIO pin 7.")
            raise
        self.relay_schedule_dict = {}
        self.relay_condition_dict = {}
        self.running = True

        # create a spray-window schedule and Condition() for each nozzle
        self.logger.info("[INFO] Setting up nozzles...")
        self.relay_vis = RelayVis(relays=len(self.relay_dict.keys()))
        for relay_number in range(0, len(self.relay_dict)):
            self.relay_schedule_dict[relay_number] = {'on_at': None, 'off_at': None}
            self.relay_condition_dict[relay_number] = Condition()

            # create the consumer threads, setDaemon and start the threads.
            relay_thread = Thread(target=self.consumer, args=[relay_number])
            relay_thread.setDaemon(True)
            relay_thread.start()

        time.sleep(1)
        self.logger.info("[INFO] Nozzle setup complete. Initiating camera...")
        self.relay.beep(duration=0.5)

    def receive(self, relay, time_stamp, location=0, delay=0, duration=1):
        """
        Schedule a spray window for a relay. The window runs from time_stamp + delay
        until time_stamp + delay + duration, anchored to the true detection time from
        the main thread so processing lag never stretches the spray.

        If a window is already open or pending, the new one merges into it: on_at
        never moves later and off_at only ever extends. A continuously detected weed
        therefore holds the nozzle on without off/on cycling, and a live drop in
        `duration` (e.g. a GPS speed-adaptive update) can never cut short spray that
        was already promised.
        :param relay: relay id (zero based)
        :param time_stamp: this is the time of detection
        :param location: GPS functionality to be added here
        :param delay: seconds between detection and the ground reaching the nozzle
        :param duration: duration of spray
        """
        schedule = self.relay_schedule_dict[relay]
        condition = self.relay_condition_dict[relay]
        on_at = time_stamp + delay
        off_at = on_at + duration

        with condition:
            now = time.time()
            if off_at <= now:
                # window already in the past (stale backlog) — nothing to spray
                return
            if schedule['off_at'] is None or schedule['off_at'] <= now:
                # nozzle idle: open a fresh window
                schedule['on_at'] = on_at
                schedule['off_at'] = off_at
            else:
                # window open or pending: merge
                schedule['on_at'] = min(schedule['on_at'], on_at)
                schedule['off_at'] = max(schedule['off_at'], off_at)
            condition.notify()

    def _set_relay(self, relay, on):
        if on:
            self.relay.relay_on(relay, verbose=False)
            if self.status_led:
                self.status_led.blink(on_time=0.1, n=1, background=True)
        else:
            self.relay.relay_off(relay, verbose=False)

        if self.vis:
            self.relay_vis.update(relay=relay, status=on)

    def consumer(self, relay):
        """
        Takes only one parameter - relay, which selects the schedule and condition from the dictionaries.
        The consumer method is threaded per nozzle and sleeps on its Condition until receive() opens or
        extends that nozzle's spray window. It switches the relay on while now is inside the window and
        off once off_at passes; waits use a timeout so an extension arriving mid-window simply re-arms
        the deadline instead of cycling the relay.
        :param relay: relay id number
        """
        condition = self.relay_condition_dict[relay]
        schedule = self.relay_schedule_dict[relay]
        relay_on = False

        with condition:
            while self.running:
                now = time.time()
                off_at = schedule['off_at']

                if off_at is None or off_at <= now:
                    # no window, or window just closed
                    if relay_on:
                        self._set_relay(relay, on=False)
                        relay_on = False
                    schedule['on_at'] = None
                    schedule['off_at'] = None
                    condition.wait()
                    continue

                on_at = schedule['on_at']
                if on_at is not None and on_at > now:
                    # window scheduled but the camera-to-nozzle delay hasn't elapsed
                    if relay_on:
                        self._set_relay(relay, on=False)
                        relay_on = False
                    condition.wait(timeout=on_at - now)
                    continue

                if not relay_on:
                    self._set_relay(relay, on=True)
                    relay_on = True
                condition.wait(timeout=off_at - now)

            # never leave a nozzle on after shutdown
            if relay_on:
                self._set_relay(relay, on=False)

    def stop(self):
        self.running = False
        for condition in self.relay_condition_dict.values():
            with condition:
                condition.notify_all()


if __name__ == "__main__":
    print("Starting test of status indicators...")

    # Test HeadlessStatusIndicator
    print("\nTesting HeadlessStatusIndicator...")
    headless_indicator = HeadlessStatusIndicator(save_directory="output")
    headless_indicator.show_error(3)  # Show an error with 3 flashes
    headless_indicator.stop()

    # Test UteStatusIndicator (single LED, inherits AdvancedStatusIndicator)
    print("\nTesting UteStatusIndicator...")
    ute_indicator = UteStatusIndicator(save_directory="output", status_led_pin='BOARD40')
    ute_indicator.error(4)
    ute_indicator.stop()

    # Test AdvancedStatusIndicator
    print("\nTesting AdvancedStatusIndicator...")
    advanced_indicator = AdvancedStatusIndicator(save_directory="output", status_led_pin='BOARD40')
    advanced_indicator.error(2)
    advanced_indicator.stop()

    # Test GPSStatusLED
    print("\nTesting GPSStatusLED...")
    gps_led = GPSStatusLED(pin='BOARD38')
    gps_led.set_state(GPSLEDState.ACQUIRING)
    time.sleep(2)
    gps_led.set_state(GPSLEDState.FIX)
    time.sleep(2)
    gps_led.set_state(GPSLEDState.ERROR)
    time.sleep(2)
    gps_led.stop()

    print("\nTest complete.")