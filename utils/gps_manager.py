"""
GPS Manager for OWL Central Controller.

Receives NMEA sentences from a Teltonika router via TCP push,
parses GPS data, tracks sessions, and records GeoJSON tracks.

Data flow:
    Teltonika RUTX14 --TCP--> GPSManager (port 8500)
        -> NMEA parsing -> GPSState (thread-safe current fix)
        -> SessionStats (distance, time, area during detection)
        -> TrackRecorder (GeoJSON file output)
"""

import json
import logging
import math
import os
import socket
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
KNOTS_TO_KMH = 1.852
EARTH_RADIUS_KM = 6371.0

# ---------------------------------------------------------------------------
# NMEA Parser — pure stateless functions, no I/O
# ---------------------------------------------------------------------------

def validate_checksum(sentence):
    """Verify NMEA XOR checksum.

    Sentence format: $....*HH\r\n
    Returns True if checksum matches or no checksum present.
    """
    sentence = sentence.strip()
    if not sentence.startswith('$'):
        return False

    if '*' not in sentence:
        return False

    body, checksum_hex = sentence[1:].rsplit('*', 1)
    checksum_hex = checksum_hex.strip()

    if len(checksum_hex) != 2:
        return False

    try:
        expected = int(checksum_hex, 16)
    except ValueError:
        return False

    computed = 0
    for ch in body:
        computed ^= ord(ch)

    return computed == expected


def nmea_to_decimal(raw, hemisphere):
    """Convert NMEA coordinate (DDMM.MMMM or DDDMM.MMMM) to signed decimal degrees.

    Args:
        raw: string like '4807.038' (lat) or '01131.000' (lon)
        hemisphere: 'N', 'S', 'E', or 'W'

    Returns:
        float decimal degrees (negative for S/W), or None on error.
    """
    if not raw or not hemisphere:
        return None

    try:
        raw_float = float(raw)
    except ValueError:
        return None

    # Degrees are the integer part of (value / 100)
    degrees = int(raw_float / 100)
    minutes = raw_float - (degrees * 100)
    decimal = degrees + minutes / 60.0

    if hemisphere in ('S', 'W'):
        decimal = -decimal

    return decimal


def parse_gprmc(sentence):
    """Parse $GPRMC (Recommended Minimum) sentence.

    Returns dict with: lat, lon, speed_knots, heading, status, time_utc, date
    or None on parse failure.
    """
    parts = _split_sentence(sentence)
    if parts is None or len(parts) < 12:
        return None

    status = parts[2]  # A=active, V=void

    return {
        'type': 'RMC',
        'time_utc': parts[1] or None,
        'status': status,
        'lat': nmea_to_decimal(parts[3], parts[4]),
        'lon': nmea_to_decimal(parts[5], parts[6]),
        'speed_knots': _safe_float(parts[7]),
        'heading': _safe_float(parts[8]),
        'date': parts[9] or None,
    }


def parse_gpgga(sentence):
    """Parse $GPGGA (Global Positioning System Fix Data) sentence.

    Returns dict with: lat, lon, fix_quality, satellites, hdop, altitude
    or None on parse failure.
    """
    parts = _split_sentence(sentence)
    if parts is None or len(parts) < 15:
        return None

    return {
        'type': 'GGA',
        'lat': nmea_to_decimal(parts[2], parts[3]),
        'lon': nmea_to_decimal(parts[4], parts[5]),
        'fix_quality': _safe_int(parts[6]),
        'satellites': _safe_int(parts[7]),
        'hdop': _safe_float(parts[8]),
        'altitude': _safe_float(parts[9]),
    }


def parse_gpvtg(sentence):
    """Parse $GPVTG (Track Made Good and Ground Speed) sentence.

    Returns dict with: heading_true, speed_knots, speed_kmh
    or None on parse failure.
    """
    parts = _split_sentence(sentence)
    if parts is None or len(parts) < 9:
        return None

    return {
        'type': 'VTG',
        'heading_true': _safe_float(parts[1]),
        'speed_knots': _safe_float(parts[5]),
        'speed_kmh': _safe_float(parts[7]),
    }


