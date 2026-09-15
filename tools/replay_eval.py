#!/usr/bin/env python3
"""Replay recorded frames through the real detection path and record what it did.

Why this exists
---------------
Every detection test in the suite draws green blocks on flat brown and asserts
a count bound. Nothing measures whether OWL finds weeds in a real paddock, and
the benchmarks measure speed only. This gives you the missing layer: run a
fixed set of frames through the shipped detector and lane maths, write the
result to JSON, and diff the next run against it.

It needs no hardware. `utils/frame_reader.FrameReader` already accepts a
directory, a single image or a video, and `owl_data/` is full of real sessions.

Two modes
---------
  baseline   run and write a reference file
  compare    run and diff against a reference, exit non-zero on drift

    python tools/replay_eval.py baseline owl_data/20260210 -o eval/day1.json
    python tools/replay_eval.py compare  owl_data/20260210 -b eval/day1.json

Add --config to evaluate a specific profile, --algorithm to override it, and
--labels to score against hand-marked boxes (see score_labels() below).

What it reports, per frame: the detections found, their boxes and centres, the
relays those centres would fire, and the processing time. Aggregate: detection
count, relay histogram, and timing percentiles. Everything that determines
where spray lands, and nothing that needs a tractor.
"""

import argparse
import configparser
import json
import sys
import time
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.frame_reader import FrameReader                # noqa: E402
from utils.geometry import compute_geometry, assign_lanes  # noqa: E402
from utils.greenonbrown import GreenOnBrown               # noqa: E402


def load_settings(config_path, algorithm_override=None):
    """Detection settings straight from an OWL config, not a hand-written dict."""
    config = configparser.ConfigParser(inline_comment_prefixes=None)
    if not config.read(config_path):
        raise SystemExit(f'Could not read config: {config_path}')

    gob = 'GreenOnBrown'
    settings = {
        'algorithm': algorithm_override or config.get('System', 'algorithm',
                                                      fallback='exhsv'),
        'exg_min': config.getint(gob, 'exg_min', fallback=25),
        'exg_max': config.getint(gob, 'exg_max', fallback=200),
        'hue_min': config.getint(gob, 'hue_min', fallback=30),
        'hue_max': config.getint(gob, 'hue_max', fallback=90),
        'saturation_min': config.getint(gob, 'saturation_min', fallback=30),
        'saturation_max': config.getint(gob, 'saturation_max', fallback=255),
        'brightness_min': config.getint(gob, 'brightness_min', fallback=10),
        'brightness_max': config.getint(gob, 'brightness_max', fallback=220),
        'invert_hue': config.getboolean(gob, 'invert_hue', fallback=False),
        'min_detection_area_percent': config.getfloat(
            gob, 'min_detection_area_percent', fallback=0.01),
        'relay_num': config.getint('System', 'relay_num', fallback=4),
        'crop_left': config.getfloat('Camera', 'crop_left', fallback=0.0),
        'crop_right': config.getfloat('Camera', 'crop_right', fallback=0.0),
        'crop_top': config.getfloat('Camera', 'crop_top', fallback=0.0),
        'crop_bottom': config.getfloat('Camera', 'crop_bottom', fallback=0.0),
        'actuation_top': config.getfloat('System', 'actuation_top', fallback=0.0),
        'actuation_bottom': config.getfloat('System', 'actuation_bottom', fallback=1.0),
    }
    return settings


def replay(source, settings, limit=None):
    """Run every frame through the production detector and lane assignment."""
    detector = GreenOnBrown(algorithm=settings['algorithm'])
    reader = FrameReader(path=str(source), loop_time=0)

    frames, geometry = [], None
    while True:
        frame = reader.read()
        if frame is None:
            break
        if limit is not None and len(frames) >= limit:
            break

        if geometry is None:
            h, w = frame.shape[:2]
            geometry = compute_geometry(
                frame_w=w, frame_h=h,
                crop_left=settings['crop_left'], crop_right=settings['crop_right'],
                crop_top=settings['crop_top'], crop_bottom=settings['crop_bottom'],
                relay_num=settings['relay_num'],
                actuation_top=settings['actuation_top'],
                actuation_bottom=settings['actuation_bottom'])

        cropped = frame[geometry['crop_slice']]
        min_area = max(1, int(round(
            settings['min_detection_area_percent'] / 100.0
            * geometry['cropped_width'] * geometry['cropped_height'])))

        start = time.perf_counter()
        _, boxes, centres, _ = detector.inference(
            cropped,
            exg_min=settings['exg_min'], exg_max=settings['exg_max'],
            hue_min=settings['hue_min'], hue_max=settings['hue_max'],
            saturation_min=settings['saturation_min'],
            saturation_max=settings['saturation_max'],
            brightness_min=settings['brightness_min'],
            brightness_max=settings['brightness_max'],
            show_display=False, algorithm=settings['algorithm'],
            min_detection_area=min_area, invert_hue=settings['invert_hue'])
        elapsed_ms = (time.perf_counter() - start) * 1000

        fired = assign_lanes(centres, geometry['lane_width'],
                             settings['relay_num'],
                             geometry['y_top'], geometry['y_bottom'])

        frames.append({
            'index': len(frames),
            'detections': len(boxes),
            'boxes': [[int(v) for v in b] for b in boxes],
            'centres': [[int(c[0]), int(c[1])] for c in centres],
            'relays': sorted(int(r) for r in fired),
            'ms': round(elapsed_ms, 3),
        })

    reader.stop()
    return frames, geometry


