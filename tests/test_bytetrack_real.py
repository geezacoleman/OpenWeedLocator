"""
Real-ByteTrack integration tests with rendered synthetic frames.

Unlike test_tracking_integration.py (which feeds ground-truth IDs straight
into ClassSmoother), these tests drive the REAL ultralytics BYTETracker,
configured from config/bytetrack_owl.yaml, with detections derived from
rendered frames of moving objects — tractor motion, camera vibration,
detection dropouts and class flicker.

Covers the review findings:
  - ID stability with the shipped yaml params under vibration
  - Lost tracks persist with MOVING Kalman-predicted boxes (not frozen)
  - The zero-track window exists in real ByteTrack (motivates the
    class-filter passthrough fix in GreenOnGreen.inference)
  - Regression: zero-track frames must not leak non-target classes
  - Regression: update_tracker_params_direct must actually change
    tracker.args (BYTETracker reads thresholds from args, not attributes)
  - get_lost_tracks() against a real BYTETracker
  - Per-frame tracking overhead sanity

Annotated frames are written to tests/tracking_output/real_bytetrack/
for manual review (gitignored).

Run: pytest tests/test_bytetrack_real.py -v
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

cv2 = pytest.importorskip('cv2')
pytest.importorskip('ultralytics')
torch = pytest.importorskip('torch')

from ultralytics.trackers.byte_tracker import BYTETracker
from ultralytics.utils import YAML, IterableSimpleNamespace
from ultralytics.engine.results import Results

from utils.tracker import ClassSmoother
from utils.greenongreen import GreenOnGreen, TRACKER_YAML

VIZ_DIR = Path(__file__).parent / 'tracking_output' / 'real_bytetrack'
TRACKER_YAML_PATH = PROJECT_ROOT / 'config' / 'bytetrack_owl.yaml'

# Pi-realistic frame rate. ultralytics hardcodes frame_rate=30 when creating
# trackers for model.track(), so max_time_lost = track_buffer there; here we
# use 30 too so buffer semantics match production.
FRAME_RATE = 30

CLASS_NAMES = {0: 'weed', 1: 'crop'}


def load_owl_tracker_cfg():
    """Load the shipped tracker yaml exactly as ultralytics does."""
    return IterableSimpleNamespace(**YAML.load(str(TRACKER_YAML_PATH)))


def make_tracker(**overrides):
    """Real BYTETracker configured from config/bytetrack_owl.yaml."""
    cfg = load_owl_tracker_cfg()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return BYTETracker(cfg, frame_rate=FRAME_RATE)


class DetBatch:
    """Wrap detections in the interface BYTETracker.update() expects.

    BYTETracker accesses: .conf, .cls, .xywh (centre format), .xyxy,
    len(), and boolean-mask indexing.
    """

    def __init__(self, xywh, conf, cls, xyxy):
        self.xywh = xywh
        self.conf = conf
        self.cls = cls
        self.xyxy = xyxy

    def __len__(self):
        return len(self.conf)

    def __getitem__(self, mask):
        return DetBatch(self.xywh[mask], self.conf[mask],
                        self.cls[mask], self.xyxy[mask])


def to_det_batch(boxes_tlwh, class_ids, confidences):
    """Convert top-left [x, y, w, h] boxes to a DetBatch."""
    if not boxes_tlwh:
        e4 = np.empty((0, 4), dtype=np.float32)
        e1 = np.empty((0,), dtype=np.float32)
        return DetBatch(e4, e1, e1.copy(), e4.copy())
    xyxy = np.array([[x, y, x + w, y + h] for x, y, w, h in boxes_tlwh],
                    dtype=np.float32)
    xywh = np.array([[x + w / 2, y + h / 2, w, h] for x, y, w, h in boxes_tlwh],
                    dtype=np.float32)
    return DetBatch(xywh,
                    np.array(confidences, dtype=np.float32),
                    np.array(class_ids, dtype=np.float32),
                    xyxy)


class FieldScene:
    """Render frames of weeds/crops moving through a paddock view.

    Models a forward-moving tractor (objects drift down-frame), camera
    vibration (global per-frame jitter), and a noisy detector (box noise,
    confidence jitter, dropouts, class flicker).
    """

    WIDTH, HEIGHT = 640, 480

    def __init__(self, num_weeds=5, num_crops=3, speed=8.0, vibration=2.5,
                 seed=7):
        self.rng = np.random.RandomState(seed)
        self.speed = speed
        self.vibration = vibration
        self.objects = []
        for i in range(num_weeds + num_crops):
            is_weed = i < num_weeds
            self.objects.append({
                'gt_id': i,
                'cls': 0 if is_weed else 1,
                'x': float(self.rng.randint(40, self.WIDTH - 80)),
                'y0': float(self.rng.randint(-self.HEIGHT, self.HEIGHT - 60)),
                'w': self.rng.randint(28, 42) if is_weed else self.rng.randint(60, 90),
                'h': self.rng.randint(24, 38) if is_weed else self.rng.randint(55, 80),
                'conf': 0.55 + self.rng.uniform(0, 0.3),
            })
        # Pre-generate per-frame camera jitter so render and detections agree
        self._jitter = {}

    def jitter(self, frame_idx):
        if frame_idx not in self._jitter:
            self._jitter[frame_idx] = self.rng.normal(0, self.vibration, 2)
        return self._jitter[frame_idx]

    def gt_boxes(self, frame_idx):
        """Ground-truth visible boxes [x, y, w, h] with camera jitter."""
        jx, jy = self.jitter(frame_idx)
        out = []
        for obj in self.objects:
            y = obj['y0'] + self.speed * frame_idx + jy
            x = obj['x'] + jx
            if y + obj['h'] < 0 or y > self.HEIGHT or x + obj['w'] < 0 \
                    or x > self.WIDTH:
                continue
            out.append({'gt_id': obj['gt_id'], 'cls': obj['cls'],
                        'conf': obj['conf'],
                        'box': [x, y, obj['w'], obj['h']]})
        return out

    def render(self, frame_idx):
        """Render a BGR frame: brown soil + green weeds + darker green crops."""
        img = np.full((self.HEIGHT, self.WIDTH, 3), (60, 95, 125), np.uint8)
        noise = self.rng.randint(-18, 18, (self.HEIGHT, self.WIDTH, 1))
        img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        for det in self.gt_boxes(frame_idx):
            x, y, w, h = det['box']
            cx, cy = int(x + w / 2), int(y + h / 2)
            colour = (60, 200, 70) if det['cls'] == 0 else (40, 130, 30)
            cv2.ellipse(img, (cx, cy), (int(w / 2), int(h / 2)), 0, 0, 360,
                        colour, -1)
            cv2.ellipse(img, (cx, cy), (int(w / 3), int(h / 3)), 45, 0, 360,
                        (50, 170, 60) if det['cls'] == 0 else (35, 110, 25), -1)
        return img

    def detections(self, frame_idx, drop_ids=None, drop_rate=0.0,
                   flicker_rate=0.0):
        """Noisy detector output for one frame.

        Returns (boxes_tlwh, class_ids, confidences, gt_ids) where gt_ids
        maps each detection row back to its ground-truth object.
        """
        boxes, cls_ids, confs, gt_ids = [], [], [], []
        for det in self.gt_boxes(frame_idx):
            if drop_ids and det['gt_id'] in drop_ids:
                continue
            if self.rng.random() < drop_rate:
                continue
            x, y, w, h = det['box']
            bx = x + self.rng.normal(0, 1.5)
            by = y + self.rng.normal(0, 1.5)
            bw = w * (1 + self.rng.normal(0, 0.04))
            bh = h * (1 + self.rng.normal(0, 0.04))
            cls = det['cls']
            if self.rng.random() < flicker_rate:
                cls = 1 - cls
            conf = float(np.clip(det['conf'] + self.rng.normal(0, 0.06),
                                 0.1, 0.99))
            boxes.append([bx, by, bw, bh])
            cls_ids.append(cls)
            confs.append(conf)
            gt_ids.append(det['gt_id'])
        return boxes, cls_ids, confs, gt_ids


def run_tracker(tracker, scene, n_frames, **det_kwargs):
    """Run the real tracker over a scene.

    Returns per-frame list of dicts:
        {'tracks': ndarray rows [x1,y1,x2,y2,tid,score,cls,idx],
         'gt_ids': det-row -> gt id mapping, 'n_dets': int}
    """
    history = []
    for f in range(n_frames):
        # det_kwargs values may be callables of frame index (for bursts)
        kwargs = {k: (v(f) if callable(v) else v) for k, v in det_kwargs.items()}
        boxes, cls_ids, confs, gt_ids = scene.detections(f, **kwargs)
        tracks = tracker.update(to_det_batch(boxes, cls_ids, confs))
        history.append({'tracks': tracks, 'gt_ids': gt_ids,
                        'n_dets': len(boxes)})
    return history


def gt_to_track_ids(history):
    """Map gt_id -> ordered list of (frame, track_id) assignments."""
    assignments = {}
    for f, entry in enumerate(history):
        for row in entry['tracks']:
            det_idx = int(row[7])
            gt_id = entry['gt_ids'][det_idx]
            assignments.setdefault(gt_id, []).append((f, int(row[4])))
    return assignments


# ============================================================
# Config / construction
# ============================================================

@pytest.mark.unit
class TestTrackerConfig:
    def test_yaml_has_all_required_keys(self):
        """bytetrack_owl.yaml must contain every key BYTETracker reads."""
        cfg = load_owl_tracker_cfg()
        for key in ('tracker_type', 'track_high_thresh', 'track_low_thresh',
                    'new_track_thresh', 'track_buffer', 'match_thresh',
                    'fuse_score'):
            assert hasattr(cfg, key), f'{key} missing from bytetrack_owl.yaml'
        assert cfg.tracker_type == 'bytetrack'

    def test_tracker_constructs_from_owl_yaml(self):
        tracker = make_tracker()
        assert tracker.max_time_lost == int(
            FRAME_RATE / 30.0 * load_owl_tracker_cfg().track_buffer)

    def test_ini_and_yaml_tracking_params_are_aligned(self):
        """GENERAL_CONFIG.ini [Tracking] must match the bootstrap yaml.

        The INI is the runtime source of truth (applied after the first
        track call); the yaml bootstraps the tracker. If they diverge the
        first frames run with different params than the rest.
        """
        import configparser
        ini = configparser.ConfigParser()
        ini.read(PROJECT_ROOT / 'config' / 'GENERAL_CONFIG.ini')
        cfg = load_owl_tracker_cfg()
        assert ini.getfloat('Tracking', 'track_high_thresh') == cfg.track_high_thresh
        assert ini.getfloat('Tracking', 'track_low_thresh') == cfg.track_low_thresh
        assert ini.getfloat('Tracking', 'new_track_thresh') == cfg.new_track_thresh
        assert ini.getint('Tracking', 'track_buffer') == cfg.track_buffer
        assert ini.getfloat('Tracking', 'match_thresh') == cfg.match_thresh


# ============================================================
# ID stability with the real tracker
# ============================================================

@pytest.mark.unit
class TestRealTrackerStability:
    def test_ids_stable_under_vibration(self):
        """Each object keeps one track ID across 80 frames of camera shake."""
        scene = FieldScene(num_weeds=5, num_crops=3, vibration=2.5)
        history = run_tracker(make_tracker(), scene, 80)
        assignments = gt_to_track_ids(history)

        total_switches = 0
        for gt_id, frames in assignments.items():
            ids = [tid for _, tid in frames]
            switches = sum(1 for a, b in zip(ids, ids[1:]) if a != b)
            total_switches += switches
        assert total_switches <= 2, (
            f'{total_switches} ID switches across 8 objects/80 frames — '
            f'tracking unstable under vibration')

    def test_track_coverage_high_on_stable_scene(self):
        """Objects should be tracked on nearly every frame they are visible."""
        scene = FieldScene(num_weeds=4, num_crops=2, vibration=2.0)
        history = run_tracker(make_tracker(), scene, 60)
        tracked = sum(len(e['tracks']) for e in history)
        detected = sum(e['n_dets'] for e in history)
        assert detected > 0
        # First-frame activations lag one frame for objects entering later;
        # allow a small deficit but no systematic loss.
        assert tracked / detected > 0.9, (
            f'only {tracked}/{detected} detections were tracked')

    def test_id_survives_dropout_within_buffer(self):
        """An object dropped for 10 frames (< buffer) keeps its track ID,
        and its lost-track Kalman box keeps MOVING during the dropout."""
        scene = FieldScene(num_weeds=1, num_crops=0, speed=6.0, vibration=1.0)
        scene.objects[0]['y0'] = 20.0  # visible for the whole test
        tracker = make_tracker()

        drop_window = range(20, 30)
        id_before, id_after = None, None
        lost_y_positions = []

        for f in range(45):
            drop = {0} if f in drop_window else None
            boxes, cls_ids, confs, gt_ids = scene.detections(f, drop_ids=drop)
            tracks = tracker.update(to_det_batch(boxes, cls_ids, confs))
            if f == 19:
                assert len(tracks) == 1
                id_before = int(tracks[0][4])
            if f in drop_window:
                assert len(tracker.lost_stracks) == 1, \
                    f'frame {f}: track not in lost_stracks during dropout'
                lost_y_positions.append(float(tracker.lost_stracks[0].xyxy[1]))
            if f == 30 and len(tracks):
                id_after = int(tracks[0][4])

        assert id_before is not None and id_after is not None
        assert id_after == id_before, (
            f'track ID changed across a 10-frame dropout '
            f'({id_before} -> {id_after}) despite track_buffer covering it')
        # Kalman prediction must continue the downward motion, not freeze
        moved = lost_y_positions[-1] - lost_y_positions[0]
        assert moved > scene.speed * (len(lost_y_positions) - 1) * 0.5, (
            f'lost-track box barely moved ({moved:.1f}px) during dropout — '
            f'Kalman prediction not advancing')

    def test_new_id_after_dropout_longer_than_buffer(self):
        """Documents expected behaviour: dropouts > track_buffer start a
        new ID (and a new spray decision) — buffer length matters."""
        scene = FieldScene(num_weeds=1, num_crops=0, speed=1.0, vibration=0.5)
        scene.objects[0]['y0'] = 50.0
        tracker = make_tracker(track_buffer=10)  # short buffer
        assert tracker.max_time_lost == 10

        ids_seen = []
        for f in range(50):
            drop = {0} if 15 <= f < 35 else None  # 20-frame dropout
            boxes, cls_ids, confs, gt_ids = scene.detections(f, drop_ids=drop)
            tracks = tracker.update(to_det_batch(boxes, cls_ids, confs))
            if len(tracks):
                ids_seen.append(int(tracks[0][4]))
        assert len(set(ids_seen)) == 2, (
            f'expected a new ID after a dropout longer than the buffer, '
            f'got IDs {set(ids_seen)}')


# ============================================================
# The zero-track window (motivates the passthrough fix)
# ============================================================

@pytest.mark.unit
class TestZeroTrackWindow:
    def test_fresh_objects_yield_zero_tracks_for_one_frame(self):
        """When all visible objects are new (entered after frame 1), the
        real ByteTrack returns ZERO tracks for one frame. ultralytics then
        keeps the RAW unfiltered detections in result.boxes — which is why
        GreenOnGreen.inference must class-filter untracked passthrough."""
        scene = FieldScene(num_weeds=1, num_crops=1, speed=0.0, vibration=0.5)
        for obj in scene.objects:
            obj['y0'] = 100.0
        tracker = make_tracker()

        # Frames 0-4: only object 0 visible. Frames 5+: object 0 gone,
        # object 1 (new) appears.
        for f in range(5):
            boxes, cls_ids, confs, _ = scene.detections(f, drop_ids={1})
            tracker.update(to_det_batch(boxes, cls_ids, confs))

        boxes, cls_ids, confs, _ = scene.detections(5, drop_ids={0})
        assert len(boxes) == 1  # the new object IS detected
        tracks = tracker.update(to_det_batch(boxes, cls_ids, confs))
        assert len(tracks) == 0, (
            'expected an unconfirmed (zero-track) frame for a brand-new '
            'object — if this fails, ByteTrack activation semantics changed')

        # Second sighting confirms the track
        boxes, cls_ids, confs, _ = scene.detections(6, drop_ids={0})
        tracks = tracker.update(to_det_batch(boxes, cls_ids, confs))
        assert len(tracks) == 1


def _bare_gog(model, detect_class_ids, tracking_enabled=True):
    """Build a GreenOnGreen without running __init__ (no model load)."""
    gog = GreenOnGreen.__new__(GreenOnGreen)
    gog.model = model
    gog.model_path = Path('synthetic')
    gog.confidence = 0.4
    gog.hybrid_mode = False
    gog.inference_resolution = 320
    gog.crop_buffer_px = 0
    gog._model_filename = 'stub'
    gog.task = 'detect'
    gog.detection_mask = None
    gog._detect_class_ids = detect_class_ids
    gog.tracking_enabled = tracking_enabled
    gog._crop_stabilizer = None
    gog.detection_persist_frames = 0
    gog._pending_tracker_params = None
    gog.last_track_ids = []
    gog.last_raw_boxes = []
    gog.last_class_ids = []
    gog.last_confidences = []
    gog._gob = None
    gog._dilate_kernel = None
    gog._executor = None
    return gog


def _make_results(img, rows):
    """Build a real ultralytics Results object.

    rows: list of [x1, y1, x2, y2, (track_id,) conf, cls] — 6 columns means
    untracked (boxes.id is None), 7 columns means tracked.
    """
    boxes = torch.tensor(rows, dtype=torch.float32) if rows else \
        torch.zeros((0, 6), dtype=torch.float32)
    return Results(orig_img=img, path='synthetic.jpg', names=CLASS_NAMES,
                   boxes=boxes)


@pytest.mark.unit
class TestZeroTrackPassthroughFilter:
    """Regression tests for the crop-spray bug: zero-track frames keep raw
    unfiltered detections and previously bypassed all class filtering."""

    IMG = np.zeros((240, 320, 3), dtype=np.uint8)

    def test_untracked_frame_filters_non_target_classes(self):
        rows = [
            [10, 10, 50, 50, 0.9, 0.0],    # weed — target
            [100, 100, 200, 200, 0.95, 1.0],  # crop — must NOT pass
            [60, 10, 90, 40, 0.8, 0.0],    # weed — target
        ]
        model = SimpleNamespace(
            track=lambda **kw: [_make_results(self.IMG, rows)],
            names=CLASS_NAMES, predictor=None)
        gog = _bare_gog(model, detect_class_ids=[0])

        contours, boxes, centres, _ = gog.inference(self.IMG)
        assert len(boxes) == 2, (
            f'zero-track passthrough leaked non-target classes: {boxes}')
        for x, y, w, h in boxes:
            assert (x, y) in ((10, 10), (60, 10))  # only the weed boxes

    def test_untracked_frame_no_filter_when_all_classes(self):
        """With no class filter configured, passthrough keeps everything."""
        rows = [[10, 10, 50, 50, 0.9, 0.0],
                [100, 100, 200, 200, 0.95, 1.0]]
        model = SimpleNamespace(
            track=lambda **kw: [_make_results(self.IMG, rows)],
            names=CLASS_NAMES, predictor=None)
        gog = _bare_gog(model, detect_class_ids=None)
        _, boxes, _, _ = gog.inference(self.IMG)
        assert len(boxes) == 2

    def test_tracked_frame_keeps_all_classes_for_smoother(self):
        """Tracked frames keep every class — owl.py's ClassSmoother filter
        handles them — and raw tracking attrs are populated."""
        rows = [[10, 10, 50, 50, 1.0, 0.9, 0.0],     # track 1, weed
                [100, 100, 200, 200, 2.0, 0.95, 1.0]]  # track 2, crop
        model = SimpleNamespace(
            track=lambda **kw: [_make_results(self.IMG, rows)],
            names=CLASS_NAMES, predictor=None)
        gog = _bare_gog(model, detect_class_ids=[0])
        _, boxes, _, _ = gog.inference(self.IMG)
        assert len(boxes) == 2
        assert gog.last_track_ids == [1, 2]
        assert gog.last_class_ids == [0, 1]


# ============================================================
# Runtime param application (regression for the no-op bug)
# ============================================================

@pytest.mark.unit
class TestTrackerParamApplication:
    def _gog_with_live_tracker(self):
        tracker = make_tracker()
        model = SimpleNamespace(
            predictor=SimpleNamespace(trackers=[tracker]),
            names=CLASS_NAMES)
        gog = _bare_gog(model, detect_class_ids=[0])
        return gog, tracker

    def test_direct_params_reach_tracker_args(self):
        """BYTETracker reads thresholds from tracker.args — setattr on the
        tracker instance (the old code) silently did nothing."""
        gog, tracker = self._gog_with_live_tracker()
        gog.update_tracker_params_direct({
            'track_high_thresh': 0.33, 'match_thresh': 0.91,
            'track_buffer': 90,
        })
        assert tracker.args.track_high_thresh == 0.33
        assert tracker.args.match_thresh == 0.91
        assert tracker.args.track_buffer == 90
        assert tracker.max_time_lost == 90

    def test_param_update_does_not_wipe_active_tracks(self):
        """The old code called tracker.reset() — destroying every active
        track (and restarting the global ID counter) on each UI change."""
        gog, tracker = self._gog_with_live_tracker()
        scene = FieldScene(num_weeds=2, num_crops=0, speed=0.0)
        for obj in scene.objects:
            obj['y0'] = 100.0
        for f in range(5):
            boxes, cls_ids, confs, _ = scene.detections(f)
            tracker.update(to_det_batch(boxes, cls_ids, confs))
        n_before = len(tracker.tracked_stracks)
        assert n_before > 0

        gog.update_tracker_params_direct({'match_thresh': 0.85})
        assert len(tracker.tracked_stracks) == n_before, \
            'param update wiped active tracks'

    def test_stability_preset_applies(self):
        gog, tracker = self._gog_with_live_tracker()
        gog.update_tracker_params('high')
        preset = GreenOnGreen.TRACK_STABILITY_PRESETS['high']
        assert tracker.args.match_thresh == preset['match_thresh']
        assert tracker.max_time_lost == preset['track_buffer']

    def test_params_queue_until_tracker_exists(self):
        """Params sent before the first track call must apply right after
        the tracker is created, not vanish."""
        tracker = make_tracker()
        rows = [[10, 10, 50, 50, 1.0, 0.9, 0.0]]
        img = np.zeros((240, 320, 3), dtype=np.uint8)
        model = SimpleNamespace(
            track=lambda **kw: [_make_results(img, rows)],
            names=CLASS_NAMES, predictor=None)
        gog = _bare_gog(model, detect_class_ids=[0])

        gog.update_tracker_params_direct({'match_thresh': 0.93})
        assert gog._pending_tracker_params == {'match_thresh': 0.93}

        # Tracker appears (as it does after ultralytics' on_predict_start)
        model.predictor = SimpleNamespace(trackers=[tracker])
        gog.inference(img)
        assert tracker.args.match_thresh == 0.93
        assert gog._pending_tracker_params is None

    def test_preset_values_match_ui_presets(self):
        """Python presets must stay in sync with the JS UI presets."""
        import re
        for js_file in (
            PROJECT_ROOT / 'controller/standalone/static/js/modules/_controls.js',
            PROJECT_ROOT / 'controller/networked/static/js/modules/_controls.js',
        ):
            text = js_file.read_text(encoding='utf-8')
            m = re.search(r'TRACK_STABILITY_PRESETS = \{(.*?)\};', text,
                          re.DOTALL)
            assert m, f'TRACK_STABILITY_PRESETS not found in {js_file.name}'
            block = m.group(1)
            for level, preset in GreenOnGreen.TRACK_STABILITY_PRESETS.items():
                line = re.search(level + r':\s*\{([^}]*)\}', block)
                assert line, f'{level} preset missing in {js_file.name}'
                js_vals = dict(re.findall(r'(\w+):\s*([\d.]+)', line.group(1)))
                for key, val in preset.items():
                    assert float(js_vals[key]) == float(val), (
                        f'{js_file.name} {level}.{key} = {js_vals[key]} but '
                        f'Python preset has {val}')


# ============================================================
# get_lost_tracks against a real tracker
# ============================================================

@pytest.mark.unit
class TestGetLostTracksReal:
    def test_lost_tracks_exposed_with_ages(self):
        tracker = make_tracker()
        model = SimpleNamespace(
            predictor=SimpleNamespace(trackers=[tracker]),
            names=CLASS_NAMES)
        gog = _bare_gog(model, detect_class_ids=[0])

        scene = FieldScene(num_weeds=1, num_crops=0, speed=4.0, vibration=0.5)
        scene.objects[0]['y0'] = 40.0
        for f in range(20):
            drop = {0} if f >= 12 else None
            boxes, cls_ids, confs, _ = scene.detections(f, drop_ids=drop)
            tracker.update(to_det_batch(boxes, cls_ids, confs))

        lost = gog.get_lost_tracks()
        assert len(lost) == 1
        lt = lost[0]
        assert set(lt) == {'track_id', 'xyxy', 'cls', 'score', 'age'}
        assert lt['age'] == 8  # dropped at frame 12, now frame 19 (1-based 20)
        assert lt['cls'] == 0

        assert gog.get_lost_tracks(max_age=5) == []

    def test_no_predictor_returns_empty(self):
        gog = _bare_gog(SimpleNamespace(predictor=None, names=CLASS_NAMES),
                        detect_class_ids=[0])
        assert gog.get_lost_tracks() == []


# ============================================================
# ClassSmoother fed by the REAL tracker
# ============================================================

@pytest.mark.unit
class TestSmootherWithRealTracker:
    def test_smoothing_reduces_flips_with_real_ids(self):
        scene = FieldScene(num_weeds=4, num_crops=4, vibration=2.0, seed=11)
        tracker = make_tracker()
        smoother = ClassSmoother(window=5)

        raw_flips, smoothed_flips = 0, 0
        last_raw, last_smooth = {}, {}
        for f in range(100):
            boxes, cls_ids, confs, _ = scene.detections(f, flicker_rate=0.25)
            tracks = tracker.update(to_det_batch(boxes, cls_ids, confs))
            if not len(tracks):
                continue
            tids = [int(r[4]) for r in tracks]
            tcls = [int(r[6]) for r in tracks]
            tconf = [float(r[5]) for r in tracks]
            smoothed = smoother.update(tids, tcls, tconf, frame_count=f)
            for tid, cls in zip(tids, tcls):
                if tid in last_raw and last_raw[tid] != cls:
                    raw_flips += 1
                last_raw[tid] = cls
                s = smoothed[tid]
                if tid in last_smooth and last_smooth[tid] != s:
                    smoothed_flips += 1
                last_smooth[tid] = s

        assert raw_flips > 10, 'scene not flickery enough to test smoothing'
        assert smoothed_flips < raw_flips * 0.4, (
            f'smoothing only reduced flips {raw_flips} -> {smoothed_flips}')


# ============================================================
# Timing sanity (Pi feasibility)
# ============================================================

@pytest.mark.unit
class TestTrackingOverhead:
    def test_per_frame_update_is_cheap(self):
        """Tracker update must stay far below the YOLO inference budget.
        Desktop median < 5 ms ≈ tens of ms worst-case on a Pi — still
        negligible vs 50-150 ms NCNN inference."""
        scene = FieldScene(num_weeds=8, num_crops=4, vibration=2.0)
        tracker = make_tracker()
        times = []
        for f in range(100):
            boxes, cls_ids, confs, _ = scene.detections(f, drop_rate=0.1,
                                                        flicker_rate=0.1)
            batch = to_det_batch(boxes, cls_ids, confs)
            t0 = time.perf_counter()
            tracker.update(batch)
            times.append((time.perf_counter() - t0) * 1000)
        median = float(np.median(times))
        assert median < 5.0, f'tracker update median {median:.2f}ms'


# ============================================================
# Annotated frame artifacts for manual review
# ============================================================

@pytest.mark.unit
class TestRenderedSequenceArtifacts:
    def test_render_annotated_tracking_sequence(self):
        """Render 60 frames with dropouts/flicker, run the real tracker,
        and write annotated frames (IDs, lost-track Kalman boxes) to
        tests/tracking_output/real_bytetrack/ for visual inspection."""
        VIZ_DIR.mkdir(parents=True, exist_ok=True)
        scene = FieldScene(num_weeds=4, num_crops=2, speed=5.0,
                           vibration=2.0, seed=3)
        tracker = make_tracker()
        smoother = ClassSmoother(window=5)

        frames_written = 0
        for f in range(60):
            drop = {1} if 25 <= f < 33 else None  # burst dropout of one weed
            boxes, cls_ids, confs, _ = scene.detections(
                f, drop_ids=drop, drop_rate=0.05, flicker_rate=0.15)
            tracks = tracker.update(to_det_batch(boxes, cls_ids, confs))

            img = scene.render(f)
            if len(tracks):
                tids = [int(r[4]) for r in tracks]
                tcls = [int(r[6]) for r in tracks]
                tconf = [float(r[5]) for r in tracks]
                smoothed = smoother.update(tids, tcls, tconf, frame_count=f)
                for row in tracks:
                    x1, y1, x2, y2 = map(int, row[:4])
                    tid, cls = int(row[4]), int(row[6])
                    s_cls = smoothed[tid]
                    colour = (0, 0, 255) if s_cls == 0 else (255, 160, 0)
                    cv2.rectangle(img, (x1, y1), (x2, y2), colour, 2)
                    cv2.putText(img, f'ID{tid} {CLASS_NAMES[s_cls]}',
                                (x1, max(12, y1 - 6)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1)
            # Lost tracks: Kalman-predicted boxes drawn dimmed
            for strack in tracker.lost_stracks:
                x1, y1, x2, y2 = map(int, strack.xyxy)
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 120, 120), 1)
                cv2.putText(img, f'ID{strack.track_id} lost',
                            (x1, max(12, y1 - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 120, 120), 1)
            cv2.putText(img, f'frame {f:03d}', (8, 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            cv2.imwrite(str(VIZ_DIR / f'frame_{f:03d}.jpg'), img)
            frames_written += 1

        assert frames_written == 60
        assert any(VIZ_DIR.glob('frame_*.jpg'))


# ============================================================
# Real model E2E smoke (uses models/yolo26n.pt if present)
# ============================================================

MODEL_PT = PROJECT_ROOT / 'models' / 'yolo26n.pt'


@pytest.mark.integration
@pytest.mark.skipif(not MODEL_PT.exists(), reason='yolo26n.pt not present')
class TestRealModelEndToEnd:
    def test_model_track_pipeline_from_foreign_cwd(self, tmp_path, monkeypatch):
        """Full GreenOnGreen + model.track() on rendered frames, run from a
        DIFFERENT cwd — proves the tracker yaml resolves via TRACKER_YAML
        (the old relative path raised FileNotFoundError here) and that INI
        tracker_params reach the live ByteTrack after the first call."""
        monkeypatch.chdir(tmp_path)
        assert Path(TRACKER_YAML).exists()

        gog = GreenOnGreen(
            model_path=str(MODEL_PT),
            confidence=0.25,
            tracking_enabled=True,
            tracker_params={'match_thresh': 0.85, 'track_buffer': 45},
        )
        scene = FieldScene(num_weeds=3, num_crops=2)
        for f in range(3):
            contours, boxes, centres, img_out = gog.inference(scene.render(f))
            assert isinstance(boxes, list)
            assert len(boxes) == len(centres)

        trackers = gog.model.predictor.trackers
        assert len(trackers) >= 1
        assert trackers[0].args.match_thresh == 0.85
        assert trackers[0].max_time_lost == 45
        assert gog._pending_tracker_params is None

        # Lost-track API and reset path must not raise on the real model
        gog.get_lost_tracks(max_age=5)
        gog.reset_tracker()
        assert gog.last_track_ids == []
