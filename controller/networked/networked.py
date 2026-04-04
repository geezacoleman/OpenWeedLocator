#!/usr/bin/env python3
"""
OWL Central Controller - Ghost OWL Fix
Key improvements:
1. Reduced TTL from 15s to 8s
2. More aggressive offline marking (5s timeout)
3. Last Will & Testament support
4. Better state cleanup
"""

import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from collections import deque
from flask import Flask, render_template, jsonify, request, Response, stream_with_context, send_from_directory, send_file
import urllib3
import paho.mqtt.client as mqtt
import json
import threading
import time
import logging
import configparser
import hashlib
import shutil
import io
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
import requests
from werkzeug.utils import secure_filename

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

SHARED_DIR = os.path.join(os.path.dirname(__file__), '..', 'shared')

# Model management constants
UPLOADS_DIR = Path(__file__).parent.parent.parent / 'uploads'
ALLOWED_MODEL_EXTENSIONS = {'pt', 'zip'}
PROTECTED_MODEL_FILES = set()  # .gitignore protected by secure_filename stripping dots
MAX_ZIP_UNCOMPRESSED_SIZE = 500 * 1024 * 1024  # 500MB zip bomb guard

# Downloads staging directory
DOWNLOADS_DIR = Path(__file__).parent.parent.parent / 'downloads'
DOWNLOADS_DIR.mkdir(exist_ok=True)
MAX_DOWNLOADS_SIZE_MB = 2000  # 2GB default staging quota


def _compute_sha256(filepath):
    """Streaming SHA256 hash for large files."""
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _validate_ncnn_zip(zip_path):
    """Validate a zip contains valid NCNN model files (.param + .bin)."""
    with zipfile.ZipFile(zip_path, 'r') as zf:
        names = zf.namelist()

        # Check total uncompressed size (zip bomb guard)
        total_size = sum(info.file_size for info in zf.infolist())
        if total_size > MAX_ZIP_UNCOMPRESSED_SIZE:
            return False, f'Uncompressed size {total_size} exceeds limit'

        # Check for path traversal
        for name in names:
            if name.startswith('/') or '..' in name:
                return False, f'Unsafe path in zip: {name}'

        # Check for symlinks
        for info in zf.infolist():
            if info.external_attr >> 16 & 0o120000 == 0o120000:
                return False, f'Symlink in zip: {info.filename}'

        # Check NCNN structure: need at least one .param and one .bin
        has_param = any(n.endswith('.param') for n in names)
        has_bin = any(n.endswith('.bin') for n in names)

        if not (has_param and has_bin):
            return False, 'Zip must contain .param and .bin files for NCNN model'

    return True, 'ok'


class SpeedAverager:
    """Time-windowed moving average of GPS speed."""

    def __init__(self, window_seconds=5.0):
        self.window_seconds = window_seconds
        self._samples = deque()  # (timestamp, speed_kmh)
        self._lock = threading.Lock()
        self._last_valid_avg = None
        self._last_sample_time = None

    def add_sample(self, speed_kmh):
        """Add a speed sample and return current average."""
        now = time.time()
        with self._lock:
            self._samples.append((now, speed_kmh))
            self._prune()
            avg = self._compute_avg()
            if avg is not None:
                self._last_valid_avg = avg
            self._last_sample_time = now
            return avg

    def get_average(self):
        """Current window average without adding a sample."""
        with self._lock:
            self._prune()
            return self._compute_avg()

    def get_fallback_speed(self):
        """Last known valid average for GPS dropout."""
        with self._lock:
            return self._last_valid_avg

    def seconds_since_update(self):
        """Time since last GPS sample."""
        with self._lock:
            if self._last_sample_time is None:
                return None
            return time.time() - self._last_sample_time

    def _prune(self):
        """Remove samples older than the window."""
        cutoff = time.time() - self.window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def _compute_avg(self):
        """Compute average from current window."""
        if not self._samples:
            return None
        total = sum(s[1] for s in self._samples)
        return total / len(self._samples)


class ActuationCalculator:
    """Pure math: converts speed + geometry into actuation timing."""

    MIN_SPEED = 0.5   # km/h — below this, use fallback
    MIN_DURATION = 0.01  # seconds floor
    MAX_DURATION = 5.0   # seconds cap

    def __init__(self, actuation_length_cm=10.0, offset_cm=30.0,
                 fallback_duration=0.15, fallback_delay=0.0):
        self.actuation_length_cm = actuation_length_cm
        self.offset_cm = offset_cm
        self.fallback_duration = fallback_duration
        self.fallback_delay = fallback_delay

    def compute(self, speed_kmh):
        """Compute actuation params from speed.

        Returns dict with actuation_duration, delay, source, speed_used.
        """
        if speed_kmh is None or speed_kmh < self.MIN_SPEED:
            return {
                'actuation_duration': self.fallback_duration,
                'delay': self.fallback_delay,
                'source': 'config',
                'speed_used': 0.0
            }

        speed_m_s = speed_kmh / 3.6
        duration = (self.actuation_length_cm / 100.0) / speed_m_s
        delay = (self.offset_cm / 100.0) / speed_m_s

        # Clamp to safety bounds
        duration = max(self.MIN_DURATION, min(self.MAX_DURATION, duration))
        delay = max(0.0, min(self.MAX_DURATION, delay))

        return {
            'actuation_duration': round(duration, 4),
            'delay': round(delay, 4),
            'source': 'gps',
            'speed_used': round(speed_kmh, 2)
        }

    def check_coverage(self, speed_kmh, avg_loop_time_ms):
        """Check if actuation length covers the gap between frames.

        Returns dict with coverage_ok, min_gap_cm, message.
        """
        if speed_kmh is None or speed_kmh < self.MIN_SPEED or avg_loop_time_ms <= 0:
            return {'coverage_ok': True, 'min_gap_cm': 0.0, 'message': ''}

        # Distance travelled per frame (loop iteration)
        min_gap_cm = (speed_kmh / 3.6) * (avg_loop_time_ms / 1000.0) * 100.0
        coverage_ok = self.actuation_length_cm >= min_gap_cm

        message = ''
        if not coverage_ok:
            message = (f"Coverage gap: actuation {self.actuation_length_cm}cm "
                       f"< frame gap {min_gap_cm:.1f}cm at {speed_kmh:.1f} km/h")

        return {
            'coverage_ok': coverage_ok,
            'min_gap_cm': round(min_gap_cm, 1),
            'message': message
        }


