"""Data-collection feature: capture spacing, image quality, shutter cap,
session metadata, the controller's collect screen (state, warnings, uploads,
Noktura connect) and the OWL-side MQTT handlers that back it.

Plan: ~/.claude/plans/enumerated-cuddling-nebula.md (signed off 2026-10-08).
"""

import configparser
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from multiprocessing import Value

from utils.exposure_governor import (ExposureGovernor, MIN_EXPOSURE_US, ground_smear_mm,
                                     shutter_cap_us, parse_max_gain, GAIN_RECHECK_S, RELEASE_HOLD_S)
from utils.image_sampler import (CaptureQC, CaptureScheduler, ImageRecorder, image_quality,
                                 make_thumbnail)
from utils.session_metadata import (SESSION_METADATA_FIELDS, clean_session_metadata,
                                    presets_from_config, DEFAULT_CROPS)
from utils.config_manager import update_ini_text
from controller.networked.collect import (CollectSession, collect_warnings, parse_provision_command,
                                          thresholds_from_config, UPLOAD_REQUEST_TTL_S)

REPO = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# CaptureScheduler
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCaptureScheduler:
    def test_frames_mode_every_nth_frame(self):
        s = CaptureScheduler(mode='frames', frequency=30)
        assert [f for f in range(100) if s.should_capture(f)] == [0, 30, 60, 90]

    def test_frames_mode_uses_live_frequency(self):
        s = CaptureScheduler(mode='frames', frequency=30)
        assert s.should_capture(10, frequency=10)
        assert not s.should_capture(10)
        # 0/None = not set: the configured frequency applies, never a divide by zero
        assert s.should_capture(30, frequency=0) and not s.should_capture(7, frequency=0)

    def test_unknown_mode_falls_back_to_frames(self):
        assert CaptureScheduler(mode='bogus').mode == 'frames'

    def test_distance_first_moving_frame_saved_then_every_metre(self):
        s = CaptureScheduler(mode='distance', distance_m=1.0)
        # 3.6 km/h = 1 m/s, frames 0.1 s apart
        saved = [i for i in range(35) if s.should_capture(i, now=i * 0.1, speed_kmh=3.6)]
        assert saved[0] == 0
        gaps = np.diff(saved)
        assert all(9 <= g <= 11 for g in gaps), saved

    def test_distance_parked_saves_nothing(self):
        s = CaptureScheduler(mode='distance', distance_m=1.0, interval_s=0.5)
        assert not any(s.should_capture(i, now=i * 0.5, speed_kmh=0.2) for i in range(20))

    def test_distance_without_gps_falls_back_to_time(self):
        s = CaptureScheduler(mode='distance', distance_m=1.0, interval_s=2.0)
        saved = [i for i in range(50) if s.should_capture(i, now=i * 0.1, speed_kmh=None)]
        assert saved == [0, 20, 40]

    def test_distance_stall_does_not_burst(self):
        s = CaptureScheduler(mode='distance', distance_m=1.0)
        assert s.should_capture(0, now=0.0, speed_kmh=36)
        # A 30 s gap at 10 m/s counts at most 1 s of travel: one image, not 300
        assert s.should_capture(1, now=30.0, speed_kmh=36)
        assert not s.should_capture(2, now=30.01, speed_kmh=36)

    def test_reset_saves_first_frame_of_new_session(self):
        s = CaptureScheduler(mode='time', interval_s=5)
        assert s.should_capture(0, now=0)
        assert not s.should_capture(1, now=1)
        s.reset()
        assert s.should_capture(2, now=1.5)

    @pytest.mark.parametrize('mode', ['frames', 'distance', 'time'])
    def test_below_min_speed_saves_nothing_in_every_mode(self, mode):
        s = CaptureScheduler(mode=mode, frequency=1, interval_s=0.1, min_speed_kmh=1.0)
        assert not any(s.should_capture(i, now=i * 0.5, speed_kmh=0.8) for i in range(20))
        # Speeding up again starts saving straight away
        assert s.should_capture(20, now=10.0, speed_kmh=5.0)

    def test_min_speed_ignored_without_gps_and_when_zero(self):
        s = CaptureScheduler(mode='time', interval_s=1.0, min_speed_kmh=1.0)
        assert s.should_capture(0, now=0.0, speed_kmh=None)
        assert not s.too_slow(None)
        s.configure(min_speed_kmh=0)
        assert not s.too_slow(0.0)
        assert s.should_capture(1, now=2.0, speed_kmh=0.0)

    def test_configure_clamps(self):
        s = CaptureScheduler()
        s.configure(min_speed_kmh=-3)
        assert s.min_speed_kmh == 0.0
        s.configure(min_speed_kmh=99)
        assert s.min_speed_kmh == 20.0
        s.configure(distance_m=0, interval_s=-1, frequency=0)
        assert s.distance_m >= 0.05 and s.interval_s >= 0.1 and s.frequency == 1
        s.configure(distance_m=1e9, interval_s=1e9)
        assert (s.distance_m, s.interval_s) == (50.0, 60.0)
        for bad in ('inf', float('nan')):
            with pytest.raises(ValueError):
                s.configure(distance_m=bad)
            with pytest.raises(ValueError):
                s.configure(interval_s=bad)


@pytest.mark.unit
class TestImageRecorderMainLoopCost:
    @staticmethod
    def _recorder(quality):
        r = ImageRecorder.__new__(ImageRecorder)
        r.queue = MagicMock()
        r.queue.full.return_value = False
        r.queue.qsize.return_value = 0
        r.processes, r.max_processes, r.new_process_threshold = [], 0, 90
        r.jpeg_quality = quality
        r.saved_count, r.last_thumbnail, r.last_saved = 0, None, None
        r.qc = CaptureQC()
        r.logger = MagicMock()
        r.failed, r._failed_seen = Value('i', 0), 0   # worker failure count
        return r

    def test_thumbnail_only_when_publish_due_quality_per_frame(self, monkeypatch):
        import utils.image_sampler as sampler
        made = []
        monkeypatch.setattr(sampler, 'make_thumbnail', lambda frame: made.append(1) or b'jpg')
        r = self._recorder(quality=70)
        frame = np.zeros((48, 64, 3), np.uint8)
        for i in range(5):
            r.add_frame(frame, i, None, None)
        assert len(made) == 1 and r.saved_count == 5 and r.qc.summary()['count'] == 5
        # Live quality travels with each queued frame (workers are forked copies)
        r.jpeg_quality = 60
        r.add_frame(frame, 5, None, None)
        assert [c.args[0][-1] for c in r.queue.put.call_args_list] == [70] * 5 + [60]


