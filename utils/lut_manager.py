"""
LUT-based green detection for OWL.

A painted "profile" stores weed (foreground) and background pixel samples
collected from operator brush strokes on frozen camera frames. The samples
build a pair of 32x32x32 colour histograms which bake into a 32KB binary
lookup table: one table lookup per pixel classifies any BGR colour as
weed / not-weed. Any colour decision boundary — including ones no threshold
slider can express — costs the same at runtime.

The same module runs on the controller (live mask preview while painting)
and on the OWL (detection loop), so preview and field behaviour cannot
diverge.

Sensitivity (0-100) moves the Bayes likelihood-ratio threshold and only
requires re-baking the table (~ms), never repainting.
"""

import json
import logging
import os
import re
import tempfile
import time

import cv2
import numpy as np

logger = logging.getLogger(__name__)

LUT_BINS = 32                # 5 bits per channel
LUT_SHIFT = 3                # quantise 8-bit channel -> 5 bits
LUT_SIZE = LUT_BINS ** 3
PROFILE_VERSION = 1
PROFILE_SUFFIX = '.npz'
DEFAULT_SENSITIVITY = 50

# A profile must contain at least this many painted pixels per class —
# a profile with no background evidence would classify everything as weed.
MIN_CLASS_PIXELS = 300

# Stored pixel samples are capped per class (uniform subsample beyond this)
# to keep profile files small while staying re-trainable.
MAX_STORED_PIXELS = 200_000

# Likelihood-ratio thresholds at sensitivity 0 and 100 (log-linear between;
# sensitivity 50 -> ratio 2.0). Higher sensitivity -> lower threshold ->
# more colours classified as weed.
_RATIO_AT_0 = 20.0
_RATIO_AT_100 = 0.2

NAME_PATTERN = re.compile(r'^[a-z][a-z0-9_]{0,30}$')

# Built-in profiles are generated on first run (the profile folder is
# git-ignored site data) and cannot be deleted — like BUILTIN_PRESETS.
BUILTIN_PROFILES = frozenset({'starter'})


class LUTProfileError(Exception):
    """Raised for missing, corrupt or invalid LUT profiles."""


# ----------------------------------------------------------------------
# Core maths
# ----------------------------------------------------------------------

def pixel_histogram(pixels):
    """Histogram (N, 3) uint8 BGR pixels into a (32, 32, 32) uint32 array.

    Axes are (r, g, b) so that hist.ravel() matches the flat index used by
    apply_lut().
    """
    pixels = np.asarray(pixels, dtype=np.uint8).reshape(-1, 3)
    if pixels.shape[0] == 0:
        return np.zeros((LUT_BINS, LUT_BINS, LUT_BINS), dtype=np.uint32)
    b = pixels[:, 0] >> LUT_SHIFT
    g = pixels[:, 1] >> LUT_SHIFT
    r = pixels[:, 2] >> LUT_SHIFT
    idx = (r.astype(np.int64) << 10) | (g.astype(np.int64) << 5) | b
    counts = np.bincount(idx, minlength=LUT_SIZE)
    return counts.astype(np.uint32).reshape(LUT_BINS, LUT_BINS, LUT_BINS)


def build_histograms(fg_pixels, bg_pixels):
    """Return (hist_fg, hist_bg) from painted pixel sample arrays."""
    return pixel_histogram(fg_pixels), pixel_histogram(bg_pixels)


def _smooth3(hist):
    """Separable 3-tap binomial ([0.25, 0.5, 0.25]) blur along each axis.

    Spreads painted colour evidence to neighbouring bins so the profile
    generalises to colours slightly off the ones painted. Edge-replicated,
    no scipy dependency.
    """
    out = np.asarray(hist, dtype=np.float64)
    for axis in range(3):
        pad = [(1, 1) if a == axis else (0, 0) for a in range(3)]
        p = np.pad(out, pad, mode='edge')
        lo = [slice(None)] * 3
        mid = [slice(None)] * 3
        hi = [slice(None)] * 3
        lo[axis] = slice(0, -2)
        mid[axis] = slice(1, -1)
        hi[axis] = slice(2, None)
        out = 0.25 * p[tuple(lo)] + 0.5 * p[tuple(mid)] + 0.25 * p[tuple(hi)]
    return out


def sensitivity_to_ratio(sensitivity):
    """Map sensitivity 0-100 to a likelihood-ratio threshold (log-linear)."""
    s = float(np.clip(sensitivity, 0, 100))
    return _RATIO_AT_0 * (_RATIO_AT_100 / _RATIO_AT_0) ** (s / 100.0)


