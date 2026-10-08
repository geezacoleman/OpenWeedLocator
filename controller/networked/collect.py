"""Data-collection mode for the networked controller.

The collect screen (templates/collect.html) is a simple in-cab recorder:
one Record button, preset conditions, a gallery of the latest saved image
from each OWL, a map of every image location in the current run, and
plain-language quality warnings. Detection stays off in this mode.

Everything here is controller-side state built from what the OWLs publish
on the local broker (owl/<id>/state, owl/<id>/capture, owl/<id>/thumb) plus
pure warning rules, so it can be tested without MQTT or a camera.
"""

import threading
import time
import uuid
from collections import deque

# --- Presets ---------------------------------------------------------------
# Values are slugs: they go into EXIF / session_metadata.json and Noktura maps
# them onto dataset fields (crop_type, lighting enum, agcontext). Labels and
# translations live in static/js/collect/i18n.js. Each list is overridable as
# a comma-separated key in CONTROLLER.ini [Collect].
DEFAULT_CROPS = ('wheat', 'barley', 'canola', 'rye', 'oats', 'triticale', 'sugar_beet',
                 'maize', 'grass_clover', 'peas', 'faba_beans', 'potatoes', 'fallow')
SEASONAL_CROPS = ('wheat', 'barley', 'canola', 'rye', 'triticale')
DEFAULT_WEATHER = ('dry', 'dew', 'wet', 'windy')
# Noktura datasets.lighting enum values
DEFAULT_LIGHTING = ('natural_sun', 'natural_partial', 'natural_overcast')
DEFAULT_PURPOSES = ('herbicide', 'fungicide', 'insecticide', 'survey')

# --- Warning thresholds (defaults; [Collect] keys override) ---------------
DEFAULT_THRESHOLDS = {
    'smear_limit_mm': 2.0,       # ground movement during one exposure
    'soft_ratio': 0.6,           # sharpness vs the session's first images
    'dark_brightness': 40.0,     # mean 0-255, with the gain at its limit
    'dark_percent': 30.0,        # % pixels near black
    'clipped_percent': 5.0,      # % pixels blown out
    'not_saving_s': 10.0,        # moving but no new image for this long (floor; see below)
    'storage_low_mb': 2048,
}
MOVING_KMH = 1.0
NOT_RECORDING_GRACE_S = 5.0      # OWLs report recording a moment after Start
GAIN_AT_LIMIT = 15.5             # the IMX296/HQ AGC tops out at 16x

MAX_POINTS = 50000               # ~14 h at one image a second per OWL
THUMBS_PER_OWL = 2
UPLOAD_REQUEST_TTL_S = 24 * 3600


def update_ini_text(text, section, values):
    """Set keys in one INI section, keeping every comment and other line.

    configparser rewrites drop comments, and CONTROLLER.ini is meant to be
    hand-edited, so settings saved from the screen edit lines in place:
    existing keys are replaced, missing keys go at the end of the section,
    and a missing section is appended to the file.
    """
    lines = text.splitlines()
    remaining = {k: str(v) for k, v in values.items()}
    out, in_section, section_seen = [], False, False

    def flush():
        for key, value in remaining.items():
            out.append(f'{key} = {value}')
        remaining.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            if in_section:
                # Keep a blank line between the new keys and the next header
                while out and not out[-1].strip():
                    out.pop()
                flush()
                out.append('')
            in_section = stripped[1:-1].strip() == section
            section_seen = section_seen or in_section
            out.append(line)
            continue
        if in_section and '=' in stripped and not stripped.startswith('#'):
            key = stripped.split('=', 1)[0].strip()
            if key in remaining:
                out.append(f'{key} = {remaining.pop(key)}')
                continue
        out.append(line)
    if in_section:
        flush()
    elif not section_seen:
        if out and out[-1].strip():
            out.append('')
        out.append(f'[{section}]')
        flush()
    return '\n'.join(out) + '\n'


_PROVISION_FLAGS = {'--host': 'broker_host', '--port': 'broker_port', '--device-id': 'device_id',
                    '--username': 'username', '--password': 'password',
                    '--portal-url': 'portal_url', '--storage-host': 'storage_host'}


