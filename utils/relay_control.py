import sys

from utils.logger import Logger
from threading import Thread, Event, Condition
from utils.cli_vis import RelayVis
from datetime import datetime
import collections
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


class StatusIndicator:
    def __init__(self, save_directory, record_led_boardpin='BOARD38', storage_led_boardpin='BOARD40'):
        self.testing = True if testing else False
        self.save_directory = save_directory
        self.save_subdirectory = None
        self.storage_used = None
        self.storage_total = None
        self.update_event = Event()

        self.running = True
        self.thread = None

        self.DRIVE_FULL = False

        if not testing:
            self.record_LED = LED(pin=record_led_boardpin)
            self.storage_LED = LED(pin=storage_led_boardpin)

        else:
            self.record_LED = TestLED(pin=record_led_boardpin)
            self.storage_LED = TestLED(pin=storage_led_boardpin)

    def setup_directories(self, enable_device_save=False):
        self.save_subdirectory = os.path.join(self.save_directory, datetime.now().strftime('%Y%m%d'))

        try:
            if os.path.ismount(self.save_directory):
                os.makedirs(self.save_subdirectory, exist_ok=True)

            elif enable_device_save:
                os.makedirs(self.save_subdirectory, exist_ok=True)

            else:
                os.makedirs(self.save_subdirectory, exist_ok=True)

            self.setup_success()

        except PermissionError:
            try:
                username = os.listdir('/media/')[0]
                usb_drives = os.listdir(os.path.join('/media', username))
                for drive in usb_drives:
                    try:
                        self.save_directory = os.path.join('/media', username, drive)
                        self.save_subdirectory = os.path.join(self.save_directory, datetime.now().strftime('%Y%m%d'))

                        if os.path.ismount(self.save_directory):
                            os.makedirs(self.save_subdirectory, exist_ok=True)
                        print(f'[SUCCESS] Tried {drive}. Connected')
                        self.setup_success()

                        return self.save_subdirectory

                    except PermissionError:
                        print(f'[ERROR] Tried {drive}. Failed')

            except Exception as e:
                print(f"\n[USB ERROR] Permission error.\nError message: {e}")

        return self.save_subdirectory

    def setup_success(self):
        self.storage_LED.blink(on_time=0.1, off_time=0.2, n=3)
        self.record_LED.blink(on_time=0.1, off_time=0.2, n=3)

    def image_write_indicator(self):
        self.record_LED.blink(on_time=0.1, n=1, background=True)

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

        percent_full = (self.storage_used / (self.storage_total))

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

    def alert_flash(self):
        self.storage_LED.blink(on_time=0.5, off_time=0.5, n=None, background=True)
        self.record_LED.blink(on_time=0.5, off_time=0.5, n=None, background=True)

    def stop(self):
        self.update_event.set()

        self.running = False
        self.storage_LED.off()
        self.record_LED.off()

        self.thread.join()


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

    def all_on(self):
        for relay in self.relay_dict.keys():
            self.relay_on(relay)

    def all_off(self):
        for relay in self.relay_dict.keys():
            self.relay_off(relay)

    def remove(self, relay_number):
        self.relay_dict.pop(relay_number, None)

    def clear(self):
        self.relay_dict = {}

    def stop(self):
        self.clear()
        self.all_off()

# this class does the hard work of receiving detection 'jobs' and queuing them to be actuated. It only turns a nozzle on
# if the sprayDur has not elapsed or if the nozzle isn't already on.
class Controller:
    def __init__(self, relay_dict, vis=False):
        self.relay_dict = relay_dict
        self.vis = vis
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
            self.relay_queue_dict[relay_number] = collections.deque(maxlen=5)
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

        line = f"nozzle: {relay} | time: {time_stamp} | location {location} | delay: {delay} | duration: {duration}"
        self.logger.log_line(line, verbose=False)

    def consumer(self, relay):
        """
        Takes only one parameter - nozzle, which enables the selection of the deque, condition from the dictionaries.
        The consumer method is threaded for each nozzle and will wait until it is notified that a new job has been added
        from the receive method. It will then compare the time of detection with time of spraying to activate that nozzle
        for requried length of time.
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
                    if self.vis:
                        self.relay_vis.update(relay=relay, status=True)
                    relay_on = True
                try:
                    time.sleep(onDur)
                    self.logger.log_line(f'[INFO] onDur {onDur} for nozzle {relay} received.')

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
