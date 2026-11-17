"""Tests for the 4-edge crop / actuation-band geometry feature and named config
saves ([Meta] section).

Covers:
- build_config_filename / parse_config_meta helpers
- geometry math via the shared utils.geometry.compute_geometry and assign_lanes
  (the SAME functions owl.py calls)
- the restart-required flag in mqtt_manager
- save_geometry / get_config against a real GEOMETRY.ini in a private OWL home

That a live band or geometry edit changes which weeds spray is asserted at
the relay of a real Owl: tests/owl/test_mqtt_commands_at_the_relay.py
(test_a_live_band_edit_changes_which_weeds_spray, test_save_geometry_...) and
tests/owl/test_relay_matrix.py (band, lanes). The tests that used to stand in
for that here asserted `recompute_geometry.called` on a MagicMock, searched
owl.py for literals, or timed a loop written in the test.
"""

import configparser
import io
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.config_manager import (
    build_config_filename, parse_config_meta, strip_geometry_keys,
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
# Structure only: owl.py must call the shared lane function, not carry a copy
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_owl_uses_the_shared_lane_assignment():
    """Structure check, not behaviour (that is TestLaneAssignment below and
    tests/owl/test_relay_matrix.py). Until v3.16 the band check existed as
    three hand-copied replicas that drifted; this fails if owl.py stops
    importing and calling utils.geometry.assign_lanes."""
    src = (PROJECT_ROOT / 'owl.py').read_text(encoding='utf-8')
    assert 'from utils.geometry import compute_geometry, assign_lanes' in src
    assert 'assign_lanes(weed_centres' in src


# ---------------------------------------------------------------------------
# Restart-required flag (mqtt_manager)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRestartRequired:
    def test_resolution_sets_restart_required(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state.pop('restart_required', None)
        mqtt_publisher._handle_set_config_section('Camera', {'resolution_width': '1280'})
        assert 'resolution_width' in mqtt_publisher.state.get('restart_required', '')

    def test_relay_num_sets_restart_required(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state.pop('restart_required', None)
        mqtt_publisher._handle_set_config_section('System', {'relay_num': '8'})
        assert 'relay_num' in mqtt_publisher.state.get('restart_required', '')

    def test_state_seeds_restart_required_empty(self, mqtt_publisher):
        # A freshly started OWL must actively publish "nothing pending" so
        # controllers can drop a notice cached from before the restart.
        assert mqtt_publisher.state.get('restart_required') == ''

    def test_unchanged_restart_key_not_reflagged(self, mqtt_publisher, mock_owl):
        # First apply writes the value into config and flags the restart…
        mqtt_publisher._handle_set_config_section('Camera', {'resolution_width': '1280'})
        assert 'resolution_width' in mqtt_publisher.state.get('restart_required', '')
        # …then the OWL restarts (pending flag gone) and the config tab's
        # "Apply to OWLs" resends the SAME value — that must not re-raise it.
        mqtt_publisher.state['restart_required'] = ''
        mqtt_publisher._handle_set_config_section('Camera', {'resolution_width': '1280'})
        assert mqtt_publisher.state.get('restart_required', '') == ''

    def test_changed_restart_key_flags_again(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_set_config_section('Camera', {'resolution_width': '1280'})
        mqtt_publisher.state['restart_required'] = ''
        mqtt_publisher._handle_set_config_section('Camera', {'resolution_width': '640'})
        assert 'resolution_width' in mqtt_publisher.state.get('restart_required', '')

    def test_controller_drops_a_restart_notice_the_owl_no_longer_publishes(self):
        """A state payload WITHOUT restart_required (older firmware after its
        restart) must evict the controller's cached copy, otherwise the
        notice outlives the restart it asked for."""
        import threading
        from unittest.mock import MagicMock
        from controller.networked.networked import CentralController

        ctrl = CentralController.__new__(CentralController)
        ctrl.owls_state, ctrl.desired_state, ctrl.lwt_timestamps = {}, {}, {}
        ctrl.mqtt_connected, ctrl.mqtt_lock, ctrl.mqtt_client = True, threading.Lock(), MagicMock()
        ctrl.offline_timeout, ctrl.gps_manager = 15.0, None

        def state(payload):
            msg = MagicMock()
            msg.topic = 'owl/owl-1/state'
            msg.payload = json.dumps(payload).encode()
            ctrl._on_message(None, None, msg)

        state({'restart_required': 'relay_num', 'algorithm': 'exhsv'})
        assert ctrl.owls_state['owl-1']['restart_required'] == 'relay_num'
        state({'restart_required': 'relay_num,resolution_width'})
        assert ctrl.owls_state['owl-1']['restart_required'] == 'relay_num,resolution_width'
        state({'algorithm': 'exhsv'})
        assert 'restart_required' not in ctrl.owls_state['owl-1']

    def test_frontend_labels_cover_all_restart_keys(self):
        # Structure check across two files: the notice shows operator
        # language, not config keys, so every key the OWL can flag needs an
        # entry in the frontend label map.
        from utils.mqtt_manager import RESTART_REQUIRED_KEYS
        js = (PROJECT_ROOT / 'controller' / 'networked' / 'static' / 'js' /
              'modules' / '_config_editor.js').read_text(encoding='utf-8')
        for key in RESTART_REQUIRED_KEYS:
            assert f'{key}:' in js, f'RESTART_KEY_LABELS missing {key}'


@pytest.mark.unit
class TestGeometrySeparation:
    """GEOMETRY.ini is device-resident and kept out of named detection configs."""

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
    def test_save_geometry_writes_only_geometry_keys_to_this_units_file(
            self, mqtt_publisher, mock_owl, owl_home):
        mqtt_publisher._handle_command({'action': 'save_geometry', 'params': {
            'crop_left': '0.1', 'actuation_top': '0.2', 'relay_num': '8', 'exg_min': '1'}})
        assert mqtt_publisher.io_flush()

        written = configparser.ConfigParser()
        written.read_string((owl_home / 'config' / 'GEOMETRY.ini').read_text())
        assert {s: dict(written[s]) for s in written.sections()} == {
            'Camera': {'crop_left': '0.1'}, 'System': {'actuation_top': '0.2'}}
        # nothing else in config/: not the pointer, not a profile, no temp file
        assert [p.name for p in (owl_home / 'config').iterdir()] == ['GEOMETRY.ini']
        # and the running config a later get_config reads carries them
        assert mock_owl.config.get('Camera', 'crop_left') == '0.1'
        assert mock_owl.config.get('System', 'relay_num') == '4'

    def test_save_geometry_keeps_the_keys_it_was_not_sent(self, mqtt_publisher, mock_owl, owl_home):
        geom = owl_home / 'config' / 'GEOMETRY.ini'
        geom.write_text('[Camera]\ncrop_left = 0.11\ncrop_right = 0.07\n')
        mqtt_publisher._handle_save_geometry({'crop_left': '0.3', 'crop_top': 'wide'})
        written = configparser.ConfigParser()
        written.read_string(geom.read_text())
        assert dict(written['Camera']) == {'crop_left': '0.3', 'crop_right': '0.07'}

    def test_config_payload_merges_geometry_ini(self, mqtt_publisher, owl_home):
        """get_config must seed the editor with GEOMETRY.ini values — otherwise the
        geometry editor opens on defaults and Done re-saves defaults over the real
        file (2026-07-10 field bug: geometry 'reverting' after save)."""
        (owl_home / 'config' / 'GEOMETRY.ini').write_text(
            '[Camera]\ncrop_left = 0.11\ncrop_right = 0.07\n'
            '[System]\nactuation_top = 0.25\nactuation_bottom = 0.9\n')
        mqtt_publisher._handle_get_config()

        mqtt_publisher.client.publish.assert_called_once()
        payload = json.loads(mqtt_publisher.client.publish.call_args[0][1])
        assert payload['config']['Camera']['crop_left'] == '0.11'
        assert payload['config']['Camera']['crop_right'] == '0.07'
        assert payload['config']['System']['actuation_top'] == '0.25'
        assert payload['config']['System']['actuation_bottom'] == '0.9'
        # Merging geometry must not clobber the active config's other keys
        assert payload['config']['Camera']['resolution_width'] == '416'
        assert payload['config']['GreenOnBrown']['exg_min'] == '25'

    def test_geometry_save_then_get_round_trips(self, mqtt_publisher, mock_owl, owl_home):
        """Full field-bug repro: save geometry, then get_config must return the
        SAVED values — not defaults. (The broken round trip is what made saved
        geometry 'revert', and re-saving from the default-seeded editor then
        destroyed the real GEOMETRY.ini.)"""
        mqtt_publisher._handle_save_geometry({'crop_left': '0.13', 'actuation_top': '0.31'})
        mqtt_publisher.client.publish.reset_mock()
        mqtt_publisher._handle_get_config()

        publishes = [c for c in mqtt_publisher.client.publish.call_args_list
                     if c[0][0] == mqtt_publisher.topics['config']]
        assert len(publishes) == 1
        payload = json.loads(publishes[0][0][1])
        assert payload['config']['Camera']['crop_left'] == '0.13'
        assert payload['config']['System']['actuation_top'] == '0.31'

    def test_config_payload_ok_without_geometry_ini(self, mqtt_publisher, owl_home):
        """A unit that has never used the geometry editor has no GEOMETRY.ini —
        get_config must still publish normally, without inventing geometry."""
        mqtt_publisher._handle_get_config()

        mqtt_publisher.client.publish.assert_called_once()
        payload = json.loads(mqtt_publisher.client.publish.call_args[0][1])
        assert payload['config']['GreenOnBrown']['exg_min'] == '25'
        assert 'crop_left' not in payload['config']['Camera']

    def test_set_preview_mode_toggles_flag(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_preview_mode', 'mode': 'full'})
        assert mock_owl._stream_full_frame is True
        mqtt_publisher._handle_command({'action': 'set_preview_mode', 'mode': 'cropped'})
        assert mock_owl._stream_full_frame is False

    def test_config_name_published_in_state(self, mqtt_publisher, mock_owl):
        mqtt_publisher._sync_parameters_to_state()
        assert mqtt_publisher.state['config_name'] == 'test_config.ini'


@pytest.mark.unit
class TestLaneAssignment:
    """Behavioural tests for the lane maths owl.py actually runs.

    Until v3.16 this logic existed as three hand-copied replicas (two test
    files and a benchmark), all of which had drifted to a one-sided lower
    threshold while the product moved to a top-and-bottom band. The band's
    UPPER edge — `actuation_bottom`, the "ignore weeds too close to the boom"
    control — therefore had no behavioural test at all, and a lane-offset error
    under an asymmetric crop would have shipped with a green suite.
    """

    def test_centre_maps_to_its_lane(self):
        from utils.geometry import assign_lanes
        # 4 relays across 400px -> 100px lanes
        assert assign_lanes([[50, 10]], 100.0, 4, 0, 100) == {0}
        assert assign_lanes([[150, 10]], 100.0, 4, 0, 100) == {1}
        assert assign_lanes([[250, 10]], 100.0, 4, 0, 100) == {2}
        assert assign_lanes([[350, 10]], 100.0, 4, 0, 100) == {3}

    def test_lane_boundary_belongs_to_the_upper_lane(self):
        from utils.geometry import assign_lanes
        assert assign_lanes([[99, 10]], 100.0, 4, 0, 100) == {0}
        assert assign_lanes([[100, 10]], 100.0, 4, 0, 100) == {1}

    def test_right_edge_cannot_exceed_the_last_relay(self):
        from utils.geometry import assign_lanes
        assert assign_lanes([[400, 10]], 100.0, 4, 0, 100) == {3}
        assert assign_lanes([[9999, 10]], 100.0, 4, 0, 100) == {3}

    def test_weed_above_the_band_is_ignored(self):
        from utils.geometry import assign_lanes
        assert assign_lanes([[50, 19]], 100.0, 4, 20, 80) == set()

    def test_weed_below_the_band_is_ignored(self):
        """The upper edge (actuation_bottom) must actually filter.

        A one-sided implementation passes every other test in this class and
        fails only this one — which is exactly how the replicas drifted
        unnoticed.
        """
        from utils.geometry import assign_lanes
        assert assign_lanes([[50, 81]], 100.0, 4, 20, 80) == set()

    def test_band_edges_are_inclusive(self):
        from utils.geometry import assign_lanes
        assert assign_lanes([[50, 20]], 100.0, 4, 20, 80) == {0}
        assert assign_lanes([[50, 80]], 100.0, 4, 20, 80) == {0}

    def test_one_relay_fires_once_for_many_weeds(self):
        from utils.geometry import assign_lanes
        fired = assign_lanes([[10, 10], [20, 10], [30, 10]], 100.0, 4, 0, 100)
        assert fired == {0}

    def test_asymmetric_crop_shifts_lanes_correctly(self):
        """A weed at a known position must land in the right lane after an
        uneven crop. Lane width comes from the CROPPED frame, and detection
        centres are already in cropped coordinates, so the two must agree —
        if they did not, the boom would spray the wrong row."""
        from utils.geometry import compute_geometry, assign_lanes
        geo = compute_geometry(frame_w=1000, frame_h=400,
                               crop_left=0.10, crop_right=0.30,
                               crop_top=0.0, crop_bottom=0.0, relay_num=4,
                               actuation_top=0.0, actuation_bottom=1.0)
        # 1000px wide, 10% off the left and 30% off the right -> 600px, 150px lanes
        assert geo['cropped_width'] == 600
        assert geo['lane_width'] == 150.0

        lw, n = geo['lane_width'], 4
        top, bottom = geo['y_top'], geo['y_bottom']
        for cropped_x, expected in ((0, 0), (149, 0), (150, 1),
                                    (300, 2), (450, 3), (599, 3)):
            assert assign_lanes([[cropped_x, 10]], lw, n, top, bottom) == {expected}, \
                f'cropped x={cropped_x} should fire relay {expected}'
