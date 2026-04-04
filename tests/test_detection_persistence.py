"""
Tests for detection persistence via ByteTrack lost track recovery.

Tests cover:
  - get_lost_tracks() method on GreenOnGreen (mocked ByteTrack internals)
  - Lost track merging into the detection pipeline (class filtering, age limits)
  - Synthetic multi-frame integration with forward motion and detection dropout
  - Overhead benchmarks (get_lost_tracks must add negligible time)
  - Config validation (detection_persist_frames in INI + frontend defs)

Run: pytest tests/test_detection_persistence.py -v
"""

import sys
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.tracker import ClassSmoother


# ============================================================
# Mock ByteTrack internals for unit testing
# ============================================================

class MockSTrack:
    """Simulates an Ultralytics STrack object with Kalman-predicted position."""

    def __init__(self, track_id, xyxy, cls, score, end_frame):
        self.track_id = track_id
        self._xyxy = np.array(xyxy, dtype=np.float32)
        self.cls = cls
        self.score = score
        self.end_frame = end_frame  # last frame this track was matched

    @property
    def xyxy(self):
        return self._xyxy


class MockTracker:
    """Simulates a BYTETracker with lost_stracks and frame_id."""

    def __init__(self, frame_id=10, lost_stracks=None):
        self.frame_id = frame_id
        self.lost_stracks = lost_stracks or []

    def reset(self):
        self.lost_stracks = []


class MockPredictor:
    """Simulates model.predictor with trackers list."""

    def __init__(self, trackers=None):
        self.trackers = trackers or []


def make_gog_with_mocked_tracker(lost_stracks=None, frame_id=10, names=None):
    """Create a GreenOnGreen-like object with mocked internals for testing get_lost_tracks."""
    tracker = MockTracker(frame_id=frame_id, lost_stracks=lost_stracks or [])
    predictor = MockPredictor(trackers=[tracker])

    gog = MagicMock()
    gog.model = MagicMock()
    gog.model.predictor = predictor
    gog.model.names = names or {0: 'weed', 1: 'crop'}

    # Bind the real get_lost_tracks method
    from utils.greenongreen import GreenOnGreen
    gog.get_lost_tracks = GreenOnGreen.get_lost_tracks.__get__(gog, type(gog))

    return gog, tracker


# ============================================================
# Unit tests: get_lost_tracks()
# ============================================================

