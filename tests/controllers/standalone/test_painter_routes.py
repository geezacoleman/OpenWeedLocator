"""Tests for the standalone weed painter routes (/api/painter/*)."""

import base64
import configparser

import cv2
import numpy as np
import pytest

from utils.lut_manager import LUTProfileManager, MIN_CLASS_PIXELS

GREEN = (40, 180, 60)
BROWN = (60, 90, 120)


def _disc_frame(h=120, w=160):
    frame = np.zeros((h, w, 3), np.uint8)
    frame[:] = BROWN
    cv2.circle(frame, (w // 2, h // 2), 25, GREEN, -1)
    return frame


def _setup_session(dashboard, tmp_dir):
    """Redirect profiles to tmp and inject a frame directly into the store
    (the frame route needs a live camera server, so tests bypass it)."""
    dashboard.lut_profile_manager = LUTProfileManager(str(tmp_dir / 'lut_profiles'))
    sid = dashboard.painter_store.new_session()
    fid = dashboard.painter_store.add_frame(sid, _disc_frame())
    return sid, fid


def _strokes(fid):
    return [
        {'frame_id': fid, 'label': 'weed',
         'points': [[0.5, 0.5]], 'radius': 0.08},
        {'frame_id': fid, 'label': 'background',
         'points': [[0.05, 0.05], [0.3, 0.05], [0.3, 0.15], [0.05, 0.15]],
         'radius': 0.05},
    ]


@pytest.mark.unit
class TestPainterSessionRoutes:
    def test_session_start_and_end(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.post('/api/painter/session')
        data = resp.get_json()
        assert resp.status_code == 200 and data['success']
        sid = data['session_id']
        assert dashboard.painter_store.has_session(sid)

        client.post('/api/painter/session/end', json={'session_id': sid})
        assert not dashboard.painter_store.has_session(sid)

    def test_session_frames_recovery(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sid, fid = _setup_session(dashboard, tmp_dir)
        resp = client.get(f'/api/painter/session/frames?session_id={sid}')
        data = resp.get_json()
        assert data['success'] and len(data['frames']) == 1
        assert data['frames'][0]['frame_id'] == fid
        assert data['frames'][0]['width'] == 160

    def test_frames_unknown_session_404(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/painter/session/frames?session_id=nope')
        assert resp.status_code == 404


@pytest.mark.unit
class TestPainterPreview:
    def test_partial_painting_returns_counts_only(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sid, fid = _setup_session(dashboard, tmp_dir)
        resp = client.post('/api/painter/preview', json={
            'session_id': sid, 'frame_id': fid, 'sensitivity': 50,
            'strokes': [_strokes(fid)[0]],   # weed only, no background yet
        })
        data = resp.get_json()
        assert data['success'] and data['overlay'] is None
        assert data['counts']['weed'] > 0
        assert data['counts']['background'] == 0
        assert data['counts']['required'] == MIN_CLASS_PIXELS

    def test_full_preview_returns_bgra_overlay(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sid, fid = _setup_session(dashboard, tmp_dir)
        resp = client.post('/api/painter/preview', json={
            'session_id': sid, 'frame_id': fid, 'sensitivity': 50,
            'strokes': _strokes(fid),
        })
        data = resp.get_json()
        assert data['success'] and data['overlay']
        png = base64.b64decode(data['overlay'])
        overlay = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED)
        assert overlay.shape == (120, 160, 4)
        assert overlay[60, 80, 3] > 0      # weed disc flagged
        assert overlay[5, 100, 3] == 0     # far background clear
        # Sprayed-colours swatch rides along with the preview
        assert data['swatch']
        swatch = cv2.imdecode(np.frombuffer(base64.b64decode(data['swatch']),
                                            np.uint8), cv2.IMREAD_COLOR)
        assert swatch is not None
        assert data['coverage'] > 0

    def test_swatch_route(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sid, fid = _setup_session(dashboard, tmp_dir)
        client.post('/api/painter/save', json={
            'session_id': sid, 'name': 'swatchy', 'strokes': _strokes(fid)})

        resp = client.get('/api/painter/swatch?name=swatchy&sensitivity=50')
        assert resp.status_code == 200
        assert resp.mimetype == 'image/png'
        img = cv2.imdecode(np.frombuffer(resp.data, np.uint8), cv2.IMREAD_COLOR)
        assert img is not None and img.shape[1] == 256
        # Coverage rides along as a header for the dashboard's fill square
        assert float(resp.headers['X-Coverage']) > 0

        resp = client.get('/api/painter/swatch?name=ghost')
        assert resp.status_code == 404

    def test_invalid_strokes_400(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sid, fid = _setup_session(dashboard, tmp_dir)
        resp = client.post('/api/painter/preview', json={
            'session_id': sid, 'frame_id': fid,
            'strokes': [{'frame_id': fid, 'label': 'tractor',
                         'points': [[0.5, 0.5]]}],
        })
        assert resp.status_code == 400


@pytest.mark.unit
class TestPainterSave:
    def test_save_refuses_thin_profile(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sid, fid = _setup_session(dashboard, tmp_dir)
        resp = client.post('/api/painter/save', json={
            'session_id': sid, 'name': 'thin', 'strokes': [{
                'frame_id': fid, 'label': 'weed',
                'points': [[0.5, 0.5]], 'radius': 0.005,
            }],
        })
        assert resp.status_code == 400
        assert 'at least' in resp.get_json()['error']

    def test_save_persists_profile(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sid, fid = _setup_session(dashboard, tmp_dir)
        resp = client.post('/api/painter/save', json={
            'session_id': sid, 'name': 'my_paddock',
            'strokes': _strokes(fid), 'sensitivity': 60,
            'thumbnail_frame_id': fid,
        })
        data = resp.get_json()
        assert resp.status_code == 200 and data['success']
        assert data['applied'] is False
        assert dashboard.lut_profile_manager.exists('my_paddock')
        profile = dashboard.lut_profile_manager.load('my_paddock')
        assert profile['meta']['frames'] == 1
        assert profile['thumbnail'] is not None

    def test_save_and_apply_persists_config(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sid, fid = _setup_session(dashboard, tmp_dir)
        resp = client.post('/api/painter/save', json={
            'session_id': sid, 'name': 'applied_one',
            'strokes': _strokes(fid), 'sensitivity': 70, 'apply': True,
        })
        assert resp.get_json()['applied'] is True

        # Config persisted (possibly to a copy-on-write file)
        active = (tmp_dir / 'active_config.txt').read_text().strip()
        cfg_file = tmp_dir / active.split('/')[-1]
        config = configparser.ConfigParser()
        config.read(str(cfg_file))
        assert config.get('GreenOnBrown', 'lut_profile') == 'applied_one'
        assert config.getint('GreenOnBrown', 'lut_sensitivity') == 70
        assert config.get('System', 'algorithm') == 'lut'

    def test_invalid_name_400(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sid, fid = _setup_session(dashboard, tmp_dir)
        resp = client.post('/api/painter/save', json={
            'session_id': sid, 'name': '../EVIL', 'strokes': _strokes(fid),
        })
        assert resp.status_code == 400


@pytest.mark.unit
class TestPainterProfiles:
    def test_list_apply_delete(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sid, fid = _setup_session(dashboard, tmp_dir)
        client.post('/api/painter/save', json={
            'session_id': sid, 'name': 'listed', 'strokes': _strokes(fid)})

        resp = client.get('/api/painter/profiles')
        names = [p['name'] for p in resp.get_json()['profiles']]
        assert names == ['listed']

        resp = client.post('/api/painter/apply',
                           json={'name': 'listed', 'sensitivity': 40})
        assert resp.get_json()['success']

        resp = client.post('/api/painter/apply',
                           json={'name': 'ghost', 'sensitivity': 40})
        assert resp.status_code == 400

        resp = client.post('/api/painter/profiles/delete', json={'name': 'listed'})
        assert resp.get_json()['success']
        assert not dashboard.lut_profile_manager.exists('listed')

    def test_sensitivity_route_persists(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.post('/api/painter/sensitivity', json={'value': 85})
        assert resp.get_json()['success']
        active = (tmp_dir / 'active_config.txt').read_text().strip()
        cfg_file = tmp_dir / active.split('/')[-1]
        config = configparser.ConfigParser()
        config.read(str(cfg_file))
        assert config.getint('GreenOnBrown', 'lut_sensitivity') == 85

    def test_sensitivity_clamped(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.post('/api/painter/sensitivity', json={'value': 400})
        assert resp.get_json()['success']
        resp = client.post('/api/painter/sensitivity', json={'value': 'abc'})
        assert resp.status_code == 400