def bake_lut(hist_fg, hist_bg, sensitivity=DEFAULT_SENSITIVITY):
    """Bake histograms into a flat LUT_SIZE uint8 table (values 0 / 255).

    Bayes likelihood ratio on class-normalised, smoothed histograms with a
    uniform Laplace prior (alpha = one count spread over the whole colour
    space). Bins holding near-zero evidence in both classes get ratio ~= 1,
    so smoothing spill alone cannot classify a colour as weed at strict
    sensitivities. Colours with zero weed evidence (even after smoothing)
    are never classified as weed, regardless of sensitivity.
    """
    hf = _smooth3(hist_fg)
    hb = _smooth3(hist_bg)
    fg_total = hf.sum()
    bg_total = hb.sum()
    if fg_total <= 0 or bg_total <= 0:
        raise LUTProfileError('profile has an empty class histogram')

    alpha = 1.0 / LUT_SIZE
    p_fg = hf / fg_total
    p_bg = hb / bg_total
    ratio = (p_fg + alpha) / (p_bg + alpha)

    threshold = sensitivity_to_ratio(sensitivity)
    lut = (ratio > threshold) & (hf > 0)
    return (lut.astype(np.uint8) * 255).ravel()


def apply_lut(image, lut, index_buffer=None):
    """Classify an HxWx3 uint8 BGR image against a baked LUT.

    Returns an HxW uint8 binary mask (0 / 255). Pass a reusable uint16
    *index_buffer* of shape HxW from a single-threaded caller (the detection
    loop) to avoid per-frame allocation; omit it from multi-threaded callers.
    """
    h, w = image.shape[:2]
    if index_buffer is not None and index_buffer.shape == (h, w):
        idx = index_buffer
    else:
        idx = np.empty((h, w), dtype=np.uint16)
    np.right_shift(image[:, :, 2], LUT_SHIFT, out=idx, casting='unsafe')
    idx <<= 5
    idx |= image[:, :, 1] >> LUT_SHIFT
    idx <<= 5
    idx |= image[:, :, 0] >> LUT_SHIFT
    return lut.take(idx)


