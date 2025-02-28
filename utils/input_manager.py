import time
import platform
import configparser
import subprocess
import cv2
import logging

logger = logging.getLogger(__name__)

def is_raspberry_pi() -> bool:
    """Check if system is running on Raspberry Pi"""
    platform_str = platform.platform().lower()
    return 'rpi' in platform_str or 'aarch' in platform_str

# Determine if we're in testing mode and import GPIO if needed
testing = not is_raspberry_pi()
if not testing:
    from gpiozero import Button
else:
    platform_name = platform.system() if platform.system() == "Windows" else "unrecognized"
    logger.warning(
        f"The system is running on a {platform_name} platform. GPIO disabled. Test mode active.")

class UteController:
    def __init__(self, detection_state,
                 sample_state,
                 stop_flag,
                 owl_instance,
                 status_indicator,
                 switch_purpose='recording',
                 switch_board_pin='BOARD37',
                 bounce_time=1.0):

        self.switch = Button(switch_board_pin, bounce_time=bounce_time) if not testing else None
        self.switch_purpose = switch_purpose

        self.detection_state = detection_state
        self.sample_state = sample_state

        self.owl = owl_instance
        self.status_indicator = status_indicator
        self.status_indicator.start_storage_indicator()

        self.stop_flag = stop_flag

        # Set up a single handler for both press and release
        self.switch.when_pressed = self.toggle_state
        self.switch.when_released = self.toggle_state

        # Initialize state based on initial switch position
        self.update_state()

    def update_state(self):
        is_active = self.switch.is_pressed if self.switch else False

        if self.switch_purpose == 'detection':
            with self.detection_state.get_lock():
                self.detection_state.value = is_active
            if is_active:
                self.status_indicator.enable_weed_detection()
            else:
                self.status_indicator.disable_weed_detection()
        elif self.switch_purpose == 'recording':
            with self.sample_state.get_lock():
                self.sample_state.value = is_active
            if is_active:
                self.status_indicator.enable_image_recording()
            else:
                self.status_indicator.disable_image_recording()

    def toggle_state(self):
        self.update_state()

    def weed_detect_indicator(self):
        self.status_indicator.weed_detect_indicator()

    def image_write_indicator(self):
        self.status_indicator.image_write_indicator()

    def run(self):
        try:
            while not self.stop_flag.value:
                time.sleep(0.1)  # sleep to reduce CPU usage
        except KeyboardInterrupt:
            logger.info("[INFO] KeyboardInterrupt received in controller run loop. Exiting.")
            self.stop()  # Ensure the stop flag is set
        except Exception as e:
            logger.error(f"Error in controller run loop: {e}", exc_info=True)

    def stop(self):
        with self.stop_flag.get_lock():
            self.stop_flag.value = True