class CentralController:
    """Central controller for managing multiple OWLs via MQTT"""

    def __init__(self, config_file=None):
        if config_file is None:
            config_file = Path(__file__).parent.parent.parent / 'config' / 'CONTROLLER.ini'
        self.config = self._load_config(config_file)

        # MQTT Configuration from config file
        self.broker_host = self.config.get('MQTT', 'broker_ip', fallback='localhost')
        self.broker_port = self.config.getint('MQTT', 'broker_port', fallback=1883)
        self.client_id = self.config.get('MQTT', 'client_id', fallback='owl_central_controller')

        # State management
        self.owls_state = {}
        self.desired_state = {}  # {device_id: {'detection_enable': bool, ...}}
        self.lwt_timestamps = {}  # {device_id: time.time()} — when LWT marked device offline
        self.mqtt_connected = False
        self.mqtt_lock = threading.Lock()

        # Allow 7 missed 2s heartbeats before marking offline
        self.offline_timeout = 15.0  # seconds

        # MQTT client
        self.mqtt_client = None

        # GPS Manager (optional)
        self.gps_manager = None
        if self.config.getboolean('GPS', 'enable', fallback=False):
            try:
                from utils.gps_manager import GPSManager
                self.gps_manager = GPSManager(
                    port=self.config.getint('GPS', 'nmea_port', fallback=8500),
                    boom_width=self.config.getfloat('GPS', 'boom_width', fallback=12.0),
                    track_dir=self.config.get('GPS', 'track_save_directory', fallback='tracks')
                )
                logger.info("GPS Manager initialized")
            except Exception as e:
                logger.error(f"Failed to initialize GPS Manager: {e}")

        # Actuation calculator (speed-adaptive relay timing)
        self.speed_averager = SpeedAverager(
            window_seconds=self.config.getfloat('Actuation', 'speed_avg_window', fallback=5.0)
        )
        self.actuation_calculator = ActuationCalculator(
            actuation_length_cm=self.config.getfloat('Actuation', 'actuation_length_cm', fallback=10.0),
            offset_cm=self.config.getfloat('Actuation', 'offset_cm', fallback=30.0),
            fallback_duration=self.config.getfloat('Actuation', 'actuation_duration', fallback=0.15),
            fallback_delay=self.config.getfloat('Actuation', 'delay', fallback=0.0),
        )
        self._actuation_state = {
            'speed_kmh': 0.0,
            'actuation_duration': self.actuation_calculator.fallback_duration,
            'delay': self.actuation_calculator.fallback_delay,
            'source': 'config',
            'gps_status': 'no_gps',
            'coverage_ok': True,
            'min_gap_cm': 0.0,
            'coverage_message': '',
            'actuation_length_cm': self.actuation_calculator.actuation_length_cm,
            'offset_cm': self.actuation_calculator.offset_cm,
        }

        logger.info(f"Central Controller initialized (broker: {self.broker_host}:{self.broker_port})")

    def _load_config(self, config_file):
        """Load configuration from file"""
        config_path = Path(config_file)
        config = configparser.ConfigParser()

        if config_path.exists():
            config.read(config_path)
            logger.info(f"Config loaded from {config_path}")
        else:
            logger.warning(f"Config file not found at {config_path}, using defaults")
            config.add_section('MQTT')
            config.set('MQTT', 'broker_ip', 'localhost')
            config.set('MQTT', 'broker_port', '1883')
            config.set('MQTT', 'client_id', 'owl_central_controller')

        return config

    def setup_mqtt(self):
        """Initialize and start MQTT client.

        If the broker is unreachable, starts a background reconnect thread
        with exponential backoff instead of giving up.
        """
        logger.info(f"Setting up MQTT (ID: {self.client_id})")

        self.mqtt_client = mqtt.Client(client_id=self.client_id)
        self.mqtt_client.on_connect = self._on_connect
        self.mqtt_client.on_disconnect = self._on_disconnect
        self.mqtt_client.on_message = self._on_message

        # Enable paho's built-in auto-reconnect after initial connection is lost
        self.mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)

        if not self._try_mqtt_connect():
            # Start background reconnect instead of giving up
            thread = threading.Thread(target=self._mqtt_background_reconnect, daemon=True)
            thread.start()

    def _try_mqtt_connect(self):
        """Attempt a single MQTT connection. Returns True on success."""
        try:
            logger.info(f"Connecting to MQTT broker at {self.broker_host}:{self.broker_port}")
            self.mqtt_client.connect(self.broker_host, self.broker_port, 60)
            self.mqtt_client.loop_start()
            logger.info("MQTT client loop started")
            return True
        except Exception as e:
            logger.warning(f"Failed to connect to MQTT broker: {e}")
            return False

    def _mqtt_background_reconnect(self):
        """Retry broker connection with exponential backoff (2s -> 60s cap)."""
        delay = 2
        max_delay = 60

        while not self.mqtt_connected:
            logger.info(f"MQTT reconnect: retrying in {delay}s...")
            time.sleep(delay)

            if self._try_mqtt_connect():
                logger.info("MQTT reconnect: connection attempt sent")
                break

            delay = min(delay * 2, max_delay)

    def _on_connect(self, client, userdata, flags, rc):
        """MQTT connection callback"""
        if rc == 0:
            logger.info("Connected to MQTT broker")
            self.mqtt_connected = True

            # Subscribe to all OWL topics
            topics = [
                ("owl/+/state", 0),
                ("owl/+/status", 0),
                ("owl/+/detection", 0),
                ("owl/+/config", 0),
                ("owl/+/system", 0),
            ]
            client.subscribe(topics)
            logger.info(f"Subscribed to {len(topics)} topic patterns")

            logger.info("Clearing potential ghost OWL retained messages...")

        else:
            logger.error(f"Failed to connect to MQTT broker (code: {rc})")
            self.mqtt_connected = False

    def _on_disconnect(self, client, userdata, rc):
        """MQTT disconnection callback"""
        self.mqtt_connected = False
        if rc != 0:
            logger.warning(f"Unexpected MQTT disconnection (code: {rc})")
        else:
            logger.info("Disconnected from MQTT broker")

    def _on_message(self, client, userdata, msg):
        """Process incoming MQTT messages"""
        try:
            topic_parts = msg.topic.split('/')
            if len(topic_parts) < 3:
                return

            device_id = topic_parts[1]
            topic_type = topic_parts[2]

            # Decode payload
            try:
                payload = json.loads(msg.payload.decode())
            except json.JSONDecodeError:
                return

            current_time = time.time()

            # Update state
            is_reconnect = False
            with self.mqtt_lock:
                if device_id not in self.owls_state:
                    self.owls_state[device_id] = {
                        'device_id': device_id,
                        'first_seen': current_time,
                        'last_seen': current_time,
                        'connected': True
                    }
                    is_reconnect = True
                    logger.info(f"New OWL discovered: {device_id}")
                else:
                    was_connected = self.owls_state[device_id].get('connected', False)
                    if not was_connected:
                        is_reconnect = True

                # Merge payload into state
                if topic_type == 'state':
                    self.owls_state[device_id].update(payload)
                elif topic_type == 'status':
                    self.owls_state[device_id]['status'] = payload
                elif topic_type == 'detection':
                    self.owls_state[device_id]['detection'] = payload
                elif topic_type == 'config':
                    self.owls_state[device_id]['config'] = payload
                elif topic_type == 'system':
                    self.owls_state[device_id]['system'] = payload
                else:
                    self.owls_state[device_id][topic_type] = payload

                # LWT handling: if status message says disconnected, mark offline immediately
                if topic_type == 'status':
                    if not payload.get('connected', True) or not payload.get('owl_running', True):
                        self.owls_state[device_id]['connected'] = False
                        self.lwt_timestamps[device_id] = current_time
                        is_reconnect = False  # cancel any reconnect flag from stale heartbeat
                        logger.info(f"{device_id} marked offline via LWT/status message")
                        return

                self.owls_state[device_id]['last_seen'] = current_time
                self.owls_state[device_id]['connected'] = True

            # Push desired state on reconnect (outside lock to avoid deadlock)
            # Skip if device was marked offline by LWT within 5s — that's a
            # stale in-flight heartbeat, not a real reconnect (Pi boot takes 10-30s)
            if is_reconnect:
                lwt_time = self.lwt_timestamps.get(device_id, 0)
                if current_time - lwt_time > 5:
                    self._push_desired_state(device_id)
                else:
                    logger.debug(f"Suppressed reconnect push for {device_id} — LWT grace period")

            logger.debug(f"Updated {device_id} ({topic_type})")

        except Exception as e:
            logger.error(f"Error processing message on {msg.topic}: {e}")

    def check_connections(self):
        """Background thread to check for offline OWLs - runs every 1 second"""
        while True:
            try:
                current_time = time.time()

                with self.mqtt_lock:
                    for device_id, state in list(self.owls_state.items()):
                        last_seen = state.get('last_seen', 0)
                        time_since = current_time - last_seen

                        if time_since > self.offline_timeout:
                            if state.get('connected', False):
                                state['connected'] = False
                                logger.warning(f"{device_id} marked offline ({time_since:.1f}s since last seen)")

                        if time_since > (self.offline_timeout * 4):
                            logger.info(f"Removing stale OWL: {device_id} ({time_since:.1f}s since last seen)")
                            del self.owls_state[device_id]

                self._update_session_state()
                time.sleep(2)

            except Exception as e:
                logger.error(f"Error in connection checker: {e}")
                time.sleep(5)

    def _update_session_state(self):
        """Auto-start/stop GPS session when any OWL detection state changes."""
        if not self.gps_manager:
            return
        any_detecting = any(
            s.get('detection_enable', False)
            for s in self.owls_state.values()
            if s.get('connected', False)
        )
        if any_detecting and not self.gps_manager.session_active:
            self.gps_manager.start_session()
        elif not any_detecting and self.gps_manager.session_active:
            self.gps_manager.stop_session()

    def _push_desired_state(self, device_id):
        """Push stored desired state to a (re)connected OWL device."""
        # Merge 'all' defaults with device-specific overrides
        merged = {}
        merged.update(self.desired_state.get('all', {}))
        merged.update(self.desired_state.get(device_id, {}))

        if not merged:
            return

        topic = f"owl/{device_id}/commands"
        if 'detection_enable' in merged:
            payload = json.dumps({'action': 'set_detection_enable', 'value': merged['detection_enable']})
            self.mqtt_client.publish(topic, payload)
            logger.info(f"Pushed detection_enable={merged['detection_enable']} to {device_id}")

        if 'image_sample_enable' in merged:
            payload = json.dumps({'action': 'set_image_sample_enable', 'value': merged['image_sample_enable']})
            self.mqtt_client.publish(topic, payload)
            logger.info(f"Pushed image_sample_enable={merged['image_sample_enable']} to {device_id}")

        if 'detect_classes' in merged:
            payload = json.dumps({'action': 'set_detect_classes', 'value': merged['detect_classes']})
            self.mqtt_client.publish(topic, payload)
            logger.info(f"Pushed detect_classes={merged['detect_classes']} to {device_id}")

        if 'model' in merged:
            payload = json.dumps({'action': 'set_model', 'value': merged['model']})
            self.mqtt_client.publish(topic, payload)
            logger.info(f"Pushed model={merged['model']} to {device_id}")

        if 'tracking_enabled' in merged:
            payload = json.dumps({'action': 'set_tracking', 'value': merged['tracking_enabled']})
            self.mqtt_client.publish(topic, payload)
            logger.info(f"Pushed tracking_enabled={merged['tracking_enabled']} to {device_id}")

    def _actuation_broadcast_loop(self):
        """Broadcast computed actuation params to all connected OWLs every 1s."""
        while True:
            try:
                # Read speed from GPS manager
                speed_kmh = None
                gps_status = 'no_gps'

                if self.gps_manager:
                    gps_state = self.gps_manager.get_state()
                    fix = gps_state.get('fix', {})
                    conn = gps_state.get('connection', {})

                    if fix.get('speed_kmh') is not None:
                        raw_speed = fix['speed_kmh']
                        avg = self.speed_averager.add_sample(raw_speed)
                        speed_kmh = avg
                        gps_status = 'active'
                    else:
                        # GPS connected but no valid fix
                        ssu = self.speed_averager.seconds_since_update()
                        if ssu is not None and ssu < 30:
                            speed_kmh = self.speed_averager.get_fallback_speed()
                            gps_status = 'stale'
                        elif self.speed_averager.get_fallback_speed() is not None:
                            speed_kmh = self.speed_averager.get_fallback_speed()
                            gps_status = 'stale'

                # Compute actuation params
                result = self.actuation_calculator.compute(speed_kmh)

                # Compute avg loop time across all OWLs
                loop_times = []
                with self.mqtt_lock:
                    for state in self.owls_state.values():
                        lt = state.get('avg_loop_time_ms', 0)
                        if lt > 0:
                            loop_times.append(lt)
                avg_loop_time = sum(loop_times) / len(loop_times) if loop_times else 0.0

                # Check coverage
                coverage = self.actuation_calculator.check_coverage(
                    speed_kmh, avg_loop_time
                )

                # Update state
                self._actuation_state = {
                    'speed_kmh': round(speed_kmh or 0.0, 2),
                    'actuation_duration': result['actuation_duration'],
                    'delay': result['delay'],
                    'source': result['source'],
                    'gps_status': gps_status,
                    'coverage_ok': coverage['coverage_ok'],
                    'min_gap_cm': coverage['min_gap_cm'],
                    'coverage_message': coverage['message'],
                    'actuation_length_cm': self.actuation_calculator.actuation_length_cm,
                    'offset_cm': self.actuation_calculator.offset_cm,
                    'avg_loop_time_ms': round(avg_loop_time, 1),
                }

                # Broadcast to all connected OWLs
                if self.mqtt_client and self.mqtt_connected:
                    payload = json.dumps({
                        'action': 'set_actuation_params',
                        'actuation_duration': result['actuation_duration'],
                        'delay': result['delay'],
                        'source': result['source']
                    })
                    with self.mqtt_lock:
                        for owl_id, state in self.owls_state.items():
                            if state.get('connected', False):
                                topic = f"owl/{owl_id}/commands"
                                self.mqtt_client.publish(topic, payload)

                time.sleep(1.0)

            except Exception as e:
                logger.error(f"Error in actuation broadcast loop: {e}")
                time.sleep(2.0)

    def get_owls(self):
        """Get current state of all OWLs"""
        with self.mqtt_lock:
            owls_copy = json.loads(json.dumps(self.owls_state, default=str))

        # Add computed fields
        for device_id, state in owls_copy.items():
            last_seen = state.get('last_seen', 0)
            state['seconds_since_update'] = time.time() - last_seen
            state['last_seen_formatted'] = datetime.fromtimestamp(last_seen).strftime(
                '%H:%M:%S') if last_seen else 'Never'

        return {
            'owls': owls_copy,
            'mqtt_connected': self.mqtt_connected,
            'controller_time': time.time(),
            'owl_count': len(owls_copy)
        }

    def get_recent_owls(self, ttl=8):
        """Get only recently active OWLs"""
        now = time.time()
        recent = {}

        with self.mqtt_lock:
            for oid, state in list(self.owls_state.items()):
                last_seen = state.get("last_seen", 0)
                time_since = now - last_seen

                if time_since <= ttl:
                    recent[oid] = dict(state)
                else:
                    logger.debug(f"Dropping stale OWL {oid} from get_recent_owls (TTL exceeded)")

        return recent

    def request_device_config(self, device_id, timeout=3.0):
        """Send get_config command and poll for the response in owls_state"""
        if not self.mqtt_connected or not self.mqtt_client:
            return None

        # Clear any stale config data
        with self.mqtt_lock:
            if device_id in self.owls_state:
                self.owls_state[device_id].pop('config', None)

        # Send the get_config command
        topic = f"owl/{device_id}/commands"
        payload = json.dumps({'action': 'get_config'})
        self.mqtt_client.publish(topic, payload)

        # Poll for the config response
        start = time.time()
        while time.time() - start < timeout:
            with self.mqtt_lock:
                state = self.owls_state.get(device_id, {})
                config_data = state.get('config')
                if config_data and isinstance(config_data, dict) and 'config' in config_data:
                    return config_data
            time.sleep(0.1)

        logger.warning(f"Timeout waiting for config from {device_id}")
        return None

    def list_local_presets(self):
        """List INI config files in the config/ directory"""
        config_dir = Path(__file__).parent.parent.parent / 'config'
        presets = []
        if config_dir.exists():
            for ini_file in sorted(config_dir.glob('*.ini')):
                presets.append({
                    'name': ini_file.stem,
                    'filename': ini_file.name,
                    'path': str(ini_file),
                    'is_default': ini_file.name in ('GENERAL_CONFIG.ini', 'CONTROLLER.ini')
                })
        return presets

    def read_preset(self, filename):
        """Read a preset INI file and return as dict"""
        config_dir = Path(__file__).parent.parent.parent / 'config'
        config_path = config_dir / filename

        if not config_path.exists():
            return None

        # Security: only allow files in the config directory
        if not config_path.resolve().parent == config_dir.resolve():
            return None

        config = configparser.ConfigParser()
        config.read(config_path)

        config_dict = {}
        for section in config.sections():
            config_dict[section] = dict(config[section])

        return {
            'config': config_dict,
            'config_name': filename,
            'config_path': str(config_path)
        }

    # -- Tool-compatible interface (used by agent_engine) ----------------

    @property
    def current_state(self):
        """Return first connected OWL's state (tools need a flat dict)."""
        for state in self.owls_state.values():
            if state.get('connected'):
                return state
        return {}

    @property
    def owl_config(self):
        """Return the full config dict from the first connected OWL.

        OWLs publish their config on the 'config' topic, stored as
        owls_state[device_id]['config'].  This is a nested dict with
        section names as keys (e.g. 'GreenOnBrown', 'System').
        Returns ``None`` if no OWL config is available.
        """
        for state in self.owls_state.values():
            if state.get('connected') and state.get('config'):
                return state['config']
        return None

    def _send_command(self, action, **kwargs):
        """Tool-compatible command interface. Broadcasts to all OWLs."""
        if action == 'set_config':
            section = kwargs.get('section', 'GreenOnBrown')
            key = kwargs.get('key')
            val = kwargs.get('value')
            if section and section != 'GreenOnBrown':
                return self.send_command('all', 'set_config_section',
                                         {'section': section, 'params': {key: str(val)}})
            value = {'key': key, 'value': val}
        elif action == 'save_sensitivity_preset':
            return self._broadcast_raw({'action': action, **kwargs})
        elif action == 'delete_sensitivity_preset':
            return self._broadcast_raw({'action': action, **kwargs})
        elif action == 'install_algorithm':
            return self._broadcast_raw({
                'action': action,
                'name': kwargs.get('name'),
                'code': kwargs.get('code'),
                'description': kwargs.get('description', ''),
            })
        else:
            if kwargs and 'value' not in kwargs:
                logger.warning(
                    f"_send_command: action '{action}' has kwargs "
                    f"{list(kwargs.keys())} that may be dropped"
                )
            value = kwargs.get('value') if kwargs else None
        return self.send_command('all', action, value)

    def _broadcast_raw(self, payload):
        """Publish a raw payload to all connected OWLs."""
        if not self.mqtt_connected or not self.mqtt_client:
            return {'success': False, 'error': 'MQTT not connected'}
        sent_to = []
        with self.mqtt_lock:
            for owl_id, state in self.owls_state.items():
                if state.get('connected', False):
                    self.mqtt_client.publish(f"owl/{owl_id}/commands", json.dumps(payload))
                    sent_to.append(owl_id)
        return {'success': True, 'message': f'Sent to {len(sent_to)} OWLs'}

    def set_detection_enable(self, value):
        """Tool-compatible detection toggle."""
        return self.send_command('all', 'toggle_detection', bool(value))

    def set_sensitivity_level(self, level):
        """Tool-compatible sensitivity setter."""
        return self.send_command('all', 'set_sensitivity', level.lower())

    def send_command(self, device_id, action, value=None):
        """Send command to OWL(s)"""
        if not self.mqtt_connected or not self.mqtt_client:
            return {'success': False, 'error': 'MQTT not connected'}

        # Build payload
        payload = {'action': action}

        if action == 'toggle_detection':
            if device_id != 'all':
                current = self.owls_state.get(device_id, {}).get('detection_enable', False)
                new_val = not current
                payload = {'action': 'set_detection_enable', 'value': new_val}
                self.desired_state.setdefault(device_id, {})['detection_enable'] = new_val
            else:
                payload = {'action': 'set_detection_enable', 'value': value}
                self.desired_state.setdefault('all', {})['detection_enable'] = value
                with self.mqtt_lock:
                    for oid in self.owls_state:
                        self.desired_state.setdefault(oid, {})['detection_enable'] = value

        elif action == 'toggle_recording':
            if device_id != 'all':
                current = self.owls_state.get(device_id, {}).get('image_sample_enable', False)
                new_val = not current
                payload = {'action': 'set_image_sample_enable', 'value': new_val}
                self.desired_state.setdefault(device_id, {})['image_sample_enable'] = new_val
            else:
                payload = {'action': 'set_image_sample_enable', 'value': value}
                self.desired_state.setdefault('all', {})['image_sample_enable'] = value
                with self.mqtt_lock:
                    for oid in self.owls_state:
                        self.desired_state.setdefault(oid, {})['image_sample_enable'] = value

        elif action == 'toggle_all_nozzles':
            mode = 2 if value else 1
            payload = {'action': 'set_detection_mode', 'value': mode}
            self.desired_state.setdefault('all', {})['detection_mode'] = mode
            if value:
                # Turning on nozzles disables detection
                self.desired_state.setdefault('all', {})['detection_enable'] = False
                with self.mqtt_lock:
                    for oid in self.owls_state:
                        self.desired_state.setdefault(oid, {})['detection_mode'] = mode
                        self.desired_state.setdefault(oid, {})['detection_enable'] = False
            else:
                with self.mqtt_lock:
                    for oid in self.owls_state:
                        self.desired_state.setdefault(oid, {})['detection_mode'] = mode

        elif action == 'set_detect_classes':
            payload = {'action': 'set_detect_classes', 'value': value}
            # Store so reconnecting OWLs get the class filter
            self.desired_state.setdefault('all', {})['detect_classes'] = value

        elif action == 'set_model':
            payload = {'action': 'set_model', 'value': value}
            self.desired_state.setdefault('all', {})['model'] = value

        elif action == 'set_tracking':
            payload = {'action': 'set_tracking', 'value': value}
            self.desired_state.setdefault('all', {})['tracking_enabled'] = value
            with self.mqtt_lock:
                for oid in self.owls_state:
                    self.desired_state.setdefault(oid, {})['tracking_enabled'] = value

        elif action == 'set_sensitivity':
            payload = {'action': 'set_sensitivity_level', 'level': value}

        elif action == 'set_config':
            payload = {
                'action': 'set_greenonbrown_param',
                'param': value.get('key'),
                'value': value.get('value')
            }

        elif action == 'set_greenongreen_param':
            payload = {
                'action': 'set_config_section',
                'section': 'GreenOnGreen',
                'params': {value.get('key'): str(value.get('value'))}
            }

        elif action == 'set_config_section':
            payload = {
                'action': 'set_config_section',
                'section': value.get('section'),
                'params': value.get('params')
            }

        elif action == 'save_config':
            payload = {
                'action': 'save_config',
                'filename': value.get('filename') if value else None
            }

        elif action == 'set_active_config':
            payload = {
                'action': 'set_active_config',
                'config': value
            }

        elif action == 'restart_service':
            payload = {'action': 'restart_service'}

        elif action == 'shutdown':
            payload = {'action': 'shutdown'}

        else:
            payload = {'action': action, 'value': value}

        # Publish
        if device_id == 'all':
            sent_to = []
            with self.mqtt_lock:
                for owl_id, state in self.owls_state.items():
                    if state.get('connected', False):
                        topic = f"owl/{owl_id}/commands"
                        result = self.mqtt_client.publish(topic, json.dumps(payload))
                        if result.rc == mqtt.MQTT_ERR_SUCCESS:
                            sent_to.append(owl_id)

            logger.info(f"Broadcast '{action}' to {len(sent_to)} OWLs")
            return {'success': True, 'message': f"Sent to {len(sent_to)} OWLs", 'targets': sent_to}
        else:
            topic = f"owl/{device_id}/commands"
            result = self.mqtt_client.publish(topic, json.dumps(payload))

            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.info(f"Sent '{action}' to {device_id}")
                return {'success': True, 'message': f"Sent to {device_id}"}
            else:
                logger.error(f"Failed to publish to {device_id} (code: {result.rc})")
                return {'success': False, 'error': f"Publish failed (code: {result.rc})"}


