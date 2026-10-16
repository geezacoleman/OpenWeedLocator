#!/usr/bin/env python
import os
import sys
import signal
import socket

import logging
import argparse
import time
import threading
from collections import deque

try:
    import serial
except ImportError:
    serial = None
from datetime import datetime, timezone
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
    # Beside owl.py, not in the working directory: LogManager writes here
    # too, and `python /path/to/owl.py` from elsewhere (common on a laptop)
    # must not scatter half-written logs/ folders around the filesystem.
    log_dir = Path(__file__).resolve().parent / 'logs'
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

# Imported before the guarded block below: the handler raises through it, and
# it has no third-party dependencies of its own so it cannot be the import that
# fails.
import utils.error_manager as errors

try:
    import numpy as np
    from utils.input_manager import UteController, AdvancedController, get_rpi_version
    from utils.output_manager import RelayController, HeadlessStatusIndicator, UteStatusIndicator, AdvancedStatusIndicator, GPSStatusLED, GPSLEDState
    from utils.directory_manager import DirectorySetup, session_folder_name
    from utils.device_identity import get_device_serial
    from utils.video_manager import VideoStream, StreamingHandler, ThreadedHTTPServer
    from utils.image_sampler import ImageRecorder, CaptureScheduler
    from utils.exposure_governor import ExposureGovernor, ground_smear_mm, parse_max_gain
    from utils.algorithms import fft_blur
    from utils.greenonbrown import GreenOnBrown, draw_boxes
    from utils.frame_reader import FrameReader
    from utils.config_manager import ConfigValidator, atomic_write_config
    from utils.geometry import compute_geometry, assign_lanes
    from utils.log_manager import LogManager, MQTTLogHandler
    from utils.shared_types import Sensitivity
    from utils.gps_manager import GPSState, TrackRecorder, nrf91_command, nrf91_start_gps
    from utils.actuation import SpeedAverager, ActuationCalculator, speed_warning
    from version import SystemInfo, VERSION

# DependencyError as well as ImportError: utils/image_sampler.py re-raises a
# missing piexif as DependencyError, which does NOT subclass ImportError, so it
# used to sail past this handler and reach the user as a bare traceback with no
# hint of which package to install.
except (ImportError, errors.DependencyError) as e:
    # "No module named 'x'" and "cannot import name 'x' from 'y'" both quote the
    # name, but DependencyError messages may not — never crash the error path.
    parts = str(e).split("'")
    missing_module = parts[1] if len(parts) > 1 else getattr(e, 'package', 'unknown')
    logger.error(f"Failed to import required module: {missing_module}")
    logger.error(f"Error details: {str(e)}")
    logger.error(f"Current virtual env: {os.environ.get('VIRTUAL_ENV', 'None')}")
    logger.error(f"Current working directory: {os.getcwd()}")
    if isinstance(e, errors.DependencyError):
        raise
    raise errors.DependencyError(missing_module, str(e)) from None

logger.info("All required modules imported successfully")

# The dashboard MJPEG stream consumes every Nth frame of the detection loop
STREAM_FRAME_INTERVAL = 5

# Consecutive empty reads tolerated from a live camera before giving up. A
# dropped or late frame is common and used to end the whole job silently; a
# genuine disconnect still stops the OWL, just loudly and after ~0.5 s.
MAX_DROPPED_FRAMES = 10


def nothing(x):
    pass


def has_gui_support():
    """True when this OpenCV build can open windows.

    Headless builds (opencv-python-headless, a Pi over SSH with no display,
    CI) raise cv2.error from any highgui call. Probing once here keeps
    --show-display from killing OWL on those setups - the detection loop
    runs exactly the same, just without the preview window.
    """
    try:
        cv2.namedWindow('owl_gui_probe', cv2.WINDOW_AUTOSIZE)
        cv2.destroyWindow('owl_gui_probe')
        return True
    except Exception:
        return False


