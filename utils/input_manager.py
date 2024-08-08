import time
import platform
import warnings
import configparser

import cv2

# check if the system is being tested on a Windows or Linux x86 64 bit machine
if 'rpi' in platform.platform():
    testing = False
    from gpiozero import Button, LED

elif platform.system() == "Windows":
    warning_message = "[WARNING] The system is running on a Windows platform. GPIO disabled. Test mode active."
    warnings.warn(warning_message, RuntimeWarning)
    testing = True

elif 'aarch' in platform.platform():
    testing = False
    from gpiozero import Button, LED

else:
    warning_message = "[WARNING] The system is not running on a recognized platform. GPIO disabled. Test mode active."
    warnings.warn(warning_message, RuntimeWarning)
    testing = True

class UteController:
    def __init__(self, detection_state,
                 sample_state,
                 stop_flag,
                 owl_instance,
                 status_indicator,
                 switch_purpose='recording',
                 switch_board_pin='BOARD37',
                 bounce_time=1.0):

        self.switch = Button(switch_board_pin, bounce_time=bounce_time)
        self.switch_purpose = switch_purpose

        self.detection_state = detection_state
        self.sample_state = sample_state

        self.owl = owl_instance
        self.status_indicator = status_indicator

        self.stop_flag = stop_flag

        # Set up a single handler for both press and release
        self.switch.when_pressed = self.toggle_state
        self.switch.when_released = self.toggle_state

        # Initialize state based on initial switch position
        self.update_state()

    def update_state(self):
        is_active = self.switch.is_pressed

        if self.switch_purpose == 'detection':
            with self.detection_state.get_lock():
                self.detection_state.value = is_active
            self.owl.disable_detection = not is_active
            if is_active:
                self.status_indicator.enable_weed_detection()
            else:
                self.status_indicator.disable_weed_detection()

        elif self.switch_purpose == 'recording':
            with self.sample_state.get_lock():
                self.sample_state.value = is_active
            self.owl.sample_images = is_active
            if is_active:
                self.status_indicator.enable_image_recording()
            else:
                self.status_indicator.disable_image_recording()

    def toggle_state(self):
        self.update_state()

    def weed_detect_indicator(self):
        self.status_indicator.weed_detect_indicator()

    def image_write_indicator(self):
        self.status_indicator.image_write_indicator()

    def run(self):
        while not self.stop_flag.value:
            time.sleep(0.1)  # sleep to reduce CPU usage

    def stop(self):
        with self.stop_flag.get_lock():
            self.stop_flag.value = True


