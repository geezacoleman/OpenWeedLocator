import cv2
import time
import platform

from threading import Thread, Event, Condition, Lock
from utils.log_manager import LogManager

# determine availability of picamera versions
PICAMERA_VERSION = None

try:
    from picamera.array import PiRGBArray
    from picamera import PiCamera

    PICAMERA_VERSION = 'legacy'
except ImportError:
    pass

try:
    from picamera2 import Picamera2
    from libcamera import Transform
    import libcamera

    PICAMERA_VERSION = 'picamera2'
except ImportError:
    pass


def is_raspberry_pi() -> bool:
    """Check if running on a Raspberry Pi."""
    try:
        with open('/proc/device-tree/model', 'r') as f:
            model = f.read().lower()
            return 'raspberry pi' in model
    except (FileNotFoundError, IOError):
        return False


def get_platform_info() -> dict:
    """Get platform information for camera selection."""
    return {
        'system': platform.system(),
        'is_rpi': is_raspberry_pi(),
        'picamera_version': PICAMERA_VERSION
    }


# class to support webcams
class WebcamStream:
    def __init__(self, src=0, resolution=(640, 480), framerate=30):
        self.logger = LogManager.get_logger(__name__)
        self.name = "WebcamStream"
        self.logger.info(f'Camera type: {self.name}')

        self.stream = cv2.VideoCapture(src)

        # Check if the stream opened successfully
        if not self.stream.isOpened():
            self.stream.release()
            self.logger.error(f'Unable to open video source: {src}')
            raise ValueError(f'Unable to open video source: {src}')

        target_width, target_height = resolution

        self.stream.set(cv2.CAP_PROP_FRAME_WIDTH, target_width)
        self.stream.set(cv2.CAP_PROP_FRAME_HEIGHT, target_height)
        self.stream.set(cv2.CAP_PROP_FPS, framerate)

        self.frame_width = int(self.stream.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.frame_height = int(self.stream.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = self.stream.get(cv2.CAP_PROP_FPS)

        if (self.frame_width, self.frame_height) != (target_width, target_height):
            self.logger.warning(
                f"Requested resolution {target_width}x{target_height}, "
                f"but got {self.frame_width}x{self.frame_height} from webcam."
            )

        if actual_fps != 0 and abs(actual_fps - framerate) > 1:
            self.logger.warning(
                f"Requested FPS {framerate}, but webcam reports {actual_fps:.1f}."
            )

        # read the first frame from the stream
        self.grabbed, self.frame = self.stream.read()
        if not self.grabbed:
            self.stream.release()
            self.logger.error(f'Unable to read from video source: {src}')
            raise ValueError(f'Unable to read from video source: {src}')

        # initialize the thread name, stop event, and the thread itself
        self.stop_event = Event()
        self.thread = Thread(target=self.update, name=self.name, args=())
        self.thread.daemon = True

    def start(self):
        self.thread.start()
        return self

    def update(self):
        # keep looping infinitely until the thread is stopped
        try:
            while not self.stop_event.is_set():
                # Read the next frame from the stream
                self.grabbed, self.frame = self.stream.read()

                # If not grabbed, end of the stream has been reached.
                if not self.grabbed:
                    self.stop_event.set()  # Ensure the loop stops if no frame is grabbed
        except Exception as e:
            self.logger.error(f"Exception in WebcamStream update loop: {e}", exc_info=True)
        finally:
            # Clean up resources after loop is done
            self.stream.release()

    def read(self):
        # return the frame most recently read
        return self.frame

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2.0)
        if self.stream.isOpened():
            self.stream.release()


class PiCamera2Stream:
    def __init__(self, src=0, resolution=(416, 320), exp_compensation=-2, **kwargs):
        self.logger = LogManager.get_logger(__name__)
        self.name = 'Picamera2Stream'
        self.logger.info(f'Camera type: {self.name}')
        self.size = resolution  # picamera2 uses size instead of resolution, keeping this consistent
        self.frame_width = None
        self.frame_height = None
        self.frame = None
        self.frame_available = False

        self.stopped = Event()
        self.condition = Condition()
        self.lock = Lock()

        # set the picamera2 config and controls. Refer to picamera2 documentation for full explanations:

        self.configurations = {
            # for those checking closely, using RGB888 may seem incorrect, however libcamera means a BGR format. Check
            # https://github.com/raspberrypi/picamera2/issues/848 for full explanation.
            "format": 'RGB888',
            "size": self.size
        }

        self.controls = {
            "AeExposureMode": 1,
            "AwbMode": libcamera.controls.AwbModeEnum.Daylight,
            "ExposureValue": exp_compensation
        }
        # Or if you prefer split logs for different aspects:
        self.logger.info("Setting camera format", extra=dict(
            format='RGB888',
            image_size=list(self.size),
            note='RGB888 represents BGR format in libcamera'))

        self.logger.info("Setting camera controls", extra=dict(
            exposure_mode=1,
            awb_mode='Daylight',
            exposure_value=exp_compensation))

        # Update config with any additional/overridden parameters
        self.controls.update(kwargs)

        # Initialize the camera
        self.camera = Picamera2(src)
        self.camera_model = self.camera.camera_properties['Model']

        if self.camera_model == 'imx296':
            self.logger.info('[INFO] Using IMX296 Global Shutter Camera')

        elif self.camera_model == 'imx477':
            self.logger.info('[INFO] Using IMX477 HQ Camera')

        elif self.camera_model == 'imx708':
            self.logger.info('[INFO] Using Raspberry Pi Camera Module 3. Setting focal point at 1.2 m...')
            self.controls['AfMode'] = libcamera.controls.AfModeEnum.Manual
            self.controls['LensPosition'] = 1.2

        else:
            self.logger.info('[INFO] Unrecognised camera module, continuing with default settings.')

        try:
            self.config = self.camera.create_preview_configuration(main=self.configurations,
                                                                   transform=Transform(hflip=True, vflip=True),
                                                                   queue=False,
                                                                   controls=self.controls)
            self.camera.configure(self.config)
            self.camera.start()

            # set dimensions directly from the video feed
            self.frame_width = self.camera.camera_configuration()['main']['size'][0]
            self.frame_height = self.camera.camera_configuration()['main']['size'][1]

            # allow the camera time to warm up
            time.sleep(2)

        except Exception as e:
            self.logger.error(f"Failed to initialize PiCamera2: {e}", exc_info=True)
            raise

        if self.frame_width != resolution[0] or self.frame_height != resolution[1]:
            message = (f"The actual frame size ({self.frame_width}x{self.frame_height}) "
                       f"differs from the expected resolution ({resolution[0]}x{resolution[1]}).")
            self.logger.warning(message)

    def start(self):
        # Start the thread to update frames
        self.thread = Thread(target=self.update, name=self.name, args=())
        self.thread.daemon = True
        self.thread.start()
        return self

    def update(self):
        try:
            while not self.stopped.is_set():
                frame = self.camera.capture_array("main")
                if frame is not None:
                    with self.lock:
                        self.frame = frame
                        self.frame_available = True

                    with self.condition:
                        self.condition.notify_all()

        except Exception as e:
            self.logger.error(f"Exception in PiCamera2Stream update loop: {e}", exc_info=True)
        finally:
            self.camera.stop()  # Ensure camera resources are released properly

    def read(self):
        # return the frame most recently read
        with self.condition:
            while not self.frame_available:
                self.condition.wait()

            with self.lock:
                self.frame_available = False
                return self.frame

    def stop(self):
        self.stopped.set()
        with self.condition:
            self.condition.notify_all()  # Wake up any waiting threads
        if hasattr(self, 'thread') and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self.camera.stop()
        time.sleep(2)  # Allow time for the camera to be released properly


class PiCameraStream:
    def __init__(self, resolution=(416, 320), exp_compensation=-2, **kwargs):
        self.logger = LogManager.get_logger(__name__)
        self.name = 'PicameraStream'
        self.logger.info(f'Camera type: {self.name}')
        self.frame_width = None
        self.frame_height = None

        try:
            self.camera = PiCamera()

            self.camera.resolution = resolution
            self.camera.exposure_mode = 'beach'
            self.camera.awb_mode = 'auto'
            self.camera.sensor_mode = 0
            self.camera.exposure_compensation = exp_compensation

            self.frame_width = self.camera.resolution[0]
            self.frame_height = self.camera.resolution[1]

            if self.frame_width != resolution[0] or self.frame_height != resolution[1]:
                message = (f"The actual frame size ({self.frame_width}x{self.frame_height}) "
                           f"differs from the expected resolution ({resolution[0]}x{resolution[1]}).")
                self.logger.warning(message)

            # Set optional camera parameters (refer to PiCamera docs)
            for (arg, value) in kwargs.items():
                setattr(self.camera, arg, value)

            # Initialize the stream
            self.rawCapture = PiRGBArray(self.camera, size=resolution)
            self.stream = self.camera.capture_continuous(self.rawCapture,
                                                         format="bgr",
                                                         use_video_port=True)

        except Exception as e:
            self.logger.error(f"Failed to initialize PiCamera: {e}", exc_info=True)
            raise

        self.frame = None
        self.stopped = Event()
        self.thread = Thread(target=self.update, name=self.name, args=())
        self.thread.daemon = True  # Thread will close when main program exits

    def start(self):
        # Start the thread to read frames from the video stream
        self.thread.start()
        return self

    def update(self):
        try:
            for f in self.stream:
                self.frame = f.array
                self.rawCapture.truncate(0)

                if self.stopped.is_set():
                    break
        except Exception as e:
            self.logger.error(f"Exception in PiCameraStream update loop: {e}", exc_info=True)

        finally:
            self.stream.close()
            self.rawCapture.close()
            self.camera.close()

    def read(self):
        # return the frame most recently read
        return self.frame

    def stop(self):
        # Signal the thread to stop
        self.stopped.set()

        # Wait for the thread to finish
        if self.thread.is_alive():
            self.thread.join(timeout=2.0)


# overarching class to determine which stream to use
class VideoStream:
    """
    Unified video stream interface that automatically selects the appropriate
    camera backend based on platform and configuration.

    Camera type options:
        - 'rpi': Force Raspberry Pi camera (PiCamera2 or legacy)
        - 'usb': Force USB webcam
        - 'auto': Auto-detect best available camera (Pi camera preferred on RPi)

    On non-Linux systems (Windows/Mac), automatically uses USB webcam regardless
    of camera_type setting.
    """

    def __init__(self, src=0, resolution=(416, 320), exp_compensation=-2, camera_type='auto', **kwargs):
        self.logger = LogManager.get_logger(__name__)
        self.platform_info = get_platform_info()
        self.frame_height = None
        self.frame_width = None
        self.stream = None

        # Resolve effective camera type based on platform
        effective_camera_type = self._resolve_camera_type(camera_type)
        self.logger.info(
            f"Camera selection: requested='{camera_type}', effective='{effective_camera_type}', "
            f"platform={self.platform_info['system']}, is_rpi={self.platform_info['is_rpi']}, "
            f"picamera_version={self.platform_info['picamera_version']}"
        )

        # Initialize the appropriate stream
        init_error = None

        if effective_camera_type == 'rpi':
            init_error = self._init_rpi_camera(src, resolution, exp_compensation, **kwargs)

        elif effective_camera_type == 'usb':
            init_error = self._init_usb_camera(src, resolution)

        else:
            # This shouldn't happen if _resolve_camera_type works correctly
            raise ValueError(f"Unsupported camera type: {effective_camera_type}")

        # Verify stream was initialized
        if self.stream is None:
            error_msg = f"Failed to initialize any camera stream"
            if init_error:
                error_msg += f": {init_error}"
            self.logger.error(error_msg)
            raise RuntimeError(error_msg)

        # Set frame dimensions from the initialized stream
        self.frame_width = self.stream.frame_width
        self.frame_height = self.stream.frame_height

    def _resolve_camera_type(self, camera_type: str) -> str:
        """
        Resolve the effective camera type based on platform.

        Note: 'auto' makes a single selection based on platform detection - it does NOT
        attempt fallback between camera types, as this can cause resource locking issues
        (e.g., libcamera holding /dev/video* devices after a failed Pi camera init).
        """
        camera_type = camera_type.lower().strip()

        # Non-Linux systems always use USB
        if self.platform_info['system'] != 'Linux':
            if camera_type == 'rpi':
                self.logger.warning(
                    f"RPi camera requested but running on {self.platform_info['system']}. "
                    f"Using USB webcam instead."
                )
            return 'usb'

        # Handle 'auto' - make a single decision, no fallback attempts
        if camera_type == 'auto':
            if self.platform_info['is_rpi'] and self.platform_info['picamera_version']:
                self.logger.info("Auto mode on RPi with picamera available: selecting RPi camera")
                return 'rpi'
            else:
                self.logger.info("Auto mode: selecting USB camera (not RPi or no picamera library)")
                return 'usb'

        return camera_type

    def _init_rpi_camera(self, src, resolution, exp_compensation, **kwargs) -> str | None:
        """Initialize Raspberry Pi camera. Returns error message on failure, None on success."""
        picamera_version = self.platform_info['picamera_version']

        if picamera_version is None:
            msg = "No PiCamera library available (neither picamera2 nor legacy picamera)"
            self.logger.error(msg)
            return msg

        if picamera_version == 'picamera2':
            try:
                self.stream = PiCamera2Stream(
                    src=src,
                    resolution=resolution,
                    exp_compensation=exp_compensation,
                    **kwargs
                )
                return None
            except Exception as e:
                self.logger.error(f"PiCamera2Stream initialization failed: {e}", exc_info=True)
                return str(e)

        elif picamera_version == 'legacy':
            try:
                self.stream = PiCameraStream(
                    resolution=resolution,
                    exp_compensation=exp_compensation,
                    **kwargs
                )
                return None
            except Exception as e:
                self.logger.error(f"PiCameraStream initialization failed: {e}", exc_info=True)
                return str(e)

        return f"Unknown picamera version: {picamera_version}"

    def _init_usb_camera(self, src, resolution) -> str | None:
        """Initialize USB webcam. Returns error message on failure, None on success."""
        # Determine video source based on platform
        if self.platform_info['system'] == 'Linux':
            video_src = src if isinstance(src, str) else '/dev/video0'
        else:
            video_src = src if isinstance(src, int) else 0

        try:
            self.stream = WebcamStream(src=video_src, resolution=resolution)
            return None
        except Exception as e:
            self.logger.error(f"WebcamStream initialization failed: {e}", exc_info=True)
            return str(e)

    def start(self):
        """Start the threaded video stream."""
        return self.stream.start()

    def update(self):
        """Grab the next frame from the stream."""
        self.stream.update()

    def read(self):
        """Return the current frame."""
        return self.stream.read()

    def stop(self):
        """Stop the thread and release any resources."""
        if self.stream:
            self.stream.stop()