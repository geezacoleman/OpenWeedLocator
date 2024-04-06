#!/home/pi/.virtualenvs/owl/bin/python3
from button_inputs import Recorder
from image_sampler import bounding_box_image_sample, square_image_sample, whole_image_save
from utils.blur_algorithms import fft_blur
from greenonbrown import GreenOnBrown
from relay_control import Controller
from utils.frame_reader import FrameReader

from datetime import datetime, timezone
from imutils.video import VideoStream, FPS
from queue import Queue
from time import strftime
from threading import Thread

import numpy as np
import argparse
import imutils
import json
import time
import sys
import cv2
import os


def nothing(x):
    pass


class Owl:
    def __init__(self,
                 input_file_or_directory=None,
                 show_display=False,
                 focus=False,
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
                 exp_compensation=-4,
                 parameters_json=None,
                 image_loop_time=5):

        # different detection parameters
        self.show_display = show_display
        self.nozzle_vis = None
        self.recording = recording
        self.focus = focus
        if self.focus:
            self.show_display = True

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

        self.thresholdDict = {}
        self.image_loop_time = image_loop_time  # time spent on each image when looping over a directory

        if parameters_json:
            try:
                with open(parameters_json) as f:
                    self.thresholdDict = json.load(f)
                    self.exgMin = self.thresholdDict['exgMin']
                    self.exgMax = self.thresholdDict['exgMax']
                    self.hueMin = self.thresholdDict['hueMin']
                    self.hueMax = self.thresholdDict['hueMax']
                    self.saturationMin = self.thresholdDict['saturationMin']
                    self.saturationMax = self.thresholdDict['saturationMax']
                    self.brightnessMin = self.thresholdDict['brightnessMin']
                    self.brightnessMax = self.thresholdDict['brightnessMax']
                    print('[INFO] Parameters successfully loaded.')

            except FileExistsError:
                print('[ERROR] Parameters file not found. Continuing with default settings.')

            except KeyError:
                print('[ERROR] Parameter key not found. Continuing with default settings.')

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
            self.logger.log_line(f"[WARNING] Resolution {resolution} selected is dangerously high. Resolution has been reset to default to avoid damaging the OWL",
                                 verbose=True)

        # instantiate the recorder if recording is True
        if self.recording:
            self.fourcc = cv2.VideoWriter_fourcc(*'MJPG')
            self.writer = None

        else:
            self.record = False
            self.saveRecording = False

        # check if test video or videostream from camera
        if input_file_or_directory:
            self.cam = FrameReader(path=input_file_or_directory,
                                   resolution=self.resolution,
                                   loop_time=self.image_loop_time)
            self.frame_width, self.frame_height = self.cam.resolution

            self.logger.log_line(f'[INFO] Using {self.cam.input_type} from {input_file_or_directory}...', verbose=True)

        # if no video, start the camera with the provided parameters
        else:
            try:
                from picamera import PiCameraMMALError
                
            except ImportError:
                PiCameraMMALError = None
            
            try:
                self.cam = VideoStream(usePiCamera=True,
                                       resolution=self.resolution,
                                       framerate=self.framerate,
                                       exposure_mode=self.exp_mode,
                                       awb_mode=self.awb_mode,
                                       sensor_mode=self.sensor_mode,
                                       exposure_compensation=self.exp_compensation).start()
                
                self.frame_width = self.resolution[0]  #
                self.frame_height = self.resolution[1]  #

                # save camera settings to the log
                self.logger.log_line('[INFO] Camera setup complete. Settings: '
                                     f'\nResolution: {self.resolution}'
                                     f'\nFramerate: {self.framerate}'
                                     f'\nExposure Mode: {self.exp_mode}'
                                     f'\nAutoWhiteBalance: {self.awb_mode}'
                                     f'\nExposure Compensation: {self.exp_compensation}'
                                     f'\nSensor Mode: {self.sensor_mode}', verbose=True)

            except ModuleNotFoundError as e:
                self.cam = VideoStream(src=0).start()
                self.frame_width = self.cam.stream.get(cv2.CAP_PROP_FRAME_WIDTH)
                self.frame_height = self.cam.stream.get(cv2.CAP_PROP_FRAME_HEIGHT)
                self.logger.log_line('[INFO] Camera setup complete. Using inbuilt webcam...', verbose=True)

            except PiCameraMMALError as e:
                self.logger.log_line(f"[CAMERA ERROR] Note, camera is in use by another OWL process.\n"
                                     f"To resolve this error, follow these steps:\n1. Use <ps -C owl.py> to find the process\n"
                                     f"2. Run <sudo kill PID_NUM>\n"
                                     f"This will close the other process using its PID.\n"
                                     f"Original error message: {str(e)}", verbose=True)

                self.controller.solenoid.beep(duration=0.1, repeats=3)
                time.sleep(2)
                sys.exit()

            except Exception as e:
                self.logger.log_line(f"[CRITICAL ERROR] STOPPED OWL AT START: {e}", verbose=True)
                
                self.controller.solenoid.beep(duration=1, repeats=1)
                time.sleep(2)
                sys.exit()

        time.sleep(2.0)
        # set the sprayqueue size
        self.sprayQueue = Queue(maxsize=10)

        ### Data collection only ###
        # this is where a recording button can be added. Currently set to pin 37
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
        self.yAct = int(0.2 * self.frame_height)
        self.laneWidth = self.frame_width / self.nozzleNum

        # calculate lane coords and draw on frame
        for i in range(self.nozzleNum):
            laneX = int(i * self.laneWidth)
            self.laneCoords[i] = laneX

    def hoot(self,
             sprayDur,
             delay,
             sampleMethod=None,
             sampleFreq=60,
             saveDir='output',
             camera_name='cam1',
             algorithm='exg',
             model_path='models/',
             confidence=0.5,
             minArea=10,
             log_fps=False,
             invert_hue=False):

        # track FPS and framecount
        frameCount = 0
        if sampleMethod is not None:
            if not os.path.exists(saveDir):
                os.makedirs(saveDir)

        if log_fps:
            fps = FPS().start()

        try:
            if algorithm == 'gog':
                from greenongreen import GreenOnGreen
                weed_detector = GreenOnGreen(model_path=model_path)

            else:
                weed_detector = GreenOnBrown(algorithm=algorithm)

        except (ModuleNotFoundError, IndexError, FileNotFoundError, ValueError) as e:
            self._handle_exceptions(e, algorithm)

        except Exception as e:
            self.logger.log_line(
                f"\n[ALGORITHM ERROR] Unrecognised error while starting algorithm: {algorithm}.\nError message: {e}", verbose=True)
            sys.exit()

        self.nozzle_vis = self.controller.nozzle_vis
        self.nozzle_vis.setup()
        self.controller.vis = True

        try:
            while True:
                delay = self.update_delay(delay)
                frame = self.cam.read()

                if self.focus:
                    grey = cv2.cvtColor(frame.copy(), cv2.COLOR_BGR2GRAY)
                    blurriness = fft_blur(grey, size=30)

                if self.recording:
                    self.record = self.recorderButton.record
                    self.saveRecording = self.recorderButton.saveRecording

                if frame is None:
                    if log_fps:
                        fps.stop()
                        print(f"[INFO] Stopped. Approximate FPS: {fps.fps():.2f}")
                        self.stop()
                        break
                    else:
                        print("[INFO] Frame is None. Stopped.")
                        self.stop()
                        break

                if self.record and self.writer is None:
                    saveDir = os.path.join(saveDir, strftime(f"%Y%m%d-{camera_name}-{algorithm}"))
                    if not os.path.exists(saveDir):
                        os.makedirs(saveDir)

                    self.baseName = os.path.join(saveDir, strftime(f"%Y%m%d-%H%M%S-{camera_name}-{algorithm}"))
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
                if algorithm == 'gog':
                    cnts, boxes, weedCentres, imageOut = weed_detector.inference(frame.copy(),
                                                                                 confidence=confidence,
                                                                                 filter_id=63)
                else:
                    cnts, boxes, weedCentres, imageOut = weed_detector.inference(frame.copy(), exgMin=self.exgMin,
                                                                                 exgMax=self.exgMax,
                                                                                 hueMin=self.hueMin,
                                                                                 hueMax=self.hueMax,
                                                                                 saturationMin=self.saturationMin,
                                                                                 saturationMax=self.saturationMax,
                                                                                 brightnessMin=self.brightnessMin,
                                                                                 brightnessMax=self.brightnessMax,
                                                                                 show_display=self.show_display,
                                                                                 algorithm=algorithm,
                                                                                 minArea=minArea,
                                                                                 invert_hue=invert_hue)

                ##### IMAGE SAMPLER #####
                # record sample images if required of weeds detected. sampleFreq specifies how often
                if sampleMethod is not None:
                    # only record every sampleFreq number of frames. If sampleFreq = 60, this will activate every 60th frame
                    if frameCount % sampleFreq == 0:
                        saveFrame = frame.copy()

                        if sampleMethod == 'whole':
                            whole_image_thread = Thread(target=whole_image_save,
                                                        args=[saveFrame, saveDir, frameCount])
                            whole_image_thread.start()

                        elif sampleMethod == 'bbox':
                            sample_thread = Thread(target=bounding_box_image_sample,
                                                   args=[saveFrame, boxes, saveDir, frameCount])
                            sample_thread.start()

                        elif sampleMethod == 'square':
                            sample_thread = Thread(target=square_image_sample,
                                                   args=[saveFrame, weedCentres, saveDir, frameCount, 200])
                            sample_thread.start()

                        else:
                            # if nothing/incorrect specified - sample the whole image
                            whole_image_thread = Thread(target=whole_image_save,
                                                        args=[imageOut, saveDir, frameCount])
                            whole_image_thread.start()


                    frameCount += 1
                # ########################

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
                    cv2.putText(imageOut, f'OWL-gorithm: {algorithm}', (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.75,
                                (80, 80, 255), 1)
                    cv2.putText(imageOut, f'Press "S" to save {algorithm} thresholds to file.',
                                (20, int(imageOut.shape[1 ] *0.72)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 80, 255), 1)
                    if self.focus:
                        cv2.putText(imageOut, f'Blurriness: {blurriness:.2f}', (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 1,
                                    (80, 80, 255), 1)

                    cv2.imshow("Detection Output", imutils.resize(imageOut, width=600))

                if self.record and not self.saveRecording:
                    self.writer.write(frame)

                if self.saveRecording and not self.record:
                    self.writer.release()
                    self.controller.solenoid.beep(duration=0.1)
                    self.recorderButton.saveRecording = False
                    if log_fps:
                        fps.stop()
                        self.logger.log_line_video(f"[INFO] Approximate FPS: {fps.fps():.2f}", verbose=True)
                        fps = FPS().start()

                    self.writer = None
                    self.logger.log_line_video(f"[INFO] {self.baseName} stopped.", verbose=True)

                k = cv2.waitKey(1) & 0xFF
                if k == ord('s'):
                    self.save_parameters()
                    self.logger.log_line("[INFO] Parameters saved.", verbose=True)

                if k == 27:
                    if log_fps:
                        fps.stop()
                        self.logger.log_line_video(f"[INFO] Approximate FPS: {fps.fps():.2f}", verbose=True)
                    self.controller.nozzle_vis.close()
                    self.logger.log_line("[INFO] Stopped.", verbose=True)
                    self.stop()
                    break

        except KeyboardInterrupt:
            if log_fps:
                fps.stop()
                self.logger.log_line(f"[INFO] Approximate FPS: {fps.fps():.2f}", verbose=True)
            self.controller.nozzle_vis.close()
            self.logger.log_line("[INFO] Stopped.", verbose=True)
            self.stop()

        except Exception as e:
            print(e)
            self.controller.solenoid.beep(duration=0.5, repeats=5)
            self.logger.log_line(f"[CRITICAL ERROR] STOPPED: {e}")

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

    def _handle_exceptions(self, e, algorithm):
        # handle exceptions cleanly
        error_type = type(e).__name__
        error_message = str(e)

        if isinstance(e, ModuleNotFoundError):
            detailed_message = f"\nIs pycoral correctly installed? Visit: https://coral.ai/docs/accelerator/get-started/#requirements"

        elif isinstance(e, (IndexError, FileNotFoundError)):
            detailed_message = "\nAre there model files in the 'models' directory?"

        elif isinstance(e, ValueError) and 'delegate' in error_message:
            detailed_message = (
                "\nThis is due to an unrecognised Google Coral device. Please make sure it is connected correctly.\n"
                "If the error persists, try unplugging it and plugging it again or restarting the\n"
                "Raspberry Pi. For more information visit:\nhttps://github.com/tensorflow/tensorflow/issues/32743")

        else:
            detailed_message = ""

        full_message = f"\n[{error_type}] while starting algorithm: {algorithm}.\nError message: {error_message}{detailed_message}"

        self.logger.log_line(full_message, verbose=True)
        self.controller.solenoid.beep(duration=0.25, repeats=4)
        sys.exit()

    def save_parameters(self):
        self.thresholdDict['exgMin'] = cv2.getTrackbarPos("ExG-Min", self.window_name)
        self.thresholdDict['exgMax'] = cv2.getTrackbarPos("ExG-Max", self.window_name)
        self.thresholdDict['hueMin'] = cv2.getTrackbarPos("Hue-Min", self.window_name)
        self.thresholdDict['hueMax'] = cv2.getTrackbarPos("Hue-Max", self.window_name)
        self.thresholdDict['saturationMin'] = cv2.getTrackbarPos("Sat-Min", self.window_name)
        self.thresholdDict['saturationMax'] = cv2.getTrackbarPos("Sat-Max", self.window_name)
        self.thresholdDict['brightnessMin'] = cv2.getTrackbarPos("Bright-Min", self.window_name)
        self.thresholdDict['brightnessMax'] = cv2.getTrackbarPos("Bright-Max", self.window_name)

        datetime.now(timezone.utc).strftime("%Y%m%d")
        json_name = datetime.now(timezone.utc).strftime("%Y%m%d%H%M") + '-owl-parameters.json'
        with open(json_name, 'w') as f:
            json.dump(self.thresholdDict, f)


# business end of things
if __name__ == "__main__":
    # these command line arguments enable people to operate/change some settings from the command line instead of
    # opening up the OWL code each time.
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', type=str, default=None, help='path to image directory, single image or video file')
    ap.add_argument('--show-display', action='store_true', default=False, help='show display windows')
    ap.add_argument('--focus', action='store_true', default=False, help='add FFT blur to output frame')
    ap.add_argument('--recording', action='store_true', default=False, help='record video')
    ap.add_argument('--algorithm', type=str, default='exhsv', choices=['exg', 'nexg', 'exgr', 'maxg', 'exhsv', 'hsv', 'gog'])
    ap.add_argument('--model-path', type=str, default=None, help='path to trained weed detection .tflite model or directory')
    ap.add_argument('--conf', type=float, default=0.5, choices=np.arange(0.01, 0.99, 0.01), metavar="2 s.f. Float between 0.01 and 1.00",
                    help='set the confidence value for a "green-on-green" algorithm between 0.01 and 1.00. Must be a two-digit float.')
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
    ap.add_argument('--exp-compensation', type=int, default=-4, choices=range(-24, 24), metavar="[-24 to 24]",
                    help='set the exposure compensation (EV) for the camera between -24 and 24. '
                         'Raspberry Pi cameras seem to overexpose images preferentially.')
    args = ap.parse_args()

    owl = Owl(input_file_or_directory=args.input,
              show_display=args.show_display,
              focus=args.focus,
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
              sensor_mode=args.sensor_mode,
              parameters_json=None,
              image_loop_time=5)

    # start the targeting!
    owl.hoot(sprayDur=0.15,
             delay=0,
             sampleMethod=None,  # choose from 'bbox' | 'square' | 'whole'. If sampleMethod=None, it won't sample anything
             sampleFreq=30,  # select how often to sample - number of frames to skip.
             saveDir='images/bbox2',
             algorithm=args.algorithm,
             model_path=args.model_path,
             camera_name='hsv',
             minArea=10,
             confidence=args.conf,
             invert_hue=False  # invert the hue threshold - useful for excluding green to find red/purple stems
             )