def parse_gpgsv(sentence):
    """Parse $GPGSV (Satellites in View) sentence.

    Returns dict with: satellites_in_view
    or None on parse failure.
    """
    parts = _split_sentence(sentence)
    if parts is None or len(parts) < 4:
        return None

    return {
        'type': 'GSV',
        'satellites_in_view': _safe_int(parts[3]),
    }


def parse_sentence(line):
    """Route an NMEA sentence to the correct parser.

    Handles $GP and $GN prefixes.
    Returns parsed dict or None if unrecognised/invalid.
    """
    line = line.strip()
    if not line.startswith('$'):
        return None

    if not validate_checksum(line):
        return None

    # Normalise prefix: $GPRMC, $GNRMC -> RMC
    # Find the sentence type after the prefix
    if '*' in line:
        body = line[1:].split('*')[0]
    else:
        body = line[1:]

    # The sentence type is the first field up to the first comma
    header = body.split(',')[0]

    # Strip prefix (GP, GN, GL, GA, etc.) to get sentence type
    if len(header) >= 5:
        sentence_type = header[2:]  # e.g. 'GPRMC' -> 'RMC'
    else:
        sentence_type = header

    parsers = {
        'RMC': parse_gprmc,
        'GGA': parse_gpgga,
        'VTG': parse_gpvtg,
        'GSV': parse_gpgsv,
    }

    parser = parsers.get(sentence_type)
    if parser:
        return parser(line)

    return None


# --- Parser helpers ---

def _split_sentence(sentence):
    """Strip checksum and split NMEA sentence into fields."""
    sentence = sentence.strip()
    if '*' in sentence:
        sentence = sentence.split('*')[0]
    if sentence.startswith('$'):
        sentence = sentence[1:]
    parts = sentence.split(',')
    return parts if len(parts) > 1 else None


def _safe_float(s):
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _safe_int(s):
    try:
        return int(s) if s else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------

