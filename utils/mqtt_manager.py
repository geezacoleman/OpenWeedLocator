#!/usr/bin/env python3
"""
Complete MQTT IPC System for OWL Dashboard Communication
Supports both local (standalone) and networked (central controller) modes
"""

import json
import time
import threading
import logging
import os
import re
import ssl
import sys
import configparser
import ipaddress
import tempfile
from pathlib import Path
import socket
from urllib.parse import urlparse

from collections import deque
from utils.config_manager import (
    GREENONBROWN_PARAMS, GEOMETRY_KEYS as MOUNT_GEOMETRY_KEYS,
    GEOMETRY_SECTION_KEYS, GEOMETRY_FILE, atomic_write_config,
    AUTOSAVE_CONFIG, seed_autosave, parse_config_meta, config_unsaved_state,
    ConfigValidator,
)
from utils.directory_manager import scan_sessions, collect_session_files, select_preview_images
from utils import software_update
from utils.session_metadata import clean_session_metadata, CAPTURE_PARAM_KEYS

# Config keys whose live change requires re-deriving crop/lane/actuation geometry.
# When any of these change via set_config_section we call owl.recompute_geometry()
# once (event-driven — never per-frame). Includes the legacy symmetric aliases.
GEOMETRY_KEYS = set(MOUNT_GEOMETRY_KEYS) | {
    'crop_factor_horizontal', 'crop_factor_vertical', 'actuation_zone',
}

# Config keys that can't be applied live — they need a camera/hardware re-init.
# Changing one updates config + state and surfaces a "restart required" notice.
RESTART_REQUIRED_KEYS = {
    'resolution_width', 'resolution_height', 'relay_num',
}

# ---------------------------------------------------------------------------
# Cloud command allowlist (OWL trust chain, Noktura spec §6 / design D7).
# owl_cloud_provision.sh remaps the cloud's inbound `.../commands` onto a
# SEPARATE local topic so nothing arriving from the bridge can reach the
# dashboard command set (spraying, config, install_algorithm). Only these
# actions pass.
#
# The set is exactly what the Noktura portal sends (apps/worker
# command-publisher.ts and transfer-orchestrator.ts). v3.18.0 allowed only the
# first four, so the portal's reboot/restart buttons, model deployment and the
# automatic delete after a verified upload were silently dropped (cross-repo
# review 2026-09-17, O-2; decided by Guy: allow all portal commands).
# ---------------------------------------------------------------------------
CLOUD_ALLOWED_ACTIONS = frozenset({
    'list_data_sessions', 'transfer_session', 'delete_session', 'upload_previews',
    'update_software', 'download_model', 'set_model', 'reboot', 'restart_service',
})
# fullmatch, not match: `$` also matches before a trailing newline, so
# "v1.2.3\n" passed the old check (O-6).
CLOUD_RELEASE_TAG_RE = re.compile(r'v\d+\.\d+\.\d+')


def cloud_command_topic(networked_mode, device_id):
    """Local topic the bridge delivers cloud commands to (see
    owl_cloud_provision.sh: standalone `topic commands in 1 owl/cloud_ ...`,
    networked `topic +/commands in 1 owl/cloud/ ...`)."""
    if networked_mode:
        return f'owl/cloud/{device_id}/commands'
    return 'owl/cloud_commands'


def _url_host(url):
    try:
        return (urlparse(str(url)).hostname or '').lower()
    except ValueError:
        return ''


def is_private_host(host):
    """True only for a literal private or loopback IP, or an mDNS `.local`
    name: a LAN controller that legitimately runs a self-signed certificate.

    Deliberately NOT "any name without a dot" (O-8): urlparse leaves
    `https://16843009/` and `https://0x01010101/` as dotless names that the
    resolver turns into public addresses, and single-label names follow DNS
    search domains anywhere.
    """
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith('.local')
    return address.is_private or address.is_loopback


def ssl_context_for_url(url):
    """Verified TLS by default. Certificate checks are relaxed ONLY for
    private/LAN hosts (the in-cab controller's self-signed dashboard); any
    public host (Noktura storage, GitHub) is verified against the system CAs.
    Design finding M4: presigned upload URLs and models used to be fetched
    with verification off on every host."""
    ctx = ssl.create_default_context()
    if is_private_host(_url_host(url)):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


CLOUD_POLICY_TOPIC = 'owl/cloud/policy'
CONNECTIVITY_INTERVAL_S = 60


def parse_cloud_policy(payload):
    """{'storage_host', 'portal_host'} from the controller's retained policy,
    keeping only well-formed host names."""
    policy = {}
    if not isinstance(payload, dict):
        return policy
    storage = str(payload.get('storage_host') or '').strip().lower()
    if re.fullmatch(r'[a-z0-9.-]{3,253}', storage):
        policy['storage_host'] = storage
    portal = _url_host(payload.get('portal_url') or '')
    if portal:
        policy['portal_host'] = portal
    return policy


# ---------------------------------------------------------------------------
# Slim cloud feed. The mosquitto bridge forwards cloud_out/... to Noktura
# over 4G in place of the full dashboard state (88 keys every 2 s, about
# 20 MB per running hour for a 2-OWL rig). Only these fields leave the unit.
# They must match Noktura's OwlStatePayload (apps/worker/src/mqtt/state-cache.ts);
# upload_requests is the controller's own. A new local state field stays
# local unless it is added here on purpose.
# See docs/handover/CLOUD_DATA_BUDGET_HANDOVER-2026-10-08.md.
# ---------------------------------------------------------------------------
CLOUD_STATE_FIELDS = (
    'device_id', 'owl_running', 'detection_enable',
    'cpu_percent', 'cpu_temp', 'memory_percent', 'disk_percent',
    'gps_latitude', 'gps_longitude', 'gps_available',
    'version', 'git_branch', 'git_commit', 'pi_model', 'os_pretty',
    'software_update', 'data_transfer', 'preview_upload',
    'available_models', 'current_model', 'model_download',
)
# Telemetry rides along but never counts as a change by itself.
CLOUD_VOLATILE_FIELDS = frozenset({
    'cpu_percent', 'cpu_temp', 'memory_percent', 'disk_percent',
    'gps_latitude', 'gps_longitude', 'gps_available', 'timestamp',
})
# In-progress objects: a status change goes out at once, counters are throttled.
CLOUD_PROGRESS_FIELDS = ('data_transfer', 'software_update', 'model_download', 'preview_upload')
# The keys Noktura's manifest reconcile reads from each data_sessions entry.
CLOUD_SESSION_KEYS = ('session_id', 'date', 'time', 'image_count', 'image_size', 'total_size')
# Keepalive under Noktura's 30 s offline timeout (MQTT_STALENESS_MS).
CLOUD_KEEPALIVE_S = 20
CLOUD_PROGRESS_THROTTLE_S = 5
# Resend the session list now and then so a restarted worker re-syncs.
CLOUD_SESSIONS_REFRESH_S = 600


def cloud_state_out_topic(networked_mode, device_id):
    """Local topic for the slim cloud state. Outside owl/ so the controller's
    owl/+/state subscription never mistakes it for a full state."""
    return f'cloud_out/{device_id}/state' if networked_mode else 'cloud_out/state'


def trim_sessions(sessions):
    """data_sessions reduced to the keys Noktura uses. Always the full list:
    Noktura treats a missing session as deleted from the device."""
    return [{k: s[k] for k in CLOUD_SESSION_KEYS if k in s} for s in sessions]


def _cloud_sig(value):
    return json.dumps(value, sort_keys=True, default=str)


class CloudFeed:
    """Decides when the slim cloud state is published: at once when a field
    Noktura acts on changes, progress counters at most every 5 s, otherwise a
    20 s keepalive. Keepalives are QoS 0 so a 4G outage queues nothing stale;
    changes are QoS 1."""

    def __init__(self):
        self.reset()

    def reset(self):
        """Forget what was sent, so the next message is a full one."""
        self._sent_at = None
        self._status_sig = None
        self._full_sig = None
        self._sessions_sig = None
        self._sessions_sent_at = None

    def next_message(self, snapshot, sessions=None, now=None):
        """(payload, qos) to publish now, or None. sessions is the complete
        trimmed list, or None while the device has not scanned its storage
        (an empty list would tell Noktura every session is gone)."""
        now = time.monotonic() if now is None else now
        triggers = {k: v for k, v in snapshot.items() if k not in CLOUD_VOLATILE_FIELDS}
        status = {k: (v.get('status') if k in CLOUD_PROGRESS_FIELDS and isinstance(v, dict) else v)
                  for k, v in triggers.items()}
        status_sig, full_sig = _cloud_sig(status), _cloud_sig(triggers)
        since = float('inf') if self._sent_at is None else now - self._sent_at

        send_sessions = False
        if sessions is not None:
            sessions_sig = _cloud_sig(sessions)
            send_sessions = (sessions_sig != self._sessions_sig or self._sessions_sent_at is None
                             or now - self._sessions_sent_at >= CLOUD_SESSIONS_REFRESH_S)

        if status_sig != self._status_sig or send_sessions:
            qos = 1
        elif full_sig != self._full_sig and since >= CLOUD_PROGRESS_THROTTLE_S:
            qos = 1
        elif since >= CLOUD_KEEPALIVE_S:
            qos = 0
        else:
            return None

        payload = dict(snapshot)
        if send_sessions:
            payload['data_sessions'] = sessions
            self._sessions_sig = sessions_sig
            self._sessions_sent_at = now
        self._sent_at = now
        self._status_sig, self._full_sig = status_sig, full_sig
        return payload, qos


