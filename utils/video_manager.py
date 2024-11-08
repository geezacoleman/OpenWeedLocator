import cv2
import time

from threading import Thread, Event, Condition, Lock
from utils.log_manager import LogManager

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

# class to support webcams
class WebcamStream:
    def __init__(self, src=0):
        self.logger = LogManager.get_logger(__name__)
        self.name = "WebcamStream"
        self.logger.info(f'Camera type: {self.name}')
        self.stream = cv2.VideoCapture(src)

        self.frame_width = self.stream.get(cv2.CAP_PROP_FRAME_WIDTH)
        self.frame_height = self.stream.get(cv2.CAP_PROP_FRAME_HEIGHT)

        # Check if the stream opened successfully
        if not self.stream.isOpened():
            self.stream.release()
            self.logger.error(f'Unable to open video source: {src}')
            raise ValueError("Unable to open video source:", src)

        # read the first frame from the stream
        self.grabbed, self.frame = self.stream.read()
        if not self.grabbed:
            self.stream.release()
            self.logger.error(f'Unable to read from video source: {src}')
            raise ValueError("Unable to read from video source:", src)

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
        self.thread.join()


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

            while self.lock:
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
    def __init__(self, src=0, resolution=(416, 320), exp_compensation=-2, **kwargs):
        self.CAMERA_VERSION = PICAMERA_VERSION if PICAMERA_VERSION is not None else 'webcam'
        self.logger = LogManager.get_logger(__name__)
        self.frame_height = None
        self.frame_width = None

        if self.CAMERA_VERSION == 'legacy':
            self.stream = PiCameraStream(resolution=resolution, exp_compensation=exp_compensation, **kwargs)

        elif self.CAMERA_VERSION == 'picamera2':
            self.stream = PiCamera2Stream(src=src, resolution=resolution, exp_compensation=exp_compensation, **kwargs)

        elif self.CAMERA_VERSION == 'webcam':
            self.stream = WebcamStream(src=src)

        else:
            self.logger.error(f"Unsupported camera version: {self.CAMERA_VERSION}")
            raise ValueError(f"Unsupported camera version: {self.CAMERA_VERSION}")

        # set the image dimensions directly from the frame streamed
        self.frame_width = self.stream.frame_width
        self.frame_height = self.stream.frame_height

    def start(self):
        # start the threaded video stream
        return self.stream.start()

    def update(self):
        # grab the next frame from the stream
        self.stream.update()

    def read(self):
        # return the current frame
        return self.stream.read()

    def stop(self):
        # stop the thread and release any resources
        self.stream.stop()


