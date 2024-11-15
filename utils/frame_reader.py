from pathlib import Path
import time
import cv2
import logging
from typing import Optional, Tuple, Union
from imutils.video import FileVideoStream

logger = logging.getLogger(__name__)


class FrameReader:
    """Handles reading of different media types for OWL processing."""

    def __init__(self, path: Union[str, Path], resolution: Optional[Tuple[int, int]] = None, loop_time: float = 5.0):
        """
        Initialize media reader for images, videos or directories.

        Args:
            path: Path to media (directory, image, or video)
            resolution: Optional (width, height) to resize media
            loop_time: Time between frames when reading from directory
        """
        self.path = Path(path)
        self._resolution = None
        self.loop_time = loop_time
        self.loop_start_time = time.time()
        self.cam = None
        self.curr_image = None
        self.files = None
        self.single_image = False

        if not self.path.exists():
            raise FileNotFoundError(f"Path does not exist: {self.path}")

        if self.path.is_dir():
            self._setup_directory()
        else:
            self._setup_file()

        # Set provided resolution after getting original dimensions
        if resolution:
            self._resolution = resolution

        logger.info(f"Initialized FrameReader for {self.path} with resolution {self._resolution}")

    def _setup_directory(self):
        """Set up for reading from directory of images."""
        files = list(self.path.glob("*.[jp][pn][g]"))  # jpg, jpeg, png
        if not files:
            raise ValueError(f"No valid images found in {self.path}")

        # Get dimensions from first image
        first_img = cv2.imread(str(files[0]))
        if first_img is None:
            raise ValueError(f"Could not read first image: {files[0]}")

        h, w = first_img.shape[:2]
        self._resolution = (w, h)
        self.files = iter(files)
        self.input_type = "directory"

    def _setup_file(self):
        """Set up for reading from single image or video file."""
        if self.path.suffix.lower() in ('.jpg', '.jpeg', '.png'):
            img = cv2.imread(str(self.path))
            if img is None:
                raise ValueError(f"Could not read image: {self.path}")

            h, w = img.shape[:2]
            self._resolution = (w, h)
            self.cam = img
            self.input_type = "image"
            self.single_image = True

        elif self.path.suffix.lower() in ('.mp4', '.avi', '.mov'):
            # Get dimensions first
            cap = cv2.VideoCapture(str(self.path))
            if not cap.isOpened():
                raise ValueError(f"Could not open video: {self.path}")

            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            self._resolution = (w, h)
            cap.release()

            # Initialize video stream
            self.cam = FileVideoStream(str(self.path)).start()
            time.sleep(1)  # Allow stream to initialize
            self.input_type = "video"
        else:
            raise ValueError(f"Unsupported file type: {self.path.suffix}")

    @property
    def resolution(self) -> Tuple[int, int]:
        """Current resolution as (width, height)."""
        return self._resolution

    def read(self):
        """Read next frame/image from the source."""
        if self.single_image:
            return self.cam

        if self.input_type == "directory":
            return self._read_from_directory()

        return self._read_from_video()

    def _read_from_directory(self):
        """Handle reading from image directory."""
        if self.curr_image is None or (time.time() - self.loop_start_time) > self.loop_time:
            try:
                img_path = next(self.files)
                self.curr_image = cv2.imread(str(img_path))
                if self.curr_image is None:
                    raise ValueError(f"Could not read image: {img_path}")

                if self._resolution:
                    self.curr_image = cv2.resize(self.curr_image, self._resolution,
                                                 interpolation=cv2.INTER_AREA)
                self.loop_start_time = time.time()
            except StopIteration:
                self.files = iter(self.path.glob("*.[jp][pn][g]"))
                return self._read_from_directory()

        return self.curr_image

    def _read_from_video(self):
        """Handle reading from video stream."""
        frame = self.cam.read()
        if frame is not None and self._resolution:
            frame = cv2.resize(frame, self._resolution, interpolation=cv2.INTER_AREA)
        return frame

    def reset(self):
        """Reset reader to beginning of source."""
        if self.input_type == "directory":
            self.files = iter(self.path.glob("*.[jp][pn][g]"))
            self.curr_image = None
        elif self.input_type == "video":
            self.cam.stop()
            self.cam = FileVideoStream(str(self.path)).start()
        self.loop_start_time = time.time()

    def stop(self):
        """Clean up resources."""
        if not self.single_image and self.cam:
            self.cam.stop()