"""
Integrated multi-frame tracking tests.

Simulates realistic detection sequences across multiple frames to verify:
  - ClassSmoother reduces class flicker vs raw detections
  - CropMaskStabilizer preserves crop mask through detection dropouts
  - End-to-end pipeline correctly filters weed detections

Every assertion is on the numbers: classes, flips, masked pixels. Three
tests here used to render review images and assert only that a PNG had been
written; they are gone, and what the images showed is asserted instead.
Nothing in this file writes a file.
"""

import numpy as np

from utils.tracker import ClassSmoother, CropMaskStabilizer


def _per_frame_mask(boxes, shape=(480, 640)):
    """The crop mask with no stabilisation: this frame's boxes only."""
    mask = np.zeros(shape, dtype=np.uint8)
    for x1, y1, x2, y2 in boxes:
        mask[max(0, y1):min(y2, shape[0]), max(0, x1):min(x2, shape[1])] = 255
    return mask


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
# Tests
# ============================================

class TestClassSmootherMultiFrame:
    """Multi-frame class smoothing tests."""

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

        # 25% flicker over 60 frames: well over a hundred raw flips (147 with
        # this seed), and a 5-frame majority vote removes about four in five
        assert raw_flips > 100, f'the scene only flickered {raw_flips} times'
        assert smoothed_flips < raw_flips * 0.3, (
            f'Smoothing only reduced flips from {raw_flips} to {smoothed_flips} '
            f'({smoothed_flips/max(raw_flips,1):.0%}), expected under 30% left'
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

class TestCropMaskStabilizerMultiFrame:
    """Multi-frame crop mask stabilization tests."""

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

        # On every dropout frame the stabilizer holds pixels the per-frame
        # mask lost (">=" alone passes a stabilizer that does nothing)
        dropout_frames = [f for f in range(num_frames) if f % 5 in (2, 3)]
        assert len(dropout_frames) == 12
        for f in dropout_frames:
            assert stabilized_coverage[f] > per_frame_coverage[f], (
                f'Frame {f}: half the crops dropped out and the stabilized mask kept nothing')

        # and coverage is steadier for it
        assert st_std < pf_std * 0.5, (
            f'Stabilizer variance ({st_std:.0f}) is not well under per-frame ({pf_std:.0f})'
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

    def test_a_dropped_crop_stays_masked_where_it_was_last_seen_until_max_age(self):
        """What the side-by-side mask picture showed: a crop that drops out
        keeps its last box in the stabilized mask (and has none in the
        per-frame mask) for max_age frames, then goes."""
        scene = SyntheticTrackingScene(num_objects=4, weed_fraction=0.0, seed=42)
        stabilizer = CropMaskStabilizer(max_age=3)

        tids, boxes, _, _, _ = scene.generate_frame(0)
        stabilizer.update(tids, boxes)
        last_seen = dict(zip(tids, boxes))
        dropped, kept = {0, 2}, {1, 3}

        for frame in range(1, 6):                         # 5 frames with crops 0 and 2 missing
            tids, boxes, _, _, _ = scene.generate_frame(frame, burst_drop_ids=dropped)
            assert set(tids) == kept
            stabilizer.update(tids, boxes)
            stabilized = stabilizer.build_stabilized_mask((480, 640))
            per_frame = _per_frame_mask(boxes)
            for tid in dropped:
                x1, y1, x2, y2 = last_seen[tid]
                assert not per_frame[y1:y2, x1:x2].any()
                if frame <= 3:
                    assert (stabilized[y1:y2, x1:x2] == 255).all(), (
                        f'frame {frame}: crop {tid} fell out of the mask after {frame} missed frames')
                else:
                    assert not stabilized[y1:y2, x1:x2].any(), (
                        f'frame {frame}: crop {tid} still masked {frame} frames after it was last seen')
            for tid, (x1, y1, x2, y2) in zip(tids, boxes):  # the ones in view follow their boxes
                assert (stabilized[y1:y2, x1:x2] == 255).all()
            assert stabilizer.persisted_count == (2 if frame <= 3 else 0)
            assert stabilizer.active_count == (4 if frame <= 3 else 2)


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

        # 20% flicker makes a crop look like a weed 23 times in 159 here;
        # "<=" alone passes a smoother that does nothing
        assert false_positives_raw >= 15, 'the scene produced too few misclassified crops'
        assert smoothed_fp_rate < raw_fp_rate / 3, (
            f'Smoothed FP rate ({smoothed_fp_rate:.1%}) is not well under raw ({raw_fp_rate:.1%})'
        )
        # without throwing the real weeds away
        assert total_smoothed_detections >= total_raw_detections * 0.9

    def test_pipeline_with_dropouts_and_flicker(self):
        """15% dropouts and 20% class flicker together, as owl.py feeds the
        stabilizer (crop-class detections only). A real crop whose centre is
        outside the mask is a hole a weed detection leaks through: the
        stabilized mask must close nearly all the holes the per-frame mask has."""
        scene = SyntheticTrackingScene(
            num_objects=6, weed_fraction=0.5, seed=123)
        stabilizer = CropMaskStabilizer(max_age=3)

        crops_in_view = holes_per_frame = holes_stabilized = 0
        for f in range(50):
            tids, boxes, cls_ids, confs, gt = scene.generate_frame(
                f, drop_rate=0.15, class_flicker_rate=0.2)

            crop_tids = [tid for tid, cls in zip(tids, cls_ids) if cls == 1]
            crop_boxes = [box for box, cls in zip(boxes, cls_ids) if cls == 1]
            stabilizer.update(crop_tids, crop_boxes)
            stabilized = stabilizer.build_stabilized_mask((480, 640))
            per_frame = _per_frame_mask(crop_boxes)

            for truth in gt.values():
                if truth['true_class'] != 1:
                    continue
                x1, y1, x2, y2 = truth['box']
                cy, cx = (y1 + y2) // 2, (x1 + x2) // 2
                if not 0 <= cy < 480:
                    continue
                crops_in_view += 1
                holes_per_frame += per_frame[cy, cx] == 0
                holes_stabilized += stabilized[cy, cx] == 0

        assert crops_in_view == 150
        assert holes_per_frame >= 30, f'the scene only opened {holes_per_frame} holes'   # 44 here
        assert holes_stabilized * 5 <= holes_per_frame, (                               # 3 here
            f'{holes_stabilized} of {holes_per_frame} holes left open by the stabilizer')

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