# ---------------------------------------------------------------------------
# Image quality + thumbnails
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestImageQuality:
    def test_black_frame_is_all_dark_and_finite(self):
        q = image_quality(np.zeros((1088, 1456, 3), np.uint8))
        assert q['dark'] == 100.0 and q['clipped'] == 0.0 and q['brightness'] == 0.0
        assert np.isfinite(q['sharpness'])

    def test_white_frame_is_clipped(self):
        q = image_quality(np.full((480, 640, 3), 255, np.uint8))
        assert q['clipped'] == 100.0

    def test_detail_scores_sharper_than_blur(self):
        rng = np.random.default_rng(1)
        sharp = (rng.random((480, 640, 3)) * 255).astype(np.uint8)
        import cv2
        blurred = cv2.GaussianBlur(sharp, (31, 31), 10)
        assert image_quality(sharp)['sharpness'] > image_quality(blurred)['sharpness']

    def test_qc_baseline_after_first_ten(self):
        qc = CaptureQC(window=10, baseline_n=10)
        for i in range(9):
            qc.add({'sharpness': 30.0, 'brightness': 100, 'clipped': 0, 'dark': 0})
        assert qc.summary()['baseline_sharpness'] is None
        qc.add({'sharpness': 30.0, 'brightness': 100, 'clipped': 0, 'dark': 0})
        for i in range(5):
            qc.add({'sharpness': 10.0, 'brightness': 100, 'clipped': 0, 'dark': 0})
        summary = qc.summary()
        assert summary['baseline_sharpness'] == 30.0  # not dragged down by later images
        assert summary['recent_sharpness'] == [10.0, 10.0, 10.0]
        json.dumps(summary)

    def test_thumbnail_is_small_jpeg(self):
        data = make_thumbnail(np.zeros((1088, 1456, 3), np.uint8))
        assert data[:2] == b'\xff\xd8' and len(data) < 60000


# ---------------------------------------------------------------------------
# Exposure governor (shutter cap / gain limit)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestExposureGovernor:
    def test_smear_maths(self):
        # 10 km/h, 1 ms -> 2.78 mm
        assert ground_smear_mm(10, 1000) == pytest.approx(2.78, abs=0.01)
        assert ground_smear_mm(None, 1000) is None

    def test_cap_maths_and_floor(self):
        assert shutter_cap_us(10, 2.0) == 720
        assert shutter_cap_us(200, 0.1) == MIN_EXPOSURE_US
        assert shutter_cap_us(0.3, 2.0) is None   # parked
        assert shutter_cap_us(None, 2.0) is None

    def test_caps_when_auto_exposure_too_long(self):
        g = ExposureGovernor(smear_limit_mm=2.0, cap_enabled=True)
        out = g.update({'ExposureTime': 2000, 'AnalogueGain': 2.0}, speed_kmh=10, now=0)
        assert out == {'exposure_us': 720, 'gain': None}

    def test_no_cap_within_hysteresis(self):
        g = ExposureGovernor(smear_limit_mm=2.0)
        assert g.update({'ExposureTime': 760, 'AnalogueGain': 2.0}, speed_kmh=10, now=0) is None

    def test_releases_when_gps_stale(self):
        g = ExposureGovernor()
        g.update({'ExposureTime': 2000, 'AnalogueGain': 2.0}, speed_kmh=10, now=0)
        assert g.update({'ExposureTime': 720, 'AnalogueGain': 4.0}, speed_kmh=None, now=1) == \
            {'exposure_us': None, 'gain': None}

    def test_releases_when_bright_enough(self):
        g = ExposureGovernor()
        g.update({'ExposureTime': 2000, 'AnalogueGain': 2.0}, speed_kmh=10, now=0)
        # At the cap and at unity gain: released only once that has held
        assert g.update({'ExposureTime': 720, 'AnalogueGain': 1.0}, speed_kmh=10, now=1) is None
        assert g.update({'ExposureTime': 720, 'AnalogueGain': 1.0}, speed_kmh=10,
                        now=1 + RELEASE_HOLD_S)['exposure_us'] is None

    def test_no_release_before_cap_takes_effect(self):
        g = ExposureGovernor()
        g.update({'ExposureTime': 2000, 'AnalogueGain': 1.0}, speed_kmh=10, now=0)
        # Control latency: the old long exposure at unity gain is not "bright"
        for i in range(1, 60):
            assert g.update({'ExposureTime': 2000, 'AnalogueGain': 1.0}, speed_kmh=10, now=i) is None
        assert g.exposure_us == 720

    @staticmethod
    def _simulate(scene_us, frames, delay=3, speed_kmh=10):
        """Camera model at 30 fps: the override lands `delay` frames late; under
        a fixed shutter the AGC holds gain for one frame, then moves it halfway
        to what the scene needs each frame. scene_us(i) is the exposure the
        scene needs at unity gain. Returns the governor and its overrides."""
        g = ExposureGovernor(smear_limit_mm=2.0)
        in_flight = [None] * delay
        exposure, gain, applied = scene_us(0), 1.0, None
        changes = []
        for i in range(frames):
            out = g.update({'ExposureTime': exposure, 'AnalogueGain': gain}, speed_kmh, now=i / 30)
            if out is not None:
                changes.append(out['exposure_us'])
            in_flight.append(g.exposure_us)
            switched = in_flight[0] != applied
            applied = in_flight.pop(0)
            if applied is None:
                exposure, gain = scene_us(i), 1.0
            else:
                exposure = applied
                if not switched:
                    gain += (max(1.0, scene_us(i) / applied) - gain) / 2
        return g, changes

    def test_cap_settles_in_daylight_with_control_delay(self):
        # Auto picks ~2000 µs at gain 1.0; the 10 km/h / 2 mm cap is 720 µs
        g, changes = self._simulate(lambda i: 2000, frames=60)
        assert changes == [720]
        assert g.exposure_us == 720

    def test_cap_releases_when_scene_bright_at_capped_shutter(self):
        # Cloud clears after 1 s: the scene now needs less than the cap
        g, changes = self._simulate(lambda i: 2000 if i < 30 else 600, frames=120)
        assert changes == [720, None]
        assert g.exposure_us is None

    def test_cap_not_allowed_keeps_gain_limit(self):
        g = ExposureGovernor(max_gain=8.0)
        g.update({'ExposureTime': 2000, 'AnalogueGain': 12.0}, speed_kmh=10, now=0)
        assert (g.exposure_us, g.gain) == (720, 8.0)
        assert g.update({'ExposureTime': 720, 'AnalogueGain': 8.0}, speed_kmh=10, now=1,
                        cap_allowed=False) == {'exposure_us': None, 'gain': 8.0}
        # Detecting from the start: never capped
        g2 = ExposureGovernor()
        assert g2.update({'ExposureTime': 5000, 'AnalogueGain': 2.0}, speed_kmh=10, now=0,
                         cap_allowed=False) is None

    def test_configure_clamps_and_rejects_non_finite(self):
        g = ExposureGovernor()
        g.configure(smear_limit_mm=500, max_gain=1000)
        assert (g.smear_limit_mm, g.max_gain) == (20.0, 64.0)
        for bad in ('inf', float('nan'), '-inf'):
            with pytest.raises(ValueError):
                g.configure(smear_limit_mm=bad)

    def test_parse_max_gain(self):
        for auto in ('auto', 'AUTO', 'none', 'off', '', '  ', None):
            assert parse_max_gain(auto) is None
        assert parse_max_gain('8') == 8.0 and parse_max_gain(16.0) == 16.0
        assert parse_max_gain('0.5') == 1.0 and parse_max_gain('500') == 64.0
        for bad in ('eight', 'nan', 'inf'):
            with pytest.raises(ValueError):
                parse_max_gain(bad)

    def test_follows_speed_changes(self):
        g = ExposureGovernor()
        g.update({'ExposureTime': 3000, 'AnalogueGain': 2.0}, speed_kmh=10, now=0)
        assert g.update({'ExposureTime': 720, 'AnalogueGain': 3.0}, speed_kmh=20, now=1)['exposure_us'] == 360
        assert g.update({'ExposureTime': 360, 'AnalogueGain': 3.0}, speed_kmh=20.5, now=2) is None

    def test_disabled_cap_does_nothing(self):
        g = ExposureGovernor(cap_enabled=False)
        assert g.update({'ExposureTime': 5000, 'AnalogueGain': 2.0}, speed_kmh=10, now=0) is None

    def test_gain_limit_and_recheck(self):
        g = ExposureGovernor(cap_enabled=False, max_gain=8.0)
        assert g.update({'ExposureTime': 500, 'AnalogueGain': 12.0}, None, now=0) == {'exposure_us': None, 'gain': 8.0}
        assert g.update({'ExposureTime': 500, 'AnalogueGain': 8.0}, None, now=1) is None
        assert g.update({'ExposureTime': 500, 'AnalogueGain': 8.0}, None, now=GAIN_RECHECK_S + 1)['gain'] is None

    def test_release(self):
        g = ExposureGovernor()
        assert g.release() is None
        g.update({'ExposureTime': 2000, 'AnalogueGain': 2.0}, speed_kmh=10, now=0)
        assert g.release() == {'exposure_us': None, 'gain': None}