# Create Flask app
app = Flask(__name__,
    static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static'),
    template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
)

app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB upload limit

# Initialize controller
controller = CentralController()

# Widget system (optional — does not block startup)
widget_manager = None
try:
    from agent.widget_manager import WidgetManager
    widget_manager = WidgetManager(str(Path(__file__).parent.parent.parent / 'agent' / 'widgets'))
except Exception as e:
    logger.warning(f"Widget system unavailable: {e}")

# Agent engine (optional — does not block startup)
agent_engine = None
try:
    from agent import ToolRegistry, AgentEngine
    _registry = ToolRegistry(developer_mode=False)
    _registry.discover()
    _sessions_dir = str(Path(__file__).parent.parent.parent / 'agent' / 'sessions')
    agent_engine = AgentEngine(
        tool_registry=_registry,
        context={'mqtt_client': controller, 'config': controller.config, 'widget_manager': widget_manager},
        sessions_dir=_sessions_dir,
    )
except Exception as e:
    logger.warning(f"Agent engine unavailable: {e}")

# ---------------------------------------------------------------------------
# Access control — localhost (kiosk) gets everything, everyone else gets /demo
# only. Nginx sets X-Real-IP so we see the real client, not 127.0.0.1.
# Disable with DASHBOARD_OPEN=1 env var if you need laptop access.
# ---------------------------------------------------------------------------
DASHBOARD_OPEN = os.environ.get('DASHBOARD_OPEN', '').strip() in ('1', 'true', 'yes')

