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
    def __init__(self, detection_state, stop_flag, board_pin='BOARD37', bounce_time=1.0):
        self.switch = Button(board_pin, bounce_time=bounce_time)

        self.detection_state = detection_state
        self.stop_flag = stop_flag

        self.switch.when_pressed = self.enable_detection
        self.switch.when_released = self.disable_detection

        if self.switch.is_pressed:
            self.enable_detection()
        else:
            self.disable_detection()

    def enable_detection(self):
        with self.detection_state.get_lock():
            self.detection_state.value = True

    def disable_detection(self):
        with self.detection_state.get_lock():
            self.detection_state.value = False

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


