import cv2
import time

import numpy as np

from threading import Thread, Event, Condition, Lock
from utils.log_manager import LogManager

# determine the availability of cameras
ARENA_CAMERA = False
PICAMERA_VERSION = None
IMX500_CAMERA = False

try:
    from picamera.array import PiRGBArray
    from picamera import PiCamera

    PICAMERA_VERSION = 'legacy'
except Exception:
    pass

try:
    from arena_api.system import system
    from arena_api.buffer import *
    import ctypes

    ARENA_CAMERA = True
except Exception:
    pass

# First try standard picamera2
try:
    from picamera2 import Picamera2
    from libcamera import Transform
    import libcamera

    PICAMERA_VERSION = 'picamera2'

    # Then check for IMX500 specific modules
    try:
        from picamera2.devices import IMX500
        from picamera2.devices.imx500 import (NetworkIntrinsics, scale_boxes,
                                              postprocess_nanodet_detection)

        IMX500_CAMERA = True
    except ImportError:
        IMX500_CAMERA = False

except Exception:
    pass

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
        return self.frame

    def stop(self):
        self.stopped.set()

        self.thread.join()


class ArenaCameraStream:
    def __init__(self, resolution=(416, 320),
                 white_balance_auto=True,
                 target_brightness=None,
                 gain_control=None,
                 exposure_auto=True,
                 exposure_time=None):
        self.logger = LogManager.get_logger(__name__)
        self.name = "ArenaCameraStream"
        self.resolution = resolution
        self.frame_width, self.frame_height = resolution
        self.frame = None
        self.stop_event = Event()
        self.lock = Lock()
        self.condition = Condition()

        try:
            self.devices = self._create_devices_with_tries()
            if not self.devices:
                self.logger.error("No Arena cameras found.")
                raise ValueError("No Arena cameras found.")

            self.device = system.select_device(self.devices)
            self.nodemap = self.device.nodemap
            self.tl_stream_nodemap = self.device.tl_stream_nodemap

            # Get and log device info
            device_info = self._get_device_info()
            self.logger.info(
                f"Device control > Serial: {device_info.get('serial', 'N/A')}, Model: {device_info.get('model', 'N/A')}")

            # Configure stream settings
            self._configure_stream()

            # Configure camera settings
            self._configure_camera_settings(
                white_balance_auto=white_balance_auto,
                target_brightness=target_brightness,
                gain_control=gain_control,
                exposure_auto=exposure_auto,
                exposure_time=exposure_time
            )

            # Get actual resolution from camera
            self._update_resolution()

            # Start streaming
            self.device.start_stream()
            self.logger.info("Arena camera streaming started.")
        except Exception as e:
            self.logger.error(f"Failed to initialize Arena camera: {e}", exc_info=True)
            raise

        self.thread = Thread(target=self.update, name=self.name)
        self.thread.daemon = True

    def _create_devices_with_tries(self):
        tries = 0
        tries_max = 6
        sleep_time_secs = 10
        while tries < tries_max:
            devices = system.create_device()
            if not devices:
                self.logger.info(f'Try {tries + 1} of {tries_max}: waiting for {sleep_time_secs} secs for a device')
                for sec_count in range(sleep_time_secs):
                    time.sleep(1)
                tries += 1
            else:
                self.logger.info(f'Created {len(devices)} device(s)')
                return devices

        self.logger.error('No device found after multiple attempts.')
        return None

    def _get_device_info(self):
        device_info = {}
        try:
            nodes = self.nodemap.get_node(['DeviceSerialNumber', 'DeviceModelName'])
            if 'DeviceSerialNumber' in nodes:
                device_info['serial'] = nodes['DeviceSerialNumber'].value
            if 'DeviceModelName' in nodes:
                device_info['model'] = nodes['DeviceModelName'].value
        except Exception as e:
            self.logger.warning(f"Error getting device info: {e}")
        return device_info

    def _update_resolution(self):
        try:
            nodes = self.nodemap.get_node(['Width', 'Height'])
            if nodes['Width'] and nodes['Height']:
                self.frame_width = int(nodes['Width'].value)
                self.frame_height = int(nodes['Height'].value)
                self.logger.info(f"Camera resolution: {self.frame_width}x{self.frame_height}")
        except Exception as e:
            self.logger.warning(f"Unable to update frame dimensions: {e}")

    def _configure_stream(self):
        try:
            # Set buffer handling mode
            buffer_handling_node = self.tl_stream_nodemap.get_node('StreamBufferHandlingMode')
            if buffer_handling_node and buffer_handling_node.is_writable:
                buffer_handling_node.value = "NewestOnly"

            # Set auto negotiate packet size
            auto_negotiate_node = self.tl_stream_nodemap.get_node('StreamAutoNegotiatePacketSize')
            if auto_negotiate_node and auto_negotiate_node.is_writable:
                auto_negotiate_node.value = True

            # Set packet resend
            packet_resend_node = self.tl_stream_nodemap.get_node('StreamPacketResendEnable')
            if packet_resend_node and packet_resend_node.is_writable:
                packet_resend_node.value = True

            self.logger.info("Stream configured for optimal performance")
        except Exception as e:
            self.logger.warning(f"Error configuring stream: {e}")

    def _configure_camera_settings(self, white_balance_auto, target_brightness,
                                   gain_control, exposure_auto, exposure_time):
        try:
            # Configure white balance
            wb_node = self.nodemap.get_node('BalanceWhiteAuto')
            if wb_node and wb_node.is_writable:
                wb_node.value = 'Continuous' if white_balance_auto else 'Off'
                self.logger.info(f"White balance mode: {wb_node.value}")

            # Configure target brightness
            if target_brightness is not None:
                brightness_node = self.nodemap.get_node('TargetBrightness')
                if brightness_node and brightness_node.is_writable:
                    # Ensure value is within valid range
                    if target_brightness > brightness_node.max:
                        target_brightness = brightness_node.max
                    elif target_brightness < brightness_node.min:
                        target_brightness = brightness_node.min
                    brightness_node.value = target_brightness
                    self.logger.info(f"Target brightness: {brightness_node.value}")

            # Configure gain
            if gain_control is not None:
                gain_node = self.nodemap.get_node('GainAuto')
                if gain_node and gain_node.is_writable:
                    if isinstance(gain_control, bool):
                        gain_node.value = 'Continuous' if gain_control else 'Off'
                    else:
                        gain_node.value = gain_control
                    self.logger.info(f"Gain mode: {gain_node.value}")

            # Configure exposure
            exposure_auto_node = self.nodemap.get_node('ExposureAuto')
            if exposure_auto_node and exposure_auto_node.is_writable:
                exposure_auto_node.value = 'Continuous' if exposure_auto else 'Off'
                self.logger.info(f"Exposure mode: {exposure_auto_node.value}")

                # Set exposure time if auto is off and time is provided
                if not exposure_auto and exposure_time is not None:
                    exposure_time_node = self.nodemap.get_node('ExposureTime')
                    if exposure_time_node and exposure_time_node.is_writable:
                        # Ensure value is within valid range
                        if exposure_time > exposure_time_node.max:
                            exposure_time = exposure_time_node.max
                        elif exposure_time < exposure_time_node.min:
                            exposure_time = exposure_time_node.min
                        exposure_time_node.value = exposure_time
                        self.logger.info(f"Exposure time: {exposure_time_node.value}")

        except Exception as e:
            self.logger.warning(f"Error configuring camera settings: {e}")

    def start(self):
        self.thread.start()
        return self

    def update(self):
        try:
            curr_frame_time = 0
            prev_frame_time = 0

            while not self.stop_event.is_set():
                try:
                    curr_frame_time = time.time()

                    # Get buffer from device
                    buffer = self.device.get_buffer()

                    # Process buffer data
                    try:
                        # Copy the buffer to avoid running out of buffers
                        item = BufferFactory.copy(buffer)

                        # Calculate bytes per pixel
                        buffer_bytes_per_pixel = 3  # Assuming RGB8 format

                        # Access buffer data using cpointer approach from the example
                        array = (ctypes.c_ubyte * buffer_bytes_per_pixel * item.width * item.height).from_address(
                            ctypes.addressof(item.pbytes))

                        # Create a reshaped NumPy array
                        frame = np.ndarray(buffer=array, dtype=np.uint8,
                                           shape=(item.height, item.width, buffer_bytes_per_pixel))

                        # Update the frame
                        with self.lock:
                            self.frame = frame.copy()  # Make a copy to prevent reference issues

                        with self.condition:
                            self.condition.notify_all()

                        # Calculate FPS for debugging
                        if prev_frame_time > 0:
                            fps = 1 / (curr_frame_time - prev_frame_time)
                            if fps < 10:  # Only log if FPS is low
                                self.logger.debug(f"Frame rate: {fps:.2f} FPS")

                        # Cleanup copied buffer
                        BufferFactory.destroy(item)

                    except Exception as e:
                        self.logger.error(f"Error converting buffer to image: {e}", exc_info=True)

                    # Always requeue the original buffer
                    self.device.requeue_buffer(buffer)

                    # Update previous frame time
                    prev_frame_time = curr_frame_time

                except Exception as e:
                    self.logger.error(f"Error getting buffer: {e}")
                    time.sleep(0.01)

                time.sleep(0.001)

        except Exception as e:
            self.logger.error(f"Exception in update loop: {e}")
        finally:
            try:
                self.device.stop_stream()
                self.logger.info("Camera streaming stopped")
            except Exception as e:
                self.logger.error(f"Error stopping stream: {e}")

            try:
                system.destroy_device()
                self.logger.info("Camera device destroyed")
            except Exception as e:
                self.logger.error(f"Error destroying device: {e}")

    def read(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def stop(self):
        self.stop_event.set()
        self.thread.join()
        self.logger.info("ArenaCameraStream stopped")


class IMX500Stream:
    def __init__(self, resolution=(640, 480), threshold=0.50, iou=0.65, max_detections=10,
                 model_path=None):
        self.logger = LogManager.get_logger(__name__)
        self.name = "IMX500Stream"
        self.resolution = resolution
        self.frame_width, self.frame_height = resolution
        self.frame = None
        self.last_detections = []
        self.stop_event = Event()
        self.lock = Lock()
        self.condition = Condition()

        try:
            # Initialize IMX500 camera and network
            self.imx500 = IMX500(model_path)
            self.intrinsics = self.imx500.network_intrinsics
            if not self.intrinsics:
                self.intrinsics = NetworkIntrinsics()
                self.intrinsics.task = "object detection"
            elif self.intrinsics.task != "object detection":
                raise ValueError("Network is not configured for object detection")

            # Configure detection parameters
            self.threshold = threshold
            self.iou = iou
            self.max_detections = max_detections

            # Initialize camera
            self.camera = Picamera2(self.imx500.camera_num)
            self.config = self.camera.create_preview_configuration(
                controls={"FrameRate": self.intrinsics.inference_rate},
                buffer_count=12
            )

            # Load default labels if not provided
            if self.intrinsics.labels is None:
                with open("assets/coco_labels.txt", "r") as f:
                    self.intrinsics.labels = f.read().splitlines()
            self.intrinsics.update_with_defaults()

            # Start camera
            self.imx500.show_network_fw_progress_bar()
            self.camera.start(self.config)

            if self.intrinsics.preserve_aspect_ratio:
                self.imx500.set_auto_aspect_ratio()

            self.logger.info("IMX500 camera initialized successfully")

        except Exception as e:
            self.logger.error(f"Failed to initialize IMX500 camera: {e}", exc_info=True)
            raise

        self.thread = Thread(target=self.update, name=self.name)
        self.thread.daemon = True

    def parse_detections(self, metadata):
        """Parse network output into detections"""
        bbox_normalization = self.intrinsics.bbox_normalization
        bbox_order = self.intrinsics.bbox_order

        np_outputs = self.imx500.get_outputs(metadata, add_batch=True)
        input_w, input_h = self.imx500.get_input_size()

        if np_outputs is None:
            return self.last_detections

        if self.intrinsics.postprocess == "nanodet":
            boxes, scores, classes = postprocess_nanodet_detection(
                outputs=np_outputs[0],
                conf=self.threshold,
                iou_thres=self.iou,
                max_out_dets=self.max_detections
            )[0]
            boxes = scale_boxes(boxes, 1, 1, input_h, input_w, False, False)
        else:
            boxes, scores, classes = np_outputs[0][0], np_outputs[1][0], np_outputs[2][0]
            if bbox_normalization:
                boxes = boxes / input_h
            if bbox_order == "xy":
                boxes = boxes[:, [1, 0, 3, 2]]
            boxes = np.array_split(boxes, 4, axis=1)
            boxes = zip(*boxes)

        return [
            {"box": self.imx500.convert_inference_coords(box, metadata, self.camera),
             "confidence": score,
             "class_id": int(category)}
            for box, score, category in zip(boxes, scores, classes)
            if score > self.threshold
        ]

    def start(self):
        self.thread.start()
        return self

    def update(self):
        try:
            while not self.stop_event.is_set():
                metadata = self.camera.capture_metadata()
                frame = self.camera.capture_array("main")
                detections = self.parse_detections(metadata)

                with self.lock:
                    self.frame = frame
                    self.last_detections = detections

                with self.condition:
                    self.condition.notify_all()

                time.sleep(0.001)

        except Exception as e:
            self.logger.error(f"Exception in {self.name} update loop: {e}", exc_info=True)
        finally:
            try:
                self.camera.stop()
                self.logger.info("IMX500 camera stopped")
            except Exception as stop_err:
                self.logger.error(f"Error stopping IMX500 camera: {stop_err}", exc_info=True)

    def read(self):
        """Return current frame"""
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def detect(self):
        """Return latest detections"""
        with self.lock:
            return self.last_detections.copy() if self.last_detections else []

    def stop(self):
        self.stop_event.set()
        self.thread.join()
        self.logger.info("IMX500 stream stopped")


# overarching class to determine which stream to use
class VideoStream:
    def __init__(self, src=0, resolution=(416, 320), exp_compensation=-2, **kwargs):
        if ARENA_CAMERA:
            self.CAMERA_VERSION = 'arena'
        elif IMX500_CAMERA:
            self.CAMERA_VERSION = 'imx500'
        else:
            self.CAMERA_VERSION = PICAMERA_VERSION if PICAMERA_VERSION is not None else 'webcam'

        self.logger = LogManager.get_logger(__name__)
        self.frame_height = None
        self.frame_width = None

        if self.CAMERA_VERSION == 'legacy':
            self.stream = PiCameraStream(resolution=resolution, exp_compensation=exp_compensation, **kwargs)

        elif self.CAMERA_VERSION == 'arena':
            self.stream = ArenaCameraStream(resolution=resolution, **kwargs)

        elif self.CAMERA_VERSION == 'imx500':
            self.stream = IMX500Stream(resolution=resolution, **kwargs)

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


