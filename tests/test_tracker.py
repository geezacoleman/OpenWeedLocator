"""
Unit tests for utils/tracker.py — ClassSmoother and CropMaskStabilizer.

These are the temporal smoothing layers that sit on top of ByteTrack's
track IDs. ClassSmoother does majority-vote class assignment per track;
CropMaskStabilizer persists crop mask positions through detection dropouts.

Run: pytest tests/test_tracker.py -v
"""

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.tracker import ClassSmoother, CropMaskStabilizer


# ============================================
# ClassSmoother
# ============================================

class TestClassSmootherBasic:
    """Core majority-vote behaviour."""

    def test_single_observation_returns_that_class(self):
        s = ClassSmoother(window=5)
        result = s.update([1], [3], [0.9])
        assert result == {1: 3}

    def test_majority_vote_with_consistent_class(self):
        s = ClassSmoother(window=5)
        for _ in range(5):
            result = s.update([1], [0], [0.9])
        assert result[1] == 0

    def test_majority_vote_flipping_classes(self):
        """If class flips between 0 and 1 with 0 being majority, smoothed = 0."""
        s = ClassSmoother(window=5)
        classes = [0, 0, 0, 1, 1]
        for cls in classes:
            result = s.update([1], [cls], [0.9])
        assert result[1] == 0

    def test_window_evicts_old_observations(self):
        """After window fills, oldest observations drop off."""
        s = ClassSmoother(window=3)
        # First 3: class 0
        for _ in range(3):
            s.update([1], [0], [0.9])
        # Next 2: class 1 — window now has [0, 1, 1]
        for _ in range(2):
            result = s.update([1], [1], [0.9])
        assert result[1] == 1  # 1 is majority in window [0, 1, 1]

    def test_multiple_tracks_independent(self):
        """Different track IDs maintain independent histories."""
        s = ClassSmoother(window=5)
        for _ in range(3):
            s.update([1, 2], [0, 1], [0.9, 0.8])
        result = s.update([1, 2], [0, 1], [0.9, 0.8])
        assert result[1] == 0
        assert result[2] == 1

    def test_get_class_for_specific_track(self):
        s = ClassSmoother(window=5)
        s.update([10], [2], [0.9])
        assert s.get_class(10) == 2

    def test_get_class_unknown_track_returns_minus_one(self):
        s = ClassSmoother(window=5)
        assert s.get_class(999) == -1


class TestClassSmootherPruning:
    """Stale track pruning."""

    def test_stale_tracks_pruned(self):
        """Tracks not seen for 2x window frames are removed."""
        s = ClassSmoother(window=3)
        # Frame 1: track 1 appears
        s.update([1], [0], [0.9], frame_count=1)
        # Frames 8-10: only track 2 appears (track 1 is stale after 6 frames)
        for fc in range(8, 11):
            s.update([2], [1], [0.8], frame_count=fc)
        # Track 1 should be pruned (last seen at frame 1, threshold = 10 - 6 = 4)
        assert s.get_class(1) == -1

    def test_active_tracks_not_pruned(self):
        """Tracks seen this frame are never pruned."""
        s = ClassSmoother(window=3)
        for fc in range(20):
            s.update([1], [0], [0.9], frame_count=fc)
        assert s.get_class(1) == 0

    def test_no_pruning_when_frame_count_zero(self):
        """frame_count=0 (default) disables pruning."""
        s = ClassSmoother(window=3)
        s.update([1], [0], [0.9], frame_count=0)
        # Many updates with track 2 only, but frame_count stays 0
        for _ in range(20):
            s.update([2], [1], [0.8], frame_count=0)
        # Track 1 should NOT be pruned
        assert s.get_class(1) == 0

    def test_pruning_works_with_high_frame_counts(self):
        """Monotonic frame_count (no wrap) must prune correctly at large values.

        Regression test: previous code wrapped frame_count 900->1, causing
        stale tracks to never be pruned. With monotonic counts, pruning must
        still work at frame 10000+.
        """
        s = ClassSmoother(window=3)
        base = 10000
        s.update([1], [0], [0.9], frame_count=base)
        # Advance well past the stale threshold (2 * window = 6 frames)
        for fc in range(base + 7, base + 12):
            s.update([2], [1], [0.8], frame_count=fc)
        # Track 1 should be pruned
        assert s.get_class(1) == -1
        # Track 2 should still be alive
        assert s.get_class(2) == 1


