#!/usr/bin/env python
"""
Raspberry Pi tracking smoke test — run ON THE OWL Pi before field deployment.

Verifies the parts of the tracking stack that cannot be tested off-device:
  1. `lap` is installed (without it, ultralytics attempts a runtime
     pip install on the first tracked frame — which fails offline).
  2. The NCNN/PyTorch model in models/ works with model.track().
  3. The tracker yaml resolves regardless of cwd.
  4. [Tracking] INI params reach the live ByteTrack.
  5. Per-frame latency with tracking enabled is acceptable.

Usage (in the owl virtualenv):
    workon owl
    python tests/pi_tracking_smoke.py [--model models] [--frames 20]

Exit code 0 = all checks passed.
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def fail(msg):
    print(f'  FAIL: {msg}')
    sys.exit(1)


def make_frame(i, w=416, h=320):
    """Brown background with a couple of moving green blobs."""
    import cv2
    img = np.full((h, w, 3), (60, 95, 125), np.uint8)
    for n, (x, spd) in enumerate(((80, 6), (260, 5))):
        y = 30 + spd * i
        cv2.ellipse(img, (x, y % h), (22, 18), 0, 0, 360, (60, 200, 70), -1)
    return img


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default=str(PROJECT_ROOT / 'models'))
    parser.add_argument('--frames', type=int, default=20)
    args = parser.parse_args()

    print('[1/5] lap installed (no runtime pip install needed)...')
    try:
        import lap
        print(f'  OK: lap {lap.__version__}')
    except ImportError:
        fail('lap not installed — run: pip install "lap>=0.5.12" '
             '(or pip install -r requirements-gog.txt)')

    print('[2/5] ultralytics + tracker yaml...')
    try:
        import ultralytics
        from utils.greenongreen import GreenOnGreen, TRACKER_YAML
        print(f'  OK: ultralytics {ultralytics.__version__}')
    except ImportError as e:
        fail(f'import failed: {e}')
    if not Path(TRACKER_YAML).exists():
        fail(f'tracker yaml missing: {TRACKER_YAML}')
    print(f'  OK: {TRACKER_YAML}')

    print('[3/5] loading model with tracking enabled...')
    try:
        gog = GreenOnGreen(
            model_path=args.model,
            confidence=0.3,
            tracking_enabled=True,
            tracker_params={'match_thresh': 0.8, 'track_buffer': 60},
        )
        print(f'  OK: {gog._model_filename} (task={gog.task})')
    except Exception as e:
        fail(f'model load failed: {e}')

    print(f'[4/5] running model.track() on {args.frames} frames...')
    times = []
    try:
        for i in range(args.frames):
            t0 = time.perf_counter()
            contours, boxes, centres, _ = gog.inference(make_frame(i))
            times.append((time.perf_counter() - t0) * 1000)
    except Exception as e:
        fail(f'tracked inference crashed on frame {len(times)}: {e}')
    med = float(np.median(times))
    print(f'  OK: median {med:.1f} ms/frame '
          f'(min {min(times):.1f}, max {max(times):.1f})')
    if med > 500:
        print('  WARNING: very slow — check NCNN model is being used, '
              'not a .pt file on CPU')

    print('[5/5] INI params applied to live ByteTrack...')
    trackers = getattr(gog.model.predictor, 'trackers', [])
    if not trackers:
        fail('no trackers created — model.track() path not active')
    t = trackers[0]
    if t.args.match_thresh != 0.8 or t.max_time_lost != 60:
        fail(f'params not applied: match_thresh={t.args.match_thresh}, '
             f'max_time_lost={t.max_time_lost}')
    print('  OK: tracker.args updated, lost-track API returns '
          f'{len(gog.get_lost_tracks())} lost tracks')

    print('\nALL CHECKS PASSED — tracking is field-ready on this device.')


if __name__ == '__main__':
    main()
