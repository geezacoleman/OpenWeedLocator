#!/usr/bin/env python
import os
import sys

import logging
import argparse
import time
import threading
from collections import deque
from datetime import datetime
from multiprocessing import Process, Value
from pathlib import Path

from utils.error_manager import MJPEGStreamError


def get_python_env():
    """Get current Python environment status"""
    venv = os.environ.get('VIRTUAL_ENV')
    if venv:
        return f"Virtual environment: {venv}"
    return "No virtual environment active (using system Python)"


def setup_basic_logger():
    """Simple startup logger that uses the same file as LogManager"""
    log_dir = Path(os.getcwd()) / 'logs'
    log_dir.mkdir(exist_ok=True)

    file_handler = logging.FileHandler(log_dir / 'owl.jsonl')
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    return logging.getLogger('owl_startup')


logger = setup_basic_logger()
logger.info("Starting OWL - checking imports...")

try:
    import utils.error_manager as errors
except ImportError:
    logger.critical("Cannot import from utils package! Not in correct directory.")
    logger.critical(f"Current working directory: {os.getcwd()}")
    print("\nERROR: Cannot import from utils package!")
    print("This usually means you are not in the correct directory.")
    print("\nTo fix:")
    print("1. Ensure owl environment is active: workon owl")
    print("2. Navigate to owl directory:        cd /home/owl/owl")
    sys.exit(1)

try:
    import cv2
except ImportError as e:
    logger.error("OpenCV import failed - likely not in `owl` virtual environment")
    logger.error(f"Error details: {str(e)}")
    logger.error(f"Python environment: {get_python_env()}")
    raise errors.OpenCVError(str(e)) from None

try:
    import numpy as np
    from utils.input_manager import UteController, AdvancedController, get_rpi_version
    from utils.output_manager import RelayController, HeadlessStatusIndicator, UteStatusIndicator, AdvancedStatusIndicator
    from utils.directory_manager import DirectorySetup
    from utils.video_manager import VideoStream, StreamingHandler, ThreadedHTTPServer
    from utils.image_sampler import ImageRecorder
    from utils.algorithms import fft_blur
    from utils.greenonbrown import GreenOnBrown
    from utils.frame_reader import FrameReader
    from utils.config_manager import ConfigValidator
    from utils.log_manager import LogManager, MQTTLogHandler
    from utils.shared_types import Sensitivity
    import utils.error_manager as errors
    from version import SystemInfo, VERSION

except ImportError as e:
    missing_module = str(e).split("'")[1]
    logger.error(f"Failed to import required module: {missing_module}")
    logger.error(f"Error details: {str(e)}")
    logger.error(f"Current virtual env: {os.environ.get('VIRTUAL_ENV', 'None')}")
    logger.error(f"Current working directory: {os.getcwd()}")
    raise errors.DependencyError(missing_module, str(e)) from None

logger.info("All required modules imported successfully")


def nothing(x):
    pass


