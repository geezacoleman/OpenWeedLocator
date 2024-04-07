import cv2
import time
from threading import Thread, Event

# determine availability of picamera versions
try:
    from picamera.array import PiRGBArray
    from picamera import PiCamera
    PICAMERA_VERSION = 'legacy'

except ImportError:
    PICAMERA_VERSION = None

try:
    from picamera2 import Picamera2
    from libcamera import Transform
    PICAMERA_VERSION = 'picamera2'

except ImportError:
    PICAMERA_VERSION = None

# class to support webcams
class WebcamStream:
    def __init__(self, src=0):
        self.name = "WebcamStream"
        self.stream = cv2.VideoCapture(src)

        # Check if the stream opened successfully
        if not self.stream.isOpened():
            self.stream.release()  # Ensure resources are released
            raise ValueError("Unable to open video source:", src)

        # read the first frame from the stream
        self.grabbed, self.frame = self.stream.read()
        if not self.grabbed:
            self.stream.release()  # Ensure resources are released if no frame is grabbed
            raise ValueError("Unable to read from video source:", src)

        # initialize the thread name, stop event, and the thread itself
        self.stop_event = Event()
        self.thread = Thread(target=self.update, name=self.name, args=())
        self.thread.daemon = True  # Thread will close when main program exits

    def start(self):
        # start the thread to read frames from the video stream
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
            print(f"Exception in WebcamStream update loop: {e}")
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
    def __init__(self, resolution=(320, 240), framerate=32, **kwargs):
        self.name = 'Picamera2Stream'
        self.size = resolution  # picamera2 uses size instead of resolution, keeping this consistent
        self.framerate = framerate

        self.config = {
            "format": 'XRGB8888',
            "size": self.size
        }
        # Update config with any additional/overridden parameters
        self.config.update(kwargs)

        # Initialize the camera
        self.picam2 = Picamera2()
        try:
            self.picam2.set_logging(Picamera2.INFO)
            self.picam2.configure(self.picam2.create_preview_configuration(main=self.config))
            self.picam2.start()
            time.sleep(1)  # Allow the camera time to warm up
        except Exception as e:
            print(f"Failed to initialize PiCamera2: {e}")
            raise

        self.frame = None
        self.stopped = Event()

    def start(self):
        # Start the thread to update frames
        self.thread = Thread(target=self.update, name=self.name, args=())
        self.thread.daemon = True
        self.thread.start()
        return self

    def update(self):
        try:
            while not self.stopped.is_set():
                frame = self.picam2.capture_array("main")
                if frame is not None:
                    self.frame = frame
                time.sleep(0.01)  # Slow down loop a little
        except Exception as e:
            print(f"Exception in PiCamera2Stream update loop: {e}")
        finally:
            self.picam2.stop()  # Ensure camera resources are released properly

    def read(self):
        # return the frame most recently read
        return self.frame

    def stop(self):
        self.stopped.set()
        self.thread.join()
        self.picam2.stop()
        time.sleep(2)  # Allow time for the camera to be released properly


class PiCameraStream:
    def __init__(self, resolution=(320, 240), framerate=32, **kwargs):
        self.name = 'PiCameraStream'
        try:
            self.camera = PiCamera()

            self.camera.resolution = resolution
            self.camera.framerate = framerate

            # Set optional camera parameters (refer to PiCamera docs)
            for (arg, value) in kwargs.items():
                setattr(self.camera, arg, value)

            # Initialize the stream
            self.rawCapture = PiRGBArray(self.camera, size=resolution)
            self.stream = self.camera.capture_continuous(self.rawCapture,
                format="bgr", use_video_port=True)

        except Exception as e:
            print(f"Failed to initialize PiCamera: {e}")
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
            print(f"Exception in PiCameraStream update loop: {e}")

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

import configparser
# overarching class to determine stream to use
class VideoStream:
    def __init__(self, src=0, resolution=(416, 320), framerate=32, **kwargs):
        self.CAMERA_VERSION = PICAMERA_VERSION if PICAMERA_VERSION is not None else 'webcam'
        print(self.CAMERA_VERSION)

        if self.CAMERA_VERSION == 'legacy':
            self.stream = PiCameraStream(resolution=resolution, framerate=framerate, **kwargs)

        elif self.CAMERA_VERSION == 'picamera2':
            self.stream = PiCamera2Stream(resolution=resolution, framerate=framerate, **kwargs)

        elif self.CAMERA_VERSION == 'webcam':
            self.stream = WebcamStream(src=src)
            print('Using webcam')

        else:
            raise ConnectionError

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


