import time
import platform
import warnings
# check if the system is being tested on a Windows or Linux x86 64 bit machine
if 'rpi' in platform.platform():
    testing = False
    from gpiozero import Button, LED

elif platform.system() == "Windows":
    warning_message = "[WARNING] The system is running on a Windows platform. GPIO disabled. Test mode active."
    warnings.warn(warning_message, RuntimeWarning)
    testing = True
    testing = True

elif 'aarch' in platform.platform():
    testing = False
    from gpiozero import Button, LED

else:
    warning_message = "[WARNING] The system is not running on a recognized platform. GPIO disabled. Test mode active."
    warnings.warn(warning_message, RuntimeWarning)
    testing = True


class BasicController:
    def __init__(self, detection_state, sample_state, stop_flag, switch_purpose='detection', switch_board_pin='BOARD36',
                 det_status_pin='BOARD37', bounce_time=1.0):
        self.switch = Button(switch_board_pin, bounce_time=bounce_time)
        self.status_led = LED(det_status_pin)
        self.switch_purpose = switch_purpose

        self.detection_state = detection_state
        self.sample_state = sample_state

        self.stop_flag = stop_flag

        if self.switch_purpose == 'detection':
            self.switch.when_pressed = self.toggle_off
            self.switch.when_released = self.toggle_on

        elif self.switch_purpose == 'recording':
            self.switch.when_pressed = self.toggle_on
            self.switch.when_released = self.toggle_off

        else:
            raise ValueError("[ERROR] Invalid switch purpose. Use 'detection' or 'recording'.")

        if self.switch.is_pressed:
            self.enable_current_purpose()
        else:
            self.disable_current_purpose()

    def toggle_on(self):
        with self.detection_state.get_lock():
            self.detection_state.value = False
            self.status_led.off()

        with self.sample_state.get_lock():
            self.sample_state.value = True

    def toggle_off(self):
        with self.detection_state.get_lock():
            self.detection_state.value = True
            self.status_led.blink(on_time=0.2, off_time=0.2, n=5, background=True)

        with self.sample_state.get_lock():
            self.sample_state.value = False

    def enable_current_purpose(self):
        if self.switch_purpose == 'detection':
            self.toggle_off()

        elif self.switch_purpose == 'recording':
            self.toggle_on()

    def disable_current_purpose(self):
        if self.switch_purpose == 'detection':
            self.toggle_on()

        elif self.switch_purpose == 'recording':
            self.toggle_off()

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