# ---------------------------------------------------------------------------
# Session metadata single source
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSessionMetadata:
    def test_every_field_present_and_trimmed(self):
        md = clean_session_metadata({'crop': '  wheat ', 'notes': 'x' * 900, 'evil': 'no'})
        assert set(md) == set(SESSION_METADATA_FIELDS)
        assert md['crop'] == 'wheat' and len(md['notes']) == 500 and 'evil' not in md
        assert clean_session_metadata(None)['crop'] == ''

    def test_fields_cover_noktura_mapping(self):
        for key in ('crop', 'crop_season', 'weather', 'lighting', 'purpose', 'notes', 'run_id'):
            assert key in SESSION_METADATA_FIELDS

    def test_owl_handler_uses_shared_fields(self, mqtt_publisher):
        mqtt_publisher._handle_command({'action': 'set_session_metadata', 'crop': 'barley',
                                        'crop_season': 'spring', 'lighting': 'natural_sun',
                                        'purpose': 'fungicide', 'run_id': 'abc'})
        md = mqtt_publisher.state['session_metadata']
        assert md['crop_season'] == 'spring' and md['purpose'] == 'fungicide' and md['run_id'] == 'abc'


# ---------------------------------------------------------------------------
# Controller collect helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestUpdateIniText:
    def test_replaces_in_place_keeping_comments(self):
        text = '[A]\n# keep me\nx = 1\n\n[Collect]\n# comment\nlanguage = en\n\n[B]\ny = 2\n'
        out = update_ini_text(text, 'Collect', {'language': 'da', 'jpeg_quality': 90})
        assert '# comment' in out and '# keep me' in out and 'language = da' in out
        cp = configparser.ConfigParser()
        cp.read_string(out)
        assert cp.get('Collect', 'jpeg_quality') == '90' and cp.get('B', 'y') == '2'

    def test_appends_missing_section(self):
        out = update_ini_text('[A]\nx = 1\n', 'Collect', {'language': 'da'})
        cp = configparser.ConfigParser()
        cp.read_string(out)
        assert cp.get('Collect', 'language') == 'da' and cp.get('A', 'x') == '1'

    def test_commented_key_not_treated_as_value(self):
        out = update_ini_text('[Collect]\n# language = xx\n', 'Collect', {'language': 'da'})
        assert '# language = xx' in out and 'language = da' in out


@pytest.mark.unit
class TestParseProvisionCommand:
    NOKTURA = ("sudo bash owl_cloud_provision.sh \\\n    --host mqtt.noktura.tech --port 8883 \\\n"
               "    --device-id rig-farm-1 \\\n    --username rig-farm-1 \\\n"
               "    --password 'AbCdEfGhIjKlMnOpQrStUvWxYz012345' \\\n    --system-ca")

    def test_parses_noktura_command(self):
        data = parse_provision_command(self.NOKTURA)
        assert data['broker_host'] == 'mqtt.noktura.tech' and data['device_id'] == 'rig-farm-1'
        assert data['password'] == 'AbCdEfGhIjKlMnOpQrStUvWxYz012345' and data['broker_port'] == '8883'
        from utils.cloud_provision import validate_payload
        assert validate_payload(data)['username'] == 'rig-farm-1'

    def test_optional_storage_and_portal(self):
        data = parse_provision_command(self.NOKTURA + ' --storage-host hel1.example.com --portal-url https://app.noktura.tech')
        assert data['storage_host'] == 'hel1.example.com' and data['portal_url'] == 'https://app.noktura.tech'

    @pytest.mark.parametrize('text', ['', 'rm -rf /', "sudo bash owl_cloud_provision.sh --host x",
                                      "owl_cloud_provision.sh --host h --device-id d --password-file /tmp/p",
                                      "owl_cloud_provision.sh --password 'unterminated"])
    def test_rejects_other_text(self, text):
        with pytest.raises(ValueError):
            parse_provision_command(text)


@pytest.mark.unit
class TestPresetsAndThresholds:
    def test_defaults_and_overrides(self):
        cp = configparser.ConfigParser()
        assert presets_from_config(cp)['crops'] == DEFAULT_CROPS
        cp.read_string('[Collect]\ncrops = wheat, , barley\nsoft_ratio = 0.5\nnot_saving_s = abc\n')
        assert presets_from_config(cp)['crops'] == ('wheat', 'barley')
        t = thresholds_from_config(cp)
        assert t['soft_ratio'] == 0.5 and t['not_saving_s'] == 10.0


def _owl(**over):
    state = {'connected': True, 'image_sample_enable': True, 'storage_free_mb': 40000,
             'capture': {'speed_kmh': 10.0, 'ground_smear_mm': 1.0, 'record_mode': 'images',
                         'qc': {'median': {'brightness': 110, 'clipped': 0.5, 'dark': 1.0},
                                'baseline_sharpness': 30.0, 'recent_sharpness': [29, 30, 31]}},
             'last_capture_at': 100.0}
    capture = over.pop('capture', None)
    state.update(over)
    if capture:
        state['capture'] = dict(state['capture'], **capture)
    return state


