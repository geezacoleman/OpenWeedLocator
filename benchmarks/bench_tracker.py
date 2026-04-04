#!/usr/bin/env python
"""
Benchmark: Tracking overhead for weed detection pipeline.

Measures the cost of adding ByteTrack + ClassSmoother + CropMaskStabilizer
to the GoG/GoG-hybrid detection loop. Operates on synthetic detection outputs
(no images, no YOLO inference) to isolate tracking overhead.

Usage:
    python benchmarks/bench_tracker.py
    python benchmarks/bench_tracker.py --objects 40 --frames 500
"""

import argparse
import json
import os
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass
from types import SimpleNamespace

import cv2
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from ultralytics.trackers.byte_tracker import BYTETracker, STrack
from ultralytics.trackers.utils.matching import iou_distance


# ============================================================
# Synthetic detection generator
# ============================================================

class SyntheticScene:
    """Generate realistic detection sequences with configurable noise.

    Objects move linearly top-to-bottom (simulating tractor movement).
    Noise models: random detection drops, class flicker, burst dropout.
    """

    def __init__(self, num_objects=10, frame_width=640, frame_height=480,
                 speed_px_per_frame=15, box_size_range=(30, 80),
                 weed_fraction=0.5, seed=42):
        self.rng = np.random.RandomState(seed)
        self.frame_width = frame_width
        self.frame_height = frame_height

        self.objects = []
        for i in range(num_objects):
            w = self.rng.randint(*box_size_range)
            h = self.rng.randint(*box_size_range)
            x = self.rng.randint(10, frame_width - w - 10)
            y = self.rng.randint(-frame_height, 0)  # start above frame
            speed = speed_px_per_frame + self.rng.uniform(-3, 3)
            is_weed = i < int(num_objects * weed_fraction)
            cls_id = 0 if is_weed else 1  # 0=weed, 1=crop
            conf = 0.6 + self.rng.uniform(0, 0.35)
            self.objects.append({
                'id': i, 'x': float(x), 'y': float(y),
                'w': w, 'h': h, 'speed': speed,
                'class_id': cls_id, 'confidence': conf,
            })

    def generate_frame(self, frame_idx, drop_rate=0.0, class_flicker_rate=0.0,
                       burst_drop_ids=None):
        """Generate one frame of detections with noise applied."""
        boxes = []
        class_ids = []
        confidences = []
        ground_truth = []

        for obj in self.objects:
            # Update position
            y = obj['y'] + obj['speed'] * frame_idx

            # Skip if outside frame
            if y + obj['h'] < 0 or y > self.frame_height:
                continue

            # Clamp to frame
            y_clamped = max(0, min(y, self.frame_height - obj['h']))
            x = obj['x']

            gt_entry = {
                'id': obj['id'], 'class_id': obj['class_id'],
                'box': [x, y_clamped, obj['w'], obj['h']],
            }
            ground_truth.append(gt_entry)

            # Apply burst dropout
            if burst_drop_ids and obj['id'] in burst_drop_ids:
                continue

            # Apply random drop
            if self.rng.random() < drop_rate:
                continue

            # Apply class flicker
            cls = obj['class_id']
            if self.rng.random() < class_flicker_rate:
                cls = 1 - cls  # flip 0↔1

            # Add noise to confidence
            conf = obj['confidence'] + self.rng.uniform(-0.1, 0.1)
            conf = max(0.1, min(0.99, conf))

            boxes.append([x, y_clamped, obj['w'], obj['h']])
            class_ids.append(cls)
            confidences.append(conf)

        return boxes, class_ids, confidences, ground_truth


# ============================================================
# ClassSmoother prototype (what we'd build in utils/tracker.py)
# ============================================================