def parse_provision_command(text):
    """Credential dict from the command Noktura's one-time link shows
    (buildProvisionCommand: `sudo bash owl_cloud_provision.sh --host ...
    --password '...' --system-ca`). Raises ValueError on anything else.

    The result still goes through cloud_provision.validate_payload; this only
    turns pasted shell text into fields. Nothing is executed.
    """
    import shlex
    text = str(text or '').replace('\\\r\n', ' ').replace('\\\n', ' ')
    try:
        tokens = shlex.split(text)
    except ValueError:
        raise ValueError('Could not read the command. Copy it again from the Noktura page.')
    if 'owl_cloud_provision.sh' not in ' '.join(tokens):
        raise ValueError('That is not the Noktura connect command (owl_cloud_provision.sh).')
    data = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == '--password-file':
            raise ValueError('Paste the command that contains --password, not --password-file.')
        if token in _PROVISION_FLAGS and i + 1 < len(tokens):
            data[_PROVISION_FLAGS[token]] = tokens[i + 1]
            i += 2
            continue
        i += 1
    for required in ('broker_host', 'device_id', 'password'):
        if not data.get(required):
            raise ValueError(f'The command is missing {required.replace("_", " ")}.')
    return data


def parse_list(value, default):
    items = [v.strip() for v in str(value or '').split(',') if v.strip()]
    return tuple(items) or tuple(default)


def presets_from_config(config):
    """Preset lists for the record modal, from CONTROLLER.ini [Collect]."""
    get = (lambda key: config.get('Collect', key, fallback='')) if config.has_section('Collect') \
        else (lambda key: '')
    return {
        'crops': parse_list(get('crops'), DEFAULT_CROPS),
        'seasonal_crops': parse_list(get('seasonal_crops'), SEASONAL_CROPS),
        'weather': parse_list(get('weather'), DEFAULT_WEATHER),
        'lighting': parse_list(get('lighting'), DEFAULT_LIGHTING),
        'purposes': parse_list(get('purposes'), DEFAULT_PURPOSES),
    }


def thresholds_from_config(config):
    out = dict(DEFAULT_THRESHOLDS)
    if config.has_section('Collect'):
        for key, default in DEFAULT_THRESHOLDS.items():
            raw = config.get('Collect', key, fallback='').strip()
            if raw:
                try:
                    out[key] = type(default)(float(raw))
                except ValueError:
                    pass
    return out


def expected_interval_s(capture, speed_kmh):
    """Seconds between saved images for these capture settings, or None.

    Time mode uses the interval; distance mode the spacing at this speed,
    or the interval when there is no speed (the OWL falls back to time).
    """
    capture = capture or {}
    try:
        interval = float(capture.get('sample_interval_s') or 0) or None
        if capture.get('sample_mode') == 'time':
            return interval
        if capture.get('sample_mode') == 'distance':
            distance = float(capture.get('sample_distance_m') or 0)
            if speed_kmh and speed_kmh > 0 and distance > 0:
                return distance / (speed_kmh / 3.6)
            return interval
    except (TypeError, ValueError):
        pass
    return None