@pytest.mark.unit
class TestCollectWarnings:
    def codes(self, owls, **kw):
        return [w['code'] for w in collect_warnings(owls, now=101.0, **kw)]

    def test_healthy_rig_has_no_warnings(self):
        assert self.codes({'owl-1': _owl(), 'owl-2': _owl()}) == []

    def test_offline_first(self):
        warnings = collect_warnings({'owl-2': _owl(capture={'ground_smear_mm': 3.0}),
                                     'owl-1': _owl(connected=False)}, now=101.0)
        assert warnings[0]['code'] == 'offline' and warnings[0]['severity'] == 'bad'

    def test_blur_risk_suggests_speed(self):
        w = collect_warnings({'owl-1': _owl(capture={'ground_smear_mm': 4.0})}, now=101.0)[0]
        assert w['code'] == 'blur_risk' and w['target_kmh'] == 5.0

    def test_soft_needs_three_in_a_row(self):
        assert 'soft' in self.codes({'o': _owl(capture={'qc': {'median': {}, 'baseline_sharpness': 30.0,
                                                                 'recent_sharpness': [10, 11, 12]}})})
        assert 'soft' not in self.codes({'o': _owl(capture={'qc': {'median': {}, 'baseline_sharpness': 30.0,
                                                                     'recent_sharpness': [10, 25, 12]}})})

    def test_dark_and_overexposed(self):
        dark = _owl(capture={'analogue_gain': 16.0, 'qc': {'median': {'brightness': 20, 'dark': 5, 'clipped': 0}}})
        assert 'too_dark' in self.codes({'o': dark})
        bright = _owl(capture={'qc': {'median': {'brightness': 200, 'dark': 0, 'clipped': 12}}})
        assert 'overexposed' in self.codes({'o': bright})

    def test_not_saving_only_when_moving(self):
        assert 'not_saving' in self.codes({'o': _owl(last_capture_at=50.0)})
        assert 'not_saving' not in self.codes({'o': _owl(last_capture_at=50.0, capture={'speed_kmh': 0.2})})
        assert 'not_saving' not in self.codes({'o': _owl(last_capture_at=50.0, capture={'record_mode': 'video'})})

    def test_paused_slow_is_one_line_and_not_a_fault(self):
        paused = {'paused_slow': True, 'min_speed_kmh': 1.0, 'speed_kmh': 0.5, 'ground_smear_mm': 4.0}
        warnings = collect_warnings({'owl-1': _owl(capture=paused), 'owl-2': _owl(capture=paused)}, now=101.0)
        assert [(w['code'], w['owl'], w['kmh']) for w in warnings] == [('paused_slow', None, 1.0)]

    def test_not_saving_waits_for_the_pause_speed(self):
        # 2 km/h is moving by the default rule, but under a 3 km/h pause
        # speed the OWL is meant to be idle
        slow = {'speed_kmh': 2.0, 'min_speed_kmh': 3.0}
        assert 'not_saving' not in self.codes({'o': _owl(last_capture_at=50.0, capture=slow)})
        assert 'not_saving' in self.codes({'o': _owl(last_capture_at=50.0, capture=dict(slow, speed_kmh=4.0))})

    def test_quality_checks_skip_when_not_recording(self):
        assert self.codes({'o': _owl(image_sample_enable=False, capture={'ground_smear_mm': 9})}) == []

    def test_connectivity_and_gps(self):
        assert 'no_internet' not in self.codes({'o': _owl(internet_ok=False)})
        assert 'no_internet' in self.codes({'o': _owl(internet_ok=False)}, cloud_configured=True)
        assert 'clock' in self.codes({'o': _owl(clock_synced=False)})
        assert 'no_gps' in self.codes({'o': _owl()}, gps_fix=False)
        assert 'storage_low' in self.codes({'o': _owl(storage_free_mb=500)})


@pytest.mark.unit
class TestCollectSession:
    def test_points_only_during_run(self):
        c = CollectSession()
        c.add_capture('owl-1', {'lat': 1, 'lon': 2})
        assert c.points_after()['points'] == []
        run = c.start({'crop': 'wheat'})
        c.add_capture('owl-1', {'lat': 55.1, 'lon': 9.2, 'ts': 't'})
        c.add_capture('owl-1', {'lat': 'bad', 'lon': 9.2})
        c.add_capture('owl-1', {'count': 3})
        data = c.points_after(0)
        assert data['run_id'] == run and len(data['points']) == 1 and data['next'] == 1
        assert c.points_after(1)['points'] == []

    def test_thumbs_keep_latest_two_jpegs(self):
        c = CollectSession()
        c.add_thumb('o', b'not a jpeg')
        assert c.thumb('o') is None
        for i in range(3):
            c.add_thumb('o', b'\xff\xd8' + bytes([i]))
        assert c.thumb('o', 0)[1].endswith(b'\x02') and c.thumb('o', 1)[1].endswith(b'\x01')
        assert c.thumb('o', 2) is None and len(c.thumb_index()['o']) == 2

    def test_upload_requests_dedupe_ack_and_age_out(self):
        c = CollectSession()
        first = c.request_uploads([{'owl': 'owl-1', 'session_id': '20261008/session_090000'},
                                   {'owl': 'owl-1', 'session_id': '20261008/session_090000'},
                                   {'owl': '', 'session_id': 'x'}])
        assert len(first) == 1
        assert c.request_uploads([{'owl': 'owl-1', 'session_id': '20261008/session_090000'}]) == []
        bridged = c.bridged_upload_requests()
        assert bridged[0]['local_id'] == 'owl-1' and 'status' not in bridged[0]
        assert c.ack_upload(first[0]['id'], 'rejected', 'quota')
        assert c.bridged_upload_requests() == []
        # rejected: the farmer may ask again
        assert len(c.request_uploads([{'owl': 'owl-1', 'session_id': '20261008/session_090000'}])) == 1
        assert not c.ack_upload('nope', 'accepted')
        queued = c.request_uploads([{'owl': 'owl-2', 'session_id': 's2'}])[0]
        assert c.ack_upload(queued['id'], 'queued', 'busy')
        assert any(r['id'] == queued['id'] for r in c.bridged_upload_requests())  # still waiting
        assert not c.ack_upload(queued['id'], 'bogus')
        c.ack_upload(queued['id'], 'accepted')
        assert all(r['id'] != queued['id'] for r in c.bridged_upload_requests())
        for r in c.upload_requests.values():
            r['requested_at'] -= UPLOAD_REQUEST_TTL_S + 1
        assert c.bridged_upload_requests() == [] and c.upload_list() == []


# ---------------------------------------------------------------------------
# Networked routes
# ---------------------------------------------------------------------------

