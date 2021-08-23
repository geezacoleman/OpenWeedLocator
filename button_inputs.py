from sys import platform
if platform == "win32":
    testing = True
else:
    print(platform)
    from gpiozero import Button, LED
    testing = False

import time

class SensitivitySelector:
    def __init__(self, switchDict: dict):
        self.switchDict = switchDict
        self.buttonList = []

        for sensitivityList, GPIOpin in self.switchDict.items():
            button = Button("BOARD{}".format(GPIOpin))
            self.buttonList.append([button, sensitivityList])

    def sensitivity_selector(self):
        pass

class Selector:
    def __init__(self, switchDict: dict):
        self.switchDict = switchDict
        self.buttonList = []

        for algorithm, GPIOpin in self.switchDict.items():
            button = Button("BOARD{}".format(GPIOpin))
            self.buttonList.append([button, algorithm])

    def algorithm_selector(self, algorithm):
        for button in self.buttonList:
            if button[0].is_pressed:
                if algorithm == button[1]:
                    return button[1], False

                return button[1], True

        return 'exg', False

class Recorder:
    def __init__(self, recordGPIO: int):
        self.recordButton = Button("BOARD{}".format(recordGPIO))
        self.record = False
        self.saveRecording = False
        self.running = True
        self.led = LED(pin='BOARD38')

        self.recordButton.when_pressed = self.start_recording
        self.recordButton.when_released = self.stop_recording

    def button_check(self):
        while self.running:
            self.recordButton.when_pressed = self.start_recording
            self.recordButton.when_released = self.stop_recording
            time.sleep(1)

    def start_recording(self):
        self.record = True
        self.saveRecording = False
        self.led.on()

    def stop_recording(self):
        self.saveRecording = True
        self.record = False
        self.led.off()


