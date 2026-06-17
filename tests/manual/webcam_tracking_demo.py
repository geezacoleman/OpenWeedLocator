#!/usr/bin/env python
"""
Webcam tracking demo — test ByteTrack tracking on a desktop machine.

Drives GreenOnGreen with tracking enabled against a webcam (or synthetic
moving blobs) and shows tracked boxes with IDs, plus dimmed Kalman-predicted
boxes for lost tracks. owl.py itself can't run on Windows (GPIO imports),
so this exercises the same detection+tracking path standalone.

Use a COCO model (default models/yolo26n.pt) so everyday objects are
detected — drag a cup / bottle / phone through the field of view and watch
its ID stay stable. Cover it briefly to see the lost-track box keep moving.

Usage:
    python dev/webcam_tracking_demo.py                 # webcam 0, yolo26n
    python dev/webcam_tracking_demo.py --camera 1
    python dev/webcam_tracking_demo.py --conf 0.3
    python dev/webcam_tracking_demo.py --synthetic     # no webcam needed
    python dev/webcam_tracking_demo.py --synthetic --headless 20  # smoke test

Keys:  q / ESC quit   |   r reset tracker (IDs restart)
"""

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.greenongreen import GreenOnGreen


class SyntheticSource:
    """Moving green blobs on brown soil — lets the demo run without a webcam.
    Note: a COCO model won't classify these meaningfully; this mode mainly
    verifies the pipeline runs."""

    def __init__(self, w=640, h=480):
        self.w, self.h, self.i = w, h, 0

    def read(self):
        img = np.full((self.h, self.w, 3), (60, 95, 125), np.uint8)
        for n, (x, spd) in enumerate(((120, 4), (320, 3), (500, 5))):
            y = (40 + spd * self.i) % self.h
            cv2.ellipse(img, (x, y), (28, 22), 0, 0, 360, (60, 200, 70), -1)
        self.i += 1
        return True, img

    def release(self):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    parser.add_argument('--model', default=str(PROJECT_ROOT / 'models' / 'yolo26n.pt'),
                        help='Model path — use a COCO model for household objects')
    parser.add_argument('--camera', type=int, default=0, help='Webcam index')
    parser.add_argument('--conf', type=float, default=0.4,
                        help='Detection confidence threshold')
    parser.add_argument('--synthetic', action='store_true',
                        help='Use synthetic moving blobs instead of a webcam')
    parser.add_argument('--headless', type=int, default=0, metavar='N',
                        help='Run N frames without a window (smoke test)')
    args = parser.parse_args()

    print(f'Loading {args.model} with tracking enabled...')
    gog = GreenOnGreen(
        model_path=args.model,
        confidence=args.conf,
        tracking_enabled=True,
    )
    print(f'Model classes: {list(gog.class_names.values())[:15]}...')

    if args.synthetic:
        cap = SyntheticSource()
    else:
        cap = cv2.VideoCapture(args.camera)
        if not cap.isOpened():
            print(f'ERROR: cannot open webcam {args.camera} — '
                  f'try --camera 1 or --synthetic')
            sys.exit(1)

    print('Tracking — drag an object (cup/bottle/phone) through the view. '
          "'q' quits, 'r' resets the tracker.")
    frame_times = []
    n = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print('Camera read failed.')
                break

            t0 = time.perf_counter()
            _, boxes, centres, img_out = gog.inference(
                frame, confidence=args.conf, show_display=True)
            frame_times.append((time.perf_counter() - t0) * 1000)

            # Dimmed boxes for lost tracks (Kalman-predicted positions)
            for lt in gog.get_lost_tracks(max_age=30):
                x1, y1, x2, y2 = map(int, lt['xyxy'])
                cv2.rectangle(img_out, (x1, y1), (x2, y2), (0, 130, 130), 1)
                cv2.putText(img_out, f"ID{lt['track_id']} lost[{lt['age']}]",
                            (x1, max(14, y1 - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 130, 130), 1)

            ms = np.median(frame_times[-30:])
            cv2.putText(img_out,
                        f'{ms:.0f} ms/frame | {len(boxes)} tracked | '
                        f'{len(gog.get_lost_tracks(max_age=30))} lost',
                        (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        (255, 255, 255), 2)

            n += 1
            if args.headless:
                if n >= args.headless:
                    break
                continue

            cv2.imshow('OWL tracking demo', img_out)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord('q'), 27):
                break
            if key == ord('r'):
                gog.reset_tracker()
                print('Tracker reset — IDs restart from 1.')
    finally:
        cap.release()
        if not args.headless:
            cv2.destroyAllWindows()

    print(f'\n{n} frames | median {np.median(frame_times):.1f} ms/frame '
          f'({1000 / max(np.median(frame_times), 1e-6):.1f} fps)')


if __name__ == '__main__':
    main()
