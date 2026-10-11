"""Helpers for tests that build a real Owl and run real frames through hoot().

Fixtures that use these (owl_factory, config_factory, input_dir) live in
tests/owl/conftest.py. Import from a test with `from harness import ...`.
"""

import time
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = PROJECT_ROOT / 'config' / 'GENERAL_CONFIG.ini'


def make_frame(width=416, height=320):
    """A brown frame with a green blob — reliably detected by exhsv."""
    frame = np.full((height, width, 3), (40, 60, 90), dtype=np.uint8)
    cv2.rectangle(frame, (width // 2 - 40, height // 2 - 40),
                  (width // 2 + 40, height // 2 + 40), (60, 180, 60), -1)
    return frame


class FakeCamera:
    """Yields a fixed number of frames, then None so hoot() shuts down."""

    def __init__(self, frames=6, width=416, height=320):
        self.remaining = frames
        self.frame = make_frame(width, height)
        self.stopped = False

    def read_with_metadata(self):
        if self.remaining <= 0:
            return None, None
        self.remaining -= 1
        return self.frame.copy(), None

    def read(self):
        return self.read_with_metadata()[0]

    def stop(self):
        self.stopped = True


def run_hoot(instance, frames=6):
    """Run the detection loop over `frames` frames and return relay firings."""
    instance.cam = FakeCamera(frames=frames)

    fired = []
    original_receive = instance.relay_controller.receive

    def record(**kwargs):
        # called_at lets tests check the window against the moment the relay
        # controller was actually asked to spray, not just the arguments
        fired.append(dict(kwargs, called_at=time.monotonic()))
        return original_receive(**kwargs)

    instance.relay_controller.receive = record

    try:
        instance.hoot()
    except SystemExit:
        pass  # hoot() -> stop() -> sys.exit(0) on the None frame
    return fired