def collect_warnings(owls, now, thresholds=None, cloud_configured=False, gps_fix=True,
                     run_started_at=None, capture_settings=None):
    """Plain-language problems to show the driver, most serious first.

    owls: {device_id: state dict as merged from owl/<id>/state, plus the
          controller's own 'connected' / 'last_capture_at' bookkeeping}
    now, run_started_at, last_capture_at: the same monotonic clock.
    run_started_at: when Record was pressed (None = no run in progress).
    capture_settings: the [Collect] capture settings pushed to the OWLs.
    Returns [{'owl': id or None, 'code': str, 'severity': 'bad'|'warn', ...values}].
    The screen turns `code` into a sentence in the chosen language.
    """
    t = dict(DEFAULT_THRESHOLDS, **(thresholds or {}))
    warnings = []

    def add(owl, code, severity='warn', **values):
        warnings.append(dict(owl=owl, code=code, severity=severity, **values))

    any_recording = any(s.get('image_sample_enable') for s in owls.values() if s.get('connected'))
    if any_recording and not gps_fix:
        add(None, 'no_gps', 'warn')

    for owl_id in sorted(owls):
        s = owls[owl_id]
        if not s.get('connected'):
            add(owl_id, 'offline', 'bad')
            continue
        recording = bool(s.get('image_sample_enable'))
        capture = s.get('capture') or {}

        if s.get('storage_warning') or (s.get('storage_free_mb') is not None
                                        and s['storage_free_mb'] < t['storage_low_mb']):
            add(owl_id, 'storage_low', 'bad' if s.get('storage_warning') == 'critical' else 'warn',
                free_mb=s.get('storage_free_mb'))
        if s.get('recording_blocked_reason') and not recording:
            add(owl_id, 'recording_blocked', 'bad', reason=s['recording_blocked_reason'])
        if capture.get('stopped_reason') == 'video_limit' and not recording:
            add(owl_id, 'video_limit', 'warn', seconds=capture.get('video_max_s'))
        if s.get('clock_synced') is False:
            add(owl_id, 'clock', 'warn')
        if cloud_configured and s.get('internet_ok') is False:
            add(owl_id, 'no_internet', 'warn')

        if not recording:
            # A run is in progress but this OWL is not recording (rebooted,
            # missed the command). Blocked / video limit already say why.
            if (run_started_at is not None and now - run_started_at > NOT_RECORDING_GRACE_S
                    and not s.get('recording_blocked_reason')
                    and capture.get('stopped_reason') != 'video_limit'):
                add(owl_id, 'not_recording', 'bad')
            continue

        smear = capture.get('ground_smear_mm')
        if smear is not None and smear > t['smear_limit_mm']:
            speed = capture.get('speed_kmh') or 0
            # Speed that would bring the smear back to the limit at this shutter
            target = round(speed * t['smear_limit_mm'] / smear, 1) if smear else None
            add(owl_id, 'blur_risk', 'warn', smear_mm=smear, target_kmh=target,
                capped=bool(capture.get('shutter_capped')))

        qc = capture.get('qc') or {}
        baseline = qc.get('baseline_sharpness')
        recent = qc.get('recent_sharpness') or []
        if baseline and len(recent) >= 3 and all(v < baseline * t['soft_ratio'] for v in recent[-3:]):
            add(owl_id, 'soft', 'warn')

        median = qc.get('median') or {}
        gain = capture.get('analogue_gain')
        gain_limit = capture.get('max_gain') or GAIN_AT_LIMIT
        if median:
            too_dark = (median.get('dark', 0) > t['dark_percent']
                        or (median.get('brightness', 255) < t['dark_brightness']
                            and gain is not None and gain >= gain_limit * 0.95))
            if too_dark:
                add(owl_id, 'too_dark', 'warn')
            elif median.get('clipped', 0) > t['clipped_percent']:
                add(owl_id, 'overexposed', 'warn')

        # Counted from the last image, or from Start for an OWL that has not
        # saved one this run. The limit grows with the spacing so a 10 m
        # spacing at walking pace is not flagged.
        since = [v for v in (s.get('last_capture_at'), run_started_at) if v is not None]
        last_at = max(since) if since else None
        speed = capture.get('speed_kmh')
        interval = expected_interval_s(capture_settings, speed)
        limit = max(t['not_saving_s'], 3 * interval) if interval else t['not_saving_s']
        if (capture.get('record_mode') != 'video' and speed is not None and speed >= MOVING_KMH
                and last_at is not None and now - last_at > limit):
            add(owl_id, 'not_saving', 'bad', seconds=int(now - last_at))

    order = {'bad': 0, 'warn': 1}
    warnings.sort(key=lambda w: order.get(w['severity'], 2))
    return warnings