class ClassSmoother:
    """Majority-vote class assignment per tracked object."""

    def __init__(self, window=5):
        self.window = window
        self.history = {}  # {track_id: deque of (class_id, confidence)}

    def update(self, track_ids, class_ids, confidences):
        """Record observations, return smoothed class per track_id."""
        seen = set()
        for tid, cls, conf in zip(track_ids, class_ids, confidences):
            tid = int(tid)
            seen.add(tid)
            if tid not in self.history:
                self.history[tid] = deque(maxlen=self.window)
            self.history[tid].append((cls, conf))

        # Prune stale tracks
        stale = [k for k in self.history if k not in seen]
        for k in stale:
            # Keep for a bit in case track is just lost temporarily
            pass  # ByteTrack handles this via lost_stracks

        return {int(tid): self._majority(int(tid)) for tid in seen}

    def _majority(self, track_id):
        hist = self.history.get(track_id, [])
        if not hist:
            return -1
        # Simple majority vote
        counts = Counter(cls for cls, _ in hist)
        return counts.most_common(1)[0][0]

    def reset(self):
        self.history.clear()


# ============================================================
# CropMaskStabilizer prototype
# ============================================================

class CropMaskStabilizer:
    """Persist crop positions for stable hybrid-mode masking."""

    def __init__(self, max_age=3):
        self.max_age = max_age
        self.tracks = {}  # {track_id: {'box': [...], 'age': int}}

    def update(self, track_ids, boxes, frame_count=0):
        """Update with current crop detections."""
        seen = set()
        for tid, box in zip(track_ids, boxes):
            tid = int(tid)
            seen.add(tid)
            self.tracks[tid] = {'box': box, 'age': 0}

        # Age unseen tracks
        to_remove = []
        for tid in self.tracks:
            if tid not in seen:
                self.tracks[tid]['age'] += 1
                if self.tracks[tid]['age'] > self.max_age:
                    to_remove.append(tid)
        for tid in to_remove:
            del self.tracks[tid]

    def build_stabilized_mask(self, shape):
        """Build crop mask including persisted positions."""
        mask = np.zeros(shape[:2], dtype=np.uint8)
        for tid, info in self.tracks.items():
            x, y, w, h = [int(v) for v in info['box']]
            x2, y2 = min(x + w, shape[1]), min(y + h, shape[0])
            x, y = max(0, x), max(0, y)
            if x2 > x and y2 > y:
                mask[y:y2, x:x2] = 255
        return mask

    def reset(self):
        self.tracks.clear()


# ============================================================
# ByteTrack wrapper (simulates model.track() overhead)
# ============================================================

def make_bytetracker(frame_rate=30, track_buffer=30, track_high_thresh=0.25,
                     track_low_thresh=0.1, new_track_thresh=0.25,
                     match_thresh=0.8, fuse_score=True):
    """Create a BYTETracker with OWL-tuned defaults."""
    args = SimpleNamespace(
        track_high_thresh=track_high_thresh,
        track_low_thresh=track_low_thresh,
        new_track_thresh=new_track_thresh,
        track_buffer=track_buffer,
        match_thresh=match_thresh,
        fuse_score=fuse_score,
    )
    return BYTETracker(args, frame_rate=frame_rate)


class MockResults:
    """Mock the Ultralytics Results interface for BYTETracker.update().

    BYTETracker.update() accesses: results.conf, results.cls, results.xywh,
    results.xyxy, results[bool_mask], and len(results).
    """

    def __init__(self, xywh, conf, cls, xyxy):
        self.xywh = xywh
        self.conf = conf
        self.cls = cls
        self.xyxy = xyxy

    def __len__(self):
        return len(self.conf)

    def __getitem__(self, mask):
        return MockResults(
            xywh=self.xywh[mask],
            conf=self.conf[mask],
            cls=self.cls[mask],
            xyxy=self.xyxy[mask],
        )


def boxes_to_results_format(boxes, class_ids, confidences):
    """Convert detection lists to MockResults for BYTETracker.update()."""
    if not boxes:
        return MockResults(
            xywh=np.empty((0, 4), dtype=np.float32),
            conf=np.empty((0,), dtype=np.float32),
            cls=np.empty((0,), dtype=np.float32),
            xyxy=np.empty((0, 4), dtype=np.float32),
        )

    xyxy = np.array([[x, y, x + w, y + h] for x, y, w, h in boxes], dtype=np.float32)
    xywh = np.array([[x + w / 2, y + h / 2, w, h] for x, y, w, h in boxes], dtype=np.float32)
    conf = np.array(confidences, dtype=np.float32)
    cls = np.array(class_ids, dtype=np.float32)

    return MockResults(xywh=xywh, conf=conf, cls=cls, xyxy=xyxy)


# ============================================================
# Timing utility
# ============================================================