def summarise(frames, geometry, settings):
    times = np.array([f['ms'] for f in frames]) if frames else np.array([0.0])
    relay_hits = {}
    for f in frames:
        for r in f['relays']:
            relay_hits[r] = relay_hits.get(r, 0) + 1
    return {
        'frames': len(frames),
        'frames_with_detection': sum(1 for f in frames if f['detections']),
        'total_detections': sum(f['detections'] for f in frames),
        'relay_hits': {str(k): relay_hits[k] for k in sorted(relay_hits)},
        'ms_mean': round(float(times.mean()), 3),
        'ms_p95': round(float(np.percentile(times, 95)), 3),
        'ms_max': round(float(times.max()), 3),
        'settings': settings,
        'geometry': {
            'cropped_width': geometry['cropped_width'] if geometry else None,
            'cropped_height': geometry['cropped_height'] if geometry else None,
            'lane_width': geometry['lane_width'] if geometry else None,
            'y_top': geometry['y_top'] if geometry else None,
            'y_bottom': geometry['y_bottom'] if geometry else None,
        },
    }


def compare(current, baseline):
    """Frame-by-frame diff. Detection counts and relay choices must not move."""
    drift = []
    base_frames = {f['index']: f for f in baseline['frames']}
    for f in current['frames']:
        b = base_frames.get(f['index'])
        if b is None:
            drift.append(f"frame {f['index']}: not in baseline")
            continue
        if f['detections'] != b['detections']:
            drift.append(f"frame {f['index']}: detections "
                         f"{b['detections']} -> {f['detections']}")
        if f['relays'] != b['relays']:
            drift.append(f"frame {f['index']}: relays "
                         f"{b['relays']} -> {f['relays']}")
    missing = set(base_frames) - {f['index'] for f in current['frames']}
    for i in sorted(missing):
        drift.append(f"frame {i}: missing from this run")
    return drift


def score_labels(frames, labels_path, iou_threshold=0.5):
    """Recall/precision against hand-marked boxes.

    labels.json: {"<frame index>": [[x, y, w, h], ...], ...} in CROPPED-frame
    pixels. Twenty to fifty labelled frames is enough to turn this from a
    regression check into a real accuracy measurement.
    """
    labels = json.loads(Path(labels_path).read_text())

    def iou(a, b):
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
        iy = max(0, min(ay + ah, by + bh) - max(ay, by))
        inter = ix * iy
        union = aw * ah + bw * bh - inter
        return inter / union if union else 0.0

    tp = fp = fn = 0
    for f in frames:
        truth = labels.get(str(f['index']), [])
        unmatched = list(truth)
        for box in f['boxes']:
            hit = next((t for t in unmatched if iou(box, t) >= iou_threshold), None)
            if hit is not None:
                unmatched.remove(hit)
                tp += 1
            else:
                fp += 1
        fn += len(unmatched)

    recall = tp / (tp + fn) if (tp + fn) else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    return {'true_positives': tp, 'false_positives': fp, 'false_negatives': fn,
            'recall': round(recall, 4), 'precision': round(precision, 4),
            'labelled_frames': len(labels)}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', choices=['baseline', 'compare'])
    ap.add_argument('source', help='image directory, single image or video')
    ap.add_argument('-o', '--out', help='where to write the result JSON')
    ap.add_argument('-b', '--baseline', help='reference JSON for compare mode')
    ap.add_argument('-c', '--config', default='config/GENERAL_CONFIG.ini')
    ap.add_argument('-a', '--algorithm', help='override the config algorithm')
    ap.add_argument('-n', '--limit', type=int, help='stop after N frames')
    ap.add_argument('--labels', help='labels JSON for recall/precision scoring')
    args = ap.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    settings = load_settings(str(config_path), args.algorithm)

    print(f"Replaying {args.source} with algorithm '{settings['algorithm']}'...")
    frames, geometry = replay(args.source, settings, args.limit)
    if not frames:
        print('No frames read - check the path.')
        return 1

    result = summarise(frames, geometry, settings)
    result['frames'] = frames
    summary = {k: v for k, v in result.items() if k != 'frames'}

    print(f"  {summary['frames_with_detection']}/{len(frames)} frames with a detection, "
          f"{summary['total_detections']} detections total")
    print(f"  relay hits: {summary['relay_hits']}")
    print(f"  {summary['ms_mean']} ms mean, {summary['ms_p95']} ms p95, "
          f"{summary['ms_max']} ms max")

    if args.labels:
        score = score_labels(frames, args.labels)
        result['score'] = score
        print(f"  recall {score['recall']}, precision {score['precision']} "
              f"over {score['labelled_frames']} labelled frames")

    if args.mode == 'baseline':
        out = Path(args.out or 'replay_baseline.json')
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2))
        print(f"Baseline written to {out}")
        return 0

    if not args.baseline:
        print('compare mode needs --baseline')
        return 2
    baseline = json.loads(Path(args.baseline).read_text())
    drift = compare(result, baseline)
    if drift:
        print(f"\nDRIFT vs baseline ({len(drift)} differences):")
        for line in drift[:40]:
            print(f"  {line}")
        if len(drift) > 40:
            print(f"  ... and {len(drift) - 40} more")
        return 1
    print('\nNo drift: detections and relay choices match the baseline.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
