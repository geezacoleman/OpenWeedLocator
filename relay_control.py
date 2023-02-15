from logger import Logger
from threading import Thread, Condition
from utils.cli_vis import NozzleVis
import collections
import time
import os

import platform
# check if the system is being tested on a Windows or Linux x86 64 bit machine
if platform.system() == "Windows":
    testing = True
else:
    if '64' in platform.machine():
        testing = True
    else:
        from gpiozero import Buzzer, OutputDevice
        testing = False

# two test classes to run the analysis on a desktop computer if a "win32" platform is detected
class TestRelay:
    def __init__(self, relayNumber, verbose=False):
        self.relayNumber = relayNumber
        self.verbose = verbose

    def on(self):
        if self.verbose:
            print("[TEST] Relay {} ON".format(self.relayNumber))

    def off(self):
        if self.verbose:
            print("[TEST] Relay {} OFF".format(self.relayNumber))

class TestBuzzer:
    def beep(self, on_time: int, off_time: int, n=1, verbose=False):
        for i in range(n):
            if verbose:
                print('BEEP')

# control class for the relay board
class RelayControl:
    def __init__(self, solenoidDict):
        self.testing = True if testing else False
        self.solenoidDict = solenoidDict
        self.on = False

        if not self.testing:
            self.buzzer = Buzzer(pin='BOARD7')
            for nozzle, boardPin in self.solenoidDict.items():
                self.solenoidDict[nozzle] = OutputDevice(pin='BOARD{}'.format(boardPin))

        else:
            self.buzzer = TestBuzzer()
            for nozzle, boardPin in self.solenoidDict.items():
                self.solenoidDict[nozzle] = TestRelay(boardPin)


    def relay_on(self, solenoidNumber, verbose=True):
        relay = self.solenoidDict[solenoidNumber]
        relay.on()

        if verbose:
            print("Solenoid {} ON".format(solenoidNumber))

    def relay_off(self, solenoidNumber, verbose=True):
        relay = self.solenoidDict[solenoidNumber]
        relay.off()

        if verbose:
            print("Solenoid {} OFF".format(solenoidNumber))

    def beep(self, duration=0.2, repeats=2):
        self.buzzer.beep(on_time=duration, off_time=(duration / 2), n=repeats)

    def all_on(self):
        for nozzle in self.solenoidDict.keys():
            self.relay_on(nozzle)

    def all_off(self):
        for nozzle in self.solenoidDict.keys():
            self.relay_off(nozzle)

    def remove(self, solenoidNumber):
        self.solenoidDict.pop(solenoidNumber, None)

    def clear(self):
        self.solenoidDict = {}

    def stop(self):
        self.clear()
        self.all_off()

# this class does the hard work of receiving detection 'jobs' and queuing them to be actuated. It only turns a nozzle on
# if the sprayDur has not elapsed or if the nozzle isn't already on.
class Controller:
    def __init__(self, nozzleDict, vis=False):
        self.nozzleDict = nozzleDict
        self.vis = vis
        # instantiate relay control with supplied nozzle dictionary to map to correct board pins
        self.solenoid = RelayControl(self.nozzleDict)
        self.nozzleQueueDict = {}
        self.nozzleconditionDict = {}

        # start the logger and log file using absolute path of python file
        self.saveDir = os.path.join(os.path.dirname(__file__), 'logs')
        self.logger = Logger(name="weed_log.txt", saveDir=self.saveDir)

        # create a job queue and Condition() for each nozzle
        print("[INFO] Setting up nozzles...")
        self.nozzle_vis = NozzleVis(relays=len(self.nozzleDict.keys()))
        for nozzle in range(0, len(self.nozzleDict)):
            self.nozzleQueueDict[nozzle] = collections.deque(maxlen=5)
            self.nozzleconditionDict[nozzle] = Condition()

            # create the consumer threads, setDaemon and start the threads.
            nozzleThread = Thread(target=self.consumer, args=[nozzle])
            nozzleThread.setDaemon(True)
            nozzleThread.start()

        time.sleep(1)
        print("[INFO] Nozzle setup complete. Initiating camera...")
        self.solenoid.beep(duration=0.5)

    def receive(self, nozzle, timeStamp, location=0, delay=0, duration=1):
        """
        this method adds a new spray job to specified nozzle queue. GPS location data etc to be added. Time stamped
        records the true time of weed detection from main thread, which is compared to time of nozzle activation for accurate
        on durations. There will be a minimum on duration of this processing speed ~ 0.3s. Will default to 0 though.
        :param nozzle: nozzle number (zero based)
        :param timeStamp: this is the time of detection
        :param location: GPS functionality to be added here
        :param delay: on delay to be added in the future
        :param duration: duration of spray
        """
        inputQMessage = [nozzle, timeStamp, delay, duration]
        inputQ = self.nozzleQueueDict[nozzle]
        inputCondition = self.nozzleconditionDict[nozzle]
        # notifies the consumer thread when something has been added to the queue
        with inputCondition:
            inputQ.append(inputQMessage)
            inputCondition.notify()

        line = "nozzle: {} | time: {} | location {} | delay: {} | duration: {}".format(nozzle, timeStamp, location, delay, duration)
        self.logger.log_line(line, verbose=False)

    def consumer(self, nozzle):
        """
        Takes only one parameter - nozzle, which enables the selection of the deque, condition from the dictionaries.
        The consumer method is threaded for each nozzle and will wait until it is notified that a new job has been added
        from the receive method. It will then compare the time of detection with time of spraying to activate that nozzle
        for requried length of time.
        :param nozzle: nozzle vlaue
        """
        self.running = True
        inputCondition = self.nozzleconditionDict[nozzle]
        inputCondition.acquire()
        nozzleOn = False
        nozzleQueue = self.nozzleQueueDict[nozzle]
        while self.running:
            while nozzleQueue:
                sprayJob = nozzleQueue.popleft()
                inputCondition.release()
                # check to make sure time is positive
                onDur = 0 if (sprayJob[3] - (time.time() - sprayJob[1])) <= 0 else (sprayJob[3] - (time.time() - sprayJob[1]))

                if not nozzleOn:
                    time.sleep(sprayJob[2]) # add in the delay variable
                    self.solenoid.relay_on(nozzle, verbose=False)
                    if self.vis:
                        self.nozzle_vis.update(relay=nozzle, status=True)
                    nozzleOn = True
                try:
                    time.sleep(onDur)
                    self.logger.log_line(
                        '[INFO] onDur {} for nozzle {} received.'.format(onDur, nozzle))

                except ValueError:
                    time.sleep(0)
                    self.logger.log_line(
                        '[ERROR] negative onDur {} for nozzle {} received. Turning on for 0 seconds.'.format(onDur,
                                                                                                             nozzle))
                inputCondition.acquire()
            if len(nozzleQueue) == 0:
                self.solenoid.relay_off(nozzle, verbose=False)
                if self.vis:
                    self.nozzle_vis.update(relay=nozzle, status=False)
                nozzleOn = False

            inputCondition.wait()
