"""Detection persistence: what GreenOnGreen reports about tracks ByteTrack
has lost, and the config key that limits how long they are kept.

GreenOnGreen is the production class; the model and tracker are the
stand-ins in fake_yolo.py (CI has no ultralytics).

Where the rest of the feature is tested:
  - the merge of lost weeds into the spray decision in owl.py hoot(), its
    class filter and age limit, and a dashboard change to the limit:
    tests/owl/test_tracked_detections_at_the_relay.py (real Owl, at the relay)
  - lost crop painted into the hybrid crop mask: test_greenongreen.py
  - against a real BYTETracker: test_bytetrack_real.py (needs ultralytics)
"""

import configparser
from pathlib import Path

import numpy as np
import pytest

import fake_yolo
from fake_yolo import CROP, WEED, LostTrack, Tracker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRAME = np.zeros((240, 320, 3), dtype=np.uint8)


def detector(monkeypatch, tmp_path, tracker):
    """A tracking GreenOnGreen that has processed one frame (ultralytics
    only creates the tracker on the first track() call)."""
    from utils.greenongreen import GreenOnGreen
    model = fake_yolo.install(monkeypatch, fake_yolo.Model(tracker=tracker))
    gog = GreenOnGreen(model_path=fake_yolo.model_file(tmp_path), tracking_enabled=True)
    assert gog.get_lost_tracks() == [], 'no tracker exists before the first frame'
    gog.inference(FRAME)
    assert model.calls[0][0] == 'track'
    return gog


class TestGetLostTracks:
    def test_reports_each_lost_track_with_its_predicted_box_and_age(self, monkeypatch, tmp_path):
        tracker = Tracker(frame_id=10, lost=[
            LostTrack(7, (100, 200, 150, 250), WEED, end_frame=8, score=0.85),
            LostTrack(12, (300, 100, 350, 150), CROP, end_frame=6, score=0.72)])
        gog = detector(monkeypatch, tmp_path, tracker)

        first, second = gog.get_lost_tracks()

        assert first['xyxy'].tolist() == [100, 200, 150, 250]
        assert {k: v for k, v in first.items() if k != 'xyxy'} == {
            'track_id': 7, 'cls': WEED, 'score': 0.85, 'age': 2}
        assert second['xyxy'].tolist() == [300, 100, 350, 150]
        assert {k: v for k, v in second.items() if k != 'xyxy'} == {
            'track_id': 12, 'cls': CROP, 'score': 0.72, 'age': 4}

    @pytest.mark.parametrize('max_age, ids', [
        (None, [1, 2, 3]),      # no limit
        (99, [1, 2, 3]),
        (5, [1, 2]),            # a track exactly at the limit is kept
        (4, [1]),
        (1, [1]),
        (0, []),                # a lost track is always at least one frame old
    ])
    def test_max_age_is_inclusive(self, monkeypatch, tmp_path, max_age, ids):
        tracker = Tracker(frame_id=100, lost=[
            LostTrack(1, (0, 0, 10, 10), WEED, end_frame=99),      # age 1
            LostTrack(2, (0, 0, 10, 10), WEED, end_frame=95),      # age 5
            LostTrack(3, (0, 0, 10, 10), WEED, end_frame=1)])      # age 99
        gog = detector(monkeypatch, tmp_path, tracker)
        assert [lt['track_id'] for lt in gog.get_lost_tracks(max_age=max_age)] == ids

    def test_the_box_is_the_trackers_current_prediction(self, monkeypatch, tmp_path):
        """ByteTrack moves a lost track along its Kalman prediction every
        frame; what is reported must be where it is now, not where it was
        when first read."""
        track = LostTrack(7, (100, 100, 140, 140), WEED, end_frame=9)
        tracker = Tracker(frame_id=10, lost=[track])
        gog = detector(monkeypatch, tmp_path, tracker)
        assert gog.get_lost_tracks()[0]['xyxy'].tolist() == [100, 100, 140, 140]

        track.xyxy = np.array([100, 120, 140, 160], dtype=np.float32)
        tracker.frame_id = 11
        (moved,) = gog.get_lost_tracks()
        assert moved['xyxy'].tolist() == [100, 120, 140, 160]
        assert moved['age'] == 2

    def test_no_trackers_on_the_predictor_gives_nothing(self, monkeypatch, tmp_path):
        gog = detector(monkeypatch, tmp_path, Tracker(frame_id=10, lost=[
            LostTrack(7, (0, 0, 10, 10), WEED, end_frame=9)]))
        gog.model.predictor.trackers = []
        assert gog.get_lost_tracks() == []

    def test_a_tracker_without_the_expected_internals_gives_nothing_not_a_crash(
            self, monkeypatch, tmp_path):
        """get_lost_tracks reads ultralytics internals that are not a public
        API. If they change, persistence stops; the detection loop must not."""
        class Changed:
            track_id = 7
            cls = WEED
            score = 0.9
            # no end_frame, no xyxy

        gog = detector(monkeypatch, tmp_path, Tracker(frame_id=10, lost=[Changed()]))
        assert gog.get_lost_tracks() == []

    def test_reset_empties_the_lost_tracks(self, monkeypatch, tmp_path):
        tracker = Tracker(frame_id=10, lost=[LostTrack(7, (0, 0, 10, 10), WEED, end_frame=9)])
        gog = detector(monkeypatch, tmp_path, tracker)
        assert len(gog.get_lost_tracks()) == 1
        gog.reset_tracker()
        assert gog.get_lost_tracks() == []


class TestDetectionPersistFramesConfig:
    """[Tracking] detection_persist_frames in a config file."""

    @staticmethod
    def config_with(tmp_path, value):
        config = configparser.ConfigParser()
        config.read(PROJECT_ROOT / 'config' / 'GENERAL_CONFIG.ini')
        config.set('Tracking', 'detection_persist_frames', value)
        path = tmp_path / 'profile.ini'
        with open(path, 'w') as f:
            config.write(f)
        return path

    @pytest.mark.parametrize('value', ['0', '5', '15'])
    def test_values_in_range_load(self, tmp_path, value):
        from utils.config_manager import ConfigValidator
        loaded = ConfigValidator.load_and_validate_config(self.config_with(tmp_path, value))
        assert loaded.get('Tracking', 'detection_persist_frames') == value

    @pytest.mark.parametrize('value', ['-1', '16', 'lots'])
    def test_values_out_of_range_are_refused_by_name(self, tmp_path, value):
        from utils import error_manager as errors
        from utils.config_manager import ConfigValidator
        with pytest.raises(errors.ConfigValueError, match='detection_persist_frames'):
            ConfigValidator.load_and_validate_config(self.config_with(tmp_path, value))

    def test_the_dashboard_offers_the_range_the_validator_accepts(self):
        """Contract check on a shipped file (reads config.js, does not run
        it): the editor's min/max for this key must be the validator's."""
        import re
        from utils.config_manager import ConfigValidator
        js = (PROJECT_ROOT / 'controller' / 'shared' / 'js' / 'config.js').read_text(
            encoding='utf-8')
        field = re.search(r"'detection_persist_frames'\s*:\s*\{([^}]*)\}", js).group(1)
        offered = (int(re.search(r'min:\s*(-?\d+)', field).group(1)),
                   int(re.search(r'max:\s*(-?\d+)', field).group(1)))
        assert offered == ConfigValidator.VALUE_VALIDATORS['detection_persist_frames'][1:]
