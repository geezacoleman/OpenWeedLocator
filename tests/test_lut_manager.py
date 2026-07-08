"""Tests for the LUT detection engine — histogram build, bake, apply,
stroke sampling and profile persistence."""

import os

import numpy as np
import cv2
import pytest

from utils.lut_manager import (
    LUT_BINS,
    LUT_SIZE,
    MIN_CLASS_PIXELS,
    LUTProfileError,
    LUTProfileManager,
    apply_lut,
    bake_lut,
    build_histograms,
    lut_swatch,
    lut_swatch_png,
    pixel_histogram,
    sample_stroke_pixels,
    sensitivity_to_ratio,
    subsample_pixels,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GREEN = (40, 180, 60)     # BGR weed colour
BROWN = (60, 90, 120)     # BGR soil colour


def _pixels(colour, n=1000, jitter=0):
    """(n, 3) uint8 pixel block of one colour, optionally with noise."""
    px = np.tile(np.array(colour, dtype=np.float32), (n, 1))
    if jitter:
        rng = np.random.default_rng(0)
        px += rng.normal(0, jitter, px.shape)
    return np.clip(px, 0, 255).astype(np.uint8)


def _disc_scene(h=240, w=320):
    """Brown frame with a green disc; returns (frame, inside_mask)."""
    frame = np.zeros((h, w, 3), np.uint8)
    frame[:] = BROWN
    cv2.circle(frame, (w // 2, h // 2), 50, GREEN, -1)
    yy, xx = np.mgrid[0:h, 0:w]
    inside = (xx - w // 2) ** 2 + (yy - h // 2) ** 2 < 46 ** 2
    return frame, inside


# ---------------------------------------------------------------------------
# Histograms
# ---------------------------------------------------------------------------

class TestHistograms:
    def test_single_colour_lands_in_one_bin(self):
        hist = pixel_histogram(_pixels(GREEN, 500))
        assert hist.sum() == 500
        assert (hist > 0).sum() == 1
        b, g, r = GREEN
        assert hist[r >> 3, g >> 3, b >> 3] == 500

    def test_empty_input(self):
        hist = pixel_histogram(np.empty((0, 3), np.uint8))
        assert hist.shape == (LUT_BINS, LUT_BINS, LUT_BINS)
        assert hist.sum() == 0

    def test_build_histograms_pair(self):
        hf, hb = build_histograms(_pixels(GREEN, 100), _pixels(BROWN, 200))
        assert hf.sum() == 100
        assert hb.sum() == 200


# ---------------------------------------------------------------------------
# Bake
# ---------------------------------------------------------------------------

class TestBake:
    def test_painted_colours_classified(self):
        hf, hb = build_histograms(_pixels(GREEN), _pixels(BROWN))
        lut = bake_lut(hf, hb, sensitivity=50)
        assert lut.shape == (LUT_SIZE,)
        assert lut.dtype == np.uint8
        b, g, r = GREEN
        assert lut[(r >> 3) * 1024 + (g >> 3) * 32 + (b >> 3)] == 255
        b, g, r = BROWN
        assert lut[(r >> 3) * 1024 + (g >> 3) * 32 + (b >> 3)] == 0

    def test_unseen_colours_never_weed_even_at_max_sensitivity(self):
        hf, hb = build_histograms(_pixels(GREEN), _pixels(BROWN))
        lut = bake_lut(hf, hb, sensitivity=100)
        # blue: far from anything painted — must stay off at any sensitivity
        b, g, r = 250, 10, 10
        assert lut[(r >> 3) * 1024 + (g >> 3) * 32 + (b >> 3)] == 0

    def test_sensitivity_monotonic(self):
        hf, hb = build_histograms(
            _pixels(GREEN, 2000, jitter=25), _pixels(BROWN, 2000, jitter=25))
        counts = [int((bake_lut(hf, hb, s) > 0).sum()) for s in (0, 25, 50, 75, 100)]
        assert counts == sorted(counts), f'not monotonic: {counts}'
        assert counts[0] < counts[-1]

    def test_smoothing_generalises_to_neighbour_bins(self):
        hf, hb = build_histograms(_pixels(GREEN), _pixels(BROWN))
        lut3d = bake_lut(hf, hb, sensitivity=80).reshape(
            LUT_BINS, LUT_BINS, LUT_BINS)
        b, g, r = GREEN
        # a colour one bin greener than anything painted still classifies
        assert lut3d[(r >> 3), (g >> 3) + 1, (b >> 3)] == 255

    def test_empty_class_raises(self):
        hf, hb = build_histograms(_pixels(GREEN), np.empty((0, 3), np.uint8))
        with pytest.raises(LUTProfileError):
            bake_lut(hf, hb)

    def test_sensitivity_to_ratio_endpoints(self):
        assert sensitivity_to_ratio(0) == pytest.approx(20.0)
        assert sensitivity_to_ratio(50) == pytest.approx(2.0)
        assert sensitivity_to_ratio(100) == pytest.approx(0.2)

    def test_spill_only_bins_not_sprayed_at_low_sensitivity(self):
        """A handful of stray dark pixels must not spray their bins at
        strict sensitivities (the eps-ratio blow-up regression)."""
        dark = (10, 10, 10)
        fg = np.vstack([_pixels(GREEN, 100_000), _pixels(dark, 10)])
        hb = pixel_histogram(_pixels(BROWN, 100_000))
        hf = pixel_histogram(fg)
        b, g, r = dark
        flat = (r >> 3) * 1024 + (g >> 3) * 32 + (b >> 3)
        for sensitivity in (25, 50):
            lut = bake_lut(hf, hb, sensitivity)
            assert lut[flat] == 0, f'dark bin sprayed at sensitivity {sensitivity}'

    def test_swatch_no_near_black_colours_at_default_sensitivity(self):
        """Swatch of a green profile with stray dark pixels has no black bars."""
        fg = np.vstack([_pixels(GREEN, 100_000, jitter=15), _pixels((10, 10, 10), 10)])
        hf = pixel_histogram(fg)
        hb = pixel_histogram(_pixels(BROWN, 100_000, jitter=15))
        lut3d = bake_lut(hf, hb, 50).reshape(LUT_BINS, LUT_BINS, LUT_BINS)
        r_bins, g_bins, b_bins = np.nonzero(lut3d)
        centres = np.stack([b_bins, g_bins, r_bins], axis=1) * 8 + 4
        assert centres.max(axis=1).min() > 40, 'near-black bin in swatch'


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

class TestApply:
    def test_matches_naive_reference(self):
        rng = np.random.default_rng(1)
        image = rng.integers(0, 256, (60, 80, 3), dtype=np.uint8)
        lut = rng.integers(0, 2, LUT_SIZE).astype(np.uint8) * 255

        mask = apply_lut(image, lut)

        b = image[:, :, 0] >> 3
        g = image[:, :, 1] >> 3
        r = image[:, :, 2] >> 3
        idx = (r.astype(np.int64) << 10) | (g.astype(np.int64) << 5) | b
        assert np.array_equal(mask, lut[idx])

    def test_reusable_index_buffer(self):
        image = np.full((30, 40, 3), 100, np.uint8)
        lut = np.zeros(LUT_SIZE, np.uint8)
        buf = np.empty((30, 40), np.uint16)
        mask = apply_lut(image, lut, index_buffer=buf)
        assert mask.shape == (30, 40)
        # wrong-shape buffer is ignored, not an error
        bad = np.empty((10, 10), np.uint16)
        mask2 = apply_lut(image, lut, index_buffer=bad)
        assert mask2.shape == (30, 40)

    def test_end_to_end_disc_scene(self):
        """Painted profile detects the weed disc interior, not the soil."""
        frame, inside = _disc_scene()
        # noisy scene, like a real sensor
        rng = np.random.default_rng(2)
        noisy = np.clip(frame.astype(np.float32)
                        + rng.normal(0, 3, frame.shape), 0, 255).astype(np.uint8)

        fg = noisy[inside]
        bg = noisy[~inside]
        hf, hb = build_histograms(fg, bg)
        lut = bake_lut(hf, hb, sensitivity=50)
        mask = apply_lut(noisy, lut)

        assert mask[inside].mean() / 255 > 0.90
        assert mask[~inside].mean() / 255 < 0.02


# ---------------------------------------------------------------------------
# Swatch (LUT visualisation)
# ---------------------------------------------------------------------------

class TestSwatch:
    def test_green_profile_swatch_is_green(self):
        hf, hb = build_histograms(_pixels(GREEN, 2000, jitter=15),
                                  _pixels(BROWN, 2000, jitter=15))
        image, coverage = lut_swatch(bake_lut(hf, hb, 50), width=64, height=8)
        assert image.shape == (8, 64, 3)
        assert 0 < coverage < 0.5
        # dominant channel across the strip is green (BGR index 1)
        means = image.reshape(-1, 3).mean(axis=0)
        assert means[1] > means[0] and means[1] > means[2]

    def test_empty_lut_renders_grey(self):
        image, coverage = lut_swatch(np.zeros(LUT_SIZE, np.uint8))
        assert coverage == 0.0
        assert (image == image[0, 0]).all()   # uniform placeholder

    def test_png_round_trip(self):
        hf, hb = build_histograms(_pixels(GREEN), _pixels(BROWN))
        png, coverage = lut_swatch_png(bake_lut(hf, hb, 50))
        decoded = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        assert decoded is not None and decoded.shape[1] == 256
        assert coverage > 0

    def test_coverage_grows_with_sensitivity(self):
        hf, hb = build_histograms(_pixels(GREEN, 2000, jitter=25),
                                  _pixels(BROWN, 2000, jitter=25))
        _, low = lut_swatch(bake_lut(hf, hb, 10))
        _, high = lut_swatch(bake_lut(hf, hb, 90))
        assert high >= low


# ---------------------------------------------------------------------------
# Stroke sampling
# ---------------------------------------------------------------------------

class TestStrokeSampling:
    def test_line_stroke_samples_disc_colour(self):
        frame, _ = _disc_scene()
        stroke = {'points': [[0.4, 0.5], [0.6, 0.5]], 'radius': 0.03}
        px = sample_stroke_pixels(frame, stroke)
        assert px.shape[0] > 100
        # every sampled pixel is the disc colour (stroke stays inside disc)
        assert (px == np.array(GREEN, np.uint8)).all()

    def test_single_tap_is_a_dab(self):
        frame, _ = _disc_scene()
        stroke = {'points': [[0.5, 0.5]], 'radius': 0.02}
        px = sample_stroke_pixels(frame, stroke)
        assert px.shape[0] > 10

    def test_out_of_range_points_clamped(self):
        frame, _ = _disc_scene()
        stroke = {'points': [[-0.5, 0.5], [1.5, 0.5]], 'radius': 0.02}
        px = sample_stroke_pixels(frame, stroke)  # must not raise
        assert px.ndim == 2 and px.shape[1] == 3

    def test_empty_stroke(self):
        frame, _ = _disc_scene()
        assert sample_stroke_pixels(frame, {'points': []}).shape == (0, 3)

    def test_subsample_cap(self):
        px = _pixels(GREEN, 5000)
        out = subsample_pixels(px, cap=1000)
        assert out.shape == (1000, 3)
        assert subsample_pixels(px, cap=10000).shape == (5000, 3)


# ---------------------------------------------------------------------------
# Profile manager
# ---------------------------------------------------------------------------

class TestProfileManager:
    def _manager(self, tmp_path):
        return LUTProfileManager(tmp_path / 'lut_profiles')

    def _save_valid(self, mgr, name='paddock_one'):
        return mgr.save(name, _pixels(GREEN, 1000), _pixels(BROWN, 1000),
                        strokes=[{'label': 'weed', 'points': [[0.5, 0.5]],
                                  'radius': 0.02}],
                        frames=2)

    def test_save_load_round_trip(self, tmp_path):
        mgr = self._manager(tmp_path)
        meta = self._save_valid(mgr)
        assert meta['fg_pixels'] == 1000

        profile = mgr.load('paddock_one')
        assert profile['hist_fg'].sum() == 1000
        assert profile['hist_bg'].sum() == 1000
        assert profile['meta']['frames'] == 2
        assert profile['strokes'][0]['label'] == 'weed'
        assert profile['fg_pixels'].shape == (1000, 3)

    def test_load_and_bake(self, tmp_path):
        mgr = self._manager(tmp_path)
        self._save_valid(mgr)
        lut = mgr.load_and_bake('paddock_one', sensitivity=50)
        b, g, r = GREEN
        assert lut[(r >> 3) * 1024 + (g >> 3) * 32 + (b >> 3)] == 255

    def test_save_refuses_insufficient_pixels(self, tmp_path):
        mgr = self._manager(tmp_path)
        with pytest.raises(LUTProfileError, match='at least'):
            mgr.save('thin', _pixels(GREEN, MIN_CLASS_PIXELS - 1),
                     _pixels(BROWN, 1000))
        with pytest.raises(LUTProfileError, match='at least'):
            mgr.save('thin', _pixels(GREEN, 1000),
                     _pixels(BROWN, MIN_CLASS_PIXELS - 1))

    def test_invalid_names_rejected(self, tmp_path):
        mgr = self._manager(tmp_path)
        for bad in ('', 'Has Space', 'UPPER', '../evil', 'a' * 40, '1starts_num'):
            with pytest.raises(LUTProfileError):
                mgr.profile_path(bad)

    def test_missing_profile_raises(self, tmp_path):
        mgr = self._manager(tmp_path)
        with pytest.raises(LUTProfileError, match='not found'):
            mgr.load('ghost')

    def test_corrupt_file_raises_typed_error(self, tmp_path):
        mgr = self._manager(tmp_path)
        os.makedirs(mgr.profile_dir, exist_ok=True)
        with open(os.path.join(mgr.profile_dir, 'broken.npz'), 'wb') as f:
            f.write(b'not an npz file at all')
        with pytest.raises(LUTProfileError, match='Corrupt'):
            mgr.load('broken')

    def test_list_profiles(self, tmp_path):
        mgr = self._manager(tmp_path)
        assert mgr.list_profiles() == []
        self._save_valid(mgr, 'alpha')
        self._save_valid(mgr, 'beta')
        names = [p['name'] for p in mgr.list_profiles()]
        assert names == ['alpha', 'beta']
        assert all('created' in p for p in mgr.list_profiles())

    def test_list_skips_corrupt_files(self, tmp_path):
        mgr = self._manager(tmp_path)
        self._save_valid(mgr, 'good')
        with open(os.path.join(mgr.profile_dir, 'bad.npz'), 'wb') as f:
            f.write(b'garbage')
        assert [p['name'] for p in mgr.list_profiles()] == ['good']

    def test_delete(self, tmp_path):
        mgr = self._manager(tmp_path)
        self._save_valid(mgr)
        mgr.delete('paddock_one')
        assert not mgr.exists('paddock_one')
        with pytest.raises(LUTProfileError):
            mgr.delete('paddock_one')

    def test_no_temp_files_left_behind(self, tmp_path):
        mgr = self._manager(tmp_path)
        self._save_valid(mgr)
        leftovers = [f for f in os.listdir(mgr.profile_dir)
                     if f.startswith('.owl_lut_')]
        assert leftovers == []

    def test_builtin_starter_profile(self, tmp_path):
        mgr = self._manager(tmp_path)
        mgr.ensure_builtin_profiles()
        assert mgr.exists('starter')
        profiles = mgr.list_profiles()
        assert [p['name'] for p in profiles] == ['starter']
        assert profiles[0]['is_builtin'] is True

        # Generic greens detected, soil browns not
        lut = mgr.load_and_bake('starter', 50)
        frame = np.zeros((40, 60, 3), np.uint8)
        frame[:, :30] = GREEN
        frame[:, 30:] = BROWN
        mask = apply_lut(frame, lut)
        assert mask[:, :30].mean() / 255 > 0.9
        assert mask[:, 30:].mean() / 255 < 0.1

        # Cannot be deleted; ensure is idempotent
        with pytest.raises(LUTProfileError, match='built-in'):
            mgr.delete('starter')
        mgr.ensure_builtin_profiles()
        assert len(mgr.list_profiles()) == 1

    def test_custom_profiles_not_flagged_builtin(self, tmp_path):
        mgr = self._manager(tmp_path)
        self._save_valid(mgr, 'mine')
        assert mgr.list_profiles()[0]['is_builtin'] is False

    def test_thumbnail_round_trip(self, tmp_path):
        mgr = self._manager(tmp_path)
        jpeg = cv2.imencode('.jpg', np.zeros((10, 10, 3), np.uint8))[1].tobytes()
        mgr.save('with_thumb', _pixels(GREEN, 1000), _pixels(BROWN, 1000),
                 thumbnail_jpeg=jpeg)
        profile = mgr.load('with_thumb')
        assert profile['thumbnail'] == jpeg
        assert mgr.load('with_thumb')['thumbnail'] is not None
