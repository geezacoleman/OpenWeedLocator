"""
Unit tests for controller/networked/fleet_roster.py.

Injected clock + tmp_path files — no network, no Flask, runs anywhere.
"""

import json

import pytest

from controller.networked.fleet_roster import (
    FleetRoster,
    FleetRosterError,
    RESERVATION_TTL_S,
)


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def roster(tmp_path, clock):
    return FleetRoster('192.168.1.2', path=tmp_path / 'fleet.json', clock=clock)


@pytest.mark.unit
class TestAllocation:
    def test_first_reservation_is_owl_1(self, roster):
        record = roster.reserve()
        assert record['device_id'] == 'owl-1'
        assert record['number'] == 1
        assert record['assigned_ip'] == '192.168.1.11'

    def test_sequential_allocation(self, roster):
        assert roster.reserve()['device_id'] == 'owl-1'
        assert roster.reserve()['device_id'] == 'owl-2'
        assert roster.reserve()['assigned_ip'] == '192.168.1.13'

    def test_gap_reuse_after_remove(self, roster):
        roster.reserve()                      # owl-1
        roster.reserve()                      # owl-2
        roster.remove('owl-1')
        assert roster.reserve()['device_id'] == 'owl-1'

    def test_live_unregistered_ids_are_skipped(self, roster):
        record = roster.reserve(live_ids=['owl-1', 'owl-2'])
        assert record['device_id'] == 'owl-3'

    def test_controller_id_never_allocated(self, tmp_path, clock):
        roster = FleetRoster('192.168.1.2', path=tmp_path / 'f.json', clock=clock)
        for _ in range(5):
            assert roster.reserve()['device_id'] != 'owl-controller'

    def test_ip_collision_with_controller_skipped(self, tmp_path, clock):
        # Controller parked on .11 (owl-1's slot) — number 1 must be skipped
        roster = FleetRoster('192.168.1.11', path=tmp_path / 'f.json', clock=clock)
        record = roster.reserve()
        assert record['assigned_ip'] != '192.168.1.11'
        assert record['device_id'] == 'owl-2'

    def test_gateway_derived_from_subnet(self, tmp_path, clock):
        roster = FleetRoster('10.0.5.2', path=tmp_path / 'f.json', clock=clock)
        assert roster.gateway == '10.0.5.1'
        assert roster.reserve()['assigned_ip'] == '10.0.5.11'

    def test_exhaustion_raises(self, tmp_path, clock):
        roster = FleetRoster('192.168.1.2', path=tmp_path / 'f.json', clock=clock)
        from controller.networked import fleet_roster
        for _ in range(fleet_roster.MAX_NUMBER):
            roster.reserve()
        with pytest.raises(FleetRosterError):
            roster.reserve()


