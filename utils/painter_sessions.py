"""
Server-side state for the dashboard weed painter.

The painter frontend never touches pixels: frames grabbed from an OWL are
decoded and held here (in memory, per painting session), strokes arrive as
normalised coordinates, and pixel sampling / histogram building / mask
preview all run through utils.lut_manager — the same code the OWL detection
loop uses, so the preview cannot diverge from field behaviour.

Used identically by the standalone and networked controllers; only frame
acquisition differs (local MJPEG server vs per-device snapshot).
"""

import threading
import time
import uuid
from collections import OrderedDict

import cv2
import numpy as np

from utils.lut_manager import (
    apply_lut,
    bake_lut,
    build_histograms,
    lut_swatch_png,
    sample_stroke_pixels,
)

# Bounds guarding a public endpoint on a field LAN — generous for real use.
MAX_SESSIONS = 4
MAX_FRAMES_PER_SESSION = 12
MAX_STROKES = 400
MAX_POINTS_PER_STROKE = 4000
SESSION_TTL_S = 4 * 3600

STROKE_LABELS = ('weed', 'background')

# BGRA spray-preview overlay colour (orange, distinct from the green/red
# stroke tints drawn client-side)
_OVERLAY_BGRA = (0, 140, 255, 150)


class PainterError(Exception):
    """Raised for invalid painter session/stroke input."""


class PainterSessionStore:
    """In-memory painting sessions: frozen frames keyed by (session, frame)."""

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def new_session(self):
        with self._lock:
            self._prune()
            if len(self._sessions) >= MAX_SESSIONS:
                # Drop the oldest session — the kiosk realistically has one
                oldest = min(self._sessions, key=lambda s: self._sessions[s]['created'])
                del self._sessions[oldest]
            session_id = uuid.uuid4().hex[:12]
            self._sessions[session_id] = {
                'created': time.time(),
                'frames': OrderedDict(),
            }
            return session_id

    def add_frame(self, session_id, frame):
        """Store a decoded BGR frame; returns its frame_id."""
        with self._lock:
            session = self._get(session_id)
            if len(session['frames']) >= MAX_FRAMES_PER_SESSION:
                raise PainterError(
                    f'Session frame limit reached ({MAX_FRAMES_PER_SESSION})')
            frame_id = f'f{len(session["frames"]) + 1}_{uuid.uuid4().hex[:6]}'
            session['frames'][frame_id] = np.ascontiguousarray(frame)
            return frame_id

    def get_frame(self, session_id, frame_id):
        with self._lock:
            session = self._get(session_id)
            frame = session['frames'].get(frame_id)
            if frame is None:
                raise PainterError(f'Unknown frame: {frame_id}')
            return frame

    def list_frames(self, session_id):
        """Return [(frame_id, frame), ...] for session resume."""
        with self._lock:
            session = self._get(session_id)
            return list(session['frames'].items())

    def has_session(self, session_id):
        with self._lock:
            return session_id in self._sessions

    def end_session(self, session_id):
        with self._lock:
            self._sessions.pop(session_id, None)

    def _get(self, session_id):
        session = self._sessions.get(session_id)
        if session is None:
            raise PainterError('Painting session not found (it may have '
                               'expired or the service restarted)')
        return session

    def _prune(self):
        cutoff = time.time() - SESSION_TTL_S
        for sid in [s for s, v in self._sessions.items() if v['created'] < cutoff]:
            del self._sessions[sid]


def validate_strokes(strokes):
    """Bound-check a stroke list from the client. Returns the cleaned list."""
    if not isinstance(strokes, list):
        raise PainterError('strokes must be a list')
    if len(strokes) > MAX_STROKES:
        raise PainterError(f'Too many strokes (max {MAX_STROKES})')
    cleaned = []
    for stroke in strokes:
        if not isinstance(stroke, dict):
            raise PainterError('Each stroke must be an object')
        label = stroke.get('label')
        if label not in STROKE_LABELS:
            raise PainterError(f'Invalid stroke label: {label!r}')
        points = stroke.get('points')
        if not isinstance(points, list) or not points:
            raise PainterError('Stroke has no points')
        if len(points) > MAX_POINTS_PER_STROKE:
            raise PainterError(f'Stroke too long (max {MAX_POINTS_PER_STROKE} points)')
        cleaned.append({
            'frame_id': str(stroke.get('frame_id', '')),
            'label': label,
            'points': [[float(p[0]), float(p[1])] for p in points],
            'radius': float(stroke.get('radius', 0.02)),
        })
    return cleaned


def collect_class_pixels(store, session_id, strokes, base_profile=None):
    """Sample pixels under every stroke, grouped by class.

    *base_profile* is an already-loaded profile dict (from
    LUTProfileManager.load) whose stored samples seed the classes when the
    operator extends an existing profile.
    Returns (fg_pixels, bg_pixels) as (N, 3) uint8 arrays.
    """
    fg_parts, bg_parts = [], []
    if base_profile is not None:
        fg_parts.append(np.asarray(base_profile['fg_pixels'], np.uint8).reshape(-1, 3))
        bg_parts.append(np.asarray(base_profile['bg_pixels'], np.uint8).reshape(-1, 3))

    for stroke in strokes:
        frame = store.get_frame(session_id, stroke['frame_id'])
        pixels = sample_stroke_pixels(frame, stroke)
        if pixels.shape[0] == 0:
            continue
        (fg_parts if stroke['label'] == 'weed' else bg_parts).append(pixels)

    empty = np.empty((0, 3), np.uint8)
    fg = np.concatenate(fg_parts) if fg_parts else empty
    bg = np.concatenate(bg_parts) if bg_parts else empty
    return fg, bg


def preview_overlay_png(frame, fg_pixels, bg_pixels, sensitivity):
    """Bake a LUT from the current samples and render the spray-preview
    overlay for *frame* as a BGRA PNG (alpha = mask), ready to composite
    client-side. Returns PNG bytes.

    Raises LUTProfileError (via bake_lut) if either class is still empty.
    """
    lut = bake_lut(*build_histograms(fg_pixels, bg_pixels), sensitivity)
    return _encode_overlay(apply_lut(frame, lut))


def preview_images(frame, fg_pixels, bg_pixels, sensitivity):
    """One-bake preview bundle for the painter: the spray overlay for
    *frame*, the hue-sorted "sprayed colours" swatch strip, and the
    colour-space coverage fraction.

    Returns (overlay_png, swatch_png, coverage).
    """
    lut = bake_lut(*build_histograms(fg_pixels, bg_pixels), sensitivity)
    overlay = _encode_overlay(apply_lut(frame, lut))
    swatch, coverage = lut_swatch_png(lut)
    return overlay, swatch, coverage


def _encode_overlay(mask):
    h, w = mask.shape
    overlay = np.zeros((h, w, 4), np.uint8)
    overlay[mask > 0] = _OVERLAY_BGRA
    ok, png = cv2.imencode('.png', overlay)
    if not ok:
        raise PainterError('Failed to encode preview overlay')
    return png.tobytes()


def make_thumbnail_jpeg(frame, max_width=240):
    """Small JPEG of a frame for the profile library."""
    h, w = frame.shape[:2]
    if w > max_width:
        frame = cv2.resize(frame, (max_width, max(1, int(h * max_width / w))))
    ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return jpeg.tobytes() if ok else None
