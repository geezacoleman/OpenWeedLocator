"""A stand-in for ultralytics, so GreenOnGreen runs in CI (which has none).

Only the model is fake. GreenOnGreen, GreenOnBrown, ClassSmoother and
CropMaskStabilizer are the production classes. The fakes carry exactly the
attributes GreenOnGreen reads from ultralytics objects and nothing else
(plain classes, not MagicMock: a misspelt attribute is an AttributeError):

  Results:  .boxes, .masks, results[list_of_rows]
  Boxes:    .id (array or None), .cls, len(), iteration, boxes[i]
  one box:  .xyxy[0], .conf[0], .cls[0]
  model:    .task, .names, .predictor.trackers, .predict(**kw), .track(**kw)
  tracker:  .args.<param>, .max_frames_lost (max_time_lost before 8.4.38),
            .frame_id, .lost_stracks, .reset()

tests/unit/test_bytetrack_real.py runs the same class filter against real
ultralytics Results where ultralytics is installed.
"""

from types import SimpleNamespace

import numpy as np

NAMES = {0: 'weed', 1: 'crop'}
WEED, CROP = 0, 1


class Box:
    """One detection, as iterating a Boxes object yields it."""

    def __init__(self, xyxy, cls, conf):
        self.xyxy = [np.array(xyxy, dtype=np.float32)]
        self.cls = np.array([cls], dtype=np.float32)
        self.conf = np.array([conf], dtype=np.float32)


class Boxes:
    def __init__(self, rows, ids=None):
        self._rows = list(rows)            # (xyxy, cls, conf)
        self._ids = list(ids) if ids is not None else None
        assert self._ids is None or len(self._ids) == len(self._rows)

    @property
    def id(self):
        return None if self._ids is None else np.array(self._ids, dtype=np.float32)

    @property
    def cls(self):
        return np.array([cls for _, cls, _ in self._rows], dtype=np.float32)

    def __len__(self):
        return len(self._rows)

    def __iter__(self):
        return iter([Box(*row) for row in self._rows])

    def __getitem__(self, index):
        if isinstance(index, (int, np.integer)):
            return Box(*self._rows[index])
        return Boxes([self._rows[i] for i in index],
                     None if self._ids is None else [self._ids[i] for i in index])


class Results:
    """One frame's detections.

    rows: (xyxy, class id, confidence). ids: ByteTrack ids, or None for a
    frame on which the tracker confirmed nothing. polygons: one outline per
    row for a segmentation model.
    """

    def __init__(self, rows=(), ids=None, polygons=None):
        self.boxes = Boxes(rows, ids)
        self._polygons = polygons
        self.masks = None if polygons is None else SimpleNamespace(
            xy=[np.array(p, dtype=np.float32) for p in polygons])

    def __getitem__(self, index):
        rows = [self.boxes._rows[i] for i in index]
        ids = None if self.boxes._ids is None else [self.boxes._ids[i] for i in index]
        polygons = None if self._polygons is None else [self._polygons[i] for i in index]
        return Results(rows, ids, polygons)


def box(x1, y1, x2, y2, cls=WEED, conf=0.9):
    return ((x1, y1, x2, y2), cls, conf)


class LostTrack:
    """A ByteTrack STrack the detector lost; xyxy is its Kalman prediction."""

    def __init__(self, track_id, xyxy, cls, end_frame, score=0.8):
        self.track_id = track_id
        self.xyxy = np.array(xyxy, dtype=np.float32)
        self.cls = cls
        self.score = score
        self.end_frame = end_frame


class Tracker:
    """The parts of BYTETracker GreenOnGreen reads and writes."""

    def __init__(self, frame_id=0, lost=(), lost_attr='max_frames_lost', **args):
        """lost_attr: the lost-frames limit's name. 'max_frames_lost' since
        ultralytics 8.4.38, 'max_time_lost' before, None for a version that
        has neither."""
        defaults = dict(track_high_thresh=0.2, track_low_thresh=0.05, new_track_thresh=0.2,
                        track_buffer=60, match_thresh=0.8)
        self.args = SimpleNamespace(**dict(defaults, **args))
        if lost_attr:
            setattr(self, lost_attr, self.args.track_buffer)
        self.frame_id = frame_id
        self.lost_stracks = list(lost)
        self.resets = 0

    def reset(self):
        self.resets += 1
        self.lost_stracks = []


class Model:
    """What ultralytics.YOLO(path, task=...) returns.

    `frames` is what the model sees: a list of Results (one per call, the
    last repeated) or a callable(image) -> Results. ultralytics only builds
    the predictor (and its tracker) on the first track() call; `tracker` is
    what it will find there.
    """

    def __init__(self, frames=None, task='detect', names=None, tracker=None):
        self.task = task
        self.names = dict(names or NAMES)
        self.predictor = None
        self._tracker = tracker
        self._frames = frames if frames is not None else [Results()]
        self.calls = []                    # ('predict'|'track', kwargs without the image)
        self.loaded_from = None            # (path, task) the production code asked for

    def _next(self, kind, kwargs):
        image = kwargs.pop('source')
        self.calls.append((kind, kwargs))
        if callable(self._frames):
            result = self._frames(image)
        else:
            result = self._frames[min(len(self.calls), len(self._frames)) - 1]
        return [result]

    def predict(self, **kwargs):
        return self._next('predict', kwargs)

    def track(self, **kwargs):
        if self.predictor is None and self._tracker is not None:
            self.predictor = SimpleNamespace(trackers=[self._tracker])
        return self._next('track', kwargs)


def install(monkeypatch, model):
    """Make utils.greenongreen load `model` instead of an ultralytics one."""
    import utils.greenongreen as gog_module

    def load(path, task=None):
        model.loaded_from = (path, task)
        return model

    monkeypatch.setattr(gog_module, 'YOLO', load)
    return model


def model_file(tmp_path, name='weeds.pt'):
    path = tmp_path / name
    path.touch()
    return str(path)