class Owl:
    def __init__(self, show_display=False,
                 input_file_or_directory=None,
                 config_file='config/DAY_SENSITIVITY_2.ini'):
        # set up the logger
        log_dir = Path(os.path.join(os.path.dirname(__file__), 'logs'))
        LogManager.setup(log_dir=log_dir, log_level='INFO')
        self.logger = LogManager.get_logger(__name__)

        self.logger.info("Initializing OWL...")
        self._log_system_info()

        # read the config file
        self._config_path = Path(__file__).parent / config_file
        try:
            self.config = ConfigValidator.load_and_validate_config(self._config_path)
        except errors.OWLConfigError as e:
            self.logger.error(f"Configuration error: {e}", exc_info=True)
            raise

        self.config.read(self._config_path)
        self.config.read(Path(__file__).parent / 'config' / 'CONTROLLER.ini')
        self.RPI_VERSION = get_rpi_version()
        self.logger.info(msg=f'Raspberry Pi version: {self.RPI_VERSION}')

        # is the source a directory/file
        self.input_file_or_directory = input_file_or_directory

        # visualise the detections with video feed
        self.show_display = show_display

        # threshold parameters for different algorithms
        self.exg_min = self.config.getint('GreenOnBrown', 'exg_min')
        self.exg_max = self.config.getint('GreenOnBrown', 'exg_max')
        self.hue_min = self.config.getint('GreenOnBrown', 'hue_min')
        self.hue_max = self.config.getint('GreenOnBrown', 'hue_max')
        self.saturation_min = self.config.getint('GreenOnBrown', 'saturation_min')
        self.saturation_max = self.config.getint('GreenOnBrown', 'saturation_max')
        self.brightness_min = self.config.getint('GreenOnBrown', 'brightness_min')
        self.brightness_max = self.config.getint('GreenOnBrown', 'brightness_max')

        # time spent on each image when looping over a directory
        self.image_loop_time = self.config.getint('Visualisation', 'image_loop_time')

        # setup the track bars if show_display is True
        if self.show_display:
            # create trackbars for the threshold calculation
            self.window_name = "Adjust Detection Thresholds"
            cv2.namedWindow("Adjust Detection Thresholds", cv2.WINDOW_AUTOSIZE)
            cv2.createTrackbar("ExG-Min", self.window_name, self.exg_min, 255, nothing)
            cv2.createTrackbar("ExG-Max", self.window_name, self.exg_max, 255, nothing)
            cv2.createTrackbar("Hue-Min", self.window_name, self.hue_min, 179, nothing)
            cv2.createTrackbar("Hue-Max", self.window_name, self.hue_max, 179, nothing)
            cv2.createTrackbar("Sat-Min", self.window_name, self.saturation_min, 255, nothing)
            cv2.createTrackbar("Sat-Max", self.window_name, self.saturation_max, 255, nothing)
            cv2.createTrackbar("Bright-Min", self.window_name, self.brightness_min, 255, nothing)
            cv2.createTrackbar("Bright-Max", self.window_name, self.brightness_max, 255, nothing)

        self.resolution = (self.config.getint('Camera', 'resolution_width'),
                           self.config.getint('Camera', 'resolution_height'))
        self.exp_compensation = self.config.getint('Camera', 'exp_compensation')

        # Relay Dict maps the reference relay number to a boardpin on the embedded device
        self.relay_dict = {}

        # use the [Relays] section to build the dictionary
        for key, value in self.config['Relays'].items():
            self.relay_dict[int(key)] = int(value)

        # instantiate the relay controller - successful start should beep the buzzer
        try:
            self.relay_controller = RelayController(relay_dict=self.relay_dict)

        except errors.OWLAlreadyRunningError:
            self.logger.critical("OWL initialization failed: GPIO pin conflict. Another OWL instance may be running.",
                                 exc_info=True)
            raise

        ### Data collection only ###
        self.detection_enable = Value('b', self.config.getboolean('DataCollection', 'detection_enable', fallback=False))
        self.image_sample_enable = Value('b', self.config.getboolean('DataCollection', 'image_sample_enable',
                                                                     fallback=False))

        # Local cached versions for performance
        self._detection_enable = self.detection_enable.value
        self._image_sample_enable = self.image_sample_enable.value
        self._STATE_CHECK_INTERVAL = 0.1
        self.stop_state_update = threading.Event()

        ####################### DASHBOARD ############################
        self.dash = None
        self.stream_active = None
        self.latest_stream_frame = None
        mqtt_enable = self.config.getboolean('MQTT', 'enable', fallback=False)

        broker_ip = self.config.get('MQTT', 'broker_ip', fallback='localhost')
        broker_port = self.config.getint('MQTT', 'broker_port', fallback=1883)
        device_id = self.config.get('MQTT', 'device_id', fallback='auto')
        network_mode = self.config.get('Network', 'mode', fallback=None)
        client_id = f"client_{device_id}"

        if mqtt_enable:
            try:
                from utils.mqtt_manager import OWLMQTTPublisher

                self.dash = OWLMQTTPublisher(
                    broker_host=broker_ip,
                    broker_port=broker_port,
                    client_id=client_id,
                    device_id=device_id,
                    network_mode=network_mode)

                low_config = self.config.get('Controller', 'low_sensitivity_config',
                                             fallback='config/SENSITIVITY_LOW.ini')
                medium_config = self.config.get('Controller', 'medium_sensitivity_config',
                                                fallback='config/SENSITIVITY_MEDIUM.ini')
                high_config = self.config.get('Controller', 'high_sensitivity_config',
                                              fallback='config/SENSITIVITY_HIGH.ini')

                self.dash.set_owl_instance(self, low_config, medium_config, high_config)

                self.dash.start()

                # Enhanced logging
                mode = 'NETWORKED' if self.dash.networked_mode else 'STANDALONE'
                self.logger.info(f"MQTT enabled - Mode: {mode}")
                self.logger.info(f"MQTT Broker: {broker_ip}:{broker_port}")
                self.logger.info(f"Device ID: {device_id}")
                self.logger.info(f"Client ID: {client_id}")

                LogManager.add_mqtt_handler(
                    mqtt_client=self.dash.client,
                    mqtt_error_topic=self.dash.topics['errors']
                )

            except Exception as e:
                self.dash = None
                self.logger.critical("Failed to initialize MQTT server. Dashboard disabled.")
                raise errors.MQTTConnectionError(host=broker_ip, port=broker_port, original_error=e)

            try:
                self.stream_lock = threading.Lock()
                self.start_streaming_server()
                self.stream_active = True  # IMPORTANT: Set status to True on success
                self.logger.info("MJPEG video streaming server started successfully")

            except MJPEGStreamError as e:
                self.logger.warning(f"Could not start MJPEG stream, but core functions will continue: {e}")
                self.stream_active = False

            if self.dash:
                self.dash.set_stream_status(self.stream_active)

        # GPS setup (only if enabled in config)
        self.gps_source = self.config.get('GPS', 'source', fallback='none').lower()
        self.gps_data = None

        if self.gps_source != "none":
            if self.dash:  # Override with dashboard if enabled
                self.gps_source = 'dashboard'
            self.gps_port = self.config.get('GPS', 'port', fallback='/dev/ttyUSB0')
            self.gps_baudrate = self.config.getint('GPS', 'baudrate', fallback=9600)

        # if a controller is connected, sample images must be true to set up directories correctly
        self.controller_type = self.config.get('Controller', 'controller_type').strip("'\" ").lower()

        if self.controller_type not in {'none', 'ute', 'advanced'}:
            self.logger.error(f"Invalid controller type: {self.controller_type}")
            raise errors.ControllerTypeError(self.controller_type, valid_types=list({'none', 'ute', 'advanced'}))

        if self.controller_type != 'none' and not self.dash:
            self.detection_enable = Value('b', False)
            self.image_sample_enable = Value('b', False)
            self.sensitivity_level = Value('i', Sensitivity.HIGH.value)

        if self.controller_type != 'none' or self.dash:
            # Force infrastructure setup (directories, ImageRecorder) so
            # controller/dashboard can toggle recording when ready.
            # Actual recording state is set after setup — see below.
            self._image_sample_enable = True

        # if controller is 'none' but _image_sample_enable is True, then it will set everything up
        if self._image_sample_enable:
            self.sample_method = self.config.get('DataCollection', 'sample_method')
            self.sample_frequency = self.config.getint('DataCollection', 'sample_frequency')
            self.save_directory = self.config.get('DataCollection', 'save_directory')
            self.camera_name = self.config.get('DataCollection', 'camera_name')

            try:
                self.directory_manager = DirectorySetup(save_directory=self.save_directory)
                self.save_directory, self.save_subdirectory = self.directory_manager.setup_directories()

                self.image_recorder = ImageRecorder(save_directory=self.save_subdirectory, mode=self.sample_method)

            except (errors.NoWritableUSBError, errors.USBMountError, errors.USBWriteError,
                    errors.StorageSystemError) as e:
                self.logger.critical(str(e))
                self.stop()
                raise

        ############################

        # initialise controller buttons and async management
        if self.controller_type == 'ute':
            self.status_indicator = UteStatusIndicator(save_directory=self.save_directory, record_led_pin='BOARD38',
                                                       storage_led_pin='BOARD40')
            self.controller = UteController(
                detection_state=self.detection_enable,
                sample_state=self.image_sample_enable,
                stop_flag=Value('b', False),
                owl_instance=self,
                status_indicator=self.status_indicator,
                switch_purpose=self.config.get('Controller', 'switch_purpose', fallback='recording').strip("'\" ").lower(),
                switch_board_pin=f"BOARD{self.config.getint('Controller', 'switch_pin', fallback=37)}"
            )
            self.controller_process = Process(target=self.controller.run)
            self.controller_process.start()

        elif self.controller_type == 'advanced':
            if not hasattr(self, 'sensitivity_level'):
                self.sensitivity_level = Value('i', Sensitivity.HIGH.value)

            self.status_indicator = AdvancedStatusIndicator(save_directory=self.save_directory,
                                                            status_led_pin='BOARD37')
            self.controller = AdvancedController(
                recording_state=self.image_sample_enable,
                sensitivity_level=self.sensitivity_level,
                detection_mode_state=Value('i', 1),
                stop_flag=Value('b', False),
                owl_instance=self,
                status_indicator=self.status_indicator,
                low_sensitivity_config=self.config.get('Controller', 'low_sensitivity_config', fallback='').strip("'\" "),
                high_sensitivity_config=self.config.get('Controller', 'high_sensitivity_config', fallback='').strip("'\" "),
                recording_bpin=f"BOARD{self.config.getint('Controller', 'recording_pin', fallback=38)}",
                sensitivity_bpin=f"BOARD{self.config.getint('Controller', 'sensitivity_pin', fallback=40)}",
                detection_mode_bpin_up=f"BOARD{self.config.getint('Controller', 'detection_mode_pin_up', fallback=36)}",
                detection_mode_bpin_down=f"BOARD{self.config.getint('Controller', 'detection_mode_pin_down', fallback=35)}"
            )
            self.controller_process = Process(target=self.controller.run)
            self.controller_process.start()

        else:
            self.controller = None
            if self._image_sample_enable:
                self.status_indicator = HeadlessStatusIndicator(save_directory=self.save_directory)
                self.status_indicator.start_storage_indicator()

            else:
                self.status_indicator = HeadlessStatusIndicator(save_directory=None, no_save=True)

        # Infrastructure is set up. Now set the actual initial recording state.
        # Dashboard-only (no hardware controller): start OFF — user enables via dashboard.
        # Hardware controllers: switch position was read during controller init above.
        if self.dash and self.controller_type == 'none':
            self._image_sample_enable = False
            with self.image_sample_enable.get_lock():
                self.image_sample_enable.value = False
            self.dash.set_image_sample_enable(False)

        if self.dash or self.controller_type != 'none':
            threading.Thread(target=self.update_state, daemon=True).start()
            self.logger.info("State monitoring thread started")

        # Actuation timing — read from [Actuation] (CONTROLLER.ini), fall back to [System]
        if self.config.has_section('Actuation'):
            self.actuation_duration = self.config.getfloat('Actuation', 'actuation_duration',
                                                           fallback=self.config.getfloat('System', 'actuation_duration'))
            self.delay = self.config.getfloat('Actuation', 'delay',
                                              fallback=self.config.getfloat('System', 'delay'))
        else:
            self.actuation_duration = self.config.getfloat('System', 'actuation_duration')
            self.delay = self.config.getfloat('System', 'delay')

        # Loop time tracking (30-frame rolling window)
        self._loop_times = deque(maxlen=30)
        self._avg_loop_time_ms = 0.0

        self.relay_vis = None

        # Check which Raspberry Pi is being used and adjust the resolution accordingly.
        # Use `cat /proc-device-tree/model` to check the model of the Raspberry Pi.
        total_pixels = self.resolution[0] * self.resolution[1]

        if (self.RPI_VERSION in ['rpi-3', 'rpi-4']) and total_pixels > (832 * 640):
            # change here if you want to test higher resolutions, but be warned, backup your current image!
            # the older versions of the Pi are known to 'brick' and become unusable if too high resolutions are used.
            self.resolution = (640, 480)
            self.logger.warning(f"Resolution {self.config.getint('Camera', 'resolution_width')}, "
                                f"{self.config.getint('Camera', 'resolution_height')} selected is dangerously high. ")
        else:
            self.logger.warning(
                f'High resolution, expect low framerate. Resolution set to {self.resolution[0]}x{self.resolution[1]}.')

        self.frame_width = None
        self.frame_height = None

        try:
            self.cam = self.setup_media_source(input_file_or_directory)
            self.logger.info('Media source successfully set up...')
            time.sleep(1.0)

        except (errors.MediaPathError, errors.InvalidMediaError, errors.MediaInitError, errors.CameraInitError) as e:
            self.logger.error(str(e))
            self.stop()

        # sensitivity and weed size to be added
        self.sensitivity = None
        self.lane_coords = {}

        # add the total number of relays being controlled. This can be changed easily, but the relay_dict and physical relays would need
        # to be updated too. Fairly straightforward, so an opportunity for more precise application
        self.relay_num = self.config.getint('System', 'relay_num')

        # GreenOnGreen / hybrid config
        self.inference_resolution = self.config.getint('GreenOnGreen', 'inference_resolution', fallback=320)
        self.crop_buffer_px = self.config.getint('GreenOnGreen', 'crop_buffer_px', fallback=20)
        self._pending_algorithm = None
        self._pending_trackbar_updates = {}
        self._pending_model = None
        self._pending_detect_classes = None
        self._gog_detector = None

        # Crop factors to reduce edge artifacts (0.1 = 10% crop from each side)
        self.crop_factor_horizontal = self.config.getfloat('Camera', 'crop_factor_horizontal', fallback=0.1)
        self.crop_factor_vertical = self.config.getfloat('Camera', 'crop_factor_vertical', fallback=0.1)
        self.logger.info(f'[INFO] Crop factor: X {self.crop_factor_horizontal} | Y {self.crop_factor_vertical}')

        if self.frame_width and self.frame_height:
            # Calculate cropped dimensions
            crop_left = int(self.frame_width * self.crop_factor_horizontal)
            crop_right = int(self.frame_width - crop_left)
            crop_top = int(self.frame_height * self.crop_factor_vertical)
            crop_bottom = int(self.frame_height - crop_top)
            self.cropped_width = crop_right - crop_left
            self.cropped_height = crop_bottom - crop_top
            self.logger.info(f'[INFO] Image cropped to {crop_left}x{crop_right}x{crop_top}x{crop_bottom}.')
            # Store crop boundaries for slicing
            self.crop_slice = (slice(crop_top, crop_bottom), slice(crop_left, crop_right))

            # Calculate lane width based on cropped width
            self.lane_width = self.cropped_width / self.relay_num

            # Calculate lane coords relative to cropped frame
            for i in range(self.relay_num):
                laneX = int(i * self.lane_width)
                self.lane_coords[i] = laneX

            # Precompute the integer lane coordinates for reuse
            self.lane_coords_int = {k: int(v) for k, v in self.lane_coords.items()}

        else:
            self.logger.error('[ERROR] No frame width or frame height provided.')

    @property
    def config_path(self):
        return self._config_path

    def hoot(self):
        self.record_video = False  # Flag to control video recording
        self.video_writer = None
        image_out = None

        algorithm = self.config.get('System', 'algorithm')
        log_fps = self.config.getboolean('DataCollection', 'log_fps')
        if self.controller:
            self.controller.update_state()

        # track framecount
        frame_count = 0
        last_fps_time = time.time()
        fps_frame_count = 0

        # GoG shared config (used by both gog and gog-hybrid)
        self._model_path = self.config.get('GreenOnGreen', 'model_path')
        self._gog_confidence = self.config.getfloat('GreenOnGreen', 'confidence')
        detect_classes_str = self.config.get('GreenOnGreen', 'detect_classes', fallback='')
        self._detect_classes_list = [c.strip() for c in detect_classes_str.split(',') if c.strip()]
        detect_classes = self._detect_classes_list or None
        actuation_mode = self.config.get('GreenOnGreen', 'actuation_mode', fallback='centre')
        min_detection_pixels = self.config.getint('GreenOnGreen', 'min_detection_pixels', fallback=50)

        # GoB shared config (used by both colour and gog-hybrid)
        # Instance attributes so MQTT can update them live
        self.min_detection_area = self.config.getint('GreenOnBrown', 'min_detection_area')
        self.invert_hue = self.config.getboolean('GreenOnBrown', 'invert_hue')

        def _create_detector(algo):
            """Three-way detector factory."""
            current_classes = self._detect_classes_list or None
            if algo == 'gog':
                from utils.greenongreen import GreenOnGreen
                return GreenOnGreen(
                    model_path=self._model_path,
                    confidence=self._gog_confidence,
                    detect_classes=current_classes
                )
            elif algo == 'gog-hybrid':
                from utils.greenongreen import GreenOnGreen
                return GreenOnGreen(
                    model_path=self._model_path,
                    confidence=self._gog_confidence,
                    detect_classes=current_classes,
                    hybrid_mode=True,
                    inference_resolution=self.inference_resolution,
                    crop_buffer_px=self.crop_buffer_px
                )
            else:
                return GreenOnBrown(algorithm=algo)

        try:
            weed_detector = _create_detector(algorithm)
            if algorithm in ('gog', 'gog-hybrid'):
                self._gog_detector = weed_detector

        except (ModuleNotFoundError, IndexError, FileNotFoundError, ValueError) as e:
            algo_error = errors.AlgorithmError(algorithm, e)
            algo_error.handle(self)

        except Exception as e:
            algo_error = errors.AlgorithmError(algorithm, e)
            algo_error.handle(self)

        if self.show_display:
            self.relay_vis = self.relay_controller.relay_vis
            self.relay_vis.setup()
            self.relay_controller.vis = True

        try:
            while True:
                loop_start = time.time()
                frame = self.cam.read()

                if frame is None:
                    self.logger.info("[INFO] Frame is None. Stopped.")
                    self.stop()
                    break

                # Drain queued trackbar updates from MQTT thread (thread-safe)
                if self.show_display and self._pending_trackbar_updates:
                    pending = self._pending_trackbar_updates
                    self._pending_trackbar_updates = {}
                    for tb_name, tb_val in pending.items():
                        cv2.setTrackbarPos(tb_name, self.window_name, tb_val)

                # retrieve the trackbar positions for thresholds
                if self.show_display:
                    self.exg_min = cv2.getTrackbarPos("ExG-Min", self.window_name)
                    self.exg_max = cv2.getTrackbarPos("ExG-Max", self.window_name)
                    self.hue_min = cv2.getTrackbarPos("Hue-Min", self.window_name)
                    self.hue_max = cv2.getTrackbarPos("Hue-Max", self.window_name)
                    self.saturation_min = cv2.getTrackbarPos("Sat-Min", self.window_name)
                    self.saturation_max = cv2.getTrackbarPos("Sat-Max", self.window_name)
                    self.brightness_min = cv2.getTrackbarPos("Bright-Min", self.window_name)
                    self.brightness_max = cv2.getTrackbarPos("Bright-Max", self.window_name)

                # pass image, thresholds to green_on_brown function
                if self._detection_enable:
                    # Live algorithm switching
                    if self._pending_algorithm and self._pending_algorithm != algorithm:
                        try:
                            weed_detector = _create_detector(self._pending_algorithm)
                            algorithm = self._pending_algorithm
                            if algorithm in ('gog', 'gog-hybrid'):
                                self._gog_detector = weed_detector
                            self.logger.info(f"Live algorithm switch to: {algorithm}")
                        except Exception as e:
                            self.logger.error(f"Failed to load detector for {self._pending_algorithm}: {e}")
                            # Revert — keep using current weed_detector and algorithm
                            if self.dash:
                                self.dash.state['algorithm'] = algorithm
                                self.dash.state['algorithm_error'] = str(e)
                        self._pending_algorithm = None

                    # Live model switching
                    if self._pending_model:
                        new_model = self._pending_model
                        self._pending_model = None
                        try:
                            self._model_path = new_model
                            weed_detector = _create_detector(algorithm)
                            if algorithm in ('gog', 'gog-hybrid'):
                                self._gog_detector = weed_detector
                            self.logger.info(f"Live model switch to: {new_model}")
                        except Exception as e:
                            self.logger.error(f"Failed to switch model: {e}")

                    # Live detect_classes update
                    if self._pending_detect_classes is not None:
                        new_classes = self._pending_detect_classes
                        self._pending_detect_classes = None
                        self._detect_classes_list = new_classes
                        if hasattr(weed_detector, 'update_detect_classes'):
                            weed_detector.update_detect_classes(new_classes or None)
                        self.logger.info(f"detect_classes updated: {new_classes}")

                    # Live crop buffer update (hybrid mode only)
                    if (algorithm == 'gog-hybrid'
                            and hasattr(weed_detector, 'set_crop_buffer')
                            and weed_detector.crop_buffer_px != self.crop_buffer_px):
                        weed_detector.set_crop_buffer(self.crop_buffer_px)

                    cropped_frame = frame[self.crop_slice]

                    return_image_out = self.show_display or bool(self.dash)

                    if algorithm == 'gog':
                        cnts, boxes, weed_centres, image_out = weed_detector.inference(
                            cropped_frame,
                            confidence=self._gog_confidence,
                            show_display=return_image_out,
                            build_mask=(actuation_mode == 'zone')
                        )
                    elif algorithm == 'gog-hybrid':
                        cnts, boxes, weed_centres, image_out = weed_detector.inference(
                            cropped_frame,
                            confidence=self._gog_confidence,
                            show_display=return_image_out,
                            exg_min=self.exg_min, exg_max=self.exg_max,
                            hue_min=self.hue_min, hue_max=self.hue_max,
                            saturation_min=self.saturation_min, saturation_max=self.saturation_max,
                            brightness_min=self.brightness_min, brightness_max=self.brightness_max,
                            min_detection_area=self.min_detection_area, invert_hue=self.invert_hue
                        )
                    else:
                        cnts, boxes, weed_centres, image_out = weed_detector.inference(
                            cropped_frame,
                            exg_min=self.exg_min,
                            exg_max=self.exg_max,
                            hue_min=self.hue_min,
                            hue_max=self.hue_max,
                            saturation_min=self.saturation_min,
                            saturation_max=self.saturation_max,
                            brightness_min=self.brightness_min,
                            brightness_max=self.brightness_max,
                            show_display=return_image_out,
                            algorithm=algorithm,
                            min_detection_area=self.min_detection_area,
                            invert_hue=self.invert_hue,
                            label='WEED'
                        )

                    if len(weed_centres) > 0:
                        if self.dash:
                            self.dash.weed_detect_indicator()
                        if self.controller:
                            self.controller.weed_detect_indicator()

                    # Zone-based actuation (segmentation models with gog/gog-hybrid)
                    if (algorithm.startswith('gog')
                            and actuation_mode == 'zone'
                            and hasattr(weed_detector, 'detection_mask')
                            and weed_detector.detection_mask is not None):
                        actuation_time = time.time()
                        for i in range(self.relay_num):
                            lane_start = self.lane_coords_int[i]
                            lane_end = int(lane_start + self.lane_width)
                            lane_pixels = np.count_nonzero(
                                weed_detector.detection_mask[:, lane_start:lane_end]
                            )
                            if lane_pixels >= min_detection_pixels:
                                self.relay_controller.receive(
                                    relay=i,
                                    delay=self.delay,
                                    time_stamp=actuation_time,
                                    duration=self.actuation_duration)
                    else:
                        # Centre-based actuation (default, works for all model types)
                        for centre in weed_centres:
                            actuation_time = time.time()
                            centre_x = centre[0]

                            for i in range(self.relay_num):
                                lane_start = self.lane_coords_int[i]
                                lane_end = lane_start + self.lane_width
                                if lane_start <= centre_x < lane_end:
                                    self.relay_controller.receive(
                                        relay=i,
                                        delay=self.delay,
                                        time_stamp=actuation_time,
                                        duration=self.actuation_duration)

                ##### Update Dashboard Stream #####
                if frame_count % 90 == 0:  # Every 90 frames (~3 seconds at 30fps)
                    if self.dash and hasattr(self.dash, 'update_system_stats'):
                        try:
                            stats = self.get_system_stats()
                            self.dash.update_system_stats(stats)
                        except Exception as e:
                            self.logger.debug(f"Error updating system stats: {e}")

                if self.dash and frame_count % 5 == 0:  # send every 5th frame to the streamer to reduce overhead
                    try:
                        if self._detection_enable and image_out is not None:
                            final_frame_to_stream = image_out
                        else:
                            final_frame_to_stream = frame

                        self.set_latest_stream_frame(final_frame_to_stream)
                    except Exception as e:
                        self.logger.error(f"Error sending frame to dashboard: {e}")

                ##### IMAGE SAMPLER #####
                # record sample images if required of weeds detected. sampleFreq specifies how often
                if self._image_sample_enable:
                    # only record every sampleFreq number of frames.
                    # If sample_frequency = 60, this will activate every 60th frame
                    if frame_count % self.sample_frequency == 0:
                        save_boxes = None
                        save_centres = None
                        if self.sample_method != 'whole' and self._detection_enable:
                            save_boxes = boxes
                            save_centres = weed_centres

                        self.image_recorder.add_frame(frame=frame,
                                                      frame_id=frame_count,
                                                      boxes=save_boxes,
                                                      centres=save_centres,
                                                      gps_data=self.gps_data)

                        if self.controller:
                            self.status_indicator.image_write_indicator()
                        if self.dash:
                            self.dash.image_write_indicator()

                        if self.status_indicator.DRIVE_FULL:
                            if self.dash:
                                self.dash.set_image_sample_enable(False)
                                self.dash.drive_full_indicator()
                                self.logger.info("Drive full: Image sampling disabled via MQTT")
                            else:
                                with self.image_sample_enable.get_lock():
                                    self.image_sample_enable.value = False
                                self.logger.info("Drive full: Image sampling disabled locally")

                            self._image_sample_enable = False

                            self.image_recorder.stop()
                            self.status_indicator.error(5)
                            self.logger.info("Drive full: Image sampling disabled permanently due to storage full")

                frame_count = frame_count + 1 if frame_count < 900 else 1

                # Loop time tracking
                loop_time_ms = (time.time() - loop_start) * 1000
                self._loop_times.append(loop_time_ms)
                self._avg_loop_time_ms = sum(self._loop_times) / len(self._loop_times)

                # FPS logging (time-based, replaces imutils FPS)
                if log_fps:
                    fps_frame_count += 1
                    elapsed = time.time() - last_fps_time
                    if elapsed >= 30.0:
                        approx_fps = fps_frame_count / elapsed
                        self.logger.info(f"[INFO] Approximate FPS: {approx_fps:.2f} | Avg loop: {self._avg_loop_time_ms:.1f}ms")
                        fps_frame_count = 0
                        last_fps_time = time.time()

                if self.show_display:
                    if not self._detection_enable:
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
                                (20, int(image_out.shape[1] * 0.72)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 80, 255), 1)
                    h, w = image_out.shape[:2]
                    display_frame = cv2.resize(image_out, (600, int(h * 600 / w))) if w != 600 else image_out
                    cv2.imshow("Detection Output", display_frame)

                k = cv2.waitKey(1) & 0xFF
                if k == ord('s'):
                    self.save_parameters()
                    self.logger.info("[INFO] Parameters saved.")

                elif k == ord('r'):
                    # Toggle video recording
                    self.record_video = not self.record_video
                    if self.record_video:
                        self.logger.info("[INFO] Started video recording.")
                    else:
                        if self.video_writer:
                            self.video_writer.release()
                            self.video_writer = None
                        self.logger.info("[INFO] Stopped video recording.")

                elif k == 27:
                    if self.show_display:
                        self.relay_controller.relay_vis.close()

                    self.logger.info("[INFO] Stopped.")
                    self.stop()
                    break

        except KeyboardInterrupt:
            if self.show_display:
                self.relay_controller.relay_vis.close()
            self.logger.info("[INFO] Stopped.")
            self.stop()

        except Exception as e:
            self.logger.error(f"[CRITICAL ERROR] STOPPED: {e}", exc_info=True)
            self.stop()

    def stop(self):
        """Gracefully shut down all OWL components."""

        def safe_stop(component, name, fallback_to_terminate=True):
            """
            Attempt to gracefully stop a component, with an option to terminate if stopping fails.
            """
            try:
                if hasattr(component, 'stop'):
                    component.stop()
                self.logger.info(f"Stopped {name}")
            except Exception as e:
                self.logger.warning(f"Graceful stop failed for {name}: {e}")
                if fallback_to_terminate and hasattr(component, 'terminate'):
                    try:
                        component.terminate()
                        self.logger.info(f"Forcefully terminated {name}")
                    except Exception as terminate_error:
                        self.logger.error(f"Failed to terminate {name}: {terminate_error}")

        try:
            # Stop controller processes
            if hasattr(self, 'controller') and self.controller:
                safe_stop(self.controller, 'controller', fallback_to_terminate=False)
                if hasattr(self, 'controller_process') and self.controller_process.is_alive():
                    self.controller_process.terminate()
                    self.controller_process.join(timeout=0.5)
                    self.logger.info("Controller process terminated")

            # Stop image recorder
            if hasattr(self, 'image_recorder') and self.image_recorder:
                safe_stop(self.image_recorder, 'image recorder')

            # Stop status indicator
            if hasattr(self, 'status_indicator') and self.status_indicator:
                safe_stop(self.status_indicator, 'status indicator', fallback_to_terminate=False)

            # Stop relay controller
            if hasattr(self, 'relay_controller') and self.relay_controller:
                safe_stop(self.relay_controller, 'relay controller', fallback_to_terminate=False)
                try:
                    self.relay_controller.relay.all_off()  # Ensure all relays are off
                except Exception as e:
                    self.logger.warning(f"Failed to turn off relays: {e}")

            # Stop state update thread
            if hasattr(self, 'stop_state_update'):
                self.stop_state_update.set()

            # Stop dashboard controller
            if hasattr(self, 'dash') and self.dash:
                try:
                    self.dash.stop()
                except Exception as e:
                    self.logger.warning(f"Error stopping dashboard controller: {e}")

            # Stop camera
            if hasattr(self, 'cam') and self.cam:
                safe_stop(self.cam, 'camera', fallback_to_terminate=False)

        except Exception as e:
            self.logger.error(f"Critical error during shutdown: {e}", exc_info=True)
        finally:
            try:
                LogManager().stop()  # Ensure logger shuts down properly
                self.logger.info("OWL shutdown complete")
            except Exception as log_error:
                print(f"Failed to stop LogManager: {log_error}", file=sys.stderr)
            sys.exit(0)

    def save_parameters(self):
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        new_config_filename = f"{timestamp}_{self._config_path.name}"
        new_config_path = self._config_path.parent / new_config_filename

        # Update the 'GreenOnBrown' section with current attribute values
        if 'GreenOnBrown' not in self.config.sections():
            self.config.add_section('GreenOnBrown')

        self.config.set('GreenOnBrown', 'exg_min', str(self.exg_min))
        self.config.set('GreenOnBrown', 'exg_max', str(self.exg_max))
        self.config.set('GreenOnBrown', 'hue_min', str(self.hue_min))
        self.config.set('GreenOnBrown', 'hue_max', str(self.hue_max))
        self.config.set('GreenOnBrown', 'saturation_min', str(self.saturation_min))
        self.config.set('GreenOnBrown', 'saturation_max', str(self.saturation_max))
        self.config.set('GreenOnBrown', 'brightness_min', str(self.brightness_min))
        self.config.set('GreenOnBrown', 'brightness_max', str(self.brightness_max))

        # Write the updated configuration to the new file with a timestamped filename
        with open(new_config_path, 'w') as configfile:
            self.config.write(configfile)

        self.logger.info(f"[INFO] Configuration saved to {new_config_path}")

    def setup_media_source(self, input_file_or_directory):
        """
        Configure and initialize the appropriate media source (camera or media file/directory).

        Args:
            input_file_or_directory: Optional path from CLI args to image/video source

        Returns:
            VideoStream or FrameReader: Initialized media source

        Raises:
            FileNotFoundError: If specified media path does not exist
            InvalidMediaError: If specified file is not a valid image/video format
            RuntimeError: If media source initialization fails
        """
        # Determine input source with CLI taking precedence over config
        if input_file_or_directory:
            if len(self.config.get('System', 'input_file_or_directory')) > 0:
                self.logger.warning('[WARNING] Input sources provided in both CLI and config file. Using CLI argument.')
            self.input_file_or_directory = input_file_or_directory
        else:
            self.input_file_or_directory = self.config.get('System', 'input_file_or_directory').strip('"\'')

        if self.input_file_or_directory:
            path = Path(self.input_file_or_directory)

            if not path.exists():
                raise errors.MediaPathError(path=path, message="Specified input path does not exist")

            if path.is_file():
                valid_extensions = {
                    '.jpg', '.jpeg', '.png', '.bmp',  # Images
                    '.mp4', '.avi', '.mov', '.mkv'  # Videos
                }
                if path.suffix.lower() not in valid_extensions:
                    raise errors.InvalidMediaError(path=path, valid_formats=valid_extensions)

            try:
                media_source = FrameReader(path=self.input_file_or_directory,
                                           resolution=self.resolution,
                                           loop_time=self.image_loop_time)

                self.frame_width, self.frame_height = media_source.resolution
                self.logger.info(f'[INFO] Using {media_source.input_type} from {self.input_file_or_directory}...')
                return media_source

            except Exception as e:
                raise errors.MediaInitError(path=path, original_error=str(e)) from e

        # Set up camera if no file input specified
        try:
            media_source = VideoStream(resolution=self.resolution,
                                       exp_compensation=self.exp_compensation)
            media_source.start()

            self.frame_width = media_source.frame_width
            self.frame_height = media_source.frame_height

            return media_source

        except IndexError as e:
            self.logger.critical("Camera index not found", exc_info=True)
            self.status_indicator.error(2)
            self.stop()
            raise errors.CameraNotFoundError(error_type="Camera Not Found", original_error=str(e))

        except ModuleNotFoundError as e:
            self.logger.critical(e, exc_info=True)
            module_name = str(e).split("'")[-2]
            self.status_indicator.error(1)
            self.stop()
            raise errors.DependencyError(missing_module=module_name, error_msg=str(e)) from None

        except Exception as e:
            error_msg = f"[CRITICAL ERROR] Failed to initialize camera: {str(e)}"
            self.logger.critical(error_msg)
            self.status_indicator.error(1)
            self.stop()
            raise errors.CameraInitError(str(e)) from e

    def start_streaming_server(self):
        """Initializes and starts the MJPEG streaming server in a background thread."""
        host = '0.0.0.0'
        port = 8001
        try:
            self.logger.info(f"Starting MJPEG streaming server on port {port}")
            server_address = (host, port)
            httpd = ThreadedHTTPServer(server_address, StreamingHandler, owl_instance=self)

            server_thread = threading.Thread(target=httpd.serve_forever)
            server_thread.daemon = True
            server_thread.start()

        except OSError as e:
            self.logger.error(f"Failed to start MJPEG streaming server on port {port}: {e}", exc_info=True)
            raise errors.MJPEGStreamError(host=host, port=port, original_error=e) from e
        except Exception as e:
            self.logger.error(f"An unexpected error occurred while starting MJPEG streaming server: {e}", exc_info=True)
            raise errors.MJPEGStreamError(host=host, port=port, original_error=e) from e

    def set_latest_stream_frame(self, frame):
        """Thread-safe method to update the frame for the stream."""
        with self.stream_lock:
            self.latest_stream_frame = frame.copy()

    def get_latest_stream_frame(self):
        """Thread-safe method to get the latest frame for the stream."""
        with self.stream_lock:
            return self.latest_stream_frame

    def get_system_stats(self):
        """
        Get system statistics with robust error handling.
        """
        import subprocess
        import glob
        try:
            import psutil
        except ImportError:
            self.logger.warning("psutil not installed - system stats unavailable")
            return {
                'cpu_percent': 0,
                'cpu_temp': 0,
                'memory_percent': 0,
                'memory_used': 0,
                'memory_total': 0,
                'disk_percent': 0,
                'disk_used': 0,
                'disk_total': 0,
                'fan_status': {'is_rpi5': False, 'mode': 'unavailable', 'rpm': 0},
                'owl_running': True
            }

        stats = {
            'cpu_percent': 0,
            'cpu_temp': 0,
            'memory_percent': 0,
            'memory_used': 0,
            'memory_total': 0,
            'disk_percent': 0,
            'disk_used': 0,
            'disk_total': 0,
            'fan_status': {'is_rpi5': False, 'mode': 'unavailable', 'rpm': 0},
            'owl_running': True
        }

        try:
            # Get RPI version
            from utils.input_manager import get_rpi_version
            rpi_version = get_rpi_version()

            if rpi_version == 'rpi-5':
                stats['fan_status']['is_rpi5'] = True
                stats['fan_status']['mode'] = 'auto'  # or self.fan_state if you track it

                try:
                    rpm_files = glob.glob('/sys/devices/platform/cooling_fan/hwmon/*/fan1_input')
                    if rpm_files:
                        with open(rpm_files[0], 'r') as f:
                            stats['fan_status']['rpm'] = int(f.read().strip())
                except Exception as e:
                    self.logger.debug(f"Could not read fan RPM: {e}")

            # CPU, Memory, Disk
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')

            stats.update({
                'cpu_percent': round(cpu_percent, 1),
                'memory_percent': round(memory.percent, 1),
                'memory_used': round(memory.used / (1024 ** 3), 1),
                'memory_total': round(memory.total / (1024 ** 3), 1),
                'disk_percent': round(disk.percent, 1),
                'disk_used': round(disk.used / (1024 ** 3), 1),
                'disk_total': round(disk.total / (1024 ** 3), 1),
            })

            # CPU Temperature
            try:
                result = subprocess.run(['/usr/bin/vcgencmd', 'measure_temp'],
                                        capture_output=True, text=True, timeout=1)
                if result.returncode == 0:
                    temp_str = result.stdout.replace('temp=', '').replace("'C\n", '').strip()
                    stats['cpu_temp'] = round(float(temp_str), 1)
            except Exception as e:
                self.logger.debug(f"Could not get CPU temp: {e}")

        except Exception as e:
            self.logger.warning(f"Error getting system stats: {e}")

        stats['avg_loop_time_ms'] = round(self._avg_loop_time_ms, 1)
        stats['actuation_duration'] = self.actuation_duration
        stats['delay'] = self.delay

        return stats

    def update_state(self):
        """Update local state from MQTT server or local hardware controllers"""
        while not self.stop_state_update.is_set():
            if self.dash:
                if self.controller_type != 'none':
                    # Hardware controller active - push hardware states to existing MQTT methods
                    if hasattr(self, 'detection_enable'):
                        with self.detection_enable.get_lock():
                            hardware_detection = self.detection_enable.value
                        self.dash.set_detection_enable(hardware_detection)
                        self._detection_enable = hardware_detection

                    if hasattr(self, 'image_sample_enable'):
                        with self.image_sample_enable.get_lock():
                            hardware_recording = self.image_sample_enable.value
                        self.dash.set_image_sample_enable(hardware_recording)
                        self._image_sample_enable = hardware_recording

                    if hasattr(self, 'sensitivity_level'):
                        with self.sensitivity_level.get_lock():
                            hardware_sensitivity_int = self.sensitivity_level.value

                        hardware_sensitivity_string = Sensitivity(hardware_sensitivity_int).name.lower()
                        self.dash.set_sensitivity_level(hardware_sensitivity_string)
                        self._sensitivity_level = hardware_sensitivity_string

                    # GPS from dashboard takes priority
                    self.gps_data = self.dash.get_gps_data()

                else:
                    # No hardware controller - normal dashboard control
                    self._detection_enable = self.dash.get_detection_enable()
                    self._image_sample_enable = self.dash.get_image_sample_enable()
                    self._sensitivity_level = self.dash.get_sensitivity_level()
                    self.gps_data = self.dash.get_gps_data()

            else:
                # Hardware only, no dashboard - existing behavior
                if hasattr(self, 'detection_enable'):
                    with self.detection_enable.get_lock():
                        self._detection_enable = self.detection_enable.value
                if hasattr(self, 'image_sample_enable'):
                    with self.image_sample_enable.get_lock():
                        self._image_sample_enable = self.image_sample_enable.value
                if hasattr(self, 'sensitivity_level'):
                    with self.sensitivity_level.get_lock():
                        sensitivity_int = self.sensitivity_level.value
                    self._sensitivity_level = Sensitivity(sensitivity_int).name.lower()

            time.sleep(self._STATE_CHECK_INTERVAL)

    def _log_system_info(self):
        """Log system information on startup"""
        self.logger.info(f"Starting OWL version {VERSION}")

        try:
            sys_info = SystemInfo.get_os_info()
            self.logger.info(
                f"System Information: OS: {sys_info['system']} {sys_info['release']}, "
                f"Machine: {sys_info['machine']}"
            )
        except Exception as e:
            self.logger.warning(f"Failed to retrieve OS information: {e}")

        try:
            python_info = SystemInfo.get_python_info()
            self.logger.info(
                f"Python Version: {python_info['version']}, "
                f"Implementation: {python_info['implementation']}, "
                f"Compiler: {python_info['compiler']}"
            )
        except Exception as e:
            self.logger.warning(f"Failed to retrieve Python information: {e}")

        try:
            rpi_info = SystemInfo.get_rpi_info()
            if rpi_info:
                self.logger.info(f"Hardware: {rpi_info}")
            else:
                self.logger.info("Raspberry Pi hardware info not available.")
        except Exception as e:
            self.logger.warning(f"Failed to retrieve Raspberry Pi information: {e}")

        try:
            git_info = SystemInfo.get_git_info()
            if git_info:
                self.logger.info(f"Git: branch={git_info['branch']}, commit={git_info['commit']}")
            else:
                self.logger.info("Git information not available.")
        except Exception as e:
            self.logger.warning(f"Failed to retrieve Git information: {e}")


