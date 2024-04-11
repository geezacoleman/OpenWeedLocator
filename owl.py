#!/home/pi/.virtualenvs/owl/bin/python3
from button_inputs import Recorder
from image_sampler import bounding_box_image_sample, square_image_sample, whole_image_save
from utils.blur_algorithms import fft_blur
from greenonbrown import GreenOnBrown
from relay_control import Controller
from utils.frame_reader import FrameReader

from configparser import ConfigParser
from pathlib import Path
from datetime import datetime
from imutils.video import FPS
from utils.video import VideoStream
from time import strftime
from threading import Thread

import argparse
import imutils
import time
import sys
import cv2
import os


def nothing(x):
    pass


class Owl:
    def __init__(self, show_display=False,
                 focus=False,
                 input_file_or_directory=None,
                 config_file='config/DAY_SENSITIVITY_2.ini'):

        # start by reading the config file
        self._config_path = Path(__file__).parent / config_file
        self.config = ConfigParser()
        self.config.read(self._config_path)

        # is the source a directory/file
        self.input_file_or_directory = input_file_or_directory

        # different detection parameters
        self.show_display = show_display
        self.relay_vis = None
        self.recording = self.config.getboolean('DataCollection', 'recording')
        self.focus = focus
        if self.focus:
            self.show_display = True

        self.resolution = (self.config.getint('Camera', 'resolution_width'),
                           self.config.getint('Camera', 'resolution_height'))
        self.exp_compensation = self.config.getint('Camera', 'exp_compensation')

        # threshold parameters for different algorithms
        self.exgMin = self.config.getint('GreenOnBrown', 'exgMin')
        self.exgMax = self.config.getint('GreenOnBrown', 'exgMax')
        self.hueMin = self.config.getint('GreenOnBrown', 'hueMin')
        self.hueMax = self.config.getint('GreenOnBrown', 'hueMax')
        self.saturationMin = self.config.getint('GreenOnBrown', 'saturationMin')
        self.saturationMax = self.config.getint('GreenOnBrown', 'saturationMax')
        self.brightnessMin = self.config.getint('GreenOnBrown', 'brightnessMin')
        self.brightnessMax = self.config.getint('GreenOnBrown', 'brightnessMax')

        self.threshold_dict = {}
        self.image_loop_time = 5  # time spent on each image when looping over a directory

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

        # Relay Dict maps the reference relay number to a boardpin on the embedded device
        self.relay_dict = {}

        # use the [Relays] section to build the dictionary
        for key, value in self.config['Relays'].items():
            self.relay_dict[int(key)] = int(value)

        # instantiate the relay controller - successful start should beep the buzzer
        self.controller = Controller(relay_dict=self.relay_dict)

        # instantiate the logger
        self.logger = self.controller.logger

        # check that the resolution is not so high it will entirely brick/destroy the OWL.
        total_pixels = self.resolution[0] * self.resolution[1]
        if total_pixels > (832 * 640):
            # change here if you want to test higher resolutions, but be warned, backup your current image!
            self.resolution = (416, 320)
            self.logger.log_line(f"[WARNING] Resolution {self.config.getint('Camera', 'resolution_width')}, "
                                 f"{self.config.getint('Camera', 'resolution_height')} selected is dangerously high. "
                                 f"Resolution has been reset to default to avoid damaging the OWL",
                                 verbose=True)

        # instantiate the recorder if recording is True
        if self.recording:
            self.fourcc = cv2.VideoWriter_fourcc(*'MJPG')
            self.writer = None

        else:
            self.record = False
            self.save_recording = False

        # check if test video or videostream from camera
        self.input_file_or_directory = self.config.get('System', 'input_file_or_directory')

        if self.input_file_or_directory:
            self.cam = FrameReader(path=self.input_file_or_directory,
                                   resolution=self.resolution,
                                   loop_time=self.image_loop_time)
            self.frame_width, self.frame_height = self.cam.resolution

            self.logger.log_line(f'[INFO] Using {self.cam.input_type} from {self.input_file_or_directory}...', verbose=True)

        # if no video, start the camera with the provided parameters
        else:

            try:
                self.cam = VideoStream(resolution=self.resolution,
                                       exp_compensation=self.exp_compensation)
                self.cam.start()

                self.frame_width = self.cam.frame_width
                self.frame_height = self.cam.frame_height

            except ModuleNotFoundError as e:
                missing_module = str(e).split("'")[-2]
                error_message = f"Missing required module: {missing_module}. Please install it and try again."

                raise ModuleNotFoundError(error_message) from None

            except Exception as e:
                error_detail = f"[CRITICAL ERROR] Stopped OWL at start: {e}"
                self.logger.log_line(error_detail, verbose=True)
                self.controller.relay.beep(duration=1, repeats=1)
                time.sleep(2)

                sys.exit(1)

        time.sleep(1.0)

        ### Data collection only ###
        # this is where a recording button can be added. Currently set to pin 37
        if self.recording:
            self.recorder_button = Recorder(recordGPIO=37)
        ############################

        # sensitivity and weed size to be added
        self.sensitivity = None
        self.lane_coords = {}

        # add the total number of relays being controlled. This can be changed easily, but the relay_dict and physical relays would need
        # to be updated too. Fairly straightforward, so an opportunity for more precise application
        self.relay_num = self.config.getint('System', 'relay_num')

        # activation region limit - once weed crosses this line, relay is activated
        self.yAct = int(0.01 * self.frame_height)
        self.lane_width = self.frame_width / self.relay_num

        # calculate lane coords and draw on frame
        for i in range(self.relay_num):
            laneX = int(i * self.lane_width)
            self.lane_coords[i] = laneX

    def hoot(self):
        algorithm = self.config.get('System', 'algorithm')

        log_fps = self.config.getboolean('DataCollection', 'log_fps')

        sample_method = self.config.get('DataCollection', 'sample_method')
        sample_frequency = self.config.getint('DataCollection', 'sample_frequency')
        save_directory = self.config.get('DataCollection', 'save_directory')
        camera_name = self.config.get('DataCollection', 'camera_name')

        # track FPS and framecount
        frame_count = 0
        if sample_method is not None:
            if not os.path.exists(save_directory):
                os.makedirs(save_directory)

        if log_fps:
            fps = FPS().start()

        try:
            if algorithm == 'gog':
                from greenongreen import GreenOnGreen
                model_path = self.config.get('GreenOnGreen', 'model_path')
                confidence = self.config.getfloat('GreenOnGreen', 'confidence')

                weed_detector = GreenOnGreen(model_path=model_path)

            else:
                min_detection_area = self.config.getint('GreenOnBrown', 'min_detection_area')
                invert_hue = self.config.getboolean('GreenOnBrown', 'invert_hue')

                weed_detector = GreenOnBrown(algorithm=algorithm)

        except (ModuleNotFoundError, IndexError, FileNotFoundError, ValueError) as e:
            self._handle_exceptions(e, algorithm)

        except Exception as e:
            self.logger.log_line(
                f"\n[ALGORITHM ERROR] Unrecognised error while starting algorithm: {algorithm}.\nError message: {e}", verbose=True)
            sys.exit()

        self.relay_vis = self.controller.relay_vis
        self.relay_vis.setup()
        self.controller.vis = True

        try:
            actuation_duration = self.config.getfloat('System', 'actuation_duration')
            delay = self.config.getfloat('System', 'delay')

            while True:
                delay = self.update_delay(delay)
                frame = self.cam.read()

                if self.focus:
                    grey = cv2.cvtColor(frame.copy(), cv2.COLOR_BGR2GRAY)
                    blurriness = fft_blur(grey, size=30)

                if self.recording:
                    self.record = self.recorder_button.record
                    self.save_recording = self.recorder_button.save_recording

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
                    save_directory = os.path.join(save_directory, strftime(f"%Y%m%d-{camera_name}-{algorithm}"))
                    if not os.path.exists(save_directory):
                        os.makedirs(save_directory)

                    self.base_name = os.path.join(save_directory, strftime(f"%Y%m%d-%H%M%S-{camera_name}-{algorithm}"))
                    video_name = self.base_name + '.avi'
                    self.logger.new_video_logfile(name=self.base_name + '.txt')
                    self.writer = cv2.VideoWriter(video_name, self.fourcc, 30, (frame.shape[1], frame.shape[0]), True)

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
                    cnts, boxes, weed_centres, image_out = weed_detector.inference(frame.copy(),
                                                                                   confidence=confidence,
                                                                                   filter_id=63)
                else:
                    cnts, boxes, weed_centres, image_out = weed_detector.inference(frame.copy(),
                                                                                   exgMin=self.exgMin,
                                                                                   exgMax=self.exgMax,
                                                                                   hueMin=self.hueMin,
                                                                                   hueMax=self.hueMax,
                                                                                   saturationMin=self.saturationMin,
                                                                                   saturationMax=self.saturationMax,
                                                                                   brightnessMin=self.brightnessMin,
                                                                                   brightnessMax=self.brightnessMax,
                                                                                   show_display=self.show_display,
                                                                                   algorithm=algorithm,
                                                                                   min_detection_area=min_detection_area,
                                                                                   invert_hue=invert_hue,
                                                                                   label='WEED')

                ##### IMAGE SAMPLER #####
                # record sample images if required of weeds detected. sampleFreq specifies how often
                if sample_method is not None:
                    # only record every sampleFreq number of frames. If sampleFreq = 60, this will activate every 60th frame
                    if frame_count % sample_frequency == 0:
                        save_frame = frame.copy()

                        if sample_method == 'whole':
                            whole_image_thread = Thread(target=whole_image_save,
                                                        args=[save_frame, save_directory, frame_count])
                            whole_image_thread.start()

                        elif sample_method == 'bbox':
                            sample_thread = Thread(target=bounding_box_image_sample,
                                                   args=[save_frame, boxes, save_directory, frame_count])
                            sample_thread.start()

                        elif sample_method == 'square':
                            sample_thread = Thread(target=square_image_sample,
                                                   args=[save_frame, weed_centres, save_directory, frame_count, 200])
                            sample_thread.start()

                        else:
                            # if nothing/incorrect specified - sample the whole image
                            whole_image_thread = Thread(target=whole_image_save,
                                                        args=[image_out, save_directory, frame_count])
                            whole_image_thread.start()


                    frame_count += 1
                # ########################

                # loop over the ID/weed centres from contours
                for ID, centre in enumerate(weed_centres):
                    # if they are in activation region then actuate
                    if centre[1] > self.yAct:
                        actuation_time = time.time()
                        for i in range(self.relay_num):
                            # determine which lane needs to be activated
                            if int(self.lane_coords[i]) <= centre[0] < int(self.lane_coords[i] + self.lane_width):
                                # log a weed control job with the controller using the relay, delay, timestamp and actuation duration
                                # if GPS is used/speed control, delay can be updated automatically based on forward speed
                                self.controller.receive(relay=i, delay=delay, time_stamp=actuation_time, duration=actuation_duration)

                # update the framerate counter
                if log_fps:
                    fps.update()

                if self.show_display:
                    cv2.putText(image_out, f'OWL-gorithm: {algorithm}', (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.75,
                                (80, 80, 255), 1)
                    cv2.putText(image_out, f'Press "S" to save {algorithm} thresholds to file.',
                                (20, int(image_out.shape[1 ] *0.72)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 80, 255), 1)
                    if self.focus:
                        cv2.putText(image_out, f'Blurriness: {blurriness:.2f}', (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 1,
                                    (80, 80, 255), 1)

                    cv2.imshow("Detection Output", imutils.resize(image_out, width=600))

                if self.record and not self.save_recording:
                    self.writer.write(frame)

                if self.save_recording and not self.record:
                    self.writer.release()
                    self.controller.relay.beep(duration=0.1)
                    self.recorder_button.save_recording = False
                    if log_fps:
                        fps.stop()
                        self.logger.log_line_video(f"[INFO] Approximate FPS: {fps.fps():.2f}", verbose=True)
                        fps = FPS().start()

                    self.writer = None
                    self.logger.log_line_video(f"[INFO] {self.base_name} stopped.", verbose=True)

                k = cv2.waitKey(1) & 0xFF
                if k == ord('s'):
                    self.save_parameters()
                    self.logger.log_line("[INFO] Parameters saved.", verbose=True)

                if k == 27:
                    if log_fps:
                        fps.stop()
                        self.logger.log_line_video(f"[INFO] Approximate FPS: {fps.fps():.2f}", verbose=True)
                    self.controller.relay_vis.close()
                    self.logger.log_line("[INFO] Stopped.", verbose=True)
                    self.stop()
                    break

        except KeyboardInterrupt:
            if log_fps:
                fps.stop()
                self.logger.log_line(f"[INFO] Approximate FPS: {fps.fps():.2f}", verbose=True)
            self.controller.relay_vis.close()
            self.logger.log_line("[INFO] Stopped.", verbose=True)
            self.stop()

        except Exception as e:
            print(e)
            self.controller.relay.beep(duration=0.5, repeats=5)
            self.logger.log_line(f"[CRITICAL ERROR] STOPPED: {e}")

    def stop(self):
        self.controller.running = False
        self.controller.relay.all_off()
        self.controller.relay.beep(duration=0.1)
        self.controller.relay.beep(duration=0.1)
        self.cam.stop()
        if self.record:
            self.writer.release()
            self.recorder_button.running = False

        if self.show_display:
            cv2.destroyAllWindows()

        sys.exit()

    def update(self, exgMin=30, exgMax=180):
        self.exgMin = exgMin
        self.exgMax = exgMax

    def update_delay(self, delay=0):
        # if GPS added, could use it here to return a delay variable based on speed.
        return delay

    def save_parameters(self):
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        new_config_filename = f"{timestamp}_{self._config_path.name}"
        new_config_path = self._config_path.parent / new_config_filename

        # Update the 'GreenOnBrown' section with current attribute values
        if 'GreenOnBrown' not in self.config.sections():
            self.config.add_section('GreenOnBrown')

        self.config.set('GreenOnBrown', 'exgMin', str(self.exgMin))
        self.config.set('GreenOnBrown', 'exgMax', str(self.exgMax))
        self.config.set('GreenOnBrown', 'hueMin', str(self.hueMin))
        self.config.set('GreenOnBrown', 'hueMax', str(self.hueMax))
        self.config.set('GreenOnBrown', 'saturationMin', str(self.saturationMin))
        self.config.set('GreenOnBrown', 'saturationMax', str(self.saturationMax))
        self.config.set('GreenOnBrown', 'brightnessMin', str(self.brightnessMin))
        self.config.set('GreenOnBrown', 'brightnessMax', str(self.brightnessMax))

        # Write the updated configuration to the new file with a timestamped filename
        with open(new_config_path, 'w') as configfile:
            self.config.write(configfile)

        print(f"Configuration saved to {new_config_path}")

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
        self.controller.relay.beep(duration=0.25, repeats=4)
        sys.exit()


# business end of things
if __name__ == "__main__":
    # these command line arguments enable people to operate/change some settings from the command line instead of
    # opening up the OWL code each time.
    ap = argparse.ArgumentParser()
    ap.add_argument('--show-display', action='store_true', default=False, help='show display windows')
    ap.add_argument('--focus', action='store_true', default=False, help='add FFT blur to output frame')
    ap.add_argument('--input', type=str, default=None, help='path to image directory, single image or video file')

    args = ap.parse_args()

    owl = Owl(show_display=args.show_display,
              focus=args.focus,
              input_file_or_directory=args.input)

    # start the targeting!
    owl.hoot()
