#!/usr/bin/env python3
"""
Hailo bench probe: the plan's bench gate (docs/HAILO_PLAN.md §9) in one run.

    python tools/hailo_probe.py --model models/weeds_hailo_model \\
        [--cpu-model models/weeds.pt] [--model2 models/other_hailo_model] \\
        [--frames 300] [--camera | --images DIR] [--json out.json]

Run it in the OWL venv, with owl.service stopped (one process owns the Hailo).

  B3  chip from the card, chip from the HEF, and the raw parse-hef output
      (confirms the format utils/model_catalog.parse_hef_output reads)
  B1  the Hailo model through GreenOnGreen: fps, ms per frame, boxes, and
      agreement with the CPU build on the same frames (--cpu-model)
  B2  tracking on the Hailo model: track ids, lost tracks, tracker settings
  B4  a segmentation model: pass one as --model and read fps
  B6  model switch: a second Hailo model while the first is open, then
      after releasing it (--model2)
  B7  disk use and package versions

The OWL decides from B1/B2/B6 whether Ultralytics' Hailo backend is good
enough (plan D2). Nothing here sprays or changes the unit.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from utils import ai_capability, model_catalog  # noqa: E402

CAMERA_FPS = 30


def frames_from(args):
    """A list of BGR frames: the camera, a folder of images, or synthetic."""
    import cv2
    if args.camera:
        from picamera2 import Picamera2
        cam = Picamera2()
        cam.configure(cam.create_preview_configuration(main={'size': (1456, 1088), 'format': 'RGB888'}))
        cam.start()
        time.sleep(1.0)
        out = [cam.capture_array('main') for _ in range(args.frames)]
        cam.stop()
        cam.close()
        return out
    if args.images:
        paths = sorted(p for p in Path(args.images).iterdir() if p.suffix.lower() in ('.jpg', '.jpeg', '.png'))
        out = [cv2.imread(str(p)) for p in paths[:args.frames]]
        return [f for f in out if f is not None]
    rng = np.random.default_rng(0)
    out = []
    for i in range(args.frames):
        img = np.full((544, 728, 3), (60, 95, 125), np.uint8)
        for n in range(4):
            x, y = int(rng.integers(40, 680)), int((40 + 90 * n + 6 * i) % 520)
            cv2.ellipse(img, (x, y), (24, 18), 0, 0, 360, (60, 200, 70), -1)
        out.append(img)
    return out


def run_detector(path, frames, tracking=False):
    from utils.greenongreen import GreenOnGreen
    t0 = time.monotonic()
    gog = GreenOnGreen(model_path=str(path), confidence=0.25, tracking_enabled=tracking)
    load_s = time.monotonic() - t0
    times, boxes_per_frame, ids = [], [], set()
    for frame in frames:
        t = time.monotonic()
        _c, boxes, _centres, _img = gog.inference(frame, confidence=0.25)
        times.append((time.monotonic() - t) * 1000)
        boxes_per_frame.append(boxes)
        ids.update(gog.last_track_ids or [])
    warm = times[5:] or times
    result = {'model': str(path), 'backend': gog.backend, 'task': gog.task,
              'classes': list(gog.class_names.values())[:20], 'load_s': round(load_s, 1),
              'ms_median': round(float(np.median(warm)), 1), 'ms_max': round(float(np.max(warm)), 1),
              'fps': round(1000 / float(np.median(warm)), 1),
              'keeps_up_with_camera': 1000 / float(np.median(warm)) >= 0.8 * CAMERA_FPS,
              'boxes_mean': round(float(np.mean([len(b) for b in boxes_per_frame])), 2)}
    if tracking:
        gog.update_tracker_params('high')
        trackers = getattr(getattr(gog.model, 'predictor', None), 'trackers', []) or []
        t = trackers[0] if trackers else None
        attr = next((a for a in ('max_frames_lost', 'max_time_lost') if t is not None and hasattr(t, a)), None)
        result.update({'track_ids_seen': len(ids), 'lost_tracks_now': len(gog.get_lost_tracks()),
                       'tracker_present': t is not None,
                       'lost_frames_after_high_preset': getattr(t, attr) if attr else None})
    return gog, result, boxes_per_frame


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def agreement(hailo_boxes, cpu_boxes):
    """Share of CPU boxes the Hailo model also found (IoU >= 0.5)."""
    found = total = 0
    for hb, cb in zip(hailo_boxes, cpu_boxes):
        for c in cb:
            total += 1
            found += any(iou(c, h) >= 0.5 for h in hb)
    return round(found / total, 3) if total else None


def disk_report():
    def du(path):
        p = Path(path).expanduser()
        if not p.exists():
            return None
        out = subprocess.run(['du', '-sm', str(p)], capture_output=True, text=True).stdout.split()
        return int(out[0]) if out else None

    usage = shutil.disk_usage('/')
    versions = {}
    for pkg in ('ultralytics', 'torch', 'opencv-contrib-python', 'opencv-python', 'numpy'):
        try:
            from importlib.metadata import version
            versions[pkg] = version(pkg)
        except Exception:
            versions[pkg] = None
    return {'root_total_gb': round(usage.total / 1e9, 1), 'root_free_gb': round(usage.free / 1e9, 1),
            'venv_mb': du('~/.virtualenvs/owl'), 'models_mb': du(REPO / 'models'),
            'hailo_models_mb': du('/usr/share/hailo-models'), 'versions': versions}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--model', required=True, help='a Hailo folder (<name>_hailo_model)')
    parser.add_argument('--cpu-model', help='the same model as .pt or NCNN, for agreement')
    parser.add_argument('--model2', help='a second Hailo folder, for the switch test')
    parser.add_argument('--frames', type=int, default=300)
    parser.add_argument('--camera', action='store_true')
    parser.add_argument('--images')
    parser.add_argument('--json', help='also write the report here')
    args = parser.parse_args()
    report = {}

    print('[B3] chip and HEF')
    caps = ai_capability.detect()          # before anything opens the Hailo
    report['capability'] = caps
    hef = next(Path(args.model).glob('*.hef'), None)
    if hef is None:
        sys.exit(f'no .hef in {args.model}')
    raw = subprocess.run(['hailortcli', 'parse-hef', str(hef)], capture_output=True, text=True)
    info = model_catalog.parse_hef_output(raw.stdout + raw.stderr)
    report['hef'] = {'file': hef.name, 'arch': info.arch, 'imgsz': info.imgsz, 'nms': info.nms,
                     'num_classes': info.num_classes, 'parse_hef_output': raw.stdout[:3000]}
    print(f'  card: {caps["hailo"]["chip"]} (accelerator {caps["accelerator"]}), '
          f'HEF built for: {info.arch or "UNKNOWN - check parse_hef_output"}')
    runnable = info.arch in model_catalog.runnable_archs(caps['accelerator'])
    print(f'  this chip can run it: {runnable}')

    frames = frames_from(args)
    print(f'[B1/B2/B4] {len(frames)} frames')
    gog, hailo_result, hailo_boxes = run_detector(args.model, frames, tracking=False)
    print(f'  {hailo_result["backend"]}: {hailo_result["fps"]} fps, {hailo_result["ms_median"]} ms '
          f'(task {hailo_result["task"]}, keeps up: {hailo_result["keeps_up_with_camera"]})')
    report['hailo'] = hailo_result
    gog.close()
    del gog

    if args.cpu_model:
        cpu, cpu_result, cpu_boxes = run_detector(args.cpu_model, frames)
        cpu_result['agreement_with_hailo'] = agreement(hailo_boxes, cpu_boxes)
        print(f'  cpu: {cpu_result["fps"]} fps; Hailo found {cpu_result["agreement_with_hailo"]} of the CPU boxes')
        report['cpu'] = cpu_result
        cpu.close()

    tracked, track_result, _ = run_detector(args.model, frames, tracking=True)
    print(f'  tracking: {track_result["track_ids_seen"]} ids, tracker present {track_result["tracker_present"]}, '
          f'lost-frames after "high": {track_result["lost_frames_after_high_preset"]} (want 90)')
    report['tracking'] = track_result

    if args.model2:
        print('[B6] model switch')
        from utils.greenongreen import GreenOnGreen
        try:
            second = GreenOnGreen(model_path=args.model2)
            second.inference(frames[0])
            report['switch_without_release'] = 'loaded and ran'
            second.close()
        except Exception as e:
            report['switch_without_release'] = f'failed: {e}'
        tracked.close()
        try:
            second = GreenOnGreen(model_path=args.model2)
            second.inference(frames[0])
            report['switch_after_release'] = 'loaded and ran'
            second.close()
        except Exception as e:
            report['switch_after_release'] = f'failed: {e}'
        print(f'  without release: {report["switch_without_release"]}')
        print(f'  after release:   {report["switch_after_release"]}')
    else:
        tracked.close()

    print('[B7] disk and versions')
    report['disk'] = disk_report()
    print(f'  {json.dumps(report["disk"])}')

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=str))
        print(f'report: {args.json}')


if __name__ == '__main__':
    main()
