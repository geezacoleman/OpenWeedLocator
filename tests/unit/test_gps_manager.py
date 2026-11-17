"""Tests for GPS manager classes in utils/gps_manager.py."""

import json
import os
import socket
import threading
import time

import pytest
import utils.gps_manager as gps_module
from utils.gps_manager import (
    haversine,
    GPSState,
    SessionStats,
    TrackRecorder,
    GPSManager,
    TCPNMEASource,
    SerialNMEASource,
    GpsdNMEASource,
)


def nmea(body):
    """A sentence with its checksum, as a receiver sends it."""
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f'${body}*{checksum:02X}'


def rmc(status='A', knots=5.0, lat='3347.2000', lon='15112.0000'):
    return nmea(f'GPRMC,123519,{status},{lat},S,{lon},E,{knots:.3f},180.0,230394,,')


def gga(quality=1, lat='3347.2000', lon='15112.0000'):
    return nmea(f'GPGGA,123519,{lat},S,{lon},E,{quality},08,0.9,45.3,M,0.0,M,,')


def vtg(kmh):
    return nmea(f'GPVTG,90.0,T,,M,{kmh / 1.852:.3f},N,{kmh:.2f},K,A')


class Clock:
    """Stands in for the `time` module inside utils.gps_manager: monotonic()
    is whatever the test says it is, everything else is the real thing."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(gps_module, 'time', fake)
    return fake


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
        # 0.2 degrees of longitude at 51.5 N
        assert haversine(51.5, -0.1, 51.5, 0.1) == pytest.approx(13.84, abs=0.05)


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

    def test_rmc_active_stores_utc(self):
        state = GPSState()
        state.update_from_rmc({
            'status': 'A',
            'lat': -33.8688,
            'lon': 151.2093,
            'speed_knots': 5.0,
            'heading': 180.0,
            'time_utc': '015230.50',
            'date': '110626',
        })
        d = state.get_dict()
        assert d['utc_time'] == '015230.50'
        assert d['utc_date'] == '110626'

    def test_rmc_void_does_not_store_utc(self):
        """UTC from a void fix is untrusted — must stay None."""
        state = GPSState()
        state.update_from_rmc({'status': 'V', 'lat': None, 'lon': None,
                               'speed_knots': None, 'heading': None,
                               'time_utc': '015230.50', 'date': '110626'})
        d = state.get_dict()
        assert d['utc_time'] is None
        assert d['utc_date'] is None

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
            state.last_fix_time = time.monotonic() - 15
        d = state.get_dict()
        assert d['fix_valid'] is False
        assert d['age_seconds'] > 10


# ---------------------------------------------------------------------------
# GPSState speed: the number the controller times every nozzle from
# ---------------------------------------------------------------------------

class TestReportedSpeed:
    """A speed is reported only while it is fresh and the fix is good."""

    def test_void_rmc_carrying_a_speed_reports_none(self, clock):
        """Antenna unplugged: status V, but the receiver repeats its last speed."""
        state = GPSState()
        assert state.update_from_line(rmc(status='V', knots=10.0)) == 'RMC'
        d = state.get_dict()
        assert d['speed_kmh'] is None
        assert d['fix_valid'] is False

    def test_void_rmc_does_not_refresh_or_change_a_good_speed(self, clock):
        state = GPSState()
        state.update_from_line(rmc(knots=8.0 / 1.852))
        assert state.get_dict()['speed_kmh'] == 8.0
        # the fix drops to void, still claiming 20 knots; GGA keeps the position fix
        for _ in range(4):
            clock.now += 1.0
            state.update_from_line(rmc(status='V', knots=20.0))
            state.update_from_line(gga())
        d = state.get_dict()
        assert d['fix_valid'] is True
        assert d['speed_kmh'] is None, 'a void RMC must not keep the old speed alive'
        assert state.speed_kmh == pytest.approx(8.0, abs=0.01), 'nor replace it with its own'

    def test_speed_is_reported_for_three_seconds_and_no_longer(self, clock):
        state = GPSState()
        state.update_from_line(rmc(knots=8.0 / 1.852))
        clock.now += 2.9
        state.update_from_line(gga())                 # the fix itself stays fresh
        assert state.get_dict()['speed_kmh'] == 8.0
        clock.now += 0.2                              # 3.1 s since the speed
        d = state.get_dict()
        assert d['fix_valid'] is True
        assert d['speed_kmh'] is None, 'a frozen speed looks exactly like a live one'

    def test_a_new_speed_restarts_the_three_seconds(self, clock):
        state = GPSState()
        state.update_from_line(rmc(knots=8.0 / 1.852))
        clock.now += 2.5
        state.update_from_line(rmc(knots=9.0 / 1.852))
        clock.now += 2.5
        assert state.get_dict()['speed_kmh'] == 9.0

    def test_no_fix_means_no_speed(self, clock):
        """VTG has no validity of its own: without a position fix its speed
        is not reported."""
        state = GPSState()
        assert state.update_from_line(vtg(12.0)) == 'VTG'
        assert state.get_dict() == dict(state.get_dict(), fix_valid=False, speed_kmh=None)
        state.update_from_line(gga(quality=0))
        assert state.get_dict()['speed_kmh'] is None

    def test_speed_is_dropped_when_the_fix_goes_stale(self, clock):
        state = GPSState()
        state.update_from_line(gga())
        clock.now += 10.5                             # fix is now older than 10 s
        state.update_from_line(vtg(12.0))             # speed itself is brand new
        d = state.get_dict()
        assert d['fix_valid'] is False
        assert d['speed_kmh'] is None

    def test_vtg_updates_speed_and_heading(self, clock):
        state = GPSState()
        state.update_from_line(rmc(knots=5.0))
        assert state.get_dict()['speed_kmh'] == 9.3   # 5 kn
        clock.now += 0.2
        state.update_from_line(vtg(12.5))
        d = state.get_dict()
        assert d['speed_kmh'] == 12.5
        assert d['heading'] == 90.0

    @pytest.mark.parametrize('kmh, reported', [
        (0.0, 0.0),       # stopped is a speed, not "no speed"
        (0.5, 0.5),
        (0.04, 0.0),      # reported to one decimal
        (0.26, 0.3),
        (7.96, 8.0),
    ])
    def test_low_speeds_pass_through_to_one_decimal(self, clock, kmh, reported):
        for line in (rmc(knots=kmh / 1.852), vtg(kmh)):
            state = GPSState()
            state.update_from_line(gga())
            state.update_from_line(line)
            got = state.get_dict()['speed_kmh']
            assert got == reported and got is not None, line

    def test_unrecognised_and_corrupt_lines_change_nothing(self, clock):
        state = GPSState()
        before = state.get_dict()
        good = rmc(knots=5.0)
        assert state.update_from_line(good[:-2] + '00') is None      # bad checksum
        assert state.update_from_line('garbage') is None
        assert state.get_dict() == before


# ---------------------------------------------------------------------------
# SessionStats
# ---------------------------------------------------------------------------

class TestSessionStats:
    def test_initial_state(self):
        s = SessionStats()
        assert s.active is False
        assert s.distance_km == 0.0

    def test_distance_accumulation(self):
        s = SessionStats(boom_width_m=12.0)
        s.start()
        assert s.active is True
        # Move ~111 m north (0.001 degree latitude at equator)
        s.update(0.0, 0.0)
        s.update(0.001, 0.0)
        assert s.distance_km == pytest.approx(0.1112, abs=0.0005)
        s.stop()
        assert s.active is False
        s.update(0.002, 0.0)
        assert s.distance_km == pytest.approx(0.1112, abs=0.0005), 'a stopped session must not grow'

    def test_jitter_under_a_metre_adds_no_distance(self):
        s = SessionStats()
        s.start()
        s.update(0.0, 0.0)
        for _ in range(50):
            s.update(0.000005, 0.0)   # ~0.55 m: a parked receiver wandering
        assert s.distance_km == 0.0

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
        s.start()
        s.update(0.0, 0.0)
        s.update(0.01, 0.0)      # 1.112 km
        d = s.to_dict()
        assert d['time_active_s'] == pytest.approx(0, abs=1)
        del d['time_active_s']
        assert d == {'active': True, 'distance_km': 1.112,
                     'area_hectares': 1.33, 'boom_width_m': 12.0}


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

    def test_coordinates_property_returns_copy(self, tmp_path):
        save_dir = str(tmp_path / 'tracks')
        rec = TrackRecorder()
        rec.start(save_dir)
        rec._last_record_time = 0
        rec.add_point(-33.8688, 151.2093, speed=5.0, heading=90.0)
        rec._last_record_time = 0
        rec.add_point(-33.8700, 151.2110, speed=6.0, heading=95.0)

        snap = rec.coordinates
        assert isinstance(snap, list)
        assert len(snap) == 2
        # Mutating the snapshot must not affect the recorder's buffer
        snap.clear()
        assert len(rec.coordinates) == 2

    def test_concurrent_add_and_read_safe(self, tmp_path):
        """Many concurrent add_point + coordinates reads should not raise."""
        save_dir = str(tmp_path / 'tracks')
        rec = TrackRecorder()
        rec.start(save_dir)

        stop_flag = threading.Event()
        errors = []

        def writer():
            i = 0
            while not stop_flag.is_set():
                try:
                    rec._last_record_time = 0
                    rec.add_point(
                        -33.8688 + i * 0.0001, 151.2093 + i * 0.0001,
                        speed=5.0, heading=90.0,
                    )
                    i += 1
                except Exception as e:
                    errors.append(e)

        def reader():
            while not stop_flag.is_set():
                try:
                    _ = rec.coordinates
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        stop_flag.set()
        for t in threads:
            t.join(timeout=2)
        rec.stop()

        assert errors == []


# ---------------------------------------------------------------------------
# NMEA source constructors — ensure they instantiate without side-effects
# ---------------------------------------------------------------------------

class TestNMEASources:
    def test_serial_source_without_pyserial(self, monkeypatch):
        """If pyserial is missing, the source should log and exit cleanly, not crash."""
        import utils.gps_manager as gm
        monkeypatch.setattr(gm, 'serial', None)

        called = {'line': False, 'connect': False}
        src = SerialNMEASource(
            on_line=lambda l: called.__setitem__('line', True),
            on_connect_change=lambda b: called.__setitem__('connect', True),
            device='/dev/nonexistent', baudrate=9600,
        )
        src.start()
        src._thread.join(timeout=2)
        assert not src._thread.is_alive(), 'the reader must give up, not spin'
        assert called == {'line': False, 'connect': False}


# ---------------------------------------------------------------------------
# GPSManager — source selection
# ---------------------------------------------------------------------------

class TestGPSManagerSourceSelection:
    def test_default_is_tcp(self):
        mgr = GPSManager(source='tcp', port=18500)
        assert isinstance(mgr._source, TCPNMEASource) and mgr._source.port == 18500
        state = mgr.get_state()
        assert state['connection'] == {'gps_connected': False, 'gps_enabled': True,
                                       'source': 'tcp'}

    def test_serial_source_gets_the_configured_port(self):
        mgr = GPSManager(source='SERIAL', serial_device='/dev/ttyUSB3', baudrate=115200)
        assert mgr.source_name == 'serial'
        assert isinstance(mgr._source, SerialNMEASource)
        assert (mgr._source.device, mgr._source.baudrate) == ('/dev/ttyUSB3', 115200)

    def test_gpsd_source(self):
        mgr = GPSManager(source='gpsd')
        assert isinstance(mgr._source, GpsdNMEASource)
        assert (mgr._source.host, mgr._source.port) == ('localhost', 2947)

    def test_invalid_source_raises(self):
        with pytest.raises(ValueError):
            GPSManager(source='bogus')

    def test_on_connect_change_updates_state(self):
        mgr = GPSManager(source='tcp')
        assert mgr.get_state()['connection']['gps_connected'] is False
        mgr._source._on_connect_change(True)
        assert mgr.get_state()['connection']['gps_connected'] is True
        mgr._source._on_connect_change(False)
        assert mgr.get_state()['connection']['gps_connected'] is False


# ---------------------------------------------------------------------------
# GPSManager: a line from the source reaches state, session and track
# ---------------------------------------------------------------------------

def _wait(condition, what, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    pytest.fail(f'timed out waiting for {what}')


class TestLinesReachTheManager:
    @pytest.mark.parametrize('source', ['tcp', 'serial', 'gpsd'])
    def test_a_line_from_the_source_updates_the_fix(self, source, tmp_path):
        """Whatever the transport, the callback it was built with must end in
        the manager's state."""
        mgr = GPSManager(source=source, track_dir=str(tmp_path))
        mgr._source._on_line(rmc(knots=5.0))
        fix = mgr.get_state()['fix']
        assert fix['fix_valid'] is True
        assert fix['latitude'] == pytest.approx(-33.78667, abs=1e-4)
        assert fix['longitude'] == pytest.approx(151.2, abs=1e-4)
        assert fix['speed_kmh'] == 9.3
        assert fix['heading'] == 180.0

    def test_lines_feed_the_session_and_the_track(self, tmp_path):
        mgr = GPSManager(source='tcp', track_dir=str(tmp_path), boom_width=10.0)
        mgr.start_session()
        mgr._source._on_line(rmc(lat='3347.2000'))
        mgr.recorder._last_record_time = 0            # skip the 1 s point spacing
        mgr._source._on_line(rmc(lat='3347.3000'))    # 0.1 minute south = 185 m

        session = mgr.get_state()['session']
        assert session['active'] is True
        assert session['distance_km'] == pytest.approx(0.185, abs=0.002)
        assert session['area_hectares'] == pytest.approx(0.19, abs=0.01)

        mgr.stop_session()
        (track,) = tmp_path.glob('track_*.geojson')
        props = json.loads(track.read_text())['features'][0]
        assert props['properties']['point_count'] == 2
        assert props['properties']['speeds_kmh'] == [9.3, 9.3]
        assert props['geometry']['coordinates'][0] == [151.2, pytest.approx(-33.7866667)]

    def test_no_fix_adds_nothing_to_the_session_or_track(self, tmp_path):
        mgr = GPSManager(source='tcp', track_dir=str(tmp_path))
        mgr.start_session()
        mgr._source._on_line(rmc(status='V'))
        mgr._source._on_line(gga(quality=0))
        assert mgr.session.last_point is None
        assert mgr.recorder.coordinates == []

    def test_nmea_over_a_real_tcp_connection(self, tmp_path):
        """The Teltonika router pushes NMEA to the OWL over TCP: lines split
        across packets, with its IMEI prefix, end up as a fix."""
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        mgr = GPSManager(source='tcp', port=port, track_dir=str(tmp_path))
        mgr.start()
        try:
            _wait(lambda: mgr._source._server_socket is not None, 'the GPS TCP server')
            client = None
            deadline = time.monotonic() + 5
            while client is None:
                try:
                    client = socket.create_connection(('127.0.0.1', port), timeout=1)
                except OSError:
                    assert time.monotonic() < deadline, 'GPS TCP server never listened'
                    time.sleep(0.05)
            with client:
                _wait(lambda: mgr.get_state()['connection']['gps_connected'], 'gps_connected')
                line = '6003197898_' + rmc(knots=4.0 / 1.852) + '\r\n'
                client.sendall(line[:20].encode())
                time.sleep(0.05)
                client.sendall(line[20:].encode() + gga().encode() + b'\r\n')
                _wait(lambda: mgr.get_state()['fix']['fix_valid'], 'a fix from the TCP stream')
                fix = mgr.get_state()['fix']
                assert fix['speed_kmh'] == 4.0
                assert fix['satellites'] == 8
            _wait(lambda: not mgr.get_state()['connection']['gps_connected'],
                  'the disconnect to be noticed')
        finally:
            mgr.stop()