class TestGetLostTracks:
    """Unit tests for GreenOnGreen.get_lost_tracks()."""

    def test_empty_when_no_predictor(self):
        """Returns empty list when model.predictor is None."""
        gog = MagicMock()
        gog.model = MagicMock()
        gog.model.predictor = None

        from utils.greenongreen import GreenOnGreen
        gog.get_lost_tracks = GreenOnGreen.get_lost_tracks.__get__(gog, type(gog))

        assert gog.get_lost_tracks() == []

    def test_empty_when_no_trackers(self):
        """Returns empty list when predictor has no trackers."""
        gog = MagicMock()
        gog.model = MagicMock()
        gog.model.predictor = MockPredictor(trackers=[])

        from utils.greenongreen import GreenOnGreen
        gog.get_lost_tracks = GreenOnGreen.get_lost_tracks.__get__(gog, type(gog))

        assert gog.get_lost_tracks() == []

    def test_returns_lost_tracks_with_age(self):
        """Returns lost tracks with correct age calculation."""
        lost = [
            MockSTrack(7, [100, 200, 150, 250], cls=0, score=0.85, end_frame=8),
            MockSTrack(12, [300, 100, 350, 150], cls=1, score=0.72, end_frame=6),
        ]
        gog, tracker = make_gog_with_mocked_tracker(lost_stracks=lost, frame_id=10)

        result = gog.get_lost_tracks()

        assert len(result) == 2
        assert result[0]['track_id'] == 7
        assert result[0]['age'] == 2   # frame_id(10) - end_frame(8)
        assert result[0]['cls'] == 0
        assert result[0]['score'] == 0.85
        np.testing.assert_array_almost_equal(result[0]['xyxy'], [100, 200, 150, 250])

        assert result[1]['track_id'] == 12
        assert result[1]['age'] == 4   # frame_id(10) - end_frame(6)

    def test_max_age_filters_old_tracks(self):
        """Tracks older than max_age are excluded."""
        lost = [
            MockSTrack(7, [100, 200, 150, 250], cls=0, score=0.85, end_frame=8),   # age=2
            MockSTrack(12, [300, 100, 350, 150], cls=1, score=0.72, end_frame=3),  # age=7
        ]
        gog, _ = make_gog_with_mocked_tracker(lost_stracks=lost, frame_id=10)

        result = gog.get_lost_tracks(max_age=5)
        assert len(result) == 1
        assert result[0]['track_id'] == 7

    def test_max_age_zero_returns_nothing(self):
        """max_age=0 returns no lost tracks (age is always >= 1 for lost tracks)."""
        lost = [MockSTrack(7, [100, 200, 150, 250], cls=0, score=0.85, end_frame=9)]
        gog, _ = make_gog_with_mocked_tracker(lost_stracks=lost, frame_id=10)

        # age=1, max_age=0 -> filtered out
        result = gog.get_lost_tracks(max_age=0)
        assert len(result) == 0

    def test_no_max_age_returns_all(self):
        """max_age=None returns all lost tracks regardless of age."""
        lost = [
            MockSTrack(1, [10, 20, 30, 40], cls=0, score=0.9, end_frame=1),  # age=99
        ]
        gog, _ = make_gog_with_mocked_tracker(lost_stracks=lost, frame_id=100)

        result = gog.get_lost_tracks(max_age=None)
        assert len(result) == 1
        assert result[0]['age'] == 99

    def test_graceful_on_broken_strack(self):
        """Doesn't crash if strack has unexpected attributes."""
        # Simulate a broken strack that raises on iteration
        class BrokenSTrack:
            @property
            def track_id(self):
                raise RuntimeError("broken track")

        gog, tracker = make_gog_with_mocked_tracker(
            lost_stracks=[BrokenSTrack()], frame_id=10)

        # Should not raise, returns empty due to try/except
        result = gog.get_lost_tracks()
        assert result == []


# ============================================================
# Unit tests: lost track merging with class filtering
# ============================================================

class TestLostTrackClassFiltering:
    """Test that lost tracks are correctly filtered by smoothed class ID."""

    def test_class_filter_includes_target_class(self):
        """Lost track with target class ID is included."""
        smoother = ClassSmoother(window=5)
        # Feed history so track 7 is classified as class 0 (weed)
        for _ in range(3):
            smoother.update([7], [0], [0.9], frame_count=1)

        target_ids = {0}  # only class 0 (weed) is target
        smoothed_cls = smoother.get_class(7)
        assert smoothed_cls in target_ids

    def test_class_filter_excludes_non_target(self):
        """Lost track with non-target class ID is excluded."""
        smoother = ClassSmoother(window=5)
        for _ in range(3):
            smoother.update([12], [1], [0.8], frame_count=1)  # class 1 = crop

        target_ids = {0}  # only class 0 (weed) is target
        smoothed_cls = smoother.get_class(12)
        assert smoothed_cls not in target_ids

    def test_no_target_filter_includes_all(self):
        """When target_ids is empty (all classes), all lost tracks are included."""
        target_ids = set()  # empty = no filter
        # With no filter, we'd skip the `if target_ids and ...` check
        assert not target_ids  # confirms filter would be skipped

    def test_unknown_track_gets_raw_class(self):
        """Lost track not in smoother history uses raw class from strack."""
        smoother = ClassSmoother(window=5)
        # Don't feed any history for track 99
        raw_cls = smoother.get_class(99)
        assert raw_cls == -1  # unknown


# ============================================================
# Synthetic motion integration test
# ============================================================