class AdvancedController:
    def __init__(self, recording_state,
                 sensitivity_state,
                 detection_mode_state,
                 stop_flag,
                 owl_instance,
                 status_indicator,
                 low_sensitivity_config,
                 high_sensitivity_config,
                 detection_mode_bpin_down='BOARD35',
                 detection_mode_bpin_up='BOARD36',
                 recording_bpin='BOARD38',
                 sensitivity_bpin='BOARD40',
                 bounce_time=1.0):

        self.recording_switch = Button(recording_bpin, bounce_time=bounce_time)
        self.sensitivity_switch = Button(sensitivity_bpin, bounce_time=bounce_time)
        self.detection_mode_switch_up = Button(detection_mode_bpin_up, bounce_time=bounce_time)
        self.detection_mode_switch_down = Button(detection_mode_bpin_down, bounce_time=bounce_time)

        self.recording_state = recording_state
        self.sensitivity_state = sensitivity_state
        self.detection_mode_state = detection_mode_state

        self.stop_flag = stop_flag

        # set up instances for owl and status
        self.owl = owl_instance
        self.status_indicator = status_indicator

        self.low_sensitivity_config = low_sensitivity_config
        self.high_sensitivity_config = high_sensitivity_config

        # Set up switch handlers
        self.recording_switch.when_pressed = self.toggle_recording
        self.sensitivity_switch.when_pressed = self.toggle_sensitivity
        self.detection_mode_switch_up.when_pressed = lambda: self.set_detection_mode(2)  # All solenoids on
        self.detection_mode_switch_up.when_released = lambda: self.set_detection_mode(1)  # Off
        self.detection_mode_switch_down.when_pressed = lambda: self.set_detection_mode(0)  # Detection on
        self.detection_mode_switch_down.when_released = lambda: self.set_detection_mode(1)  # Off

        # Initialize states based on initial switch positions
        self.update_state()

    def update_state(self):
        self.update_recording_state()
        self.update_sensitivity_state()
        self.update_detection_mode_state()

    def toggle_recording(self):
        with self.recording_state.get_lock():
            self.recording_state.value = not self.recording_state.value
        if self.recording_state.value:
            self.status_indicator.enable_image_recording()
            self.owl.sample_images = True
        else:
            self.status_indicator.disable_image_recording()
            self.owl.sample_images = False

    def update_recording_state(self):
        with self.recording_state.get_lock():
            self.recording_state.value = self.recording_switch.is_pressed
        if self.recording_state.value:
            self.status_indicator.enable_image_recording()
        else:
            self.status_indicator.disable_image_recording()

    def toggle_sensitivity(self):
        with self.sensitivity_state.get_lock():
            self.sensitivity_state.value = not self.sensitivity_state.value
        self.update_sensitivity_settings()

    def update_sensitivity_state(self):
        with self.sensitivity_state.get_lock():
            self.sensitivity_state.value = self.sensitivity_switch.is_pressed
        self.update_sensitivity_settings()

    def update_sensitivity_settings(self):
        config = configparser.ConfigParser()
        if self.sensitivity_state.value:  # High sensitivity
            config.read(self.high_sensitivity_config)
        else:  # Low sensitivity
            config.read(self.low_sensitivity_config)

        # Update Owl instance settings
        self.owl.exgMin = config.getint('GreenOnBrown', 'exgMin')
        self.owl.exgMax = config.getint('GreenOnBrown', 'exgMax')
        self.owl.hueMin = config.getint('GreenOnBrown', 'hueMin')
        self.owl.hueMax = config.getint('GreenOnBrown', 'hueMax')
        self.owl.saturationMin = config.getint('GreenOnBrown', 'saturationMin')
        self.owl.saturationMax = config.getint('GreenOnBrown', 'saturationMax')
        self.owl.brightnessMin = config.getint('GreenOnBrown', 'brightnessMin')
        self.owl.brightnessMax = config.getint('GreenOnBrown', 'brightnessMax')

        # Update trackbars if show_display is True
        if self.owl.show_display:
            cv2.setTrackbarPos("ExG-Min", self.owl.window_name, self.owl.exgMin)
            cv2.setTrackbarPos("ExG-Max", self.owl.window_name, self.owl.exgMax)
            cv2.setTrackbarPos("Hue-Min", self.owl.window_name, self.owl.hueMin)
            cv2.setTrackbarPos("Hue-Max", self.owl.window_name, self.owl.hueMax)
            cv2.setTrackbarPos("Sat-Min", self.owl.window_name, self.owl.saturationMin)
            cv2.setTrackbarPos("Sat-Max", self.owl.window_name, self.owl.saturationMax)
            cv2.setTrackbarPos("Bright-Min", self.owl.window_name, self.owl.brightnessMin)
            cv2.setTrackbarPos("Bright-Max", self.owl.window_name, self.owl.brightnessMax)

    def set_detection_mode(self, mode):
        with self.detection_mode_state.get_lock():
            self.detection_mode_state.value = mode
        if mode == 0:  # Detection on
            self.status_indicator.enable_weed_detection()
            self.owl.disable_detection = False
        elif mode == 2: # all solenoids on
            self.status_indicator.disable_weed_detection()
            self.owl.relay_controller.relay.all_on()
            self.owl.disable_detection = True
        else:  # off or any other unexpected value
            self.status_indicator.disable_weed_detection()
            self.owl.relay_controller.relay.all_off()
            self.owl.disable_detection = True

    def update_detection_mode_state(self):
        if self.detection_mode_switch_up.is_pressed:
            self.set_detection_mode(2)  # All solenoids on
        elif self.detection_mode_switch_down.is_pressed:
            self.set_detection_mode(0)  # Detection on
        else:
            self.set_detection_mode(1)  # Off

    def weed_detect_indicator(self):
        self.status_indicator.weed_detect_indicator()

    def image_write_indicator(self):
        self.status_indicator.image_write_indicator()

    def run(self):
        while not self.stop_flag.value:
            time.sleep(0.1)  # sleep to reduce CPU usage

    def stop(self):
        with self.stop_flag.get_lock():
            self.stop_flag.value = True
class SensitivitySelector:
    def __init__(self, switchDict: dict):
        self.switchDict = switchDict
        self.buttonList = []

        for sensitivityList, GPIOpin in self.switchDict.items():
            button = Button(f"BOARD{GPIOpin}")
            self.buttonList.append([button, sensitivityList])

    def sensitivity_selector(self):
        pass

# used with a physical dial to select the algorithm during initial validation.
# No longer used in the main greenonbrown.py file
class Selector:
    def __init__(self, switchDict: dict):
        self.switchDict = switchDict
        self.buttonList = []

        for algorithm, GPIOpin in self.switchDict.items():
            button = Button(f"BOARD{GPIOpin}")
            self.buttonList.append([button, algorithm])

    def algorithm_selector(self, algorithm):
        for button in self.buttonList:
            if button[0].is_pressed:
                if algorithm == button[1]:
                    return button[1], False

                return button[1], True

        return 'exg', False

# video recording button
class Recorder:
    def __init__(self, recordGPIO: int):
        self.record_button = Button(f"BOARD{recordGPIO}")
        self.record = False
        self.save_recording = False
        self.running = True
        self.led = LED(pin='BOARD38')

        self.record_button.when_pressed = self.start_recording
        self.record_button.when_released = self.stop_recording

    def button_check(self):
        while self.running:
            self.record_button.when_pressed = self.start_recording
            self.record_button.when_released = self.stop_recording
            time.sleep(1)

    def start_recording(self):
        self.record = True
        self.save_recording = False
        self.led.on()

    def stop_recording(self):
        self.save_recording = True
        self.record = False
        self.led.off()


