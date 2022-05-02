#!/home/pi/.virtualenvs/owl/bin/python3
from button_inputs import Selector, Recorder
from image_sampler import image_sample
from greenonbrown import green_on_brown
from greenongreen import green_on_green
from imutils.video import VideoStream, FileVideoStream, FPS
from relay_control import Controller
from queue import Queue
from time import strftime
import subprocess
import imutils
import shutil
import time
import sys
import cv2
import os


def nothing(x):
    pass


class Owl:
    def __init__(self, video=False, videoFile=None, recording=False, nozzleNum=4, headless=True,
                 exgMin=30, exgMax=180, hueMin=30,hueMax=92, brightnessMin=5, brightnessMax=200,
                 saturationMin=30, saturationMax=255, resolution=(832, 624), framerate=32):

        # different detection parameters
        self.headless = headless
        self.recording = recording
        self.resolution = resolution
        self.framerate = framerate

        # threshold parameters for different algorithms
        self.exgMin = exgMin
        self.exgMax = exgMax
        self.hueMin = hueMin
        self.hueMax = hueMax
        self.saturationMin = saturationMin
        self.saturationMax = saturationMax
        self.brightnessMin = brightnessMin
        self.brightnessMax = brightnessMax

        # setup the track bars if headless is False
        if not self.headless:
            # create trackbars for the threshold calculation
            cv2.namedWindow("Params")
            cv2.createTrackbar("thresholdMin", "Params", self.exgMin, 255, nothing)
            cv2.createTrackbar("thresholdMax", "Params", self.exgMax, 255, nothing)

        # instantiate the recorder if recording is True
        if self.recording:
            self.fourcc = cv2.VideoWriter_fourcc(*'MJPG')
            self.writer = None

        else:
            self.record = False
            self.saveRecording = False

        # check if test video or videostream from camera
        if video:
            self.cam = FileVideoStream(videoFile).start()
        # if no video, start the camera with the provided parameters
        else:
            try:
                self.cam = VideoStream(usePiCamera=True, resolution=self.resolution,
                                       framerate=self.framerate, exposure_mode='sports')
                self.cam.start()

            except ModuleNotFoundError:
                self.cam = VideoStream(src=0).start()
            time.sleep(1.0)
        # set the sprayqueue size
        self.sprayQueue = Queue(maxsize=10)

        # nozzleDict maps the reference nozzle number to a boardpin on the embedded device
        self.nozzleDict = {
            0: 13,
            1: 15,
            2: 16,
            3: 18
            }

        ### Data collection only ###
        # algorithmDict maps pins to algorithms for data collection
        self.algorithmDict = {
            "exg": 29,
            "nexg": 31,
            "hsv": 33,
            "exhsv": 35,
        }
        # this is where the recording button can be added. Currently set to pin 37
        if self.recording:
            self.recorderButton = Recorder(recordGPIO=37)
        ############################

        # instantiate the nozzle controller - successful start should beep the buzzer
        self.controller = Controller(nozzleDict=self.nozzleDict)

        # instantiate the logger
        self.logger = self.controller.logger

        # sensitivity and weed size to be added
        self.sensitivity = None
        self.laneCoords = {}

        # add the total number of nozzles. This can be changed easily, but the nozzleDict and physical relays would need
        # to be updated too. Fairly straightforward, so an opportunity for more precise application
        self.nozzleNum = nozzleNum

    def hoot(self, sprayDur, delay, sample=False, sampleDim=400, saveDir='output', camera_name='cam1', algorithm='exg',
             selectorEnabled=False, minArea=10, model=''):
        # track FPS and framecount
        fps = FPS().start()
        if selectorEnabled:
            self.selector = Selector(switchDict=self.algorithmDict)

        if algorithm == "gog":
            # add in load model
            pass

        try:
            while True:
                delay = self.update_delay(delay)
                frame = self.cam.read()
                if selectorEnabled:
                    algorithm, newAlgorithm = self.selector.algorithm_selector(algorithm)
                    if newAlgorithm:
                        self.logger.log_line('[NEW ALGO] {}'.format(algorithm))

                if self.recording:
                    self.record = self.recorderButton.record
                    self.saveRecording = self.recorderButton.saveRecording

                if frame is None:
                    fps.stop()
                    print("[INFO] Stopped. Approximate FPS: {:.2f}".format(fps.fps()))
                    self.stop()
                    break

                if self.record and self.writer is None:
                    saveDir = os.path.join(saveDir, strftime("%Y%m%d-{}-{}".format(camera_name, algorithm)))
                    if not os.path.exists(saveDir):
                        os.makedirs(saveDir)

                    self.baseName = os.path.join(saveDir, strftime("%Y%m%d-%H%M%S-{}-{}".format(camera_name, algorithm)))
                    videoName = self.baseName + '.avi'
                    self.logger.new_video_logfile(name=self.baseName + '.txt')
                    self.writer = cv2.VideoWriter(videoName, self.fourcc, 30, (frame.shape[1], frame.shape[0]), True)

                # retrieve the trackbar positions for thresholds
                if not self.headless:
                    self.exgMin = cv2.getTrackbarPos("thresholdMin", "Params")
                    self.exgMax = cv2.getTrackbarPos("thresholdMax", "Params")

                else:
                    # this leaves it open to adding dials for sensitivity. Static at the moment, but could be dynamic
                    self.update(exgMin=self.exgMin, exgMax=self.exgMax) # add in update values here

                # pass image, thresholds to green_on_brown function
                if algorithm == 'gog':
                    boxes, weedCentres, imageOut = green_on_brown(frame.copy(), headless=True, algorithm=model)

                else:
                    boxes, weedCentres, imageOut = green_on_brown(frame.copy(), exgMin=self.exgMin,
                                                                        exgMax=self.exgMax,
                                                                        hueMin=self.hueMin,
                                                                        hueMax=self.hueMax,
                                                                        saturationMin=self.saturationMin,
                                                                        saturationMax=self.saturationMax,
                                                                        brightnessMin=self.brightnessMin,
                                                                        brightnessMax=self.brightnessMax,
                                                                        headless=self.headless,
                                                                        algorithm=algorithm, minArea=minArea)

                ##### IMAGE SAMPLER #####
                # record sample images if required of weeds detected
                # uncomment if needed
                # if frameCount % 60 == 0 and sample is True:
                #     saveFrame = frame.copy()
                #     sampleThread = Thread(target=image_sample, args=[saveFrame, weedCentres, saveDir, sampleDim])
                #     sampleThread.start()
                #########################

                # activation region limit - once weed crosses this line, nozzle is activated
                self.yAct = int((0.2) * frame.shape[0])
                laneWidth = imageOut.shape[1] / self.nozzleNum

                # calculate lane coords and draw on frame
                for i in range(self.nozzleNum):
                    laneX = int(i * laneWidth)
                    # cv2.line(displayFrame, (laneX, 0), (laneX, imageOut.shape[0]), (0, 255, 255), 2)
                    self.laneCoords[i] = laneX

                # loop over the ID/weed centres from contours
                for ID, centre in enumerate(weedCentres):
                    # if they are in activation region the spray them
                    if centre[1] > self.yAct:
                        sprayTime = time.time()
                        for i in range(self.nozzleNum):
                            # determine which lane needs to be activated
                            if int(self.laneCoords[i]) <= centre[0] < int(self.laneCoords[i] + laneWidth):
                                # log a spray job with the controller using the nozzle, delay, timestamp and spray duration
                                # if GPS is used/speed control, delay can be updated automatically based on forward speed
                                self.controller.receive(nozzle=i, delay=delay, timeStamp=sprayTime, duration=sprayDur)

                # update the framerate counter
                fps.update()

                if not self.headless:
                    cv2.imshow("Output", imutils.resize(imageOut, width=600))

                if self.record and not self.saveRecording:
                    self.writer.write(frame)

                if self.saveRecording and not self.record:
                    self.writer.release()
                    self.controller.solenoid.beep(duration=0.1)
                    self.recorderButton.saveRecording = False
                    fps.stop()
                    self.writer = None
                    self.logger.log_line_video("[INFO] {}. Approximate FPS: {:.2f}".format(self.baseName, fps.fps()), verbose=True)
                    fps = FPS().start()

                k = cv2.waitKey(1) & 0xFF
                if k == 27:
                    fps.stop()
                    self.logger.log_line_video("[INFO] Stopped. Approximate FPS: {:.2f}".format(fps.fps()), verbose=True)
                    self.stop()
                    break

        except KeyboardInterrupt:
            fps.stop()
            self.logger.log_line_video("[INFO] Stopped. Approximate FPS: {:.2f}".format(fps.fps()), verbose=True)
            self.stop()

        except Exception as e:
            self.controller.solenoid.beep(duration=0.5, repeats=5)
            self.logger.log_line("[CRITICAL ERROR] STOPPED: {}".format(e))

    # still in development
    def update_software(self):
        USBDir, USBConnected = check_for_usb()
        if USBConnected:
            files = os.listdir(USBDir)
            workingDir = '/home/pi'

            # move old version to version control directory first
            oldVersionDir = strftime(workingDir + "/%Y%m%d-%H%M%S_update")
            os.mkdir(oldVersionDir)

            currentDir = '/home/pi/owl'
            shutil.move(currentDir, oldVersionDir)

            # move new directory to working directory
            for item in files:
                if 'owl' in item:
                    shutil.move()

    def stop(self):
        self.controller.running = False
        self.controller.solenoid.all_off()
        self.controller.solenoid.beep(duration=0.1)
        self.controller.solenoid.beep(duration=0.1)
        self.cam.stop()
        if self.record:
            self.writer.release()
            self.recorderButton.running = False

        if not self.headless:
            cv2.destroyAllWindows()

        sys.exit()

    def update(self, exgMin=30, exgMax=180):
        self.exgMin = exgMin
        self.exgMax = exgMax

    def update_delay(self, delay=0):
        # if GPS added, could use it here to return a delay variable based on speed.
        return delay


