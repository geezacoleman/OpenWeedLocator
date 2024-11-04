#!/usr/bin/env python
import logging

from utils.error_manager import OWLConfigError
from utils.input_manager import UteController, AdvancedController, get_rpi_version
from utils.output_manager import RelayController, HeadlessStatusIndicator, UteStatusIndicator, AdvancedStatusIndicator
from utils.directory_manager import DirectorySetup
from utils.video_manager import VideoStream
from utils.image_sampler import ImageRecorder
from utils.algorithms import fft_blur
from utils.greenonbrown import GreenOnBrown
from utils.frame_reader import FrameReader
from utils.config_manager import ConfigValidator

import utils.error_manager as errors

from multiprocessing import Value, Process
from configparser import ConfigParser
from pathlib import Path
from datetime import datetime
from imutils.video import FPS

import warnings
import argparse
import imutils
import time
import sys
import cv2


def nothing(x):
    pass


class Owl:
    def __init__(self, show_display=False,
                 focus=False,
                 input_file_or_directory=None,
                 config_file='config/DAY_SENSITIVITY_2.ini'):

        # start by reading the config file
        self._config_path = Path(__file__).parent / config_file
        try:
            self.config = ConfigValidator.load_and_validate_config(self._config_path)
        except OWLConfigError as e:
            logging.error(f"Configuration error: {e}")
            raise

        self.config.read(self._config_path)
        self.RPI_VERSION = get_rpi_version()
        # is the source a directory/file
        self.input_file_or_directory = input_file_or_directory

        # visualise the detections with video feed
        self.show_display = show_display
        self.focus = focus

        if self.focus:
            self.show_display = True

        # threshold parameters for different algorithms
        self.exgMin = self.config.getint('GreenOnBrown', 'exgMin')
        self.exgMax = self.config.getint('GreenOnBrown', 'exgMax')
        self.hueMin = self.config.getint('GreenOnBrown', 'hueMin')
        self.hueMax = self.config.getint('GreenOnBrown', 'hueMax')
        self.saturationMin = self.config.getint('GreenOnBrown', 'saturationMin')
        self.saturationMax = self.config.getint('GreenOnBrown', 'saturationMax')
        self.brightnessMin = self.config.getint('GreenOnBrown', 'brightnessMin')
        self.brightnessMax = self.config.getint('GreenOnBrown', 'brightnessMax')

        # time spent on each image when looping over a directory
        self.image_loop_time = self.config.getint('Visualisation', 'image_loop_time')

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

        self.resolution = (self.config.getint('Camera', 'resolution_width'),
                           self.config.getint('Camera', 'resolution_height'))
        self.exp_compensation = self.config.getint('Camera', 'exp_compensation')

        # Relay Dict maps the reference relay number to a boardpin on the embedded device
        self.relay_dict = {}

        # use the [Relays] section to build the dictionary
        for key, value in self.config['Relays'].items():
            self.relay_dict[int(key)] = int(value)

        # instantiate the relay controller - successful start should beep the buzzer
        self.relay_controller = RelayController(relay_dict=self.relay_dict)

        # instantiate the logger
        self.logger = self.relay_controller.logger

        ### Data collection only ###
        # WARNING: initialise option disable detection for data collection
        self.disable_detection = False
        self.save_directory = None

        # if a controller is connected, sample images must be true to set up directories correctly
        self.controller_type = self.config.get('Controller', 'controller_type').strip("'\" ").lower()

        if self.controller_type not in {'none', 'ute', 'advanced'}:
            raise errors.ControllerTypeError(self.config.get('Controller', 'controller_type'))

        if self.controller_type != 'none':
            self.sample_images = True
        else:
            self.sample_images = self.config.getboolean('DataCollection', 'sample_images')

        # if controller is 'none' but sample_images is True, then it will set it up still
        if self.sample_images:
            self.sample_method = self.config.get('DataCollection', 'sample_method')
            self.disable_detection = self.config.getboolean('DataCollection', 'disable_detection')
            self.sample_frequency = self.config.getint('DataCollection', 'sample_frequency')
            self.save_directory = self.config.get('DataCollection', 'save_directory')
            self.camera_name = self.config.get('DataCollection', 'camera_name')

            self.directory_manager = DirectorySetup(save_directory=self.save_directory)
            self.save_directory, self.save_subdirectory = self.directory_manager.setup_directories()

            self.image_recorder = ImageRecorder(save_directory=self.save_subdirectory, mode=self.sample_method)
        ############################

        # initialise controller buttons and async management
        if self.controller_type != 'none':
            self.detection_state = Value('b', False)
            self.sample_state = Value('b', False)
            self.stop_flag = Value('b', False)

            # 'ute controller' that fits in a cupholder. Only one switch to toggle recording OR detection on/off.
            if self.controller_type == 'ute':
                self.status_indicator = UteStatusIndicator(
                    save_directory=self.save_directory,
                    record_led_pin='BOARD38',
                    storage_led_pin='BOARD40')

                self.switch_purpose = self.config.get('Controller', 'switch_purpose').lower()
                self.switch_pin = self.config.getint('Controller', 'switch_pin')

                self.controller = UteController(
                    detection_state=self.detection_state,
                    sample_state=self.sample_state,
                    stop_flag=self.stop_flag,
                    owl_instance=self,
                    status_indicator=self.status_indicator,
                    switch_board_pin=f'BOARD{self.switch_pin}',
                    switch_purpose=self.switch_purpose
                )

            # The 'advanced' controller. Controls multiple inputs.
            elif self.controller_type == 'advanced':
                self.status_indicator = AdvancedStatusIndicator(save_directory=self.save_directory,
                                                                status_led_pin='BOARD37')

                self.sensitivity_state = Value('b', False)
                self.detection_mode_state = Value('i', 1)  # Default to off (1)

                recording_pin = self.config.getint('Controller', 'recording_pin')
                sensitivity_pin = self.config.getint('Controller', 'sensitivity_pin')
                detection_mode_pin_up = self.config.getint('Controller', 'detection_mode_pin_up')
                detection_mode_pin_down = self.config.getint('Controller', 'detection_mode_pin_down')
                low_sensitivity_config = self.config.get('Controller', 'low_sensitivity_config')
                high_sensitivity_config = self.config.get('Controller', 'high_sensitivity_config')

                self.controller = AdvancedController(
                    recording_state=self.sample_state,
                    sensitivity_state=self.sensitivity_state,
                    detection_mode_state=self.detection_mode_state,
                    stop_flag=self.stop_flag,
                    owl_instance=self,
                    status_indicator=self.status_indicator,
                    low_sensitivity_config=low_sensitivity_config,
                    high_sensitivity_config=high_sensitivity_config,
                    recording_bpin=f'BOARD{recording_pin}',
                    sensitivity_bpin=f'BOARD{sensitivity_pin}',
                    detection_mode_bpin_up=f'BOARD{detection_mode_pin_up}',
                    detection_mode_bpin_down=f'BOARD{detection_mode_pin_down}'
                )

            else:
                raise ValueError(f"Invalid controller type: {self.controller_type}. "
                                 f"Select from None, Advanced or Ute in the config file.")

            self.controller_process = Process(target=self.controller.run)
            self.controller_process.start()
        else:
            self.controller = None
            if self.sample_images:
                self.status_indicator = HeadlessStatusIndicator(save_directory=self.save_directory)
                self.status_indicator.start_storage_indicator()

            else:
                self.status_indicator = HeadlessStatusIndicator(save_directory=None, no_save=True)

        self.relay_vis = None

        # Check which Raspberry Pi is being used and adjust the resolution accordingly.
        # Use `cat /proc-device-tree/model` to check the model of the Raspberry Pi.
        total_pixels = self.resolution[0] * self.resolution[1]

        if (self.RPI_VERSION in ['rpi-3', 'rpi-4']) and total_pixels > (832 * 640):
            # change here if you want to test higher resolutions, but be warned, backup your current image!
            self.resolution = (640, 480)
            warnings.warn(f"[WARNING] Resolution {self.config.getint('Camera', 'resolution_width')}, "
                                 f"{self.config.getint('Camera', 'resolution_height')} selected is dangerously high. ")
        else:
            warnings.warn(f'High resolution, expect low framerate. Resolution set to {self.resolution[0]}x{self.resolution[1]}.')

        # check if test video or videostream from camera
        # is the source a directory/file
        if len(self.config.get('System', 'input_file_or_directory')) > 0:
            self.input_file_or_directory = self.config.get('System', 'input_file_or_directory')

        self.input_file_or_directory = input_file_or_directory

        if len(self.config.get('System', 'input_file_or_directory')) > 0 and input_file_or_directory is not None:
            warnings.warn('[WARNING] two paths to image/videos provided. Defaulting to the command line flag.')

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
                self.status_indicator.error(1)
                time.sleep(2)
                raise ModuleNotFoundError(error_message) from None

            except Exception as e:
                error_detail = f"[CRITICAL ERROR] Stopped OWL at start: {e}"
                self.logger.log_line(error_detail, verbose=True)
                self.relay_controller.relay.beep(duration=1, repeats=1)
                self.status_indicator.error(1)
                time.sleep(5)

                sys.exit(1)

        time.sleep(1.0)

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
        self.record_video = False  # Flag to control video recording
        self.video_writer = None

        algorithm = self.config.get('System', 'algorithm')
        log_fps = self.config.getboolean('DataCollection', 'log_fps')
        if self.controller:
            self.controller.update_state()

        # track FPS and framecount
        frame_count = 0

        if log_fps:
            fps = FPS().start()

        try:
            if algorithm == 'gog':
                from utils.greenongreen import GreenOnGreen
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
            self.stop()

        if self.show_display:
            self.relay_vis = self.relay_controller.relay_vis
            self.relay_vis.setup()
            self.relay_controller.vis = True

        try:
            actuation_duration = self.config.getfloat('System', 'actuation_duration')
            delay = self.config.getfloat('System', 'delay')

            while True:
                frame = self.cam.read()

                if self.focus:
                    grey = cv2.cvtColor(frame.copy(), cv2.COLOR_BGR2GRAY)
                    blurriness = fft_blur(grey, size=30)

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

                # pass image, thresholds to green_on_brown function
                if not self.disable_detection:
                    if algorithm == 'gog':
                        cnts, boxes, weed_centres, image_out = weed_detector.inference(
                            frame,
                            confidence=confidence,
                            filter_id=63
                        )
                    else:
                        cnts, boxes, weed_centres, image_out = weed_detector.inference(
                            frame,
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
                            label='WEED'
                        )

                    # Precompute the integer lane coordinates for reuse
                    lane_coords_int = {k: int(v) for k, v in self.lane_coords.items()}

                    if len(weed_centres) > 0 and self.controller:
                        self.controller.weed_detect_indicator()

                    # loop over the weed centres
                    for centre in weed_centres:
                        if centre[1] > self.yAct:
                            actuation_time = time.time()
                            centre_x = centre[0]

                            for i in range(self.relay_num):
                                lane_start = lane_coords_int[i]
                                lane_end = lane_start + self.lane_width
                                if lane_start <= centre_x < lane_end:
                                    self.relay_controller.receive(
                                        relay=i,
                                        delay=delay,
                                        time_stamp=actuation_time,
                                        duration=actuation_duration)

                ##### IMAGE SAMPLER #####
                # record sample images if required of weeds detected. sampleFreq specifies how often
                if self.sample_images:
                    # only record every sampleFreq number of frames. If sample_frequency = 60, this will activate every 60th frame
                    if frame_count % self.sample_frequency == 0:
                        if self.sample_method == 'whole':
                            self.image_recorder.add_frame(frame=frame, frame_id=frame_count, boxes=None, centres=None)

                        elif self.sample_method != 'whole' and not self.disable_detection:
                            self.image_recorder.add_frame(frame=frame, frame_id=frame_count, boxes=boxes,
                                                          centres=weed_centres)
                        else:
                            self.image_recorder.add_frame(frame=frame, frame_id=frame_count, boxes=None, centres=None)

                        if self.controller:
                            self.status_indicator.image_write_indicator()

                        if self.status_indicator.DRIVE_FULL:
                            self.sample_images = False
                            self.image_recorder.stop()
                            self.status_indicator.error(5)

                frame_count = frame_count + 1 if frame_count < 900 else 1

                if log_fps and frame_count % 900 == 0:
                    fps.stop()
                    self.logger.log_line(f"[INFO] Approximate FPS: {fps.fps():.2f}", verbose=True)
                    fps = FPS().start()

                # update the framerate counter
                if log_fps:
                    fps.update()

                if self.show_display:
                    if self.disable_detection:
                        image_out = frame.copy()

                    if self.record_video:
                        if self.video_writer is None:
                            # Initialize video writer
                            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            video_filename = f"owl_recording_{timestamp}.mp4"
                            self.video_writer = cv2.VideoWriter(video_filename, fourcc, 30.0,
                                                                (frame.shape[1], frame.shape[0]))

                        # Write the frame with detections
                        self.video_writer.write(image_out)

                    cv2.putText(image_out, f'OWL-gorithm: {algorithm}', (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.75,
                                (80, 80, 255), 1)
                    cv2.putText(image_out, f'Press "S" to save {algorithm} thresholds to file.',
                                (20, int(image_out.shape[1 ] *0.72)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 80, 255), 1)
                    if self.focus:
                        cv2.putText(image_out, f'Blurriness: {blurriness:.2f}', (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 1,
                                    (80, 80, 255), 1)

                    cv2.imshow("Detection Output", imutils.resize(image_out, width=600))

                k = cv2.waitKey(1) & 0xFF
                if k == ord('s'):
                    self.save_parameters()
                    self.logger.log_line("[INFO] Parameters saved.", verbose=True)

                elif k == ord('r'):
                    # Toggle video recording
                    self.record_video = not self.record_video
                    if self.record_video:
                        print("[INFO] Started video recording.")
                    else:
                        if self.video_writer:
                            self.video_writer.release()
                            self.video_writer = None
                        print("[INFO] Stopped video recording.")

                elif k == 27:
                    if log_fps:
                        fps.stop()
                        self.logger.log_line(f"[INFO] Approximate FPS: {fps.fps():.2f}", verbose=True)
                    if self.show_display:
                        self.relay_controller.relay_vis.close()

                    self.logger.log_line("[INFO] Stopped.", verbose=True)
                    self.stop()
                    break

        except KeyboardInterrupt:
            if log_fps:
                fps.stop()
                self.logger.log_line(f"[INFO] Approximate FPS: {fps.fps():.2f}", verbose=True)
            if self.show_display:
                self.relay_controller.relay_vis.close()
            self.logger.log_line("[INFO] Stopped.", verbose=True)
            self.stop()

        except Exception as e:
            self.logger.log_line(f"[CRITICAL ERROR] STOPPED: {e}", verbose=True)
            self.stop()

    def stop(self):
        self.relay_controller.running = False
        self.relay_controller.relay.all_off()
        self.relay_controller.relay.beep(duration=0.1)
        self.relay_controller.relay.beep(duration=0.1)

        self.cam.stop()

        if self.video_writer:
            self.video_writer.release()

        if self.controller:
            if hasattr(self, 'controller'):
                self.controller.stop()
                if hasattr(self, 'controller_process'):
                    self.controller_process.join()

        if self.sample_images:
            self.status_indicator.stop()
            self.image_recorder.stop()

        if self.show_display:
            cv2.destroyAllWindows()

        sys.exit()

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

        print(f"[INFO] Configuration saved to {new_config_path}")

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
        self.relay_controller.relay.beep(duration=0.25, repeats=4)
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

    # this is where you can change the config file default
    owl = Owl(
        config_file='config/DAY_SENSITIVITY_2.ini',
        show_display=args.show_display,
        focus=args.focus,
        input_file_or_directory=args.input
    )

    # start the targeting!
    owl.hoot()