DEMO_PUBLIC_PREFIXES = (
    '/demo', '/models', '/api/models',
    '/api/owls', '/api/actuation', '/api/snapshot/',
    '/shared/', '/static/',
)

@app.before_request
def demo_access_guard():
    if DASHBOARD_OPEN:
        return None
    path = request.path
    if any(path.startswith(p) for p in DEMO_PUBLIC_PREFIXES):
        return None
    # X-Real-IP from nginx; falls back to remote_addr for direct connections
    client_ip = request.headers.get('X-Real-IP', request.remote_addr)
    if client_ip in ('127.0.0.1', '::1'):
        return None
    return Response('Restricted — visit /demo', status=403, content_type='text/plain')


@app.route('/shared/<path:filename>')
def shared_static(filename):
    return send_from_directory(SHARED_DIR, filename)

# Flask routes
@app.route('/')
def index():
    return render_template('index.html')


@app.route("/api/owls")
def api_owls():
    return jsonify({
        "mqtt_connected": controller.mqtt_connected,
        "owls": controller.get_recent_owls(ttl=8),  # Reduced TTL
    })


@app.route('/api/health')
def health_check():
    return jsonify({
        'status': 'healthy',
        'mqtt_connected': controller.mqtt_connected,
        'owl_count': len(controller.owls_state),
        'uptime': time.time()
    })