class Owl:
    def __init__(self, show_display=False,
                 input_file_or_directory=None,
                 config_file='config/GENERAL_CONFIG.ini'):
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
        # configparser silently skips unreadable files. CONTROLLER.ini holds
        # [MQTT]/[Network]/[Security]; losing it (e.g. a root-owned file from
        # a provisioning bug) disables MQTT and the token guard with no error,
        # so an existing-but-unread file must be loud.
        controller_ini = Path(__file__).parent / 'config' / 'CONTROLLER.ini'
        read_ok = self.config.read(controller_ini)
        if controller_ini.exists() and not read_ok:
            self.logger.error(
                f"CONTROLLER.ini exists but could not be read (check ownership/"
                f"permissions: ls -l {controller_ini}) - MQTT, network and "
                f"security settings will be missing")
        # GEOMETRY.ini holds per-unit mount geometry (crop edges + actuation band)
        # and is read LAST so it always wins over legacy crop_*/actuation_* values
        # in the active detection config.
        self.config.read(Path(__file__).parent / 'config' / 'GEOMETRY.ini')
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
        # Min weed size as % of the detection frame area — the canonical key.
        # min_detection_area (px) is legacy: accepted on read, migrated to the
        # percent key here, and stripped from configs on the next save.
        self.min_detection_area = self.config.getint(
            'GreenOnBrown', 'min_detection_area', fallback=10)
        self.min_detection_area_percent = self.config.getfloat(
            'GreenOnBrown', 'min_detection_area_percent', fallback=0.0)
        if self.min_detection_area_percent <= 0:
            frame_area = (self.config.getint('Camera', 'resolution_width', fallback=416)
                          * self.config.getint('Camera', 'resolution_height', fallback=320))
            migrated = max(0.0005, min(5.0, self.min_detection_area / frame_area * 100))
            self.min_detection_area_percent = migrated
            self.config.set('GreenOnBrown', 'min_detection_area_percent',
                            f'{migrated:.4f}')
            self.logger.info(
                f'[CONFIG] migrated legacy min_detection_area '
                f'{self.min_detection_area}px -> {migrated:.4f}% of frame')
        self.invert_hue = self.config.getboolean('GreenOnBrown', 'invert_hue')

        # Painted LUT detection profiles (algorithm = lut). Profiles are .npz
        # files built by the dashboard weed painter and stored beside the config.
        self.lut_profile = self.config.get('GreenOnBrown', 'lut_profile', fallback='')
        self.lut_sensitivity = self.config.getint('GreenOnBrown', 'lut_sensitivity', fallback=50)
        from utils.lut_manager import LUTProfileManager
        self.lut_manager = LUTProfileManager(
            os.path.join(os.path.dirname(os.path.abspath(self._config_path)), 'lut_profiles'))
        self.lut_manager.ensure_builtin_profiles()

        # Sensitivity preset manager
        from utils.sensitivity_manager import SensitivityManager
        self.sensitivity_manager = SensitivityManager(self.config, self._config_path)
        active_preset = self.sensitivity_manager.get_active_preset()
        self.sensitivity_manager.apply_preset(active_preset, self)
        self.logger.info(f"Sensitivity preset '{active_preset}' applied")

        # time spent on each image when looping over a directory
        self.image_loop_time = self.config.getint('Visualisation', 'image_loop_time')

        # setup the track bars if show_display is True. A headless OpenCV
        # build (or no display attached) cannot open windows, so fall back to
        # running without the preview instead of crashing at startup.
        if self.show_display and not has_gui_support():
            self.logger.warning(
                'Display requested but this OpenCV build has no GUI support '
                '(headless install or no display attached). Continuing without '
                'the preview window.')
            self.show_display = False

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
        self.camera_type = self.config.get('Camera', 'camera_type', fallback='auto')
        # White balance - live-tunable via MQTT set_config_section (see
        # mqtt_manager CAMERA_LIVE_KEYS); attrs kept current for dashboard state
        self.awb_mode = self.config.get('Camera', 'awb_mode', fallback='auto').lower()
        self.awb_red_gain = self.config.getfloat('Camera', 'awb_red_gain', fallback=2.0)
        self.awb_blue_gain = self.config.getfloat('Camera', 'awb_blue_gain', fallback=2.0)
        # Profiles saved before the WB keys existed lack them; write the
        # resolved values back so the next save persists them and the kiosk
        # config editor (renders present keys only) shows the WB fields.
        for wb_key, wb_value in (('awb_mode', self.awb_mode),
                                 ('awb_red_gain', self.awb_red_gain),
                                 ('awb_blue_gain', self.awb_blue_gain)):
            if not self.config.has_option('Camera', wb_key):
                self.config.set('Camera', wb_key, str(wb_value))
        # Latest picamera2 per-frame metadata (ColourGains, ColourTemperature,
        # Lux, ...) for dashboard state and the calibrate_awb command. Written
        # by the main loop only; other threads read the reference.
        self._last_camera_metadata = None
        # Shutter cap / gain limit for data collection at speed
        # (utils/exposure_governor.py). Off unless enabled in config or by
        # the controller's collect screen (set_capture_params).
        self._load_capture_settings()
        # auto: upright for GS (imx296) OWL 3.0 mounts, 180 for the classic
        # enclosure (HQ/CM3 mounted upside down); 0|180 overrides per unit
        self.camera_rotation = self.config.get('Camera', 'rotation',
                                               fallback='auto').strip().lower()

        # BOARD pins this unit's own GPS hardware uses ([GPS] source = nrf91, or a
        # serial GPS on the Pi UART). A relay, LED or switch configured on one is
        # disabled with a loud error instead of fighting the modem or receiver.
        self.protected_pins = ConfigValidator.protected_pins(self.config)

        # Relay Dict maps the reference relay number to a boardpin on the embedded device
        self.relay_dict = {}

        # What the nozzles do on a spraying fault (camera lost, control lost):
        # 'off' = detection off, all nozzles off; 'on' = blanket spray when
        # detection can no longer run. Either way the fault is published.
        self.on_error = self.config.get('System', 'on_error', fallback='off').strip().lower()
        self._fault_state = None

        # use the [Relays] section to build the dictionary
        for key, value in self.config['Relays'].items():
            self.relay_dict[int(key)] = int(value)

        # instantiate the relay controller - successful start should beep the buzzer
        try:
            self.relay_controller = RelayController(relay_dict=self.relay_dict,
                                                    disabled_pins=self.protected_pins)

        except errors.OWLAlreadyRunningError:
            self.logger.critical("OWL initialization failed: GPIO pin conflict. Another OWL instance may be running.",
                                 exc_info=True)
            raise
        self.relay_controller.on_stuck = self._relay_stuck

        ### Data collection only ###
        detection_enable_config = self.config.getboolean('DataCollection', 'detection_enable', fallback=False)
        # First-boot setup mode: keep relays silent while the customer runs
        # the camera check on the bench (see controller/setup/README.md).
        # The flag is stat-ed once here; while it was present at boot, the
        # update_state thread keeps detection forced off (and re-stats the
        # flag at a slow cadence to notice teardown). With no flag at boot,
        # nothing is ever re-checked — zero cost on normal operation.
        first_boot_flag = Path(os.environ.get('OWL_FIRSTBOOT_FLAG', '/boot/firmware/owl-firstboot.flag'))
        self._firstboot_flag_path = first_boot_flag
        self._firstboot_active = first_boot_flag.exists()
        self._firstboot_last_check = 0.0
        if self._firstboot_active:
            self.logger.info("First-boot flag present: detection locked off until setup finishes")
            detection_enable_config = False
        self.detection_enable = Value('b', detection_enable_config)
        self.image_sample_enable = Value('b', self.config.getboolean('DataCollection', 'image_sample_enable',
                                                                     fallback=False))

        # Local cached versions for performance
        self._detection_enable = self.detection_enable.value
        self._image_sample_enable = self.image_sample_enable.value
        self._STATE_CHECK_INTERVAL = 0.1
        self.stop_state_update = threading.Event()

        ####################### DASHBOARD ############################
        self.dash = None
        # The detector hoot() is actually running (None until it builds
        # one). Read by the dashboard heartbeat thread, so set before it starts.
        self.running_algorithm = None
        self.stream_active = None
        self.latest_stream_frame = None
        # Commands the dashboard queues for the main loop. Set before the MQTT
        # client starts: the controller re-sends its choices (model,
        # algorithm) the moment this OWL connects, and resetting these later
        # in __init__ wiped them, so a rebooted OWL came back on another model.
        self._pending_algorithm = None
        self._pending_trackbar_updates = {}
        self._pending_model = None
        self._pending_detect_classes = None
        self._pending_lut_profile = None
        self._pending_lut_sensitivity = None
        # Driver paused recording (dashboard): session stays open, nothing saved
        self._recording_paused = False

        mqtt_enable = self.config.getboolean('MQTT', 'enable', fallback=False)

        broker_ip = self.config.get('MQTT', 'broker_ip', fallback='localhost')
        broker_port = self.config.getint('MQTT', 'broker_port', fallback=1883)
        device_id = self.config.get('MQTT', 'device_id', fallback='auto')
        network_mode = self.config.get('Network', 'mode', fallback=None)
        static_ip = self.config.get('Network', 'static_ip', fallback=None)
        client_id = f"client_{device_id}"

        if mqtt_enable:
            try:
                from utils.mqtt_manager import OWLMQTTPublisher

                self.dash = OWLMQTTPublisher(
                    broker_host=broker_ip,
                    broker_port=broker_port,
                    client_id=client_id,
                    device_id=device_id,
                    network_mode=network_mode,
                    static_ip=static_ip)

                self.dash.set_owl_instance(self)

                self.dash.start()

                # Enhanced logging
                mode = 'NETWORKED' if self.dash.networked_mode else 'STANDALONE'
                self.logger.info(f"MQTT enabled - Mode: {mode}")
                self.logger.info(f"MQTT Broker: {broker_ip}:{broker_port}")
                self.logger.info(f"Device ID: {device_id}")
                self.logger.info(f"Client ID: {client_id}")

                # Errors go to the dashboards on owl/errors. Attached always:
                # the connection completes later on paho's thread, so the old
                # `if self.dash.connected` check was almost always False and
                # errors never left the OWL. paho queues while offline.
                LogManager.add_mqtt_handler(
                    mqtt_client=self.dash.client,
                    mqtt_error_topic=self.dash.topics['errors']
                )

            except Exception as e:
                self.dash = None
                self.logger.warning(f"Failed to initialize MQTT: {e}. Dashboard disabled, OWL continues.")

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

        # GPS setup
        self.gps_source = self.config.get('GPS', 'source', fallback='none').lower()
        self.gps_data = None
        self._gps_state = None
        self._gps_serial = None
        self._gps_running = False
        self.gps_status_led = None
        # Per-session GPS track (same GeoJSON format as the networked
        # controller's tracks/): starts/stops with each recording session,
        # file lands inside the session dir so it ships in session ZIPs.
        self.track_recorder = TrackRecorder()

        if self.gps_source in ('serial', 'nrf91'):
            # nrf91 = the nRF9151 modem on the production CM5 carrier (UART3,
            # Serial LTE Modem firmware); serial = any receiver streaming NMEA.
            default_port, default_baud = (('/dev/ttyAMA3', 460800) if self.gps_source == 'nrf91'
                                          else ('/dev/ttyUSB1', 115200))
            self.gps_port = self.config.get('GPS', 'port', fallback=default_port)
            self.gps_baudrate = self.config.getint('GPS', 'baudrate', fallback=default_baud)

            if serial is None:
                self.logger.error("pyserial not installed; serial GPS disabled. Install with: pip install pyserial")
                # Fall back to dashboard (browser geolocation) if available, otherwise disable
                if self.dash:
                    self.gps_source = 'dashboard'
                    self.logger.info("Falling back to dashboard GPS (browser geolocation)")
                else:
                    self.gps_source = 'none'
            else:
                self._gps_state = GPSState()
                self._gps_running = True
                gps_led_pin = self._allowed_board_pin('gps_led_pin', 38)
                if gps_led_pin:
                    self.gps_status_led = GPSStatusLED(pin=gps_led_pin)
                self._gps_thread = threading.Thread(target=self._serial_gps_reader, daemon=True)
                self._gps_thread.start()
                self.logger.info(f"GPS reader started ({self.gps_source}) on {self.gps_port} @ {self.gps_baudrate}")

        elif self.gps_source != 'none':
            # Non-serial, non-none source (e.g. 'tcp') — fall back to dashboard GPS
            if self.dash:
                self.gps_source = 'dashboard'

        # Create GPS status LED on hardware setups (UTE/Advanced controller) even if GPS
        # source is 'none' — the LED shows whether dashboard browser GPS is active.
        # The LED state is driven by _get_best_gps_data() in the update_state loop.
        if self.gps_status_led is None:
            controller_type_check = self.config.get('Controller', 'controller_type', fallback='none').strip("'\" ").lower()
            if controller_type_check in ('ute', 'advanced'):
                gps_led_pin = self._allowed_board_pin('gps_led_pin', 38)
                if gps_led_pin:
                    self.gps_status_led = GPSStatusLED(pin=gps_led_pin)

        # if a controller is connected, sample images must be true to set up directories correctly
        self.controller_type = self.config.get('Controller', 'controller_type').strip("'\" ").lower()
        self.switch_purpose = self.config.get('Controller', 'switch_purpose', fallback='recording').strip("'\" ").lower()

        if self.controller_type not in {'none', 'ute', 'advanced'}:
            self.logger.error(f"Invalid controller type: {self.controller_type}")
            raise errors.ControllerTypeError(self.controller_type, valid_types=list({'none', 'ute', 'advanced'}))

        # Create status indicator EARLY so it can signal boot errors via LED.
        # save_directory is set to None for now; updated after storage setup succeeds.
        # status_led_pin can be 'none' or empty to disable the LED.
        status_led_pin = self._allowed_board_pin('status_led_pin', 40)
        if self.controller_type == 'ute':
            self.status_indicator = UteStatusIndicator(save_directory=None,
                                                       status_led_pin=status_led_pin)
        elif self.controller_type == 'advanced':
            self.status_indicator = AdvancedStatusIndicator(save_directory=None,
                                                            status_led_pin=status_led_pin)
        else:
            self.status_indicator = HeadlessStatusIndicator(save_directory=None, no_save=True)

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

            # usb: recording requires a /media mount; internal: eMMC/SD path
            # (sealed OWL 3.0 units); auto (default): USB when present, else
            # internal so recording "just works" — the min_free_gb floor
            # watchdog ring-fences the system disk. See directory_manager.
            # Empty values fall back exactly like missing keys — a blank
            # `storage_location =` line must not degrade auto to usb-only,
            # and a blank internal path must not disable the auto fallback.
            self.storage_location = (self.config.get(
                'DataCollection', 'storage_location', fallback='').strip().lower() or 'auto')
            self.internal_save_directory = (self.config.get(
                'DataCollection', 'internal_save_directory', fallback='').strip()
                or '/home/owl/owl_images')
            self.min_free_gb = self.config.getint('DataCollection', 'min_free_gb', fallback=4)
            self.image_quota_gb = self.config.getint('DataCollection', 'image_quota_gb', fallback=12)

            # The storage watchdog enforces the internal free-space floor
            self.status_indicator.storage_location = self.storage_location
            self.status_indicator.min_free_gb = self.min_free_gb

            try:
                self.directory_manager = DirectorySetup(
                    save_directory=self.save_directory,
                    storage_location=self.storage_location,
                    internal_save_directory=self.internal_save_directory,
                    min_free_gb=self.min_free_gb)
                self.save_directory, self.save_subdirectory = self.directory_manager.setup_directories()

                self.image_recorder = ImageRecorder(save_directory=self.save_subdirectory, mode=self.sample_method,
                                                    jpeg_quality=self.jpeg_quality)

                # Update status indicator with the resolved save_directory for storage monitoring
                self.status_indicator.save_directory = self.save_directory
                # auto mode resolves to usb or internal at runtime
                self.status_indicator.storage_location = self.directory_manager.resolved_location
                if self.dash:
                    self.dash.set_recording_storage(
                        self.directory_manager.resolved_location,
                        fallback=self._is_storage_fallback())

            except (errors.NoWritableUSBError, errors.USBMountError, errors.USBWriteError,
                    errors.StorageSystemError) as e:
                # A missing USB drive must not stop the OWL: detection runs
                # normally and recording stays unavailable until a drive
                # appears. Every record toggle re-scans for one — see
                # _retry_storage_setup().
                self.logger.warning(f"No writable recording storage found: {e}")
                if self.storage_location == 'usb':
                    self.logger.warning("Detection will run; recording is disabled until a USB drive is inserted.")
                else:
                    self.logger.warning("Detection will run; recording is disabled until storage is available "
                                        "(see the error above for whether to insert a drive, free space, or fix permissions).")
                self._no_drive_warned = True  # already warned at boot; retries log at DEBUG
                self.save_directory = None
                self.save_subdirectory = None
                self.image_recorder = None
                if self.dash:
                    self.dash.set_storage_available(False)
                    self.dash.set_recording_blocked(
                        self._classify_storage_error(e))

        ############################

        # initialise controller buttons and async management
        if self.controller_type == 'ute':
            # Status indicator already created above — just set up controller
            self.controller = UteController(
                detection_state=self.detection_enable,
                sample_state=self.image_sample_enable,
                stop_flag=Value('b', False),
                owl_instance=self,
                status_indicator=self.status_indicator,
                switch_purpose=self.config.get('Controller', 'switch_purpose', fallback='recording').strip("'\" ").lower(),
                switch_board_pin=self._allowed_board_pin('switch_pin', 36)
            )
            self.controller_process = Process(target=self.controller.run)
            self.controller_process.start()

        elif self.controller_type == 'advanced':
            if not hasattr(self, 'sensitivity_level'):
                self.sensitivity_level = Value('i', Sensitivity.HIGH.value)

            # Status indicator already created above
            self.controller = AdvancedController(
                recording_state=self.image_sample_enable,
                sensitivity_level=self.sensitivity_level,
                detection_mode_state=Value('i', 1),
                stop_flag=Value('b', False),
                owl_instance=self,
                status_indicator=self.status_indicator,
                sensitivity_manager=self.sensitivity_manager,
                recording_bpin=self._allowed_board_pin('recording_pin', 38),
                sensitivity_bpin=self._allowed_board_pin('sensitivity_pin', 40),
                detection_mode_bpin_up=self._allowed_board_pin('detection_mode_pin_up', 36),
                detection_mode_bpin_down=self._allowed_board_pin('detection_mode_pin_down', 35)
            )
            self.controller_process = Process(target=self.controller.run)
            self.controller_process.start()

        else:
            self.controller = None
            # Status indicator already created above; start storage monitoring if recording
            if self._image_sample_enable:
                self.status_indicator.start_storage_indicator()

        # Infrastructure is set up. Now set the actual initial recording state.
        # Dashboard-only (no hardware controller): start OFF — user enables via dashboard.
        # Hardware controllers: switch position was read during controller init above.
        if self.dash and self.controller_type == 'none':
            self._image_sample_enable = False
            with self.image_sample_enable.get_lock():
                self.image_sample_enable.value = False
            self.dash.set_image_sample_enable(False)

        # Actuation timing — read from [Actuation] (CONTROLLER.ini), fall back to [System]
        if self.config.has_section('Actuation'):
            self.actuation_duration = self.config.getfloat('Actuation', 'actuation_duration',
                                                           fallback=self.config.getfloat('System', 'actuation_duration'))
            self.delay = self.config.getfloat('Actuation', 'delay',
                                              fallback=self.config.getfloat('System', 'delay'))
        else:
            self.actuation_duration = self.config.getfloat('System', 'actuation_duration')
            self.delay = self.config.getfloat('System', 'delay')

        # Same bounds the MQTT path applies. A hand-edited INI reached the
        # relays unchecked, so a misplaced decimal point sprayed for minutes.
        clamped_duration, clamped_delay = ConfigValidator.clamp_actuation(
            self.actuation_duration, self.delay)
        if (clamped_duration, clamped_delay) != (self.actuation_duration, self.delay):
            self.logger.warning(
                f'[CONFIG] actuation clamped to safety bounds: duration '
                f'{self.actuation_duration}s -> {clamped_duration}s, '
                f'delay {self.delay}s -> {clamped_delay}s')
        self.actuation_duration, self.delay = clamped_duration, clamped_delay

        # Loop time tracking (30-frame rolling window)
        self._loop_times = deque(maxlen=30)
        self._avg_loop_time_ms = 0.0

        # Spray timing from this OWL's own GPS (serial / nrf91). A fresh
        # hardware fix always wins; without one the controller's broadcast
        # applies (mqtt_manager._handle_set_actuation_params), and the
        # configured values when there is no controller either. Phone or
        # browser GPS never drives timing.
        self.actuation_source = 'config'
        self._config_actuation = (self.actuation_duration, self.delay)
        self._device_timing = False
        self._controller_timing_at = float('-inf')
        self._controller_speed_kmh = None
        self.speed_kmh = None
        self.speed_warning = ''
        self._speed_averager = SpeedAverager(
            window_seconds=self.config.getfloat('Actuation', 'speed_avg_window', fallback=2.0))
        self._actuation_calc = ActuationCalculator(
            actuation_length_cm=self.config.getfloat('Actuation', 'actuation_length_cm', fallback=10.0),
            offset_cm=self.config.getfloat('Actuation', 'offset_cm', fallback=30.0),
            fallback_duration=self.actuation_duration,
            fallback_delay=self.delay)
        # Per detection frame: capture-to-relay time, and how far that ran past
        # the camera-to-nozzle travel time (a spray that started late)
        self._latency_s = deque(maxlen=30)
        self._late_s = deque(maxlen=30)
        self._last_timing_update = 0.0
        # System stats are gathered in update_state every few seconds, never in
        # the detection loop. Prime psutil so non-blocking reads are meaningful.
        self._last_stats_at = time.monotonic()
        try:
            import psutil
            psutil.cpu_percent(interval=None)
        except ImportError:
            pass

        if self.dash or self.controller_type != 'none' or self._gps_state is not None:
            threading.Thread(target=self.update_state, daemon=True).start()
            self.logger.info("State monitoring thread started")

        self.relay_vis = None

        # Check which Raspberry Pi is being used and adjust the resolution accordingly.
        # Use `cat /proc-device-tree/model` to check the model of the Raspberry Pi.
        # Capture the requested (config) resolution BEFORE any clamp so the dashboard
        # can detect a silent clamp and prompt the user to opt in via the override flag.
        self.requested_resolution = self.resolution
        self.resolution_clamped = False
        total_pixels = self.resolution[0] * self.resolution[1]
        allow_high_resolution = self.config.getboolean('Camera', 'allow_high_resolution', fallback=False)

        if (self.RPI_VERSION in ['rpi-3', 'rpi-4']) and total_pixels > (832 * 640) and not allow_high_resolution:
            # the older versions of the Pi are known to 'brick' and become unusable if too high resolutions are used.
            # Set [Camera] allow_high_resolution = True to override this clamp once you have verified your hardware.
            self.resolution = (640, 480)
            self.resolution_clamped = True
            self.logger.warning(
                f"Resolution {self.requested_resolution[0]}x{self.requested_resolution[1]} is above the safe "
                f"limit for {self.RPI_VERSION}. Clamped to 640x480. "
                f"Set [Camera] allow_high_resolution = True to override.")
        elif allow_high_resolution and self.RPI_VERSION in ['rpi-3', 'rpi-4'] and total_pixels > (832 * 640):
            self.logger.warning(
                f'High-res override active on {self.RPI_VERSION} at {self.resolution[0]}x{self.resolution[1]} '
                f'- expect low framerate and verify camera stability.')
        elif total_pixels > (832 * 640):
            self.logger.warning(
                f'High resolution, expect low framerate. Resolution set to {self.resolution[0]}x{self.resolution[1]}.')
        else:
            self.logger.info(
                f'Resolution set to {self.resolution[0]}x{self.resolution[1]}.')

        self.frame_width = None
        self.frame_height = None

        try:
            self.cam = self.setup_media_source(input_file_or_directory, camera_type=self.camera_type)
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
        self.actuation_zone = self.config.getint('System', 'actuation_zone', fallback=100)

        # GreenOnGreen / hybrid config
        self.inference_resolution = self.config.getint('GreenOnGreen', 'inference_resolution', fallback=320)
        self.crop_buffer_px = self.config.getint('GreenOnGreen', 'crop_buffer_px', fallback=20)
        self._gog_detector = None
        # When True the dashboard preview streams the full uncropped frame (used by
        # the geometry editor so its overlay maps to the whole image). Default: the
        # preview shows the cropped frame (what the detector actually sees).
        self._stream_full_frame = False

        # Tracking config (ByteTrack + class smoothing + crop mask persistence)
        self.tracking_enabled = self.config.getboolean('Tracking', 'tracking_enabled', fallback=False)
        self.track_high_thresh = self.config.getfloat('Tracking', 'track_high_thresh', fallback=0.2)
        self.track_low_thresh = self.config.getfloat('Tracking', 'track_low_thresh', fallback=0.05)
        self.new_track_thresh = self.config.getfloat('Tracking', 'new_track_thresh', fallback=0.2)
        self.track_buffer = self.config.getint('Tracking', 'track_buffer', fallback=60)
        self.match_thresh = self.config.getfloat('Tracking', 'match_thresh', fallback=0.7)
        self._track_class_window = self.config.getint('Tracking', 'track_class_window', fallback=5)
        self._track_crop_persist = self.config.getint('Tracking', 'track_crop_persist', fallback=3)
        self.detection_persist_frames = self.config.getint(
            'Tracking', 'detection_persist_frames', fallback=5)
        self._class_smoother = None
        self._crop_stabilizer = None
        if self.tracking_enabled:
            from utils.tracker import ClassSmoother, CropMaskStabilizer
            # Both created regardless of algorithm — smoother unused in hybrid mode
            # but algorithm can change mid-session, so both must be ready
            self._class_smoother = ClassSmoother(window=self._track_class_window)
            self._crop_stabilizer = CropMaskStabilizer(max_age=self._track_crop_persist)
            self.logger.info(f'Tracking enabled: class_window={self._track_class_window}, '
                             f'crop_persist={self._track_crop_persist}')

        # Per-edge crop fractions (inset from each edge, 0.0-0.49) to reduce edge
        # artifacts / exclude a boom or wheel track. Backward-compatible with the
        # legacy symmetric crop_factor_horizontal/vertical (used only as fallback).
        self.crop_factor_horizontal = self.config.getfloat('Camera', 'crop_factor_horizontal', fallback=0.1)
        self.crop_factor_vertical = self.config.getfloat('Camera', 'crop_factor_vertical', fallback=0.1)
        self.crop_left = self.config.getfloat('Camera', 'crop_left', fallback=self.crop_factor_horizontal)
        self.crop_right = self.config.getfloat('Camera', 'crop_right', fallback=self.crop_factor_horizontal)
        self.crop_top = self.config.getfloat('Camera', 'crop_top', fallback=self.crop_factor_vertical)
        self.crop_bottom = self.config.getfloat('Camera', 'crop_bottom', fallback=self.crop_factor_vertical)

        # Actuation band — a weed fires only while its centre is inside this band of
        # the cropped frame (fractions of cropped height; 0=top, 1=bottom). Backward-
        # compatible with the legacy bottom-anchored actuation_zone %.
        self.actuation_top = self.config.getfloat(
            'System', 'actuation_top', fallback=1.0 - self.actuation_zone / 100.0)
        self.actuation_bottom = self.config.getfloat('System', 'actuation_bottom', fallback=1.0)

        # Derive crop_slice, lane coords and actuation band pixel bounds.
        self.recompute_geometry()

        # The dashboard took its state snapshot early in __init__, before
        # controller_type, save_directory and the resolution clamp were set.
        if self.dash:
            self.dash._sync_parameters_to_state()

    @property
    def config_path(self):
        return self._config_path

    def recompute_geometry(self):
        """Recompute crop slice, lane coordinates and actuation band pixel bounds
        from the current per-edge crop fractions, actuation band and relay_num.

        Event-driven and cheap: called once from __init__ and again whenever a
        geometry config value changes via MQTT. NEVER call this per-frame. Attribute
        rebinding is atomic in CPython, so the detection loop reading self.crop_slice
        always sees a complete (old or new) value without a lock.
        """
        geo = compute_geometry(
            self.frame_width, self.frame_height,
            self.crop_left, self.crop_right, self.crop_top, self.crop_bottom,
            self.relay_num, self.actuation_top, self.actuation_bottom)
        if geo is None:
            self.logger.error('[ERROR] No frame width or frame height provided.')
            return

        # Attribute rebinding is atomic in CPython, so the detection loop always sees
        # a complete (old or new) value without a lock.
        self.cropped_width = geo['cropped_width']
        self.cropped_height = geo['cropped_height']
        self.crop_slice = geo['crop_slice']
        self.lane_width = geo['lane_width']
        self.lane_coords = geo['lane_coords']
        self.lane_coords_int = geo['lane_coords_int']
        self.actuation_y_top = geo['y_top']
        self.actuation_y_bottom = geo['y_bottom']

        left, right, top, bottom = geo['clamped_edges']
        self.logger.info(f'[INFO] Crop edges L{left} R{right} T{top} B{bottom} '
                         f'-> {self.cropped_width}x{self.cropped_height}px')

    def _detection_settings(self, algorithm):
        """The settings that produced this frame's detections, for EXIF.

        Only the algorithm NAME used to be recorded, which made a saved frame
        unreproducible — you could not tell what thresholds found (or missed)
        a weed. Returns only the keys that apply to the running algorithm, so
        an hsv frame does not claim ExG bounds it never used.
        """
        settings = {
            'min_detection_area_percent': self.min_detection_area_percent or None,
            # The preset name the main loop syncs in every mode. The Value
            # self.sensitivity_level only exists with an advanced/ute switch
            # panel, and reading it raised on every saved frame otherwise.
            'sensitivity_level': getattr(self, '_sensitivity_level', None),
        }
        if algorithm in ('exg', 'exhsv'):
            settings['exg_min'] = self.exg_min
            settings['exg_max'] = self.exg_max
        if algorithm in ('exhsv', 'hsv'):
            settings.update(hue_min=self.hue_min, hue_max=self.hue_max,
                            saturation_min=self.saturation_min,
                            saturation_max=self.saturation_max,
                            brightness_min=self.brightness_min,
                            brightness_max=self.brightness_max,
                            invert_hue=self.invert_hue)
        if algorithm == 'lut':
            settings['lut_profile'] = self.lut_profile or None
            settings['lut_sensitivity'] = self.lut_sensitivity
        if algorithm.startswith('gog'):
            settings['confidence'] = getattr(self, '_gog_confidence', None)
        return {k: v for k, v in settings.items() if v is not None}

    def _is_storage_fallback(self):
        """True when recording landed on internal storage only because no
        USB drive was present (configured usb/auto, resolved internal)."""
        resolved = getattr(getattr(self, 'directory_manager', None),
                           'resolved_location', None)
        return (resolved == 'internal'
                and getattr(self, 'storage_location', 'auto') != 'internal')

    @staticmethod
    def _classify_storage_error(e):
        """Map a storage setup exception to a recording_blocked_reason the
        dashboards/app can explain:
        no_drive | storage_full | floor_unreachable | storage_error."""
        if isinstance(e, (errors.NoWritableUSBError, errors.USBMountError,
                          errors.USBWriteError)):
            return 'no_drive'
        msg = str(e)
        if 'floor unreachable' in msg:
            # min_free_gb can never be met on this disk — deleting sessions
            # won't help; the floor itself must be lowered.
            return 'floor_unreachable'
        if 'free-space floor' in msg:
            return 'storage_full'
        return 'storage_error'

    def _save_directory_writable(self):
        save_dir = getattr(self, 'save_directory', None)
        return bool(save_dir) and os.access(save_dir, os.W_OK)

    def _retry_storage_setup(self):
        """One-shot re-scan for a writable recording location, run when
        recording is requested while none was found at boot (or the drive
        was removed).

        Returns True when storage was found and the ImageRecorder is ready.
        """
        try:
            self.directory_manager = DirectorySetup(
                save_directory=self.config.get('DataCollection', 'save_directory'),
                storage_location=getattr(self, 'storage_location', 'auto'),
                internal_save_directory=getattr(self, 'internal_save_directory', None),
                min_free_gb=getattr(self, 'min_free_gb', 0))
            self.save_directory, self.save_subdirectory = self.directory_manager.setup_directories(max_retries=1)
            self.image_recorder = ImageRecorder(save_directory=self.save_subdirectory, mode=self.sample_method,
                                                    jpeg_quality=self.jpeg_quality)
            self.status_indicator.save_directory = self.save_directory
            self.status_indicator.storage_location = self.directory_manager.resolved_location
            if self.status_indicator.error_code in (5, 6):  # storage codes only
                self.status_indicator.clear_error()
            self._no_drive_warned = False
            if self.dash:
                self.dash.set_storage_available(True)
                self.dash.set_recording_storage(
                    self.directory_manager.resolved_location,
                    fallback=self._is_storage_fallback())
            self.logger.info(f"Recording storage ready: {self.save_directory} "
                             f"({self.directory_manager.resolved_location})")
            return True
        except (errors.NoWritableUSBError, errors.USBMountError, errors.USBWriteError,
                errors.StorageSystemError) as e:
            # Edge-triggered: switch-fitted units re-assert recording every
            # cycle, so repeat attempts without a drive change log at DEBUG.
            if not getattr(self, '_no_drive_warned', False):
                self._no_drive_warned = True
                self.logger.warning(f"Recording requested but no writable storage found: {e}")
            else:
                self.logger.debug(f"Recording requested but no writable storage found: {e}")
            if self.dash:
                self.dash.set_storage_available(False)
                self.dash.set_recording_blocked(self._classify_storage_error(e))
            return False

    def hoot(self):
        # Signal successful boot with LED blink
        if hasattr(self, 'status_indicator') and hasattr(self.status_indicator, 'setup_success'):
            self.status_indicator.setup_success()

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
        _zone_tracking_warned = False

        # GoB shared config — already initialised in __init__() and may have been
        # updated by SensitivityManager.apply_preset(), so do NOT re-read from config.

        def _create_detector(algo):
            """Three-way detector factory."""
            current_classes = self._detect_classes_list or None
            # [Tracking] INI values are the runtime source of truth — the
            # tracker yaml only bootstraps ByteTrack before these apply.
            tracker_params = {
                'track_high_thresh': self.track_high_thresh,
                'track_low_thresh': self.track_low_thresh,
                'new_track_thresh': self.new_track_thresh,
                'track_buffer': self.track_buffer,
                'match_thresh': self.match_thresh,
            }
            if algo == 'gog':
                from utils.greenongreen import GreenOnGreen
                return GreenOnGreen(
                    model_path=self._model_path,
                    confidence=self._gog_confidence,
                    detect_classes=current_classes,
                    tracking_enabled=self.tracking_enabled,
                    detection_persist_frames=self.detection_persist_frames,
                    tracker_params=tracker_params,
                )
            elif algo == 'gog-hybrid':
                from utils.greenongreen import GreenOnGreen
                return GreenOnGreen(
                    model_path=self._model_path,
                    confidence=self._gog_confidence,
                    detect_classes=current_classes,
                    hybrid_mode=True,
                    inference_resolution=self.inference_resolution,
                    crop_buffer_px=self.crop_buffer_px,
                    tracking_enabled=self.tracking_enabled,
                    crop_stabilizer=self._crop_stabilizer,
                    detection_persist_frames=self.detection_persist_frames,
                    tracker_params=tracker_params,
                )
            elif algo == 'lut':
                if not self.lut_profile:
                    raise ValueError(
                        'LUT algorithm selected but no lut_profile is set. '
                        'Paint one via the dashboard weed painter')
                lut_table = self.lut_manager.load_and_bake(
                    self.lut_profile, self.lut_sensitivity)
                return GreenOnBrown(algorithm='lut', lut_table=lut_table)
            else:
                return GreenOnBrown(algorithm=algo)

        try:
            weed_detector = _create_detector(algorithm)
            if algorithm in ('gog', 'gog-hybrid'):
                self._gog_detector = weed_detector

        except Exception as e:
            self.logger.error(f"[ERROR] Failed to create detector for '{algorithm}': {e}")
            weed_detector = None
            if self.dash:
                self.dash.state['algorithm_error'] = str(e)
            if algorithm in ('lut', 'gog', 'gog-hybrid'):
                # Fail-safe: a missing/corrupt painted profile, or AI on a
                # unit without ultralytics or a model, must not leave the unit
                # without detection in the field — fall back to exhsv. The
                # config file keeps its value; algorithm_error says why.
                try:
                    algorithm = 'exhsv'
                    weed_detector = _create_detector(algorithm)
                    self.logger.error("[ERROR] Falling back to 'exhsv' detection")
                except Exception as fallback_error:
                    self.logger.error(f"[ERROR] exhsv fallback also failed: {fallback_error}")
            if weed_detector is None:
                self.logger.error("[ERROR] OWL will continue running without detection. Change algorithm via dashboard to recover.")
        self.running_algorithm = algorithm if weed_detector is not None else None
        if self.dash and self.running_algorithm:
            self.dash.state['algorithm'] = self.running_algorithm

        if self.show_display:
            self.relay_vis = self.relay_controller.relay_vis
            self.relay_vis.setup()
            self.relay_controller.vis = True

        prev_detection_enable = False
        prev_recording_enable = False

        dropped_frames = 0
        try:
            while True:
                loop_start = time.time()
                frame, camera_metadata = self.cam.read_with_metadata()
                self._last_camera_metadata = camera_metadata
                if camera_metadata:
                    self._govern_exposure(camera_metadata)
                capture_time = self._frame_capture_time(camera_metadata)

                if frame is None:
                    # Replaying a directory or video: None is the legitimate
                    # end of input, so stop immediately.
                    if self.input_file_or_directory:
                        self.logger.info("[INFO] End of input. Stopped.")
                        self.stop()
                        break

                    # Live camera: a single dropped or late frame used to end
                    # the job on one quiet log line. Retry briefly, and if the
                    # camera really is gone say so loudly.
                    dropped_frames += 1
                    if dropped_frames <= MAX_DROPPED_FRAMES:
                        if dropped_frames == 1:
                            self.logger.warning('[WARNING] No frame from camera - retrying')
                        time.sleep(0.05)
                        continue
                    if (self._fault_state or {}).get('code') != 'camera_lost':
                        self._fault('camera_lost',
                                    f'No frame from camera after {MAX_DROPPED_FRAMES} '
                                    f'retries - camera disconnected?',
                                    detection_possible=False)
                    if self._fault_state['action'] != 'blanket':
                        self.stop()
                        break
                    # on_error = on: blanket spray while the operator has
                    # spraying on, and keep trying the camera.
                    if self._detection_enable:
                        self.relay_controller.relay.all_on()
                    else:
                        self.relay_controller.relay.all_off()
                    time.sleep(0.5)
                    continue
                if dropped_frames:
                    self.logger.info(f'[INFO] Camera recovered after {dropped_frames} '
                                     f'dropped frame(s)')
                    dropped_frames = 0
                    self._clear_fault('camera_lost')

                # Reset tracker when detection toggled off
                if prev_detection_enable and not self._detection_enable:
                    # Loop stats measure detection throughput only — reset so
                    # the dashboard frame-rate reads '--' while idle instead
                    # of the (near-instant) empty loop rate.
                    self._loop_times.clear()
                    self._avg_loop_time_ms = 0.0
                    self._latency_s.clear()
                    self._late_s.clear()
                    if (self.tracking_enabled and weed_detector
                            and hasattr(weed_detector, 'reset_tracker')):
                        weed_detector.reset_tracker()
                        if self._class_smoother:
                            self._class_smoother.reset()
                        self.logger.info("Tracker state reset (detection disabled)")
                prev_detection_enable = self._detection_enable

                # Create new session directory when recording starts
                if self._image_sample_enable and not prev_recording_enable:
                    # No drive at boot, or it has been removed or gone
                    # read-only since: re-scan now so plugging a USB in
                    # mid-session just works. Checking only for an empty
                    # path missed a pulled drive, and the session folder
                    # creation below then raised and stopped the OWL.
                    if not self._save_directory_writable():
                        self._retry_storage_setup()

                    if self._save_directory_writable():
                        # One clock reading for both, so a start at midnight
                        # can't put the folder under the wrong day. The name
                        # carries this OWL's id + serial: OWLs started by one
                        # tap on the controller would otherwise collide.
                        started = datetime.now()
                        session_name = session_folder_name(
                            started, self.dash.device_id if self.dash else socket.gethostname(),
                            get_device_serial())
                        date_dir = os.path.join(self.save_directory, started.strftime('%Y%m%d'))
                        session_dir = os.path.join(date_dir, session_name)
                        os.makedirs(session_dir, exist_ok=True)
                        # Stamped into every image's EXIF: the directory name is
                        # the only other record, and it is lost the moment files
                        # are copied out of the session folder.
                        self._session_id = session_name
                        self._session_start = datetime.now(timezone.utc).isoformat(
                            timespec='seconds').replace('+00:00', 'Z')

                        # Stop old ImageRecorder, create new one for this session
                        if hasattr(self, 'image_recorder') and self.image_recorder:
                            self.image_recorder.stop(wait=False)
                        self.image_recorder = ImageRecorder(
                            save_directory=session_dir, mode=self.sample_method,
                            jpeg_quality=self.jpeg_quality)
                        self.capture_scheduler.reset()
                        self._close_video_clip()
                        self._video_started_at = None
                        self._recording_stopped_reason = None
                        self.logger.info(f"New recording session: {session_dir}")
                        # GPS track alongside the images (flush any previous
                        # session's tail first — start() resets buffers)
                        if self.track_recorder.recording:
                            self.track_recorder.stop()
                        self.track_recorder.start(session_dir)
                        if self.dash:
                            # Metadata arrives before recording starts and is
                            # buffered in state — write it into the fresh
                            # session dir now, and (re)publish where recording
                            # is landing so the app can warn on fallback.
                            self.dash.save_session_metadata()
                            self.dash.set_recording_storage(
                                self.directory_manager.resolved_location,
                                fallback=self._is_storage_fallback())
                    else:
                        # Still no drive — refuse to record and snap the
                        # toggle back off everywhere so state stays honest.
                        if self.dash:
                            self.dash.set_image_sample_enable(False)
                        if hasattr(self, 'image_sample_enable'):
                            with self.image_sample_enable.get_lock():
                                self.image_sample_enable.value = False
                        self._image_sample_enable = False
                # Recording stopped: close out the session GPS track
                if prev_recording_enable and not self._image_sample_enable:
                    if self.track_recorder.recording:
                        self.track_recorder.stop()
                    self._close_video_clip()
                prev_recording_enable = self._image_sample_enable

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

                # Live LUT profile / sensitivity updates. Drained BEFORE the
                # algorithm drain so a set_lut_profile + set_algorithm pair
                # arriving together builds the new detector from the new profile.
                if self._pending_lut_sensitivity is not None:
                    self.lut_sensitivity = self._pending_lut_sensitivity
                    self._pending_lut_sensitivity = None
                    # Re-bake now unless a profile switch is also pending
                    # (that drain bakes at the new sensitivity anyway).
                    if (self._pending_lut_profile is None and algorithm == 'lut'
                            and weed_detector is not None
                            and hasattr(weed_detector, 'set_lut') and self.lut_profile):
                        try:
                            weed_detector.set_lut(self.lut_manager.load_and_bake(
                                self.lut_profile, self.lut_sensitivity))
                            self.logger.info(f"LUT re-baked at sensitivity {self.lut_sensitivity}")
                        except Exception as e:
                            self.logger.error(f"LUT sensitivity re-bake failed: {e}")

                if self._pending_lut_profile is not None:
                    new_lut_profile = self._pending_lut_profile
                    self._pending_lut_profile = None
                    try:
                        lut_table = self.lut_manager.load_and_bake(
                            new_lut_profile, self.lut_sensitivity)
                        self.lut_profile = new_lut_profile
                        if algorithm == 'lut' and weed_detector is not None \
                                and hasattr(weed_detector, 'set_lut'):
                            weed_detector.set_lut(lut_table)
                        self.logger.info(f"LUT profile switched to: {new_lut_profile}")
                        if self.dash:
                            self.dash.state['algorithm_error'] = None
                    except Exception as e:
                        self.logger.error(f"Failed to load LUT profile '{new_lut_profile}': {e}")
                        if self.dash:
                            self.dash.state['lut_profile'] = self.lut_profile
                            self.dash.state['algorithm_error'] = str(e)

                # Pre-load detectors/models outside detection guard so they're
                # ready instantly when the user enables detection.
                # Live algorithm switching
                if self._pending_algorithm and (weed_detector is None or self._pending_algorithm != algorithm):
                    try:
                        weed_detector = _create_detector(self._pending_algorithm)
                        algorithm = self._pending_algorithm
                        if algorithm in ('gog', 'gog-hybrid'):
                            self._gog_detector = weed_detector
                        self.running_algorithm = algorithm
                        # Only now does the config say so: a later save, or the
                        # dashboard persisting it, can never store a mode
                        # this unit failed to load.
                        self.config.set('System', 'algorithm', algorithm)
                        self.logger.info(f"Live algorithm switch to: {algorithm}")
                        if self.dash:
                            self.dash.state['algorithm'] = algorithm
                            self.dash.state['algorithm_error'] = None
                    except Exception as e:
                        self.logger.error(f"Failed to load detector for {self._pending_algorithm}: {e}")
                        # Revert — keep using current weed_detector and algorithm
                        if self.dash:
                            self.dash.state['algorithm'] = algorithm
                            self.dash.state['algorithm_error'] = str(e)
                    self._pending_algorithm = None
                    if self.dash:
                        self.dash.state['algorithm_pending'] = None

                # Live model switching
                if self._pending_model:
                    new_model = self._pending_model
                    self._pending_model = None
                    previous_model = self._model_path
                    try:
                        self._model_path = new_model
                        weed_detector = _create_detector(algorithm)
                        if algorithm in ('gog', 'gog-hybrid'):
                            self._gog_detector = weed_detector
                        self.logger.info(f"Live model switch to: {new_model}")
                        if self.dash:
                            self.dash.state['algorithm_error'] = None
                            # Saved only once it has loaded, so a reboot comes
                            # back on this model. With model_path = models
                            # each OWL picked its own model at boot.
                            with self.dash.config_lock:
                                if not self.config.has_section('GreenOnGreen'):
                                    self.config.add_section('GreenOnGreen')
                                self.config.set('GreenOnGreen', 'model_path', new_model)
                            self.dash._handle_save_config()
                    except Exception as e:
                        self.logger.error(f"Failed to switch model: {e}")
                        # Keep the model that works for later detector rebuilds
                        self._model_path = previous_model
                        if self.dash:
                            self.dash.state['algorithm_error'] = str(e)

                # Live detect_classes update
                if self._pending_detect_classes is not None:
                    new_classes = self._pending_detect_classes
                    self._pending_detect_classes = None
                    self._detect_classes_list = new_classes
                    if weed_detector and hasattr(weed_detector, 'update_detect_classes'):
                        weed_detector.update_detect_classes(new_classes or None)
                    self.logger.info(f"detect_classes updated: {new_classes}")

                # Live crop buffer update (hybrid mode only)
                if (algorithm == 'gog-hybrid'
                        and weed_detector
                        and hasattr(weed_detector, 'set_crop_buffer')
                        and weed_detector.crop_buffer_px != self.crop_buffer_px):
                    weed_detector.set_crop_buffer(self.crop_buffer_px)

                detection_ran = self._detection_enable and weed_detector is not None
                if detection_ran:
                    cropped_frame = frame[self.crop_slice]

                    # Min weed size: % of the detection frame wins over the
                    # legacy px value (resolution/crop independent)
                    if self.min_detection_area_percent > 0:
                        min_detection_area = max(1, int(round(
                            self.min_detection_area_percent / 100.0
                            * self.cropped_width * self.cropped_height)))
                    else:
                        min_detection_area = self.min_detection_area

                    # Annotated output costs an image.copy() + drawing, but the
                    # dashboard stream only reads every STREAM_FRAME_INTERVAL-th
                    # frame — skip the copy on frames nobody will see.
                    return_image_out = self.show_display or (
                        bool(self.dash) and frame_count % STREAM_FRAME_INTERVAL == 0)

                    persisted_boxes = []
                    if algorithm == 'gog':
                        cnts, boxes, weed_centres, image_out = weed_detector.inference(
                            cropped_frame,
                            confidence=self._gog_confidence,
                            show_display=return_image_out,
                            build_mask=(actuation_mode == 'zone' and not self.tracking_enabled)
                        )
                        if (self.tracking_enabled and actuation_mode == 'zone'
                                and not _zone_tracking_warned):
                            self.logger.warning(
                                'Zone actuation disabled while tracking is enabled; '
                                'using centre-based actuation instead')
                            _zone_tracking_warned = True

                        # Apply class smoothing: filter to target classes using majority-vote
                        if (self.tracking_enabled
                                and self._class_smoother
                                and weed_detector.last_track_ids):
                            smoothed = self._class_smoother.update(
                                weed_detector.last_track_ids,
                                weed_detector.last_class_ids,
                                weed_detector.last_confidences,
                                frame_count=frame_count
                            )
                            target_ids = set(weed_detector._detect_class_ids or [])
                            if target_ids:
                                filtered_boxes = []
                                filtered_centres = []
                                for tid, raw_box in zip(
                                        weed_detector.last_track_ids,
                                        weed_detector.last_raw_boxes):
                                    if smoothed.get(tid, -1) in target_ids:
                                        x, y, w, h = raw_box
                                        filtered_centres.append([x + w // 2, y + h // 2])
                                        filtered_boxes.append(raw_box)
                                boxes = filtered_boxes
                                weed_centres = filtered_centres

                    elif algorithm == 'gog-hybrid':
                        cnts, boxes, weed_centres, image_out = weed_detector.inference(
                            cropped_frame,
                            confidence=self._gog_confidence,
                            show_display=return_image_out,
                            exg_min=self.exg_min, exg_max=self.exg_max,
                            hue_min=self.hue_min, hue_max=self.hue_max,
                            saturation_min=self.saturation_min, saturation_max=self.saturation_max,
                            brightness_min=self.brightness_min, brightness_max=self.brightness_max,
                            min_detection_area=min_detection_area, invert_hue=self.invert_hue
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
                            # boxes are drawn after the relays are scheduled
                            show_display=False,
                            algorithm=algorithm,
                            min_detection_area=min_detection_area,
                            invert_hue=self.invert_hue,
                            label='WEED'
                        )

                    # Merge Kalman-predicted lost tracks into detection output
                    # Only for pure gog mode — in hybrid, lost_stracks are crops not weeds
                    if (self.tracking_enabled
                            and self.detection_persist_frames > 0
                            and algorithm == 'gog'
                            and hasattr(weed_detector, 'get_lost_tracks')):
                        lost = weed_detector.get_lost_tracks(
                            max_age=self.detection_persist_frames)
                        target_ids = set(weed_detector._detect_class_ids or [])
                        persisted_boxes = []
                        for lt in lost:
                            smoothed_cls = (self._class_smoother.get_class(lt['track_id'])
                                            if self._class_smoother else lt['cls'])
                            if target_ids and smoothed_cls not in target_ids:
                                continue
                            x1, y1, x2, y2 = [int(v) for v in lt['xyxy']]
                            w, h = x2 - x1, y2 - y1
                            boxes.append([x1, y1, w, h])
                            weed_centres.append([int((x1 + x2) / 2), int((y1 + y2) / 2)])
                            cls_name = weed_detector.model.names.get(lt['cls'], 'unknown')
                            persisted_boxes.append({
                                'x': x1, 'y': y1, 'w': w, 'h': h,
                                'track_id': lt['track_id'], 'age': lt['age'],
                                'conf': lt['score'], 'cls_name': cls_name,
                            })

                    # Capture-to-relay time, measured on every detection frame.
                    # self.delay is the camera-to-nozzle travel time counted
                    # from capture; processing has already used part of it, so
                    # the relays get only what is left (a nozzle 30 cm behind
                    # the camera at 8 km/h has 135 ms; after 85 ms of capture
                    # and inference, 50 ms remain). When processing took longer
                    # than the travel time the nozzle opens at once and the
                    # shortfall is recorded for the speed warning. Duration is
                    # never shortened: see PROJECT_NOTES 2026-09-13 (2).
                    actuation_time = time.monotonic()
                    lag_s = actuation_time - capture_time
                    delay_used = max(0.0, self.delay - lag_s)
                    self._latency_s.append(lag_s)
                    self._late_s.append(max(0.0, lag_s - self.delay))

                    # Zone-based actuation (segmentation models with gog/gog-hybrid)
                    if (algorithm.startswith('gog')
                            and actuation_mode == 'zone'
                            and hasattr(weed_detector, 'detection_mask')
                            and weed_detector.detection_mask is not None):
                        for i in range(self.relay_num):
                            lane_start = self.lane_coords_int[i]
                            lane_end = int(lane_start + self.lane_width)
                            lane_pixels = np.count_nonzero(
                                weed_detector.detection_mask[self.actuation_y_top:self.actuation_y_bottom,
                                                             lane_start:lane_end]
                            )
                            if lane_pixels >= min_detection_pixels:
                                self.relay_controller.receive(
                                    relay=i,
                                    delay=delay_used,
                                    time_stamp=actuation_time,
                                    duration=self.actuation_duration)
                    else:
                        # Centre-based actuation (default, works for all model types)
                        # One timestamp per frame, deduplicated relay calls (at most relay_num)
                        if weed_centres:
                            fired = assign_lanes(weed_centres, self.lane_width,
                                                 self.relay_num,
                                                 self.actuation_y_top,
                                                 self.actuation_y_bottom)
                            for relay_id in fired:
                                self.relay_controller.receive(
                                    relay=relay_id,
                                    delay=delay_used,
                                    time_stamp=actuation_time,
                                    duration=self.actuation_duration)

                    # Side work only once the relays are scheduled, so it can
                    # never delay a spray: weed indicators, then annotation.
                    if len(weed_centres) > 0:
                        if self.dash:
                            self.dash.weed_detect_indicator()
                        if self.controller:
                            self.controller.weed_detect_indicator()

                    if return_image_out:
                        if algorithm not in ('gog', 'gog-hybrid'):
                            image_out = draw_boxes(cropped_frame, boxes, label='WEED')
                        # Kalman-persisted tracks, dimmed
                        if image_out is not None:
                            for pb in persisted_boxes:
                                cv2.rectangle(image_out,
                                              (pb['x'], pb['y']),
                                              (pb['x'] + pb['w'], pb['y'] + pb['h']),
                                              (0, 120, 0), 2)
                                lbl = (f"ID{pb['track_id']} [{pb['age']}] "
                                       f"{int(pb['conf'] * 100)}% {pb['cls_name']}")
                                cv2.putText(image_out, lbl,
                                            (pb['x'], pb['y'] - 10),
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                                            (0, 120, 0), 1)

                ##### Update Dashboard Stream #####
                # (system stats are gathered in update_state, off this loop)
                if self.dash and frame_count % STREAM_FRAME_INTERVAL == 0:  # reduce streamer overhead
                    try:
                        if self._stream_full_frame:
                            # Geometry editor wants the full, uncropped frame so its
                            # overlay maps to the whole image. No band lines (the
                            # client draws its own).
                            self.set_latest_stream_frame(frame)
                        else:
                            # Always show the CROPPED frame (what the detector sees):
                            # the annotated detection output when detecting, else the
                            # raw frame cropped for display (cheap view).
                            if self._detection_enable and image_out is not None:
                                final_frame_to_stream = image_out
                            else:
                                # .copy() → contiguous (cv2.line needs it) and avoids
                                # drawing band lines onto the live capture buffer.
                                final_frame_to_stream = frame[self.crop_slice].copy()

                            # Draw the actuation band edges (cropped-frame coords).
                            frame_w = final_frame_to_stream.shape[1]
                            if self.actuation_y_top > 0:
                                cv2.line(final_frame_to_stream,
                                         (0, self.actuation_y_top), (frame_w, self.actuation_y_top),
                                         (0, 255, 255), 1)
                            if self.actuation_y_bottom < final_frame_to_stream.shape[0]:
                                cv2.line(final_frame_to_stream,
                                         (0, self.actuation_y_bottom), (frame_w, self.actuation_y_bottom),
                                         (0, 255, 255), 1)

                            self.set_latest_stream_frame(final_frame_to_stream)
                    except Exception as e:
                        self.logger.error(f"Error sending frame to dashboard: {e}")

                ##### IMAGE SAMPLER #####
                # record sample images if required of weeds detected. sampleFreq specifies how often
                if self._image_sample_enable and self._recording_paused:
                    # Paused at the headland: the session stays open and
                    # nothing is saved. A clip ends here; resuming starts
                    # the next one in the same session.
                    if self._video_writer_clip is not None:
                        self._close_video_clip()
                        self._video_started_at = None
                elif self._image_sample_enable:
                    # Video mode records every frame into a clip instead.
                    # Never while detecting: encoding every frame in the loop
                    # would cost spray latency, so save stills instead.
                    record_video = self.record_mode == 'video'
                    if record_video and self._detection_enable:
                        record_video = False
                        if self._video_writer_clip is not None or not self._video_fallback_logged:
                            self.logger.warning("Video recording is not available while detecting "
                                                "- saving still images instead")
                            self._video_fallback_logged = True
                            self._close_video_clip()
                    if record_video:
                        self._write_video_frame(frame)
                    # Which frames to save: every Nth frame, every X metres
                    # of GPS travel, or every X seconds (CaptureScheduler).
                    elif self.capture_scheduler.should_capture(
                            frame_count, speed_kmh=self._fresh_speed_kmh(),
                            frequency=self.sample_frequency):
                        save_boxes = None
                        save_centres = None
                        # detection_ran, not _detection_enable: boxes/weed_centres
                        # are only bound when a detector actually ran. With
                        # detection on but the detector failed to build, the
                        # enable flag is True and both names are unbound, and the
                        # NameError escapes the guarded block below and stops the OWL.
                        if self.sample_method != 'whole' and detection_ran:
                            # Detections are in cropped-frame pixels; the full
                            # frame is saved, so shift them by the crop origin
                            # (crops and their EXIF ROI were offset before).
                            dy = self.crop_slice[0].start or 0
                            dx = self.crop_slice[1].start or 0
                            save_boxes = [(x + dx, y + dy, w, h) for x, y, w, h in boxes]
                            save_centres = [(cx + dx, cy + dy) for cx, cy in weed_centres]

                        # Image sampling is auxiliary — a metadata/EXIF/disk error here
                        # must never reach the loop's except (which stops the OWL).
                        try:
                            context = {
                                'device_id': self.dash.device_id if self.dash else None,
                                'algorithm': algorithm,
                                'model': Path(self._model_path).name if algorithm.startswith('gog') else None,
                                'owl_version': str(VERSION),
                                'camera_model': getattr(self.cam, 'camera_model', None),
                                'session_id': getattr(self, '_session_id', None),
                                'session_start': getattr(self, '_session_start', None),
                                # Without this a clean miss and an empty paddock
                                # look identical, which breaks any recall measure
                                # taken from a recorded set.
                                'detections': len(weed_centres) if detection_ran else None,
                            }
                            context.update(self._detection_settings(algorithm))
                            if self.dash:
                                context.update(self.dash.get_session_metadata())

                            self.image_recorder.add_frame(frame=frame,
                                                          frame_id=frame_count,
                                                          boxes=save_boxes,
                                                          centres=save_centres,
                                                          gps_data=self.gps_data,
                                                          camera_metadata=camera_metadata,
                                                          context=context)

                            if self.controller:
                                self.status_indicator.image_write_indicator()
                            if self.dash:
                                self.dash.image_write_indicator()
                                self.dash.publish_capture(self.image_recorder)
                        except Exception as e:
                            self.logger.error(f"Image sampling error (detection continues): {e}",
                                              exc_info=True)

                    # Checked every recording frame, stills or video clip
                    if self.status_indicator.DRIVE_FULL:
                        # Clears the shared switch value too: with a dashboard
                        # it used to clear only the dashboard state, and a
                        # held record switch turned recording straight back on.
                        self._stop_recording('storage_full')
                        if self.dash:
                            self.dash.set_recording_blocked('storage_full')
                            self.dash.drive_full_indicator()

                        self._close_video_clip()
                        self.image_recorder.stop(wait=False)
                        self.status_indicator.error(5)
                        self.logger.info("Drive full: image sampling disabled until space is freed")

                frame_count += 1

                # Loop time tracking — detection frames only, so the
                # published average is real detection throughput
                if detection_ran:
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
                    # image_out is None when detection is on but no detector
                    # was built (e.g. gog with a missing model)
                    if not self._detection_enable or image_out is None:
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

                # Key handling only makes sense with a window open. waitKey()
                # raises on headless OpenCV builds, so never call it otherwise.
                k = cv2.waitKey(1) & 0xFF if self.show_display else 255
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
            self._close_video_clip()

            # Flush the session GPS track
            if hasattr(self, 'track_recorder') and self.track_recorder.recording:
                safe_stop(self.track_recorder, 'track recorder', fallback_to_terminate=False)

            # Stop status indicator
            if hasattr(self, 'status_indicator') and self.status_indicator:
                safe_stop(self.status_indicator, 'status indicator', fallback_to_terminate=False)

            # Stop serial GPS
            self._gps_running = False
            if self._gps_serial and self._gps_serial.is_open:
                try:
                    self._gps_serial.close()
                except Exception as e:
                    self.logger.warning(f"Error closing GPS serial port: {e}")
            if self.gps_status_led:
                safe_stop(self.gps_status_led, 'GPS status LED', fallback_to_terminate=False)

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
        atomic_write_config(new_config_path, self.config.write)

        self.logger.info(f"[INFO] Configuration saved to {new_config_path}")

    def setup_media_source(self, input_file_or_directory, camera_type='auto'):
        """
        Configure and initialize the appropriate media source (camera or media file/directory).

        Args:
            input_file_or_directory: Optional path from CLI args to image/video source
            camera_type: Camera selection ('auto', 'rpi', 'usb')

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
                                       exp_compensation=self.exp_compensation,
                                       camera_type=camera_type,
                                       awb_mode=self.awb_mode,
                                       awb_red_gain=self.awb_red_gain,
                                       awb_blue_gain=self.awb_blue_gain,
                                       rotation=self.camera_rotation)
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

    # ------------------------------------------------------------------
    # Data collection: capture spacing, video clips, shutter cap
    # ------------------------------------------------------------------

    def _load_capture_settings(self):
        """[DataCollection] capture keys + [Camera] max_gain. Missing or
        blank keys keep the old behaviour: every Nth frame, JPEG 95, stills,
        no shutter cap."""
        cfg = self.config

        def get(section, key, fallback):
            value = cfg.get(section, key, fallback='').strip() if cfg.has_section(section) else ''
            return value or fallback

        self.sample_mode = get('DataCollection', 'sample_mode', 'frames').lower()
        self.jpeg_quality = min(100, max(50, int(float(get('DataCollection', 'jpeg_quality', 95)))))
        self.record_mode = 'video' if get('DataCollection', 'record_mode', 'images').lower() == 'video' else 'images'
        self.video_max_s = min(60, max(5, int(float(get('DataCollection', 'video_max_s', 60)))))
        self.capture_scheduler = CaptureScheduler(
            mode=self.sample_mode,
            frequency=int(float(get('DataCollection', 'sample_frequency', 30))),
            distance_m=float(get('DataCollection', 'sample_distance_m', 1.0)),
            interval_s=float(get('DataCollection', 'sample_interval_s', 2.0)),
            min_speed_kmh=float(get('DataCollection', 'min_speed_kmh', 1.0)))
        try:
            max_gain = parse_max_gain(get('Camera', 'max_gain', 'auto'))
        except ValueError:
            # A bad hand edit must not crash-loop owl.service
            self.logger.warning(f"[Camera] max_gain {get('Camera', 'max_gain', '')!r} "
                                f"is not auto or a number - using auto")
            max_gain = None
        self.exposure_governor = ExposureGovernor(
            smear_limit_mm=float(get('DataCollection', 'smear_limit_mm', 2.0)),
            cap_enabled=get('DataCollection', 'shutter_cap', 'False').lower() in ('true', '1', 'yes'),
            max_gain=max_gain)
        self._video_writer_clip = None
        self._video_started_at = None
        self._video_clip_path = None
        self._video_last_t = None
        self._video_dts = []
        self._video_preview_at = 0.0
        self._recording_stopped_reason = None
        self._video_fallback_logged = False

    def apply_capture_params(self, params):
        """Live update from the controller (MQTT set_capture_params). Returns
        the dict of values actually applied. Not persisted: the controller
        keeps them and re-sends on every connect and Record."""
        applied = {}
        if 'sample_mode' in params:
            self.sample_mode = str(params['sample_mode']).lower()
            self.capture_scheduler.configure(mode=self.sample_mode)
            applied['sample_mode'] = self.capture_scheduler.mode
        if 'sample_distance_m' in params:
            self.capture_scheduler.configure(distance_m=params['sample_distance_m'])
            applied['sample_distance_m'] = self.capture_scheduler.distance_m
        if 'sample_interval_s' in params:
            self.capture_scheduler.configure(interval_s=params['sample_interval_s'])
            applied['sample_interval_s'] = self.capture_scheduler.interval_s
        if 'min_speed_kmh' in params:
            self.capture_scheduler.configure(min_speed_kmh=params['min_speed_kmh'])
            applied['min_speed_kmh'] = self.capture_scheduler.min_speed_kmh
        if 'jpeg_quality' in params:
            self.jpeg_quality = min(100, max(50, int(float(params['jpeg_quality']))))
            if getattr(self, 'image_recorder', None):
                self.image_recorder.jpeg_quality = self.jpeg_quality
            applied['jpeg_quality'] = self.jpeg_quality
        if 'record_mode' in params:
            self.record_mode = 'video' if str(params['record_mode']).lower() == 'video' else 'images'
            applied['record_mode'] = self.record_mode
        if 'video_max_s' in params:
            self.video_max_s = min(60, max(5, int(float(params['video_max_s']))))
            applied['video_max_s'] = self.video_max_s
        if 'smear_limit_mm' in params:
            self.exposure_governor.configure(smear_limit_mm=params['smear_limit_mm'])
            applied['smear_limit_mm'] = self.exposure_governor.smear_limit_mm
        if 'shutter_cap' in params:
            enabled = str(params['shutter_cap']).lower() in ('true', '1', 'yes')
            self.exposure_governor.configure(cap_enabled=enabled)
            applied['shutter_cap'] = enabled
        if 'max_gain' in params:
            max_gain = parse_max_gain(params['max_gain'])
            if max_gain is None:
                self.exposure_governor.configure(clear_max_gain=True)
                applied['max_gain'] = 'auto'
            else:
                self.exposure_governor.configure(max_gain=max_gain)
                # '8', not '8.0': matches the Config tab's select options
                applied['max_gain'] = f"{self.exposure_governor.max_gain:g}"
        return applied

    def _fresh_speed_kmh(self):
        """GPS speed when the fix is fresh (gps_data is None once stale)."""
        gps = self.gps_data
        if not gps or gps.get('speed_kmh') is None:
            return None
        try:
            return float(gps['speed_kmh'])
        except (TypeError, ValueError):
            return None

    def _govern_exposure(self, camera_metadata):
        governor = self.exposure_governor
        if not (governor.cap_enabled or governor.max_gain or governor.active):
            return
        # The shutter cap is for collecting only (recording, not detecting);
        # the gain limit is a general camera setting and always applies.
        collecting = self._image_sample_enable and not self._detection_enable
        override = governor.update(camera_metadata, self._fresh_speed_kmh(),
                                   cap_allowed=collecting)
        if override is not None and hasattr(self.cam, 'set_exposure_override'):
            self.cam.set_exposure_override(**override)

    def _write_video_frame(self, frame):
        """Video mode: append the frame to the session's clip; stop the
        recording when the clip reaches video_max_s."""
        recorder = getattr(self, 'image_recorder', None)
        if recorder is None:
            return
        now = time.monotonic()
        try:
            if self._video_writer_clip is None:
                # Measure the real frame rate over ~10 frames first: the clip
                # plays back at the wrong speed if the header rate is guessed.
                if self._video_last_t is not None:
                    self._video_dts.append(now - self._video_last_t)
                self._video_last_t = now
                if len(self._video_dts) < 10:
                    return
                dts = sorted(self._video_dts)
                fps = min(60.0, max(1.0, 1.0 / max(dts[len(dts) // 2], 1e-3)))
                name = f"clip_{datetime.now().strftime('%H%M%S')}.mp4"
                self._video_clip_path = os.path.join(recorder.save_directory, name)
                height, width = frame.shape[:2]
                self._video_writer_clip = cv2.VideoWriter(
                    self._video_clip_path, cv2.VideoWriter_fourcc(*'mp4v'), fps, (width, height))
                if not self._video_writer_clip.isOpened():
                    # write() on an unopened writer silently does nothing
                    self.logger.error(f"Could not open video clip {self._video_clip_path} "
                                      f"(mp4v encoder unavailable?) - recording stopped")
                    self._close_video_clip()
                    self._stop_recording('video_error')
                    return
                self._video_started_at = now
                self.logger.info(f"Video clip started: {self._video_clip_path} at {fps:.1f} fps")

            self._video_writer_clip.write(frame)
            if now - self._video_preview_at >= 2.0:
                self._video_preview_at = now
                recorder.note_preview(frame, self.gps_data)
                if self.dash:
                    self.dash.publish_capture(recorder)

            if now - self._video_started_at >= self.video_max_s:
                self.logger.info(f"Video clip reached {self.video_max_s} s - recording stopped")
                self._close_video_clip()
                self._stop_recording('video_limit')
        except Exception as e:
            self.logger.error(f"Video recording error: {e}", exc_info=True)
            self._close_video_clip()
            self._stop_recording('video_error')

    def _close_video_clip(self):
        writer = getattr(self, '_video_writer_clip', None)
        if writer is not None:
            try:
                writer.release()
            except Exception as e:
                self.logger.warning(f"Could not close video clip: {e}")
        self._video_writer_clip = None
        self._video_last_t = None
        self._video_dts = []

    # Status LED blink count per spraying fault
    _FAULT_LED = {'camera_lost': 3, 'control_lost': 4, 'relay_stuck': 7}

    def _fault(self, code, message, detection_possible):
        """A spraying fault. Always loud (log, LED, dashboard/app); then
        [System] on_error decides the nozzles: 'off' -> detection off, all
        nozzles off; 'on' -> blanket spray if detection can no longer run,
        otherwise keep spot spraying."""
        if self.on_error == 'on':
            action = 'spot_spray' if detection_possible else 'blanket'
        else:
            action = 'nozzles_off'
        self._fault_state = {'code': code, 'message': message,
                             'action': action, 'since': time.time()}
        self.logger.error(f"[FAULT] {code}: {message} "
                          f"(on_error = {self.on_error}: {action.replace('_', ' ')})")
        if action == 'nozzles_off':
            self._detection_enable = False
            self.relay_controller.relay.all_off()
        elif action == 'blanket' and self._detection_enable:
            # Blanket follows the operator's detection switch, so spraying
            # can always be turned off (see the camera-lost wait in hoot).
            self.relay_controller.relay.all_on()
        indicator = getattr(self, 'status_indicator', None)
        if indicator:
            indicator.error(self._FAULT_LED.get(code, 1))
        dash = getattr(self, 'dash', None)
        if dash:
            dash.set_fault(self._fault_state)

    def _relay_stuck(self, message):
        # Called from the relay thread on every failed retry: raise it once.
        if (self._fault_state or {}).get('code') != 'relay_stuck':
            self._fault('relay_stuck', message, detection_possible=True)

    def _clear_fault(self, code):
        """The fault condition has gone: release the nozzles and say so."""
        fault = self._fault_state
        if not fault or fault['code'] != code:
            return
        self._fault_state = None
        self.logger.info(f"[FAULT] {code} cleared")
        if fault['action'] == 'blanket':
            self.relay_controller.relay.all_off()
        indicator = getattr(self, 'status_indicator', None)
        if indicator and indicator.error_code == self._FAULT_LED.get(code, 1):
            indicator.clear_error()
        dash = getattr(self, 'dash', None)
        if dash:
            dash.set_fault(None)

    def _stop_recording(self, reason):
        """Turn recording off from inside the loop and say why."""
        self._recording_stopped_reason = reason
        if self.dash:
            self.dash.set_image_sample_enable(False)
        if hasattr(self, 'image_sample_enable'):
            with self.image_sample_enable.get_lock():
                self.image_sample_enable.value = False
        self._image_sample_enable = False

    def capture_state(self):
        """What the controller's collect screen shows: capture settings,
        last saved image, quality summary, shutter cap. JSON-safe."""
        recorder = getattr(self, 'image_recorder', None)
        governor = self.exposure_governor
        meta = self._last_camera_metadata or {}
        speed = self._fresh_speed_kmh()
        exposure = meta.get('ExposureTime')
        video_s = (round(time.monotonic() - self._video_started_at, 1)
                   if self._video_writer_clip is not None and self._video_started_at else None)
        return {
            'sample_mode': self.capture_scheduler.mode,
            'sample_distance_m': self.capture_scheduler.distance_m,
            'sample_interval_s': self.capture_scheduler.interval_s,
            'min_speed_kmh': self.capture_scheduler.min_speed_kmh,
            # Recording stills but parked or creeping: the screens say so,
            # or a driver at a headland thinks recording has broken
            'paused_slow': bool(self._image_sample_enable and self.record_mode != 'video'
                            and not self._recording_paused
                            and self.capture_scheduler.too_slow(speed)),
            'paused': bool(self._image_sample_enable and self._recording_paused),
            'jpeg_quality': self.jpeg_quality,
            'record_mode': self.record_mode,
            'video_max_s': self.video_max_s,
            'video_seconds': video_s,
            'session_id': getattr(self, '_session_id', None),
            'saved_count': recorder.saved_count if recorder else 0,
            'last_saved': recorder.last_saved if recorder else None,
            'qc': recorder.qc.summary() if recorder else None,
            'exposure_time_us': exposure,
            'analogue_gain': round(meta['AnalogueGain'], 2) if meta.get('AnalogueGain') else None,
            'speed_kmh': speed,
            'ground_smear_mm': ground_smear_mm(speed, exposure),
            'shutter_cap': governor.cap_enabled,
            'smear_limit_mm': governor.smear_limit_mm,
            'max_gain': governor.max_gain,
            'shutter_capped': governor.exposure_us is not None,
            'gain_limited': governor.gain is not None,
            'stopped_reason': self._recording_stopped_reason,
        }

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
            if self.RPI_VERSION == 'rpi-5':
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
            # Non-blocking: CPU use since the previous call (primed in __init__)
            cpu_percent = psutil.cpu_percent(interval=None)
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
        stats['actuation_source'] = self.actuation_source
        stats['speed_kmh'] = self.speed_kmh
        stats['latency_ms'] = (round(sum(self._latency_s) / len(self._latency_s) * 1000, 1)
                               if self._latency_s else None)
        stats['speed_warning'] = self.speed_warning

        return stats

    # Consecutive failed sync cycles (0.1 s each) before control counts as lost
    _STATE_FAILURES_BEFORE_FAULT = 20

    def update_state(self):
        """Run _update_state_once forever. An exception used to kill this
        thread silently, leaving the dashboard unable to turn spraying off and
        GPS frozen; now it is logged, and persistent failure raises a fault."""
        failures = 0
        while not self.stop_state_update.is_set():
            try:
                self._update_state_once()
                if failures >= self._STATE_FAILURES_BEFORE_FAULT:
                    self.logger.info("State sync recovered")
                    self._clear_fault('control_lost')
                failures = 0
            except Exception as e:
                failures += 1
                if failures == 1:
                    self.logger.error(f"State sync error: {e}", exc_info=True)
                if failures == self._STATE_FAILURES_BEFORE_FAULT:
                    self._fault('control_lost',
                                f'Dashboard and switch control stopped updating: {e}',
                                detection_possible=True)
            time.sleep(self._STATE_CHECK_INTERVAL)

    def _update_state_once(self):
        """Update local state from MQTT server or local hardware controllers.

        Ute controller has ONE switch that controls either recording or detection
        (based on switch_purpose). The other control comes from the dashboard.
        Advanced controller manages recording, detection mode, and sensitivity.
        """
        if self.dash:
            if self.controller_type == 'ute':
                # Ute has one switch — only override the control it manages.
                # The other control + sensitivity come from MQTT (dashboard).
                if self.switch_purpose == 'recording':
                    # Hardware controls recording; detection from dashboard
                    with self.image_sample_enable.get_lock():
                        hardware_recording = self.image_sample_enable.value
                    self.dash.set_image_sample_enable(hardware_recording)
                    self._image_sample_enable = hardware_recording
                    self._detection_enable = self.dash.get_detection_enable()
                else:
                    # Hardware controls detection; recording from dashboard
                    with self.detection_enable.get_lock():
                        hardware_detection = self.detection_enable.value
                    self.dash.set_detection_enable(hardware_detection)
                    self._detection_enable = hardware_detection
                    self._image_sample_enable = self.dash.get_image_sample_enable()
                # Sensitivity always from dashboard for Ute
                self._sensitivity_level = self.dash.get_sensitivity_level()

            elif self.controller_type == 'advanced':
                # Advanced controller manages all three
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

            else:
                # No hardware controller - normal dashboard control
                self._detection_enable = self.dash.get_detection_enable()
                self._image_sample_enable = self.dash.get_image_sample_enable()
                self._sensitivity_level = self.dash.get_sensitivity_level()

        else:
            # Hardware only, no dashboard
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

        self._recording_paused = bool(self.dash and self._image_sample_enable
                                      and self.dash.get_recording_paused())

        # First-boot setup mode: detection stays off no matter what the
        # hardware switch or dashboard said above — a flipped-on Ute
        # switch must not click relays on the bench. Runs only when the
        # flag existed at boot; re-stats it at most every 5 s to notice
        # setup finishing (teardown removes the flag).
        if self._firstboot_active:
            now = time.time()
            if now - self._firstboot_last_check >= 5.0:
                self._firstboot_last_check = now
                if not self._firstboot_flag_path.exists():
                    self._firstboot_active = False
                    self.logger.info("First-boot flag cleared: detection control restored")
            if self._firstboot_active:
                self._detection_enable = False

        # on_error = off: a spraying fault holds detection off whatever the
        # switch or dashboard says, until it clears.
        if self._fault_state and self._fault_state['action'] == 'nozzles_off':
            self._detection_enable = False

        # GPS: serial takes priority, then dashboard (browser geolocation)
        self.gps_data = self._get_best_gps_data()

        # Best-GPS reading → dashboard/app state (change-guarded in the
        # setter; coords quantized there so a parked rig stays quiet)
        if self.dash:
            gps = self.gps_data
            self.dash.set_gps_status(
                fix=gps is not None,
                source=gps.get('source') if gps else None,
                latitude=gps.get('latitude') if gps else None,
                longitude=gps.get('longitude') if gps else None,
                accuracy=gps.get('accuracy') if gps else None)

        # Session GPS track: recorder throttles internally (1 s / 0.5 m)
        if (self.track_recorder.recording and self._image_sample_enable
                and not self._recording_paused and self.gps_data):
            self.track_recorder.add_point(
                lat=self.gps_data.get('latitude'),
                lon=self.gps_data.get('longitude'),
                speed=self.gps_data.get('speed_kmh'),
                heading=self.gps_data.get('heading'),
                altitude=self.gps_data.get('altitude'))

        # Spray timing once a second; system stats every 3 s. Stats used to
        # be gathered inside the detection loop, where psutil's sampling
        # interval and the vcgencmd call froze detection for ~130 ms.
        now = time.monotonic()
        if now - self._last_timing_update >= 1.0:
            self._last_timing_update = now
            self._update_spray_timing()
        if self.dash and now - self._last_stats_at >= 3.0:
            self._last_stats_at = now
            try:
                self.dash.update_system_stats(self.get_system_stats())
            except Exception as e:
                self.logger.debug(f"Error updating system stats: {e}")

        # Storage watchdog readings → dashboard/app (change-guarded in
        # the setter, so this is cheap every cycle)
        if self.dash and hasattr(self, 'status_indicator'):
            indicator = self.status_indicator
            free_mb = (int(indicator.storage_free / (1024 * 1024))
                       if indicator.storage_free is not None else None)
            self.dash.set_storage_status(
                location=indicator.storage_location,
                free_mb=free_mb,
                warning=indicator.storage_warning,
                quota_gb=getattr(self, 'image_quota_gb', 12))

    # Controller timing counts as live for this long after its last broadcast
    # (it broadcasts once a second).
    _CONTROLLER_TIMING_TIMEOUT_S = 3.0

    def _update_spray_timing(self):
        """Once a second: speed-adaptive timing from this OWL's own GPS fix,
        handover when that fix is lost, and the farmer-facing speed warning."""
        fix = self._gps_state.get_dict() if self._gps_state is not None else None
        speed = fix.get('speed_kmh') if fix and fix.get('fix_valid') else None

        if speed is not None:
            if not self._device_timing:
                if self.actuation_source == 'config':
                    # Remember the configured timing to restore when the fix goes
                    self._config_actuation = (self.actuation_duration, self.delay)
                self.logger.info("Spray timing now from this OWL's GPS")
            average = self._speed_averager.add_sample(speed)
            result = self._actuation_calc.compute(average, self._avg_loop_time_ms)
            self.actuation_duration, self.delay = ConfigValidator.clamp_actuation(
                result['actuation_duration'], result['delay'])
            self._device_timing = True
            self.actuation_source = 'device-gps'
            self.speed_kmh = round(average, 1)
        else:
            if self._device_timing:
                self.logger.warning("GPS fix lost: spray timing handed to the controller, "
                                    "or the configured values when there is none")
            self._device_timing = False
            if time.monotonic() - self._controller_timing_at <= self._CONTROLLER_TIMING_TIMEOUT_S:
                self.actuation_source = 'controller'
                self.speed_kmh = self._controller_speed_kmh
            else:
                if self.actuation_source != 'config':
                    self.actuation_duration, self.delay = self._config_actuation
                    self.actuation_source = 'config'
                self.speed_kmh = None

        lag_s = sum(self._latency_s) / len(self._latency_s) if self._latency_s else 0.0
        late_s = sum(self._late_s) / len(self._late_s) if self._late_s else 0.0
        self.speed_warning = speed_warning(
            self.speed_kmh, self._actuation_calc.actuation_length_cm,
            self._actuation_calc.offset_cm, self._avg_loop_time_ms, lag_s, late_s)

    @staticmethod
    def _frame_capture_time(camera_metadata):
        """Monotonic time the frame was exposed.

        picamera2's SensorTimestamp (ns, start of exposure) is used when it reads
        as a plausible age on the monotonic clock (under a second old). Webcams,
        replayed files and anything implausible use the hand-over time, which
        misses the sensor and ISP delay (about one frame).
        """
        now = time.monotonic()
        sensor_ns = camera_metadata.get('SensorTimestamp') if camera_metadata else None
        if sensor_ns:
            captured = sensor_ns / 1e9
            if 0.0 <= now - captured < 1.0:
                return captured
        return now

    def _allowed_board_pin(self, key, fallback):
        """[Controller] pin as 'BOARDn', or None when it is unset ('none') or
        claimed by this unit's own GPS hardware (logged loudly)."""
        raw = self.config.get('Controller', key, fallback=str(fallback)).strip("'\" ").lower()
        if raw in ('', 'none'):
            return None
        pin = int(raw)
        if pin in self.protected_pins:
            self.logger.error(f"[Controller] {key} on BOARD {pin} disabled: "
                              f"{self.protected_pins[pin]}. Move it to a free pin.")
            return None
        return f"BOARD{pin}"

    # Dashboard GPS is stale if no update received for this many seconds.
    # Browser watchPosition fires every 1-5s when active; 3s gives fast LED feedback
    # when the farmer closes the browser or switches apps.
    _GPS_STALE_THRESHOLD = 3

    def _get_best_gps_data(self):
        """Return GPS data from the best available source. Serial > Dashboard (browser).

        Returns None if no source has fresh data, preventing stale coordinates
        from being written into image EXIF.
        """
        # Try serial GPS first
        if self._gps_state is not None:
            snapshot = self._gps_state.get_dict()
            if snapshot.get('fix_valid') and snapshot.get('latitude') is not None:
                if self.gps_status_led:
                    self.gps_status_led.set_state(GPSLEDState.FIX)
                return {
                    'latitude': snapshot['latitude'],
                    'longitude': snapshot['longitude'],
                    # Metres from receivers that report it (nRF91), else HDOP
                    'accuracy': (snapshot['accuracy_m'] if snapshot.get('accuracy_m') is not None
                                 else snapshot.get('hdop')),
                    'hdop': snapshot.get('hdop'),
                    'altitude': snapshot.get('altitude'),
                    'speed_kmh': snapshot.get('speed_kmh'),
                    'heading': snapshot.get('heading'),
                    'satellites': snapshot.get('satellites'),
                    'utc_time': snapshot.get('utc_time'),
                    'utc_date': snapshot.get('utc_date'),
                    'timestamp': time.time(),
                    'source': 'serial'
                }

        # Fallback to dashboard GPS (browser geolocation or controller broadcast via MQTT)
        if self.dash:
            gps = self.dash.get_gps_data()
            if gps is not None:
                # Staleness uses LOCAL receipt time — the sender's clock
                # (phone/controller) may be skewed against this Pi's clock.
                age = time.time() - gps.get('received_at', gps.get('timestamp', 0))
                if age <= self._GPS_STALE_THRESHOLD:
                    if self.gps_status_led:
                        self.gps_status_led.set_state(GPSLEDState.FIX)
                    # Copy — the stored payload must not grow keys.
                    return dict(gps, source='dashboard')
                else:
                    # Dashboard GPS is stale — phone disconnected or tab backgrounded
                    if self.gps_status_led:
                        self.gps_status_led.set_state(GPSLEDState.ERROR)
                    return None

        # No GPS source available
        if self.gps_status_led:
            self.gps_status_led.set_state(GPSLEDState.OFF)
        return None

    def _serial_gps_reader(self):
        """Background thread: read a serial GPS receiver, update self._gps_state.

        source = serial: the receiver streams NMEA on its own.
        source = nrf91: the Nordic nRF9151 on the production CM5 carrier only
        reports fixes after GPS is started with AT commands, and forgets that if
        it resets or loses power. GPS is started on every connect; after a quiet
        spell (normal while searching for satellites) the modem is pinged, and
        no answer or a fresh 'Ready' banner means reconnect and start again.
        """
        RECONNECT_DELAY = 5
        NRF91_PING_AFTER_S = 30
        nrf91 = self.gps_source == 'nrf91'
        failure_logged = False

        while self._gps_running:
            try:
                self._gps_serial = serial.Serial(
                    port=self.gps_port,
                    baudrate=self.gps_baudrate,
                    timeout=2
                )
                if nrf91 and nrf91_start_gps(self._gps_serial,
                                             on_line=self._gps_state.update_from_line) is None:
                    raise OSError(f"nRF91 modem did not start GPS on {self.gps_port} @ {self.gps_baudrate} "
                                  f"(boot config needs dtoverlay=uart3-pi5 and gpio=26=op,dh)")
                self.logger.info(f"GPS connected ({self.gps_source}): {self.gps_port}")
                failure_logged = False
                last_line_at = time.monotonic()

                while self._gps_running:
                    try:
                        raw_line = self._gps_serial.readline().decode('ascii', errors='replace').strip()
                    except (serial.SerialException, OSError) as e:
                        self.logger.warning(f"Serial GPS read error: {e}")
                        if self.gps_status_led:
                            self.gps_status_led.set_state(GPSLEDState.ERROR)
                        break

                    if not raw_line:
                        if nrf91 and time.monotonic() - last_line_at > NRF91_PING_AFTER_S:
                            if not nrf91_command(self._gps_serial, 'AT', timeout=3.0,
                                                 on_line=self._gps_state.update_from_line):
                                self.logger.warning("nRF91 modem stopped answering, restarting GPS")
                                break
                            last_line_at = time.monotonic()
                        continue

                    last_line_at = time.monotonic()
                    if nrf91 and raw_line == 'Ready':
                        self.logger.warning("nRF91 modem restarted, restarting GPS")
                        break

                    if self._gps_state.update_from_line(raw_line) is None:
                        continue

                    # Update GPS LED state
                    if self.gps_status_led:
                        snapshot = self._gps_state.get_dict()
                        if snapshot['fix_valid']:
                            self.gps_status_led.set_state(GPSLEDState.FIX)
                        else:
                            self.gps_status_led.set_state(GPSLEDState.ACQUIRING)

            except (OSError, FileNotFoundError) as e:
                # First failure at WARNING, repeats (every RECONNECT_DELAY) at
                # DEBUG so an absent receiver does not flood the journal.
                log = self.logger.debug if failure_logged else self.logger.warning
                log(f"Serial GPS connection failed: {e}")
                failure_logged = True
                if self.gps_status_led:
                    self.gps_status_led.set_state(GPSLEDState.ERROR)
            except Exception as e:
                if serial and isinstance(e, serial.SerialException):
                    self.logger.warning(f"Serial GPS connection failed: {e}")
                    if self.gps_status_led:
                        self.gps_status_led.set_state(GPSLEDState.ERROR)
                else:
                    self.logger.error(f"Unexpected error in serial GPS reader: {e}", exc_info=True)
                    if self.gps_status_led:
                        self.gps_status_led.set_state(GPSLEDState.ERROR)
            finally:
                if self._gps_serial and self._gps_serial.is_open:
                    self._gps_serial.close()

            if self._gps_running:
                time.sleep(RECONNECT_DELAY)

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
    DEFAULT_CONFIG = 'config/GENERAL_CONFIG.ini'
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

    # Ctrl-C was already handled inside hoot(), but SIGTERM was not: systemd
    # sends it on `systemctl stop/restart owl.service`, and Python's default
    # handler exits without running any cleanup, so the relays were left as
    # they were. Raising KeyboardInterrupt hands control to the existing
    # shutdown path, which turns every nozzle off.
    def _terminate(signum, frame):
        logger.info('[INFO] SIGTERM received - shutting down')
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)

    # start the targeting!
    owl.hoot()