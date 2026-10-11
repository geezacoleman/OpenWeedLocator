"""v3.22 hardening: the image recorder workers, the camera read, --input.

Real ImageRecorder worker processes write into tmp_path; the PiCamera2 stream
is built without a camera (only the frame hand-off is under test).
"""

import threading
import time

import cv2
import numpy as np
import pytest

from utils.frame_reader import FrameReader
from utils.image_sampler import ImageRecorder
from utils.video_manager import PiCamera2Stream


def frame():
    return np.full((64, 64, 3), 120, dtype=np.uint8)


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class TestImageRecorderWorkers:
    def test_a_bad_frame_is_counted_and_the_worker_keeps_saving(self, tmp_path):
        """One bad frame used to kill the worker; saving then stopped with
        nobody told."""
        recorder = ImageRecorder(save_directory=str(tmp_path), mode='whole')
        try:
            recorder.add_frame(None, 1, None, None)          # raises in the worker
            recorder.add_frame(frame(), 2, None, None)
            assert wait_for(lambda: list(tmp_path.glob('*.jpg'))), 'worker died'
            assert wait_for(lambda: recorder.failed.value == 1)
        finally:
            recorder.stop()

    def test_stop_without_wait_returns_at_once_and_keeps_queued_frames(self, tmp_path):
        """stop() is called from the detection loop at each recording start;
        it used to block >= 1 s and throw the queued frames away."""
        recorder = ImageRecorder(save_directory=str(tmp_path), mode='whole')
        for i in range(5):
            recorder.add_frame(frame(), i, None, None)

        started = time.monotonic()
        recorder.stop(wait=False)
        assert time.monotonic() - started < 0.2

        assert wait_for(lambda: len(list(tmp_path.glob('*.jpg'))) == 5)


class TestPiCamera2Read:
    def make_stream(self, thread_alive):
        stream = PiCamera2Stream.__new__(PiCamera2Stream)
        stream.lock = threading.Lock()
        stream.condition = threading.Condition()
        stream.frame_available = False
        stream.frame = stream.frame_metadata = None
        stream.thread = threading.Thread(target=lambda: None)
        if not thread_alive:
            stream.thread.start()
            stream.thread.join()
        return stream

    def test_dead_capture_thread_returns_no_frame_instead_of_hanging(self):
        """A dead capture thread (ribbon cable shaken loose) used to block the
        detection loop forever, so the camera-lost check never ran."""
        stream = self.make_stream(thread_alive=False)
        result = []
        reader = threading.Thread(target=lambda: result.append(stream.read_with_metadata()),
                                  daemon=True)
        reader.start()
        reader.join(timeout=3)
        assert result == [(None, None)]


class TestFrameReaderDirectory:
    def test_non_image_files_are_skipped(self, tmp_path):
        cv2.imwrite(str(tmp_path / 'a.png'), frame())
        (tmp_path / 'notes.txt').write_text('not an image')
        reader = FrameReader(str(tmp_path), resolution=(64, 64), loop_time=0)
        for _ in range(3):
            assert reader.read() is not None

    def test_empty_directory_is_an_error_not_endless_recursion(self, tmp_path):
        with pytest.raises(ValueError):
            FrameReader(str(tmp_path))


class TestStorageLedCodes:
    def test_drive_full_uses_the_storage_code(self):
        """Code 1 is camera/dependency failure; drive full used it too."""
        from utils.output_manager import AdvancedStatusIndicator
        indicator = AdvancedStatusIndicator(save_directory=None, status_led_pin=None)
        try:
            indicator._update_storage_indicator(0.95)
            assert indicator.error_code == 5
        finally:
            indicator.stop()
