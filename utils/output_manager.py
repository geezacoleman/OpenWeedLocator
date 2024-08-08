from utils.logger import Logger
from threading import Thread, Event, Condition
from utils.cli_vis import RelayVis
from enum import Enum
from collections import deque
from threading import Lock

import shutil
import time
import os



import platform
import warnings
# check if the system is being tested on a Windows or Linux x86 64 bit machine
if 'rpi' in platform.platform():
    testing = False
    from gpiozero import Buzzer, OutputDevice, LED

elif platform.system() == "Windows":
    warning_message = "[WARNING] The system is running on a Windows platform. GPIO disabled. Test mode active."
    warnings.warn(warning_message, RuntimeWarning)
    testing = True
    testing = True

elif 'aarch' in platform.platform():
    testing = False
    from gpiozero import Buzzer, OutputDevice, LED

else:
    warning_message = "[WARNING] The system is not running on a recognized platform. GPIO disabled. Test mode active."
    warnings.warn(warning_message, RuntimeWarning)
    testing = True

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
    def __init__(self, pin):
        self.pin = pin

    def blink(self, on_time=0.1, off_time=0.1, n=1, verbose=False, background=True):
        if n is None:
            n = 1

        for i in range(n):
            if verbose:
                print(f'BLINK {self.pin}')

    def on(self):
        print(f'LED {self.pin} ON')

    def off(self):
        print(f'LED {self.pin} OFF')


class BaseStatusIndicator:
    def __init__(self, save_directory, testing=False):
        self.save_directory = save_directory
        self.testing = testing
        self.storage_used = None
        self.storage_total = None
        self.update_event = Event()
        self.running = True
        self.thread = None
        self.DRIVE_FULL = False

    def start_storage_indicator(self):
        self.thread = Thread(target=self.run_update)
        self.thread.start()

    def run_update(self):
        while self.running:
            self.update()
            self.update_event.wait(10.5)
            self.update_event.clear()

    def update(self):
        self.storage_total, self.storage_used, _ = shutil.disk_usage(self.save_directory)
        percent_full = (self.storage_used / self.storage_total)
        self._update_storage_indicator(percent_full)

    def _update_storage_indicator(self, percent_full):
        raise NotImplementedError("This method should be implemented by subclasses")

    def stop(self):
        self.update_event.set()
        self.running = False
        self.thread.join()

class UteStatusIndicator(BaseStatusIndicator):
    def __init__(self, save_directory, record_led_pin='BOARD38', storage_led_pin='BOARD40', testing=False):
        super().__init__(save_directory, testing)
        LED_class = LED if not testing else TestLED
        self.record_LED = LED_class(pin=record_led_pin)
        self.storage_LED = LED_class(pin=storage_led_pin)

    def _update_storage_indicator(self, percent_full):
        if percent_full >= 0.90:
            self.DRIVE_FULL = True
            self.storage_LED.on()
            self.record_LED.off()
        elif percent_full >= 0.85:
            self.storage_LED.blink(on_time=0.2, off_time=0.2, n=None, background=True)
        elif percent_full >= 0.80:
            self.storage_LED.blink(on_time=0.5, off_time=0.5, n=None, background=True)
        elif percent_full >= 0.75:
            self.storage_LED.blink(on_time=0.5, off_time=1.5, n=None, background=True)
        elif percent_full >= 0.5:
            self.storage_LED.blink(on_time=0.5, off_time=3.0, n=None, background=True)
        else:
            self.storage_LED.blink(on_time=0.5, off_time=4.5, n=None, background=True)

    def setup_success(self):
        self.storage_LED.blink(on_time=0.1, off_time=0.2, n=3)
        self.record_LED.blink(on_time=0.1, off_time=0.2, n=3)

    def image_write_indicator(self):
        self.record_LED.blink(on_time=0.1, n=1, background=True)

    def alert_flash(self):
        self.storage_LED.blink(on_time=0.5, off_time=0.5, n=None, background=True)
        self.record_LED.blink(on_time=0.5, off_time=0.5, n=None, background=True)

    def stop(self):
        super().stop()
        self.storage_LED.off()
        self.record_LED.off()


class AdvancedIndicatorState(Enum):
    IDLE = 0
    RECORDING = 1
    DETECTING = 2
    RECORDING_AND_DETECTING = 3
    ERROR = 4

