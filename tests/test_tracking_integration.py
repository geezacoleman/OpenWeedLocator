"""
Integrated multi-frame tracking tests.

Simulates realistic detection sequences across multiple frames to verify:
  - ClassSmoother reduces class flicker vs raw detections
  - CropMaskStabilizer preserves crop mask through detection dropouts
  - End-to-end pipeline correctly filters weed detections

Generates visualization images in tests/tracking_output/ for manual review
of smoothing behavior. These are gitignored but useful for debugging.

Run: pytest tests/test_tracking_integration.py -v
"""

import os
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.tracker import ClassSmoother, CropMaskStabilizer

# Output directory for visualization images
VIZ_DIR = Path(__file__).parent / 'tracking_output'


def ensure_viz_dir():
    """Create the visualization output directory if it doesn't exist."""
    VIZ_DIR.mkdir(exist_ok=True)


# ============================================
# Synthetic scene generator
# ============================================

class SyntheticTrackingScene:
    """Generate realistic multi-frame detection sequences with ground truth.

    Objects drift across the frame simulating tractor movement. Noise models
    configurable detection dropouts and class flicker (the two problems
    tracking is designed to solve).
    """

    def __init__(self, num_objects=6, frame_width=640, frame_height=480,
                 box_size=40, weed_fraction=0.5, seed=42):
        self.rng = np.random.RandomState(seed)
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.box_size = box_size

        self.objects = []
        for i in range(num_objects):
            is_weed = i < int(num_objects * weed_fraction)
            self.objects.append({
                'id': i,
                'true_class': 0 if is_weed else 1,  # 0=weed, 1=crop
                'x': 50 + i * (frame_width // (num_objects + 1)),
                'y': self.rng.randint(50, frame_height - 100),
                'w': box_size + self.rng.randint(-5, 10),
                'h': box_size + self.rng.randint(-5, 10),
                'speed_y': 3 + self.rng.uniform(-1, 1),  # px per frame
                'conf': 0.7 + self.rng.uniform(0, 0.2),
            })

    def generate_frame(self, frame_idx, drop_rate=0.0, class_flicker_rate=0.0,
                       burst_drop_ids=None):
        """Generate detections for one frame with noise.

        Args:
            frame_idx: Frame number.
            drop_rate: Probability of dropping a detection (per object).
            class_flicker_rate: Probability of flipping class 0↔1.
            burst_drop_ids: Set of object IDs to drop entirely (burst dropout).

        Returns:
            (track_ids, boxes, class_ids, confidences, ground_truth)
        """
        track_ids = []
        boxes = []
        class_ids = []
        confidences = []
        ground_truth = {}

        for obj in self.objects:
            # Update position
            y = obj['y'] + obj['speed_y'] * frame_idx
            if y + obj['h'] < 0 or y > self.frame_height:
                continue

            y_clamped = max(0, min(y, self.frame_height - obj['h']))
            x = obj['x']

            gt_entry = {
                'true_class': obj['true_class'],
                'box': [int(x), int(y_clamped),
                        int(x) + obj['w'], int(y_clamped) + obj['h']],
            }
            ground_truth[obj['id']] = gt_entry

            # Apply burst dropout
            if burst_drop_ids and obj['id'] in burst_drop_ids:
                continue

            # Apply random drop
            if self.rng.random() < drop_rate:
                continue

            # Apply class flicker
            cls = obj['true_class']
            if self.rng.random() < class_flicker_rate:
                cls = 1 - cls

            # Add noise to confidence
            conf = obj['conf'] + self.rng.uniform(-0.05, 0.05)
            conf = max(0.3, min(0.99, conf))

            track_ids.append(obj['id'])
            boxes.append([int(x), int(y_clamped),
                          int(x) + obj['w'], int(y_clamped) + obj['h']])
            class_ids.append(cls)
            confidences.append(conf)

        return track_ids, boxes, class_ids, confidences, ground_truth


# ============================================
# Visualization helpers
# ============================================

def draw_frame_state(frame_idx, ground_truth, detected_ids, raw_classes,
                     smoothed_classes, frame_width=640, frame_height=480):
    """Draw a visualization frame showing raw vs smoothed class assignments.

    Returns a BGR image with:
     - Green boxes for weeds (class 0), blue for crops (class 1)
     - Solid border = smoothed assignment, dashed = raw (when different)
     - Gray boxes for undetected (dropped) objects
     - Legend showing flicker stats
    """
    img = np.ones((frame_height, frame_width, 3), dtype=np.uint8) * 240  # light gray bg

    colors = {0: (50, 180, 50), 1: (180, 120, 50)}  # weed=green, crop=blue
    class_names = {0: 'weed', 1: 'crop'}

    for obj_id, gt in ground_truth.items():
        x1, y1, x2, y2 = gt['box']
        true_cls = gt['true_class']
        detected = obj_id in detected_ids

        if detected:
            idx = detected_ids.index(obj_id)
            raw_cls = raw_classes[idx]
            sm_cls = smoothed_classes.get(obj_id, raw_cls)

            # Smoothed border (thick)
            sm_color = colors[sm_cls]
            cv2.rectangle(img, (x1, y1), (x2, y2), sm_color, 3)

            # If raw differs from smoothed, show raw as thin dashed inner border
            if raw_cls != sm_cls:
                raw_color = colors[raw_cls]
                cv2.rectangle(img, (x1 + 4, y1 + 4), (x2 - 4, y2 - 4), raw_color, 1)
                # "FLICKERED" label
                cv2.putText(img, 'FIXED', (x1, y1 - 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 0, 200), 1)

            # Labels
            label = f'T{obj_id} {class_names[sm_cls]}'
            cv2.putText(img, label, (x1 + 2, y2 + 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, sm_color, 1)
        else:
            # Dropped detection — gray dashed box
            cv2.rectangle(img, (x1, y1), (x2, y2), (180, 180, 180), 1)
            cv2.putText(img, f'T{obj_id} DROPPED', (x1 + 2, y2 + 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (150, 150, 150), 1)

    # Frame label
    cv2.putText(img, f'Frame {frame_idx}', (10, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)

    return img


def draw_mask_comparison(frame_idx, gt, per_frame_mask, stabilized_mask,
                         frame_width=640, frame_height=480):
    """Draw side-by-side comparison of per-frame vs stabilized crop mask.

    Left half: per-frame mask (what you get without stabilizer)
    Right half: stabilized mask (with CropMaskStabilizer)
    """
    h, w = frame_height, frame_width
    img = np.ones((h, w * 2 + 20, 3), dtype=np.uint8) * 240

    # Left: per-frame mask
    left_mask_color = cv2.cvtColor(per_frame_mask, cv2.COLOR_GRAY2BGR)
    # Tint crop regions blue
    left_mask_color[per_frame_mask > 0] = (180, 120, 50)
    img[0:h, 0:w] = left_mask_color

    # Right: stabilized mask
    right_mask_color = cv2.cvtColor(stabilized_mask, cv2.COLOR_GRAY2BGR)
    right_mask_color[stabilized_mask > 0] = (50, 180, 50)
    img[0:h, w + 20:w * 2 + 20] = right_mask_color

    # Draw GT object positions on both halves
    for obj_id, info in gt.items():
        bx1, by1, bx2, by2 = info['box']
        if info['true_class'] == 1:  # crop
            cv2.rectangle(img, (bx1, by1), (bx2, by2), (0, 0, 255), 1)
            cv2.rectangle(img, (bx1 + w + 20, by1), (bx2 + w + 20, by2), (0, 0, 255), 1)

    # Labels
    cv2.putText(img, f'Frame {frame_idx} - Per-frame mask', (10, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
    cv2.putText(img, f'Frame {frame_idx} - Stabilized mask', (w + 30, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)

    return img


# ============================================
# Integrated Tests
# ============================================

class TestClassSmootherMultiFrame:
    """Multi-frame class smoothing tests with visualization."""

    def test_smoothing_reduces_class_flips(self):
        """ClassSmoother significantly reduces class flips vs raw detections."""
        scene = SyntheticTrackingScene(num_objects=8, weed_fraction=0.5, seed=42)
        smoother = ClassSmoother(window=5)
        num_frames = 60
        flicker_rate = 0.25  # 25% chance per object per frame

        raw_flips = 0
        smoothed_flips = 0
        raw_last = {}
        smoothed_last = {}
        total_observations = 0

        for f in range(num_frames):
            tids, boxes, cls_ids, confs, gt = scene.generate_frame(
                f, drop_rate=0.0, class_flicker_rate=flicker_rate)

            smoothed = smoother.update(tids, cls_ids, confs, frame_count=f)
            total_observations += len(tids)

            for tid, raw_c in zip(tids, cls_ids):
                sm_c = smoothed.get(tid, raw_c)

                if tid in raw_last and raw_last[tid] != raw_c:
                    raw_flips += 1
                if tid in smoothed_last and smoothed_last[tid] != sm_c:
                    smoothed_flips += 1

                raw_last[tid] = raw_c
                smoothed_last[tid] = sm_c

        # Smoothing should cut flips by at least 50%
        assert smoothed_flips < raw_flips * 0.5, (
            f'Smoothing only reduced flips from {raw_flips} to {smoothed_flips} '
            f'({smoothed_flips/max(raw_flips,1):.0%}), expected >50% reduction'
        )

    def test_smoothing_converges_to_true_class(self):
        """After enough observations, smoothed class matches ground truth."""
        scene = SyntheticTrackingScene(num_objects=6, weed_fraction=0.5, seed=99)
        smoother = ClassSmoother(window=5)

        # Run 30 frames with 20% flicker — enough for majority vote to settle
        for f in range(30):
            tids, boxes, cls_ids, confs, gt = scene.generate_frame(
                f, class_flicker_rate=0.2)
            smoothed = smoother.update(tids, cls_ids, confs, frame_count=f)

        # After 30 frames, check last frame's smoothed classes against ground truth
        tids, boxes, cls_ids, confs, gt = scene.generate_frame(30)
        smoothed = smoother.update(tids, cls_ids, confs, frame_count=30)

        correct = 0
        total = 0
        for tid in tids:
            if tid in gt:
                total += 1
                if smoothed.get(tid) == gt[tid]['true_class']:
                    correct += 1

        accuracy = correct / max(total, 1)
        assert accuracy >= 0.8, (
            f'Smoothed class accuracy {accuracy:.0%} is below 80% threshold '
            f'({correct}/{total} correct)'
        )

    def test_smoothing_visualization_saved(self):
        """Generate visualization frames showing smoothing in action."""
        ensure_viz_dir()
        scene = SyntheticTrackingScene(num_objects=6, weed_fraction=0.5, seed=42)
        smoother = ClassSmoother(window=5)
        num_frames = 20
        flicker_rate = 0.3  # High flicker for visible effect

        frames = []
        raw_flip_count = 0
        smoothed_flip_count = 0
        raw_last = {}
        smoothed_last = {}

        for f in range(num_frames):
            tids, boxes, cls_ids, confs, gt = scene.generate_frame(
                f, class_flicker_rate=flicker_rate)
            smoothed = smoother.update(tids, cls_ids, confs, frame_count=f)

            # Count flips
            for tid, raw_c in zip(tids, cls_ids):
                sm_c = smoothed.get(tid, raw_c)
                if tid in raw_last and raw_last[tid] != raw_c:
                    raw_flip_count += 1
                if tid in smoothed_last and smoothed_last[tid] != sm_c:
                    smoothed_flip_count += 1
                raw_last[tid] = raw_c
                smoothed_last[tid] = sm_c

            # Draw visualization frame
            viz = draw_frame_state(f, gt, tids, cls_ids, smoothed)
            frames.append(viz)

        # Save grid of frames (4 columns)
        cols = 4
        rows = (num_frames + cols - 1) // cols
        cell_h, cell_w = frames[0].shape[:2]
        grid = np.ones((rows * cell_h, cols * cell_w, 3), dtype=np.uint8) * 255

        for i, frame in enumerate(frames):
            r, c = divmod(i, cols)
            grid[r * cell_h:(r + 1) * cell_h, c * cell_w:(c + 1) * cell_w] = frame

        # Add summary text at bottom
        summary = (f'Raw flips: {raw_flip_count} | '
                   f'Smoothed flips: {smoothed_flip_count} | '
                   f'Reduction: {1 - smoothed_flip_count/max(raw_flip_count,1):.0%}')
        cv2.putText(grid, summary, (20, grid.shape[0] - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 200), 2)

        out_path = str(VIZ_DIR / 'class_smoothing_grid.png')
        cv2.imwrite(out_path, grid)
        assert os.path.exists(out_path), f'Visualization not saved to {out_path}'


class TestCropMaskStabilizerMultiFrame:
    """Multi-frame crop mask stabilization tests with visualization."""

    def test_stabilizer_fills_dropout_gaps(self):
        """CropMaskStabilizer maintains mask coverage during detection dropouts."""
        scene = SyntheticTrackingScene(num_objects=6, weed_fraction=0.0, seed=42)
        # All crops (weed_fraction=0) for mask testing
        stabilizer = CropMaskStabilizer(max_age=3)

        num_frames = 30
        per_frame_coverage = []
        stabilized_coverage = []

        for f in range(num_frames):
            # Alternating burst dropouts to simulate detection flicker
            burst_drops = None
            if f % 5 in (2, 3):  # Drop detections on frames 2-3 of every 5
                burst_drops = {0, 2, 4}  # Drop half the objects

            tids, boxes, cls_ids, confs, gt = scene.generate_frame(
                f, burst_drop_ids=burst_drops)

            # Per-frame mask (no stabilization)
            pf_mask = np.zeros((480, 640), dtype=np.uint8)
            for box in boxes:
                x1, y1, x2, y2 = box
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(x2, 640), min(y2, 480)
                pf_mask[y1:y2, x1:x2] = 255

            # Stabilized mask
            stabilizer.update(tids, boxes)
            st_mask = stabilizer.build_stabilized_mask((480, 640))

            pf_pixels = np.count_nonzero(pf_mask)
            st_pixels = np.count_nonzero(st_mask)
            per_frame_coverage.append(pf_pixels)
            stabilized_coverage.append(st_pixels)

        # Stabilized should have more consistent coverage (less variance)
        pf_std = np.std(per_frame_coverage)
        st_std = np.std(stabilized_coverage)

        # Stabilizer should also never have LESS coverage than per-frame
        for f in range(num_frames):
            assert stabilized_coverage[f] >= per_frame_coverage[f], (
                f'Frame {f}: stabilized coverage ({stabilized_coverage[f]}) < '
                f'per-frame ({per_frame_coverage[f]})'
            )

        # Stabilizer should have lower variance in coverage
        assert st_std <= pf_std, (
            f'Stabilizer variance ({st_std:.0f}) > per-frame ({pf_std:.0f})'
        )

    def test_stabilizer_expires_old_tracks(self):
        """Tracks that are dropped for longer than max_age are removed."""
        stabilizer = CropMaskStabilizer(max_age=2)

        # Frame 0: 3 tracks (xyxy format)
        stabilizer.update([1, 2, 3], [[10, 10, 60, 60], [100, 10, 150, 60], [200, 10, 250, 60]])
        assert stabilizer.active_count == 3

        # Frames 1-3: only track 1 present → tracks 2,3 age out
        for _ in range(3):
            stabilizer.update([1], [[10, 10, 60, 60]])

        assert stabilizer.active_count == 1  # only track 1 remains
        mask = stabilizer.build_stabilized_mask((100, 300))
        # Only track 1's region should be in mask
        assert np.any(mask[10:60, 10:60] == 255)
        assert np.all(mask[10:60, 100:150] == 0)  # track 2 expired
        assert np.all(mask[10:60, 200:250] == 0)  # track 3 expired

    def test_mask_stabilization_visualization_saved(self):
        """Generate side-by-side mask comparison frames."""
        ensure_viz_dir()
        scene = SyntheticTrackingScene(num_objects=4, weed_fraction=0.0, seed=42)
        stabilizer = CropMaskStabilizer(max_age=3)

        num_frames = 15
        comparison_frames = []

        for f in range(num_frames):
            # Simulate periodic detection dropout
            burst_drops = None
            if f % 4 in (2, 3):
                burst_drops = {0, 2}  # Drop 2 of 4 objects

            tids, boxes, cls_ids, confs, gt = scene.generate_frame(
                f, burst_drop_ids=burst_drops)

            # Per-frame mask
            pf_mask = np.zeros((480, 640), dtype=np.uint8)
            for box in boxes:
                x1, y1, x2, y2 = box
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(x2, 640), min(y2, 480)
                pf_mask[y1:y2, x1:x2] = 255

            # Stabilized mask
            stabilizer.update(tids, boxes)
            st_mask = stabilizer.build_stabilized_mask((480, 640))

            viz = draw_mask_comparison(f, gt, pf_mask, st_mask)
            comparison_frames.append(viz)

        # Save grid (3 columns, each is 2x wide due to side-by-side)
        cols = 3
        rows = (num_frames + cols - 1) // cols
        cell_h, cell_w = comparison_frames[0].shape[:2]
        grid = np.ones((rows * cell_h, cols * cell_w, 3), dtype=np.uint8) * 255

        for i, frame in enumerate(comparison_frames):
            r, c = divmod(i, cols)
            grid[r * cell_h:(r + 1) * cell_h, c * cell_w:(c + 1) * cell_w] = frame

        out_path = str(VIZ_DIR / 'mask_stabilization_grid.png')
        cv2.imwrite(out_path, grid)
        assert os.path.exists(out_path), f'Visualization not saved to {out_path}'


class TestEndToEndTrackingPipeline:
    """End-to-end tests simulating the full tracking pipeline as owl.py would."""

    def test_pipeline_smoother_filters_weeds_correctly(self):
        """Full pipeline: track → smooth → filter by target class."""
        scene = SyntheticTrackingScene(
            num_objects=8, weed_fraction=0.5, seed=42)
        smoother = ClassSmoother(window=5)

        target_class_ids = {0}  # Only detect weeds
        num_frames = 40
        flicker_rate = 0.2

        false_positives_raw = 0
        false_positives_smoothed = 0
        total_raw_detections = 0
        total_smoothed_detections = 0

        for f in range(num_frames):
            tids, boxes, cls_ids, confs, gt = scene.generate_frame(
                f, class_flicker_rate=flicker_rate)

            smoothed = smoother.update(tids, cls_ids, confs, frame_count=f)

            # Raw filtering (what happens without smoothing)
            raw_weeds = [(tid, box) for tid, cls, box
                         in zip(tids, cls_ids, boxes) if cls in target_class_ids]

            # Smoothed filtering (what happens with smoothing)
            smoothed_weeds = [(tid, box) for tid, box
                              in zip(tids, boxes) if smoothed.get(tid, -1) in target_class_ids]

            # Check false positives (crop classified as weed)
            for tid, _ in raw_weeds:
                total_raw_detections += 1
                if tid in gt and gt[tid]['true_class'] != 0:  # Not actually a weed
                    false_positives_raw += 1

            for tid, _ in smoothed_weeds:
                total_smoothed_detections += 1
                if tid in gt and gt[tid]['true_class'] != 0:
                    false_positives_smoothed += 1

        # Smoothing should reduce false positives
        raw_fp_rate = false_positives_raw / max(total_raw_detections, 1)
        smoothed_fp_rate = false_positives_smoothed / max(total_smoothed_detections, 1)

        assert smoothed_fp_rate <= raw_fp_rate, (
            f'Smoothed FP rate ({smoothed_fp_rate:.1%}) > raw FP rate ({raw_fp_rate:.1%})'
        )

    def test_pipeline_with_dropouts_and_flicker(self):
        """Pipeline handles simultaneous dropouts and class flicker."""
        scene = SyntheticTrackingScene(
            num_objects=6, weed_fraction=0.5, seed=123)
        smoother = ClassSmoother(window=5)
        stabilizer = CropMaskStabilizer(max_age=3)

        num_frames = 50
        drop_rate = 0.15
        flicker_rate = 0.2

        # Track how many frames have mask coverage
        frames_with_coverage = 0

        for f in range(num_frames):
            tids, boxes, cls_ids, confs, gt = scene.generate_frame(
                f, drop_rate=drop_rate, class_flicker_rate=flicker_rate)

            smoothed = smoother.update(tids, cls_ids, confs, frame_count=f)

            # Feed crop detections to stabilizer
            crop_tids = [tid for tid, cls in zip(tids, cls_ids) if cls == 1]
            crop_boxes = [box for box, cls in zip(boxes, cls_ids) if cls == 1]
            stabilizer.update(crop_tids, crop_boxes)
            mask = stabilizer.build_stabilized_mask((480, 640))

            if np.any(mask):
                frames_with_coverage += 1

        # With 50% crops and stabilization, we should have mask coverage
        # on most frames despite 15% drop rate
        coverage_ratio = frames_with_coverage / num_frames
        assert coverage_ratio >= 0.7, (
            f'Mask coverage only on {coverage_ratio:.0%} of frames, expected >= 70%'
        )

    def test_pipeline_reset_and_restart(self):
        """Smoother and stabilizer work correctly after reset (detection toggle off/on)."""
        scene = SyntheticTrackingScene(num_objects=4, weed_fraction=0.5, seed=42)
        smoother = ClassSmoother(window=5)
        stabilizer = CropMaskStabilizer(max_age=3)

        # Run 10 frames
        for f in range(10):
            tids, boxes, cls_ids, confs, gt = scene.generate_frame(f)
            smoother.update(tids, cls_ids, confs, frame_count=f)
            stabilizer.update(tids, boxes)

        assert smoother.get_class(0) != -1  # Has history
        assert stabilizer.active_count > 0

        # Reset (simulating detection toggle off)
        smoother.reset()
        stabilizer.reset()

        assert smoother.get_class(0) == -1
        assert stabilizer.active_count == 0

        # Run 10 more frames (simulating detection toggle on)
        for f in range(10, 20):
            tids, boxes, cls_ids, confs, gt = scene.generate_frame(f)
            smoothed = smoother.update(tids, cls_ids, confs, frame_count=f)
            stabilizer.update(tids, boxes)

        # Should be working again
        assert len(smoothed) > 0
        assert stabilizer.active_count > 0

    def test_pipeline_visualization_saved(self):
        """Generate comprehensive visualization showing full pipeline behavior."""
        ensure_viz_dir()
        scene = SyntheticTrackingScene(
            num_objects=6, weed_fraction=0.5, seed=42)
        smoother = ClassSmoother(window=5)
        stabilizer = CropMaskStabilizer(max_age=3)

        num_frames = 24
        drop_rate = 0.1
        flicker_rate = 0.25

        viz_frames = []
        stats_log = []

        raw_flip_total = 0
        sm_flip_total = 0
        raw_last = {}
        sm_last = {}

        for f in range(num_frames):
            # Burst dropout every 6th frame for 2 frames
            burst = None
            if f % 6 in (4, 5):
                burst = {1, 3}  # Drop weed id=1, crop id=3

            tids, boxes, cls_ids, confs, gt = scene.generate_frame(
                f, drop_rate=drop_rate, class_flicker_rate=flicker_rate,
                burst_drop_ids=burst)

            smoothed = smoother.update(tids, cls_ids, confs, frame_count=f)

            # Track flips
            for tid, raw_c in zip(tids, cls_ids):
                sm_c = smoothed.get(tid, raw_c)
                if tid in raw_last and raw_last[tid] != raw_c:
                    raw_flip_total += 1
                if tid in sm_last and sm_last[tid] != sm_c:
                    sm_flip_total += 1
                raw_last[tid] = raw_c
                sm_last[tid] = sm_c

            # Feed crops to stabilizer
            crop_tids = [tid for tid, cls in zip(tids, cls_ids) if cls == 1]
            crop_boxes = [box for box, cls in zip(boxes, cls_ids) if cls == 1]
            stabilizer.update(crop_tids, crop_boxes)
            mask = stabilizer.build_stabilized_mask((480, 640))

            viz = draw_frame_state(f, gt, tids, cls_ids, smoothed)

            # Draw mask coverage indicator
            mask_pct = np.count_nonzero(mask) / (480 * 640) * 100
            cv2.putText(viz, f'Mask: {mask_pct:.1f}%', (440, 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (100, 100, 100), 1)
            cv2.putText(viz, f'Persisted: {stabilizer.persisted_count}',
                        (440, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (100, 100, 100), 1)
            if burst:
                cv2.putText(viz, 'BURST DROP', (250, 25),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)

            viz_frames.append(viz)

        # Build grid
        cols = 4
        rows = (num_frames + cols - 1) // cols
        cell_h, cell_w = viz_frames[0].shape[:2]
        grid = np.ones((rows * cell_h + 60, cols * cell_w, 3), dtype=np.uint8) * 255

        for i, frame in enumerate(viz_frames):
            r, c = divmod(i, cols)
            grid[r * cell_h:(r + 1) * cell_h, c * cell_w:(c + 1) * cell_w] = frame

        # Summary bar
        y_summary = rows * cell_h + 20
        reduction = 1 - sm_flip_total / max(raw_flip_total, 1)
        summary = (f'Pipeline: {num_frames}f | '
                   f'Drop: {drop_rate:.0%} + burst | '
                   f'Flicker: {flicker_rate:.0%} | '
                   f'Raw flips: {raw_flip_total} | '
                   f'Smoothed flips: {sm_flip_total} | '
                   f'Reduction: {reduction:.0%}')
        cv2.putText(grid, summary, (20, y_summary),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 150), 1)

        out_path = str(VIZ_DIR / 'full_pipeline_grid.png')
        cv2.imwrite(out_path, grid)
        assert os.path.exists(out_path), f'Visualization not saved to {out_path}'


class TestTrackingPerformanceBaseline:
    """Verify that tracking overhead is within acceptable bounds.

    These tests don't need a YOLO model — they measure the pure Python
    tracking layers (ClassSmoother + CropMaskStabilizer) which are what
    we add on top of ByteTrack.
    """

    def test_class_smoother_under_1ms_for_20_tracks(self):
        """ClassSmoother.update() should be < 1ms for 20 tracks."""
        import time
        smoother = ClassSmoother(window=5)
        tids = list(range(20))
        cls = [0 if i % 2 == 0 else 1 for i in range(20)]
        confs = [0.8] * 20

        # Warmup
        for _ in range(50):
            smoother.update(tids, cls, confs)

        times = []
        for _ in range(200):
            t0 = time.perf_counter()
            smoother.update(tids, cls, confs)
            times.append((time.perf_counter() - t0) * 1000)

        median_ms = sorted(times)[len(times) // 2]
        assert median_ms < 1.0, f'ClassSmoother median {median_ms:.3f}ms > 1ms for 20 tracks'

    def test_crop_stabilizer_under_1ms_for_10_crops(self):
        """CropMaskStabilizer.build_stabilized_mask() should be < 1ms for 10 crops."""
        import time
        stabilizer = CropMaskStabilizer(max_age=3)
        tids = list(range(10))
        boxes = [[i * 60, 50, i * 60 + 40, 90] for i in range(10)]
        stabilizer.update(tids, boxes)

        # Warmup
        for _ in range(50):
            stabilizer.build_stabilized_mask((480, 640))

        times = []
        for _ in range(200):
            t0 = time.perf_counter()
            stabilizer.build_stabilized_mask((480, 640))
            times.append((time.perf_counter() - t0) * 1000)

        median_ms = sorted(times)[len(times) // 2]
        assert median_ms < 1.0, f'CropMaskStabilizer median {median_ms:.3f}ms > 1ms for 10 crops'