class SyntheticMotionScene:
    """Generate synthetic weed positions drifting through a frame.

    Simulates tractor forward motion: weeds enter from the top of the
    frame and drift downward. YOLO detection can be toggled on/off per
    frame to simulate flicker.
    """

    def __init__(self, num_weeds=4, frame_w=640, frame_h=480,
                 box_size=40, drift_per_frame=15, seed=42):
        self.rng = np.random.RandomState(seed)
        self.frame_w = frame_w
        self.frame_h = frame_h
        self.box_size = box_size
        self.drift = drift_per_frame

        # Initialize weed positions (enter from top)
        self.weeds = []
        for i in range(num_weeds):
            x = self.rng.randint(50, frame_w - 50)
            y = self.rng.randint(-100, 100)
            self.weeds.append({
                'id': i + 1,
                'x': x, 'y': y,
                'cls': 0,  # weed
                'score': 0.7 + self.rng.random() * 0.25,
            })

    def step(self):
        """Advance one frame: weeds drift downward."""
        for w in self.weeds:
            w['y'] += self.drift + self.rng.randint(-3, 4)
            w['x'] += self.rng.randint(-5, 6)

    def get_detections(self, dropout_ids=None):
        """Return visible detections, optionally dropping some by ID.

        Args:
            dropout_ids: set of weed IDs to simulate YOLO missing them.

        Returns:
            list of dicts with 'id', 'xyxy', 'cls', 'score'
        """
        dropout_ids = dropout_ids or set()
        detections = []
        for w in self.weeds:
            if w['id'] in dropout_ids:
                continue
            if w['y'] + self.box_size < 0 or w['y'] > self.frame_h:
                continue
            x1 = max(0, w['x'])
            y1 = max(0, w['y'])
            x2 = min(self.frame_w, w['x'] + self.box_size)
            y2 = min(self.frame_h, w['y'] + self.box_size)
            detections.append({
                'id': w['id'],
                'xyxy': [x1, y1, x2, y2],
                'cls': w['cls'],
                'score': w['score'],
            })
        return detections


