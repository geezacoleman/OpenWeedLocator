"""Tests for the 4-edge crop / actuation-band geometry feature and named config
saves ([Meta] section).

Covers:
- build_config_filename / parse_config_meta helpers
- geometry math via the shared utils.geometry.compute_geometry (the SAME function
  owl.py calls, so the tests can't drift from production); owl.py's use of it is
  also checked at source level since owl.py can't import on non-Pi platforms
- live geometry recompute trigger + restart-required flag in mqtt_manager
- hot-path timing guard for the per-frame crop + actuation-band filter
"""

import ast
import configparser
import io
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.config_manager import (
    build_config_filename, parse_config_meta, strip_geometry_keys, GEOMETRY_KEYS,
)
from utils.geometry import compute_geometry


# ---------------------------------------------------------------------------
# build_config_filename
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBuildConfigFilename:
    def test_name_with_spaces_and_dashes(self):
        fn = build_config_filename('High sensitivity - wheat', None, '20260101_120000')
        assert fn == 'high-sensitivity-wheat_20260101_120000.ini'

    def test_timestamp_always_retained(self):
        fn = build_config_filename('wheat', None, '20260101_120000')
        assert fn.endswith('_20260101_120000.ini')

    def test_empty_name_honours_explicit_filename(self):
        # Legacy/programmatic callers passing an explicit filename keep it as-is.
        assert build_config_filename('', 'mycfg.ini', '20260101_120000') == 'mycfg.ini'

    def test_empty_everything_uses_plain_timestamp(self):
        assert build_config_filename('', None, '20260101_120000') == 'config_20260101_120000.ini'

    def test_unsafe_chars_removed(self):
        fn = build_config_filename('w/e:*?ird name', None, 'TS')
        assert fn.endswith('_TS.ini')
        for ch in '/:*?':
            assert ch not in fn


# ---------------------------------------------------------------------------
# parse_config_meta
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestParseConfigMeta:
    def test_roundtrip(self, tmp_path):
        p = tmp_path / 'c.ini'
        cp = configparser.ConfigParser()
        cp.optionxform = str
        cp.add_section('Meta')
        cp.set('Meta', 'display_name', 'My setup')
        cp.set('Meta', 'notes', 'dewy mornings')
        cp.set('Meta', 'created', '2026-01-01T00:00:00')
        with open(p, 'w') as f:
            cp.write(f)

        meta = parse_config_meta(p)
        assert meta['display_name'] == 'My setup'
        assert meta['notes'] == 'dewy mornings'
        assert meta['created'] == '2026-01-01T00:00:00'

    def test_missing_section_returns_empty(self, tmp_path):
        p = tmp_path / 'c.ini'
        p.write_text('[System]\nalgorithm = exhsv\n')
        assert parse_config_meta(p) == {}

    def test_absent_keys_omitted_not_invented(self, tmp_path):
        p = tmp_path / 'c.ini'
        p.write_text('[Meta]\ndisplay_name = Only name\n')
        assert parse_config_meta(p) == {'display_name': 'Only name'}

    def test_unreadable_returns_empty(self, tmp_path):
        assert parse_config_meta(tmp_path / 'does_not_exist.ini') == {}


# ---------------------------------------------------------------------------
# Geometry math — via the SHARED utils.geometry.compute_geometry that owl.py uses,
# so these assertions validate production code, not a replica that can drift.
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGeometryMath:
    def test_symmetric_matches_legacy_crop_factor(self):
        # 4-edge with equal insets must equal the old symmetric crop math.
        geo = compute_geometry(1456, 1088, 0.1, 0.1, 0.1, 0.1, 4, 0.0, 1.0)
        # Old: crop_left = w*0.1, crop_right = w - crop_left
        assert geo['crop_slice'][1] == slice(145, 1310)   # int(1456*0.1)=145, 1456-145=1311 vs int(1456*0.9)=1310
        # cropped width within rounding of legacy (w - 2*crop_left)
        assert abs(geo['cropped_width'] - (1456 - 2 * int(1456 * 0.1))) <= 1

    def test_independent_edges(self):
        geo = compute_geometry(1000, 800, 0.10, 0.20, 0.05, 0.25, 4, 0.0, 1.0)
        assert geo['crop_slice'] == (slice(40, 600), slice(100, 800))
        assert geo['cropped_width'] == 700
        assert geo['cropped_height'] == 560

    def test_default_band_is_full_height(self):
        geo = compute_geometry(1000, 800, 0.0, 0.0, 0.0, 0.0, 4, 0.0, 1.0)
        assert geo['y_top'] == 0
        assert geo['y_bottom'] == 800

    def test_band_subset(self):
        geo = compute_geometry(1000, 800, 0.0, 0.0, 0.0, 0.0, 4, 0.25, 0.75)
        assert geo['y_top'] == 200
        assert geo['y_bottom'] == 600

    def test_lanes_recenter_with_crop(self):
        wide = compute_geometry(1000, 800, 0.0, 0.0, 0.0, 0.0, 4, 0.0, 1.0)
        narrow = compute_geometry(1000, 800, 0.2, 0.2, 0.0, 0.0, 4, 0.0, 1.0)
        assert narrow['lane_width'] < wide['lane_width']

    def test_crop_edges_clamped_to_049(self):
        # Absurd insets must not collapse the frame — each edge clamps at 0.49.
        geo = compute_geometry(1000, 800, 0.9, 0.9, 0.9, 0.9, 4, 0.0, 1.0)
        assert geo['cropped_width'] > 0 and geo['cropped_height'] > 0
        assert geo['clamped_edges'] == (0.49, 0.49, 0.49, 0.49)

    def test_degenerate_band_falls_back_to_full_height(self):
        # top >= bottom is meaningless — fall back to the full cropped height.
        geo = compute_geometry(1000, 800, 0.0, 0.0, 0.0, 0.0, 4, 0.8, 0.2)
        assert geo['y_top'] == 0
        assert geo['y_bottom'] == 800

    def test_missing_frame_dims_returns_none(self):
        assert compute_geometry(0, 0, 0.1, 0.1, 0.1, 0.1, 4, 0.0, 1.0) is None