@pytest.fixture
def collect_client(networked_test_client, tmp_path):
    client, ctrl = networked_test_client
    import controller.networked.networked as nw
    ctrl.collect = CollectSession()
    ctrl.config = configparser.ConfigParser()
    ctrl.config_file = tmp_path / 'CONTROLLER.ini'
    ctrl.config_file.write_text('[MQTT]\nbroker_ip = localhost\n\n# collect keys below\n')
    ctrl.cloud_enable = True
    ctrl.cloud_connected = True
    ctrl.cloud_device_id = 'rig-farm-1'
    ctrl.cloud_portal_url = ''
    ctrl.gps_manager = None
    ctrl.ui_mode = 'collect'
    ctrl.mqtt_lock = __import__('threading').Lock()
    ctrl.owls_state = {'owl-1': {'connected': True, 'image_sample_enable': False,
                                 'data_sessions': [{'session_id': '20261008/session_090000',
                                                    'total_size': 100}]}}
    return client, ctrl, nw


@pytest.mark.unit
class TestCollectRoutes:
    def test_index_follows_ui_mode(self, collect_client):
        client, ctrl, _ = collect_client
        assert b'collect.js' in client.get('/').data
        ctrl.ui_mode = 'full'
        assert b'collect.js' not in client.get('/').data
        assert b'collect.js' in client.get('/collect').data
        assert client.get('/full').status_code == 200

    def test_start_holds_detection_off_then_records(self, collect_client):
        client, ctrl, _ = collect_client
        resp = client.post('/api/collect/start', json={'metadata': {'crop': 'barley', 'crop_season': 'spring'}})
        assert resp.status_code == 200
        actions = [c.args[1] for c in ctrl.send_command.call_args_list]
        assert actions == ['toggle_detection', 'set_capture_params', 'set_session_metadata', 'toggle_recording']
        md = ctrl.send_command.call_args_list[2].args[2]
        assert md['crop'] == 'barley' and md['run_id'] == resp.get_json()['run_id']
        assert ctrl.send_command.call_args_list[0].args[2] is False

    def test_stop_refreshes_sessions(self, collect_client):
        client, ctrl, _ = collect_client
        client.post('/api/collect/stop', json={})
        actions = [c.args[1] for c in ctrl.send_command.call_args_list]
        assert actions == ['toggle_recording', 'list_data_sessions']

    def test_state_shape(self, collect_client):
        client, ctrl, _ = collect_client
        data = client.get('/api/collect/state').get_json()
        assert data['owls'][0]['id'] == 'owl-1' and data['cloud']['site'] == 'rig-farm-1'
        assert data['recording'] is False and isinstance(data['warnings'], list)

    def test_settings_validation_and_persist(self, collect_client):
        client, ctrl, _ = collect_client
        assert client.post('/api/collect/settings', json={'capture': {'jpeg_quality': 20}}).status_code == 400
        assert client.post('/api/collect/settings', json={'capture': {'sample_mode': 'x'}}).status_code == 400
        assert client.post('/api/collect/settings', json={'language': 'fr'}).status_code == 400
        assert client.post('/api/collect/settings', json={}).status_code == 400
        resp = client.post('/api/collect/settings', json={'capture': {'jpeg_quality': 90, 'shutter_cap': 'false'},
                                                          'language': 'da'})
        assert resp.status_code == 200
        text = ctrl.config_file.read_text()
        assert '# collect keys below' in text and 'jpeg_quality = 90' in text and 'shutter_cap = false' in text
        pushed = [c.args for c in ctrl.send_command.call_args_list if c.args[1] == 'set_capture_params'][-1]
        assert pushed[2]['jpeg_quality'] == 90 and pushed[2]['shutter_cap'] is False

    def test_upload_needs_cloud_and_known_session(self, collect_client):
        client, ctrl, _ = collect_client
        ok = {'items': [{'owl': 'owl-1', 'session_id': '20261008/session_090000'}]}
        assert client.post('/api/collect/upload', json={'items': []}).status_code == 400
        assert client.post('/api/collect/upload', json={'items': [{'owl': 'owl-1', 'session_id': 'other'}]}).status_code == 400
        assert client.post('/api/collect/upload', json=ok).status_code == 200
        assert ctrl.collect.bridged_upload_requests()[0]['session_id'] == '20261008/session_090000'
        ctrl.cloud_enable = False
        assert client.post('/api/collect/upload', json=ok).status_code == 409

    def test_camera_validation(self, collect_client):
        client, ctrl, _ = collect_client
        assert client.post('/api/collect/camera', json={'params': {'awb_mode': 'purple'}}).status_code == 400
        assert client.post('/api/collect/camera', json={'params': {'rotation': '90'}}).status_code == 400
        assert client.post('/api/collect/camera', json={'params': {'max_gain': '999'}}).status_code == 400
        resp = client.post('/api/collect/camera', json={'params': {'exp_compensation': '-30', 'awb_mode': 'auto',
                                                                   'ignored': 1}})
        assert resp.get_json()['applied'] == {'exp_compensation': '-10', 'awb_mode': 'auto'}
        client.post('/api/collect/camera', json={'action': 'calibrate_awb'})
        assert ctrl.send_command.call_args_list[-1].args[1] == 'calibrate_awb'

    def test_thumb_and_points(self, collect_client):
        client, ctrl, _ = collect_client
        assert client.get('/api/collect/thumb/owl-1/0').status_code == 404
        ctrl.collect.add_thumb('owl-1', b'\xff\xd8abc')
        resp = client.get('/api/collect/thumb/owl-1/0')
        assert resp.status_code == 200 and resp.mimetype == 'image/jpeg' and resp.headers['X-Thumb-Seq']
        assert client.get('/api/collect/points?after=abc').get_json()['next'] == 0

    def test_provision_paste(self, collect_client, monkeypatch):
        client, ctrl, nw = collect_client
        assert client.post('/api/collect/cloud/provision', json={'command': 'hello'}).status_code == 400
        from utils import cloud_provision
        calls = {}

        def fake_provision(runner, payload, controller_ini=None):
            calls['payload'] = payload
            return {'device_id': payload['device_id'], 'portal_url': None, 'provisioned_at': 'now'}
        monkeypatch.setattr(cloud_provision, 'provision', fake_provision)
        resp = client.post('/api/collect/cloud/provision', json={'command': TestParseProvisionCommand.NOKTURA})
        assert resp.status_code == 200 and resp.get_json()['device_id'] == 'rig-farm-1'
        assert calls['payload']['replace'] is False
        ctrl.reload_cloud_config.assert_called_once()

        def conflict(*a, **kw):
            raise cloud_provision.CloudSiteConflict('other-site')
        monkeypatch.setattr(cloud_provision, 'provision', conflict)
        resp = client.post('/api/collect/cloud/provision', json={'command': TestParseProvisionCommand.NOKTURA})
        assert resp.status_code == 409 and resp.get_json()['existing_site'] == 'other-site'