class CollectSession:
    """Run state for the collect screen: one run = one press of Record."""

    def __init__(self):
        self.lock = threading.Lock()
        self.run_id = None
        self.started_at = None           # wall clock, for the screen's timer
        self.started_mono = None         # monotonic, for the warnings
        self.metadata = {}
        self.points = []                 # [{'owl', 'lat', 'lon', 'ts'}]
        self.thumbs = {}                 # owl -> deque[(seq, bytes, received_at)]
        self.last_capture_at = {}        # owl -> monotonic time of last capture event
        self.upload_requests = {}        # id -> request dict
        self.camera_restart = set()      # OWLs to restart for a rotation change
        self._thumb_seq = 0

    # -- run lifecycle --------------------------------------------------------
    def start(self, metadata):
        with self.lock:
            self.run_id = uuid.uuid4().hex[:12]
            self.started_at = time.time()
            self.started_mono = time.monotonic()
            self.metadata = dict(metadata)
            self.points = []
            self.last_capture_at = {}
            return self.run_id

    def stop(self):
        with self.lock:
            self.started_at = None
            self.started_mono = None

    # -- from MQTT ------------------------------------------------------------
    def add_capture(self, owl_id, event):
        """owl/<id>/capture: {'count', 'ts', 'lat'?, 'lon'?, 'session_id'}."""
        with self.lock:
            self.last_capture_at[owl_id] = time.monotonic()
            if self.started_at is None:
                return
            lat, lon = event.get('lat'), event.get('lon')
            if lat is None or lon is None or len(self.points) >= MAX_POINTS:
                return
            try:
                self.points.append({'owl': owl_id, 'lat': round(float(lat), 7),
                                    'lon': round(float(lon), 7), 'ts': event.get('ts')})
            except (TypeError, ValueError):
                pass

    def add_thumb(self, owl_id, jpeg_bytes):
        if not jpeg_bytes or jpeg_bytes[:2] != b'\xff\xd8':
            return
        with self.lock:
            self._thumb_seq += 1
            queue = self.thumbs.setdefault(owl_id, deque(maxlen=THUMBS_PER_OWL))
            queue.append((self._thumb_seq, bytes(jpeg_bytes), time.time()))

    # -- for the screen -------------------------------------------------------
    def points_after(self, after=0):
        with self.lock:
            after = max(0, int(after))
            return {'run_id': self.run_id, 'points': self.points[after:], 'next': len(self.points)}

    def thumb(self, owl_id, index=0):
        """index 0 = latest, 1 = previous. Returns (seq, bytes) or None."""
        with self.lock:
            queue = self.thumbs.get(owl_id)
            if not queue or index >= len(queue):
                return None
            seq, data, _ = queue[-1 - index]
            return seq, data

    def thumb_index(self):
        with self.lock:
            return {owl: [seq for seq, _, _ in reversed(q)] for owl, q in self.thumbs.items()}

    # -- uploads --------------------------------------------------------------
    def request_uploads(self, items):
        """items: [{'owl', 'session_id'}]. Returns the new requests.

        These ride on the controller's bridged state (upload_requests) and the
        Noktura worker turns each one into a transfer_session for that OWL.
        A session still waiting for Noktura (requested / queued) is not
        requested again. Accepted ones may be: the transfer can fail after
        Noktura accepted it, and a fresh request is the retry. Noktura
        dedups on the request id and answers a session that is already
        uploading or stored with accepted / rejected, so a repeat is harmless.
        """
        now = time.time()
        created = []
        with self.lock:
            open_keys = {(r['local_id'], r['session_id']) for r in self.upload_requests.values()
                         if r['status'] in ('requested', 'queued')}
            for item in items:
                key = (str(item.get('owl', '')), str(item.get('session_id', '')))
                if not key[0] or not key[1] or key in open_keys:
                    continue
                request = {'id': uuid.uuid4().hex, 'local_id': key[0], 'session_id': key[1],
                           'requested_at': now, 'status': 'requested', 'reason': ''}
                self.upload_requests[request['id']] = request
                open_keys.add(key)
                created.append(dict(request))
        return created

    def ack_upload(self, request_id, status, reason=''):
        """Noktura's reply (upload_request_ack): queued = seen, waiting for
        the OWL's current upload or its session list; accepted = upload
        queued for that OWL; rejected = refused, with a reason."""
        if status not in ('queued', 'accepted', 'rejected'):
            return False
        with self.lock:
            request = self.upload_requests.get(str(request_id))
            if request is None:
                return False
            request['status'] = status
            request['reason'] = str(reason or '')[:200]
            request['acked_at'] = time.time()
            return True

    def bridged_upload_requests(self):
        """Requests Noktura still has to act on (requested or queued), for
        the controller's cloud-bridged state. Old ones age out so a lost ack
        cannot pin a request forever."""
        now = time.time()
        with self.lock:
            for rid in [rid for rid, r in self.upload_requests.items()
                        if now - r['requested_at'] > UPLOAD_REQUEST_TTL_S]:
                del self.upload_requests[rid]
            return [{k: r[k] for k in ('id', 'local_id', 'session_id', 'requested_at')}
                    for r in self.upload_requests.values() if r['status'] in ('requested', 'queued')]

    def upload_list(self):
        with self.lock:
            return sorted((dict(r) for r in self.upload_requests.values()),
                          key=lambda r: r['requested_at'], reverse=True)
