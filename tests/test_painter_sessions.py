"""Tests for the weed painter server-side session store and helpers."""

import cv2
import numpy as np
import pytest

from utils.painter_sessions import (
    MAX_FRAMES_PER_SESSION,
    MAX_STROKES,
    PainterError,
    PainterSessionStore,
    collect_class_pixels,
    make_thumbnail_jpeg,
    preview_overlay_png,
    validate_strokes,
)

GREEN = (40, 180, 60)
BROWN = (60, 90, 120)


def _disc_frame(h=120, w=160):
    frame = np.zeros((h, w, 3), np.uint8)
    frame[:] = BROWN
    cv2.circle(frame, (w // 2, h // 2), 25, GREEN, -1)
    return frame


@pytest.mark.unit
class TestSessionStore:
    def test_session_lifecycle(self):
        store = PainterSessionStore()
        sid = store.new_session()
        fid = store.add_frame(sid, _disc_frame())
        assert store.get_frame(sid, fid).shape == (120, 160, 3)
        assert store.has_session(sid)
        assert [f[0] for f in store.list_frames(sid)] == [fid]
        store.end_session(sid)
        assert not store.has_session(sid)

    def test_unknown_session_raises(self):
        store = PainterSessionStore()
        with pytest.raises(PainterError, match='session'):
            store.add_frame('nope', _disc_frame())

    def test_unknown_frame_raises(self):
        store = PainterSessionStore()
        sid = store.new_session()
        with pytest.raises(PainterError, match='frame'):
            store.get_frame(sid, 'ghost')

    def test_frame_limit(self):
        store = PainterSessionStore()
        sid = store.new_session()
        for _ in range(MAX_FRAMES_PER_SESSION):
            store.add_frame(sid, _disc_frame())
        with pytest.raises(PainterError, match='limit'):
            store.add_frame(sid, _disc_frame())


@pytest.mark.unit
class TestValidateStrokes:
    def _stroke(self, **kw):
        base = {'frame_id': 'f1', 'label': 'weed',
                'points': [[0.5, 0.5]], 'radius': 0.02}
        base.update(kw)
        return base

    def test_valid(self):
        out = validate_strokes([self._stroke(), self._stroke(label='background')])
        assert len(out) == 2
        assert out[0]['label'] == 'weed'

    def test_rejects_bad_label(self):
        with pytest.raises(PainterError, match='label'):
            validate_strokes([self._stroke(label='crop')])

    def test_rejects_no_points(self):
        with pytest.raises(PainterError, match='points'):
            validate_strokes([self._stroke(points=[])])

    def test_rejects_too_many_strokes(self):
        with pytest.raises(PainterError, match='Too many'):
            validate_strokes([self._stroke()] * (MAX_STROKES + 1))

    def test_rejects_non_list(self):
        with pytest.raises(PainterError):
            validate_strokes({'not': 'a list'})


@pytest.mark.unit
class TestCollectClassPixels:
    def test_samples_by_label(self):
        store = PainterSessionStore()
        sid = store.new_session()
        fid = store.add_frame(sid, _disc_frame())
        strokes = validate_strokes([
            {'frame_id': fid, 'label': 'weed',
             'points': [[0.5, 0.5]], 'radius': 0.05},
            {'frame_id': fid, 'label': 'background',
             'points': [[0.05, 0.05], [0.2, 0.05]], 'radius': 0.03},
        ])
        fg, bg = collect_class_pixels(store, sid, strokes)
        assert fg.shape[0] > 0 and bg.shape[0] > 0
        # dab in disc centre samples green; corner stroke samples brown
        assert (fg == np.array(GREEN, np.uint8)).all()
        assert (bg == np.array(BROWN, np.uint8)).all()

    def test_base_profile_seeds_classes(self):
        store = PainterSessionStore()
        sid = store.new_session()
        base = {
            'fg_pixels': np.tile(np.array(GREEN, np.uint8), (10, 1)),
            'bg_pixels': np.tile(np.array(BROWN, np.uint8), (20, 1)),
        }
        fg, bg = collect_class_pixels(store, sid, [], base_profile=base)
        assert fg.shape[0] == 10
        assert bg.shape[0] == 20

    def test_empty_when_no_strokes(self):
        store = PainterSessionStore()
        sid = store.new_session()
        fg, bg = collect_class_pixels(store, sid, [])
        assert fg.shape == (0, 3) and bg.shape == (0, 3)


@pytest.mark.unit
class TestPreviewOverlay:
    def test_overlay_alpha_matches_detection(self):
        frame = _disc_frame()
        fg = np.tile(np.array(GREEN, np.uint8), (1000, 1))
        bg = np.tile(np.array(BROWN, np.uint8), (1000, 1))
        png = preview_overlay_png(frame, fg, bg, sensitivity=50)
        overlay = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED)
        assert overlay.shape == (120, 160, 4)
        # disc centre is flagged (alpha > 0), corner is not
        assert overlay[60, 80, 3] > 0
        assert overlay[5, 5, 3] == 0

    def test_thumbnail(self):
        thumb = make_thumbnail_jpeg(_disc_frame(h=480, w=640), max_width=240)
        img = cv2.imdecode(np.frombuffer(thumb, np.uint8), cv2.IMREAD_COLOR)
        assert img.shape[1] == 240
