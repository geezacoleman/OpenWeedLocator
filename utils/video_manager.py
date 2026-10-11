from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

import cv2
import time
import platform
import subprocess

from typing import Optional
from threading import Thread, Event, Condition, Lock
from utils.log_manager import LogManager
from utils.error_manager import CameraNotFoundError

# determine availability of picamera versions
try:
    from picamera.array import PiRGBArray
    from picamera import PiCamera
    PICAMERA_VERSION = 'legacy'

except Exception as e:
    PICAMERA_VERSION = None

try:
    from picamera2 import Picamera2
    from libcamera import Transform
    import libcamera
    PICAMERA_VERSION = 'picamera2'

except Exception as e:
    PICAMERA_VERSION = None


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


class StreamingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urlparse(self.path)
        main_path = parsed_path.path

        owl = self.server.owl_instance

        if main_path == '/stream.mjpg':
            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
            self.end_headers()
            try:
                while True:
                    frame = owl.get_latest_stream_frame()
                    if frame is not None:
                        ret, buffer = cv2.imencode('.jpg', frame,
                                                   [cv2.IMWRITE_JPEG_QUALITY, 70])
                        if not ret:
                            continue
                        self.wfile.write(b'--FRAME\r\n')
                        self.send_header('Content-Type', 'image/jpeg')
                        self.send_header('Content-Length', len(buffer))
                        self.end_headers()
                        self.wfile.write(buffer)
                        self.wfile.write(b'\r\n')

                    time.sleep(1 / 30)  # Aim for ~30 FPS

            except BrokenPipeError:
                owl.logger.info(f"Streaming client disconnected (expected): {self.client_address}")

            except Exception as e:
                owl.logger.warning(f'Removed streaming client {self.client_address}: {e}')

        elif main_path == '/latest_frame.jpg':
            try:
                frame = owl.get_latest_stream_frame()
                if frame is not None:
                    ret, buffer = cv2.imencode('.jpg', frame,
                                               [cv2.IMWRITE_JPEG_QUALITY, 95])  # Higher quality for download
                    if ret:
                        self.send_response(200)
                        self.send_header('Content-Type', 'image/jpeg')
                        self.send_header('Content-Length', len(buffer))
                        self.end_headers()
                        self.wfile.write(buffer)
                        return

                self.send_error(404, 'No frame available')
            except Exception as e:
                owl.logger.error(f"Could not serve latest_frame.jpg: {e}")
                self.send_error(500)

        else:
            self.send_error(404)
            self.end_headers()

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread and hold a reference to the Owl instance."""

    def __init__(self, server_address, RequestHandlerClass, owl_instance):
        self.owl_instance = owl_instance
        super().__init__(server_address, RequestHandlerClass)

# class to support webcams
class WebcamStream:
    def __init__(self, src=0, resolution=(640, 480), framerate=30):
        self.logger = LogManager.get_logger(__name__)
        self.name = "WebcamStream"
        self.logger.info(f'Camera type: {self.name}')

        self.stream = cv2.VideoCapture(src, cv2.CAP_V4L2) if platform.system() == 'Linux' else cv2.VideoCapture(src)

        # Prefer MJPG for higher USB webcam resolutions (e.g., 1280x720 on C270).
        try:
            self.stream.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'YUYV'))
        except Exception:
            pass

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
                try:
                    frame = self.camera.capture_array("main")

                except Exception as e:
                    try:
                        dmesg_raw = subprocess.check_output(['dmesg', '-T'], text=True, stderr=subprocess.DEVNULL)
                        dmesg_tail = "\n".join(dmesg_raw.splitlines()[-50:])
                    except Exception:
                        dmesg_tail = "Could not read dmesg output."

                    original = f"Exception during Picamera2.capture_array(): {e}\n\nRecent kernel/libcamera messages (tail 50 lines):\n{dmesg_tail}"
                    err = CameraNotFoundError(error_type="capture_exception", original_error=original)

                    self.logger.critical(str(err), exc_info=True, extra={'error_code': 'camera_capture_exception'})
                    raise err

                if frame is None:
                    try:
                        dmesg_raw = subprocess.check_output(['dmesg', '-T'], text=True, stderr=subprocess.DEVNULL)
                        dmesg_tail = "\n".join(dmesg_raw.splitlines()[-50:])
                    except Exception:
                        dmesg_tail = "Could not read dmesg output."

                    original = (
                            "capture_array returned None (no frame). This commonly indicates a camera I/O error.\n\n"
                            "Recent kernel/libcamera messages (tail 50 lines):\n" + dmesg_tail
                    )
                    err = CameraNotFoundError(error_type="no_frame", original_error=original)
                    self.logger.critical(str(err), extra={'error_code': 'camera_no_frame'})

                    raise err

                with self.lock:
                    self.frame = frame
                    self.frame_available = True

                with self.condition:
                    self.condition.notify_all()

        except CameraNotFoundError:
            raise

        except Exception as e:
            self.logger.error(f"Exception in PiCamera2Stream update loop: {e}", exc_info=True)

        finally:
            try:
                self.camera.stop()
            except Exception:
                self.logger.debug("Camera stop raised an exception during cleanup.", exc_info=True)

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
        self.thread.join()
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
        self.thread.join()


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
            raise ValueError(f"Unsupported camera type: {effective_camera_type}")

        # Verify stream was initialized
        if self.stream is None:
            error_msg = "Failed to initialize any camera stream"
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

    def _init_rpi_camera(self, src, resolution, exp_compensation, **kwargs) -> Optional[str]:
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

    def _init_usb_camera(self, src, resolution) -> Optional[str]:
        """Initialize USB webcam. Returns error message on failure, None on success."""
        candidates = []

        if self.platform_info['system'] == 'Linux':
            # On SBCs, /dev/video0 can be a codec node (non-capture). Try several devices.
            if isinstance(src, str):
                candidates = [src]
            else:
                # Prefer real UVC capture nodes first on SBCs where /dev/video0 may be codec-only.
                candidates = ['/dev/video1', '/dev/video2', '/dev/video3', '/dev/video0']
        else:
            candidates = [src if isinstance(src, int) else 0]

        last_error = None
        for video_src in candidates:
            try:
                self.stream = WebcamStream(src=video_src, resolution=resolution)
                self.logger.info(f"USB camera opened on source: {video_src}")
                return None
            except Exception as e:
                last_error = str(e)
                self.logger.warning(f"USB camera source failed: {video_src} ({e})")

        self.logger.error(f"WebcamStream initialization failed on all candidates: {candidates}", exc_info=True)
        return last_error or 'No usable USB camera source found'

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


