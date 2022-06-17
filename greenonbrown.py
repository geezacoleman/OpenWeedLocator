#!/home/pi/.virtualenvs/owl/bin/python3
from algorithms import exg, exg_standardised, exg_standardised_hue, hsv, exgr, gndvi, maxg
from button_inputs import Selector, Recorder
from image_sampler import image_sample
from imutils.video import VideoStream, FileVideoStream, FPS
from imutils import grab_contours
from relay_control import Controller
from queue import Queue
from time import strftime
import subprocess
import argparse
import imutils
import shutil
import numpy as np
import time
import sys
import cv2
import os


def nothing(x):
    pass


def green_on_brown(image,
                   exgMin=30,
                   exgMax=250,
                   hueMin=30,
                   hueMax=90,
                   brightnessMin=5,
                   brightnessMax=200,
                   saturationMin=30,
                   saturationMax=255,
                   minArea=1,
                   show_display=False,
                   algorithm='exg'):
    '''
    Uses a provided algorithm and contour detection to determine green objects in the image. Min and Max
    thresholds are provided.
    :param image: input image to be analysed
    :param exgMin: minimum exG threshold value
    :param exgMax: maximum exG threshold value
    :param hueMin: minimum hue threshold value
    :param hueMax: maximum hue threshold value
    :param brightnessMin: minimum brightness threshold value
    :param brightnessMax: maximum brightness threshold value
    :param saturationMin: minimum saturation threshold value
    :param saturationMax: maximum saturation threshold value
    :param minArea: minimum area for the detection - used to filter out small detections
    :param show_display: True: show windows; False: operates in headless mode
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

    # run the thresholds provided
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

    # if not a binary image, run an adaptive threshold on the area that fits within the thresholded bounds.
    if not threshedAlready:
        output = np.where(output > exgMin, output, 0)
        output = np.where(output > exgMax, 0, output)
        output = np.uint8(np.abs(output))
        if show_display:
            cv2.imshow("HSV Threshold on ExG", output)

        thresholdOut = cv2.adaptiveThreshold(output, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 2)
        thresholdOut = cv2.morphologyEx(thresholdOut, cv2.MORPH_CLOSE, kernel, iterations=1)

    # if already binary, run morphological operations to remove any noise
    if threshedAlready:
        thresholdOut = cv2.morphologyEx(output, cv2.MORPH_CLOSE, kernel, iterations=5)

    if show_display:
        cv2.imshow("Binary Threshold", thresholdOut)

    # find all the contours on the binary images
    cnts = cv2.findContours(thresholdOut.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnts = grab_contours(cnts)
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
    def __init__(self,
                 videoFile=None,
                 show_display=False,
                 recording=False,
                 nozzleNum=4,
                 exgMin=30,
                 exgMax=180,
                 hueMin=30,
                 hueMax=92,
                 brightnessMin=5,
                 brightnessMax=200,
                 saturationMin=30,
                 saturationMax=255,
                 resolution=(416, 320),
                 framerate=32,
                 exp_mode='sports',
                 awb_mode='auto',
                 sensor_mode=0,
                 exp_compensation=-6):

        # different detection parameters
        self.show_display = show_display
        self.recording = recording
        self.resolution = resolution
        self.framerate = framerate
        self.exp_mode = exp_mode
        self.awb_mode = awb_mode
        self.sensor_mode = sensor_mode
        self.exp_compensation = exp_compensation

        # threshold parameters for different algorithms
        self.exgMin = exgMin
        self.exgMax = exgMax
        self.hueMin = hueMin
        self.hueMax = hueMax
        self.saturationMin = saturationMin
        self.saturationMax = saturationMax
        self.brightnessMin = brightnessMin
        self.brightnessMax = brightnessMax

        # setup the track bars if show_display is True
        if self.show_display:
            # create trackbars for the threshold calculation
            self.window_name = "Adjust Detection Thresholds"
            cv2.namedWindow("Adjust Detection Thresholds", cv2.WINDOW_AUTOSIZE)
            cv2.createTrackbar("ExG-Min", self.window_name, self.exgMin, 255, nothing)
            cv2.createTrackbar("ExG-Max", self.window_name, self.exgMax, 255, nothing)
            cv2.createTrackbar("Hue-Min", self.window_name, self.hueMin, 179, nothing)
            cv2.createTrackbar("Hue-Max", self.window_name, self.hueMax, 179, nothing)
            cv2.createTrackbar("Sat-Min", self.window_name, self.saturationMin, 255, nothing)
            cv2.createTrackbar("Sat-Max", self.window_name, self.saturationMax, 255, nothing)
            cv2.createTrackbar("Bright-Min", self.window_name, self.brightnessMin, 255, nothing)
            cv2.createTrackbar("Bright-Max", self.window_name, self.brightnessMax, 255, nothing)

        # nozzleDict maps the reference nozzle number to a boardpin on the embedded device
        self.nozzleDict = {
            0: 13,
            1: 15,
            2: 16,
            3: 18
        }

        # instantiate the nozzle controller - successful start should beep the buzzer
        self.controller = Controller(nozzleDict=self.nozzleDict)

        # instantiate the logger
        self.logger = self.controller.logger

        # check that the resolution is not so high it will entirely brick/destroy the OWL.
        total_pixels = resolution[0] * resolution[1]
        if total_pixels > (832 * 640):
            # change here if you want to test higher resolutions, but be warned, backup your current image!
            self.resolution = (416, 320)
            self.logger.log_line('[WARNING] Resolution {} selected is dangerously high. '
                                 'Resolution has been reset to default to avoid damaging the OWL'.format(resolution),
                                 verbose=True)

        # instantiate the recorder if recording is True
        if self.recording:
            self.fourcc = cv2.VideoWriter_fourcc(*'MJPG')
            self.writer = None

        else:
            self.record = False
            self.saveRecording = False

        # check if test video or videostream from camera
        if videoFile:
            self.cam = FileVideoStream(videoFile).start()
        # if no video, start the camera with the provided parameters
        else:
            try:
                self.cam = VideoStream(usePiCamera=True,
                                       resolution=self.resolution,
                                       framerate=self.framerate,
                                       exposure_mode=self.exp_mode,
                                       awb_mode=self.awb_mode,
                                       sensor_mode=self.sensor_mode,
                                       exposure_compensation=self.exp_compensation).start()
            except ModuleNotFoundError:
                self.cam = VideoStream(src=0).start()
            time.sleep(2.0)
        frame_width = self.cam.stream.get(cv2.CAP_PROP_FRAME_WIDTH)
        frame_height = self.cam.stream.get(cv2.CAP_PROP_FRAME_HEIGHT)

        # save camera settings to the log
        self.logger.log_line('[INFO] Camera setup complete. Settings: '
                             '\nResolution: {}'
                             '\nFramerate: {}'
                             '\nExposure Mode: {}'
                             '\nAutoWhiteBalance: {}'
                             '\nExposure Compensation: {}'
                             '\nSensor Mode: {}'.format(self.resolution,
                                                      self.framerate,
                                                      self.exp_mode,
                                                      self.awb_mode,
                                                      self.exp_compensation,
                                                      self.sensor_mode), verbose=True)

        # set the sprayqueue size
        self.sprayQueue = Queue(maxsize=10)

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

        # sensitivity and weed size to be added
        self.sensitivity = None
        self.laneCoords = {}

        # add the total number of nozzles. This can be changed easily, but the nozzleDict and physical relays would need
        # to be updated too. Fairly straightforward, so an opportunity for more precise application
        self.nozzleNum = nozzleNum

        # activation region limit - once weed crosses this line, nozzle is activated
        self.yAct = int((0.2) * frame_height)
        self.laneWidth = frame_width / self.nozzleNum

        # calculate lane coords and draw on frame
        for i in range(self.nozzleNum):
            laneX = int(i * self.laneWidth)
            self.laneCoords[i] = laneX


    def hoot(self, sprayDur, delay, sample=False, sampleDim=400, saveDir='output', camera_name='cam1', algorithm='exg',
             selectorEnabled=False, minArea=10, log_fps=False):

        # track FPS and framecount
        if log_fps:
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
                    if log_fps:
                        fps.stop()
                        print("[INFO] Stopped. Approximate FPS: {:.2f}".format(fps.fps()))
                        self.stop()
                        break
                    else:
                        print("[INFO] Stopped.")
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
                if self.show_display:
                    self.exgMin = cv2.getTrackbarPos("ExG-Min", self.window_name)
                    self.exgMax = cv2.getTrackbarPos("ExG-Max", self.window_name)
                    self.hueMin = cv2.getTrackbarPos("Hue-Min", self.window_name)
                    self.hueMax = cv2.getTrackbarPos("Hue-Max", self.window_name)
                    self.saturationMin = cv2.getTrackbarPos("Sat-Min", self.window_name)
                    self.saturationMax = cv2.getTrackbarPos("Sat-Max", self.window_name)
                    self.brightnessMin = cv2.getTrackbarPos("Bright-Min", self.window_name)
                    self.brightnessMax = cv2.getTrackbarPos("Bright-Max", self.window_name)

                else:
                    # this leaves it open to adding dials for sensitivity. Static at the moment, but could be dynamic
                    self.update(exgMin=self.exgMin, exgMax=self.exgMax)  # add in update values here

                # pass image, thresholds to green_on_brown function
                cnts, boxes, weedCentres, imageOut = green_on_brown(frame.copy(), exgMin=self.exgMin,
                                                                    exgMax=self.exgMax,
                                                                    hueMin=self.hueMin,
                                                                    hueMax=self.hueMax,
                                                                    saturationMin=self.saturationMin,
                                                                    saturationMax=self.saturationMax,
                                                                    brightnessMin=self.brightnessMin,
                                                                    brightnessMax=self.brightnessMax,
                                                                    show_display=self.show_display,
                                                                    algorithm=algorithm, minArea=minArea)

                ##### IMAGE SAMPLER #####
                # record sample images if required of weeds detected
                # uncomment if needed
                # if frameCount % 60 == 0 and sample is True:
                #     saveFrame = frame.copy()
                #     sampleThread = Thread(target=image_sample, args=[saveFrame, weedCentres, saveDir, sampleDim])
                #     sampleThread.start()
                #########################

                # loop over the ID/weed centres from contours
                for ID, centre in enumerate(weedCentres):
                    # if they are in activation region the spray them
                    if centre[1] > self.yAct:
                        sprayTime = time.time()
                        for i in range(self.nozzleNum):
                            # determine which lane needs to be activated
                            if int(self.laneCoords[i]) <= centre[0] < int(self.laneCoords[i] + self.laneWidth):
                                # log a spray job with the controller using the nozzle, delay, timestamp and spray duration
                                # if GPS is used/speed control, delay can be updated automatically based on forward speed
                                self.controller.receive(nozzle=i, delay=delay, timeStamp=sprayTime, duration=sprayDur)

                # update the framerate counter
                if log_fps:
                    fps.update()

                if self.show_display:
                    cv2.imshow("Detection Output", imutils.resize(imageOut, width=600))

                if self.record and not self.saveRecording:
                    self.writer.write(frame)

                if self.saveRecording and not self.record:
                    self.writer.release()
                    self.controller.solenoid.beep(duration=0.1)
                    self.recorderButton.saveRecording = False
                    if log_fps:
                        fps.stop()
                        self.logger.log_line_video(
                            "[INFO] Approximate FPS: {:.2f}".format(fps.fps()), verbose=True)
                        fps = FPS().start()

                    self.writer = None
                    self.logger.log_line_video("[INFO] {} stopped.".format(self.baseName), verbose=True)

                k = cv2.waitKey(1) & 0xFF
                if k == 27:
                    if log_fps:
                        fps.stop()
                        self.logger.log_line_video(
                            "[INFO] Approximate FPS: {:.2f}".format(fps.fps()),
                            verbose=True)
                    self.logger.log_line_video("[INFO] Stopped.", verbose=True)
                    self.stop()
                    break

        except KeyboardInterrupt:
            if log_fps:
                fps.stop()
                self.logger.log_line_video(
                    "[INFO] Approximate FPS: {:.2f}".format(fps.fps()),
                    verbose=True)
            self.logger.log_line_video("[INFO] Stopped.", verbose=True)
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

        if self.show_display:
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
    # these command line arguments enable people to operate/change some settings from the command line instead of
    # opening up a the OWL code each time.
    ap = argparse.ArgumentParser()
    ap.add_argument('--video-file', type=str, default=None, help='use video file instead')
    ap.add_argument('--show-display', action='store_true', default=False, help='show display windows')
    ap.add_argument('--recording', action='store_true', default=False, help='record video')
    ap.add_argument('--algorithm', type=str, default='exhsv', choices=['exg', 'nexg', 'exgr', 'maxg', 'exhsv', 'hsv'])
    ap.add_argument('--framerate', type=int, default=40, choices=range(10, 121), metavar="[10-120]",
                    help='set camera framerate between 10 and 120 FPS. Framerate will depend on sensor mode, though'
                         ' setting framerate takes precedence over sensor_mode, For example sensor_mode=0 and framerate=120'
                         ' will reset the sensor_mode to 3.')
    ap.add_argument('--exp-mode', type=str, default='beach', choices=['off', 'auto', 'nightpreview', 'backlight',
                                                                            'spotlight', 'sports', 'snow', 'beach',
                                                                            'verylong', 'fixedfps', 'antishake',
                                                                            'fireworks'],
                    help='set exposure mode of camera')
    ap.add_argument('--awb-mode', type=str, default='auto', choices=['off', 'auto', 'sunlight', 'cloudy', 'shade',
                                                                     'tungsten', 'fluorescent', 'incandescent',
                                                                     'flash', 'horizon'],
                    help='set the auto white balance mode of the camera')
    ap.add_argument('--sensor-mode', type=int, default=0, choices=[0, 1, 2, 3], metavar="[0 to 3]",
                    help='set the sensor mode for the camera between 0 and 3. '
                         'Check Raspberry Pi camera documentation for specifics of each mode')
    ap.add_argument('--exp-compensation', type=int, default=-6, choices=range(-24, 24), metavar="[-24 to 24]",
                    help='set the exposure compensation (EV) for the camera between -24 and 24. '
                         'Raspberry Pi cameras seem to overexpose images preferentially.')
    args = ap.parse_args()

    owl = Owl(videoFile=args.video_file,
              show_display=args.show_display,
              recording=args.recording,
              exgMin=25,
              exgMax=200,
              hueMin=39,
              hueMax=83,
              saturationMin=50,
              saturationMax=220,
              brightnessMin=60,
              brightnessMax=190,
              resolution=(416, 320),
              nozzleNum=4,
              framerate=args.framerate,
              exp_mode=args.exp_mode,
              exp_compensation=args.exp_compensation,
              awb_mode=args.awb_mode,
              sensor_mode=args.sensor_mode
              )

    # start the targeting!
    owl.hoot(sprayDur=0.15,
             delay=0,
             sample=False,
             sampleDim=1000,
             saveDir='/home/pi',
             algorithm=args.algorithm,
             selectorEnabled=False,
             camera_name='hsv',
             minArea=10
             )
