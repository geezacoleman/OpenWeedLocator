"""Tests for GPS manager classes in utils/gps_manager.py."""

import json
import os
import time

import pytest
from utils.gps_manager import (
    haversine,
    GPSState,
    SessionStats,
    TrackRecorder,
)


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------

class TestHaversine:
    def test_same_point(self):
        assert haversine(0, 0, 0, 0) == 0.0

    def test_known_distance(self):
        # Sydney to Melbourne is ~714 km
        dist = haversine(-33.8688, 151.2093, -37.8136, 144.9631)
        assert 700 < dist < 730

    def test_short_distance(self):
        # ~111 km per degree of latitude at equator
        dist = haversine(0, 0, 1, 0)
        assert 110 < dist < 112

    def test_across_prime_meridian(self):
        dist = haversine(51.5, -0.1, 51.5, 0.1)
        assert dist > 0
        assert dist < 15  # < 15 km


# ---------------------------------------------------------------------------
# GPSState
# ---------------------------------------------------------------------------

class TestGPSState:
    def test_initial_state(self):
        state = GPSState()
        d = state.get_dict()
        assert d['fix_valid'] is False
        assert d['latitude'] is None
        assert d['speed_kmh'] is None

    def test_update_from_rmc_active(self):
        state = GPSState()
        state.update_from_rmc({
            'status': 'A',
            'lat': -33.8688,
            'lon': 151.2093,
            'speed_knots': 5.0,
            'heading': 180.0,
        })
        d = state.get_dict()
        assert d['fix_valid'] is True
        assert d['latitude'] == pytest.approx(-33.8688)
        assert d['speed_kmh'] == pytest.approx(9.3, abs=0.1)  # 5 * 1.852
        assert d['heading'] == pytest.approx(180.0)

    def test_update_from_rmc_void(self):
        state = GPSState()
        state.update_from_rmc({'status': 'V', 'lat': None, 'lon': None,
                               'speed_knots': None, 'heading': None})
        d = state.get_dict()
        assert d['fix_valid'] is False

    def test_update_from_gga(self):
        state = GPSState()
        state.update_from_gga({
            'lat': -33.8688,
            'lon': 151.2093,
            'fix_quality': 1,
            'satellites': 12,
            'hdop': 0.8,
            'altitude': 45.2,
        })
        d = state.get_dict()
        assert d['satellites'] == 12
        assert d['hdop'] == 0.8
        assert d['altitude'] == 45.2
        assert d['fix_valid'] is True

    def test_stale_fix_marked_invalid(self):
        state = GPSState()
        state.update_from_rmc({
            'status': 'A',
            'lat': -33.8688,
            'lon': 151.2093,
            'speed_knots': 5.0,
            'heading': 180.0,
        })
        # Manually set last_fix_time to 15 seconds ago
        with state._lock:
            state.last_fix_time = time.time() - 15
        d = state.get_dict()
        assert d['fix_valid'] is False
        assert d['age_seconds'] > 10


# ---------------------------------------------------------------------------
# SessionStats
# ---------------------------------------------------------------------------

class TestSessionStats:
    def test_initial_state(self):
        s = SessionStats()
        assert s.active is False
        assert s.distance_km == 0.0

    def test_start_stop(self):
        s = SessionStats()
        s.start()
        assert s.active is True
        s.stop()
        assert s.active is False

    def test_distance_accumulation(self):
        s = SessionStats(boom_width_m=12.0)
        s.start()
        # Move ~111 m north (0.001 degree latitude at equator)
        s.update(0.0, 0.0)
        s.update(0.001, 0.0)
        assert s.distance_km > 0.1  # Should be ~0.111 km

    def test_no_accumulation_when_inactive(self):
        s = SessionStats()
        # Don't call start()
        s.update(0.0, 0.0)
        s.update(0.001, 0.0)
        assert s.distance_km == 0.0

    def test_area_calculation(self):
        s = SessionStats(boom_width_m=12.0)
        s.distance_km = 1.0
        # 1 km * 12m / 10 = 1.2 ha
        assert s.area_hectares == pytest.approx(1.2)

    def test_to_dict(self):
        s = SessionStats(boom_width_m=12.0)
        d = s.to_dict()
        assert 'active' in d
        assert 'distance_km' in d
        assert 'area_hectares' in d
        assert 'boom_width_m' in d


# ---------------------------------------------------------------------------
# TrackRecorder
# ---------------------------------------------------------------------------

class TestTrackRecorder:
    def test_start_creates_directory(self, tmp_path):
        save_dir = str(tmp_path / 'tracks')
        rec = TrackRecorder()
        rec.start(save_dir)
        assert rec.recording is True
        assert os.path.isdir(save_dir)

    def test_add_points_and_stop(self, tmp_path):
        save_dir = str(tmp_path / 'tracks')
        rec = TrackRecorder()
        rec.start(save_dir)

        # Add several points with sufficient distance and time gaps
        points = [
            (-33.8688, 151.2093),
            (-33.8700, 151.2100),
            (-33.8720, 151.2120),
        ]
        for i, (lat, lon) in enumerate(points):
            # Override the min interval check for testing
            rec._last_record_time = 0
            rec.add_point(lat, lon, speed=8.0 + i, heading=90.0 + i)

        filepath = rec.stop()
        assert rec.recording is False
        assert filepath is not None
        assert os.path.isfile(filepath)

        # Validate GeoJSON structure
        with open(filepath) as f:
            data = json.load(f)

        assert data['type'] == 'FeatureCollection'
        assert len(data['features']) == 1

        feature = data['features'][0]
        assert feature['geometry']['type'] == 'LineString'
        assert len(feature['geometry']['coordinates']) == 3
        assert feature['properties']['point_count'] == 3
        assert len(feature['properties']['speeds_kmh']) == 3
        assert len(feature['properties']['timestamps']) == 3

    def test_skip_too_close_points(self, tmp_path):
        save_dir = str(tmp_path / 'tracks')
        rec = TrackRecorder()
        rec.start(save_dir)

        # Add same point twice (should skip second due to min distance)
        rec._last_record_time = 0
        rec.add_point(-33.8688, 151.2093, speed=8.0, heading=90.0)
        rec._last_record_time = 0  # Reset interval
        rec.add_point(-33.8688, 151.2093, speed=8.0, heading=90.0)

        rec.stop()
        assert len(rec._coordinates) == 1  # Only first recorded

    def test_stop_without_start(self):
        rec = TrackRecorder()
        result = rec.stop()
        assert result is None
