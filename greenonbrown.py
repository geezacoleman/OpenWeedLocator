#!/home/pi/.virtualenvs/owl/bin/python3
from algorithms import exg, exg_standardised, exg_standardised_hue, hsv, exgr, gndvi, maxg
from button_inputs import Selector, Recorder
from image_sampler import image_sample
from imutils.video import VideoStream, FileVideoStream, FPS
from relay_control import Controller
from queue import Queue
from time import strftime
import picamera
import subprocess
import imutils
import shutil
import numpy as np
import time
import sys
import cv2
import os

def nothing(x):
    pass

def green_on_brown(image, exgMin=30, exgMax=250, hueMin=30, hueMax=90, brightnessMin=5, brightnessMax=200, saturationMin=30,
                   saturationMax=255, minArea=1, headless=True, algorithm='exg'):
    '''
    Uses a provided algorithm and contour detection to determine green objects in the image. Min and Max
    thresholds are provided.
    :param image: input image to be analysed
    :param exgMin:
    :param exgMax:
    :param hueMin:
    :param hueMax:
    :param brightnessMin:
    :param brightnessMax:
    :param saturationMin:
    :param saturationMax:
    :param minArea: minimum area for the detection - used to filter out small detections
    :param headless: True: no windows display; False: watch what the algorithm does
    :param algorithm: the algorithm to use. Defaults to ExG if not correct
    :return: returns the contours, bounding boxes, centroids and the image on which the boxes have been drawn
    '''

    # different algorithm options, add in your algorithm here if you make a new one!
    threshedAlready = False
    if algorithm == 'exg':
        output = exg(image)

    elif algorithm == 'exgr':
        output = exgr(image)

    elif algorithm == 'maxg':
        output = maxg(image)

    elif algorithm == 'nexg':
        output = exg_standardised(image)

    elif algorithm == 'exhsv':
        output = exg_standardised_hue(image, hueMin=hueMin, hueMax=hueMax,
                                      brightnessMin=brightnessMin, brightnessMax=brightnessMax,
                                      saturationMin=saturationMin, saturationMax=saturationMax)

    elif algorithm == 'hsv':
        output, threshedAlready = hsv(image, hueMin=hueMin, hueMax=hueMax,
                                      brightnessMin=brightnessMin, brightnessMax=brightnessMax,
                                      saturationMin=saturationMin, saturationMax=saturationMax)

    elif algorithm == 'gndvi':
        output = gndvi(image)

    else:
        output = exg(image)
        print('[WARNING] DEFAULTED TO EXG')

    if not headless:
        cv2.imshow("Threshold", output)

    # run the thresholds provided
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    # if not a binary image, run an adaptive threshold on the area that fits within the thresholded bounds.
    if not threshedAlready:
        output = np.where(output > exgMin, output, 0)
        output = np.where(output > exgMax, 0, output)
        output = np.uint8(np.abs(output))
        if not headless:
            cv2.imshow("post", output)

        thresholdOut = cv2.adaptiveThreshold(output, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 2)
        thresholdOut = cv2.morphologyEx(thresholdOut, cv2.MORPH_CLOSE, kernel, iterations=1)

    # if already binary, run morphological operations to remove any noise
    if threshedAlready:
        thresholdOut = cv2.morphologyEx(output, cv2.MORPH_CLOSE, kernel, iterations=5)

    if not headless:
        cv2.imshow("Threshold", thresholdOut)

    # find all the contours on the binary images
    cnts = cv2.findContours(thresholdOut.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnts = imutils.grab_contours(cnts)
    weedCenters = []
    boxes = []
    # loop over all the detected contours and calculate the centres and bounding boxes
    for c in cnts:
        # filter based on total area of contour
        if cv2.contourArea(c) > minArea:
            # calculate the min bounding box
            startX, startY, boxW, boxH = cv2.boundingRect(c)
            endX = startX + boxW
            endY = startY + boxH
            cv2.rectangle(image, (int(startX), int(startY)), (endX, endY), (0, 0, 255), 2)
            # save the bounding box
            boxes.append([startX, startY, boxW, boxH])
            # compute box center
            centerX = int(startX + (boxW / 2))
            centerY = int(startY + (boxH / 2))
            weedCenters.append([centerX, centerY])

    # returns the contours, bounding boxes, centroids and the image on which the boxes have been drawn
    return cnts, boxes, weedCenters, image

# the
class Owl:
    def __init__(self, video=False, videoFile=None, recording=False, nozzleNum=4, headless=True,
                 exgMin=30, exgMax=180, hueMin=30,hueMax=92, brightnessMin=5, brightnessMax=200,
                 saturationMin=30, saturationMax=255, resolution=(832, 624), framerate=32,
                 exposure_mode='sports'):

        # different detection parameters
        self.headless = headless
        self.recording = recording
        self.resolution = resolution
        self.framerate = framerate
        self.exposure_mode = exposure_mode

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
                self.cam = VideoStream(usePiCamera=True, resolution=self.resolution, framerate=self.framerate, 
                exposure_mode=self.exposure_mode).start()
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
             selectorEnabled=False, minArea=10):
        # track FPS and framecount
        fps = FPS().start()
        if selectorEnabled:
            self.selector = Selector(switchDict=self.algorithmDict)

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
                cnts, boxes, weedCentres, imageOut = green_on_brown(frame.copy(), exgMin=self.exgMin,
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
              resolution=(416, 320),
              exposure_mode='sports')

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


