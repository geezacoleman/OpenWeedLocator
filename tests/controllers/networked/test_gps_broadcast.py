"""Tests for the networked controller's GPS position broadcast to OWLs.

Root-cause regression coverage: a USB GPS on the central controller must
reach connected OWLs via owl/{id}/gps so positions land in image EXIF.
Optional payload fields are included only when present — never invented.
"""

import json
import time
from unittest.mock import MagicMock

import pytest


VALID_FIX = {
    'latitude': -33.7853,
    'longitude': 151.1234,
    'speed_kmh': 7.2,
    'heading': 90.0,
    'satellites': 10,
    'hdop': 0.8,
    'altitude': 51.2,
    'utc_time': '012345.00',
    'utc_date': '110626',
    'fix_valid': True,
    'age_seconds': 0.2,
}


def _make_controller(tmp_path, monkeypatch):
    """Real CentralController with a mocked MQTT client (test_fleet_update pattern)."""
    from controller.networked.networked import CentralController

    ctrl = CentralController(config_file=str(tmp_path / 'missing.ini'))
    ctrl.mqtt_connected = True
    ctrl.mqtt_client = MagicMock()
    ctrl.mqtt_client.publish.return_value = MagicMock(rc=0)
    ctrl.owls_state = {
        'owl-1': {'connected': True},
        'owl-2': {'connected': True},
        'owl-3': {'connected': False},
    }
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    return ctrl


def _gps_publishes(ctrl):
    """Return {topic: payload_dict} for all owl/*/gps publishes."""
    publishes = {}
    for args, _ in ctrl.mqtt_client.publish.call_args_list:
        topic, payload = args
        if topic.endswith('/gps'):
            publishes[topic] = json.loads(payload)
    return publishes


@pytest.mark.unit
class TestGPSBroadcast:

    def test_valid_fix_published_to_connected_owls_only(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        ctrl._broadcast_gps_to_owls(VALID_FIX)

        publishes = _gps_publishes(ctrl)
        assert set(publishes) == {'owl/owl-1/gps', 'owl/owl-2/gps'}

    def test_payload_contents(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        before = time.time()
        ctrl._broadcast_gps_to_owls(VALID_FIX)

        payload = _gps_publishes(ctrl)['owl/owl-1/gps']
        assert payload['latitude'] == pytest.approx(-33.7853)
        assert payload['longitude'] == pytest.approx(151.1234)
        # 'accuracy' mirrors hdop for compatibility with the browser-GPS payload shape
        assert payload['accuracy'] == pytest.approx(0.8)
        assert payload['hdop'] == pytest.approx(0.8)
        assert payload['altitude'] == pytest.approx(51.2)
        assert payload['speed_kmh'] == pytest.approx(7.2)
        assert payload['heading'] == pytest.approx(90.0)
        assert payload['satellites'] == 10
        assert payload['utc_time'] == '012345.00'
        assert payload['utc_date'] == '110626'
        assert payload['timestamp'] >= before

    def test_optional_none_fields_omitted(self, tmp_path, monkeypatch):
        """A bare RMC-only fix must not invent hdop/altitude/etc."""
        ctrl = _make_controller(tmp_path, monkeypatch)
        bare_fix = {'latitude': -33.7, 'longitude': 151.1, 'fix_valid': True,
                    'hdop': None, 'altitude': None, 'speed_kmh': None,
                    'heading': None, 'satellites': None,
                    'utc_time': None, 'utc_date': None}
        ctrl._broadcast_gps_to_owls(bare_fix)

        payload = _gps_publishes(ctrl)['owl/owl-1/gps']
        assert set(payload) == {'latitude', 'longitude', 'timestamp'}

    def test_invalid_fix_not_published(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        ctrl._broadcast_gps_to_owls(dict(VALID_FIX, fix_valid=False))
        assert not _gps_publishes(ctrl)

    def test_missing_latitude_not_published(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        ctrl._broadcast_gps_to_owls(dict(VALID_FIX, latitude=None))
        assert not _gps_publishes(ctrl)

    def test_no_fix_not_published(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        ctrl._broadcast_gps_to_owls(None)
        ctrl._broadcast_gps_to_owls({})
        assert not _gps_publishes(ctrl)

    def test_disconnected_mqtt_not_published(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        ctrl.mqtt_connected = False
        ctrl._broadcast_gps_to_owls(VALID_FIX)
        assert not _gps_publishes(ctrl)