class TestTrackFileSafety:
    """The track was rewritten in place every 100 points, so a power cut
    mid-write wiped the whole session track; two threads could flush at once."""

    def _points(self, rec, n, start=0):
        for i in range(start, start + n):
            rec._last_record_time = 0
            rec.add_point(-33.0 - i * 0.001, 151.0, speed=5.0, heading=90.0)

    def test_failed_write_keeps_the_previous_file(self, tmp_path, monkeypatch):
        import json as _json
        rec = TrackRecorder()
        rec.start(str(tmp_path))
        self._points(rec, 3)
        path = rec._flush()
        good = path and open(path).read()
        self._points(rec, 3, start=3)

        def broken_dump(obj, f, *a, **k):
            f.write('{"half": ')
            raise OSError('power cut')

        monkeypatch.setattr('utils.gps_manager.json.dump', broken_dump)
        rec._flush()
        assert open(path).read() == good, 'the last good track must survive a failed write'
        assert not os.path.exists(path + '.tmp')
        assert _json.loads(good)['features'][0]['properties']['point_count'] == 3

    def test_periodic_flush_does_not_block_the_caller(self, tmp_path):
        rec = TrackRecorder()
        rec.start(str(tmp_path))
        rec._flush_lock.acquire()          # a slow flush already running
        try:
            self._points(rec, 100)          # 100th point asks for a flush
        finally:
            rec._flush_lock.release()
        assert rec.stop() is not None
        with open(rec._filepath) as f:
            assert '"point_count": 100' in f.read()