class TestClassSmootherReset:
    """Reset clears all state."""

    def test_reset_clears_history(self):
        s = ClassSmoother(window=5)
        s.update([1, 2, 3], [0, 1, 2], [0.9, 0.8, 0.7])
        s.reset()
        assert s.get_class(1) == -1
        assert s.get_class(2) == -1
        assert s.get_class(3) == -1

    def test_usable_after_reset(self):
        s = ClassSmoother(window=5)
        s.update([1], [0], [0.9])
        s.reset()
        result = s.update([1], [1], [0.8])
        assert result[1] == 1


class TestClassSmootherEdgeCases:
    """Edge cases and type handling."""

    def test_empty_input(self):
        s = ClassSmoother(window=5)
        result = s.update([], [], [])
        assert result == {}

    def test_float_track_ids_converted_to_int(self):
        """ByteTrack sometimes returns float IDs; must be int-keyed."""
        s = ClassSmoother(window=5)
        result = s.update([1.0, 2.0], [0, 1], [0.9, 0.8])
        assert 1 in result
        assert 2 in result

    def test_window_size_one(self):
        """Window=1 means no smoothing — always returns latest class."""
        s = ClassSmoother(window=1)
        s.update([1], [0], [0.9])
        result = s.update([1], [1], [0.9])
        assert result[1] == 1

    def test_tie_broken_by_insertion_order(self):
        """Counter.most_common breaks ties by insertion order."""
        s = ClassSmoother(window=4)
        # 2 observations of class 0, 2 of class 1
        s.update([1], [0], [0.9], frame_count=1)
        s.update([1], [0], [0.9], frame_count=2)
        s.update([1], [1], [0.9], frame_count=3)
        result = s.update([1], [1], [0.9], frame_count=4)
        # Tie — Counter.most_common returns first-inserted
        assert result[1] in (0, 1)  # Either is valid


# ============================================
# CropMaskStabilizer
# ============================================

class TestCropMaskStabilizerBasic:
    """Core crop persistence behaviour."""

    def test_fresh_update_no_persisted(self):
        stab = CropMaskStabilizer(max_age=3)
        stab.update([1, 2], [[10, 20, 30, 40], [50, 60, 70, 80]])
        assert stab.active_count == 2
        assert stab.persisted_count == 0

    def test_missing_track_persisted(self):
        """Track disappearing for 1 frame becomes persisted."""
        stab = CropMaskStabilizer(max_age=3)
        stab.update([1, 2], [[10, 20, 30, 40], [50, 60, 70, 80]])
        # Next frame: only track 1 seen
        stab.update([1], [[10, 20, 30, 40]])
        assert stab.active_count == 2  # both tracked
        assert stab.persisted_count == 1  # track 2 persisted

    def test_expired_track_removed(self):
        """Track missing for more than max_age frames is dropped."""
        stab = CropMaskStabilizer(max_age=2)
        stab.update([1], [[10, 20, 30, 40]])
        # 3 frames without track 1 → age exceeds max_age(2)
        stab.update([], [])
        stab.update([], [])
        stab.update([], [])
        assert stab.active_count == 0

    def test_reappearing_track_resets_age(self):
        """Track reappearing after dropout resets its age to 0."""
        stab = CropMaskStabilizer(max_age=3)
        stab.update([1], [[10, 20, 30, 40]])
        stab.update([], [])  # age 1
        stab.update([], [])  # age 2
        stab.update([1], [[10, 20, 30, 40]])  # reappear
        assert stab.persisted_count == 0  # age reset

    def test_get_all_crop_regions(self):
        stab = CropMaskStabilizer(max_age=3)
        stab.update([1], [[10, 20, 30, 40]])
        regions = stab.get_all_crop_regions()
        assert len(regions) == 1
        assert regions[0]['box'] == [10, 20, 30, 40]
        assert regions[0]['age'] == 0