@app.route('/api/command', methods=['POST'])
def send_command():
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data'}), 400

        device_id = data.get('device_id')
        action = data.get('action')
        value = data.get('value')

        if not device_id or not action:
            return jsonify({'success': False, 'error': 'Missing device_id or action'}), 400

        result = controller.send_command(device_id, action, value)

        if result['success']:
            return jsonify(result)
        else:
            return jsonify(result), 500

    except Exception as e:
        logger.error(f"Error sending command: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mqtt/status')
def mqtt_status():
    return jsonify({
        'connected': controller.mqtt_connected,
        'client_id': controller.client_id,
        'broker': f"{controller.broker_host}:{controller.broker_port}"
    })


@app.route('/api/greenonbrown/defaults')
def get_greenonbrown_defaults():
    defaults = {
        'exg_min': {'value': 25, 'min': 0, 'max': 255, 'step': 1, 'label': 'ExG Min'},
        'exg_max': {'value': 200, 'min': 0, 'max': 255, 'step': 1, 'label': 'ExG Max'},
        'hue_min': {'value': 39, 'min': 0, 'max': 179, 'step': 1, 'label': 'Hue Min'},
        'hue_max': {'value': 83, 'min': 0, 'max': 179, 'step': 1, 'label': 'Hue Max'},
        'saturation_min': {'value': 50, 'min': 0, 'max': 255, 'step': 1, 'label': 'Saturation Min'},
        'saturation_max': {'value': 220, 'min': 0, 'max': 255, 'step': 1, 'label': 'Saturation Max'},
        'brightness_min': {'value': 60, 'min': 0, 'max': 255, 'step': 1, 'label': 'Brightness Min'},
        'brightness_max': {'value': 190, 'min': 0, 'max': 255, 'step': 1, 'label': 'Brightness Max'},
        'min_detection_area': {'value': 10, 'min': 1, 'max': 1000, 'step': 1, 'label': 'Min Detection Area'}
    }
    return jsonify(defaults)


@app.route('/api/config', methods=['GET'])
def get_config_generic():
    """Return config from first connected OWL (widget API)."""
    cfg = controller.owl_config
    if cfg is None:
        # Trigger a config request from first connected OWL
        for device_id, state in controller.owls_state.items():
            if state.get('connected'):
                controller.send_command(device_id, 'get_config')
                break
        return jsonify({'success': False, 'error': 'Config not yet available'}), 503
    config_data = cfg.get('config', cfg) if isinstance(cfg, dict) else cfg
    return jsonify({'success': True, 'config': config_data})


@app.route('/api/config/<device_id>', methods=['GET'])
def get_device_config(device_id):
    """Fetch full config from a device via MQTT"""
    try:
        config_data = controller.request_device_config(device_id, timeout=3.0)
        if config_data:
            return jsonify({'success': True, **config_data})
        else:
            return jsonify({'success': False, 'error': f'Timeout waiting for config from {device_id}'}), 504
    except Exception as e:
        logger.error(f"Error getting config for {device_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/config/<device_id>', methods=['POST'])
def push_device_config(device_id):
    """Push section changes to a device"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data'}), 400

        section = data.get('section')
        params = data.get('params', {})

        if not section or not params:
            return jsonify({'success': False, 'error': 'Missing section or params'}), 400

        result = controller.send_command(device_id, 'set_config_section', {
            'section': section,
            'params': params
        })
        return jsonify(result)

    except Exception as e:
        logger.error(f"Error pushing config to {device_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/config/<device_id>/save', methods=['POST'])
def save_device_config(device_id):
    """Tell a device to save its config to disk"""
    try:
        data = request.json or {}
        filename = data.get('filename')
        result = controller.send_command(device_id, 'save_config', {'filename': filename})
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error saving config on {device_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/presets')
def list_presets():
    """List local preset INI files"""
    presets = controller.list_local_presets()
    return jsonify({'success': True, 'presets': presets})


@app.route('/api/presets/<name>')
def get_preset(name):
    """Read a preset as JSON"""
    # Ensure filename ends with .ini
    if not name.endswith('.ini'):
        name = name + '.ini'

    data = controller.read_preset(name)
    if data:
        return jsonify({'success': True, **data})
    else:
        return jsonify({'success': False, 'error': f'Preset not found: {name}'}), 404


@app.route('/api/presets/push/<device_id>', methods=['POST'])
def push_preset_to_device(device_id):
    """Push a preset config to a device section by section"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data'}), 400

        preset_name = data.get('preset')
        if not preset_name:
            return jsonify({'success': False, 'error': 'Missing preset name'}), 400

        if not preset_name.endswith('.ini'):
            preset_name = preset_name + '.ini'

        preset_data = controller.read_preset(preset_name)
        if not preset_data:
            return jsonify({'success': False, 'error': f'Preset not found: {preset_name}'}), 404

        # Push each section to the device
        sections_pushed = 0
        for section, params in preset_data['config'].items():
            controller.send_command(device_id, 'set_config_section', {
                'section': section,
                'params': params
            })
            sections_pushed += 1

        return jsonify({
            'success': True,
            'message': f'Pushed {sections_pushed} sections from {preset_name} to {device_id}'
        })

    except Exception as e:
        logger.error(f"Error pushing preset to {device_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sensitivity/presets', methods=['GET'])
def get_sensitivity_presets():
    """Get sensitivity presets from connected OWL state."""
    try:
        if not controller:
            return jsonify({'success': False, 'error': 'Controller not initialized'}), 500

        # Get presets from the first connected OWL's state
        presets = []
        active = 'medium'
        for device_id, state in controller.owls_state.items():
            presets = state.get('sensitivity_presets', [])
            active = state.get('sensitivity_level', 'medium')
            break  # Use first OWL

        return jsonify({
            'success': True,
            'presets': presets,
            'active': active
        })
    except Exception as e:
        logger.error(f"Error getting sensitivity presets: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/config/library', methods=['GET'])
def list_config_library():
    """List all config files (presets + custom saved) — mirrors standalone /api/config/list"""
    try:
        config_dir = Path(__file__).parent.parent.parent / 'config'
        configs = []
        protected = ['GENERAL_CONFIG.ini', 'CONTROLLER.ini']

        if config_dir.exists():
            for f in sorted(config_dir.glob('*.ini')):
                stat = f.stat()
                configs.append({
                    'name': f.name,
                    'path': f'config/{f.name}',
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'is_default': f.name in protected
                })

        # Sort: defaults first, then by modified descending
        configs.sort(key=lambda x: (not x['is_default'], x['name']))

        return jsonify({'success': True, 'configs': configs})
    except Exception as e:
        logger.error(f"Error listing config library: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/config/library', methods=['POST'])
def save_to_config_library():
    """Save config to controller's local filesystem — mirrors standalone POST /api/config"""
    try:
        data = request.json
        if not data or 'config' not in data:
            return jsonify({'success': False, 'error': 'No config data provided'}), 400

        # Generate filename with timestamp (same as standalone)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        suggested_name = data.get('filename', f'config_{timestamp}.ini')

        if not suggested_name.endswith('.ini'):
            suggested_name += '.ini'

        # Sanitize filename (same as standalone)
        safe_name = "".join(c for c in suggested_name if c.isalnum() or c in ('_', '-', '.')).strip()
        if not safe_name:
            safe_name = f'config_{timestamp}.ini'

        config_dir = Path(__file__).parent.parent.parent / 'config'
        new_config_path = config_dir / safe_name

        # Don't allow overwriting protected configs (same as standalone)
        protected = ['GENERAL_CONFIG.ini', 'CONTROLLER.ini']
        if safe_name in protected:
            return jsonify({
                'success': False,
                'error': f'Cannot overwrite default config "{safe_name}". Choose a different name.'
            }), 400

        # Build and write config (same as standalone)
        config = configparser.ConfigParser()
        config.optionxform = str

        for section, options in data['config'].items():
            if not config.has_section(section):
                config.add_section(section)
            for key, value in options.items():
                config.set(section, key, str(value))

        with open(new_config_path, 'w') as f:
            config.write(f)

        logger.info(f"Config saved to library: {new_config_path}")

        return jsonify({
            'success': True,
            'message': f'Saved as {safe_name}',
            'filename': safe_name,
            'relative_path': f'config/{safe_name}'
        })

    except Exception as e:
        logger.error(f"Error saving to config library: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/config/library/<name>', methods=['DELETE'])
def delete_from_config_library(name):
    """Delete a custom config from library — mirrors standalone /api/config/delete"""
    try:
        protected = ['GENERAL_CONFIG.ini', 'CONTROLLER.ini']
        if name in protected:
            return jsonify({'success': False, 'error': 'Cannot delete default configs'}), 400

        config_dir = Path(__file__).parent.parent.parent / 'config'
        config_path = config_dir / name

        # Security: only allow files in config directory
        if not config_path.resolve().parent == config_dir.resolve():
            return jsonify({'success': False, 'error': 'Invalid path'}), 400

        if not config_path.exists():
            return jsonify({'success': False, 'error': 'Config not found'}), 404

        config_path.unlink()
        logger.info(f"Deleted config from library: {name}")

        return jsonify({'success': True, 'message': f'Deleted {name}'})
    except Exception as e:
        logger.error(f"Error deleting config: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/config/<device_id>/set-active', methods=['POST'])
def set_device_active_config(device_id):
    """Tell an OWL to set its active_config.txt — uses existing set_active_config MQTT handler"""
    try:
        data = request.json or {}
        config_path = data.get('config')
        if not config_path:
            return jsonify({'success': False, 'error': 'No config path specified'}), 400

        result = controller.send_command(device_id, 'set_active_config', config_path)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error setting active config on {device_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/owl/<device_id>/restart', methods=['POST'])
def restart_owl(device_id):
    """Send restart_service command to an OWL"""
    try:
        result = controller.send_command(device_id, 'restart_service')
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error restarting {device_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def _resolve_owl_host(device_id):
    """Get the best host for an OWL: static IP from MQTT state, fallback to .local mDNS.

    Rejects localhost/loopback — those mean the OWL hasn't configured its
    real static_ip in CONTROLLER.ini yet.
    """
    _LOOPBACK = {'localhost', '127.0.0.1', '::1', ''}
    with controller.mqtt_lock:
        state = controller.owls_state.get(device_id, {})
    ip = (state.get('static_ip') or '').strip()
    if ip and ip not in _LOOPBACK:
        return ip
    return f"{device_id}.local"


@app.route('/api/snapshot/<device_id>')
def snapshot_proxy(device_id):
    """Proxy single JPEG frame from OWL device (95% quality)."""
    device_id = device_id.replace('_', '-')
    host = _resolve_owl_host(device_id)
    snapshot_url = f"https://{host}/latest_frame.jpg"
    try:
        r = requests.get(snapshot_url, timeout=5, verify=False)
        if r.status_code != 200:
            logger.error(f"Failed to get snapshot from {device_id} at {host} (Status: {r.status_code})")
            return f"Error: Could not get snapshot from {device_id}.", 502
        return Response(r.content, mimetype='image/jpeg')
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error getting snapshot from {device_id} at {host}: {e}")
        return f"Error: {device_id} is offline or unreachable.", 502
    except Exception as e:
        logger.error(f"Error getting snapshot from {device_id}: {e}")
        return f"Error: An unknown error occurred.", 500


@app.route('/api/video_feed/<device_id>')
def video_feed_proxy(device_id):
    """Proxies the MJPEG stream from an OWL, ignoring SSL errors."""

    device_id = device_id.replace('_', '-')
    host = _resolve_owl_host(device_id)
    video_url = f"https://{host}/video_feed"
    logger.info(f"Proxying video feed for {device_id} from {video_url}")

    try:
        r = requests.get(video_url, stream=True, verify=False, timeout=5)

        if r.status_code != 200:
            logger.error(f"Failed to get stream from {video_url} (Status: {r.status_code})")
            return f"Error: Could not connect to {device_id}.", 502

        ct = r.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=frame")

        def generate():
            try:
                for chunk in r.iter_content(chunk_size=1024):
                    yield chunk
            finally:
                r.close()

        return Response(
            stream_with_context(generate()),
            content_type=ct,
            status=r.status_code,
            direct_passthrough=True,
        )

    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error proxying {video_url}: {e}")
        return f"Error: {device_id} is offline or unreachable.", 502
    except Exception as e:
        logger.error(f"Generic error proxying {video_url}: {e}")
        return "Error: An unknown error occurred.", 500


# ---------------------------------------------------------------------------
# Actuation API routes
# ---------------------------------------------------------------------------

@app.route('/api/actuation')
def api_actuation():
    """Current actuation state: speed, computed params, coverage."""
    return jsonify(controller._actuation_state)


@app.route('/api/actuation/config', methods=['POST'])
def api_actuation_config():
    """Update actuation geometry (length_cm, offset_cm) at runtime."""
    data = request.json or {}
    calc = controller.actuation_calculator

    if 'actuation_length_cm' in data:
        try:
            val = float(data['actuation_length_cm'])
            calc.actuation_length_cm = max(2.0, min(50.0, val))
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Invalid actuation_length_cm'}), 400

    if 'offset_cm' in data:
        try:
            val = float(data['offset_cm'])
            calc.offset_cm = max(0.0, min(100.0, val))
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Invalid offset_cm'}), 400

    return jsonify({
        'success': True,
        'actuation_length_cm': calc.actuation_length_cm,
        'offset_cm': calc.offset_cm
    })


# ---------------------------------------------------------------------------
# Model Management routes
# ---------------------------------------------------------------------------

@app.route('/models')
def models_page():
    return render_template('models.html')


@app.route('/demo')
def demo_page():
    return render_template('demo.html')


@app.route('/api/models')
def list_models():
    """List models in the library."""
    try:
        models = []
        if not UPLOADS_DIR.exists():
            return jsonify({'models': []})

        for item in sorted(UPLOADS_DIR.iterdir()):
            if item.name in PROTECTED_MODEL_FILES or item.name.startswith('.'):
                continue
            if item.name.endswith('.sha256'):
                continue

            entry = None
            if item.suffix == '.pt' and item.is_file():
                entry = {
                    'name': item.name,
                    'type': 'pytorch',
                    'size': item.stat().st_size,
                    'modified': datetime.fromtimestamp(item.stat().st_mtime).isoformat(),
                }
            elif item.is_dir():
                # Check for NCNN dir (has .param file)
                has_param = any(f.suffix == '.param' for f in item.iterdir() if f.is_file())
                if has_param:
                    dir_size = sum(f.stat().st_size for f in item.rglob('*') if f.is_file())
                    entry = {
                        'name': item.name,
                        'type': 'ncnn',
                        'size': dir_size,
                        'modified': datetime.fromtimestamp(item.stat().st_mtime).isoformat(),
                    }

            if entry:
                # Cross-reference deployment status from OWL states
                deployed_to = []
                with controller.mqtt_lock:
                    for owl_id, state in controller.owls_state.items():
                        owl_models = state.get('available_models', [])
                        if entry['name'] in owl_models:
                            deployed_to.append(owl_id)
                entry['deployed_to'] = deployed_to
                models.append(entry)

        return jsonify({'models': models})
    except Exception as e:
        logger.error(f"Error listing models: {e}")
        return jsonify({'models': [], 'error': str(e)}), 500


@app.route('/api/models/upload', methods=['POST'])
def upload_model():
    """Receive a .pt or .zip model file."""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400

        f = request.files['file']
        if not f.filename:
            return jsonify({'success': False, 'error': 'No file selected'}), 400

        filename = secure_filename(f.filename)
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

        if ext not in ALLOWED_MODEL_EXTENSIONS:
            return jsonify({'success': False, 'error': f'Invalid file type .{ext}. Allowed: .pt, .zip'}), 400

        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

        # Check disk space
        disk = shutil.disk_usage(UPLOADS_DIR)
        if disk.free < 500 * 1024 * 1024:
            return jsonify({'success': False, 'error': 'Insufficient disk space (<500MB free)'}), 507
        save_path = UPLOADS_DIR / filename

        # Path traversal check
        if not save_path.resolve().parent == UPLOADS_DIR.resolve():
            return jsonify({'success': False, 'error': 'Invalid filename'}), 400

        f.save(str(save_path))

        result_name = filename
        result_type = 'pytorch'

        if ext == 'zip':
            # Validate NCNN zip
            valid, msg = _validate_ncnn_zip(str(save_path))
            if not valid:
                save_path.unlink()
                return jsonify({'success': False, 'error': msg}), 400

            # Extract to directory named after zip (without .zip)
            dir_name = filename.rsplit('.', 1)[0]
            extract_dir = UPLOADS_DIR / dir_name
            if extract_dir.exists():
                shutil.rmtree(extract_dir)
            extract_dir.mkdir()

            with zipfile.ZipFile(str(save_path), 'r') as zf:
                # Extract files, flattening if all in a single subdirectory
                members = zf.namelist()
                # Check if all members share a common directory prefix
                top_dirs = set()
                for m in members:
                    parts = m.split('/')
                    if len(parts) > 1:
                        top_dirs.add(parts[0])
                    else:
                        top_dirs.add('')

                if len(top_dirs) == 1 and '' not in top_dirs:
                    # All files in a single subdir — flatten
                    prefix = top_dirs.pop() + '/'
                    for member in zf.infolist():
                        if member.is_dir():
                            continue
                        member_name = member.filename[len(prefix):]
                        if not member_name:
                            continue
                        target = extract_dir / member_name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, open(target, 'wb') as dst:
                            shutil.copyfileobj(src, dst)
                else:
                    zf.extractall(str(extract_dir))

            # Remove the zip after extraction
            save_path.unlink()
            result_name = dir_name
            result_type = 'ncnn'

            # Compute SHA256 of the whole directory (hash all files sorted)
            h = hashlib.sha256()
            for fp in sorted(extract_dir.rglob('*')):
                if fp.is_file():
                    with open(fp, 'rb') as hf:
                        while True:
                            chunk = hf.read(65536)
                            if not chunk:
                                break
                            h.update(chunk)
            sha256 = h.hexdigest()
            sha_path = UPLOADS_DIR / f'{dir_name}.sha256'
            sha_path.write_text(sha256)
        else:
            # .pt file — compute SHA256
            sha256 = _compute_sha256(str(save_path))
            sha_path = UPLOADS_DIR / f'{filename}.sha256'
            sha_path.write_text(sha256)

        size = save_path.stat().st_size if save_path.exists() else sum(
            fp.stat().st_size for fp in (UPLOADS_DIR / result_name).rglob('*') if fp.is_file()
        )

        logger.info(f"Model uploaded: {result_name} ({result_type}, {size} bytes, sha256={sha256[:12]}...)")

        return jsonify({
            'success': True,
            'filename': result_name,
            'type': result_type,
            'size': size,
            'sha256': sha256,
        })

    except Exception as e:
        logger.error(f"Error uploading model: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/models/download/<name>')
def download_model(name):
    """Serve a model file to an OWL unit for download."""
    try:
        # Security: prevent path traversal
        safe_name = secure_filename(name)
        if not safe_name:
            return jsonify({'error': 'Invalid name'}), 400

        target = UPLOADS_DIR / safe_name
        if not target.resolve().parent == UPLOADS_DIR.resolve():
            return jsonify({'error': 'Invalid path'}), 400

        if target.is_file() and target.suffix == '.pt':
            return send_from_directory(str(UPLOADS_DIR), safe_name)

        elif target.is_dir():
            # Zip NCNN directory on-the-fly
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                for fp in sorted(target.rglob('*')):
                    if fp.is_file():
                        zf.write(fp, fp.relative_to(target))
            buf.seek(0)
            return send_file(buf, mimetype='application/zip',
                             as_attachment=True, download_name=f'{safe_name}.zip')

        else:
            return jsonify({'error': 'Model not found'}), 404

    except Exception as e:
        logger.error(f"Error downloading model {name}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/models/deploy', methods=['POST'])
def deploy_model():
    """Send download_model commands to selected OWLs via MQTT."""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data'}), 400

        model_name = data.get('model_name')
        device_ids = data.get('device_ids', [])

        if not model_name or not device_ids:
            return jsonify({'success': False, 'error': 'Missing model_name or device_ids'}), 400

        safe_name = secure_filename(model_name)
        target = UPLOADS_DIR / safe_name

        if not (target.is_file() or target.is_dir()):
            return jsonify({'success': False, 'error': f'Model not found: {model_name}'}), 404

        # Read SHA256 from sidecar
        sha_path = UPLOADS_DIR / f'{safe_name}.sha256'
        sha256 = sha_path.read_text().strip() if sha_path.exists() else ''

        # Build download URL
        static_ip = controller.config.get('Network', 'static_ip', fallback='localhost')
        is_archive = target.is_dir()
        download_url = f'https://{static_ip}/api/models/download/{safe_name}'

        # Determine the filename OWL should save as
        if is_archive:
            dl_filename = f'{safe_name}.zip'
        else:
            dl_filename = safe_name

        if not controller.mqtt_connected or not controller.mqtt_client:
            return jsonify({'success': False, 'error': 'MQTT not connected'}), 503

        sent_to = []
        for device_id in device_ids:
            topic = f'owl/{device_id}/commands'
            payload = json.dumps({
                'action': 'download_model',
                'url': download_url,
                'filename': dl_filename,
                'sha256': sha256,
                'is_archive': is_archive,
            })
            result = controller.mqtt_client.publish(topic, payload)
            if result.rc == 0:
                sent_to.append(device_id)

        logger.info(f"Deploy {model_name} sent to {len(sent_to)} OWLs: {sent_to}")

        return jsonify({
            'success': True,
            'message': f'Deploy command sent to {len(sent_to)} OWLs',
            'sent_to': sent_to,
        })

    except Exception as e:
        logger.error(f"Error deploying model: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/models/<name>', methods=['DELETE'])
def delete_model(name):
    """Delete a model from the library."""
    try:
        safe_name = secure_filename(name)
        if not safe_name:
            return jsonify({'success': False, 'error': 'Invalid name'}), 400

        if safe_name in PROTECTED_MODEL_FILES:
            return jsonify({'success': False, 'error': 'Cannot delete protected file'}), 400

        target = UPLOADS_DIR / safe_name
        if not target.resolve().parent == UPLOADS_DIR.resolve():
            return jsonify({'success': False, 'error': 'Invalid path'}), 400

        if target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            return jsonify({'success': False, 'error': 'Model not found'}), 404

        # Remove SHA256 sidecar
        sha_path = UPLOADS_DIR / f'{safe_name}.sha256'
        if sha_path.exists():
            sha_path.unlink()

        logger.info(f"Model deleted: {safe_name}")
        return jsonify({'success': True, 'message': f'Deleted {safe_name}'})

    except Exception as e:
        logger.error(f"Error deleting model: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Data Downloads routes
# ---------------------------------------------------------------------------


def _get_downloads_storage():
    """Calculate downloads directory storage usage."""
    used = 0
    if DOWNLOADS_DIR.exists():
        for f in DOWNLOADS_DIR.iterdir():
            if f.is_file():
                used += f.stat().st_size
    used_mb = round(used / (1024 * 1024), 1)
    return {
        'used_mb': used_mb,
        'max_mb': MAX_DOWNLOADS_SIZE_MB,
        'free_mb': round(MAX_DOWNLOADS_SIZE_MB - used_mb, 1),
        'percent': round(used_mb / MAX_DOWNLOADS_SIZE_MB * 100, 1) if MAX_DOWNLOADS_SIZE_MB > 0 else 0,
    }


@app.route('/downloads')
def downloads_page():
    return render_template('downloads.html')


@app.route('/api/downloads/sessions/<device_id>')
def api_downloads_sessions(device_id):
    """Return data_sessions from OWL state and trigger a scan."""
    try:
        # Send scan command via MQTT
        if controller.mqtt_connected and controller.mqtt_client:
            topic = f'owl/{device_id}/commands'
            payload = json.dumps({'action': 'list_data_sessions'})
            controller.mqtt_client.publish(topic, payload)

        # Return currently known sessions from state
        with controller.mqtt_lock:
            owl_state = controller.owls_state.get(device_id, {})
            sessions = owl_state.get('data_sessions', [])
            transfer = owl_state.get('data_transfer', {})

        return jsonify({
            'sessions': sessions,
            'transfer': transfer,
            'device_id': device_id,
        })
    except Exception as e:
        logger.error(f"Error fetching sessions for {device_id}: {e}")
        return jsonify({'sessions': [], 'error': str(e)}), 500


@app.route('/api/downloads/request', methods=['POST'])
def api_downloads_request():
    """Send transfer_session MQTT command to an OWL."""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data'}), 400

        device_id = data.get('device_id')
        # session_id (YYYYMMDD/session_HHMMSS) preferred; session_date (YYYYMMDD) for legacy
        session_id = data.get('session_id') or data.get('session_date')
        data_types = data.get('data_types', ['images'])

        if not device_id or not session_id:
            return jsonify({'success': False, 'error': 'Missing device_id or session identifier'}), 400

        if not controller.mqtt_connected or not controller.mqtt_client:
            return jsonify({'success': False, 'error': 'MQTT not connected'}), 503

        # Pre-check quota: estimate from session data
        with controller.mqtt_lock:
            owl_state = controller.owls_state.get(device_id, {})
            sessions = owl_state.get('data_sessions', [])

        # Match by session_id first, fall back to date match
        session_info = next((s for s in sessions if s.get('session_id') == session_id), None)
        if not session_info:
            session_info = next((s for s in sessions if s.get('date') == session_id), None)
        if session_info:
            session_size_mb = session_info.get('total_size', 0) / (1024 * 1024)
            storage = _get_downloads_storage()
            if session_size_mb > storage['free_mb']:
                return jsonify({
                    'success': False,
                    'error': f"Not enough space on controller. Session is ~{session_size_mb:.0f}MB "
                             f"but only {storage['free_mb']:.0f}MB available. "
                             f"Download and remove existing files first."
                }), 507

        # Build upload URL for OWL to POST back to
        static_ip = controller.config.get('Network', 'static_ip', fallback='localhost')
        upload_url = f'https://{static_ip}/api/downloads/receive'

        topic = f'owl/{device_id}/commands'
        payload = json.dumps({
            'action': 'transfer_session',
            'session_id': session_id,
            'data_types': data_types,
            'upload_url': upload_url,
        })
        result = controller.mqtt_client.publish(topic, payload)

        if result.rc == 0:
            return jsonify({'success': True, 'message': f'Transfer command sent to {device_id}'})
        else:
            return jsonify({'success': False, 'error': 'MQTT publish failed'}), 500

    except Exception as e:
        logger.error(f"Error requesting transfer: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/downloads/receive', methods=['POST'])
def api_downloads_receive():
    """Receive ZIP data from an OWL unit."""
    try:
        device_id = request.headers.get('X-OWL-Device-ID', 'unknown')
        session_date = request.headers.get('X-OWL-Session-Date', 'unknown')

        safe_device = secure_filename(device_id) or 'unknown'
        safe_date = secure_filename(session_date) or 'unknown'
        filename = f'{safe_device}_{safe_date}.zip'

        # Check disk space
        disk = shutil.disk_usage(DOWNLOADS_DIR)
        if disk.free < 500 * 1024 * 1024:
            return jsonify({'success': False, 'error': 'Insufficient disk space (<500MB free)'}), 507

        # Check quota using Content-Length header (before reading body)
        incoming_size = request.content_length or 0
        storage = _get_downloads_storage()
        incoming_mb = incoming_size / (1024 * 1024)
        if incoming_size > 0 and storage['used_mb'] + incoming_mb > MAX_DOWNLOADS_SIZE_MB:
            return jsonify({
                'success': False,
                'error': f"Downloads storage full ({storage['used_mb']:.0f}MB / {MAX_DOWNLOADS_SIZE_MB}MB). "
                         f"Delete existing files before transferring more."
            }), 507

        DOWNLOADS_DIR.mkdir(exist_ok=True)
        save_path = DOWNLOADS_DIR / filename

        # Path traversal check
        if save_path.resolve().parent != DOWNLOADS_DIR.resolve():
            return jsonify({'success': False, 'error': 'Invalid filename'}), 400

        # Stream to disk — never load entire ZIP into memory
        written = 0
        with open(save_path, 'wb') as f:
            while True:
                chunk = request.stream.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)

        # Post-write quota check (if Content-Length was missing/wrong)
        actual_mb = written / (1024 * 1024)
        if storage['used_mb'] + actual_mb > MAX_DOWNLOADS_SIZE_MB:
            save_path.unlink(missing_ok=True)
            return jsonify({
                'success': False,
                'error': f"Downloads storage full ({storage['used_mb']:.0f}MB / {MAX_DOWNLOADS_SIZE_MB}MB). "
                         f"Delete existing files before transferring more."
            }), 507

        logger.info(f"Received download: {filename} ({written} bytes)")
        return jsonify({'success': True, 'filename': filename, 'size': written})

    except Exception as e:
        logger.error(f"Error receiving download: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/downloads/status/<device_id>')
def api_downloads_status(device_id):
    """Return data_transfer progress from OWL state."""
    with controller.mqtt_lock:
        owl_state = controller.owls_state.get(device_id, {})
        transfer = owl_state.get('data_transfer', {})
    return jsonify(transfer)


@app.route('/api/downloads/files')
def api_downloads_files():
    """List ZIP files in the downloads staging directory."""
    try:
        files = []
        if DOWNLOADS_DIR.exists():
            for f in sorted(DOWNLOADS_DIR.iterdir()):
                if f.is_file() and f.suffix == '.zip':
                    stat = f.stat()
                    files.append({
                        'filename': f.name,
                        'size': stat.st_size,
                        'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    })

        return jsonify({
            'files': files,
            'storage': _get_downloads_storage(),
        })
    except Exception as e:
        logger.error(f"Error listing downloads: {e}")
        return jsonify({'files': [], 'error': str(e)}), 500


@app.route('/api/downloads/file/<filename>')
def api_downloads_file(filename):
    """Serve a ZIP file to the browser for download."""
    try:
        safe_name = secure_filename(filename)
        if not safe_name:
            return jsonify({'error': 'Invalid filename'}), 400

        target = DOWNLOADS_DIR / safe_name
        if target.resolve().parent != DOWNLOADS_DIR.resolve():
            return jsonify({'error': 'Invalid path'}), 400

        if not target.is_file():
            return jsonify({'error': 'File not found'}), 404

        return send_from_directory(str(DOWNLOADS_DIR), safe_name, as_attachment=True)

    except Exception as e:
        logger.error(f"Error serving download {filename}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/downloads/file/<filename>', methods=['DELETE'])
def api_downloads_delete_file(filename):
    """Delete a ZIP from the downloads staging directory."""
    try:
        safe_name = secure_filename(filename)
        if not safe_name:
            return jsonify({'success': False, 'error': 'Invalid filename'}), 400

        target = DOWNLOADS_DIR / safe_name
        if target.resolve().parent != DOWNLOADS_DIR.resolve():
            return jsonify({'success': False, 'error': 'Invalid path'}), 400

        if not target.is_file():
            return jsonify({'success': False, 'error': 'File not found'}), 404

        target.unlink()
        logger.info(f"Download deleted: {safe_name}")
        return jsonify({'success': True, 'message': f'Deleted {safe_name}'})

    except Exception as e:
        logger.error(f"Error deleting download: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/downloads/delete-remote', methods=['POST'])
def api_downloads_delete_remote():
    """Send delete_session MQTT command to an OWL."""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data'}), 400

        device_id = data.get('device_id')
        session_id = data.get('session_id') or data.get('session_date')
        data_types = data.get('data_types', ['images'])

        if not device_id or not session_id:
            return jsonify({'success': False, 'error': 'Missing device_id or session identifier'}), 400

        if not controller.mqtt_connected or not controller.mqtt_client:
            return jsonify({'success': False, 'error': 'MQTT not connected'}), 503

        topic = f'owl/{device_id}/commands'
        payload = json.dumps({
            'action': 'delete_session',
            'session_id': session_id,
            'data_types': data_types,
        })
        result = controller.mqtt_client.publish(topic, payload)

        if result.rc == 0:
            return jsonify({'success': True, 'message': f'Delete command sent to {device_id}'})
        else:
            return jsonify({'success': False, 'error': 'MQTT publish failed'}), 500

    except Exception as e:
        logger.error(f"Error sending delete command: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# GPS API routes
# ---------------------------------------------------------------------------

@app.route('/api/gps')
def api_gps():
    """Current GPS fix, connection status, and session stats."""
    if not controller.gps_manager:
        return jsonify({'connection': {'gps_enabled': False}}), 200
    return jsonify(controller.gps_manager.get_state())


@app.route('/api/gps/tracks')
def api_gps_tracks():
    """List saved GeoJSON track files."""
    if not controller.gps_manager:
        return jsonify({'tracks': []}), 200

    track_dir = controller.gps_manager.track_dir
    tracks = []
    if os.path.isdir(track_dir):
        for f in sorted(os.listdir(track_dir), reverse=True):
            if f.endswith('.geojson'):
                filepath = os.path.join(track_dir, f)
                tracks.append({
                    'filename': f,
                    'size_bytes': os.path.getsize(filepath),
                    'modified': os.path.getmtime(filepath),
                })
    return jsonify({'tracks': tracks})


@app.route('/api/gps/tracks/<filename>')
def api_gps_track_download(filename):
    """Download a saved GeoJSON track file."""
    if not controller.gps_manager:
        return jsonify({'error': 'GPS not enabled'}), 404

    track_dir = os.path.abspath(controller.gps_manager.track_dir)

    # Security: only serve files from the track directory
    filepath = os.path.abspath(os.path.join(track_dir, filename))
    if not filepath.startswith(track_dir):
        return jsonify({'error': 'Invalid path'}), 400

    if not os.path.isfile(filepath):
        return jsonify({'error': 'Track not found'}), 404

    return send_from_directory(track_dir, filename, mimetype='application/geo+json')


@app.route('/api/gps/config', methods=['POST'])
def api_gps_config():
    """Update GPS config at runtime (e.g. boom_width)."""
    if not controller.gps_manager:
        return jsonify({'success': False, 'error': 'GPS not enabled'}), 400

    data = request.json or {}
    if 'boom_width' in data:
        try:
            bw = float(data['boom_width'])
            controller.gps_manager.session.boom_width_m = bw
            return jsonify({'success': True, 'boom_width_m': bw})
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Invalid boom_width'}), 400

    return jsonify({'success': False, 'error': 'No valid config parameters'}), 400


# ---------------------------------------------------------------------------
# System admin routes (shutdown, fix screen, reboot)
# ---------------------------------------------------------------------------

@app.route('/api/system/shutdown', methods=['POST'])
def system_shutdown():
    """Shut down all OWLs and the controller itself."""
    try:
        # Send shutdown to all connected OWLs
        result = controller.send_command('all', 'shutdown')

        # Resolve paths to match sudoers entries (command -v in setup script)
        shutdown_bin = shutil.which('shutdown') or '/usr/sbin/shutdown'

        # Background: wait, blank screen, then shut down controller
        def _delayed_shutdown():
            time.sleep(3)
            # Best-effort screen blank (EDATEC / Pi displays)
            # bl_power is root-owned so use sudo tee
            try:
                import glob as globmod
                for bl in globmod.glob('/sys/class/backlight/*/bl_power'):
                    subprocess.run(
                        ['sudo', 'tee', bl],
                        input=b'1', stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=5
                    )
            except Exception:
                pass
            time.sleep(1)
            subprocess.Popen(
                ['sudo', shutdown_bin, 'now'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

        t = threading.Thread(target=_delayed_shutdown, daemon=True)
        t.start()

        return jsonify({
            'success': True,
            'message': 'Shutdown command sent to OWLs. Controller shutting down in ~4s.',
            'owls_notified': result.get('targets', []) if result.get('success') else []
        })
    except Exception as e:
        logger.error(f"Error initiating shutdown: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/system/fix-screen', methods=['POST'])
def system_fix_screen():
    """Reinstall EDATEC HMI3010 touchscreen firmware."""
    try:
        apt_bin = shutil.which('apt') or '/usr/bin/apt'
        result = subprocess.run(
            ['sudo', apt_bin, 'reinstall', '-y', 'ed-hmi3010-101c-firmware'],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            return jsonify({
                'success': True,
                'message': 'Firmware reinstalled successfully',
                'needs_reboot': True
            })
        else:
            return jsonify({
                'success': False,
                'error': f'apt exited with code {result.returncode}',
                'stderr': result.stderr[:500]
            }), 500
    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'error': 'Firmware reinstall timed out (120s)'}), 504
    except Exception as e:
        logger.error(f"Error fixing screen: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/system/reboot', methods=['POST'])
def system_reboot():
    """Reboot the controller (e.g. after firmware fix)."""
    try:
        reboot_bin = shutil.which('reboot') or '/usr/sbin/reboot'

        def _delayed_reboot():
            time.sleep(2)
            subprocess.Popen(
                ['sudo', reboot_bin],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

        t = threading.Thread(target=_delayed_reboot, daemon=True)
        t.start()

        return jsonify({'success': True, 'message': 'Rebooting in ~2s'})
    except Exception as e:
        logger.error(f"Error initiating reboot: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Widget API routes ----

@app.route('/api/widgets')
def list_widgets():
    if widget_manager is None:
        return jsonify([])
    return jsonify(widget_manager.scan())

@app.route('/api/widgets/<widget_id>/template')
def widget_template(widget_id):
    if widget_manager is None:
        return 'Widget system not available', 404
    html = widget_manager.render(widget_id)
    if html is None:
        return 'Widget not found', 404
    return html, 200, {'Content-Type': 'text/html'}

@app.route('/api/widgets/<widget_id>/script')
def widget_script(widget_id):
    if widget_manager is None:
        return 'Widget system not available', 404
    widget = widget_manager.get(widget_id)
    if widget is None or widget.get('type') != 'custom':
        return 'Not found', 404
    script_path = widget_manager.widgets_dir / widget_id / 'script.js'
    if not script_path.exists():
        return '', 200, {'Content-Type': 'application/javascript'}
    js = script_path.read_text()
    wrapped = f'(function(OWLWidget) {{\n{js}\n}})(window.OWLWidget);'
    return wrapped, 200, {'Content-Type': 'application/javascript'}

@app.route('/api/widgets/<widget_id>/style')
def widget_style(widget_id):
    if widget_manager is None:
        return 'Widget system not available', 404
    widget = widget_manager.get(widget_id)
    if widget is None or widget.get('type') != 'custom':
        return 'Not found', 404
    style_path = widget_manager.widgets_dir / widget_id / 'style.css'
    if not style_path.exists():
        return '', 200, {'Content-Type': 'text/css'}
    css = style_path.read_text()
    scoped = f'.widget-container[data-widget-id="{widget_id}"] {{\n{css}\n}}'
    return scoped, 200, {'Content-Type': 'text/css'}

@app.route('/api/widgets/<widget_id>', methods=['DELETE'])
def delete_widget(widget_id):
    if widget_manager is None:
        return jsonify({'error': 'Widget system not available'}), 500
    success, error = widget_manager.remove(widget_id)
    if success:
        return jsonify({'status': 'ok'})
    return jsonify({'error': error}), 404


# ---- Agent API routes ----

@app.route('/api/agent/connect', methods=['POST'])
def agent_connect():
    """Validate API key and configure agent provider."""
    if agent_engine is None:
        return jsonify({'error': 'Agent engine not available'}), 500
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No JSON body'}), 400
    api_key = data.get('api_key', '').strip()
    provider = data.get('provider', 'anthropic').strip()
    if not api_key:
        return jsonify({'error': 'API key is required'}), 400
    try:
        valid = agent_engine.set_provider(api_key, provider)
        if valid:
            status = agent_engine.get_status()
            return jsonify({'status': 'connected', 'model': status.get('model')})
        return jsonify({'error': 'Invalid API key'}), 401
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/agent/chat', methods=['POST'])
def agent_chat():
    """Stream agent responses via SSE."""
    if agent_engine is None:
        return jsonify({'error': 'Agent engine not available'}), 500
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No JSON body'}), 400
    message = data.get('message', '').strip()
    images = data.get('images', [])
    session_id = data.get('session_id', 'default')
    if not message and not images:
        return jsonify({'error': 'Message or image is required'}), 400

    # Validate images
    if len(images) > 4:
        return jsonify({'error': 'Maximum 4 images per message'}), 400
    for img in images:
        if not isinstance(img, str) or len(img) > 1_400_000:
            return jsonify({'error': 'Invalid or oversized image (max ~1MB)'}), 400

    # Build content array if images present
    if images:
        content = []
        for img_data in images:
            content.append({
                'type': 'image',
                'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': img_data}
            })
        if message:
            content.append({'type': 'text', 'text': message})
        chat_input = content
    else:
        chat_input = message

    def generate():
        try:
            for chunk in agent_engine.chat(session_id, chat_input):
                event_data = json.dumps({
                    'type': chunk.type,
                    'data': chunk.data if not hasattr(chunk.data, '__dict__')
                            else {'id': chunk.data.id, 'name': chunk.data.name,
                                  'arguments': chunk.data.arguments},
                })
                yield f"data: {event_data}\n\n"
            session_info = agent_engine.get_session_info(session_id)
            yield f"data: {json.dumps({'type': 'done', 'data': session_info})}\n\n"
        except Exception as e:
            logger.exception("Agent chat error")
            yield f"data: {json.dumps({'type': 'error', 'data': str(e)})}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/api/agent/grab_frame/<device_id>')
def agent_grab_frame(device_id):
    """Grab frame from OWL device, return as base64 for agent chat."""
    import base64 as b64mod
    device_id = device_id.replace('_', '-')
    host = _resolve_owl_host(device_id)
    snapshot_url = f"https://{host}/latest_frame.jpg"
    try:
        r = requests.get(snapshot_url, timeout=5, verify=False)
        if r.status_code != 200:
            return jsonify({'error': f'Could not get frame from {device_id}'}), 502
        b64 = b64mod.b64encode(r.content).decode('ascii')
        return jsonify({'image': b64, 'device_id': device_id})
    except requests.exceptions.ConnectionError:
        return jsonify({'error': f'{device_id} is offline'}), 502
    except Exception as e:
        logger.error(f"Grab frame error for {device_id}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/agent/status')
def agent_status():
    """Return agent connection status and token usage."""
    if agent_engine is None:
        return jsonify({'connected': False, 'provider': None, 'model': None})
    status = agent_engine.get_status()
    return jsonify(status)

@app.route('/api/agent/sessions')
def agent_sessions():
    """List saved agent sessions."""
    if agent_engine is None:
        return jsonify([])
    return jsonify(agent_engine.list_sessions())

@app.route('/api/agent/sessions/<session_id>')
def agent_session_load(session_id):
    """Load a saved session with full message history."""
    if agent_engine is None:
        return jsonify({'error': 'Agent engine not available'}), 500
    data = agent_engine.load_session(session_id)
    if data is None:
        return jsonify({'error': 'Session not found'}), 404
    return jsonify(data)

@app.route('/api/agent/sessions/<session_id>', methods=['DELETE'])
def agent_session_delete(session_id):
    """Delete a saved session."""
    if agent_engine is None:
        return jsonify({'error': 'Agent engine not available'}), 500
    deleted = agent_engine.delete_session(session_id)
    if deleted:
        return jsonify({'status': 'deleted'})
    return jsonify({'error': 'Session not found'}), 404


# ---- Config param route (widget compatibility) ----

def _coerce_value(value):
    """Smart type coercion for widget API values."""
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        low = value.lower()
        if low in ('true', 'false'):
            return low == 'true'
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            pass
    return value

@app.route('/api/config/param', methods=['POST'])
def set_config_param():
    """Set a config parameter or dispatch MQTT command."""
    try:
        data = request.get_json() or {}

        # Action dispatch (widget buttons, sendCommand)
        action = data.get('action')
        if action:
            value = data.get('value')
            result = controller.send_command('all', action, value)
            return jsonify(result)

        # Param/value mode (widget sliders, setParam)
        param = data.get('param', '')
        value = data.get('value')
        if not param or value is None:
            return jsonify({'success': False, 'error': 'param and value required'}), 400

        section = data.get('section', 'GreenOnBrown')
        typed_value = _coerce_value(value)

        if not section or section == 'GreenOnBrown':
            result = controller.send_command('all', 'set_config',
                                              {'key': param, 'value': typed_value})
        else:
            result = controller.send_command('all', 'set_config_section',
                                              {'section': section, 'params': {param: str(typed_value)}})
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error in config/param: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# Initialize on startup
def init_app():
    logger.info("OWL Central Controller Starting")
    controller.setup_mqtt()

    # Start GPS manager if configured
    if controller.gps_manager:
        controller.gps_manager.start()

    # Start connection checker
    checker_thread = threading.Thread(target=controller.check_connections, daemon=True)
    checker_thread.start()

    # Start actuation broadcast loop
    actuation_thread = threading.Thread(target=controller._actuation_broadcast_loop, daemon=True)
    actuation_thread.start()


init_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=False, threaded=True)