@pytest.mark.unit
class TestReservationLifecycle:
    def test_expiry_tombstones_the_slot(self, roster, clock):
        # An expired reservation must NOT be reallocated while its tombstone
        # holds the slot — a slow first phone and a second phone would end
        # up with two OWLs sharing one identity/IP otherwise.
        roster.reserve()                              # owl-1
        clock.advance(RESERVATION_TTL_S + 1)
        assert roster.reserve()['device_id'] == 'owl-2'
        # The late OWL's first heartbeat still confirms (tombstone revives)
        assert roster.is_reserved('owl-1')
        assert roster.confirm('owl-1')['status'] == 'registered'

    def test_tombstone_purged_after_ttl(self, roster, clock):
        from controller.networked.fleet_roster import TOMBSTONE_TTL_S
        roster.reserve()                              # owl-1
        clock.advance(RESERVATION_TTL_S + 1)
        roster.snapshot()                             # lazy prune -> tombstone
        clock.advance(TOMBSTONE_TTL_S + 1)
        assert roster.reserve()['device_id'] == 'owl-1'

    def test_registered_never_expires(self, roster, clock):
        roster.reserve()
        roster.confirm('owl-1')
        clock.advance(RESERVATION_TTL_S * 10)
        assert roster.reserve()['device_id'] == 'owl-2'
        assert roster.is_registered('owl-1')

    def test_confirm_promotes_and_is_idempotent(self, roster):
        roster.reserve()
        first = roster.confirm('owl-1', observed_ip='192.168.1.11')
        second = roster.confirm('owl-1')
        assert first['status'] == second['status'] == 'registered'
        assert second['observed_ip'] == '192.168.1.11'

    def test_confirm_unknown_raises(self, roster):
        with pytest.raises(FleetRosterError):
            roster.confirm('owl-9')

    def test_snapshot_separates_states(self, roster):
        roster.reserve()
        roster.reserve()
        roster.confirm('owl-1')
        devices, reservations = roster.snapshot()
        assert [d['device_id'] for d in devices] == ['owl-1']
        assert [r['device_id'] for r in reservations] == ['owl-2']

    def test_touch_refreshes_registered_only(self, roster, clock):
        roster.reserve()
        roster.confirm('owl-1')
        before = roster.get('owl-1')['last_confirmed']
        clock.advance(120)
        roster.touch('owl-1')
        assert roster.get('owl-1')['last_confirmed'] > before
        roster.touch('owl-9')  # unknown: no-op, no raise

    def test_touch_is_throttled(self, roster, clock):
        roster.reserve()
        roster.confirm('owl-1')
        clock.advance(120)
        roster.touch('owl-1')
        stamp = roster.get('owl-1')['last_confirmed']
        clock.advance(5)   # under throttle window
        roster.touch('owl-1')
        assert roster.get('owl-1')['last_confirmed'] == stamp


@pytest.mark.unit
class TestNaming:
    def test_reserve_with_name(self, roster):
        assert roster.reserve(name='Left boom')['name'] == 'Left boom'

    def test_default_name_is_device_id(self, roster):
        assert roster.reserve()['name'] == 'owl-1'

    def test_rename(self, roster):
        roster.reserve()
        assert roster.rename('owl-1', 'Right boom')['name'] == 'Right boom'

    def test_bad_names_rejected(self, roster):
        roster.reserve()
        for bad in ('<script>', 'x' * 41, ''):
            with pytest.raises(FleetRosterError):
                roster.rename('owl-1', bad)
        with pytest.raises(FleetRosterError):
            roster.reserve(name='<img src=x>')


@pytest.mark.unit
class TestPersistence:
    def test_roundtrip_across_instances(self, tmp_path, clock):
        path = tmp_path / 'fleet.json'
        first = FleetRoster('192.168.1.2', path=path, clock=clock)
        first.reserve(name='Left boom')
        first.confirm('owl-1')

        second = FleetRoster('192.168.1.2', path=path, clock=clock)
        assert second.is_registered('owl-1')
        assert second.get('owl-1')['name'] == 'Left boom'

    def test_corrupt_file_starts_empty(self, tmp_path, clock):
        path = tmp_path / 'fleet.json'
        path.write_text('{not json')
        roster = FleetRoster('192.168.1.2', path=path, clock=clock)
        assert roster.is_empty()
        roster.reserve()  # and can still allocate + save

    def test_missing_file_is_empty_not_error(self, roster):
        assert roster.is_empty()

    def test_atomic_write_leaves_valid_json(self, tmp_path, clock):
        path = tmp_path / 'fleet.json'
        roster = FleetRoster('192.168.1.2', path=path, clock=clock)
        roster.reserve()
        data = json.loads(path.read_text())
        assert data['version'] == 1
        assert 'owl-1' in data['devices']
        # No stray temp files left behind
        assert [p.name for p in tmp_path.iterdir()] == ['fleet.json']

    def test_remove_unknown_raises(self, roster):
        with pytest.raises(FleetRosterError):
            roster.remove('owl-9')
