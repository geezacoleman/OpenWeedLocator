"""AI detection with tracking, at the relay.

A real Owl runs algorithm = gog with [Tracking] on. Only the model is a
stand-in (tests/unit/fake_yolo.py: CI has no ultralytics); it reports
whatever the test puts in `Scene`. GreenOnGreen, the class smoother, the
lost-track merge in hoot(), lane assignment and the relay controller are the
production code, and every assertion is on a relay pin being driven.

    lane 0      lane 1       lane 2       lane 3        (416 px frame, 4 relays)
    x 0-103     x 104-207    x 208-311    x 312-415

Covers what the unit tests cannot reach:
  - the lost-track merge in owl.py hoot(): a weed the model drops for a few
    frames is still sprayed, where ByteTrack predicts it has moved to; a lost
    crop is not; a track older than detection_persist_frames is not;
  - a dashboard change to detection_persist_frames reaches the loop;
  - the frame a new object first appears on (no track ids yet) sprays the
    weed and not the crop.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / 'unit'))   # fake_yolo lives with the unit tests

import fake_yolo                                              # noqa: E402
from fake_yolo import CROP, WEED, LostTrack, Results, Tracker, box   # noqa: E402
from harness import CONFIG_DURATION_S, PROMPT_S               # noqa: E402

NOW = 100                      # the tracker's frame counter (held still)


def in_lane(lane, cls=WEED):
    """A 60x60 detection in the middle of a lane."""
    x = lane * 104 + 22
    return box(x, 130, x + 60, 190, cls)


def lost(track_id, lane, cls, age):
    x = lane * 104 + 22
    return LostTrack(track_id, (x, 130, x + 60, 190), cls, end_frame=NOW - age)


class Scene:
    """What the model and ByteTrack report on every frame until changed."""

    def __init__(self):
        self.tracker = Tracker(frame_id=NOW)
        self._lock = threading.Lock()
        self._results = Results()
        self.frames = 0

    def show(self, rows=(), ids=None, lost=()):
        with self._lock:
            self._results = Results(rows, ids)
            self.tracker.lost_stracks = list(lost)
            self.frames = 0

    def clear(self):
        self.show()

    def __call__(self, image):
        with self._lock:
            self.frames += 1
            return self._results


def build(rig, monkeypatch, tmp_path, dash='none'):
    scene = Scene()
    fake_yolo.install(monkeypatch, fake_yolo.Model(scene, tracker=scene.tracker))
    r = rig(dash=dash, overrides=[
        ('System', 'algorithm', 'gog'),
        ('GreenOnGreen', 'model_path', fake_yolo.model_file(tmp_path)),
        ('GreenOnGreen', 'detect_classes', 'weed'),
        ('Tracking', 'tracking_enabled', 'True'),
        ('Tracking', 'detection_persist_frames', '10'),
    ])
    assert r.owl.running_algorithm == 'gog', 'the AI detector did not load'
    r.wait_for(lambda: scene.frames >= 2, 'frames to reach the model')
    return r, scene


def relays_for(r, scene, seconds=0.4, **shown):
    """Hold a scene for `seconds`, clear it, wait for the relays to let go.
    Returns (relays switched on, seconds from the scene to the first relay,
    seconds from the scene ending to the last relay off)."""
    time.sleep(0.1)
    mark = r.mark()
    scene.show(**shown)
    began = time.monotonic()
    time.sleep(seconds)
    scene.clear()
    ended = time.monotonic()
    time.sleep(0.15)
    r.wait_all_off()
    time.sleep(0.2)
    drives = r.drives_since(mark)
    ons = [at for at, _, on in drives if on]
    offs = [at for at, _, on in drives if not on]
    fired = sorted({relay for _, relay, on in drives if on})
    return fired, (ons[0] - began if ons else None), (max(offs) - ended if offs else None)


def test_a_weed_the_model_loses_is_still_sprayed_where_it_is_predicted_to_be(
        rig, monkeypatch, tmp_path):
    r, scene = build(rig, monkeypatch, tmp_path, dash='standalone')
    assert not r.relays_on()

    # Tracked: weed 1 in lane 0, crop 2 in lane 2, weed 3 in lane 3.
    # This is also what teaches the smoother which track is which class.
    fired, on, off = relays_for(r, scene, rows=[in_lane(0), in_lane(2, CROP), in_lane(3)],
                                ids=[1, 2, 3])
    assert fired == [0, 3], 'tracked weeds spray their lanes; the tracked crop does not'
    assert 0 <= on < PROMPT_S
    assert 0 <= off < CONFIG_DURATION_S + PROMPT_S

    # The model now sees nothing. ByteTrack still predicts: weed 1 has moved
    # to lane 1 (2 frames lost), crop 2 is in lane 2 (2 frames lost), weed 3
    # is in lane 3 but was lost 11 frames ago (limit is 10).
    fired, on, off = relays_for(r, scene, lost=[
        lost(1, lane=1, cls=WEED, age=2),
        lost(2, lane=2, cls=CROP, age=2),
        lost(3, lane=3, cls=WEED, age=11)])
    assert fired == [1], ('only the recently lost weed is sprayed, in the lane it is '
                          'predicted in')
    assert 0 <= on < PROMPT_S
    assert 0 <= off < CONFIG_DURATION_S + PROMPT_S

    # At exactly the limit it is still sprayed
    fired, _, _ = relays_for(r, scene, lost=[lost(3, lane=3, cls=WEED, age=10)])
    assert fired == [3]

    # The operator turns persistence down to 1 frame from the dashboard
    r.command('set_config_section', section='Tracking',
              params={'detection_persist_frames': 1})
    assert r.owl._gog_detector.detection_persist_frames == 1
    fired, _, _ = relays_for(r, scene, lost=[lost(1, lane=1, cls=WEED, age=2)])
    assert fired == [], 'a weed lost 2 frames ago is no longer sprayed'
    fired, _, _ = relays_for(r, scene, lost=[lost(1, lane=1, cls=WEED, age=1)])
    assert fired == [1]

    # ...and off altogether
    r.command('set_config_section', section='Tracking',
              params={'detection_persist_frames': 0})
    fired, _, _ = relays_for(r, scene, lost=[lost(1, lane=1, cls=WEED, age=1)])
    assert fired == []


def test_the_first_frame_of_a_new_plant_sprays_weed_not_crop(rig, monkeypatch, tmp_path):
    """No track ids yet: ultralytics hands back every raw detection. Field
    bug: the crop box went straight to the relays."""
    r, scene = build(rig, monkeypatch, tmp_path)

    fired, _, _ = relays_for(r, scene, rows=[in_lane(2, CROP)], ids=None)
    assert fired == [], 'crop on a new-object frame switched a relay'

    fired, on, _ = relays_for(r, scene, rows=[in_lane(1), in_lane(2, CROP)], ids=None)
    assert fired == [1]
    assert 0 <= on < PROMPT_S


WIDE = box(130, 130, 290, 190)                       # spans lanes 1 and 2; centre in lane 2
WIDE_OUTLINE = [(130, 130), (290, 130), (290, 190), (130, 190)]


@pytest.mark.parametrize('tracking, ids, lanes', [
    (False, None, [1, 2]),     # zone: every lane the weed's outline covers
    (True, [1], [2]),          # tracking on: zone is not available, the centre's lane only
])
def test_zone_actuation_sprays_what_the_outline_covers_unless_tracking_is_on(
        rig, monkeypatch, tmp_path, tracking, ids, lanes):
    """[GreenOnGreen] actuation_mode = zone with a segmentation model. With
    tracking on the OWL falls back to centre actuation (and says so in the
    log): the operator gets one lane where zone would have given two."""
    scene = Scene()
    fake_yolo.install(monkeypatch, fake_yolo.Model(scene, task='segment', tracker=scene.tracker))
    r = rig(overrides=[
        ('System', 'algorithm', 'gog'),
        ('GreenOnGreen', 'model_path', fake_yolo.model_file(tmp_path, 'weeds-seg.pt')),
        ('GreenOnGreen', 'detect_classes', 'weed'),
        ('GreenOnGreen', 'actuation_mode', 'zone'),
        ('Tracking', 'tracking_enabled', str(tracking)),
    ])
    r.wait_for(lambda: scene.frames >= 2, 'frames to reach the model')

    mark = r.mark()
    time.sleep(0.1)
    scene._results = Results([WIDE], ids, polygons=[WIDE_OUTLINE])
    began = time.monotonic()
    time.sleep(0.4)
    scene.clear()
    time.sleep(0.15)
    r.wait_all_off()
    drives = r.drives_since(mark)

    assert sorted({relay for _, relay, on in drives if on}) == lanes
    assert 0 <= min(at for at, _, on in drives if on) - began < PROMPT_S