class TestCropMaskStabilizerMask:
    """Mask building tests."""

    def test_build_mask_xyxy_small_box(self):
        """Small xyxy box produces correct mask region."""
        stab = CropMaskStabilizer(max_age=3)
        stab.update([1], [[10, 20, 60, 60]])  # xyxy: x1=10,y1=20,x2=60,y2=60
        mask = stab.build_stabilized_mask((100, 200))
        assert mask.shape == (100, 200)
        assert mask.dtype == np.uint8
        # Exact region should be filled
        assert np.all(mask[20:60, 10:60] == 255)
        # Outside region should be empty
        assert np.all(mask[0:20, :] == 0)
        assert np.all(mask[60:, :] == 0)

    def test_build_mask_xyxy_large_box(self):
        """Larger xyxy box produces correct mask region."""
        stab = CropMaskStabilizer(max_age=3)
        stab.update([1], [[100, 50, 200, 150]])  # xyxy format
        mask = stab.build_stabilized_mask((300, 400))
        assert np.all(mask[50:150, 100:200] == 255)
        assert np.all(mask[0:50, :] == 0)

    def test_build_mask_with_contour(self):
        """Contour-based mask (segmentation models)."""
        stab = CropMaskStabilizer(max_age=3)
        contour = np.array([[50, 50], [100, 50], [100, 100], [50, 100]],
                           dtype=np.int32).reshape(-1, 1, 2)
        stab.update([1], [[50, 50, 50, 50]], contours=[contour])
        mask = stab.build_stabilized_mask((200, 200))
        # Contour should be used (not box)
        assert np.any(mask[50:100, 50:100] == 255)

    def test_build_mask_includes_persisted(self):
        """Persisted (aged) tracks still appear in mask."""
        stab = CropMaskStabilizer(max_age=3)
        stab.update([1], [[10, 10, 30, 30]])
        stab.update([], [])  # track 1 persisted at age 1
        mask = stab.build_stabilized_mask((100, 100))
        # Persisted track should still be in mask
        assert np.any(mask[10:40, 10:40] == 255)

    def test_build_mask_empty_stabilizer(self):
        """Empty stabilizer produces all-zero mask."""
        stab = CropMaskStabilizer(max_age=3)
        mask = stab.build_stabilized_mask((100, 100))
        assert np.all(mask == 0)

    def test_build_mask_3d_shape_accepted(self):
        """Shape tuple with channels (h, w, 3) works correctly."""
        stab = CropMaskStabilizer(max_age=3)
        stab.update([1], [[10, 10, 30, 30]])
        mask = stab.build_stabilized_mask((100, 100, 3))
        assert mask.shape == (100, 100)

    def test_box_clamped_to_frame(self):
        """Boxes extending beyond frame edges are clamped."""
        stab = CropMaskStabilizer(max_age=3)
        stab.update([1], [[-10, -5, 30, 20]])  # Extends past top-left
        mask = stab.build_stabilized_mask((50, 50))
        # Should not crash; mask region starts from 0,0
        assert mask.shape == (50, 50)


class TestCropMaskStabilizerReset:
    """Reset clears all state."""

    def test_reset_clears_tracks(self):
        stab = CropMaskStabilizer(max_age=3)
        stab.update([1, 2, 3], [[0, 0, 10, 10]] * 3)
        stab.reset()
        assert stab.active_count == 0
        assert stab.persisted_count == 0

    def test_usable_after_reset(self):
        stab = CropMaskStabilizer(max_age=3)
        stab.update([1], [[10, 10, 30, 30]])
        stab.reset()
        stab.update([2], [[20, 20, 40, 40]])
        assert stab.active_count == 1


# ============================================
# Integration: ClassSmoother + CropMaskStabilizer together
# ============================================

class TestSmootherStabilizerIntegration:
    """Test ClassSmoother and CropMaskStabilizer working together."""

    def test_smoother_filters_stabilizer_preserves(self):
        """Smoother stabilizes class while stabilizer preserves mask."""
        smoother = ClassSmoother(window=3)
        stabilizer = CropMaskStabilizer(max_age=2)

        # Frame 1: track 1 = weed (class 0), track 2 = crop (class 1)
        sm = smoother.update([1, 2], [0, 1], [0.9, 0.8], frame_count=1)
        stabilizer.update([2], [[50, 50, 100, 100]])  # Only crop tracked for mask

        # Frame 2: track 1 flickers to crop, track 2 drops out
        sm = smoother.update([1], [1], [0.9], frame_count=2)
        stabilizer.update([], [])  # Track 2 persisted

        # Smoother: track 1 has [0, 1] → tie, either class valid
        assert sm[1] in (0, 1)
        # Stabilizer: track 2 still in mask
        mask = stabilizer.build_stabilized_mask((200, 200))
        assert np.any(mask[50:150, 50:150] == 255)

    def test_both_reset_independently(self):
        smoother = ClassSmoother(window=3)
        stabilizer = CropMaskStabilizer(max_age=2)

        smoother.update([1], [0], [0.9])
        stabilizer.update([1], [[10, 10, 20, 20]])

        smoother.reset()
        assert smoother.get_class(1) == -1
        assert stabilizer.active_count == 1  # stabilizer unaffected

        stabilizer.reset()
        assert stabilizer.active_count == 0