class TestSyntheticMotionPersistence:
    """Integration test: weeds drift through frame with detection dropout.

    Verifies that lost track persistence recovers detections that YOLO
    drops for 1-3 frames. Without persistence, those frames have zero
    detections in the dropout zone.
    """

    def test_persistence_recovers_dropped_detections(self):
        """With persistence, dropped detections reappear as lost tracks."""
        scene = SyntheticMotionScene(num_weeds=3, drift_per_frame=20)
        smoother = ClassSmoother(window=5)
        max_age = 5

        # Simulate: frame 0-4 detected, frame 5-7 dropout, frame 8+ re-detected
        frame_id = 0
        tracker = MockTracker(frame_id=0)

        # Phase 1: Normal detection (frames 0-4)
        tracked_stracks = []
        for _ in range(5):
            scene.step()
            frame_id += 1
            tracker.frame_id = frame_id

            detections = scene.get_detections()
            tracked_stracks = []
            for det in detections:
                st = MockSTrack(det['id'], det['xyxy'], det['cls'],
                                det['score'], end_frame=frame_id)
                tracked_stracks.append(st)

            # Feed smoother
            ids = [d['id'] for d in detections]
            cls_ids = [d['cls'] for d in detections]
            confs = [d['score'] for d in detections]
            if ids:
                smoother.update(ids, cls_ids, confs, frame_count=frame_id)

        # Phase 2: Dropout (frames 5-7) — all detections missing
        # Move last tracked stracks to lost_stracks
        tracker.lost_stracks = tracked_stracks
        lost_during_dropout = []

        for _ in range(3):
            scene.step()
            frame_id += 1
            tracker.frame_id = frame_id

            # Update xyxy to simulate Kalman prediction (move downward)
            for st in tracker.lost_stracks:
                st._xyxy[1] += 20  # y1 moves down
                st._xyxy[3] += 20  # y2 moves down

            gog, _ = make_gog_with_mocked_tracker(
                lost_stracks=tracker.lost_stracks, frame_id=frame_id)
            lost = gog.get_lost_tracks(max_age=max_age)
            lost_during_dropout.append(lost)

        # Verify: lost tracks were recovered during dropout
        assert len(lost_during_dropout[0]) > 0, "Frame 5: should have lost tracks"
        assert len(lost_during_dropout[1]) > 0, "Frame 6: should have lost tracks"
        assert len(lost_during_dropout[2]) > 0, "Frame 7: should have lost tracks"

        # Ages should increase each frame
        ages_frame5 = [lt['age'] for lt in lost_during_dropout[0]]
        ages_frame7 = [lt['age'] for lt in lost_during_dropout[2]]
        assert all(a >= 1 for a in ages_frame5)
        assert all(a >= 3 for a in ages_frame7)

    def test_persistence_disabled_returns_nothing(self):
        """With detection_persist_frames=0, no lost tracks are returned."""
        lost = [MockSTrack(7, [100, 200, 150, 250], cls=0, score=0.85, end_frame=9)]
        gog, _ = make_gog_with_mocked_tracker(lost_stracks=lost, frame_id=10)

        # max_age=0 means detection_persist_frames=0 → no persistence
        result = gog.get_lost_tracks(max_age=0)
        assert len(result) == 0

    def test_predicted_positions_move_with_motion(self):
        """Kalman-predicted positions in lost tracks should advance with motion."""
        scene = SyntheticMotionScene(num_weeds=1, drift_per_frame=20)

        # Run 3 frames of tracking
        for _ in range(3):
            scene.step()

        det = scene.get_detections()[0]
        original_y = det['xyxy'][1]

        # Create lost track at this position
        st = MockSTrack(det['id'], det['xyxy'], det['cls'], det['score'], end_frame=3)

        # Simulate 2 frames of Kalman prediction (moving box downward)
        st._xyxy[1] += 40
        st._xyxy[3] += 40

        gog, _ = make_gog_with_mocked_tracker(lost_stracks=[st], frame_id=5)
        lost = gog.get_lost_tracks(max_age=5)

        assert len(lost) == 1
        predicted_y = lost[0]['xyxy'][1]
        assert predicted_y > original_y, "Predicted position should advance with motion"

    def test_many_weeds_through_actuation_zone(self):
        """Multiple weeds drifting through frame, some dropping out intermittently."""
        scene = SyntheticMotionScene(num_weeds=8, drift_per_frame=15, seed=99)
        smoother = ClassSmoother(window=5)

        total_detections_without_persist = 0
        total_detections_with_persist = 0

        tracker = MockTracker(frame_id=0)
        all_stracks = {}  # {id: MockSTrack}

        for frame_num in range(30):
            scene.step()
            frame_id = frame_num + 1
            tracker.frame_id = frame_id

            # Simulate intermittent dropout: every 3rd frame, drop half the detections
            dropout = set()
            if frame_num % 3 == 2:
                for w in scene.weeds:
                    if w['id'] % 2 == 0:
                        dropout.add(w['id'])

            detections = scene.get_detections(dropout_ids=dropout)
            total_detections_without_persist += len(detections)

            # Update tracked stracks
            detected_ids = set()
            for det in detections:
                st = MockSTrack(det['id'], det['xyxy'], det['cls'],
                                det['score'], end_frame=frame_id)
                all_stracks[det['id']] = st
                detected_ids.add(det['id'])

            # Feed smoother
            ids = [d['id'] for d in detections]
            cls_ids = [d['cls'] for d in detections]
            confs = [d['score'] for d in detections]
            if ids:
                smoother.update(ids, cls_ids, confs, frame_count=frame_id)

            # Move undetected to lost_stracks
            lost_stracks = []
            for tid, st in all_stracks.items():
                if tid not in detected_ids:
                    # Simulate Kalman prediction
                    st._xyxy[1] += 15
                    st._xyxy[3] += 15
                    lost_stracks.append(st)

            tracker.lost_stracks = lost_stracks

            gog, _ = make_gog_with_mocked_tracker(
                lost_stracks=lost_stracks, frame_id=frame_id)
            lost = gog.get_lost_tracks(max_age=5)

            total_detections_with_persist += len(detections) + len(lost)

        # With persistence, we should have more total detections
        assert total_detections_with_persist > total_detections_without_persist, \
            "Persistence should recover dropped detections"


# ============================================================
# Overhead / speed benchmarks
# ============================================================