@pytest.mark.unit
class TestControllerCloudCommands:
    def make(self):
        import controller.networked.networked as nw
        ctrl = nw.CentralController.__new__(nw.CentralController)
        ctrl.collect = CollectSession()
        ctrl._cloud_feed = nw.CloudFeed()
        return nw, ctrl

    def test_upload_ack_applied(self):
        nw, ctrl = self.make()
        req = ctrl.collect.request_uploads([{'owl': 'owl-1', 'session_id': 's'}])[0]
        ctrl._handle_controller_cloud_command({'action': 'upload_request_ack', 'id': req['id'],
                                               'status': 'accepted'})
        assert ctrl.collect.upload_list()[0]['status'] == 'accepted'

    def test_anything_else_dropped(self):
        nw, ctrl = self.make()
        ctrl.send_command = MagicMock()
        for payload in ({'action': 'toggle_recording'}, {'action': 'reboot'}, 'nope', None):
            ctrl._handle_controller_cloud_command(payload)
        ctrl.send_command.assert_not_called()

    def test_controller_state_carries_upload_requests(self):
        nw, ctrl = self.make()
        ctrl.mqtt_client = MagicMock()
        ctrl.owls_state = {}
        ctrl.collect.request_uploads([{'owl': 'owl-1', 'session_id': 's'}])
        ctrl._publish_controller_presence()
        state_calls = [c for c in ctrl.mqtt_client.publish.call_args_list if c.args[0] == nw.CONTROLLER_STATE_TOPIC]
        payload = json.loads(state_calls[-1].args[1])
        assert payload['upload_requests'][0]['local_id'] == 'owl-1'


# ---------------------------------------------------------------------------
# OWL side: capture params, cloud policy, networked refusal
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestOwlCaptureParams:
    def test_applied_live_not_persisted(self, mqtt_publisher, mock_owl, monkeypatch):
        applied = []

        def apply(params):
            if 'jpeg_quality' in params:
                raise ValueError('bad value')
            if 'video_max_s' in params:
                raise OverflowError('inf')
            applied.append(params)
            return dict(params)
        mock_owl.apply_capture_params = apply
        saved = []
        monkeypatch.setattr(mqtt_publisher, '_handle_save_config', lambda *a, **kw: saved.append(True))
        before = {s: dict(mock_owl.config.items(s)) for s in mock_owl.config.sections()}
        mqtt_publisher._handle_command({'action': 'set_capture_params',
                                        'params': {'sample_mode': 'distance', 'jpeg_quality': 'x',
                                                   'video_max_s': float('inf'),
                                                   'max_gain': 8.0, 'unknown': 1}})
        assert applied == [{'sample_mode': 'distance'}, {'max_gain': 8.0}]
        # Collect settings must never reach the active config (they would
        # carry the shutter cap and sample mode into spraying)
        assert saved == []
        assert {s: dict(mock_owl.config.items(s)) for s in mock_owl.config.sections()} == before


@pytest.mark.unit
class TestVideoClipsInSessions:
    def test_clip_counted_and_collected(self, tmp_path):
        from utils.directory_manager import scan_sessions, collect_session_files, select_preview_images
        sess = tmp_path / '20261008' / 'session_101500'
        sess.mkdir(parents=True)
        (sess / 'clip_101501.mp4').write_bytes(b'v' * 1000)
        (sess / 'a.jpg').write_bytes(b'i' * 10)
        s = scan_sessions(str(tmp_path))[0]
        assert (s['image_count'], s['video_count'], s['image_size'], s['total_size']) == (1, 1, 10, 1010)
        names = sorted(name for name, _ in collect_session_files(str(tmp_path), '20261008/session_101500'))
        assert names == ['images/a.jpg', 'images/clip_101501.mp4']
        # Previews stay images only
        assert [Path(p).name for p in select_preview_images(str(tmp_path), '20261008/session_101500', 5)] == ['a.jpg']


@pytest.mark.unit
class TestOwlCollectLoopSource:
    """Source checks of the loop wiring (the behaviour runs on a real Owl in
    tests/owl/test_mode_matrix.py)."""
    SRC = (REPO / 'owl.py').read_text(encoding='utf-8')

    def _method(self, name):
        start = self.SRC.index(f'    def {name}(')
        return self.SRC[start:self.SRC.index('\n    def ', start + 1)]

    def test_cap_only_while_collecting(self):
        body = self._method('_govern_exposure')
        assert 'self._image_sample_enable and not self._detection_enable' in body
        assert 'cap_allowed=collecting' in body

    def test_video_frame_fixes(self):
        body = self._method('_write_video_frame')
        assert 'isOpened()' in body
        assert 'note_preview(frame, self.gps_data)' in body  # no monotonic float as a datetime

    def test_video_never_encoded_while_detecting(self):
        assert "record_video and self._detection_enable" in self.SRC

    def test_drive_full_checked_for_video_too(self):
        # The check sits at the recording level, not inside the stills branch
        assert '\n                    if self.status_indicator.DRIVE_FULL:' in self.SRC

    def test_bad_max_gain_falls_back_to_auto(self):
        body = self._method('_load_capture_settings')
        assert 'parse_max_gain(' in body and 'except ValueError' in body


@pytest.mark.unit
class TestCloudPolicy:
    def test_parse(self):
        from utils.mqtt_manager import parse_cloud_policy
        assert parse_cloud_policy({'storage_host': 'HEL1.Example.com', 'portal_url': 'https://app.noktura.tech'}) == \
            {'storage_host': 'hel1.example.com', 'portal_host': 'app.noktura.tech'}
        assert parse_cloud_policy({'storage_host': 'bad host/'}) == {}
        assert parse_cloud_policy('x') == {}

    def test_networked_owl_pins_uploads_to_policy_host(self, mqtt_publisher, monkeypatch):
        import utils.mqtt_manager as mm
        monkeypatch.setattr(mm, 'cloud_storage_host', lambda *a: '')
        ran = []
        monkeypatch.setattr(mqtt_publisher, '_handle_command', lambda c: ran.append(c))
        mqtt_publisher._cloud_policy = {'storage_host': 'hel1.example.com'}
        bad = {'action': 'transfer_session', 'method': 'PUT', 'upload_url': 'https://evil.example.net/x'}
        good = {'action': 'transfer_session', 'method': 'PUT', 'upload_url': 'https://hel1.example.com/x'}
        mqtt_publisher._handle_cloud_command(bad)
        mqtt_publisher._handle_cloud_command(good)
        assert ran == [good]


@pytest.mark.unit
class TestNetworkedOwlRefusesProvision:
    def test_refuse(self, tmp_path):
        from utils.cloud_provision import refuse_networked_owl, NetworkedOwlRefused
        ini = tmp_path / 'CONTROLLER.ini'
        ini.write_text('[Network]\nmode = standalone\n')
        refuse_networked_owl(ini)
        refuse_networked_owl(tmp_path / 'missing.ini')
        ini.write_text('[Network]\nmode = networked\n')
        with pytest.raises(NetworkedOwlRefused):
            refuse_networked_owl(ini)


