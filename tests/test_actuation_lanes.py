"""Tests for mapping detection boxes to relay lanes."""

import pytest

from utils.actuation import relay_ids_for_detections


@pytest.mark.unit
class TestRelayIdsForDetections:
    def test_centre_mode_preserves_single_lane_actuation(self):
        relays = relay_ids_for_detections(
            boxes=[[80, 20, 240, 100]],
            weed_centres=[[200, 70]],
            lane_width=100,
            relay_num=4,
            mode='centre',
        )

        assert relays == [2]

    def test_edge_mode_uses_start_centre_and_end(self):
        relays = relay_ids_for_detections(
            boxes=[[80, 20, 240, 100]],
            weed_centres=[[200, 70]],
            lane_width=100,
            relay_num=4,
            mode='edge',
        )

        assert relays == [0, 2, 3]

    def test_right_edge_on_boundary_stays_in_covered_lane(self):
        relays = relay_ids_for_detections(
            boxes=[[10, 20, 90, 100]],
            weed_centres=[[55, 70]],
            lane_width=100,
            relay_num=4,
            mode='edge',
        )

        assert relays == [0]

    def test_box_coordinates_are_clamped_to_available_relays(self):
        relays = relay_ids_for_detections(
            boxes=[[-20, 20, 450, 100]],
            weed_centres=[[205, 70]],
            lane_width=100,
            relay_num=4,
            mode='edge',
        )

        assert relays == [0, 2, 3]

    def test_detection_above_actuation_zone_does_not_fire(self):
        relays = relay_ids_for_detections(
            boxes=[[80, 20, 240, 100]],
            weed_centres=[[200, 70]],
            lane_width=100,
            relay_num=4,
            actuation_y_thresh=100,
            mode='edge',
        )

        assert relays == []

    def test_missing_box_falls_back_to_centre(self):
        relays = relay_ids_for_detections(
            boxes=[],
            weed_centres=[[250, 120]],
            lane_width=100,
            relay_num=4,
            mode='edge',
        )

        assert relays == [2]

    def test_duplicate_lane_samples_are_deduplicated(self):
        relays = relay_ids_for_detections(
            boxes=[[10, 20, 40, 40], [20, 30, 50, 50]],
            weed_centres=[[30, 40], [45, 55]],
            lane_width=100,
            relay_num=4,
            mode='edge',
        )

        assert relays == [0]

    def test_edge_is_a_valid_config_mode(self):
        from utils.config_manager import ConfigValidator

        assert 'edge' in ConfigValidator.VALID_ACTUATION_MODES