def lut_swatch(lut, width=256, height=28):
    """Render the colours a baked LUT classifies as weed into a hue-sorted
    swatch strip (BGR image) — "these colours get sprayed".

    Returns (image, coverage) where coverage is the fraction of the whole
    colour space classified as weed. An empty LUT renders a neutral grey
    strip.
    """
    lut3 = np.asarray(lut).reshape(LUT_BINS, LUT_BINS, LUT_BINS)
    r_bins, g_bins, b_bins = np.nonzero(lut3)
    coverage = r_bins.size / LUT_SIZE
    if r_bins.size == 0:
        return np.full((height, width, 3), 225, dtype=np.uint8), 0.0

    # Bin centres in BGR, ordered by hue then value so the strip reads
    # like a spectrum of the profile's weed colours
    colours = (np.stack([b_bins, g_bins, r_bins], axis=1)
               .astype(np.uint8) << LUT_SHIFT) + (1 << (LUT_SHIFT - 1))
    hsv = cv2.cvtColor(colours.reshape(1, -1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
    order = np.lexsort((hsv[:, 2], hsv[:, 1], hsv[:, 0]))
    colours = colours[order]

    idx = np.linspace(0, colours.shape[0] - 1, width).astype(np.int64)
    strip = colours[idx]
    return np.repeat(strip[np.newaxis, :, :], height, axis=0), coverage


def lut_swatch_png(lut, width=256, height=28):
    """PNG-encoded lut_swatch(). Returns (png_bytes, coverage)."""
    image, coverage = lut_swatch(lut, width, height)
    ok, png = cv2.imencode('.png', image)
    if not ok:
        raise LUTProfileError('Failed to encode LUT swatch')
    return png.tobytes(), coverage


def sample_stroke_pixels(frame, stroke):
    """Sample the BGR pixels under one brush stroke.

    *stroke* is a dict with:
      points: [[x, y], ...] normalised 0-1 relative to frame width/height
      radius: brush radius as a fraction of frame width
    Returns an (N, 3) uint8 array (possibly empty).
    """
    h, w = frame.shape[:2]
    points = stroke.get('points') or []
    if not points:
        return np.empty((0, 3), dtype=np.uint8)

    radius_px = max(1, int(round(float(stroke.get('radius', 0.02)) * w)))
    pts = np.array(
        [[int(round(float(x) * (w - 1))), int(round(float(y) * (h - 1)))]
         for x, y in points], dtype=np.int32)
    np.clip(pts[:, 0], 0, w - 1, out=pts[:, 0])
    np.clip(pts[:, 1], 0, h - 1, out=pts[:, 1])

    mask = np.zeros((h, w), dtype=np.uint8)
    if len(pts) == 1:
        cv2.circle(mask, tuple(pts[0]), radius_px, 255, -1)
    else:
        cv2.polylines(mask, [pts], False, 255, thickness=2 * radius_px)
        # round caps so single dabs at stroke ends sample the full brush
        cv2.circle(mask, tuple(pts[0]), radius_px, 255, -1)
        cv2.circle(mask, tuple(pts[-1]), radius_px, 255, -1)
    return frame[mask > 0]


def _hsv_grid_pixels(hue_range, sat_range, val_range, step=(4, 16, 16)):
    """Generate BGR pixels sampling an HSV box — synthetic training samples
    for the built-in starter profile."""
    h = np.arange(hue_range[0], hue_range[1] + 1, step[0])
    s = np.arange(sat_range[0], sat_range[1] + 1, step[1])
    v = np.arange(val_range[0], val_range[1] + 1, step[2])
    hh, ss, vv = np.meshgrid(h, s, v, indexing='ij')
    hsv = np.stack([hh, ss, vv], axis=-1).reshape(1, -1, 3).astype(np.uint8)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR).reshape(-1, 3)


def generate_starter_pixels():
    """Synthetic weed/background samples for the generic 'starter' profile.

    A LUT is normally the site's own painted colour distribution — no factory
    profile can be right everywhere. This one covers broad vegetation greens
    against soil browns, stubble yellows and neutral greys as a working
    baseline the operator can apply immediately and then Edit (extend with
    real strokes) to adapt to their paddock.
    Returns (fg_pixels, bg_pixels).
    """
    fg = _hsv_grid_pixels((35, 90), (60, 255), (50, 230))          # greens
    bg = np.concatenate([
        _hsv_grid_pixels((5, 25), (40, 200), (40, 210)),           # soil browns
        _hsv_grid_pixels((20, 32), (40, 160), (140, 250)),         # stubble yellows
        _hsv_grid_pixels((0, 179), (0, 30), (20, 250), step=(20, 10, 12)),  # greys
    ])
    return fg, bg


def subsample_pixels(pixels, cap=MAX_STORED_PIXELS):
    """Uniformly subsample an (N, 3) pixel array down to *cap* rows."""
    pixels = np.asarray(pixels, dtype=np.uint8).reshape(-1, 3)
    if pixels.shape[0] <= cap:
        return pixels
    step_idx = np.linspace(0, pixels.shape[0] - 1, cap).astype(np.int64)
    return pixels[step_idx]


# ----------------------------------------------------------------------
# Profile storage
# ----------------------------------------------------------------------

class LUTProfileManager:
    """Save, load, list and delete painted LUT profiles (.npz files)."""

    def __init__(self, profile_dir):
        self.profile_dir = str(profile_dir)

    # -- helpers -------------------------------------------------------

    def profile_path(self, name):
        if not NAME_PATTERN.match(name or ''):
            raise LUTProfileError(
                f'Invalid profile name {name!r} — must match [a-z][a-z0-9_]{{0,30}}')
        return os.path.join(self.profile_dir, name + PROFILE_SUFFIX)

    def exists(self, name):
        try:
            return os.path.isfile(self.profile_path(name))
        except LUTProfileError:
            return False

    def _read_meta(self, path):
        """Read just the meta dict from a profile file (for listings)."""
        try:
            with np.load(path, allow_pickle=False) as data:
                return _json_from_array(data['meta'])
        except LUTProfileError:
            raise
        except Exception as e:
            raise LUTProfileError(f'unreadable profile: {e}')

    # -- public API ----------------------------------------------------

    def list_profiles(self):
        """Return [{name, created, fg_pixels, bg_pixels, frames}, ...]."""
        result = []
        try:
            entries = sorted(os.listdir(self.profile_dir))
        except FileNotFoundError:
            return result
        for entry in entries:
            if not entry.endswith(PROFILE_SUFFIX):
                continue
            name = entry[:-len(PROFILE_SUFFIX)]
            if not NAME_PATTERN.match(name):
                continue
            try:
                meta = self._read_meta(os.path.join(self.profile_dir, entry))
            except LUTProfileError as e:
                logger.warning(f'Skipping unreadable LUT profile {entry}: {e}')
                continue
            result.append({
                'name': name,
                'created': meta.get('created', ''),
                'fg_pixels': meta.get('fg_pixels', 0),
                'bg_pixels': meta.get('bg_pixels', 0),
                'frames': meta.get('frames', 0),
                'is_builtin': name in BUILTIN_PROFILES,
            })
        return result

    def save(self, name, fg_pixels, bg_pixels, strokes=None,
             thumbnail_jpeg=None, frames=0):
        """Validate and atomically persist a profile. Returns its meta dict.

        *fg_pixels* / *bg_pixels* are (N, 3) uint8 BGR sample arrays
        accumulated from all painted strokes (and any base profile being
        extended). Histograms are rebuilt here — single source of truth.
        """
        path = self.profile_path(name)
        fg = subsample_pixels(fg_pixels)
        bg = subsample_pixels(bg_pixels)
        if fg.shape[0] < MIN_CLASS_PIXELS or bg.shape[0] < MIN_CLASS_PIXELS:
            raise LUTProfileError(
                f'Need at least {MIN_CLASS_PIXELS} painted pixels of both weed '
                f'and background (got weed={fg.shape[0]}, background={bg.shape[0]})')

        hist_fg, hist_bg = build_histograms(fg, bg)
        meta = {
            'version': PROFILE_VERSION,
            'name': name,
            'created': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'fg_pixels': int(fg.shape[0]),
            'bg_pixels': int(bg.shape[0]),
            'frames': int(frames),
        }
        arrays = {
            'hist_fg': hist_fg,
            'hist_bg': hist_bg,
            'fg_pixels': fg,
            'bg_pixels': bg,
            'meta': _json_array(meta),
            'strokes': _json_array(strokes or []),
        }
        if thumbnail_jpeg:
            arrays['thumbnail'] = np.frombuffer(thumbnail_jpeg, dtype=np.uint8)

        os.makedirs(self.profile_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            suffix='.npz', prefix='.owl_lut_', dir=self.profile_dir)
        try:
            with os.fdopen(fd, 'wb') as f:
                np.savez_compressed(f, **arrays)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        logger.info(f'Saved LUT profile: {name} '
                    f'(fg={meta["fg_pixels"]}, bg={meta["bg_pixels"]})')
        return meta

    def load(self, name):
        """Load a profile. Raises LUTProfileError if missing or corrupt."""
        path = self.profile_path(name)
        if not os.path.isfile(path):
            raise LUTProfileError(f'LUT profile not found: {name}')
        try:
            with np.load(path, allow_pickle=False) as data:
                profile = {
                    'name': name,
                    'hist_fg': data['hist_fg'],
                    'hist_bg': data['hist_bg'],
                    'fg_pixels': data['fg_pixels'],
                    'bg_pixels': data['bg_pixels'],
                    'meta': _json_from_array(data['meta']),
                    'strokes': _json_from_array(data['strokes']),
                    'thumbnail': (data['thumbnail'].tobytes()
                                  if 'thumbnail' in data else None),
                }
        except LUTProfileError:
            raise
        except Exception as e:
            raise LUTProfileError(f'Corrupt LUT profile {name}: {e}')

        if profile['hist_fg'].shape != (LUT_BINS, LUT_BINS, LUT_BINS) or \
                profile['hist_bg'].shape != (LUT_BINS, LUT_BINS, LUT_BINS):
            raise LUTProfileError(f'Corrupt LUT profile {name}: bad histogram shape')
        return profile

    def load_and_bake(self, name, sensitivity=DEFAULT_SENSITIVITY):
        """Convenience for the detection loop: load + bake in one call."""
        profile = self.load(name)
        return bake_lut(profile['hist_fg'], profile['hist_bg'], sensitivity)

    def delete(self, name):
        """Delete a profile. Built-ins are protected (regenerated anyway)."""
        if name in BUILTIN_PROFILES:
            raise LUTProfileError(f'Cannot delete built-in profile: {name}')
        path = self.profile_path(name)
        if not os.path.isfile(path):
            raise LUTProfileError(f'LUT profile not found: {name}')
        os.remove(path)
        logger.info(f'Deleted LUT profile: {name}')

    def ensure_builtin_profiles(self):
        """Create the generic 'starter' profile if missing. Never raises —
        called at startup on OWLs and controllers alike."""
        try:
            if not self.exists('starter'):
                fg, bg = generate_starter_pixels()
                self.save('starter', fg, bg, frames=0)
                logger.info("Created built-in LUT profile 'starter'")
        except Exception as e:
            logger.error(f'Could not create built-in LUT profile: {e}')


def _json_array(obj):
    """Encode a JSON-serialisable object as a uint8 numpy array for npz."""
    return np.frombuffer(json.dumps(obj).encode('utf-8'), dtype=np.uint8)


def _json_from_array(arr):
    try:
        return json.loads(bytes(arr.tobytes()).decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as e:
        raise LUTProfileError(f'Corrupt profile metadata: {e}')