@pytest.mark.unit
class TestSetupScriptDashboardChoice:
    def test_prompt_and_ini_key(self):
        text = (REPO / 'controller' / 'networked' / 'in-cab_controller_setup.sh').read_text(encoding='utf-8')
        assert 'Select dashboard (1-2, default ${ui_default})' in text
        # A re-run defaults to the current screen, never silently back to full.
        assert "^ui[[:space:]]*=[[:space:]]*collect' \"${SCRIPT_DIR}/../../config/CONTROLLER.ini\"" in text
        assert 'ui_choice=${ui_choice:-$ui_default}' in text
        assert 'ui = ${DASHBOARD_UI}' in text
        assert '$0 == "[Cloud]" || $0 == "[Collect]"' in text
        assert 'install_firstboot.sh' in text and '--cloud-helper' in text
        assert 'EDATEC 7' in text and 'SCREEN_WIDTH=1024; SCREEN_HEIGHT=600' in text

    def test_template_documents_keys(self):
        text = (REPO / 'config' / 'CONTROLLER_TEMPLATE.ini').read_text(encoding='utf-8')
        cp = configparser.ConfigParser(inline_comment_prefixes=None)
        cp.read_string(text)
        assert cp.get('WebDashboard', 'ui') == 'full'
        assert cp.get('Collect', 'sample_mode') == 'distance'
        for line in text.splitlines():
            assert not line.strip().startswith(';'), 'INI comments must use #'


@pytest.mark.unit
class TestCollectDeleteSessions:
    """Collect screen 'Delete selected' (v3.21.1)."""

    SID = '20261008/session_090000'

    def _sent(self, ctrl):
        return [(c.args[0], json.loads(c.args[1])) for c in ctrl.mqtt_client.publish.call_args_list
                if c.args[0].endswith('/commands')]

    def _client(self, collect_client):
        client, ctrl, nw = collect_client
        ctrl.mqtt_connected = True
        ctrl.mqtt_client = MagicMock()
        return client, ctrl

    def test_deletes_known_idle_session(self, collect_client):
        client, ctrl = self._client(collect_client)
        resp = client.post('/api/collect/sessions/delete', json={'items': [{'owl': 'owl-1', 'session_id': self.SID}]})
        assert resp.status_code == 200 and resp.get_json()['deleted'] == [{'owl': 'owl-1', 'session_id': self.SID}]
        assert self._sent(ctrl) == [('owl/owl-1/commands', {'action': 'delete_session', 'session_id': self.SID,
                                                            'data_types': ['images']})]

    def test_validation(self, collect_client):
        client, ctrl = self._client(collect_client)
        assert client.post('/api/collect/sessions/delete', json={'items': []}).status_code == 400
        assert client.post('/api/collect/sessions/delete', json={}).status_code == 400
        ctrl.mqtt_connected = False
        assert client.post('/api/collect/sessions/delete',
                           json={'items': [{'owl': 'owl-1', 'session_id': self.SID}]}).status_code == 503

    @pytest.mark.parametrize('setup, reason', [
        (lambda s: s.update(image_sample_enable=True, capture={'session_id': 'session_090000'}), 'recording'),
        (lambda s: s.update(data_transfer={'status': 'uploading', 'session_date': '20261008/session_090000'}), 'uploading'),
    ])
    def test_busy_sessions_skipped(self, collect_client, setup, reason):
        client, ctrl = self._client(collect_client)
        setup(ctrl.owls_state['owl-1'])
        data = client.post('/api/collect/sessions/delete',
                           json={'items': [{'owl': 'owl-1', 'session_id': self.SID}]}).get_json()
        assert data['deleted'] == [] and data['skipped'][0]['reason'] == reason
        assert self._sent(ctrl) == []

    def test_pending_upload_and_unknown_skipped(self, collect_client):
        client, ctrl = self._client(collect_client)
        ctrl.collect.request_uploads([{'owl': 'owl-1', 'session_id': self.SID}])
        data = client.post('/api/collect/sessions/delete', json={'items': [
            {'owl': 'owl-1', 'session_id': self.SID}, {'owl': 'owl-1', 'session_id': '20261008/session_999999'},
            {'owl': 'owl-9', 'session_id': self.SID}]}).get_json()
        assert [s['reason'] for s in data['skipped']] == ['upload_pending', 'not_on_owl', 'not_on_owl']
        assert self._sent(ctrl) == []

    def test_finished_upload_can_be_deleted(self, collect_client):
        client, ctrl = self._client(collect_client)
        ctrl.owls_state['owl-1']['data_transfer'] = {'status': 'complete', 'session_date': self.SID}
        data = client.post('/api/collect/sessions/delete',
                           json={'items': [{'owl': 'owl-1', 'session_id': self.SID}]}).get_json()
        assert len(data['deleted']) == 1


@pytest.mark.unit
class TestCollectLiveView:
    """Collect screen 'Live view' (v3.21.1): per-OWL button, one stream at a time."""

    HTML = (REPO / 'controller/networked/templates/collect.html').read_text(encoding='utf-8')
    JS = (REPO / 'controller/networked/static/js/collect/collect.js').read_text(encoding='utf-8')
    I18N = (REPO / 'controller/shared/js/collect_i18n.js').read_text(encoding='utf-8')

    def test_modal_and_controls(self):
        for el in ('id="live-modal"', 'id="live-img"', 'id="live-close"', 'id="live-owls"', 'id="delete-btn"',
                   'id="delete-modal"'):
            assert el in self.HTML

    def test_uses_existing_proxy_and_drops_stream(self):
        assert "'/api/video_feed/' + encodeURIComponent(id)" in self.JS
        # Each open stream holds a gunicorn thread: close/switch must clear src
        assert self.JS.count("$('live-img').removeAttribute('src')") >= 1
        assert "img.removeAttribute('src');" in self.JS
        assert "addEventListener('pointerdown', open)" in self.JS

    def test_strings_in_both_languages(self):
        for key in ('live:', 'live_title:', 'live_error:', 'close:', 'delete:', 'delete_title:',
                    'delete_hint:', 'deleted:', 'delete_skipped:', 'delete_confirm:'):
            assert self.I18N.count(key) >= 2, key