def haversine(lat1, lon1, lat2, lon2):
    """Distance in km between two GPS points using the haversine formula."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)

    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r

    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return EARTH_RADIUS_KM * c


# ---------------------------------------------------------------------------
# GPSState — thread-safe container for current fix
# ---------------------------------------------------------------------------

class GPSState:
    """Thread-safe container for the current GPS fix."""

    def __init__(self):
        self._lock = threading.Lock()
        self.latitude = None
        self.longitude = None
        self.speed_kmh = None
        self.heading = None
        self.satellites = None
        self.hdop = None
        self.altitude = None
        self.fix_valid = False
        self.last_fix_time = None
        self.connected = False

    def update_from_rmc(self, data):
        """Update state from a parsed RMC sentence."""
        with self._lock:
            if data.get('status') == 'A' and data.get('lat') is not None:
                self.latitude = data['lat']
                self.longitude = data['lon']
                self.fix_valid = True
                self.last_fix_time = time.time()
            # Don't clear fix_valid here — a GGA fix may still be valid.
            # The 10s staleness check in get_dict() handles true fix loss.

            if data.get('speed_knots') is not None:
                self.speed_kmh = data['speed_knots'] * KNOTS_TO_KMH

            if data.get('heading') is not None:
                self.heading = data['heading']

    def update_from_gga(self, data):
        """Update state from a parsed GGA sentence."""
        with self._lock:
            if data.get('lat') is not None:
                self.latitude = data['lat']
                self.longitude = data['lon']
            if data.get('satellites') is not None:
                self.satellites = data['satellites']
            if data.get('hdop') is not None:
                self.hdop = data['hdop']
            if data.get('altitude') is not None:
                self.altitude = data['altitude']
            if data.get('fix_quality') is not None and data['fix_quality'] > 0:
                self.fix_valid = True
                self.last_fix_time = time.time()

    def update_from_vtg(self, data):
        """Update state from a parsed VTG sentence."""
        with self._lock:
            if data.get('speed_kmh') is not None:
                self.speed_kmh = data['speed_kmh']
            if data.get('heading_true') is not None:
                self.heading = data['heading_true']

    def update_from_gsv(self, data):
        """Update state from a parsed GSV sentence."""
        with self._lock:
            if data.get('satellites_in_view') is not None:
                self.satellites = data['satellites_in_view']

    def get_dict(self):
        """Return a snapshot of the current state as a dict."""
        with self._lock:
            age = None
            if self.last_fix_time is not None:
                age = round(time.time() - self.last_fix_time, 1)
                # Mark fix invalid if stale (>10s)
                if age > 10:
                    fix_valid = False
                else:
                    fix_valid = self.fix_valid
            else:
                fix_valid = False

            return {
                'latitude': self.latitude,
                'longitude': self.longitude,
                'speed_kmh': round(self.speed_kmh, 1) if self.speed_kmh is not None else None,
                'heading': round(self.heading, 1) if self.heading is not None else None,
                'satellites': self.satellites,
                'hdop': round(self.hdop, 1) if self.hdop is not None else None,
                'altitude': round(self.altitude, 1) if self.altitude is not None else None,
                'fix_valid': fix_valid,
                'age_seconds': age,
            }


# ---------------------------------------------------------------------------
# SessionStats — accumulates distance/time/area during active detection
# ---------------------------------------------------------------------------

class SessionStats:
    """Accumulates session statistics during active detection."""

    def __init__(self, boom_width_m=12.0):
        self.boom_width_m = boom_width_m
        self.distance_km = 0.0
        self.time_active_s = 0.0
        self.session_start = None
        self.last_point = None
        self._active = False

    @property
    def area_hectares(self):
        """Area = distance * boom_width / 10.0 (km * m -> hectares)."""
        return self.distance_km * self.boom_width_m / 10.0

    def start(self):
        self._active = True
        self.distance_km = 0.0
        self.time_active_s = 0.0
        self.session_start = time.time()
        self.last_point = None

    def stop(self):
        if self._active and self.session_start:
            self.time_active_s = time.time() - self.session_start
        self._active = False

    def reset(self):
        self.distance_km = 0.0
        self.time_active_s = 0.0
        self.session_start = None
        self.last_point = None
        self._active = False

    @property
    def active(self):
        return self._active

    def update(self, lat, lon):
        """Add a point. Accumulates distance if moved >1m."""
        if not self._active or lat is None or lon is None:
            return

        if self.session_start:
            self.time_active_s = time.time() - self.session_start

        if self.last_point is not None:
            dist = haversine(self.last_point[0], self.last_point[1], lat, lon)
            # Only add if moved more than 1m (0.001 km)
            if dist > 0.001:
                self.distance_km += dist
                self.last_point = (lat, lon)
        else:
            self.last_point = (lat, lon)

    def to_dict(self):
        return {
            'active': self._active,
            'distance_km': round(self.distance_km, 3),
            'time_active_s': round(self.time_active_s, 0),
            'area_hectares': round(self.area_hectares, 2),
            'boom_width_m': self.boom_width_m,
        }


# ---------------------------------------------------------------------------
# TrackRecorder — writes GeoJSON track files
# ---------------------------------------------------------------------------

class TrackRecorder:
    """Records GPS track to a GeoJSON file."""

    MIN_INTERVAL_S = 1.0   # Min seconds between points
    MIN_DISTANCE_M = 0.5   # Min meters to record (skip GPS drift)

    def __init__(self):
        self._recording = False
        self._filepath = None
        self._coordinates = []
        self._speeds = []
        self._headings = []
        self._timestamps = []
        self._start_time = None
        self._last_record_time = 0
        self._last_point = None

    @property
    def recording(self):
        return self._recording

    def start(self, save_dir):
        """Start a new track recording."""
        os.makedirs(save_dir, exist_ok=True)
        timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
        self._filepath = os.path.join(save_dir, f'track_{timestamp}.geojson')
        self._coordinates = []
        self._speeds = []
        self._headings = []
        self._timestamps = []
        self._start_time = datetime.now(timezone.utc)
        self._last_record_time = 0
        self._last_point = None
        self._recording = True
        logger.info(f"Track recording started: {self._filepath}")

    def add_point(self, lat, lon, speed, heading, timestamp=None, altitude=None):
        """Add a point to the track if time/distance thresholds met."""
        if not self._recording or lat is None or lon is None:
            return

        now = time.time()

        # Check minimum interval
        if now - self._last_record_time < self.MIN_INTERVAL_S:
            return

        # Check minimum distance (skip GPS drift when stationary)
        if self._last_point is not None:
            dist_km = haversine(self._last_point[0], self._last_point[1], lat, lon)
            if dist_km * 1000 < self.MIN_DISTANCE_M:
                return

        self._last_record_time = now
        self._last_point = (lat, lon)

        # GeoJSON coordinates: [lon, lat, alt]
        coord = [round(lon, 7), round(lat, 7)]
        if altitude is not None:
            coord.append(round(altitude, 1))
        self._coordinates.append(coord)

        self._speeds.append(round(speed, 1) if speed is not None else None)
        self._headings.append(round(heading, 1) if heading is not None else None)

        ts = timestamp or datetime.now(timezone.utc).isoformat()
        self._timestamps.append(ts)

        # Flush periodically (every 100 points)
        if len(self._coordinates) % 100 == 0:
            self._flush()

    def stop(self):
        """Stop recording and write final GeoJSON file."""
        if not self._recording:
            return None

        self._recording = False
        filepath = self._flush()
        logger.info(f"Track recording stopped: {filepath} ({len(self._coordinates)} points)")
        return filepath

    def _flush(self):
        """Write current track data to the GeoJSON file."""
        if not self._filepath or not self._coordinates:
            return self._filepath

        end_time = datetime.now(timezone.utc)

        # Calculate total distance from coordinates
        total_dist = 0.0
        for i in range(1, len(self._coordinates)):
            c1 = self._coordinates[i - 1]
            c2 = self._coordinates[i]
            total_dist += haversine(c1[1], c1[0], c2[1], c2[0])

        geojson = {
            'type': 'FeatureCollection',
            'features': [{
                'type': 'Feature',
                'geometry': {
                    'type': 'LineString',
                    'coordinates': self._coordinates,
                },
                'properties': {
                    'name': f"OWL Session {self._start_time.strftime('%Y-%m-%d %H:%M') if self._start_time else 'unknown'}",
                    'start_time': self._start_time.isoformat() if self._start_time else None,
                    'end_time': end_time.isoformat(),
                    'distance_km': round(total_dist, 3),
                    'point_count': len(self._coordinates),
                    'speeds_kmh': self._speeds,
                    'headings_deg': self._headings,
                    'timestamps': self._timestamps,
                },
            }],
        }

        try:
            with open(self._filepath, 'w') as f:
                json.dump(geojson, f)
        except IOError as e:
            logger.warning(f"Failed to write track file: {e}")

        return self._filepath


# ---------------------------------------------------------------------------
# GPSManager — main coordinator with TCP server
# ---------------------------------------------------------------------------

class GPSManager:
    """Manages GPS data from a Teltonika router via TCP NMEA forwarding.

    Args:
        port: TCP port to listen on (default 8500)
        boom_width: Boom width in metres for area calculation
        track_dir: Directory to save GeoJSON track files
    """

    def __init__(self, port=8500, boom_width=12.0, track_dir='tracks/'):
        self.port = port
        self.track_dir = track_dir

        self.state = GPSState()
        self.session = SessionStats(boom_width_m=boom_width)
        self.recorder = TrackRecorder()

        self._running = False
        self._server_thread = None
        self._server_socket = None

    def start(self):
        """Start the TCP server thread to receive NMEA data."""
        if self._running:
            return

        self._running = True
        self._server_thread = threading.Thread(target=self._tcp_server_loop, daemon=True)
        self._server_thread.start()
        logger.info(f"GPS Manager started on port {self.port}")

    def stop(self):
        """Shut down the GPS manager cleanly."""
        self._running = False

        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass

        if self.recorder.recording:
            self.recorder.stop()

        logger.info("GPS Manager stopped")

    @property
    def session_active(self):
        return self.session.active

    def start_session(self):
        """Start a GPS tracking session (called when detection starts)."""
        self.session.start()
        self.recorder.start(self.track_dir)
        logger.info("GPS session started")

    def stop_session(self):
        """Stop the GPS tracking session (called when detection stops)."""
        self.session.stop()
        self.recorder.stop()
        logger.info("GPS session stopped")

    def get_state(self):
        """Return complete GPS state for the /api/gps endpoint."""
        fix = self.state.get_dict()
        return {
            'fix': fix,
            'connection': {
                'tcp_connected': self.state.connected,
                'gps_enabled': True,
            },
            'session': self.session.to_dict(),
        }

    def _tcp_server_loop(self):
        """TCP server loop — accepts connections from the Teltonika router."""
        try:
            self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_socket.settimeout(2.0)  # Allow periodic shutdown check
            self._server_socket.bind(('0.0.0.0', self.port))
            self._server_socket.listen(1)
            logger.info(f"GPS TCP server listening on 0.0.0.0:{self.port}")
        except OSError as e:
            logger.error(f"GPS TCP server failed to start: {e}")
            self._running = False
            return

        while self._running:
            try:
                conn, addr = self._server_socket.accept()
                logger.info(f"GPS client connected from {addr}")
                self.state.connected = True
                self._handle_client(conn)
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    logger.warning("GPS TCP server socket error")
                break

        try:
            self._server_socket.close()
        except Exception:
            pass

    def _handle_client(self, conn):
        """Handle a single TCP client connection (NMEA stream)."""
        conn.settimeout(30.0)
        buffer = ''

        try:
            while self._running:
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    # No data for 30s — connection probably dead
                    logger.warning("GPS client timeout (30s no data)")
                    break

                if not data:
                    break

                buffer += data.decode('ascii', errors='replace')

                # Process complete lines (NMEA uses \r\n)
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    line = line.strip()
                    if line:
                        self._process_sentence(line)

        except Exception as e:
            logger.warning(f"GPS client error: {e}")
        finally:
            self.state.connected = False
            try:
                conn.close()
            except Exception:
                pass
            logger.info("GPS client disconnected")

    def _process_sentence(self, line):
        """Parse a single NMEA sentence and update all state."""
        parsed = parse_sentence(line)
        if parsed is None:
            return

        sentence_type = parsed.get('type')

        if sentence_type == 'RMC':
            self.state.update_from_rmc(parsed)
        elif sentence_type == 'GGA':
            self.state.update_from_gga(parsed)
        elif sentence_type == 'VTG':
            self.state.update_from_vtg(parsed)
        elif sentence_type == 'GSV':
            self.state.update_from_gsv(parsed)

        # Feed session stats and track recorder with latest position
        snapshot = self.state.get_dict()
        if snapshot['fix_valid'] and snapshot['latitude'] is not None:
            self.session.update(snapshot['latitude'], snapshot['longitude'])
            self.recorder.add_point(
                lat=snapshot['latitude'],
                lon=snapshot['longitude'],
                speed=snapshot['speed_kmh'],
                heading=snapshot['heading'],
                altitude=snapshot['altitude'],
            )
