"""Tests for the LUT profile MQTT command handlers (set_lut_profile,
set_lut_sensitivity, delete_lut_profile, list_lut_profiles) and owl.py's
LUT integration points (source assertions — owl.py can't import on the
dev host)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


@pytest.mark.unit
class TestSetLutProfile:
    def test_queues_pending_and_updates_state(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_lut_profile', 'name': 'Paddock_One'})
        assert mqtt_publisher.state['lut_profile'] == 'paddock_one'
        assert mock_owl._pending_lut_profile == 'paddock_one'

    def test_mirrors_into_config(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_lut_profile', 'name': 'padd'})
        assert mock_owl.config.get('GreenOnBrown', 'lut_profile') == 'padd'

    def test_missing_name_ignored(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state['lut_profile'] = 'keep'
        mqtt_publisher._handle_command({'action': 'set_lut_profile', 'name': ''})
        assert mqtt_publisher.state['lut_profile'] == 'keep'


@pytest.mark.unit
class TestSetLutSensitivity:
    def test_queues_pending_and_updates_state(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_lut_sensitivity', 'value': 72})
        assert mqtt_publisher.state['lut_sensitivity'] == 72
        assert mock_owl._pending_lut_sensitivity == 72

    def test_clamped_to_range(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_lut_sensitivity', 'value': 400})
        assert mqtt_publisher.state['lut_sensitivity'] == 100
        mqtt_publisher._handle_command({'action': 'set_lut_sensitivity', 'value': -5})
        assert mqtt_publisher.state['lut_sensitivity'] == 0

    def test_invalid_value_ignored(self, mqtt_publisher, mock_owl):
        mqtt_publisher.state['lut_sensitivity'] = 33
        mqtt_publisher._handle_command({'action': 'set_lut_sensitivity', 'value': 'abc'})
        assert mqtt_publisher.state['lut_sensitivity'] == 33


@pytest.mark.unit
class TestDeleteLutProfile:
    def test_delete_calls_manager(self, mqtt_publisher, mock_owl):
        mock_owl.lut_manager = MagicMock()
        mqtt_publisher._handle_command({'action': 'delete_lut_profile', 'name': 'gone'})
        mock_owl.lut_manager.delete.assert_called_once_with('gone')

    def test_delete_error_does_not_crash(self, mqtt_publisher, mock_owl):
        from utils.lut_manager import LUTProfileError
        mock_owl.lut_manager = MagicMock()
        mock_owl.lut_manager.delete.side_effect = LUTProfileError('missing')
        mqtt_publisher._handle_command({'action': 'delete_lut_profile', 'name': 'ghost'})
        # no exception propagated; state still published
        assert mqtt_publisher.client.publish.called


@pytest.mark.unit
class TestLutSensitivityViaGenericParamPath:
    """lut_sensitivity rides the generic slider path (set_config /
    set_config_section) and must queue a re-bake, not a plain setattr."""

    def test_set_config_routes_to_pending(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command(
            {'action': 'set_config', 'key': 'lut_sensitivity', 'value': '64'})
        assert mock_owl._pending_lut_sensitivity == 64
        assert mqtt_publisher.state['lut_sensitivity'] == 64

    def test_clamped(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command(
            {'action': 'set_config', 'key': 'lut_sensitivity', 'value': '400'})
        assert mock_owl._pending_lut_sensitivity == 100


@pytest.mark.unit
class TestMinDetectionAreaPercent:
    def test_set_config_updates_float(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command(
            {'action': 'set_config', 'key': 'min_detection_area_percent',
             'value': '0.0123'})
        assert mock_owl.min_detection_area_percent == pytest.approx(0.0123)

    def test_clamped_to_range(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command(
            {'action': 'set_config', 'key': 'min_detection_area_percent',
             'value': '99'})
        assert mock_owl.min_detection_area_percent == 5.0


@pytest.mark.unit
class TestSetAlgorithmLut:
    def test_lut_is_a_valid_algorithm(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_algorithm', 'value': 'lut'})
        assert mqtt_publisher.state['algorithm'] == 'lut'
        assert mock_owl._pending_algorithm == 'lut'


# ---------------------------------------------------------------------------
# owl.py integration points — source assertions (owl.py imports hardware
# modules and can't be imported on the dev host)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestOwlLutSource:
    @pytest.fixture(scope='class')
    def owl_source(self):
        return (PROJECT_ROOT / 'owl.py').read_text(encoding='utf-8')

    def test_lut_detector_branch_exists(self, owl_source):
        assert "elif algo == 'lut':" in owl_source
        assert "GreenOnBrown(algorithm='lut', lut_table=lut_table)" in owl_source

    def test_lut_config_keys_read_in_init(self, owl_source):
        assert "self.lut_profile = self.config.get('GreenOnBrown', 'lut_profile'" in owl_source
        assert "self.lut_sensitivity = self.config.getint('GreenOnBrown', 'lut_sensitivity'" in owl_source

    def test_pending_drains_exist(self, owl_source):
        assert 'self._pending_lut_profile = None' in owl_source
        assert 'self._pending_lut_sensitivity = None' in owl_source
        assert 'if self._pending_lut_profile is not None:' in owl_source

    def test_lut_drain_precedes_algorithm_drain(self, owl_source):
        """A set_lut_profile + set_algorithm pair arriving together must
        build the new detector from the new profile."""
        lut_drain = owl_source.index('if self._pending_lut_profile is not None:')
        algo_drain = owl_source.index('if self._pending_algorithm and (')
        assert lut_drain < algo_drain

    def test_exhsv_failsafe_fallback(self, owl_source):
        assert "if algorithm == 'lut':" in owl_source
        assert "algorithm = 'exhsv'" in owl_source

    def test_min_detection_area_percent_wired(self, owl_source):
        """Percent mode derives px from the cropped detection frame and is
        passed to inference in place of the raw px attribute."""
        assert "self.min_detection_area_percent = self.config.getfloat" in owl_source
        assert "self.min_detection_area_percent / 100.0" in owl_source
        assert "* self.cropped_width * self.cropped_height" in owl_source
        assert 'min_detection_area=min_detection_area' in owl_source
        assert 'min_detection_area=self.min_detection_area' not in owl_source

    def test_greenonbrown_registers_lut(self):
        source = (PROJECT_ROOT / 'utils' / 'greenonbrown.py').read_text(encoding='utf-8')
        assert "'lut': self._lut_inference" in source
        assert 'def set_lut(' in source

    def test_networked_pushes_lut_desired_state(self):
        source = (PROJECT_ROOT / 'controller' / 'networked' / 'networked.py').read_text(encoding='utf-8')
        push = source.index('def _push_desired_state')
        push_end = source.index('def _actuation_broadcast_loop')
        body = source[push:push_end]
        assert 'set_lut_profile' in body
        assert 'set_lut_sensitivity' in body