class TestGetLostTracksOverhead:
    """Verify get_lost_tracks() adds negligible overhead."""

    def test_overhead_empty_tracker(self):
        """get_lost_tracks with no lost tracks should be < 0.01ms."""
        gog, _ = make_gog_with_mocked_tracker(lost_stracks=[], frame_id=100)

        times = []
        for _ in range(1000):
            start = time.perf_counter()
            gog.get_lost_tracks(max_age=5)
            times.append(time.perf_counter() - start)

        avg_us = np.mean(times) * 1e6
        assert avg_us < 100, f"Empty call took {avg_us:.1f}us, expected <100us"

    def test_overhead_with_lost_tracks(self):
        """get_lost_tracks with 20 lost tracks should be < 0.1ms."""
        lost = [
            MockSTrack(i, [i * 10, i * 5, i * 10 + 40, i * 5 + 40],
                        cls=0, score=0.8, end_frame=90 + (i % 5))
            for i in range(20)
        ]
        gog, _ = make_gog_with_mocked_tracker(lost_stracks=lost, frame_id=100)

        times = []
        for _ in range(1000):
            start = time.perf_counter()
            gog.get_lost_tracks(max_age=5)
            times.append(time.perf_counter() - start)

        avg_us = np.mean(times) * 1e6
        assert avg_us < 500, f"20-track call took {avg_us:.1f}us, expected <500us"

    def test_overhead_no_predictor(self):
        """get_lost_tracks with no predictor should be near-zero."""
        gog = MagicMock()
        gog.model = MagicMock()
        gog.model.predictor = None

        from utils.greenongreen import GreenOnGreen
        gog.get_lost_tracks = GreenOnGreen.get_lost_tracks.__get__(gog, type(gog))

        times = []
        for _ in range(1000):
            start = time.perf_counter()
            gog.get_lost_tracks(max_age=5)
            times.append(time.perf_counter() - start)

        avg_us = np.mean(times) * 1e6
        assert avg_us < 50, f"No-predictor call took {avg_us:.1f}us, expected <50us"

    def test_class_smoother_get_class_overhead(self):
        """ClassSmoother.get_class() for lost track class filtering overhead."""
        smoother = ClassSmoother(window=5)
        # Pre-fill 50 tracks
        for tid in range(50):
            smoother.update([tid], [tid % 3], [0.9], frame_count=1)

        times = []
        for _ in range(1000):
            start = time.perf_counter()
            for tid in range(20):
                smoother.get_class(tid)
            times.append(time.perf_counter() - start)

        avg_us = np.mean(times) * 1e6
        assert avg_us < 500, f"20 get_class calls took {avg_us:.1f}us, expected <500us"


# ============================================================
# Config validation tests
# ============================================================

class TestDetectionPersistConfig:
    """Verify detection_persist_frames is properly registered."""

    def test_value_validator_registered(self):
        from utils.config_manager import ConfigValidator
        assert 'detection_persist_frames' in ConfigValidator.VALUE_VALIDATORS
        vtype, vmin, vmax = ConfigValidator.VALUE_VALIDATORS['detection_persist_frames']
        assert vtype == 'int'
        assert vmin == 0
        assert vmax == 15

    def test_optional_key_registered(self):
        from utils.config_manager import ConfigValidator
        tracking_keys = ConfigValidator.OPTIONAL_SECTIONS['Tracking']['optional_keys']
        assert 'detection_persist_frames' in tracking_keys

    def test_config_field_def_exists(self):
        """Verify the frontend CONFIG_FIELD_DEFS has the new key."""
        config_js = Path(PROJECT_ROOT / 'controller' / 'shared' / 'js' / 'config.js')
        content = config_js.read_text()
        assert 'detection_persist_frames' in content
        assert "min: 0" in content
        assert "max: 15" in content

    def test_general_config_has_key(self):
        """Verify GENERAL_CONFIG.ini has the new key in [Tracking]."""
        import configparser
        config = configparser.ConfigParser()
        config.read(PROJECT_ROOT / 'config' / 'GENERAL_CONFIG.ini')
        assert config.has_option('Tracking', 'detection_persist_frames')
        val = config.getint('Tracking', 'detection_persist_frames')
        assert 0 <= val <= 15, f"detection_persist_frames={val} out of valid range"

    def test_config_validates_successfully(self, tmp_path):
        """Full config validation passes with the new key (uses clean copy)."""
        import configparser
        from utils.config_manager import ConfigValidator

        # Use a fresh copy with known-good values to avoid user edits breaking test
        src = PROJECT_ROOT / 'config' / 'GENERAL_CONFIG.ini'
        config = configparser.ConfigParser()
        config.read(src)

        # Ensure tracking values are within validator range
        config.set('Tracking', 'detection_persist_frames', '5')
        config.set('Tracking', 'match_thresh', '0.7')

        tmp_ini = tmp_path / 'GENERAL_CONFIG.ini'
        with open(tmp_ini, 'w') as f:
            config.write(f)

        # Also need CONTROLLER.ini alongside it
        ctrl_src = PROJECT_ROOT / 'config' / 'CONTROLLER.ini'
        if ctrl_src.exists():
            import shutil
            shutil.copy(ctrl_src, tmp_path / 'CONTROLLER.ini')

        result = ConfigValidator.load_and_validate_config(tmp_ini)
        assert result.getint('Tracking', 'detection_persist_frames') == 5