def internet_reachable(host, port=443, timeout=5.0):
    """True when a TCP connection to host:port opens. A cheap check that
    DNS and the route out through the router work; not an HTTPS request."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def cloud_storage_host(controller_ini=None):
    """[Cloud] storage_host from CONTROLLER.ini, or '' (accept any host).

    Read per command, not once at start: the phone app provisions the cloud
    while owl.py is already running, and a value cached at startup stayed empty
    until the next restart.
    """
    path = controller_ini or (Path(__file__).resolve().parents[1] / 'config' / 'CONTROLLER.ini')
    config = configparser.ConfigParser()
    try:
        config.read(str(path))
    except configparser.Error:
        return ''
    return config.get('Cloud', 'storage_host', fallback='').strip().lower()


def cloud_command_urls(command):
    """Every URL a cloud command would make the unit fetch or upload to, in
    the shapes Noktura actually sends (O-3: the old check read `part_urls` and
    `uploads[]`, which nothing sends, so real uploads were never checked)."""
    urls = []
    action = command.get('action')
    if action == 'transfer_session':
        if command.get('upload_url'):
            urls.append(command['upload_url'])
        upload = command.get('upload')
        if isinstance(upload, dict):
            for part in upload.get('parts') or []:
                if isinstance(part, dict) and part.get('url'):
                    urls.append(part['url'])
    elif action == 'upload_previews':
        urls.extend(u for u in (command.get('upload_urls') or []) if u)
    elif action == 'download_model':
        if command.get('url'):
            urls.append(command['url'])
    return urls


def model_path_in(models_dir, name):
    """Absolute path of model `name` inside `models_dir`, or None if `name` is
    not a plain file name that stays there.

    Model names come from commands (cloud or LAN). They are joined into
    models/ and then overwritten or rmtree'd, so `../owl.py` would replace the
    unit's own code. Only bare names are valid: models are listed one level
    deep (_list_available_models).
    """
    if not isinstance(name, str) or not name or name.startswith('.'):
        return None
    if '\x00' in name or os.path.basename(name) != name or '\\' in name:
        return None
    root = os.path.realpath(models_dir)
    path = os.path.realpath(os.path.join(root, name))
    if os.path.dirname(path) != root:
        return None
    return path


def cloud_command_rejection(command, storage_host=''):
    """Why a cloud-origin command must be dropped, or None if it may run.

    Pure function so the rule is unit-testable without a broker.
    """
    if not isinstance(command, dict):
        return 'not an object'
    action = command.get('action')
    if action not in CLOUD_ALLOWED_ACTIONS:
        return f'action not allowed from cloud: {action!r}'
    if action == 'update_software':
        ref = command.get('ref')
        if not isinstance(ref, str) or not CLOUD_RELEASE_TAG_RE.fullmatch(ref):
            return f'cloud updates must target a release tag vX.Y.Z, got {ref!r}'
    if action == 'transfer_session' and str(command.get('method', 'POST')).upper() != 'PUT':
        # POST is the LAN controller's upload endpoint; the cloud only issues
        # presigned PUT URLs (Noktura spec §6.3).
        return 'cloud transfers must use presigned PUT'
    for url in cloud_command_urls(command):
        if urlparse(str(url)).scheme != 'https':
            return f'cloud URLs must be https, got {url!r}'
        if storage_host and _url_host(url) != storage_host:
            return f'host {_url_host(url)!r} is not the configured storage_host'
    return None

# [Camera] keys hot-applied to the running camera via set_camera_controls
# (picamera2 backend). Must be checked BEFORE the generic setattr fallback in
# _handle_set_config_section — exp_compensation used to hit that fallback and
# silently do nothing until restart.
CAMERA_LIVE_KEYS = {'awb_mode', 'awb_red_gain', 'awb_blue_gain', 'exp_compensation'}

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Install paho-mqtt: pip install paho-mqtt")
    exit(1)


def _data_transfer_state(**overrides):
    """Fresh data_transfer state dict — single source of truth for its shape."""
    state = {
        'status': 'idle', 'session_date': '', 'progress': 0,
        'bytes_sent': 0, 'bytes_total': 0, 'error': '',
        'request_id': '', 'upload_id': '', 'parts': [],
        'zip_bytes': 0, 'zip_md5': ''
    }
    state.update(overrides)
    return state


def _preview_upload_state(**overrides):
    """Fresh preview_upload state dict."""
    state = {
        'request_id': '', 'session_id': '', 'status': 'idle',
        'uploaded': 0, 'total': 0, 'error': ''
    }
    state.update(overrides)
    return state


def _valid_multipart(upload):
    """Check a transfer_session 'upload' object has usable parts + part_size."""
    if not isinstance(upload, dict):
        return False
    parts = upload.get('parts')
    if not isinstance(parts, list) or not parts:
        return False
    try:
        return int(upload.get('part_size', 0)) > 0
    except (TypeError, ValueError):
        return False


class _ProgressReader:
    """Bounded file reader that publishes data_transfer progress via MQTT.

    Reads at most `length` bytes from the file handle's current position.
    Progress runs 50-100% across `total` bytes (0-50% was zipping);
    `base_sent` offsets bytes_sent so multipart parts report cumulatively.
    """

    def __init__(self, fh, length, publisher, base_sent=0, total=None):
        self.fh = fh
        self.length = length
        self.publisher = publisher
        self.base_sent = base_sent
        self.total = total if total is not None else length
        self.sent = 0
        self.last_publish = base_sent

    def read(self, size=-1):
        remaining = self.length - self.sent
        if remaining <= 0:
            return b''
        if size is None or size < 0 or size > remaining:
            size = remaining
        chunk = self.fh.read(size)
        self.sent += len(chunk)
        done = self.base_sent + self.sent
        if self.total > 0:
            pct = 50 + int(done / self.total * 50)
        else:
            pct = 100
        with self.publisher.state_lock:
            self.publisher.state['data_transfer']['progress'] = pct
            self.publisher.state['data_transfer']['bytes_sent'] = done
        # Publish every 100KB
        if done - self.last_publish >= 102400:
            self.last_publish = done
            self.publisher._publish_state()
        return chunk

    def __len__(self):
        # urllib checks this against Content-Length — part length, not file size
        return self.length


class OWLMQTTPublisher:
    """
    MQTT Publisher for owl.py - publishes status and receives commans
    Supports both standalone (localhost) and networked (central controller) modes
    """

    def __init__(self, broker_host='localhost', broker_port=1883, client_id='owl_main', device_id=None, network_mode=None, static_ip=None):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.client_id = client_id
        self.logger = logging.getLogger(__name__)

        # Determine device ID
        if device_id is None or device_id == 'auto':
            # Use hostname as default device ID
            device_id = socket.gethostname()
            self.logger.info(f"Auto-detected device_id: {device_id}")

        self.device_id = device_id

        # Determine if we're in networked mode from config, fall back to broker IP heuristic
        if network_mode is not None:
            self.networked_mode = (network_mode.lower() == 'networked')
        else:
            self.networked_mode = (broker_host.lower() not in ['localhost', '127.0.0.1'])

        if self.networked_mode:
            self.logger.info(f"Running in NETWORKED mode - connecting to broker at {broker_host}:{broker_port}")
        else:
            self.logger.info(f"Running in STANDALONE mode - using local broker")

        # MQTT topics - now include device_id for networked mode
        if self.networked_mode:
            # Networked mode: device-specific topics
            self.topics = {
                'commands': f'owl/{device_id}/commands',
                'state': f'owl/{device_id}/state',
                'status': f'owl/{device_id}/status',
                'detection': f'owl/{device_id}/detection',
                'config': f'owl/{device_id}/config',
                'indicators': f'owl/{device_id}/indicators',
                'errors': f'owl/{device_id}/errors',
                'gps': f'owl/{device_id}/gps',
                # Data collection, local broker only (the cloud bridge does
                # not forward these): one event per saved image, and a small
                # JPEG of the latest one for the controller's gallery.
                'capture': f'owl/{device_id}/capture',
                'thumb': f'owl/{device_id}/thumb'
            }
        else:
            # Standalone mode: simple topics for backward compatibility
            self.topics = {
                'commands': 'owl/commands',
                'state': 'owl/state',
                'status': 'owl/status',
                'detection': 'owl/detection',
                'config': 'owl/config',
                'indicators': 'owl/indicators',
                'errors': 'owl/errors',
                'gps': 'owl/gps',
                'capture': 'owl/capture',
                'thumb': 'owl/thumb'
            }
        # Cloud-origin commands arrive on their own topic and pass an allowlist
        self.topics['cloud_commands'] = cloud_command_topic(self.networked_mode, device_id)
        # Networked OWLs hold no [Cloud] section of their own: the controller
        # publishes the rig's upload host here (retained) so transfer URLs are
        # still pinned to Noktura storage.
        self.topics['cloud_policy'] = CLOUD_POLICY_TOPIC
        self._cloud_policy = {}
        # Slim state for the cloud bridge (local broker only; the bridge
        # decides whether it leaves the unit).
        self.topics['cloud_state'] = cloud_state_out_topic(self.networked_mode, device_id)
        self._cloud_feed = CloudFeed()
        # Trimmed session list from the last successful storage scan; None
        # until then, so the cloud never sees an unscanned empty list.
        self._cloud_sessions = None

        # Current state
        self.state = {
            'device_id': device_id,
            'detection_enable': False,
            'image_sample_enable': False,
            # False when no writable USB drive is present — recording is
            # unavailable but detection runs normally (re-checked on every
            # record toggle in owl.py).
            'storage_available': True,
            # eMMC/internal recording (OWL 3.0): additive fields, contract 1.
            # storage_free_mb is None until the storage watchdog samples.
            'storage_location': 'usb',
            'storage_free_mb': None,
            'storage_warning': 'ok',   # ok | low | full
            'image_quota_gb': 12,
            # Where recording actually lands ('usb'|'internal', None until
            # storage resolves), whether that was a fallback from a missing
            # USB drive, and why recording was refused (None when it wasn't):
            # no_drive | storage_full | floor_unreachable | storage_error.
            # Additive fields.
            'recording_location': None,
            'storage_fallback': False,
            'recording_blocked_reason': None,
            'sensitivity_level': 'medium',
            'detection_mode': 1,  # 0=spot spray, 1=off, 2=blanket
            'owl_running': False,
            'stream_active': False,
            # Comma-joined config keys needing a restart to apply. Seeded empty
            # so a freshly (re)started OWL actively publishes "nothing pending"
            # and controllers can drop any notice cached from before the restart.
            'restart_required': '',
            # System statistics
            'cpu_percent': 0,
            'cpu_temp': 0,
            'memory_percent': 0,
            'memory_used': 0,
            'memory_total': 0,
            'disk_percent': 0,
            'disk_used': 0,
            'disk_total': 0,
            'fan_status': {'is_rpi5': False, 'mode': 'unavailable', 'rpm': 0},
            # GPS
            'gps_latitude': 0.0,
            'gps_longitude': 0.0,
            'gps_accuracy': 0.0,
            'gps_timestamp': 0.0,
            'gps_available': False,
            'gps_payload': None,
            'gps_received_at': None,
            'last_update': time.time(),
            'networked_mode': self.networked_mode,
            'broker_host': broker_host,
            'static_ip': static_ip or '',
            # Algorithm state
            'algorithm': 'exhsv',
            'model_available': False,
            'crop_buffer_px': 20,
            'inference_resolution': 320,
            # Painted LUT detection profiles
            'lut_profile': '',
            'lut_sensitivity': 50,
            'available_lut_profiles': [],
            # GreenOnGreen parameters
            'confidence': 0.5,
            # AI tab: model + class info
            'current_model': '',
            'available_models': [],
            'model_classes': {},
            'detect_classes': [],
            # GreenOnBrown parameters (will be populated on first update)
            'exg_min': None,
            'exg_max': None,
            'hue_min': None,
            'hue_max': None,
            'saturation_min': None,
            'saturation_max': None,
            'brightness_min': None,
            'brightness_max': None,
            # Actuation state
            'avg_loop_time_ms': 0.0,
            'actuation_duration': 0.15,
            'delay': 0.0,
            'actuation_source': 'config',
            # Spray timing as owl.py runs it (additive): actuation_source is who
            # owns the timing ('device-gps' | 'controller' | 'config'), plus the
            # speed it uses, frame-capture-to-relay latency and the
            # farmer-facing speed warning ('' when speed is not a problem).
            'speed_kmh': None,
            'latency_ms': None,
            'speed_warning': '',
            # BOARD pins this OWL's own GPS hardware uses ({'37': reason});
            # dashboards refuse config changes that would drive them.
            'protected_pins': {},
            # Camera resolution
            'resolution_width': 0,
            'resolution_height': 0,
            'requested_resolution_width': 0,
            'requested_resolution_height': 0,
            'resolution_clamped': False,
            'allow_high_resolution': False,
            # Hardware
            'rpi_version': 'unknown',
            # Software version (populated once at startup — the process
            # restarts after every update, so this is always current)
            'version': 'unknown',
            'git_branch': 'unknown',
            'git_commit': 'unknown',
            'pi_model': 'unknown',
            'os_pretty': 'unknown',
            # Remote software update state (mirrored from .update_status.json
            # written by owl_update.sh; see _read_update_status)
            'software_update': {
                'request_id': '',
                'status': 'idle',
                'ref': '',
                'from': '',
                'to': '',
                'error': '',
                'rollback_failed': False,
                'updated_at': 0
            },
            # Model download state
            'model_download': {
                'status': 'idle',
                'model_name': '',
                'progress': 0,
                'error': ''
            },
            # One-tap white-balance calibration (calibrate_awb command):
            # auto AWB sampled on a grey card / bare soil, then locked as
            # manual gains. updated_at lets the UI spot a fresh completion.
            'awb_calibration': {
                'status': 'idle',
                'red_gain': None,
                'blue_gain': None,
                'colour_temperature': None,
                'error': '',
                'updated_at': 0
            },
            # Live camera colour state from picamera2 frame metadata
            # (refreshed every heartbeat; metadata keys omitted on webcams)
            'camera': {
                'awb_mode': None,
                'awb_red_gain': None,
                'awb_blue_gain': None,
            },
            # Storage
            'save_directory': '',
            # Session metadata (filled by farmer via dashboard)
            'session_metadata': clean_session_metadata({}),
            # Data collection: capture settings, last saved image, image
            # quality, shutter cap (owl.capture_state, refreshed each heartbeat)
            'capture': None,
            # Connectivity (_connectivity_loop): None = not checked / unknown
            'clock_synced': None,
            'internet_ok': None,
            # Data sessions and transfer state
            'data_sessions': [],
            'data_transfer': _data_transfer_state(),
            # Preview thumbnail upload state (upload_previews command)
            'preview_upload': _preview_upload_state()
        }

        self._populate_version_info()

        # Remote update status file (written by owl_update.sh)
        self._update_status_path = Path(__file__).resolve().parents[1] / '.update_status.json'
        self._update_status_stamp = None  # (mtime, size) of last successful read

        # Thread safety
        self.state_lock = threading.RLock()
        self.config_lock = threading.Lock()

        # Config file path (set via set_owl_instance or directly)
        self.config_file = None

        # OWL instance reference
        self.owl_instance = None
        self.sensitivity_manager = None

        self.last_sensitivity_level = 'medium'
        self.monitoring_thread = None
        self.heartbeat_thread = None

        # MQTT client
        self.client = mqtt.Client(client_id=self.client_id)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        # Enable paho's built-in auto-reconnect after initial connection is lost
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

        # Last Will & Testament — broker publishes this if OWL disconnects ungracefully
        lwt_payload = json.dumps({
            'device_id': self.device_id,
            'owl_running': False,
            'connected': False,
            'timestamp': time.time()
        })
        self.client.will_set(self.topics['status'], lwt_payload, qos=1, retain=True)

        # Connection state
        self.connected = False
        self.running = False
        self.connection_attempts = 0
        self.max_connection_attempts = 5
        self._reconnect_thread = None

    def set_owl_instance(self, owl_instance):
        """Set reference to owl instance and sensitivity manager"""
        self.owl_instance = owl_instance
        self.sensitivity_manager = getattr(owl_instance, 'sensitivity_manager', None)

        # Capture the config file path from the owl instance
        if hasattr(owl_instance, 'config_path'):
            self.config_file = owl_instance.config_path

        config = getattr(owl_instance, 'config', None)
        if config is not None and hasattr(config, 'has_section'):
            with self.state_lock:
                self.state['protected_pins'] = {
                    str(pin): why for pin, why in ConfigValidator.protected_pins(config).items()}

        self.logger.info("OWL instance configured")
        if self.sensitivity_manager:
            active = self.sensitivity_manager.get_active_preset()
            self.logger.info(f"  Active sensitivity preset: {active}")

        # Initialize state with current OWL parameters
        self._sync_parameters_to_state()

    def _sync_parameters_to_state(self):
        """Sync current OWL parameters to MQTT state"""
        if self.owl_instance is None:
            return

        with self.state_lock:
            self.state['exg_min'] = self.owl_instance.exg_min
            self.state['exg_max'] = self.owl_instance.exg_max
            self.state['hue_min'] = self.owl_instance.hue_min
            self.state['hue_max'] = self.owl_instance.hue_max
            self.state['saturation_min'] = self.owl_instance.saturation_min
            self.state['saturation_max'] = self.owl_instance.saturation_max
            self.state['brightness_min'] = self.owl_instance.brightness_min
            self.state['brightness_max'] = self.owl_instance.brightness_max
            self.state['min_detection_area'] = getattr(
                self.owl_instance, 'min_detection_area', 10)
            mda_pct = getattr(self.owl_instance, 'min_detection_area_percent', 0.0)
            self.state['min_detection_area_percent'] = (
                mda_pct if isinstance(mda_pct, (int, float)) else 0.0)

            self.state['confidence'] = getattr(
                self.owl_instance, '_gog_confidence', 0.5)

            # Algorithm state
            self.state['algorithm'] = self.owl_instance.config.get(
                'System', 'algorithm', fallback='exhsv')
            self.state['crop_buffer_px'] = getattr(
                self.owl_instance, 'crop_buffer_px', 20)
            self.state['inference_resolution'] = getattr(
                self.owl_instance, 'inference_resolution', 320)

            # Save directory (runtime-resolved — may differ from config if USB path changed)
            self.state['save_directory'] = getattr(self.owl_instance, 'save_directory', '')

            # Active config filename (basename) so the dashboard can show what's
            # running. The autosave working file reports the preset it derives
            # from; 'unsaved' means its content actually differs from that
            # source profile — not merely "running from the autosave file"
            # (which persists across reboots and used to always read unsaved).
            try:
                active = self._resolve_config_path()
                basename = os.path.basename(active) if active else ''
                unsaved, source = config_unsaved_state(active) if active else (False, '')
                self.state['config_name'] = basename
                self.state['config_unsaved'] = unsaved
                self.state['config_source'] = source
            except Exception:
                self.state['config_name'] = ''
                self.state['config_unsaved'] = False
                self.state['config_source'] = ''

            # Hardware controller info (for networked controller to detect incompatible setups)
            ct = getattr(self.owl_instance, 'controller_type', None)
            self.state['controller_type'] = ct if isinstance(ct, str) else 'none'
            sp = getattr(self.owl_instance, 'switch_purpose', None)
            self.state['switch_purpose'] = sp if isinstance(sp, str) else 'recording'

            # Camera resolution (actual, post-clamp) + requested + clamp flag.
            # Each field is type-guarded because owl_instance may be a Mock in
            # tests — bare getattr would leak MagicMock objects into the state
            # dict and break json.dumps in _publish_state.
            res = getattr(self.owl_instance, 'resolution', (0, 0))
            if isinstance(res, tuple) and len(res) == 2 and all(isinstance(v, int) for v in res):
                self.state['resolution_width'] = res[0]
                self.state['resolution_height'] = res[1]
            else:
                res = (0, 0)
            requested = getattr(self.owl_instance, 'requested_resolution', res)
            if isinstance(requested, tuple) and len(requested) == 2 and all(isinstance(v, int) for v in requested):
                self.state['requested_resolution_width'] = requested[0]
                self.state['requested_resolution_height'] = requested[1]
            else:
                self.state['requested_resolution_width'] = self.state['resolution_width']
                self.state['requested_resolution_height'] = self.state['resolution_height']
            clamped = getattr(self.owl_instance, 'resolution_clamped', False)
            self.state['resolution_clamped'] = clamped if isinstance(clamped, bool) else False
            rpi_version = getattr(self.owl_instance, 'RPI_VERSION', 'unknown')
            self.state['rpi_version'] = rpi_version if isinstance(rpi_version, str) else 'unknown'
            owl_config = getattr(self.owl_instance, 'config', None)
            if isinstance(owl_config, configparser.ConfigParser):
                try:
                    self.state['allow_high_resolution'] = owl_config.getboolean(
                        'Camera', 'allow_high_resolution', fallback=False)
                except Exception:
                    self.state['allow_high_resolution'] = False

            # Check model availability (any NCNN dirs or .pt files in models/)
            self.state['model_available'] = self._check_model_available()

            # AI tab: available models, current model, class names
            self.state['available_models'] = self._list_available_models()
            self.state['detect_classes'] = getattr(self.owl_instance, '_detect_classes_list', [])
            gog = getattr(self.owl_instance, '_gog_detector', None)
            # Use pending model name if OWL hasn't processed the swap yet,
            # so the dashboard dropdown doesn't snap back to the old model.
            pending_model = getattr(self.owl_instance, '_pending_model', None)
            if pending_model is not None:
                pass  # keep current_model as set by the set_model handler
            elif gog and hasattr(gog, 'model'):
                self.state['current_model'] = getattr(gog, '_model_filename', '')
                self.state['model_classes'] = {str(k): v for k, v in gog.model.names.items()}
            else:
                self.state['current_model'] = ''
                self.state['model_classes'] = {}

            # Tracking state
            self.state['tracking_enabled'] = getattr(
                self.owl_instance, 'tracking_enabled', False)

            # Sensitivity presets
            if self.sensitivity_manager:
                self.state['sensitivity_level'] = self.sensitivity_manager.get_active_preset()
                self.state['sensitivity_presets'] = self.sensitivity_manager.list_presets()

            # LUT profile state. Skip while a switch is pending so the
            # dashboard doesn't snap back to the old profile mid-drain.
            if getattr(self.owl_instance, '_pending_lut_profile', None) is None:
                lut_profile = getattr(self.owl_instance, 'lut_profile', '')
                self.state['lut_profile'] = lut_profile if isinstance(lut_profile, str) else ''
            if getattr(self.owl_instance, '_pending_lut_sensitivity', None) is None:
                lut_sens = getattr(self.owl_instance, 'lut_sensitivity', 50)
                self.state['lut_sensitivity'] = lut_sens if isinstance(lut_sens, int) else 50
            lut_mgr = getattr(self.owl_instance, 'lut_manager', None)
            if lut_mgr is not None:
                try:
                    profiles = lut_mgr.list_profiles()
                    self.state['available_lut_profiles'] = (
                        profiles if isinstance(profiles, list) else [])
                except Exception:
                    self.state['available_lut_profiles'] = []

    def start(self):
        """Start the MQTT IPC server.

        Starts heartbeat and monitoring threads immediately (they guard on
        self.connected). If the broker is unreachable, a background reconnect
        thread retries with exponential backoff instead of giving up.
        """
        self.running = True

        with self.state_lock:
            self.state['owl_running'] = True

        # Pick up the terminal status of a software update that restarted us
        # (owl_update.sh writes complete/rolled_back after the restart).
        self._read_update_status()

        # Start threads first — they already guard on self.connected
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()

        self.monitoring_thread = threading.Thread(target=self._monitor_states, daemon=True)
        self.monitoring_thread.start()

        self.connectivity_thread = threading.Thread(target=self._connectivity_loop, daemon=True)
        self.connectivity_thread.start()

        # Try to connect to broker
        try:
            self.logger.info(f"Attempting to connect to MQTT broker at {self.broker_host}:{self.broker_port}")
            self.client.connect(self.broker_host, self.broker_port, 60)
            self.client.loop_start()

            # Publish initial state
            self._publish_state()

            self.logger.info(f"MQTT IPC Server started successfully")
            self.logger.info(f"Device ID: {self.device_id}")
            self.logger.info(f"Publishing to topics: {list(self.topics.values())}")

        except Exception as e:
            if self.networked_mode:
                self.logger.warning(f"Could not connect to network broker at {self.broker_host}:{self.broker_port}: {e}")
                self.logger.warning("OWL will continue to operate locally; reconnecting in background")
            else:
                self.logger.warning(f"Could not connect to local MQTT broker: {e}")
                self.logger.warning("Dashboard features disabled; reconnecting in background")

            # Start background reconnect instead of giving up
            self._reconnect_thread = threading.Thread(target=self._background_reconnect, daemon=True)
            self._reconnect_thread.start()

    def _background_reconnect(self):
        """Retry broker connection with exponential backoff (2s → 30s cap)."""
        delay = 2
        max_delay = 30

        while self.running and not self.connected:
            self.logger.info(f"MQTT reconnect: trying {self.broker_host}:{self.broker_port} in {delay}s...")
            time.sleep(delay)

            if not self.running:
                break

            try:
                self.client.connect(self.broker_host, self.broker_port, 60)
                self.client.loop_start()
                # _on_connect callback sets self.connected = True
                self.logger.info("MQTT reconnect: connection attempt sent, waiting for callback")
                break
            except Exception as e:
                self.logger.warning(f"MQTT reconnect failed: {e}")
                delay = min(delay * 2, max_delay)

    def stop(self):
        """Stop the MQTT server"""
        self.running = False

        # Mark OWL as stopped
        with self.state_lock:
            self.state['owl_running'] = False

        if self.connected:
            try:
                self.client.publish(self.topics['status'], json.dumps({
                    'device_id': self.device_id,
                    'owl_running': False,
                    'timestamp': time.time()
                }), retain=True)

                # Also update main state
                self._publish_state()
            except Exception:
                pass  # Best-effort during shutdown

        try:
            self.client.loop_stop()
        except Exception:
            pass  # Client may never have started the loop

        try:
            self.client.disconnect()
        except Exception:
            pass  # Client may never have connected

        self.logger.info("MQTT IPC Server stopped")

    def _on_connect(self, client, userdata, flags, rc):
        """Handle MQTT connection"""
        if rc == 0:
            self.connected = True
            self.connection_attempts = 0
            self.logger.info(f"Connected to MQTT broker at {self.broker_host}:{self.broker_port}")

            # Subscribe to command topics
            client.subscribe(self.topics['commands'])
            client.subscribe(self.topics['cloud_commands'])
            client.subscribe(self.topics['gps'])
            if self.networked_mode:
                client.subscribe(self.topics['cloud_policy'])

            self.logger.info(f"Subscribed to: {self.topics['commands']}, "
                             f"{self.topics['cloud_commands']}, {self.topics['gps']}")

            # Publish connection status
            client.publish(self.topics['status'], json.dumps({
                'device_id': self.device_id,
                'owl_running': True,
                'connected': True,
                'timestamp': time.time()
            }), retain=True)

            # Publish full state immediately so controller sees everything
            # (don't wait for next 2s heartbeat cycle)
            self._publish_state()
            # First cloud message after (re)connecting is a full one.
            self._cloud_feed.reset()

        else:
            self.connected = False
            self.connection_attempts += 1
            self.logger.error(f"Failed to connect to MQTT broker: rc={rc}")

            if self.connection_attempts >= self.max_connection_attempts:
                self.logger.error(f"Max connection attempts ({self.max_connection_attempts}) reached")

    def _on_disconnect(self, client, userdata, rc):
        """Handle MQTT disconnection"""
        self.connected = False
        if rc != 0:
            self.logger.warning(f"Unexpected MQTT disconnection (rc={rc})")
            if self.networked_mode:
                self.logger.warning("Lost connection to central controller - OWL continues operating locally")

    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages"""
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode())

            if topic == self.topics['commands']:
                self._handle_command(payload)
            elif topic == self.topics['cloud_commands']:
                self._handle_cloud_command(payload)
            elif topic == self.topics['gps']:
                self._handle_gps_update(payload)
            elif topic == self.topics['cloud_policy']:
                self._cloud_policy = parse_cloud_policy(payload)

        except Exception as e:
            self.logger.error(f"Error processing MQTT message on topic {msg.topic}: {e}", exc_info=True)
            print(f"[OWL MQTT ERROR] {msg.topic}: {e}", file=sys.stderr)

    def _handle_cloud_command(self, command):
        """Commands that arrived through the cloud bridge. Allowlisted
        (CLOUD_ALLOWED_ACTIONS), release-tag-only updates, https URLs only on
        [Cloud] storage_host when set. Anything else is logged and dropped —
        a stolen bridge credential must not be able to spray or reconfigure."""
        storage_host = cloud_storage_host() or self._cloud_policy.get('storage_host', '')
        reason = cloud_command_rejection(command, storage_host)
        if reason:
            action = command.get('action') if isinstance(command, dict) else None
            self.logger.warning(f"cloud command dropped ({reason}): {action!r}")
            return
        self.logger.info(f"Cloud command accepted: {command.get('action')}")
        self._handle_command(command)

    # Commands broadcast on a ~1 Hz cadence (e.g. the controller's speed-adaptive
    # actuation loop) log at DEBUG; their handlers log real changes at INFO.
    HIGH_FREQUENCY_COMMANDS = {'set_actuation_params'}

    def _handle_command(self, command):
        """Handle control commands from dashboard or central controller"""
        action = command.get('action')

        if action in self.HIGH_FREQUENCY_COMMANDS:
            self.logger.debug(f"Received command: {action}")
        else:
            self.logger.info(f"Received command: {action}")

        with self.state_lock:
            if action == 'set_detection_enable':
                self.state['detection_enable'] = bool(command.get('value', False))
                self.logger.info(f"Detection enable set to: {self.state['detection_enable']}")

            elif action == 'set_image_sample_enable':
                was_recording = self.state['image_sample_enable']
                self.state['image_sample_enable'] = bool(command.get('value', False))
                self.logger.info(f"Image sample enable set to: {self.state['image_sample_enable']}")

                # Auto-save session metadata when recording stops
                if was_recording and not self.state['image_sample_enable']:
                    self._auto_save_session_metadata()

            elif action == 'set_sensitivity_level':
                level = command.get('level', '').lower()

                if self.sensitivity_manager:
                    valid_names = {p['name'] for p in self.sensitivity_manager.list_presets()}
                    if level not in valid_names:
                        self.logger.error(f"Invalid sensitivity preset: {level}")
                        return
                else:
                    if level not in ('low', 'medium', 'high'):
                        self.logger.error(f"Invalid sensitivity level: {level}")
                        return

                self.state['sensitivity_level'] = level
                self.logger.info(f"Sensitivity level set to: {level}")
                self._apply_sensitivity_preset(level)

            elif action == 'set_greenonbrown_param':
                # Individual parameter update
                param_name = command.get('param')
                param_value = command.get('value')

                if param_name and param_value is not None:
                    self.logger.info(f"Updating GreenOnBrown parameter: {param_name} = {param_value}")
                    self._update_greenonbrown_param(param_name, param_value)

            elif action == 'set_algorithm':
                value = command.get('value', '').lower()
                try:
                    from utils.config_manager import ConfigValidator
                    valid = ConfigValidator.get_valid_algorithms()
                except Exception:
                    valid = {'exg', 'exhsv', 'hsv', 'lut', 'gog', 'gog-hybrid'}
                if value in valid:
                    self.state['algorithm'] = value
                    if self.owl_instance:
                        self.owl_instance._pending_algorithm = value
                        # Update config so heartbeat reads new value immediately
                        if hasattr(self.owl_instance, 'config'):
                            self.owl_instance.config.set('System', 'algorithm', value)
                    self.logger.info(f"Algorithm set to: {value}")
                else:
                    self.logger.error(f"Invalid algorithm: {value}")

            elif action == 'install_algorithm':
                algo_name = command.get('name', '')
                algo_code = command.get('code', '')
                if algo_name and algo_code:
                    try:
                        from custom_algorithms import validate_algorithm_code, save_algorithm
                        ok, err = validate_algorithm_code(algo_code)
                        if ok:
                            result = save_algorithm(algo_name, algo_code,
                                                    command.get('description', ''))
                            if result.get('success'):
                                self.logger.info(f"Custom algorithm installed: {algo_name}")
                            else:
                                self.logger.error(f"Custom algorithm save failed: {result.get('error')}")
                        else:
                            self.logger.error(f"Custom algorithm validation failed: {err}")
                    except Exception as e:
                        self.logger.error(f"Custom algorithm install error: {e}")

            elif action == 'set_greenongreen_param':
                key = command.get('key')
                value = command.get('value')
                if key and value is not None:
                    self._update_greenongreen_param(key, value)

            elif action == 'set_config':
                key = command.get('key')
                value = command.get('value')
                if key is not None and value is not None:
                    self._update_greenonbrown_param(key, value)

            elif action == 'set_crop_buffer':
                try:
                    value = max(0, min(50, int(command.get('value', 20))))
                    self.state['crop_buffer_px'] = value
                    if self.owl_instance:
                        self.owl_instance.crop_buffer_px = value
                    self.logger.info(f"Crop buffer set to: {value}px")
                except (ValueError, TypeError) as e:
                    self.logger.error(f"Invalid crop_buffer value: {e}")

            elif action == 'set_detect_classes':
                raw = command.get('value', [])
                if isinstance(raw, str):
                    class_list = [c.strip() for c in raw.split(',') if c.strip()]
                elif isinstance(raw, list):
                    class_list = [str(c).strip() for c in raw if str(c).strip()]
                else:
                    class_list = []
                if self.owl_instance:
                    self.owl_instance._pending_detect_classes = class_list
                self.state['detect_classes'] = class_list
                self.logger.info(f"detect_classes queued: {class_list}")
                self._publish_state()

            elif action == 'set_model':
                model_name = command.get('value', '')
                models_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'models')
                if model_name and model_path_in(models_dir, model_name) is None:
                    self.logger.error(f"set_model refused: not a model name: {model_name!r}")
                elif model_name and self.owl_instance:
                    # Resolve to models/ directory path so GreenOnGreen can find it
                    model_path = os.path.join('models', str(model_name))
                    self.owl_instance._pending_model = model_path
                    # Immediately report new model so dashboard doesn't snap back
                    self.state['current_model'] = str(model_name)
                    self.logger.info(f"Model switch queued: {model_path}")
                    self._publish_state()

            elif action == 'get_config':
                self._handle_get_config()

            elif action == 'set_config_section':
                section = command.get('section')
                params = command.get('params', {})
                if section and params:
                    self._handle_set_config_section(section, params)

            elif action == 'save_config':
                filename = command.get('filename')
                self._handle_save_config(filename,
                                         name=command.get('name'),
                                         notes=command.get('notes'))

            elif action == 'save_geometry':
                self._handle_save_geometry(command.get('params') or {})

            elif action == 'set_preview_mode':
                mode = command.get('mode', 'cropped')
                if self.owl_instance is not None:
                    self.owl_instance._stream_full_frame = (mode == 'full')
                    self.logger.info(f"Preview mode set to {mode}")

            elif action == 'set_active_config':
                config_path = command.get('config')
                if config_path:
                    self._handle_set_active_config(config_path)

            elif action == 'set_detection_mode':
                mode = int(command.get('value', 1))
                valid_modes = {0, 1, 2}
                if mode not in valid_modes:
                    self.logger.error(f"Invalid detection mode: {mode}")
                    return
                self.state['detection_mode'] = mode
                if mode == 2:  # Blanket — all nozzles on
                    self.state['detection_enable'] = False
                    if self.owl_instance and hasattr(self.owl_instance, 'relay_controller'):
                        self.owl_instance.relay_controller.relay.all_on()
                    self.logger.info("Blanket spray: all nozzles ON, detection disabled")
                elif mode == 0:  # Spot spray — detection on
                    self.state['detection_enable'] = True
                    if self.owl_instance and hasattr(self.owl_instance, 'relay_controller'):
                        self.owl_instance.relay_controller.relay.all_off()
                    self.logger.info("Spot spray: detection enabled, nozzles auto")
                else:  # Off
                    self.state['detection_enable'] = False
                    if self.owl_instance and hasattr(self.owl_instance, 'relay_controller'):
                        self.owl_instance.relay_controller.relay.all_off()
                    self.logger.info("Off: detection disabled, all nozzles OFF")

            elif action == 'set_actuation_params':
                self._handle_set_actuation_params(command)

            elif action == 'download_model':
                url = command.get('url')
                filename = command.get('filename')
                sha256 = command.get('sha256', '')
                is_archive = command.get('is_archive', False)
                if url and filename:
                    threading.Thread(
                        target=self._download_model,
                        args=(url, filename, sha256, is_archive),
                        daemon=True
                    ).start()
                else:
                    self.logger.error("download_model missing url or filename")

            elif action == 'calibrate_awb':
                # Needs ~2 s of auto AWB; the dispatch holds state_lock, so
                # the wait runs in a worker like download_model.
                if self.state['awb_calibration'].get('status') == 'running':
                    self.logger.info("calibrate_awb already running; ignored")
                else:
                    self.state['awb_calibration'].update(
                        status='running', error='', updated_at=time.time())
                    threading.Thread(target=self._calibrate_awb,
                                     daemon=True).start()

            elif action == 'set_lut_profile':
                name = str(command.get('name', '')).strip().lower()
                if not name:
                    self.logger.error("set_lut_profile missing name")
                else:
                    self.state['lut_profile'] = name
                    if self.owl_instance:
                        self.owl_instance._pending_lut_profile = name
                        # Mirror into config so heartbeat/persist see it immediately
                        if hasattr(self.owl_instance, 'config'):
                            self.owl_instance.config.set(
                                'GreenOnBrown', 'lut_profile', name)
                    self.logger.info(f"LUT profile queued: {name}")

            elif action == 'set_lut_sensitivity':
                try:
                    value = max(0, min(100, int(command.get('value', 50))))
                except (TypeError, ValueError):
                    self.logger.error(
                        f"Invalid lut_sensitivity: {command.get('value')!r}")
                else:
                    self.state['lut_sensitivity'] = value
                    if self.owl_instance:
                        self.owl_instance._pending_lut_sensitivity = value
                        if hasattr(self.owl_instance, 'config'):
                            self.owl_instance.config.set(
                                'GreenOnBrown', 'lut_sensitivity', str(value))
                    self.logger.info(f"LUT sensitivity queued: {value}")

            elif action == 'download_lut_profile':
                url = command.get('url')
                filename = command.get('filename')
                sha256 = command.get('sha256', '')
                # apply=True activates the profile (and the lut algorithm)
                # once the download verifies — avoids the race where a
                # set_lut_profile lands before the file exists on disk.
                apply_profile = bool(command.get('apply', False))
                try:
                    apply_sensitivity = max(0, min(100, int(command.get('sensitivity', 50))))
                except (TypeError, ValueError):
                    apply_sensitivity = 50
                if url and filename:
                    threading.Thread(
                        target=self._download_lut_profile,
                        args=(url, filename, sha256, apply_profile, apply_sensitivity),
                        daemon=True
                    ).start()
                else:
                    self.logger.error("download_lut_profile missing url or filename")

            elif action == 'delete_lut_profile':
                name = str(command.get('name', '')).strip().lower()
                lut_mgr = getattr(self.owl_instance, 'lut_manager', None) \
                    if self.owl_instance else None
                if name and lut_mgr:
                    try:
                        lut_mgr.delete(name)
                        self.logger.info(f"Deleted LUT profile: {name}")
                        if self.state.get('lut_profile') == name:
                            self.logger.warning(
                                f"Deleted the active LUT profile '{name}'; "
                                "detection keeps the in-memory table until a "
                                "new profile is applied")
                        self._sync_parameters_to_state()
                    except Exception as e:
                        self.logger.error(f"Failed to delete LUT profile '{name}': {e}")
                else:
                    self.logger.error("delete_lut_profile missing name or manager")

            elif action == 'list_lut_profiles':
                # Just sync — profiles are published as part of state
                self._sync_parameters_to_state()

            elif action == 'save_sensitivity_preset':
                name = command.get('name', '').strip()
                if name and self.sensitivity_manager and self.owl_instance:
                    success = self.sensitivity_manager.save_custom_preset(
                        name, owl_instance=self.owl_instance)
                    if success:
                        self.logger.info(f"Saved sensitivity preset: {name}")
                    else:
                        self.logger.error(f"Failed to save preset: {name}")
                    self._sync_parameters_to_state()

            elif action == 'delete_sensitivity_preset':
                name = command.get('name', '').strip()
                if name and self.sensitivity_manager:
                    success = self.sensitivity_manager.delete_custom_preset(name)
                    if success:
                        self.logger.info(f"Deleted sensitivity preset: {name}")
                    else:
                        self.logger.error(f"Failed to delete preset: {name}")
                    self._sync_parameters_to_state()

            elif action == 'list_sensitivity_presets':
                # Just sync — presets are published as part of state
                self._sync_parameters_to_state()

            elif action == 'set_tracking':
                raw = command.get('value', False)
                value = raw if isinstance(raw, bool) else str(raw).lower() == 'true'
                self.state['tracking_enabled'] = value
                if self.owl_instance:
                    self.owl_instance.tracking_enabled = value
                    # Create or clear smoother/stabilizer
                    if value and not getattr(self.owl_instance, '_class_smoother', None):
                        from utils.tracker import ClassSmoother, CropMaskStabilizer
                        window = getattr(self.owl_instance, '_track_class_window', 5)
                        persist = getattr(self.owl_instance, '_track_crop_persist', 3)
                        self.owl_instance._class_smoother = ClassSmoother(window=window)
                        self.owl_instance._crop_stabilizer = CropMaskStabilizer(max_age=persist)
                    elif not value:
                        if getattr(self.owl_instance, '_class_smoother', None):
                            self.owl_instance._class_smoother.reset()
                        if getattr(self.owl_instance, '_crop_stabilizer', None):
                            self.owl_instance._crop_stabilizer.reset()
                    # Update detector's tracking_enabled flag
                    gog = getattr(self.owl_instance, '_gog_detector', None)
                    if gog and hasattr(gog, 'tracking_enabled'):
                        gog.tracking_enabled = value
                        if not value:
                            gog.reset_tracker()
                        elif value and self.owl_instance._crop_stabilizer:
                            gog._crop_stabilizer = self.owl_instance._crop_stabilizer
                self.logger.info(f"Tracking {'enabled' if value else 'disabled'}")
                self._publish_state()

            elif action == 'set_session_metadata':
                metadata = clean_session_metadata(command)
                self.state['session_metadata'] = metadata
                # The app sends metadata BEFORE it enables recording, so no
                # session directory exists yet — the copy held in state is
                # written into the session dir when recording starts (owl.py)
                # and again on stop (_auto_save_session_metadata).
                if self._session_dir_available():
                    self._write_session_metadata(metadata)
                    self.logger.info(f"Session metadata saved: {metadata}")
                else:
                    self.logger.info(
                        f"Session metadata buffered until recording starts: "
                        f"{metadata}")

            elif action == 'set_capture_params':
                self._handle_set_capture_params(command.get('params') or {})

            elif action == 'list_data_sessions':
                threading.Thread(
                    target=self._list_data_sessions,
                    daemon=True
                ).start()

            elif action == 'transfer_session':
                # session_id (YYYYMMDD/session_HHMMSS) preferred; session_date (YYYYMMDD) for legacy
                session_date = command.get('session_id') or command.get('session_date', '')
                data_types = command.get('data_types', ['images', 'logs', 'tracks', 'config'])
                upload_url = command.get('upload_url', '')
                request_id = str(command.get('request_id', ''))
                # Optional presigned multipart descriptor (upload_id, part_size, parts)
                upload = command.get('upload')
                # POST (default) = controller endpoint; PUT = S3-style presigned URL
                upload_method = str(command.get('method', 'POST')).upper()
                if upload is not None and not _valid_multipart(upload):
                    self.logger.error("transfer_session invalid upload object (needs parts list + part_size > 0)")
                elif upload_method not in ('POST', 'PUT'):
                    self.logger.error(f"transfer_session invalid method: {upload_method}")
                elif session_date and (upload_url or upload):
                    threading.Thread(
                        target=self._upload_session,
                        args=(session_date, data_types, upload_url, upload_method),
                        kwargs={'request_id': request_id, 'upload': upload},
                        daemon=True
                    ).start()
                else:
                    self.logger.error("transfer_session missing session_date or upload_url")

            elif action == 'delete_session':
                session_date = command.get('session_id') or command.get('session_date', '')
                data_types = command.get('data_types', ['images'])
                if session_date:
                    threading.Thread(
                        target=self._delete_session,
                        args=(session_date, data_types),
                        daemon=True
                    ).start()
                else:
                    self.logger.error("delete_session missing session_date")

            elif action == 'upload_previews':
                request_id = str(command.get('request_id', ''))
                session_id = command.get('session_id', '')
                upload_urls = command.get('upload_urls', [])
                try:
                    count = int(command.get('count', 5))
                    max_dimension = int(command.get('max_dimension', 1280))
                except (TypeError, ValueError):
                    count, max_dimension = 0, 0
                if (session_id and isinstance(upload_urls, list) and upload_urls
                        and count > 0 and max_dimension > 0):
                    threading.Thread(
                        target=self._upload_previews,
                        args=(request_id, session_id, count, max_dimension, upload_urls),
                        daemon=True
                    ).start()
                else:
                    self.logger.error("upload_previews missing/invalid session_id, upload_urls, count or max_dimension")

            elif action == 'update_software':
                self._handle_update_software(command)

            elif action == 'reboot':
                self.logger.warning("Reboot command received")
                self._handle_reboot()

            elif action == 'restart_service':
                self.logger.warning("Service restart command received")
                self._handle_restart_service()

            elif action == 'shutdown':
                self.logger.warning("Shutdown command received")
                self._handle_shutdown()

            else:
                # Silent no-op handlers are invisible failures — always warn.
                self.logger.warning(f"Unknown MQTT command action: {action!r}; no handler registered (command ignored)")

            # Update timestamp and publish new state
            self.state['last_update'] = time.time()
            self._publish_state()

    def _geometry_ini_path(self):
        """Path of the device-resident GEOMETRY.ini (repo config/ dir)."""
        config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config')
        return os.path.join(config_dir, GEOMETRY_FILE)

    def _handle_get_config(self):
        """Read current config from disk and publish as JSON to config topic"""
        try:
            config_path = self._resolve_config_path()
            if not config_path:
                self.logger.error("Cannot get config - no config file path available")
                return

            with self.config_lock:
                config = configparser.ConfigParser()
                config.read(config_path)
                # Merge device-resident mount geometry last so it wins, mirroring
                # owl.py's load order. Without this the geometry editor seeds from
                # defaults and a subsequent save clobbers GEOMETRY.ini.
                config.read(self._geometry_ini_path())

            # Convert to dict
            config_dict = {}
            for section in config.sections():
                config_dict[section] = dict(config[section])

            basename = os.path.basename(config_path)
            unsaved, source = config_unsaved_state(config_path)
            payload = {
                'config': config_dict,
                'config_path': str(config_path),
                'config_name': basename,
                'config_unsaved': unsaved,
                'config_source': source,
                'device_id': self.device_id,
                'timestamp': time.time()
            }

            self.client.publish(self.topics['config'], json.dumps(payload), retain=False)
            self.logger.info(f"Published config from {config_path} ({len(config_dict)} sections)")

        except Exception as e:
            self.logger.error(f"Error handling get_config: {e}")


    def _handle_set_config_section(self, section, params):
        """Update multiple parameters in a section on the live OWL instance"""
        if self.owl_instance is None:
            self.logger.warning("Cannot set config section - OWL instance not set")
            return

        # A relay, LED or switch on a pin this OWL's GPS hardware uses is refused
        # whole, whichever interface sent it. The dashboards check first; this
        # is the authority for the networked controller and the agent.
        config = getattr(self.owl_instance, 'config', None)
        protected = ConfigValidator.protected_pins(config) if config is not None else {}
        blocked = ConfigValidator.protected_pin_errors({section: params or {}}, protected)
        if blocked:
            self.logger.warning(
                f"Rejected config change: {ConfigValidator.format_protected_pin_errors(blocked)}")
            return

        try:
            gob_changed = False
            geometry_changed = False
            restart_keys = set()
            camera_live_params = {}
            rejected_keys = set()
            for key, value in params.items():
                if key in GEOMETRY_KEYS:
                    geometry_changed = True
                # Route threshold keys through _update_greenonbrown_param
                # regardless of section name (defense-in-depth)
                if key in GREENONBROWN_PARAMS:
                    self._update_greenonbrown_param(key, value)
                    gob_changed = True
                elif section == 'GreenOnGreen':
                    self._update_greenongreen_param(key, value)
                elif section == 'System' and key == 'algorithm':
                    # Route algorithm changes through set_algorithm handler
                    self._handle_command({'action': 'set_algorithm', 'value': value})
                elif key in RESTART_REQUIRED_KEYS:
                    # Can't apply live — applying relay_num/resolution to the running
                    # instance would leave lane/camera state inconsistent. Persist to
                    # config below and flag a restart instead. Only a REAL value
                    # change is flagged: the config tab's "Apply to OWLs" resends
                    # unchanged keys on every press, and those must not re-raise
                    # the restart notice.
                    with self.config_lock:
                        cfg = getattr(self.owl_instance, 'config', None)
                        current = (cfg.get(section, key)
                                   if cfg is not None and cfg.has_option(section, key)
                                   else None)
                    if current is None or str(current).strip() != str(value).strip():
                        restart_keys.add(key)
                        self.logger.info(f"{section}.{key} change needs a restart to take effect")
                elif key in CAMERA_LIVE_KEYS:
                    # Coerce and validate here — a value that fails validation
                    # must be neither applied nor persisted (it would crash the
                    # next boot's config load).
                    try:
                        typed = ConfigValidator.coerce_camera_live_value(key, value)
                    except (ValueError, TypeError) as e:
                        self.logger.warning(f"Rejected {section}.{key}={value}: {e}")
                        rejected_keys.add(key)
                        continue
                    setattr(self.owl_instance, key, typed)
                    camera_live_params[key] = typed
                elif hasattr(self.owl_instance, key):
                    # Type-convert to match existing attribute type (INI values are strings)
                    current = getattr(self.owl_instance, key)
                    try:
                        if isinstance(current, bool):
                            typed = str(value).lower() in ('true', '1', 'yes')
                        elif isinstance(current, int):
                            typed = int(float(value))
                        elif isinstance(current, float):
                            typed = float(value)
                        else:
                            typed = value
                        setattr(self.owl_instance, key, typed)
                        self.logger.info(f"Set {section}.{key} = {typed} on live instance")
                    except (ValueError, TypeError) as e:
                        self.logger.warning(f"Cannot convert {section}.{key}={value}: {e}")

                # Also update the config object for persistence (non-GoB keys only;
                # _update_greenonbrown_param handles GoB config internally).
                # Camera live keys persist their coerced/clamped value so the
                # file always passes validation on the next boot.
                if key not in GREENONBROWN_PARAMS:
                    persist_value = camera_live_params.get(key, value) \
                        if key in CAMERA_LIVE_KEYS else value
                    with self.config_lock:
                        if hasattr(self.owl_instance, 'config'):
                            if not self.owl_instance.config.has_section(section):
                                self.owl_instance.config.add_section(section)
                            self.owl_instance.config.set(section, key, str(persist_value))

            self.logger.info(f"Applied {len(params)} params to [{section}]")

            # Push ByteTrack params to live tracker when Tracking section changes
            if section == 'Tracking':
                # Route tracking_enabled through set_tracking handler (creates
                # ClassSmoother/CropMaskStabilizer and sets gog.tracking_enabled)
                if 'tracking_enabled' in params:
                    self._handle_command({
                        'action': 'set_tracking',
                        'value': params['tracking_enabled']
                    })

                tracker_keys = {'track_high_thresh', 'track_low_thresh',
                                'new_track_thresh', 'track_buffer', 'match_thresh'}
                if tracker_keys & set(params.keys()):
                    gog = getattr(self.owl_instance, '_gog_detector', None)
                    if gog and hasattr(gog, 'update_tracker_params_direct'):
                        tracker_params = {}
                        for k in tracker_keys:
                            val = getattr(self.owl_instance, k, None)
                            if val is not None:
                                tracker_params[k] = val
                        gog.update_tracker_params_direct(tracker_params)

                if 'detection_persist_frames' in params:
                    gog = getattr(self.owl_instance, '_gog_detector', None)
                    if gog:
                        gog.detection_persist_frames = getattr(
                            self.owl_instance, 'detection_persist_frames', 0)

            # Hot-apply WB/exposure to the running camera (picamera2 queues
            # control updates internally — safe from this MQTT thread).
            if camera_live_params:
                cam = getattr(self.owl_instance, 'cam', None)
                if cam is not None and hasattr(cam, 'set_camera_controls'):
                    try:
                        cam.set_camera_controls(camera_live_params)
                    except Exception as e:
                        self.logger.error(f"Error applying live camera controls: {e}")
                else:
                    self.logger.info("Camera backend does not support live control "
                                     "updates; values persisted for next start")

            # Re-derive crop slice / lane coords / actuation band on the live
            # instance — event-driven, once per change, never per-frame.
            if geometry_changed and hasattr(self.owl_instance, 'recompute_geometry'):
                try:
                    self.owl_instance.recompute_geometry()
                    self.logger.info("Recomputed geometry after live config change")
                except Exception as e:
                    self.logger.error(f"Error recomputing geometry: {e}")

            # Surface a restart-required notice for keys that can't apply live.
            if restart_keys:
                existing = set(filter(None, str(self.state.get('restart_required', '')).split(',')))
                self.state['restart_required'] = ','.join(sorted(existing | restart_keys))

            # Ensure dashboard sees the changes immediately
            self._sync_parameters_to_state()
            self._publish_state()

            # Auto-persist GreenOnBrown threshold changes (copy-on-write safe)
            if gob_changed:
                self._handle_save_config()

        except Exception as e:
            self.logger.error(f"Error setting config section [{section}]: {e}")

    def _apply_meta(self, name=None, notes=None):
        """Stamp a [Meta] section onto the live config so a saved file carries its
        human name/notes. Only writes fields that are actually provided — never
        invents a name (fail-safe metadata)."""
        if name is None and notes is None:
            return
        if not hasattr(self.owl_instance, 'config'):
            return
        cfg = self.owl_instance.config
        with self.config_lock:
            if not cfg.has_section('Meta'):
                cfg.add_section('Meta')
            if name is not None:
                cfg.set('Meta', 'display_name', str(name))
            if notes is not None:
                cfg.set('Meta', 'notes', str(notes))
            from datetime import datetime
            cfg.set('Meta', 'created', datetime.now().isoformat(timespec='seconds'))

    def _handle_save_config(self, filename=None, name=None, notes=None):
        """Write current config to disk.

        Copy-on-write: if the resolved path is a protected default
        (GENERAL_CONFIG.ini, CONTROLLER.ini), creates a timestamped copy
        and updates active_config.txt so the default is never overwritten.

        If name/notes are provided, a [Meta] section is stamped first so the
        saved file carries a human-readable name.
        """
        try:
            if not hasattr(self.owl_instance, 'config'):
                self.logger.error("Cannot save config - OWL has no config object")
                return

            self._apply_meta(name, notes)

            config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config')
            protected = ['GENERAL_CONFIG.ini', 'CONTROLLER.ini', GEOMETRY_FILE]

            if filename:
                # Save to a new file in the config directory. Basename only — a
                # client-supplied filename must never escape config/ via traversal.
                basename = os.path.basename(filename)
                save_path = os.path.join(config_dir, basename)

                # Safety: don't overwrite default/infrastructure presets
                if basename in protected:
                    self.logger.error(f"Cannot overwrite default preset: {basename}")
                    return

                # Named presets never carry working-copy provenance
                cfg = self.owl_instance.config
                if cfg.has_section('Meta') and cfg.has_option('Meta', 'source'):
                    cfg.remove_option('Meta', 'source')
            else:
                save_path = self._resolve_config_path()
                if not save_path:
                    self.logger.error("Cannot save config - no config file path")
                    return

                # Frozen-preset model: live (unsaved) changes never touch the
                # loaded file — templates AND named presets alike. They divert
                # to the single autosave working file, overwritten in place.
                basename = os.path.basename(save_path)
                if basename != AUTOSAVE_CONFIG:
                    save_path = os.path.join(config_dir, AUTOSAVE_CONFIG)
                    self.logger.info(
                        f"Unsaved change to {basename}; writing to {AUTOSAVE_CONFIG}")

                    # Record which file the working copy derives from
                    cfg = self.owl_instance.config
                    if not cfg.has_section('Meta'):
                        cfg.add_section('Meta')
                    cfg.set('Meta', 'source', basename)

                    # Update active_config.txt to point to the autosave file
                    self._handle_set_active_config(f'config/{AUTOSAVE_CONFIG}',
                                                   seed=False)
                else:
                    # Booted from the autosave, but the dashboard may have
                    # repointed active_config.txt at a named profile since
                    # (a profile switch re-seeds the autosave and pushes the
                    # values live, without a restart). A live change written
                    # here must survive the next boot, so the pointer has to
                    # follow the working file; carry the on-disk provenance
                    # across so the dashboard keeps showing "<profile> -
                    # unsaved changes" rather than the boot-time source.
                    pointer_path = os.path.join(config_dir, 'active_config.txt')
                    try:
                        with open(pointer_path) as f:
                            pointed = os.path.basename(f.read().strip())
                    except OSError:
                        pointed = ''
                    if pointed and pointed != AUTOSAVE_CONFIG:
                        on_disk = configparser.ConfigParser()
                        on_disk.optionxform = str
                        on_disk.read(save_path)
                        source = (on_disk.get('Meta', 'source', fallback=None)
                                  if on_disk.has_section('Meta') else None)
                        cfg = self.owl_instance.config
                        with self.config_lock:
                            if not cfg.has_section('Meta'):
                                cfg.add_section('Meta')
                            cfg.set('Meta', 'source', source or pointed)
                        self.logger.info(
                            f"active_config.txt pointed at {pointed}; repointing "
                            f"to {AUTOSAVE_CONFIG} so live changes persist")
                        self._handle_set_active_config(f'config/{AUTOSAVE_CONFIG}',
                                                       seed=False)

            with self.config_lock:
                atomic_write_config(
                    save_path,
                    lambda f: self._write_config_without_geometry(self.owl_instance.config, f))

            self.logger.info(f"Config saved to {save_path}")

            if filename:
                # The named file IS the running config now — point the active
                # pointer at it (re-seeding the working copy to mirror it) so
                # the dashboard stops showing "<old profile> - unsaved changes"
                # and a reboot comes back with exactly what was saved.
                self._handle_set_active_config(f'config/{os.path.basename(save_path)}')
                self._sync_parameters_to_state()
                self._publish_state()

        except Exception as e:
            self.logger.error(f"Error saving config: {e}")

    def _write_config_without_geometry(self, config, fileobj):
        """Write a config to fileobj with mount-geometry keys removed, so named
        detection configs never carry geometry (it lives in GEOMETRY.ini).

        Also drops the legacy min_detection_area px key from any section whose
        canonical percent key carries a real value — files migrate to
        percent-only on save (applies to [GreenOnBrown] and [Sensitivity_*])."""
        tmp = configparser.ConfigParser()
        tmp.optionxform = str
        for section in config.sections():
            tmp.add_section(section)
            geom = GEOMETRY_SECTION_KEYS.get(section, set())
            try:
                drop_legacy_px = config.getfloat(
                    section, 'min_detection_area_percent', fallback=0.0) > 0
            except ValueError:
                drop_legacy_px = False
            for opt in config.options(section):
                if opt in geom:
                    continue
                if drop_legacy_px and opt == 'min_detection_area':
                    continue
                tmp.set(section, opt, config.get(section, opt, raw=True))
        tmp.write(fileobj)

    def _handle_save_geometry(self, params):
        """Apply geometry params live and persist ONLY them to GEOMETRY.ini.

        Geometry is per-unit mount config — written in place (atomic temp+replace),
        never copy-on-write, and kept out of named detection configs.
        """
        if self.owl_instance is None:
            self.logger.warning("Cannot save geometry - OWL instance not set")
            return
        try:
            applied = {}
            for key, value in params.items():
                if key not in MOUNT_GEOMETRY_KEYS:
                    continue
                try:
                    setattr(self.owl_instance, key, float(value))
                    applied[key] = float(value)
                except (ValueError, TypeError):
                    self.logger.warning(f"Bad geometry value {key}={value}")

            # Also mirror into the live merged config so a subsequent get_config is correct.
            if hasattr(self.owl_instance, 'config'):
                with self.config_lock:
                    for section, keys in GEOMETRY_SECTION_KEYS.items():
                        for key in keys:
                            if key in applied:
                                if not self.owl_instance.config.has_section(section):
                                    self.owl_instance.config.add_section(section)
                                self.owl_instance.config.set(section, key, str(applied[key]))

            if hasattr(self.owl_instance, 'recompute_geometry'):
                self.owl_instance.recompute_geometry()

            # Persist only the geometry keys to GEOMETRY.ini (in place, atomic).
            geom_path = self._geometry_ini_path()
            config_dir = os.path.dirname(geom_path)
            cp = configparser.ConfigParser()
            cp.optionxform = str
            cp.read(geom_path)
            for section, keys in GEOMETRY_SECTION_KEYS.items():
                for key in keys:
                    if key in applied:
                        if not cp.has_section(section):
                            cp.add_section(section)
                        cp.set(section, key, str(applied[key]))
            with self.config_lock:
                fd, tmp_path = tempfile.mkstemp(suffix='.ini', prefix='.owl_geom_', dir=config_dir)
                with os.fdopen(fd, 'w') as f:
                    cp.write(f)
                os.replace(tmp_path, geom_path)

            self.logger.info(f"Geometry persisted to {GEOMETRY_FILE}: {applied}")
            self._sync_parameters_to_state()
            self._publish_state()
        except Exception as e:
            self.logger.error(f"Error saving geometry: {e}")

    def _handle_set_active_config(self, config_path, seed=True):
        """Write config path to active_config.txt.

        Loading a config also re-seeds the autosave working file from it
        (Word-doc model: the working copy always mirrors what was loaded;
        subsequent live changes diverge only in the working copy)."""
        try:
            config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config')
            active_path = os.path.join(config_dir, 'active_config.txt')

            with self.config_lock:
                with open(active_path, 'w') as f:
                    f.write(config_path.strip() + '\n')
                if seed:
                    seed_autosave(config_dir,
                                  os.path.join(config_dir,
                                               os.path.basename(config_path.strip())))

            self.logger.info(f"Active config set to: {config_path}")

        except Exception as e:
            self.logger.error(f"Error setting active config: {e}")

    def _handle_restart_service(self):
        """Restart the owl.service via systemctl (same mechanism as standalone)"""
        try:
            import subprocess
            self.logger.warning("Restarting owl.service...")
            subprocess.Popen(
                ['sudo', 'systemctl', 'restart', 'owl.service'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            self.logger.error(f"Error restarting service: {e}")

    def _run_power_command(self, action, argv):
        """Run a sudo power command and LOG failures.

        Popen with DEVNULL made a sudoers miss completely invisible — the
        command 'succeeded' and the box just never went down. Blocking here
        is fine: the system is about to go down anyway, and the caller is
        the MQTT callback thread.
        """
        import subprocess
        try:
            result = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=15)
            if result.returncode != 0:
                self.logger.error(
                    f"{action} command failed (rc={result.returncode}): "
                    f"{(result.stderr or '').strip()}")
        except Exception as e:
            self.logger.error(f"Error running {action}: {e}")

    def _handle_shutdown(self):
        """Shut down the system (called via MQTT from central controller).

        Resolves shutdown binary path to match the sudoers entry created
        by controller/shared/setup.sh (command -v shutdown).
        """
        import shutil
        shutdown_bin = shutil.which('shutdown') or '/usr/sbin/shutdown'
        self.logger.warning("Shutting down system...")
        self._run_power_command('shutdown',
                                ['sudo', '-n', shutdown_bin, 'now'])

    def _handle_reboot(self):
        """Reboot the system (called via MQTT).

        Resolves the reboot binary path to match the sudoers entry created
        by the setup/provisioning scripts (command -v reboot).
        """
        import shutil
        reboot_bin = shutil.which('reboot') or '/usr/sbin/reboot'
        self.logger.warning("Rebooting system...")
        self._run_power_command('reboot', ['sudo', '-n', reboot_bin])

    # Software update — shared with the dashboard's update routes
    # (utils/software_update.py owns the argv, ref allowlist and status file)
    UPDATE_REF_PATTERN = software_update.UPDATE_REF_PATTERN
    UPDATE_TERMINAL_STATUSES = software_update.TERMINAL_STATUSES

    def _handle_update_software(self, command):
        """Launch owl_update.sh in a transient systemd unit.

        The script must run OUTSIDE owl.service's cgroup or systemd kills it
        at its own restart step — hence systemd-run. The argv comes from
        software_update.update_argv and must match the sudoers entry
        installed by setup.sh / owl_cloud_provision.sh byte-for-byte.
        Progress flows back via the status file read each heartbeat.
        """
        import subprocess

        ref = str(command.get('ref', 'main'))
        request_id = str(command.get('request_id', ''))

        def _refuse(msg):
            self.logger.warning(f"update_software refused: {msg}")
            with self.state_lock:
                self.state['software_update'] = {
                    'request_id': request_id, 'status': 'error', 'ref': ref,
                    'from': '', 'to': '', 'error': msg,
                    'rollback_failed': False, 'updated_at': int(time.time())
                }
            self._publish_state()

        if not software_update.valid_ref(ref):
            _refuse(f"invalid ref: {ref!r}")
            return
        with self.state_lock:
            update_status = self.state['software_update'].get('status', 'idle')
            transfer_status = self.state['data_transfer'].get('status', 'idle')
        if update_status not in self.UPDATE_TERMINAL_STATUSES:
            _refuse(f"update already in progress (status: {update_status})")
            return
        if transfer_status not in ('idle', 'complete', 'error'):
            _refuse(f"data transfer in progress (status: {transfer_status})")
            return

        repo_dir = Path(__file__).resolve().parents[1]
        script = repo_dir / 'owl_update.sh'
        if not script.exists():
            _refuse(f"updater not found: {script}")
            return

        # Seed state so the dashboard/cloud sees acceptance immediately —
        # the script overwrites this via the status file within seconds.
        with self.state_lock:
            self.state['software_update'] = {
                'request_id': request_id, 'status': 'starting', 'ref': ref,
                'from': '', 'to': '', 'error': '',
                'rollback_failed': False, 'updated_at': int(time.time())
            }
        self._publish_state()

        try:
            argv = software_update.update_argv(
                ref, request_id, script=script,
                status_file=self._update_status_path)
            software_update.launch(argv, subprocess.Popen)
            self.logger.warning(f"Software update launched: ref={ref} request_id={request_id}")
        except Exception as e:
            _refuse(f"failed to launch updater: {e}")

    def _read_update_status(self):
        """Mirror owl_update.sh's status file into state['software_update'].

        Called every heartbeat and once at start(); after a service restart
        this is how the new process picks up and publishes the terminal
        complete/rolled_back status. The file is written via temp+rename so
        a partial read is not possible.
        """
        try:
            st = self._update_status_path.stat()
        except OSError:
            return  # no update has ever run
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._update_status_stamp:
            return
        data = software_update.read_status(self._update_status_path)
        if data is None:
            self.logger.debug("Could not read update status file")
            return  # keep previous state, retry next heartbeat
        self._update_status_stamp = stamp
        with self.state_lock:
            self.state['software_update'] = data

    def _populate_version_info(self):
        """Fill version/git/hardware fields in state. Never raises —
        MQTT startup must not depend on git or /proc being available."""
        try:
            from version import VERSION, SystemInfo
            self.state['version'] = str(VERSION)
            git_info = SystemInfo.get_git_info()
            if git_info:
                self.state['git_branch'] = git_info.get('branch', 'unknown')
                self.state['git_commit'] = git_info.get('commit', 'unknown')
            pi_model = SystemInfo.get_rpi_info()
            if pi_model:
                self.state['pi_model'] = pi_model
            try:
                with open('/etc/os-release') as f:
                    for line in f:
                        if line.startswith('PRETTY_NAME='):
                            self.state['os_pretty'] = line.split('=', 1)[1].strip().strip('"')
                            break
            except OSError:
                pass
        except Exception as e:
            self.logger.warning(f"Could not populate version info: {e}")

    def _handle_set_actuation_params(self, command):
        """Timing broadcast from the networked controller.

        An OWL timing itself from its own fresh GPS fix keeps its duration and
        delay (device GPS always wins) and only adopts the controller's ground
        geometry. Otherwise the controller's timing applies, as before.
        """
        from utils.config_manager import ConfigValidator

        try:
            duration = float(command.get('actuation_duration', 0))
            delay = float(command.get('delay', 0))
            speed = command.get('speed_kmh')
            speed = float(speed) if speed is not None else None

            # Clamp to safety bounds — shared with the config-read path so the
            # two cannot drift into disagreeing about what is safe
            duration, delay = ConfigValidator.clamp_actuation(duration, delay)

            owl = self.owl_instance
            if owl is not None:
                owl._controller_timing_at = time.monotonic()
                owl._controller_speed_kmh = speed
                calc = getattr(owl, '_actuation_calc', None)
                if calc is not None:
                    if command.get('actuation_length_cm') is not None:
                        calc.actuation_length_cm = float(command['actuation_length_cm'])
                    if command.get('offset_cm') is not None:
                        calc.offset_cm = float(command['offset_cm'])
                if getattr(owl, '_device_timing', False) is True:
                    return
                if getattr(owl, 'actuation_source', None) == 'config':
                    # Remember the configured timing to restore if the
                    # controller goes quiet
                    owl._config_actuation = (owl.actuation_duration, owl.delay)
                owl.actuation_duration = duration
                owl.delay = delay
                owl.actuation_source = 'controller'

            prev_duration = self.state.get('actuation_duration')
            prev_delay = self.state.get('delay')

            self.state['actuation_duration'] = duration
            self.state['delay'] = delay
            self.state['actuation_source'] = 'controller'

            if duration != prev_duration or delay != prev_delay:
                self.logger.info(f"Actuation params updated: duration={duration:.4f}s, "
                                 f"delay={delay:.4f}s, source=controller")

        except (ValueError, TypeError) as e:
            self.logger.error(f"Invalid actuation params: {e}")

    AWB_CALIBRATION_SETTLE_S = 2.0

    def _set_awb_calibration(self, **fields):
        with self.state_lock:
            self.state['awb_calibration'].update(fields, updated_at=time.time())
        self._publish_state()

    def _calibrate_awb(self):
        """Sample auto white balance, then lock it as manual gains.

        Farmer flow: point the camera at bare soil or a grey card, tap once.
        Runs auto AWB for AWB_CALIBRATION_SETTLE_S, reads ColourGains from
        the latest frame metadata (owl._last_camera_metadata, written by the
        main loop; read_with_metadata() is consuming and must not be called
        here), then applies awb_mode = manual with those gains through the
        normal set_config_section path and saves the config. Any failure
        restores the previous WB state. Runs in a background thread.
        """
        owl = self.owl_instance
        cam = getattr(owl, 'cam', None) if owl is not None else None
        if cam is None or not hasattr(cam, 'set_camera_controls'):
            self._set_awb_calibration(
                status='error',
                error='Camera backend has no live white-balance control')
            return

        previous = {
            'awb_mode': getattr(owl, 'awb_mode', 'auto'),
            'awb_red_gain': getattr(owl, 'awb_red_gain', 2.0),
            'awb_blue_gain': getattr(owl, 'awb_blue_gain', 2.0),
        }
        try:
            cam.set_camera_controls({'awb_mode': 'auto'})
            time.sleep(self.AWB_CALIBRATION_SETTLE_S)

            metadata = getattr(owl, '_last_camera_metadata', None) or {}
            gains = metadata.get('ColourGains')
            if not gains or len(gains) < 2:
                raise ValueError('No ColourGains in camera metadata')
            red_gain = round(float(gains[0]), 3)
            blue_gain = round(float(gains[1]), 3)
            colour_temperature = metadata.get('ColourTemperature')

            self._handle_set_config_section('Camera', {
                'awb_mode': 'manual',
                'awb_red_gain': red_gain,
                'awb_blue_gain': blue_gain,
            })
            # Camera keys are not written to disk by set_config_section
            # (only GoB thresholds auto-save); the lock must survive a restart.
            self._handle_save_config()
            self.logger.info(
                "White balance calibrated and locked",
                extra=dict(awb_red_gain=red_gain, awb_blue_gain=blue_gain,
                           colour_temperature=colour_temperature))
            self._set_awb_calibration(
                status='complete', red_gain=red_gain, blue_gain=blue_gain,
                colour_temperature=colour_temperature, error='')

        except Exception as e:
            self.logger.error(f"White balance calibration failed: {e}")
            try:
                cam.set_camera_controls(previous)
            except Exception as restore_error:
                self.logger.error(
                    f"Could not restore white balance after failed "
                    f"calibration: {restore_error}")
            self._set_awb_calibration(status='error', error=str(e))

    def _download_model(self, url, filename, expected_sha256, is_archive):
        """Download a model file from the controller. Runs in background thread."""
        import urllib.request
        import ssl
        import hashlib
        import tempfile

        models_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'models')
        os.makedirs(models_dir, exist_ok=True)

        tmp_path = None
        try:
            # Update state: downloading
            with self.state_lock:
                self.state['model_download'] = {
                    'status': 'downloading',
                    'model_name': filename,
                    'progress': 0,
                    'error': ''
                }
            self._publish_state()

            # Where it lands, checked before anything is fetched or deleted
            if is_archive:
                dir_name = filename.rsplit('.', 1)[0] if '.' in filename else filename
                target = model_path_in(models_dir, dir_name)
            else:
                target = model_path_in(models_dir, filename)
            if target is None:
                raise ValueError(f'not a model file name: {filename!r}')

            # Verified TLS unless the host is the LAN controller (self-signed)
            ssl_ctx = ssl_context_for_url(url)

            req = urllib.request.Request(url)
            response = urllib.request.urlopen(req, context=ssl_ctx)
            total_size = int(response.headers.get('Content-Length', 0))

            # Download to temp file
            tmp_fd, tmp_path = tempfile.mkstemp(dir=models_dir, suffix='.tmp')
            downloaded = 0
            last_progress_update = 0

            with os.fdopen(tmp_fd, 'wb') as f:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)

                    # Update progress every 100KB
                    if downloaded - last_progress_update >= 102400:
                        last_progress_update = downloaded
                        progress = int((downloaded / total_size * 100)) if total_size > 0 else 0
                        with self.state_lock:
                            self.state['model_download']['progress'] = progress
                        self._publish_state()

            # Place the file, then verify SHA256
            if is_archive:
                # Extract zip to a directory
                import zipfile
                extract_dir = target
                if os.path.isdir(extract_dir):
                    import shutil
                    shutil.rmtree(extract_dir)
                os.makedirs(extract_dir)

                with zipfile.ZipFile(tmp_path, 'r') as zf:
                    zf.extractall(extract_dir)

                os.unlink(tmp_path)
                tmp_path = None

                # Verify SHA256 of extracted directory contents (matches
                # how the controller computed it at upload time)
                if expected_sha256:
                    h = hashlib.sha256()
                    for fp in sorted(Path(extract_dir).rglob('*')):
                        if fp.is_file():
                            with open(fp, 'rb') as hf:
                                while True:
                                    chunk = hf.read(65536)
                                    if not chunk:
                                        break
                                    h.update(chunk)
                    actual_sha256 = h.hexdigest()
                    if actual_sha256 != expected_sha256:
                        import shutil
                        shutil.rmtree(extract_dir)
                        raise ValueError(
                            f'SHA256 mismatch: expected {expected_sha256[:12]}..., '
                            f'got {actual_sha256[:12]}...'
                        )
                self.logger.info(f"Model extracted to {extract_dir}")
            else:
                # Verify SHA256 of the file directly
                if expected_sha256:
                    h = hashlib.sha256()
                    with open(tmp_path, 'rb') as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            h.update(chunk)
                    actual_sha256 = h.hexdigest()
                    if actual_sha256 != expected_sha256:
                        raise ValueError(
                            f'SHA256 mismatch: expected {expected_sha256[:12]}..., '
                            f'got {actual_sha256[:12]}...'
                        )

                # Atomic rename
                final_path = target
                if os.path.exists(final_path):
                    os.unlink(final_path)
                os.rename(tmp_path, final_path)
                tmp_path = None
                self.logger.info(f"Model saved to {final_path}")

            # Update state: complete
            with self.state_lock:
                self.state['model_download'] = {
                    'status': 'complete',
                    'model_name': filename,
                    'progress': 100,
                    'error': ''
                }
                # Refresh available models list
                self.state['available_models'] = self._list_available_models()
            self._publish_state()

        except Exception as e:
            self.logger.error(f"Model download failed: {e}")
            # Cleanup temp file
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

            with self.state_lock:
                self.state['model_download'] = {
                    'status': 'error',
                    'model_name': filename,
                    'progress': 0,
                    'error': str(e)
                }
            self._publish_state()

    def _download_lut_profile(self, url, filename, expected_sha256,
                              apply_profile=False, apply_sensitivity=50):
        """Download a painted LUT profile from the controller. Background thread.

        Profiles are small (<1MB) .npz files — no progress ticks, just
        pending/complete/error via state['lut_download']. With
        *apply_profile* the profile (and the lut algorithm) activate once
        the download verifies.
        """
        import urllib.request
        import ssl
        import hashlib
        import tempfile

        lut_mgr = getattr(self.owl_instance, 'lut_manager', None) \
            if self.owl_instance else None
        if lut_mgr is not None:
            profiles_dir = lut_mgr.profile_dir
        else:
            profiles_dir = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), 'config', 'lut_profiles')
        os.makedirs(profiles_dir, exist_ok=True)

        tmp_path = None
        try:
            with self.state_lock:
                self.state['lut_download'] = {
                    'status': 'downloading', 'profile': filename, 'error': ''}
            self._publish_state()

            # Verified TLS unless the host is the LAN controller (same rule as models)
            ssl_ctx = ssl_context_for_url(url)

            response = urllib.request.urlopen(
                urllib.request.Request(url), context=ssl_ctx)

            tmp_fd, tmp_path = tempfile.mkstemp(dir=profiles_dir, suffix='.tmp')
            h = hashlib.sha256()
            with os.fdopen(tmp_fd, 'wb') as f:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)

            if expected_sha256 and h.hexdigest() != expected_sha256:
                raise ValueError(
                    f'SHA256 mismatch: expected {expected_sha256[:12]}..., '
                    f'got {h.hexdigest()[:12]}...')

            final_path = os.path.join(profiles_dir, os.path.basename(filename))
            os.replace(tmp_path, final_path)
            tmp_path = None
            self.logger.info(f"LUT profile saved to {final_path}")

            with self.state_lock:
                self.state['lut_download'] = {
                    'status': 'complete', 'profile': filename, 'error': ''}
                if apply_profile and self.owl_instance:
                    name = os.path.basename(filename)
                    if name.endswith('.npz'):
                        name = name[:-4]
                    self.state['lut_profile'] = name
                    self.state['lut_sensitivity'] = apply_sensitivity
                    self.state['algorithm'] = 'lut'
                    self.owl_instance._pending_lut_sensitivity = apply_sensitivity
                    self.owl_instance._pending_lut_profile = name
                    self.owl_instance._pending_algorithm = 'lut'
                    if hasattr(self.owl_instance, 'config'):
                        self.owl_instance.config.set('GreenOnBrown', 'lut_profile', name)
                        self.owl_instance.config.set('GreenOnBrown', 'lut_sensitivity',
                                                     str(apply_sensitivity))
                        self.owl_instance.config.set('System', 'algorithm', 'lut')
                    self.logger.info(f"LUT profile '{name}' queued for activation "
                                     f"(sensitivity {apply_sensitivity})")
            self._sync_parameters_to_state()
            self._publish_state()

        except Exception as e:
            self.logger.error(f"LUT profile download failed: {e}")
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            with self.state_lock:
                self.state['lut_download'] = {
                    'status': 'error', 'profile': filename, 'error': str(e)}
            self._publish_state()

    def _session_dir_available(self):
        """True only when an ACTIVE recorder session directory exists.

        The most-recent-date-dir fallback inside _write_session_metadata is
        for the recording-stop path; using it for a pre-start metadata write
        would stamp the metadata into the previous session.
        """
        if not self.owl_instance:
            return False
        recorder = getattr(self.owl_instance, 'image_recorder', None)
        if not recorder:
            return False
        rec_dir = getattr(recorder, 'save_directory', None)
        return bool(rec_dir and os.path.isdir(rec_dir))

    def _write_session_metadata(self, metadata):
        """Write session_metadata.json to the active session directory.

        Uses the ImageRecorder's save_directory (the active session subfolder)
        if available, otherwise falls back to the most recent date directory.
        """
        # Prefer the active session dir from ImageRecorder
        session_path = None
        if self.owl_instance:
            recorder = getattr(self.owl_instance, 'image_recorder', None)
            if recorder:
                rec_dir = getattr(recorder, 'save_directory', None)
                if rec_dir and os.path.isdir(rec_dir):
                    session_path = rec_dir

        # Fallback: find most recent YYYYMMDD dir (or session subdir within it)
        if not session_path:
            save_dir = getattr(self.owl_instance, 'save_directory', None) if self.owl_instance else None
            if not save_dir or not os.path.isdir(save_dir):
                self.logger.warning("Cannot write session metadata: save_directory not available")
                return

            import re
            date_pattern = re.compile(r'^\d{8}$')
            session_pattern = re.compile(r'^session_\d{6}$')

            for date_dir in sorted(os.listdir(save_dir), reverse=True):
                date_path = os.path.join(save_dir, date_dir)
                if not os.path.isdir(date_path) or not date_pattern.match(date_dir):
                    continue
                # Check for session subdirs
                subdirs = sorted([d for d in os.listdir(date_path)
                                  if os.path.isdir(os.path.join(date_path, d)) and session_pattern.match(d)],
                                 reverse=True)
                if subdirs:
                    session_path = os.path.join(date_path, subdirs[0])
                else:
                    session_path = date_path
                break

        if not session_path:
            self.logger.warning("Cannot write session metadata: no session directories found")
            return

        metadata_path = os.path.join(session_path, 'session_metadata.json')

        import json
        from datetime import datetime
        payload = dict(metadata)
        payload['recorded_at'] = datetime.now().isoformat(timespec='seconds')
        payload['owl_id'] = self.state.get('device_id', '')

        try:
            with open(metadata_path, 'w') as f:
                json.dump(payload, f, indent=2)
            self.logger.info(f"Session metadata written to {metadata_path}")
        except OSError as e:
            self.logger.error(f"Failed to write session metadata: {e}")

    def _auto_save_session_metadata(self):
        """Auto-save session metadata when recording stops (if any fields are non-empty)."""
        with self.state_lock:
            metadata = self.state.get('session_metadata', {})

        # Only save if at least one field has content
        if any(v.strip() for v in metadata.values() if isinstance(v, str)):
            self._write_session_metadata(metadata)

    def _list_data_sessions(self):
        """Scan save_directory for recording sessions. Publish to state."""
        try:
            save_dir = getattr(self.owl_instance, 'save_directory', None) if self.owl_instance else None
            sessions = scan_sessions(save_dir)

            with self.state_lock:
                self.state['data_sessions'] = sessions
            # Missing storage is "unknown", not "no sessions". The heartbeat
            # sends the change to the cloud within 2 s.
            self._cloud_sessions = trim_sessions(sessions) if save_dir and os.path.isdir(save_dir) else None
            self._publish_state()

        except Exception as e:
            self.logger.error(f"Error listing data sessions: {e}")
            with self.state_lock:
                self.state['data_sessions'] = []
            self._cloud_sessions = None
            self._publish_state()

    def _upload_session(self, session_date, data_types, upload_url, method='POST',
                        request_id='', upload=None):
        """ZIP and upload a data session. Runs in background thread.

        session_date can be "YYYYMMDD" (all sessions under that date) or
        "YYYYMMDD/session_HHMMSS" (specific session).

        method='POST' uploads to the controller endpoint (self-signed cert,
        custom X-OWL headers). method='PUT' targets an S3-style presigned URL:
        verified TLS and no custom headers (anything outside the presigned
        SignedHeaders set can invalidate the signature).

        upload (optional) switches to S3 presigned multipart: a dict with
        'upload_id', 'part_size' and 'parts' [{part_number, url}, ...].
        Parts are PUT sequentially; the ETags S3 returns accumulate in
        state.data_transfer.parts so the cloud side can call
        CompleteMultipartUpload — the device never needs S3 credentials.
        """
        import re
        import tempfile
        import zipfile

        upload_id = str(upload.get('upload_id', '')) if upload else ''

        # Validate format: date or date/session
        if not re.match(r'^\d{8}(/session_\d{6})?$', session_date):
            self.logger.error(f"Invalid session identifier: {session_date}")
            with self.state_lock:
                self.state['data_transfer'] = _data_transfer_state(
                    status='error', session_date=session_date,
                    request_id=request_id, upload_id=upload_id,
                    error='Invalid session identifier')
            self._publish_state()
            return

        # Guard: one transfer at a time
        with self.state_lock:
            if self.state['data_transfer']['status'] not in ('idle', 'complete', 'error'):
                self.logger.warning("Transfer already in progress")
                return
            self.state['data_transfer'] = _data_transfer_state(
                status='scanning', session_date=session_date,
                request_id=request_id, upload_id=upload_id)
        self._publish_state()

        tmp_path = None
        try:
            save_dir = getattr(self.owl_instance, 'save_directory', None) if self.owl_instance else None

            # Collect files to zip using shared scanner
            files_to_zip = []
            if 'images' in data_types and save_dir:
                files_to_zip = collect_session_files(save_dir, session_date)

            if not files_to_zip:
                with self.state_lock:
                    self.state['data_transfer'] = _data_transfer_state(
                        status='error', session_date=session_date,
                        request_id=request_id, upload_id=upload_id,
                        error='No files found for this session')
                self._publish_state()
                return

            # Create temp ZIP (ZIP_STORED — JPEGs already compressed)
            with self.state_lock:
                self.state['data_transfer']['status'] = 'zipping'
            self._publish_state()

            tmp_fd, tmp_path = tempfile.mkstemp(suffix='.zip')
            with os.fdopen(tmp_fd, 'wb') as tmp_f:
                with zipfile.ZipFile(tmp_f, 'w', zipfile.ZIP_STORED) as zf:
                    for i, (arc_name, full_path) in enumerate(files_to_zip):
                        zf.write(full_path, arc_name)
                        # Progress during zipping: 0-50%
                        progress = int((i + 1) / len(files_to_zip) * 50)
                        with self.state_lock:
                            self.state['data_transfer']['progress'] = progress
                        if (i + 1) % 10 == 0:
                            self._publish_state()

            zip_size = os.path.getsize(tmp_path)
            # Checksum before any upload reader touches the file — a retried
            # multipart part would feed bytes through a hashing reader twice
            zip_md5 = self._md5_file(tmp_path)

            with self.state_lock:
                self.state['data_transfer']['status'] = 'uploading'
                self.state['data_transfer']['bytes_total'] = zip_size
                self.state['data_transfer']['zip_bytes'] = zip_size
                self.state['data_transfer']['zip_md5'] = zip_md5
            self._publish_state()

            if upload:
                self._upload_multipart(tmp_path, zip_size, upload)
            else:
                self._upload_single(tmp_path, zip_size, upload_url, method, session_date)

            # Both upload paths raise on failure
            with self.state_lock:
                self.state['data_transfer'].update(
                    status='complete', progress=100,
                    bytes_sent=zip_size, error='')
            self._publish_state()
            self.logger.info(f"Session {session_date} uploaded successfully ({zip_size} bytes)")

        except Exception as e:
            self.logger.error(f"Session upload failed: {e}")
            # Keep request_id/upload_id and any accumulated parts — the cloud
            # side needs them to abort the multipart upload server-side
            with self.state_lock:
                self.state['data_transfer'].update(
                    status='error', progress=0, bytes_sent=0, error=str(e))
            self._publish_state()

        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def _md5_file(path):
        """Streaming MD5 of a file (64KB chunks)."""
        import hashlib
        h = hashlib.md5()
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _upload_single(self, tmp_path, zip_size, upload_url, method, session_date):
        """Upload the whole zip in one request. Raises on failure."""
        import urllib.request
        import ssl

        # Verified unless the host is a LAN controller (self-signed): a POST to a
        # public host used to skip verification entirely (O-9).
        ssl_ctx = ssl_context_for_url(upload_url)

        if method == 'PUT':
            headers = {
                'Content-Type': 'application/octet-stream',
                'Content-Length': str(zip_size),
            }
        else:
            headers = {
                'Content-Type': 'application/octet-stream',
                'X-OWL-Device-ID': self.device_id,
                'X-OWL-Session-Date': session_date,
                'Content-Length': str(zip_size),
            }

        # Stream the file in chunks — never load entire ZIP into memory
        with open(tmp_path, 'rb') as f:
            progress_fh = _ProgressReader(f, zip_size, self)
            req = urllib.request.Request(
                upload_url,
                data=progress_fh,
                method=method,
                headers=headers
            )
            response = urllib.request.urlopen(req, context=ssl_ctx, timeout=300)
        resp_code = response.getcode()

        # 204: some S3-compatible stores return No Content on PUT
        if resp_code not in (200, 201, 204):
            raise Exception(f"Upload failed with status {resp_code}")

    def _upload_multipart(self, tmp_path, zip_size, upload):
        """PUT each presigned part sequentially, accumulating ETags in state.

        Only a failed part is retried; raises once a part exhausts its
        retries. The cloud side calls CompleteMultipartUpload with the
        collected ETags after status reaches complete.
        """
        parts = sorted(upload['parts'], key=lambda p: int(p['part_number']))
        part_size = int(upload['part_size'])

        # The cloud side sizes the part list from the session manifest, but
        # the zip is slightly larger (zip headers). Fail before uploading
        # anything rather than completing a silently truncated object.
        if len(parts) * part_size < zip_size:
            raise Exception(
                f"Parts cover only {len(parts) * part_size} of {zip_size} "
                f"zip bytes; request more parts")

        with open(tmp_path, 'rb') as f:
            bytes_done = 0
            for part in parts:
                part_number = int(part['part_number'])
                offset = (part_number - 1) * part_size
                length = min(part_size, zip_size - offset)
                if length <= 0:
                    # Spare part URLs past the end of the zip (the cloud side
                    # may over-provision as a safety margin) — done
                    break
                etag = self._put_part(f, offset, length, part['url'],
                                      bytes_done, zip_size)
                bytes_done += length
                with self.state_lock:
                    self.state['data_transfer']['parts'].append(
                        {'part_number': part_number, 'etag': etag})
                    self.state['data_transfer']['bytes_sent'] = bytes_done
                    self.state['data_transfer']['progress'] = 50 + int(bytes_done / zip_size * 50)
                self._publish_state()

    def _put_part(self, fh, offset, length, url, base_sent, total, attempts=3):
        """PUT one multipart slice with retry/backoff. Returns the ETag.

        The ETag is kept verbatim — S3 returns it quoted and
        CompleteMultipartUpload expects the quoted form.
        """
        import urllib.request
        import ssl

        headers = {
            'Content-Type': 'application/octet-stream',
            'Content-Length': str(length),
        }
        last_error = None
        for attempt in range(attempts):
            if attempt:
                time.sleep(2 ** attempt)  # 2s, 4s
            try:
                # Re-stream just this part; rewind progress to the part start
                # so a retry doesn't double-count bytes_sent
                with self.state_lock:
                    self.state['data_transfer']['bytes_sent'] = base_sent
                fh.seek(offset)
                reader = _ProgressReader(fh, length, self,
                                         base_sent=base_sent, total=total)
                req = urllib.request.Request(url, data=reader, method='PUT',
                                             headers=headers)
                response = urllib.request.urlopen(
                    req, context=ssl.create_default_context(), timeout=300)
                code = response.getcode()
                if code in (200, 201, 204):
                    return response.headers.get('ETag', '')
                last_error = Exception(f"Part upload failed with status {code}")
                self.logger.warning(f"Part PUT attempt {attempt + 1}/{attempts}: status {code}")
            except Exception as e:
                last_error = e
                self.logger.warning(f"Part PUT attempt {attempt + 1}/{attempts} failed: {e}")
        raise last_error

    def _upload_previews(self, request_id, session_id, count, max_dimension, upload_urls):
        """Re-encode and upload sample images from a session to presigned URLs.

        Runs in background thread. One preview job at a time; each image is
        bounded (longest edge <= max_dimension, JPEG q70, ~200KB) so the whole
        job stays at a few hundred KB on metered cellular.
        """
        import re
        import ssl
        import urllib.request

        if not re.match(r'^\d{8}(/session_\d{6})?$', session_id):
            self.logger.error(f"Invalid session identifier for previews: {session_id}")
            with self.state_lock:
                self.state['preview_upload'] = _preview_upload_state(
                    status='error', request_id=request_id, session_id=session_id,
                    error='Invalid session identifier')
            self._publish_state()
            return

        # Guard: one preview job at a time
        with self.state_lock:
            if self.state['preview_upload']['status'] == 'uploading':
                self.logger.warning("Preview upload already in progress")
                return
            self.state['preview_upload'] = _preview_upload_state(
                status='uploading', request_id=request_id, session_id=session_id)
        self._publish_state()

        try:
            import cv2

            save_dir = getattr(self.owl_instance, 'save_directory', None) if self.owl_instance else None
            count = min(count, 20, len(upload_urls))
            images = select_preview_images(save_dir, session_id, count) if save_dir else []
            total = min(len(images), len(upload_urls))
            if total <= 0:
                raise Exception('No images found for this session')

            with self.state_lock:
                self.state['preview_upload']['total'] = total
            self._publish_state()

            ssl_ctx = ssl.create_default_context()
            for i in range(total):
                img = cv2.imread(images[i])
                if img is None:
                    self.logger.warning(f"Unreadable preview image skipped: {images[i]}")
                    continue
                h, w = img.shape[:2]
                if max(h, w) > max_dimension:
                    scale = max_dimension / max(h, w)
                    img = cv2.resize(img, (round(w * scale), round(h * scale)),
                                     interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if not ok:
                    self.logger.warning(f"JPEG encode failed, preview skipped: {images[i]}")
                    continue
                body = buf.tobytes()
                req = urllib.request.Request(
                    upload_urls[i], data=body, method='PUT',
                    headers={'Content-Type': 'image/jpeg',
                             'Content-Length': str(len(body))})
                response = urllib.request.urlopen(req, context=ssl_ctx, timeout=30)
                if response.getcode() not in (200, 201, 204):
                    raise Exception(f"Preview upload failed with status {response.getcode()}")
                with self.state_lock:
                    self.state['preview_upload']['uploaded'] += 1
                self._publish_state()

            with self.state_lock:
                self.state['preview_upload']['status'] = 'complete'
            self._publish_state()
            self.logger.info(f"Uploaded {total} previews for session {session_id}")

        except Exception as e:
            self.logger.error(f"Preview upload failed: {e}")
            with self.state_lock:
                self.state['preview_upload'].update(status='error', error=str(e))
            self._publish_state()

    def _session_busy(self, real_target, session_id):
        """Why this session (or date folder) must not be deleted now, or ''.
        Covers every sender: the collect screen, downloads page and Noktura."""
        with self.state_lock:
            recording = bool(self.state.get('image_sample_enable'))
            transfer = dict(self.state.get('data_transfer') or {})
        recorder = getattr(self.owl_instance, 'image_recorder', None)
        active = getattr(recorder, 'save_directory', None)
        if recording and isinstance(active, str):
            real_active = os.path.realpath(active)
            if real_active == real_target or real_active.startswith(real_target + os.sep):
                return 'recording into it'
        uploading = str(transfer.get('session_date') or '')
        if (transfer.get('status') not in (None, 'idle', 'complete', 'error')
                and (uploading == session_id or uploading.startswith(session_id + '/'))):
            return 'uploading it'
        return ''

    def _delete_session(self, session_date, data_types):
        """Delete a data session directory from the OWL. Runs in background thread.

        session_date can be "YYYYMMDD" or "YYYYMMDD/session_HHMMSS".
        """
        import re
        import shutil

        if not re.match(r'^\d{8}(/session_\d{6})?$', session_date):
            self.logger.error(f"Invalid session identifier for deletion: {session_date}")
            return

        try:
            save_dir = getattr(self.owl_instance, 'save_directory', None) if self.owl_instance else None
            if not save_dir:
                self.logger.error("No save_directory configured")
                return

            if 'images' in data_types:
                target = os.path.join(save_dir, session_date)
                real_target = os.path.realpath(target)
                real_save = os.path.realpath(save_dir)

                # Path traversal check
                if not real_target.startswith(real_save + os.sep):
                    self.logger.error(f"Path traversal rejected: {target}")
                    return

                busy = self._session_busy(real_target, session_date)
                if busy:
                    self.logger.warning(f"Not deleting {session_date}: {busy}")
                    return

                if os.path.isdir(target):
                    shutil.rmtree(target)
                    self.logger.info(f"Deleted session directory: {target}")
                else:
                    self.logger.warning(f"Session directory not found: {target}")

            # Refresh sessions list
            self._list_data_sessions()

        except Exception as e:
            self.logger.error(f"Error deleting session {session_date}: {e}")

    def _resolve_config_path(self):
        """Resolve the current config file path"""
        if self.config_file:
            return self.config_file

        if self.owl_instance and hasattr(self.owl_instance, 'config_path'):
            return self.owl_instance.config_path

        # Fallback: try active_config.txt
        config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config')
        active_path = os.path.join(config_dir, 'active_config.txt')
        if os.path.exists(active_path):
            with open(active_path, 'r') as f:
                return f.read().strip()

        return None

    def _apply_sensitivity_preset(self, preset):
        """Apply a sensitivity preset via SensitivityManager."""
        if self.owl_instance is None:
            self.logger.warning("Cannot apply preset - OWL instance not set")
            return

        if self.sensitivity_manager:
            success = self.sensitivity_manager.apply_preset(preset, self.owl_instance)
            if success:
                self._sync_parameters_to_state()
            else:
                self.logger.error(f"Failed to apply preset: {preset}")
        else:
            self.logger.warning("No SensitivityManager available")

    def _update_greenonbrown_param(self, param_name, param_value):
        """
        Update a single GreenOnBrown parameter in real-time.
        This allows fine-grained control from the GUI.
        """
        if self.owl_instance is None:
            self.logger.warning("Cannot update parameter - OWL instance not set")
            return

        # Validate parameter name
        valid_params = [
            'exg_min', 'exg_max', 'hue_min', 'hue_max',
            'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
            'min_detection_area', 'min_detection_area_percent', 'invert_hue',
            'lut_sensitivity'
        ]

        if param_name not in valid_params:
            self.logger.error(f"Invalid parameter name: {param_name}")
            return

        try:
            if param_name == 'lut_sensitivity':
                # Routed through the pending drain so the detection loop
                # re-bakes the LUT table (a plain setattr would not)
                param_value = max(0, min(100, int(float(param_value))))
                self.owl_instance._pending_lut_sensitivity = param_value
                if hasattr(self.owl_instance, 'config'):
                    self.owl_instance.config.set('GreenOnBrown', 'lut_sensitivity',
                                                 str(param_value))
                with self.state_lock:
                    self.state['lut_sensitivity'] = param_value
                self.logger.info(f"Updated lut_sensitivity = {param_value} (re-bake queued)")
                return

            # Boolean params
            if param_name == 'invert_hue':
                param_value = str(param_value).lower() in ('true', '1', 'yes')
            elif param_name == 'min_detection_area_percent':
                # % of the detection frame area (float; 0 = use px value)
                param_value = max(0.0, min(5.0, float(param_value)))
            else:
                # Convert to int
                param_value = int(float(param_value))

            # Update the Owl instance attribute directly
            setattr(self.owl_instance, param_name, param_value)

            # Queue trackbar update for main thread (cv2 HighGUI is not thread-safe)
            if self.owl_instance.show_display:
                trackbar_map = {
                    'exg_min': 'ExG-Min',
                    'exg_max': 'ExG-Max',
                    'hue_min': 'Hue-Min',
                    'hue_max': 'Hue-Max',
                    'saturation_min': 'Sat-Min',
                    'saturation_max': 'Sat-Max',
                    'brightness_min': 'Bright-Min',
                    'brightness_max': 'Bright-Max'
                }
                trackbar_name = trackbar_map.get(param_name)
                if trackbar_name:
                    self.owl_instance._pending_trackbar_updates[trackbar_name] = param_value

            self.logger.info(f"Updated {param_name} = {param_value}")

            # Also update the config object so changes can be persisted if needed
            if hasattr(self.owl_instance, 'config'):
                self.owl_instance.config.set('GreenOnBrown', param_name, str(param_value))

            # Update state to reflect the change
            with self.state_lock:
                self.state[param_name] = param_value

            # Legacy px key: convert to the canonical percent key, otherwise the
            # change is shadowed by the percent value in the detection loop.
            if param_name == 'min_detection_area':
                self._convert_px_min_area_to_percent(param_value)

        except Exception as e:
            self.logger.error(f"Error updating {param_name}: {e}")

    def _convert_px_min_area_to_percent(self, px_value):
        """Derive min_detection_area_percent from a legacy px value.

        min_detection_area_percent is the canonical min weed size key; the px
        key is accepted from legacy clients/configs but owl.py ignores it once
        percent > 0, so every px set must update the percent too.
        """
        owl = self.owl_instance
        area = (getattr(owl, 'cropped_width', 0) or 0) * \
               (getattr(owl, 'cropped_height', 0) or 0)
        if area <= 0:
            try:
                area = owl.resolution[0] * owl.resolution[1]
            except (AttributeError, TypeError, IndexError):
                area = 416 * 320
        percent = max(0.0005, min(5.0, px_value / area * 100))
        owl.min_detection_area_percent = percent
        if hasattr(owl, 'config'):
            owl.config.set('GreenOnBrown', 'min_detection_area_percent',
                           f'{percent:.4f}')
        with self.state_lock:
            self.state['min_detection_area_percent'] = percent
        self.logger.info(
            f"Converted legacy min_detection_area {px_value}px -> {percent:.4f}%")

    def _update_greenongreen_param(self, param_name, param_value):
        """Update a GreenOnGreen parameter in real-time. Only confidence can be hot-updated."""
        if self.owl_instance is None:
            self.logger.warning("Cannot update parameter - OWL instance not set")
            return

        if param_name == 'confidence':
            try:
                param_value = float(param_value)
                if hasattr(self.owl_instance, '_gog_confidence'):
                    self.owl_instance._gog_confidence = param_value
                    self.logger.info(f"Updated GreenOnGreen confidence = {param_value}")

                    if hasattr(self.owl_instance, 'config'):
                        self.owl_instance.config.set('GreenOnGreen', 'confidence', str(param_value))
                else:
                    self.logger.info(f"GreenOnGreen not active, storing config only")
                    if hasattr(self.owl_instance, 'config'):
                        self.owl_instance.config.set('GreenOnGreen', 'confidence', str(param_value))

                with self.state_lock:
                    self.state['confidence'] = param_value
            except Exception as e:
                self.logger.error(f"Error updating GreenOnGreen confidence: {e}")
        else:
            # Other params (model_path, detect_classes, actuation_mode) require restart
            self.logger.info(f"GreenOnGreen.{param_name} updated in config (restart required)")

    def _check_model_available(self):
        """Check if any YOLO model is available in the models/ directory."""
        try:
            models_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'models')
            if not os.path.isdir(models_dir):
                return False
            # Check for NCNN subdirs (have .param files) or .pt files
            for item in os.listdir(models_dir):
                item_path = os.path.join(models_dir, item)
                if item.endswith('.pt'):
                    return True
                if os.path.isdir(item_path):
                    for f in os.listdir(item_path):
                        if f.endswith('.param'):
                            return True
            return False
        except Exception:
            return False

    def _list_available_models(self):
        """List available YOLO models (.pt files and NCNN subdirs) in models/ directory."""
        models = []
        try:
            models_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'models')
            if not os.path.isdir(models_dir):
                return models
            for item in sorted(os.listdir(models_dir)):
                item_path = os.path.join(models_dir, item)
                if item.endswith('.pt'):
                    models.append(item)
                elif os.path.isdir(item_path):
                    for f in os.listdir(item_path):
                        if f.endswith('.param'):
                            models.append(item)
                            break
        except Exception:
            pass
        return models

    def _handle_gps_update(self, gps_data):
        """Handle GPS updates from dashboard or central controller.

        Stores the payload as-received (no invented defaults) plus the LOCAL
        receipt time — staleness checks must not depend on the sender's clock.
        """
        if not isinstance(gps_data, dict) or gps_data.get('latitude') is None or gps_data.get('longitude') is None:
            self.logger.warning(f"Ignoring GPS update without coordinates: {gps_data}")
            return

        with self.state_lock:
            self.state['gps_payload'] = dict(gps_data)
            self.state['gps_received_at'] = time.time()
            self.state['gps_available'] = True
            self.state['last_update'] = time.time()
            # Legacy flat keys (dashboard state display) — only when present
            self.state['gps_latitude'] = float(gps_data['latitude'])
            self.state['gps_longitude'] = float(gps_data['longitude'])
            if gps_data.get('accuracy') is not None:
                self.state['gps_accuracy'] = float(gps_data['accuracy'])
            if gps_data.get('timestamp') is not None:
                self.state['gps_timestamp'] = float(gps_data['timestamp'])

    def _monitor_states(self):
        """Monitor for state changes that need to trigger actions"""
        while self.running:
            try:
                time.sleep(0.5)

            except Exception as e:
                self.logger.error(f"Error in monitor_states: {e}")
                time.sleep(1)

    def _heartbeat_loop(self):
        """Publish periodic heartbeat to show OWL is alive"""
        heartbeat_interval = 2.0  # seconds

        while self.running:
            try:
                if self.connected:
                    self._refresh_ai_state()
                    self._refresh_camera_state()
                    self._refresh_capture_state()
                    self._read_update_status()
                    with self.state_lock:
                        self._publish_state()
                    self._publish_cloud_state()
                time.sleep(heartbeat_interval)
            except Exception as e:
                self.logger.error(f"Error in heartbeat loop: {e}")
                time.sleep(heartbeat_interval)

    def _refresh_camera_state(self):
        """Refresh state['camera'] from the configured WB and the latest
        picamera2 frame metadata. Called every heartbeat. Metadata-derived
        keys are omitted (not defaulted) when unavailable, e.g. on webcams."""
        owl = self.owl_instance
        if owl is None:
            return
        # Type-guarded so state stays JSON-serialisable even with a partial
        # owl instance (tests, webcam backends).
        mode = getattr(owl, 'awb_mode', None)
        red = getattr(owl, 'awb_red_gain', None)
        blue = getattr(owl, 'awb_blue_gain', None)
        camera = {
            'awb_mode': mode if isinstance(mode, str) else None,
            'awb_red_gain': float(red) if isinstance(red, (int, float)) else None,
            'awb_blue_gain': float(blue) if isinstance(blue, (int, float)) else None,
        }
        metadata = getattr(owl, '_last_camera_metadata', None)
        if isinstance(metadata, dict):
            gains = metadata.get('ColourGains')
            if gains and len(gains) >= 2:
                camera['colour_gains'] = [round(float(gains[0]), 3),
                                          round(float(gains[1]), 3)]
            for src_key, dst_key in (('ColourTemperature', 'colour_temperature'),
                                     ('Lux', 'lux'),
                                     ('ExposureTime', 'exposure_time_us'),
                                     ('AnalogueGain', 'analogue_gain')):
                value = metadata.get(src_key)
                if isinstance(value, (int, float)):
                    camera[dst_key] = round(float(value), 1)
        with self.state_lock:
            self.state['camera'] = camera

    def _connectivity_loop(self):
        """Every minute: is the clock NTP-synced, and can this unit reach
        the internet? Uploads go straight from each OWL to Noktura storage,
        so a rig where only the controller has a route out fails silently
        without this. internet_ok stays None until a cloud host is known."""
        from utils.cloud_provision import clock_synced
        while self.running:
            try:
                host = (cloud_storage_host() or self._cloud_policy.get('storage_host')
                        or self._cloud_policy.get('portal_host'))
                synced = clock_synced()
                reachable = internet_reachable(host) if host else None
                with self.state_lock:
                    self.state['clock_synced'] = synced
                    self.state['internet_ok'] = reachable
            except Exception as e:
                self.logger.debug(f"connectivity check failed: {e}")
            for _ in range(CONNECTIVITY_INTERVAL_S):
                if not self.running:
                    return
                time.sleep(1)

    def _refresh_capture_state(self):
        """state['capture'] from the OWL's capture_state(). Called every heartbeat."""
        owl = self.owl_instance
        if owl is None or not hasattr(owl, 'capture_state'):
            return
        try:
            capture = owl.capture_state()
        except Exception as e:
            self.logger.debug(f"capture state unavailable: {e}")
            return
        with self.state_lock:
            self.state['capture'] = capture

    def _handle_set_capture_params(self, params):
        """Apply capture settings live. Deliberately not saved to the active
        config: the controller keeps them in its own [Collect] section and
        re-sends them on every connect and Record, and saving them here would
        carry collect settings (shutter cap, sample mode) into spraying.

        Unknown keys are ignored; a value that fails to convert is rejected
        on its own without blocking the others.
        """
        owl = self.owl_instance
        if owl is None or not hasattr(owl, 'apply_capture_params'):
            return
        wanted = {k: v for k, v in params.items() if k in CAPTURE_PARAM_KEYS}
        applied = {}
        for key, value in wanted.items():
            try:
                applied.update(owl.apply_capture_params({key: value}))
            except (TypeError, ValueError, OverflowError) as e:
                self.logger.warning(f"Rejected capture param {key}={value!r}: {e}")
        if not applied:
            return
        self.logger.info(f"Capture params applied: {applied}")
        self._refresh_capture_state()
        with self.state_lock:
            self._publish_state()

    def _refresh_ai_state(self):
        """Refresh AI tab state from live OWL detector. Called every heartbeat."""
        if self.owl_instance is None:
            return
        with self.state_lock:
            # Refresh algorithm from live config (handles runtime changes)
            self.state['algorithm'] = self.owl_instance.config.get(
                'System', 'algorithm', fallback=self.state.get('algorithm', 'exhsv'))

            gog = getattr(self.owl_instance, '_gog_detector', None)
            # Use pending model name if OWL hasn't processed the swap yet,
            # so the dashboard dropdown doesn't snap back to the old model.
            pending_model = getattr(self.owl_instance, '_pending_model', None)
            if pending_model is not None:
                # Model swap queued — keep current_model as set by the handler
                pass
            elif gog and hasattr(gog, 'model'):
                self.state['current_model'] = getattr(gog, '_model_filename', '')
                self.state['model_classes'] = {str(k): v for k, v in gog.model.names.items()}
            else:
                self.state['current_model'] = ''
                self.state['model_classes'] = {}
            # Use pending classes if OWL hasn't processed them yet,
            # otherwise use the active detect_classes_list
            pending = getattr(self.owl_instance, '_pending_detect_classes', None)
            if pending is not None:
                self.state['detect_classes'] = pending
            else:
                self.state['detect_classes'] = getattr(self.owl_instance, '_detect_classes_list', [])
            self.state['available_models'] = self._list_available_models()

    def _publish_state(self):
        """Publish current state to MQTT"""
        if self.connected:
            try:
                with self.state_lock:
                    state_copy = self.state.copy()

                self.client.publish(self.topics['state'], json.dumps(state_copy), retain=False)
            except Exception as e:
                self.logger.error(f"Error publishing state: {e}")

    def _cloud_state(self):
        """The CLOUD_STATE_FIELDS subset of state, telemetry rounded so jitter
        never reads as a change. Absent fields are omitted, not defaulted."""
        with self.state_lock:
            snap = {k: self.state[k] for k in CLOUD_STATE_FIELDS if k in self.state}
            snap = json.loads(json.dumps(snap, default=str))  # deep copy outside the lock
        for key in ('cpu_percent', 'cpu_temp', 'memory_percent', 'disk_percent'):
            if isinstance(snap.get(key), (int, float)):
                snap[key] = int(round(snap[key]))
        for key in ('gps_latitude', 'gps_longitude'):
            if isinstance(snap.get(key), (int, float)):
                snap[key] = round(snap[key], 5)
        return snap

    def _publish_cloud_state(self):
        """Publish the slim cloud state when CloudFeed says it is due."""
        if not self.connected:
            return
        try:
            message = self._cloud_feed.next_message(self._cloud_state(), self._cloud_sessions)
            if message:
                payload, qos = message
                self.client.publish(self.topics['cloud_state'], json.dumps(payload), qos=qos, retain=False)
        except Exception as e:
            self.logger.error(f"Error publishing cloud state: {e}")

    # State update methods for owl.py to call
    def set_detection_enable(self, value):
        """Set detection enable state (for owl.py internal use)"""
        with self.state_lock:
            self.state['detection_enable'] = bool(value)
            self.state['last_update'] = time.time()
        self._publish_state()

    def set_detection_mode(self, mode):
        """Set detection mode: 0=spot spray, 1=off, 2=blanket (for controller use)"""
        with self.state_lock:
            self.state['detection_mode'] = int(mode)
            # Also update detection_enable for backwards compatibility
            self.state['detection_enable'] = (mode == 0)
            self.state['last_update'] = time.time()
        self._publish_state()

    def set_image_sample_enable(self, value):
        """Set image sampling state (for owl.py internal use and hardware controllers)"""
        with self.state_lock:
            was_recording = self.state['image_sample_enable']
            self.state['image_sample_enable'] = bool(value)
            self.state['last_update'] = time.time()

        # Auto-save session metadata when recording stops (hardware switch path)
        if was_recording and not bool(value):
            self._auto_save_session_metadata()

        self._publish_state()

    def set_storage_available(self, value):
        """Flag whether a writable recording drive is present (set by owl.py
        storage setup / the record-toggle re-scan)."""
        with self.state_lock:
            if self.state.get('storage_available') == bool(value):
                return
            self.state['storage_available'] = bool(value)
            self.state['last_update'] = time.time()
        self._publish_state()

    def set_recording_storage(self, location, fallback):
        """Recording storage resolved: where images will land ('usb' or
        'internal') and whether that was a fallback from a missing USB drive
        (storage_location usb/auto). Clears any blocked reason."""
        with self.state_lock:
            self.state['recording_location'] = location
            self.state['storage_fallback'] = bool(fallback)
            self.state['recording_blocked_reason'] = None
            self.state['last_update'] = time.time()
        self._publish_state()

    def set_recording_blocked(self, reason):
        """Recording was refused: publish why so dashboards/app can say so
        instead of silently snapping the toggle back.
        reason: no_drive | storage_full | floor_unreachable | storage_error."""
        with self.state_lock:
            self.state['recording_blocked_reason'] = reason
            self.state['recording_location'] = None
            self.state['last_update'] = time.time()
        self._publish_state()

    def save_session_metadata(self):
        """Write any buffered session metadata into the active session dir
        (called by owl.py right after the session directory is created)."""
        self._auto_save_session_metadata()

    def set_storage_status(self, location, free_mb, warning, quota_gb):
        """Push storage watchdog readings into state (additive, contract 1).

        free_mb is quantized to 50 MB so the 10.5 s disk sample doesn't
        trigger a state publish every cycle; warning transitions always do.
        """
        quantized = None if free_mb is None else int(free_mb // 50 * 50)
        with self.state_lock:
            changed = (self.state.get('storage_location') != location
                       or self.state.get('storage_free_mb') != quantized
                       or self.state.get('storage_warning') != warning
                       or self.state.get('image_quota_gb') != quota_gb)
            if not changed:
                return
            self.state['storage_location'] = location
            self.state['storage_free_mb'] = quantized
            self.state['storage_warning'] = warning
            self.state['image_quota_gb'] = quota_gb
            self.state['last_update'] = time.time()
        self._publish_state()

    def set_gps_status(self, fix, source, latitude, longitude, accuracy):
        """Push the current best-GPS reading into state (additive fields).

        Coordinates are quantized to 5 decimal places (~1.1 m) for the
        change check so a stationary rig doesn't republish state on every
        jitter; fix/source transitions always publish.
        """
        lat_q = None if latitude is None else round(float(latitude), 5)
        lon_q = None if longitude is None else round(float(longitude), 5)
        acc_q = None if accuracy is None else round(float(accuracy), 1)
        with self.state_lock:
            changed = (self.state.get('gps_fix') != fix
                       or self.state.get('gps_source') != source
                       or self.state.get('gps_best_lat') != lat_q
                       or self.state.get('gps_best_lon') != lon_q
                       or self.state.get('gps_best_accuracy') != acc_q)
            if not changed:
                return
            self.state['gps_fix'] = bool(fix)
            self.state['gps_source'] = source
            self.state['gps_best_lat'] = lat_q
            self.state['gps_best_lon'] = lon_q
            self.state['gps_best_accuracy'] = acc_q
            self.state['last_update'] = time.time()
        self._publish_state()

    def weed_detect_indicator(self):
        """Send weed detection indicator to dashboard (replaces DashboardController method)"""
        if self.connected:
            indicator_msg = json.dumps({
                'type': 'weed_detected',
                'timestamp': time.time()
            })
            self.client.publish(self.topics['indicators'], indicator_msg, qos=0)

    def image_write_indicator(self):
        """Send image write indicator to dashboard (replaces DashboardController method)"""
        if self.connected:
            indicator_msg = json.dumps({
                'type': 'image_written',
                'timestamp': time.time()
            })
            self.client.publish(self.topics['indicators'], indicator_msg, qos=0)

    THUMB_INTERVAL_S = 2.0

    def publish_capture(self, recorder):
        """After an image is queued (or a video preview taken): publish the
        capture event for the controller's map and, at most every
        THUMB_INTERVAL_S, the thumbnail for its gallery. Local broker only."""
        if not self.connected or recorder is None:
            return
        try:
            if recorder.last_saved:
                self.client.publish(self.topics['capture'], json.dumps(
                    dict(recorder.last_saved, session_id=getattr(self.owl_instance, '_session_id', None))),
                    qos=0)
            now = time.monotonic()
            if recorder.last_thumbnail and now - getattr(self, '_thumb_sent_at', 0.0) >= self.THUMB_INTERVAL_S:
                self._thumb_sent_at = now
                self.client.publish(self.topics['thumb'], recorder.last_thumbnail, qos=0)
        except Exception as e:
            self.logger.debug(f"capture publish failed: {e}")

    def drive_full_indicator(self):
        """Send drive full indicator to dashboard"""
        if self.connected:
            indicator_msg = json.dumps({
                'type': 'drive_full',
                'timestamp': time.time(),
                'message': 'Storage drive is full - recording disabled'
            })
            self.client.publish(self.topics['indicators'], indicator_msg, qos=0)

    def get_detection_enable(self):
        with self.state_lock:
            return self.state['detection_enable']

    def get_image_sample_enable(self):
        with self.state_lock:
            return self.state['image_sample_enable']

    def get_sensitivity_level(self):
        with self.state_lock:
            return self.state['sensitivity_level']

    def get_gps_data(self):
        """Return the last GPS payload plus local receipt time, or None."""
        with self.state_lock:
            if not self.state['gps_available'] or not self.state['gps_payload']:
                return None
            gps = dict(self.state['gps_payload'])
            gps['received_at'] = self.state['gps_received_at']
            return gps

    def get_session_metadata(self):
        """Return a copy of the farmer-entered session metadata (field, crop, etc.)."""
        with self.state_lock:
            return dict(self.state.get('session_metadata', {}))

    def set_stream_status(self, is_active: bool):
        """Allows the main Owl instance to report the video stream status."""
        with self.state_lock:
            if self.state.get('stream_active') != is_active:
                self.state['stream_active'] = is_active
                self.state['last_update'] = time.time()
                self._publish_state()

    def set_sensitivity_level(self, value):
        """Set sensitivity level (for owl.py internal use)"""
        with self.state_lock:
            old_value = self.state.get('sensitivity_level')
            if old_value == value:
                return

            self.state['sensitivity_level'] = value
            self.state['last_update'] = time.time()

        self._publish_state()
        self._apply_sensitivity_preset(value)
        self.logger.info(f"Sensitivity level changed from {old_value} to {value}")

    def update_system_stats(self, stats_dict):
        """
        Update system statistics from owl.py

        Args:
            stats_dict: Dictionary containing system stats from get_system_stats()
        """
        with self.state_lock:
            # Update all system stats
            self.state['cpu_percent'] = stats_dict.get('cpu_percent', 0)
            self.state['cpu_temp'] = stats_dict.get('cpu_temp', 0)
            self.state['memory_percent'] = stats_dict.get('memory_percent', 0)
            self.state['memory_used'] = stats_dict.get('memory_used', 0)
            self.state['memory_total'] = stats_dict.get('memory_total', 0)
            self.state['disk_percent'] = stats_dict.get('disk_percent', 0)
            self.state['disk_used'] = stats_dict.get('disk_used', 0)
            self.state['disk_total'] = stats_dict.get('disk_total', 0)
            self.state['fan_status'] = stats_dict.get('fan_status', {'is_rpi5': False, 'mode': 'unavailable', 'rpm': 0})
            self.state['owl_running'] = stats_dict.get('owl_running', True)  # owl.py is running if calling this
            self.state['avg_loop_time_ms'] = stats_dict.get('avg_loop_time_ms', 0.0)
            self.state['actuation_duration'] = stats_dict.get('actuation_duration', self.state.get('actuation_duration', 0.15))
            self.state['delay'] = stats_dict.get('delay', self.state.get('delay', 0.0))
            for key in ('actuation_source', 'speed_kmh', 'latency_ms', 'speed_warning'):
                if key in stats_dict:
                    self.state[key] = stats_dict[key]
            self.state['last_update'] = time.time()

class DashMQTTSubscriber:
    """
    MQTT Subscriber for the control interfaces - subscribes to topics and sends commands
    For networked mode, use the central controller instead
    """

    def __init__(self, broker_host='localhost', broker_port=1883, client_id='owl_dashboard', device_id=None,
                 cloud_device_id=None):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.client_id = client_id
        self.logger = logging.getLogger(__name__)

        # Cloud bridge connectivity: mosquitto publishes a retained 1/0 to
        # this local $SYS topic when the Noktura bridge connects/drops.
        # None = not yet observed (or cloud not configured).
        self.cloud_device_id = cloud_device_id or None
        self.cloud_connected = None
        self.cloud_state_topic = (
            f'$SYS/broker/connection/owl-bridge-{self.cloud_device_id}/state'
            if self.cloud_device_id else None
        )

        # Determine if this is for networked or standalone mode
        self.networked_mode = (broker_host.lower() not in ['localhost', '127.0.0.1'])

        if self.networked_mode:
            self.logger.warning(
                "MQTTClient is designed for standalone mode. For networked mode, use the central controller.")
            # In networked mode, we need to know which device to control
            if device_id is None:
                self.logger.error("device_id required for networked mode")
                device_id = 'unknown'

        # Device ID (for networked mode)
        self.device_id = device_id or socket.gethostname()

        # MQTT topics
        if self.networked_mode and device_id:
            self.topics = {
                'commands': f'owl/{device_id}/commands',
                'state': f'owl/{device_id}/state',
                'status': f'owl/{device_id}/status',
                'detection': f'owl/{device_id}/detection',
                'config': f'owl/{device_id}/config',
                'indicators': f'owl/{device_id}/indicators',
                'errors': f'owl/{device_id}/errors',
                'gps': f'owl/{device_id}/gps'
            }
        else:
            # Standalone mode: simple topics
            self.topics = {
                'commands': 'owl/commands',
                'state': 'owl/state',
                'status': 'owl/status',
                'detection': 'owl/detection',
                'config': 'owl/config',
                'indicators': 'owl/indicators',
                'errors': 'owl/errors',
                'gps': 'owl/gps'
            }

        # Error log
        self.error_log = deque(maxlen=20)

        # Current state cache
        self.current_state = {}
        self.state_lock = threading.RLock()

        # Indicators
        self.last_weed_detect = 0
        self.last_image_write = 0

        self.last_heartbeat = 0
        self.heartbeat_timeout = 5.0

        # MQTT client
        self.client = mqtt.Client(client_id=self.client_id)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        self.connected = False

    def start(self):
        """Start the MQTT IPC client"""
        try:
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            self.client.loop_start()
            self.logger.info(f"MQTT IPC Client started (broker: {self.broker_host}:{self.broker_port})")
        except Exception as e:
            self.logger.error(f"Failed to start MQTT client: {e}")
            raise

    def stop(self):
        """Stop the MQTT IPC client"""
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, rc):
        """Handle MQTT connection"""
        if rc == 0:
            self.connected = True
            self.logger.info("Connected to MQTT broker")

            # Subscribe to all relevant topics
            client.subscribe(self.topics['state'])
            client.subscribe(self.topics['status'])
            client.subscribe(self.topics['detection'])
            client.subscribe(self.topics['config'])
            client.subscribe(self.topics['indicators'])
            client.subscribe(self.topics['errors'])
            # Cloud bridge link state (retained) — only when cloud is configured
            if self.cloud_state_topic:
                client.subscribe(self.cloud_state_topic)

        else:
            self.logger.error(f"Failed to connect to MQTT broker: {rc}")

    def _on_disconnect(self, client, userdata, rc):
        """Handle MQTT disconnection"""
        self.connected = False
        if rc != 0:
            self.logger.warning(f"Unexpected MQTT disconnection (rc={rc})")

    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages"""
        topic = msg.topic

        # Cloud bridge link state — retained $SYS payload is '1'/'0', not JSON
        if getattr(self, 'cloud_state_topic', None) and topic == self.cloud_state_topic:
            self.cloud_connected = (msg.payload.decode(errors='ignore').strip() == '1')
            return

        raw = msg.payload.decode(errors='ignore')
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self.logger.error(
                f"Malformed JSON on topic '{topic}': "
                f"{raw[:60]!r}… ({e})"
            )
            return

        # ANY valid message from OWL proves it's alive — update heartbeat
        # (matches networked controller's last_seen pattern)
        with self.state_lock:
            self.last_heartbeat = time.time()

        # Dispatch based on topic
        if topic == self.topics['state']:
            with self.state_lock:
                self.current_state = data
            self.logger.debug(f"State update received ({len(data)} fields)")

        elif topic == self.topics['status']:
            self.logger.info(f"OWL status: {data}")
            # Update owl_running from status messages (these are confirmed arriving)
            with self.state_lock:
                if 'owl_running' in data:
                    self.current_state['owl_running'] = data['owl_running']
                if 'connected' in data:
                    self.current_state['connected'] = data['connected']

        elif topic == self.topics['indicators']:
            self._handle_indicator(data)

        elif topic == self.topics['errors']:
            self._handle_error(data)

    def _handle_indicator(self, indicator_data):
        """Handle indicator messages (weed detection, image write)"""
        try:
            indicator_type = indicator_data.get('type')
            timestamp = indicator_data.get('timestamp', time.time())

            if indicator_type == 'weed_detected':
                self.last_weed_detect = timestamp
            elif indicator_type == 'image_written':
                self.last_image_write = timestamp

        except Exception as e:
            self.logger.error(f"Error handling indicator: {e}")

    def _handle_error(self, error_data):
        """Handles incoming error messages from owl.py"""
        try:
            self.logger.warning(f"Received error from owl.py: {error_data.get('message')}")
            with self.state_lock:
                self.error_log.append(error_data)
        except Exception as e:
            self.logger.error(f"Error while processing error message: {e}")

    def get_and_clear_errors(self):
        """Atomically retrieves and clears the current error log."""
        with self.state_lock:
            errors_to_send = list(self.error_log)
            self.error_log.clear()
        return errors_to_send

    def get_state(self):
        """Get current state"""
        with self.state_lock:
            current_time = time.time()
            if current_time - self.last_heartbeat > self.heartbeat_timeout:
                self.current_state['owl_running'] = False

            return self.current_state.copy()

    def get_weed_detect_indicator(self):
        """Check if weed was recently detected (for UI indicators)"""
        return (time.time() - self.last_weed_detect) < 1.0  # 1 second indicator

    def get_image_write_indicator(self):
        """Check if image was recently written (for UI indicators)"""
        return (time.time() - self.last_image_write) < 1.0  # 1 second indicator

    def get_sensitivity_level(self):
        """Get current sensitivity level as string"""
        with self.state_lock:
            return self.current_state.get('sensitivity_level', 'medium')

    def get_cloud_connected(self):
        """Cloud bridge link state: True (up), False (down), None (unknown/not configured)."""
        return self.cloud_connected

    def _send_command(self, action, **kwargs):
        """Send command to OWL"""
        if not self.connected:
            return {'success': False, 'error': 'Not connected to MQTT broker'}

        command = {'action': action, **kwargs}
        try:
            self.client.publish(self.topics['commands'], json.dumps(command))
            return {'success': True, 'message': f'Command {action} sent'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def set_detection_enable(self, value):
        """Enable/disable detection"""
        return self._send_command('set_detection_enable', value=value)

    def set_image_sample_enable(self, value):
        """Enable/disable image sampling"""
        return self._send_command('set_image_sample_enable', value=value)

    def set_sensitivity_level(self, level):
        """Set sensitivity level (accepts builtin and custom preset names)"""
        level = level.lower()
        return self._send_command('set_sensitivity_level', level=level)

    def set_greenonbrown_param(self, param_name, param_value):
        """Update a single GreenOnBrown parameter"""
        valid_params = [
            'exg_min', 'exg_max', 'hue_min', 'hue_max',
            'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max'
        ]

        if param_name not in valid_params:
            return {'success': False, 'error': f'Invalid parameter name. Valid options: {valid_params}'}

        return self._send_command('set_greenonbrown_param', param=param_name, value=param_value)

    def set_detection_mode(self, mode):
        """Set detection mode: 0=spot spray, 1=off, 2=blanket"""
        return self._send_command('set_detection_mode', value=int(mode))

    def update_gps(self, lat, lon, accuracy=None, timestamp=None, extra=None):
        """Publish a GPS position to the OWL (owl/gps topic).

        Only keys with real values are sent — absent data is omitted, never
        defaulted, so the EXIF writer downstream can apply its fail-safe
        rule. `extra` carries optional fields (altitude, heading, speed_kmh)
        straight through; owl.py stores the payload verbatim.
        """
        gps_data = {
            'latitude': lat,
            'longitude': lon,
            'timestamp': time.time() if timestamp is None else timestamp,
        }
        if accuracy is not None:
            gps_data['accuracy'] = accuracy
        if extra:
            gps_data.update({k: v for k, v in extra.items() if v is not None})

        try:
            self.client.publish(self.topics['gps'], json.dumps(gps_data))
            return {'success': True, 'message': 'GPS data sent'}
        except Exception as e:
            return {'success': False, 'error': str(e)}