def timeit_ms(func, rounds=200, warmup=20):
    """Time a function, return stats in ms."""
    for _ in range(warmup):
        func()
    times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        func()
        times.append((time.perf_counter() - t0) * 1000)
    return {
        'median': float(np.median(times)),
        'mean': float(np.mean(times)),
        'p95': float(np.percentile(times, 95)),
        'p99': float(np.percentile(times, 99)),
        'min': float(min(times)),
        'max': float(max(times)),
    }


# ============================================================
# Benchmarks
# ============================================================

def bench_bytetracker_update(num_objects_list, num_frames=200):
    """Benchmark BYTETracker.update() for various detection counts."""
    print('\n=== ByteTracker.update() Latency ===')
    print(f'{"Objects":>8} {"Median":>8} {"Mean":>8} {"P95":>8} {"P99":>8} {"Min":>8} {"Max":>8}')
    print('-' * 62)

    results = {}
    for n_obj in num_objects_list:
        scene = SyntheticScene(num_objects=n_obj, speed_px_per_frame=10)
        tracker = make_bytetracker()

        # Pre-generate all frames
        frames = []
        for f in range(num_frames):
            boxes, cls_ids, confs, _ = scene.generate_frame(f, drop_rate=0.1)
            frames.append(boxes_to_results_format(boxes, cls_ids, confs))

        frame_idx = [0]

        def run_update():
            idx = frame_idx[0] % len(frames)
            # Reset tracker periodically to prevent unbounded state
            if idx == 0:
                tracker.reset()
                STrack.reset_id()
            tracker.update(frames[idx], None)
            frame_idx[0] += 1

        stats = timeit_ms(run_update, rounds=num_frames, warmup=min(20, num_frames // 2))
        results[n_obj] = stats
        print(f'{n_obj:>8} {stats["median"]:>7.2f}ms {stats["mean"]:>7.2f}ms '
              f'{stats["p95"]:>7.2f}ms {stats["p99"]:>7.2f}ms '
              f'{stats["min"]:>7.2f}ms {stats["max"]:>7.2f}ms')

    return results


def bench_class_smoother(num_objects_list, window=5):
    """Benchmark ClassSmoother.update() for various track counts."""
    print('\n=== ClassSmoother.update() Latency ===')
    print(f'{"Tracks":>8} {"Median":>8} {"Mean":>8} {"P95":>8} {"P99":>8}')
    print('-' * 42)

    results = {}
    for n_obj in num_objects_list:
        smoother = ClassSmoother(window=window)
        track_ids = list(range(n_obj))
        class_ids = [0 if i % 2 == 0 else 1 for i in range(n_obj)]
        confs = [0.8] * n_obj

        def run_update():
            smoother.update(track_ids, class_ids, confs)

        stats = timeit_ms(run_update)
        results[n_obj] = stats
        print(f'{n_obj:>8} {stats["median"]:>7.3f}ms {stats["mean"]:>7.3f}ms '
              f'{stats["p95"]:>7.3f}ms {stats["p99"]:>7.3f}ms')

    return results


def bench_crop_mask_stabilizer(num_crops_list, frame_shape=(480, 640)):
    """Benchmark CropMaskStabilizer.build_stabilized_mask() for various crop counts."""
    print('\n=== CropMaskStabilizer.build_stabilized_mask() Latency ===')
    print(f'{"Crops":>8} {"Median":>8} {"Mean":>8} {"P95":>8} {"P99":>8}')
    print('-' * 42)

    results = {}
    for n_crops in num_crops_list:
        stabilizer = CropMaskStabilizer(max_age=3)
        rng = np.random.RandomState(42)

        # Populate with tracks
        track_ids = list(range(n_crops))
        boxes = [[rng.randint(10, 500), rng.randint(10, 350),
                  rng.randint(40, 100), rng.randint(40, 100)]
                 for _ in range(n_crops)]
        stabilizer.update(track_ids, boxes)

        def run_mask():
            stabilizer.build_stabilized_mask(frame_shape)

        stats = timeit_ms(run_mask)
        results[n_crops] = stats
        print(f'{n_crops:>8} {stats["median"]:>7.3f}ms {stats["mean"]:>7.3f}ms '
              f'{stats["p95"]:>7.3f}ms {stats["p99"]:>7.3f}ms')

    return results


def bench_full_pipeline(num_objects=10, num_frames=300, drop_rate=0.1,
                        class_flicker_rate=0.1):
    """Benchmark the complete tracking pipeline: ByteTrack + ClassSmoother + mask."""
    print(f'\n=== Full Pipeline ({num_objects} objects, {drop_rate:.0%} drop, '
          f'{class_flicker_rate:.0%} class flicker) ===')

    scene = SyntheticScene(num_objects=num_objects, speed_px_per_frame=12)
    tracker = make_bytetracker()
    smoother = ClassSmoother(window=5)
    stabilizer = CropMaskStabilizer(max_age=3)

    bt_times = []
    sm_times = []
    ms_times = []
    total_times = []

    for f in range(num_frames):
        boxes, cls_ids, confs, gt = scene.generate_frame(
            f, drop_rate=drop_rate, class_flicker_rate=class_flicker_rate
        )
        mock_results = boxes_to_results_format(boxes, cls_ids, confs)

        t_total_start = time.perf_counter()

        # ByteTrack
        t0 = time.perf_counter()
        output = tracker.update(mock_results, None)
        bt_times.append((time.perf_counter() - t0) * 1000)

        # Extract track info from output
        if len(output) > 0:
            track_ids = output[:, 4].astype(int).tolist()
            track_cls = output[:, 6].astype(int).tolist()
            track_conf = output[:, 5].tolist()
            track_boxes = output[:, :4].tolist()  # xyxy
        else:
            track_ids, track_cls, track_conf, track_boxes = [], [], [], []

        # ClassSmoother
        t0 = time.perf_counter()
        smoothed = smoother.update(track_ids, track_cls, track_conf)
        sm_times.append((time.perf_counter() - t0) * 1000)

        # CropMaskStabilizer (for crop detections)
        crop_ids = [tid for tid, cls in zip(track_ids, track_cls) if cls == 1]
        crop_boxes_xyxy = [b for b, cls in zip(track_boxes, track_cls) if cls == 1]
        # Convert xyxy to xywh for stabilizer
        crop_boxes = [[x1, y1, x2 - x1, y2 - y1]
                      for x1, y1, x2, y2 in crop_boxes_xyxy]

        t0 = time.perf_counter()
        stabilizer.update(crop_ids, crop_boxes)
        mask = stabilizer.build_stabilized_mask((480, 640))
        ms_times.append((time.perf_counter() - t0) * 1000)

        total_times.append((time.perf_counter() - t_total_start) * 1000)

    def stats(times):
        return {
            'median': float(np.median(times)),
            'mean': float(np.mean(times)),
            'p95': float(np.percentile(times, 95)),
            'p99': float(np.percentile(times, 99)),
        }

    bt_s = stats(bt_times)
    sm_s = stats(sm_times)
    ms_s = stats(ms_times)
    tot_s = stats(total_times)

    print(f'  {"Component":30s} {"Median":>8} {"Mean":>8} {"P95":>8} {"P99":>8}')
    print(f'  {"-"*30} {"-"*8} {"-"*8} {"-"*8} {"-"*8}')
    print(f'  {"ByteTracker.update()":30s} {bt_s["median"]:>7.3f}ms {bt_s["mean"]:>7.3f}ms '
          f'{bt_s["p95"]:>7.3f}ms {bt_s["p99"]:>7.3f}ms')
    print(f'  {"ClassSmoother.update()":30s} {sm_s["median"]:>7.3f}ms {sm_s["mean"]:>7.3f}ms '
          f'{sm_s["p95"]:>7.3f}ms {sm_s["p99"]:>7.3f}ms')
    print(f'  {"CropMaskStabilizer (mask)":30s} {ms_s["median"]:>7.3f}ms {ms_s["mean"]:>7.3f}ms '
          f'{ms_s["p95"]:>7.3f}ms {ms_s["p99"]:>7.3f}ms')
    print(f'  {"TOTAL":30s} {tot_s["median"]:>7.3f}ms {tot_s["mean"]:>7.3f}ms '
          f'{tot_s["p95"]:>7.3f}ms {tot_s["p99"]:>7.3f}ms')

    return {'bytetrack': bt_s, 'smoother': sm_s, 'mask_stabilizer': ms_s, 'total': tot_s}


def bench_accuracy(num_objects=10, num_frames=200, drop_rate=0.1,
                   class_flicker_rate=0.15):
    """Measure tracking accuracy: class stability and crop mask coverage."""
    print(f'\n=== Accuracy ({num_objects} obj, {drop_rate:.0%} drop, '
          f'{class_flicker_rate:.0%} flicker, {num_frames} frames) ===')

    scene = SyntheticScene(num_objects=num_objects, speed_px_per_frame=12)
    tracker = make_bytetracker()
    smoother = ClassSmoother(window=5)

    # Track accuracy metrics
    raw_class_correct = 0
    smoothed_class_correct = 0
    total_observations = 0

    for f in range(num_frames):
        boxes, cls_ids, confs, gt = scene.generate_frame(
            f, drop_rate=drop_rate, class_flicker_rate=class_flicker_rate
        )
        mock_results = boxes_to_results_format(boxes, cls_ids, confs)
        output = tracker.update(mock_results, None)

        if len(output) > 0:
            track_ids = output[:, 4].astype(int).tolist()
            raw_cls = output[:, 6].astype(int).tolist()
            track_conf = output[:, 5].tolist()

            smoothed = smoother.update(track_ids, raw_cls, track_conf)

            # Compare against ground truth
            gt_boxes = {g['id']: g for g in gt}
            for tid, raw_c in zip(track_ids, raw_cls):
                # Find closest GT object (by position overlap)
                # For this benchmark we use track_id mapping heuristic
                total_observations += 1
                # Raw class from ByteTrack (no smoothing)
                # Smoothed class from our wrapper
                smoothed_c = smoothed.get(tid, raw_c)

                # We can't directly map track_id→gt_id without IoU matching,
                # so we measure class consistency instead
                raw_class_correct += 1  # raw is whatever detector says
                smoothed_class_correct += 1  # placeholder

    # More meaningful metric: class stability (how often does a track's class change?)
    tracker2 = make_bytetracker()
    smoother2 = ClassSmoother(window=5)

    raw_flips = 0
    smoothed_flips = 0
    raw_last = {}
    smoothed_last = {}
    total_tracked = 0

    for f in range(num_frames):
        boxes, cls_ids, confs, gt = scene.generate_frame(
            f, drop_rate=drop_rate, class_flicker_rate=class_flicker_rate
        )
        mock_results = boxes_to_results_format(boxes, cls_ids, confs)
        output = tracker2.update(mock_results, None)

        if len(output) > 0:
            track_ids = output[:, 4].astype(int).tolist()
            raw_cls = output[:, 6].astype(int).tolist()
            track_conf = output[:, 5].tolist()

            smoothed = smoother2.update(track_ids, raw_cls, track_conf)

            for tid, raw_c in zip(track_ids, raw_cls):
                total_tracked += 1
                sm_c = smoothed.get(tid, raw_c)

                if tid in raw_last and raw_last[tid] != raw_c:
                    raw_flips += 1
                if tid in smoothed_last and smoothed_last[tid] != sm_c:
                    smoothed_flips += 1

                raw_last[tid] = raw_c
                smoothed_last[tid] = sm_c

    raw_stability = 1.0 - (raw_flips / max(total_tracked, 1))
    smoothed_stability = 1.0 - (smoothed_flips / max(total_tracked, 1))

    print(f'  Total track observations: {total_tracked}')
    print(f'  Raw class flips:          {raw_flips} ({raw_flips/max(total_tracked,1):.1%} of observations)')
    print(f'  Smoothed class flips:     {smoothed_flips} ({smoothed_flips/max(total_tracked,1):.1%} of observations)')
    print(f'  Raw class stability:      {raw_stability:.1%}')
    print(f'  Smoothed class stability: {smoothed_stability:.1%}')
    print(f'  Improvement:              {smoothed_stability - raw_stability:+.1%}')

    return {
        'total_tracked': total_tracked,
        'raw_flips': raw_flips,
        'smoothed_flips': smoothed_flips,
        'raw_stability': raw_stability,
        'smoothed_stability': smoothed_stability,
    }


def bench_iou_computation(num_objects_list):
    """Benchmark raw IoU distance matrix computation (core of matching)."""
    print('\n=== IoU Distance Matrix Computation ===')
    print(f'{"N×N":>8} {"Median":>8} {"Mean":>8} {"P95":>8}')
    print('-' * 34)

    results = {}
    for n in num_objects_list:
        rng = np.random.RandomState(42)

        # Create mock STrack objects for iou_distance
        tracks = []
        for i in range(n):
            xywh = np.array([rng.randint(10, 500), rng.randint(10, 350),
                             rng.randint(30, 80), rng.randint(30, 80), i],
                            dtype=np.float32)
            tracks.append(STrack(xywh, 0.8, 0))

        detections = []
        for i in range(n):
            xywh = np.array([rng.randint(10, 500), rng.randint(10, 350),
                             rng.randint(30, 80), rng.randint(30, 80), i],
                            dtype=np.float32)
            detections.append(STrack(xywh, 0.8, 0))

        def run_iou():
            iou_distance(tracks, detections)

        stats = timeit_ms(run_iou)
        results[n] = stats
        print(f'{f"{n}x{n}":>8} {stats["median"]:>7.3f}ms {stats["mean"]:>7.3f}ms '
              f'{stats["p95"]:>7.3f}ms')

    return results


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Benchmark tracking overhead')
    parser.add_argument('--objects', type=int, default=10,
                        help='Number of objects for full pipeline test')
    parser.add_argument('--frames', type=int, default=300,
                        help='Number of frames for full pipeline test')
    args = parser.parse_args()

    print('=' * 62)
    print('  OWL Tracking Overhead Benchmark')
    print('  Measures: ByteTrack + ClassSmoother + CropMaskStabilizer')
    print('=' * 62)

    obj_counts = [5, 10, 20, 40, 50]

    all_results = {}

    # Individual component benchmarks
    all_results['bytetracker'] = bench_bytetracker_update(obj_counts)
    all_results['class_smoother'] = bench_class_smoother(obj_counts)
    all_results['crop_mask'] = bench_crop_mask_stabilizer([5, 10, 20, 30])
    all_results['iou_matrix'] = bench_iou_computation(obj_counts)

    # Full pipeline
    all_results['pipeline_stable'] = bench_full_pipeline(
        num_objects=args.objects, num_frames=args.frames,
        drop_rate=0.0, class_flicker_rate=0.0)

    all_results['pipeline_flickery'] = bench_full_pipeline(
        num_objects=args.objects, num_frames=args.frames,
        drop_rate=0.1, class_flicker_rate=0.1)

    all_results['pipeline_heavy'] = bench_full_pipeline(
        num_objects=args.objects, num_frames=args.frames,
        drop_rate=0.3, class_flicker_rate=0.2)

    all_results['pipeline_dense'] = bench_full_pipeline(
        num_objects=40, num_frames=args.frames,
        drop_rate=0.1, class_flicker_rate=0.1)

    # Accuracy metrics
    all_results['accuracy_light'] = bench_accuracy(
        num_objects=args.objects, num_frames=args.frames,
        drop_rate=0.1, class_flicker_rate=0.1)

    all_results['accuracy_heavy'] = bench_accuracy(
        num_objects=args.objects, num_frames=args.frames,
        drop_rate=0.1, class_flicker_rate=0.3)

    # RPi scaling estimates
    print('\n=== Estimated RPi Performance ===')
    typical = all_results['pipeline_flickery']['total']['median']
    print(f'  This machine (median):     {typical:.2f}ms')
    print(f'  RPi 5 estimate (2-3x):     {typical * 2.5:.2f}ms')
    print(f'  RPi 4 estimate (5-7x):     {typical * 6:.2f}ms')
    print(f'  Budget for 30fps:          33.3ms per frame')
    print(f'  Tracking % of budget (Pi5): {typical * 2.5 / 33.3 * 100:.1f}%')
    print(f'  Tracking % of budget (Pi4): {typical * 6 / 33.3 * 100:.1f}%')

    # Save results
    out_path = os.path.join(PROJECT_ROOT, 'benchmarks',
                            f'{time.strftime("%Y-%m-%d")}_tracker_overhead.json')
    with open(out_path, 'w') as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f'\nResults saved to: {out_path}')


if __name__ == '__main__':
    main()