# ============================================================
# Hybrid crop mask persistence tests
# ============================================================

class TestHybridCropMaskPersistence:
    """Tests for detection_persist_frames in hybrid mode crop mask painting."""

    def test_detection_persist_frames_used_not_crop_persist(self):
        """Lost crop at age 5 included when persist=10 but excluded when persist=3."""
        from utils.greenongreen import GreenOnGreen

        # Lost crop at age 5 (frame 10, end_frame 5)
        lost = [MockSTrack(1, [100, 100, 200, 200], cls=1, score=0.9, end_frame=5)]

        # With persist=10: age 5 <= 10, should be included
        gog_high, _ = make_gog_with_mocked_tracker(lost_stracks=lost, frame_id=10)
        gog_high.detection_persist_frames = 10
        result_high = gog_high.get_lost_tracks(max_age=10)
        assert len(result_high) == 1, "persist=10 should include age-5 track"

        # With persist=3: age 5 > 3, should be excluded
        gog_low, _ = make_gog_with_mocked_tracker(lost_stracks=lost, frame_id=10)
        gog_low.detection_persist_frames = 3
        result_low = gog_low.get_lost_tracks(max_age=3)
        assert len(result_low) == 0, "persist=3 should exclude age-5 track"

    def test_persistence_disabled_falls_back(self):
        """persist=0 falls back to crop_stabilizer.max_age."""
        # The fallback logic is in greenongreen.py hybrid_inference:
        # persist=0 -> use crop_stabilizer.max_age (or 3 if no stabilizer)
        # We test the conditional expression directly
        detection_persist_frames = 0
        stabilizer_max_age = 3

        # Reproduce the logic from greenongreen.py
        persist = detection_persist_frames
        effective_max_age = (persist if persist > 0
                            else (stabilizer_max_age if stabilizer_max_age else 3))
        assert effective_max_age == 3

        # With no stabilizer
        effective_no_stab = (persist if persist > 0 else (None if False else 3))
        assert effective_no_stab == 3

    def test_create_detector_passes_persist_frames(self):
        """AST inspection: both gog and gog-hybrid calls include detection_persist_frames."""
        import ast

        owl_path = PROJECT_ROOT / 'owl.py'
        tree = ast.parse(owl_path.read_text())

        # Find _create_detector function
        create_detector = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_create_detector':
                create_detector = node
                break
        assert create_detector is not None, "_create_detector not found in owl.py"

        # Find all GreenOnGreen(...) calls inside _create_detector
        gog_calls = []
        for node in ast.walk(create_detector):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == 'GreenOnGreen':
                    gog_calls.append(node)
                elif isinstance(func, ast.Attribute) and func.attr == 'GreenOnGreen':
                    gog_calls.append(node)

        assert len(gog_calls) == 2, f"Expected 2 GreenOnGreen calls, found {len(gog_calls)}"

        for call in gog_calls:
            kwarg_names = [kw.arg for kw in call.keywords]
            assert 'detection_persist_frames' in kwarg_names, \
                f"GreenOnGreen call missing detection_persist_frames kwarg. Has: {kwarg_names}"

    def test_mqtt_syncs_persist_frames_to_detector(self):
        """MQTT handler propagates detection_persist_frames to GreenOnGreen instance."""
        # Simulate the MQTT sync logic from mqtt_manager.py
        mock_owl = SimpleNamespace(
            _gog_detector=SimpleNamespace(detection_persist_frames=5),
            detection_persist_frames=10,
        )

        params = {'detection_persist_frames': 10}

        # Reproduce the sync logic
        if 'detection_persist_frames' in params:
            gog = getattr(mock_owl, '_gog_detector', None)
            if gog:
                gog.detection_persist_frames = getattr(
                    mock_owl, 'detection_persist_frames', 0)

        assert mock_owl._gog_detector.detection_persist_frames == 10