class AdvancedStatusIndicator(BaseStatusIndicator):
    def __init__(self, save_directory, status_led_pin='BOARD37', testing=False):
        super().__init__(save_directory, testing)
        LED_class = LED if not testing else TestLED
        self.led = LED_class(pin=status_led_pin)
        self.state = AdvancedIndicatorState.IDLE
        self.error_queue = deque()
        self.state_lock = Lock()
        self.weed_detection_enabled = False
        self.image_recording_enabled = False

    def _update_storage_indicator(self, percent_full):
        if percent_full >= 0.90:
            self.DRIVE_FULL = True
            self.error(1)  # Use error code 1 for drive full

    def setup_success(self):
        self.led.blink(on_time=0.1, off_time=0.2, n=3)

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
            if self.state not in [AdvancedIndicatorState.ERROR, AdvancedIndicatorState.DETECTING, AdvancedIndicatorState.RECORDING_AND_DETECTING]:
                self.led.blink(on_time=0.1, off_time=0.1, n=1, background=True)

    def weed_detect_indicator(self):
        with self.state_lock:
            if self.state in [AdvancedIndicatorState.DETECTING, AdvancedIndicatorState.RECORDING_AND_DETECTING]:
                self.led.blink(on_time=0.05, off_time=0.05, n=1, background=True)

    def error(self, error_code):
        with self.state_lock:
            self.state = AdvancedIndicatorState.ERROR
            self.error_queue.append(error_code)
        self._process_error_queue()

    def _process_error_queue(self):
        with self.state_lock:
            if self.error_queue:
                error_code = self.error_queue.popleft()
                self.led.blink(on_time=0.2, off_time=0.2, n=error_code*2, background=False)
                self.led.off()
                if self.error_queue:
                    self.led.blink(on_time=0.5, off_time=0.5, n=1, background=False)
                    self._process_error_queue()
                else:
                    self._update_state()
                    self.led.off()

    def stop(self):
        super().stop()
        self.led.off()

# control class for the relay board
class RelayControl:
    def __init__(self, relay_dict):
        self.testing = True if testing else False
        self.relay_dict = relay_dict
        self.on = False

        # used to toggle activation of GPIO pins for LEDs
        self.field_data_recording = False

        if not self.testing:
            self.buzzer = Buzzer(pin='BOARD7')

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

# this class does the hard work of receiving detection 'jobs' and queuing them to be actuated. It only turns a nozzle on
# if the sprayDur has not elapsed or if the nozzle isn't already on.
class RelayController:
    def __init__(self, relay_dict, vis=False, status_led=None):
        self.relay_dict = relay_dict
        self.vis = vis
        self.status_led = status_led
        # instantiate relay control with supplied relay dictionary to map to correct board pins
        self.relay = RelayControl(self.relay_dict)
        self.relay_queue_dict = {}
        self.relay_condition_dict = {}

        # start the logger and log file using absolute path of python file
        self.save_dir = os.path.join(os.path.dirname(__file__), 'logs')
        self.logger = Logger(name="weed_log.txt", saveDir=self.save_dir)

        # create a job queue and Condition() for each nozzle
        print("[INFO] Setting up nozzles...")
        self.relay_vis = RelayVis(relays=len(self.relay_dict.keys()))
        for relay_number in range(0, len(self.relay_dict)):
            self.relay_queue_dict[relay_number] = deque(maxlen=5)
            self.relay_condition_dict[relay_number] = Condition()

            # create the consumer threads, setDaemon and start the threads.
            relay_thread = Thread(target=self.consumer, args=[relay_number])
            relay_thread.setDaemon(True)
            relay_thread.start()

        time.sleep(1)
        print("[INFO] Nozzle setup complete. Initiating camera...")
        self.relay.beep(duration=0.5)

    def receive(self, relay, time_stamp, location=0, delay=0, duration=1):
        """
        this method adds a new job to specified relay queue. GPS location data etc to be added. Time stamped
        records the true time of weed detection from main thread, which is compared to time of relay activation for accurate
        on durations. There will be a minimum on duration of this processing speed ~ 0.3s. Will default to 0 though.
        :param relay: relay id (zero based)
        :param time_stamp: this is the time of detection
        :param location: GPS functionality to be added here
        :param delay: on delay to be added in the future
        :param duration: duration of spray
        """
        input_queue_message = [relay, time_stamp, delay, duration]
        input_queue = self.relay_queue_dict[relay]
        input_condition = self.relay_condition_dict[relay]
        # notifies the consumer thread when something has been added to the queue
        with input_condition:
            input_queue.append(input_queue_message)
            input_condition.notify()

    def consumer(self, relay):
        """
        Takes only one parameter - nozzle, which enables the selection of the deque, condition from the dictionaries.
        The consumer method is threaded for each nozzle and will wait until it is notified that a new job has been added
        from the receive method. It will then compare the time of detection with time of spraying to activate that nozzle
        for required length of time.
        :param relay: relay id number
        """
        self.running = True
        input_condition = self.relay_condition_dict[relay]
        input_condition.acquire()
        relay_on = False
        relay_queue = self.relay_queue_dict[relay]

        while self.running:
            while relay_queue:
                job = relay_queue.popleft()
                input_condition.release()
                # check to make sure time is positive
                onDur = 0 if (job[3] - (time.time() - job[1])) <= 0 else (job[3] - (time.time() - job[1]))

                if not relay_on:
                    time.sleep(job[2]) # add in the delay variable
                    self.relay.relay_on(relay, verbose=False)
                    if self.status_led:
                        self.status_led.blink(on_time=0.1, n=1, background=True)

                    if self.vis:
                        self.relay_vis.update(relay=relay, status=True)

                    relay_on = True

                try:
                    time.sleep(onDur)

                except ValueError:
                    time.sleep(0)
                    self.logger.log_line(f'[ERROR] negative onDur {onDur} for nozzle {relay} received. Turning on for 0 seconds.')

                input_condition.acquire()

            if len(relay_queue) == 0:
                self.relay.relay_off(relay, verbose=False)

                if self.vis:
                    self.relay_vis.update(relay=relay, status=False)
                relay_on = False

            input_condition.wait()