class AdvancedController:
    def __init__(self, recording_state,
                 sensitivity_state,
                 detection_mode_state,
                 stop_flag,
                 owl_instance,
                 status_indicator,
                 low_sensitivity_config,
                 high_sensitivity_config,
                 detection_mode_bpin_down='BOARD35',
                 detection_mode_bpin_up='BOARD36',
                 recording_bpin='BOARD38',
                 sensitivity_bpin='BOARD40',
                 bounce_time=1.0):

        self.recording_switch = Button(recording_bpin, bounce_time=bounce_time) if not testing else None
        self.sensitivity_switch = Button(sensitivity_bpin, bounce_time=bounce_time) if not testing else None
        self.detection_mode_switch_up = Button(detection_mode_bpin_up, bounce_time=bounce_time) if not testing else None
        self.detection_mode_switch_down = Button(detection_mode_bpin_down, bounce_time=bounce_time) if not testing else None

        self.recording_state = recording_state
        self.sensitivity_state = sensitivity_state
        self.detection_mode_state = detection_mode_state

        self.stop_flag = stop_flag

        # set up instances for owl and status
        self.owl = owl_instance
        self.status_indicator = status_indicator
        self.status_indicator.start_storage_indicator()

        self.low_sensitivity_settings = self._read_config(low_sensitivity_config)
        self.high_sensitivity_settings = self._read_config(high_sensitivity_config)

        if self.recording_switch:
            self.recording_switch.when_pressed = self.update_recording_state
            self.recording_switch.when_released = self.update_recording_state
        if self.sensitivity_switch:
            self.sensitivity_switch.when_pressed = self.update_sensitivity_state
            self.sensitivity_switch.when_released = self.update_sensitivity_state
        if self.detection_mode_switch_up:
            self.detection_mode_switch_up.when_pressed = lambda: self.set_detection_mode(2)
            self.detection_mode_switch_up.when_released = lambda: self.set_detection_mode(1)
        if self.detection_mode_switch_down:
            self.detection_mode_switch_down.when_pressed = lambda: self.set_detection_mode(0)
            self.detection_mode_switch_down.when_released = lambda: self.set_detection_mode(1)

        # Initialize states based on initial switch positions
        self.update_state()

    def update_state(self):
        try:
            self.update_recording_state()
            self.update_sensitivity_state()
            self.update_detection_mode_state()
        except KeyboardInterrupt:
            logger.info("[INFO] KeyboardInterrupt received in update_state. Exiting.")
            raise  # Propagate to hoot()
        except Exception as e:
            logger.error(f"Error in update_state: {e}", exc_info=True)

    def update_recording_state(self):
        self.status_indicator.generic_notification()
        is_pressed = self.recording_switch.is_pressed if self.recording_switch else False
        with self.recording_state.get_lock():
            self.recording_state.value = is_pressed
        if is_pressed:
            self.status_indicator.enable_image_recording()
        else:
            self.status_indicator.disable_image_recording()

    def update_sensitivity_state(self):
        with self.sensitivity_state.get_lock():
            self.sensitivity_state.value = self.sensitivity_switch.is_pressed
        self.update_sensitivity_settings()

    def update_sensitivity_settings(self):
        self.status_indicator.generic_notification()
        settings = self.low_sensitivity_settings if self.sensitivity_state.value else self.high_sensitivity_settings

        # Update Owl instance settings
        self.owl.exg_min = settings['exg_min']
        self.owl.exg_max = settings['exg_max']
        self.owl.hue_min = settings['hue_min']
        self.owl.hue_max = settings['hue_max']
        self.owl.saturation_min = settings['saturation_min']
        self.owl.saturation_max = settings['saturation_max']
        self.owl.brightness_min = settings['brightness_min']
        self.owl.brightness_max = settings['brightness_max']

        # Update trackbars if show_display is True
        if self.owl.show_display:
            cv2.setTrackbarPos("ExG-Min", self.owl.window_name, self.owl.exg_min)
            cv2.setTrackbarPos("ExG-Max", self.owl.window_name, self.owl.exg_max)
            cv2.setTrackbarPos("Hue-Min", self.owl.window_name, self.owl.hue_min)
            cv2.setTrackbarPos("Hue-Max", self.owl.window_name, self.owl.hue_max)
            cv2.setTrackbarPos("Sat-Min", self.owl.window_name, self.owl.saturation_min)
            cv2.setTrackbarPos("Sat-Max", self.owl.window_name, self.owl.saturation_max)
            cv2.setTrackbarPos("Bright-Min", self.owl.window_name, self.owl.brightness_min)
            cv2.setTrackbarPos("Bright-Max", self.owl.window_name, self.owl.brightness_max)

    def set_detection_mode(self, mode):
        try:
            with self.detection_mode_state.get_lock():
                self.detection_mode_state.value = mode
            self.status_indicator.generic_notification()
            with self.owl.detection_enable.get_lock():
                if mode == 0:  # Detection on
                    self.owl.detection_enable.value = True
                    self.status_indicator.enable_weed_detection()
                elif mode == 2:  # All solenoids on
                    self.owl.detection_enable.value = False
                    self.status_indicator.disable_weed_detection()
                    self.owl.relay_controller.relay.all_on()
                else:  # Off
                    self.owl.detection_enable.value = False
                    self.status_indicator.disable_weed_detection()
                    self.owl.relay_controller.relay.all_off()

        except KeyboardInterrupt:
            logger.info("[INFO] KeyboardInterrupt received in set_detection_mode. Exiting.")
            raise
        except Exception as e:
            logger.error(f"Error in set_detection_mode: {e}", exc_info=True)

    def update_detection_mode_state(self):
        if self.detection_mode_switch_up.is_pressed:
            self.set_detection_mode(2)  # All solenoids on
        elif self.detection_mode_switch_down.is_pressed:
            self.set_detection_mode(0)  # Detection on
        else:
            self.set_detection_mode(1)  # Off

    def weed_detect_indicator(self):
        self.status_indicator.weed_detect_indicator()

    def image_write_indicator(self):
        self.status_indicator.image_write_indicator()

    def run(self):
        try:
            while not self.stop_flag.value:
                time.sleep(0.1)  # sleep to reduce CPU usage
        except KeyboardInterrupt:
            logger.info("[INFO] KeyboardInterrupt received in controller run loop. Exiting.")
            self.stop()  # Ensure the stop flag is set
        except Exception as e:
            logger.error(f"Error in controller run loop: {e}", exc_info=True)

    def stop(self):
        with self.stop_flag.get_lock():
            self.stop_flag.value = True

    def _read_config(self, config_file):
        config = configparser.ConfigParser()
        config.read(config_file)
        return {
            'exg_min': config.getint('GreenOnBrown', 'exg_min'),
            'exg_max': config.getint('GreenOnBrown', 'exg_max'),
            'hue_min': config.getint('GreenOnBrown', 'hue_min'),
            'hue_max': config.getint('GreenOnBrown', 'hue_max'),
            'saturation_min': config.getint('GreenOnBrown', 'saturation_min'),
            'saturation_max': config.getint('GreenOnBrown', 'saturation_max'),
            'brightness_min': config.getint('GreenOnBrown', 'brightness_min'),
            'brightness_max': config.getint('GreenOnBrown', 'brightness_max')
        }

def get_rpi_version():
    try:
        cmd = ["cat", "/proc/device-tree/model"]
        model = subprocess.check_output(cmd).decode('utf-8').rstrip('\x00').strip()

        if 'Pi 5' in model:
            return 'rpi-5'
        elif 'Pi 4' in model:
            return 'rpi-4'
        elif 'Pi 3' in model:
            return 'rpi-3'
        else:
            return 'non-rpi'

    except FileNotFoundError:
        return 'non-rpi'
    except subprocess.CalledProcessError:

        raise ValueError("Error reading Raspberry Pi version.")