def check_for_usb():
    try:
        nanoMediaFolder = 'ls /media/pi'
        proc = subprocess.Popen(nanoMediaFolder, shell=True, preexec_fn=os.setsid, stdout=subprocess.PIPE)
        usbName = proc.stdout.readline().rstrip().decode('utf-8')

        if len(usbName) > 0:
            print('[INFO] Saving to {} usb'.format(usbName))
            saveDir = '/media/pi/{}/'.format(usbName)
            return saveDir, True

        else:
            print('[INFO] No USB connected. Saving to videos')
            saveDir = '/home/pi/owl/videos'
            return saveDir, False

    except AttributeError:
        print('[INFO] Windows computer detected...')
        saveDir = '/videos/'
        return saveDir, False

# business end of things
if __name__ == "__main__":
    owl = Owl(video=False,
              videoFile=r'',
              headless=True,
              recording=False,
              exgMin=25,
              exgMax=200,
              hueMin=39,
              hueMax=83,
              saturationMin=50,
              saturationMax=220,
              brightnessMin=60,
              brightnessMax=190,
              framerate=32,
              resolution=(416, 320))

    # start the targeting!
    owl.hoot(sprayDur=0.15,
             delay=0,
             sample=False,
             sampleDim=1000,
             saveDir='/home/pi',
             algorithm='exhsv',
             selectorEnabled=False,
             camera_name='hsv',
             minArea=10)