# business end of things
if __name__ == "__main__":
    # these command line arguments enable people to operate/change some settings from the command line instead of
    # opening up the OWL code each time.
    ap = argparse.ArgumentParser()
    ap.add_argument('--show-display', action='store_true', default=False, help='show display windows')
    ap.add_argument('--focus', action='store_true', default=False,
                    help='(DEPRECATED) launch the focus GUI; please use the desktop icon instead')
    ap.add_argument('--input', type=str, default=None, help='path to image directory, single image or video file')
    ap.add_argument('--config', type=str, default=None, help='path to config file (overrides active_config.txt)')

    args = ap.parse_args()

    if args.focus:
        logger.warning(
            "--focus is deprecated, auto-launching focus GUI; please switch to the desktop icon in the future")
        import desktop.focus_gui

        desktop.focus_gui.main()
        sys.exit(0)

    # Determine which config file to use
    # Priority: 1) --config argument, 2) active_config.txt pointer, 3) default
    DEFAULT_CONFIG = 'config/DAY_SENSITIVITY_2.ini'
    ACTIVE_CONFIG_POINTER = Path(__file__).parent / 'config/active_config.txt'

    config_file = DEFAULT_CONFIG

    if args.config:
        # Command line argument takes priority
        config_file = args.config
        logger.info(f"Using config from command line: {config_file}")
    elif ACTIVE_CONFIG_POINTER.exists():
        # Check for active config pointer from dashboard
        try:
            active_config = ACTIVE_CONFIG_POINTER.read_text().strip()
            if active_config:
                # Verify the config file exists
                active_path = Path(__file__).parent / active_config
                if active_path.exists():
                    config_file = active_config
                    logger.info(f"Using active config from dashboard: {config_file}")
                else:
                    logger.warning(f"Active config not found: {active_config}, using default")
        except Exception as e:
            logger.warning(f"Could not read active_config.txt: {e}, using default")

    logger.info(f"Starting OWL with config: {config_file}")

    owl = Owl(
        config_file=config_file,
        show_display=args.show_display,
        input_file_or_directory=args.input
    )

    # start the targeting!
    owl.hoot()