# ---------------------------------------------------------------------------
# Field round 2026-10-15: crops, pause, upload queue acks, fleet model
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCollectCropsRoutes:
    def test_add_saves_straight_away_and_returns_presets(self, collect_client):
        client, ctrl, _ = collect_client
        resp = client.post('/api/collect/crops', json={'name': 'Pak choi'})
        data = resp.get_json()
        assert resp.status_code == 200 and data['crop'] == 'pak_choi'
        assert data['presets']['crops'][0] == 'pak_choi'
        assert data['presets']['custom_crops'] == ['pak_choi']
        text = ctrl.config_file.read_text()
        assert 'custom_crops = pak_choi' in text and '# collect keys below' in text

    def test_add_built_in_saves_nothing(self, collect_client):
        client, ctrl, _ = collect_client
        resp = client.post('/api/collect/crops', json={'name': 'Cabbage'})
        assert resp.status_code == 200 and resp.get_json()['crop'] == 'cabbage'
        assert 'custom_crops' not in ctrl.config_file.read_text()

    def test_add_rejects_empty(self, collect_client):
        client, _, _ = collect_client
        assert client.post('/api/collect/crops', json={'name': '***'}).status_code == 400
        assert client.post('/api/collect/crops', json={}).status_code == 400

    def test_remove(self, collect_client):
        client, ctrl, _ = collect_client
        client.post('/api/collect/crops', json={'name': 'Swede'})
        client.post('/api/collect/crops', json={'name': 'Pak choi'})
        resp = client.delete('/api/collect/crops/swede')
        assert resp.status_code == 200
        assert resp.get_json()['presets']['custom_crops'] == ['pak_choi']
        assert client.delete('/api/collect/crops/wheat').status_code == 404


@pytest.mark.unit
class TestCollectPause:
    def test_pause_needs_a_recording(self, collect_client):
        client, ctrl, _ = collect_client
        assert client.post('/api/collect/pause').status_code == 409
        ctrl.send_command.assert_not_called()

    def test_pause_and_resume_broadcast(self, collect_client):
        client, ctrl, _ = collect_client
        ctrl.owls_state['owl-1']['image_sample_enable'] = True
        assert client.post('/api/collect/pause').get_json() == {'success': True, 'paused': True}
        assert ctrl.send_command.call_args_list[-1].args == ('all', 'set_recording_paused', True)
        assert client.post('/api/collect/resume').get_json()['paused'] is False
        assert ctrl.send_command.call_args_list[-1].args == ('all', 'set_recording_paused', False)

    def test_paused_owl_raises_no_saving_warnings(self):
        owl = _owl(last_capture_at=0.0, capture={'paused': True, 'ground_smear_mm': 9.0})
        assert collect_warnings({'owl-1': owl}, now=101.0) == []


@pytest.mark.unit
class TestUploadAckDetails:
    def test_queue_position_and_outcomes_kept(self):
        collect = CollectSession()
        req = collect.request_uploads([{'owl': 'owl-1', 'session_id': 's'}])[0]
        assert collect.ack_upload(req['id'], 'queued', 'busy', {'position': 2, 'queue_length': 5})
        row = collect.upload_list()[0]
        assert (row['status'], row['position'], row['queue_length']) == ('queued', 2, 5)
        assert collect.ack_upload(req['id'], 'retrying', 'connection_reset',
                                  {'attempt': 2, 'max_attempts': 3, 'retry_in_s': 120, 'position': 'x'})
        row = collect.upload_list()[0]
        assert row['attempt'] == 2 and row['retry_in_s'] == 120 and 'position' not in row
        assert collect.ack_upload(req['id'], 'stored')
        assert collect.bridged_upload_requests() == []

    def test_unknown_status_refused(self):
        collect = CollectSession()
        req = collect.request_uploads([{'owl': 'owl-1', 'session_id': 's'}])[0]
        assert not collect.ack_upload(req['id'], 'teleported')


@pytest.mark.unit
class TestFleetModel:
    def make(self, tmp_path, ini=''):
        import controller.networked.networked as nw
        ctrl = nw.CentralController.__new__(nw.CentralController)
        ctrl.config_file = tmp_path / 'CONTROLLER.ini'
        if ini or not ctrl.config_file.exists():
            ctrl.config_file.write_text('[MQTT]\nbroker_ip = localhost\n' + ini)
        ctrl.config = configparser.ConfigParser()
        ctrl.config.read(ctrl.config_file)
        ctrl.desired_state = {}
        ctrl.owls_state = {}
        ctrl.mqtt_lock = __import__('threading').Lock()
        ctrl.mqtt_client = MagicMock()
        ctrl.mqtt_client.publish.return_value = MagicMock(rc=0)
        ctrl.mqtt_connected = True
        ctrl._load_fleet_models()
        return nw, ctrl

    def pushed_models(self, ctrl):
        payloads = [json.loads(c.args[1]) for c in ctrl.mqtt_client.publish.call_args_list]
        return [p['value'] for p in payloads if p.get('action') == 'set_model']

    def test_choice_for_all_is_saved_and_survives_a_restart(self, tmp_path):
        _, ctrl = self.make(tmp_path)
        ctrl.send_command('all', 'set_model', 'weeds-v3')
        assert '[Fleet]' in ctrl.config_file.read_text()
        _, restarted = self.make(tmp_path)
        assert restarted.desired_state['all']['model'] == 'weeds-v3'

    def test_one_owl_choice_does_not_change_the_fleet(self, tmp_path):
        _, ctrl = self.make(tmp_path, '[Fleet]\nmodel = weeds-v3\n')
        ctrl.send_command('owl-2', 'set_model', 'weeds-v4')
        assert ctrl.desired_state['all']['model'] == 'weeds-v3'
        assert ctrl.desired_state['owl-2']['model'] == 'weeds-v4'
        assert 'model_owl-2 = weeds-v4' in ctrl.config_file.read_text()

    def test_use_on_all_clears_one_owl_choices(self, tmp_path):
        _, ctrl = self.make(tmp_path, '[Fleet]\nmodel = weeds-v3\nmodel_owl-2 = weeds-v4\n')
        assert ctrl.desired_state['owl-2']['model'] == 'weeds-v4'
        ctrl.send_command('all', 'set_model', 'weeds-v5')
        assert 'model' not in ctrl.desired_state['owl-2']
        cfg = configparser.ConfigParser()
        cfg.read(ctrl.config_file)
        assert cfg.get('Fleet', 'model') == 'weeds-v5' and cfg.get('Fleet', 'model_owl-2') == ''

    def test_reconnect_pushes_only_a_different_model(self, tmp_path):
        _, ctrl = self.make(tmp_path, '[Fleet]\nmodel = weeds-v3\n')
        ctrl.owls_state = {'owl-1': {'current_model': 'weeds-v3'}, 'owl-2': {'current_model': 'other'}}
        ctrl._push_desired_state('owl-1')
        assert self.pushed_models(ctrl) == []
        ctrl._push_desired_state('owl-2')
        assert self.pushed_models(ctrl) == ['weeds-v3']

    def test_paused_run_stays_paused_for_a_rejoining_owl(self, tmp_path):
        _, ctrl = self.make(tmp_path)
        ctrl.desired_state['all'] = {'image_sample_enable': True}
        ctrl.send_command('all', 'set_recording_paused', True)
        ctrl.mqtt_client.publish.reset_mock()
        ctrl._push_desired_state('owl-1')
        actions = [json.loads(c.args[1])['action'] for c in ctrl.mqtt_client.publish.call_args_list]
        assert actions.index('set_recording_paused') > actions.index('set_image_sample_enable')