# ---------------------------------------------------------------------------
# owl.py source-level checks (can't import on non-Pi)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestOwlGeometrySource:
    def _src(self):
        return (PROJECT_ROOT / 'owl.py').read_text(encoding='utf-8')

    def test_recompute_method_defined(self):
        tree = ast.parse(self._src())
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert 'recompute_geometry' in names

    def test_init_calls_recompute(self):
        assert 'self.recompute_geometry()' in self._src()

    def test_band_membership_logic(self):
        src = self._src()
        assert 'self.actuation_y_top <= centre[1] <= self.actuation_y_bottom' in src
        # Zone mode slices the band, not a single threshold
        assert 'self.actuation_y_top:self.actuation_y_bottom' in src

    def test_per_edge_config_read(self):
        src = self._src()
        for key in ('crop_left', 'crop_right', 'crop_top', 'crop_bottom',
                    'actuation_top', 'actuation_bottom'):
            assert key in src


# ---------------------------------------------------------------------------
# Live geometry recompute trigger + restart-required (mqtt_manager)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLiveGeometryTrigger:
    def test_constants_defined(self):
        from utils.mqtt_manager import GEOMETRY_KEYS, RESTART_REQUIRED_KEYS
        assert {'crop_left', 'crop_right', 'crop_top', 'crop_bottom',
                'actuation_top', 'actuation_bottom'} <= GEOMETRY_KEYS
        assert {'resolution_width', 'resolution_height', 'relay_num'} <= RESTART_REQUIRED_KEYS

    def test_crop_change_triggers_recompute(self, mqtt_publisher, mock_owl):
        mock_owl.recompute_geometry.reset_mock()
        mqtt_publisher._handle_set_config_section('Camera', {'crop_left': '0.1'})
        assert mock_owl.recompute_geometry.called

    def test_band_change_triggers_recompute(self, mqtt_publisher, mock_owl):
        mock_owl.recompute_geometry.reset_mock()
        mqtt_publisher._handle_set_config_section('System', {'actuation_top': '0.2'})
        assert mock_owl.recompute_geometry.called

    def test_threshold_change_does_not_recompute(self, mqtt_publisher, mock_owl):
        mock_owl.recompute_geometry.reset_mock()
        mqtt_publisher._handle_set_config_section('GreenOnBrown', {'exg_min': '30'})
        assert not mock_owl.recompute_geometry.called

    def test_resolution_sets_restart_required(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state.pop('restart_required', None)
        mqtt_publisher._handle_set_config_section('Camera', {'resolution_width': '1280'})
        assert 'resolution_width' in mqtt_publisher.state.get('restart_required', '')

    def test_relay_num_sets_restart_required(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state.pop('restart_required', None)
        mqtt_publisher._handle_set_config_section('System', {'relay_num': '8'})
        assert 'relay_num' in mqtt_publisher.state.get('restart_required', '')


# ---------------------------------------------------------------------------
# Hot-path timing guard — per-frame crop slice + actuation-band filter must stay
# negligible. Flags accidental per-frame regressions (e.g. a per-pixel mask).
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGeometrySeparation:
    """GEOMETRY.ini is device-resident and kept out of named detection configs."""

    def test_geometry_ini_overrides_active_config(self, tmp_path):
        # Merge order owl.py uses: active config first, GEOMETRY.ini last (wins).
        active = tmp_path / 'active.ini'
        active.write_text('[Camera]\ncrop_left = 0.05\nresolution_width = 1456\n')
        geom = tmp_path / 'GEOMETRY.ini'
        geom.write_text('[Camera]\ncrop_left = 0.2\n')
        cp = configparser.ConfigParser()
        cp.read(str(active))
        cp.read(str(geom))
        assert cp.get('Camera', 'crop_left') == '0.2'        # geometry wins
        assert cp.get('Camera', 'resolution_width') == '1456'  # non-geometry preserved

    def test_strip_geometry_keys(self):
        d = {
            'Camera': {'crop_left': '0.1', 'resolution_width': '1456'},
            'System': {'actuation_top': '0.2', 'relay_num': '4'},
            'GreenOnBrown': {'exg_min': '25'},
        }
        out = strip_geometry_keys(d)
        assert 'crop_left' not in out['Camera']
        assert out['Camera']['resolution_width'] == '1456'
        assert 'actuation_top' not in out['System']
        assert out['System']['relay_num'] == '4'
        assert out['GreenOnBrown']['exg_min'] == '25'

    def test_geometry_keys_set(self):
        assert GEOMETRY_KEYS == {'crop_left', 'crop_right', 'crop_top',
                                 'crop_bottom', 'actuation_top', 'actuation_bottom'}

    def test_write_config_without_geometry(self, mqtt_publisher):
        cfg = configparser.ConfigParser()
        cfg.add_section('Camera')
        cfg.set('Camera', 'crop_left', '0.1')
        cfg.set('Camera', 'resolution_width', '1456')
        cfg.add_section('System')
        cfg.set('System', 'actuation_top', '0.2')
        cfg.set('System', 'relay_num', '4')
        buf = io.StringIO()
        mqtt_publisher._write_config_without_geometry(cfg, buf)
        out = configparser.ConfigParser()
        out.read_string(buf.getvalue())
        assert not out.has_option('Camera', 'crop_left')
        assert out.get('Camera', 'resolution_width') == '1456'
        assert not out.has_option('System', 'actuation_top')
        assert out.get('System', 'relay_num') == '4'


@pytest.mark.unit
class TestSaveGeometryAndPreview:
    def test_save_geometry_applies_recompute_and_persists(self, mqtt_publisher, mock_owl, tmp_path):
        import utils.mqtt_manager as mm
        mock_owl.recompute_geometry.reset_mock()
        real_path = os.path.join(str(tmp_path), 'g.ini')
        fd = os.open(real_path, os.O_WRONLY | os.O_CREAT)
        with patch.object(mm.os, 'replace') as mock_replace, \
             patch.object(mm.tempfile, 'mkstemp', return_value=(fd, real_path)):
            mqtt_publisher._handle_save_geometry({'crop_left': '0.1', 'actuation_top': '0.2'})
        assert mock_owl.recompute_geometry.called   # applied live
        assert mock_replace.called                   # persisted (atomic replace)
        assert float(mock_owl.crop_left) == 0.1

    def test_save_geometry_ignores_non_geometry_keys(self, mqtt_publisher, mock_owl, tmp_path):
        import utils.mqtt_manager as mm
        real_path = os.path.join(str(tmp_path), 'g2.ini')
        fd = os.open(real_path, os.O_WRONLY | os.O_CREAT)
        with patch.object(mm.os, 'replace'), \
             patch.object(mm.tempfile, 'mkstemp', return_value=(fd, real_path)):
            mqtt_publisher._handle_save_geometry({'relay_num': '8', 'crop_left': '0.3'})
        # relay_num is not a geometry key — must not be written to GEOMETRY.ini
        written = configparser.ConfigParser()
        written.read(real_path)
        assert not (written.has_section('System') and written.has_option('System', 'relay_num'))

    def test_set_preview_mode_toggles_flag(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_preview_mode', 'mode': 'full'})
        assert mock_owl._stream_full_frame is True
        mqtt_publisher._handle_command({'action': 'set_preview_mode', 'mode': 'cropped'})
        assert mock_owl._stream_full_frame is False

    def test_config_name_published_in_state(self, mqtt_publisher, mock_owl):
        mqtt_publisher._sync_parameters_to_state()
        assert 'config_name' in mqtt_publisher.state


@pytest.mark.unit
class TestHotPathTiming:
    def test_crop_and_band_filter_per_frame_budget(self):
        frame = np.zeros((1088, 1456, 3), dtype=np.uint8)
        geo = compute_geometry(1456, 1088, 0.02, 0.02, 0.02, 0.02, 4, 0.0, 1.0)
        crop_slice = geo['crop_slice']
        y_top, y_bottom, lane_width = geo['y_top'], geo['y_bottom'], geo['lane_width']
        centres = [[(i * 31) % 1400, (i * 17) % 1000] for i in range(50)]

        N = 2000
        for _ in range(50):           # warmup
            _ = frame[crop_slice]

        start = time.perf_counter()
        for _ in range(N):
            _cropped = frame[crop_slice]          # view, no copy
            fired = set()
            for centre in centres:
                if y_top <= centre[1] <= y_bottom:
                    fired.add(min(int(centre[0] / lane_width), 3))
        per_frame_ms = (time.perf_counter() - start) / N * 1000

        print(f"\n  hot-path crop+band filter: {per_frame_ms:.4f} ms/frame "
              f"({len(centres)} centres, {N} iters)")
        # Generous ceiling — the real op is microseconds; this only catches a
        # gross regression such as adding a per-pixel mask to the hot path.
        assert per_frame_ms < 1.0
