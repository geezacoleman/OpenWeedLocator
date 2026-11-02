"""Support bundle: one zip of recent logs a farmer can email to support.

Both dashboards serve it on the same two routes (create_support_blueprint):

    POST /api/support/bundle            build it -> {url, filename, bytes, ...}
    GET  /api/support/bundle/<ticket>   the zip; reusable for TICKET_TTL_S

The ticket is the credential for the download, so a plain link (kiosk) and
the app's header-less native downloader both work. Who may build is up to the
dashboard: the OWL unit wants its device token, the networked controller is
open like its fleet routes.

Everything that goes in passes through redact(): passwords, tokens and keys
never leave the unit, and neither do GPS positions (the farmer is emailing
this). Fix, accuracy, speed and source stay, which is enough to debug GPS.
"""

import configparser
import io
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import zipfile
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = REPO_ROOT / 'logs'

DEFAULT_SUPPORT_EMAIL = 'support@noktura.tech'

OWL_UNITS = ('owl', 'owl-dash', 'owl-firstboot', 'owl-net-watchdog', 'mosquitto',
             'NetworkManager', 'nginx')
CONTROLLER_UNITS = ('owl-controller', 'mosquitto', 'NetworkManager', 'nginx')

# CONTROLLER.ini goes in by section ALLOWLIST: a secret key added later
# stays out by default. [Security] (device token), [Farm] (farm and operator
# names) and the rest of [Cloud] never leave.
CONTROLLER_SECTIONS = ('MQTT', 'WebDashboard', 'Network', 'GPS', 'Actuation',
                       'Coverage', 'Collect', 'Fleet', 'Support')
CLOUD_KEYS = ('enable', 'broker_host')

DEADLINE_S = 20          # whole build; the app waits 45 s
COMMAND_TIMEOUT_S = 5
JOURNAL_LINES = 6000
KERNEL_LINES = 3000
OWL_JSONL_TAIL = 4 * 2**20
DASHBOARD_LOG_TAIL = 2 * 2**20
UPDATE_LOG_TAIL = 2**20
APP_EVENTS_MAX = 300
APP_TEXT_MAX = 500

TICKET_TTL_S = 300
TICKETS_KEPT = 3

REMOVED = '[removed]'
POSITION_REMOVED = '[position removed]'

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
_SECRET_KEY = r'(?:device_token|token|password|passwd|psk|secret|api[_-]?key|credentials?)'
_POSITION_KEY = (r'(?:latitude|longitude|lat|lon|lng|gps_latitude|gps_longitude|'
                 r'gps_best_lat|gps_best_lon)')

_REDACTIONS = [
    # PEM private keys (whole block)
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S),
     '-----PRIVATE KEY ' + REMOVED + '-----'),
    # nmcli on a log line or a sudo COMMAND= line: everything after the psk
    # or password argument (a passphrase may contain spaces)
    (re.compile(r'(?im)((?:wifi-sec|802-11-wireless-security)\.psk[\s:=]+).*$'), r'\1' + REMOVED),
    (re.compile(r'(?im)(\bnmcli\b.*?\bpassword\s+).*$'), r'\1' + REMOVED),
    # key = value / key: value / "key": value / 'key': value
    (re.compile(r'(?i)(["\']?\b[\w.-]*?' + _SECRET_KEY + r'["\']?\s*[:=]\s*)'
                r'(?!\[removed\])("[^"]*"|\'[^\']*\'|[^\s,;}\])]+)'), r'\1' + REMOVED),
    (re.compile(r'(?i)(\bbearer\s+)[\w.~+/=-]+'), r'\1' + REMOVED),
    (re.compile(r'\beyJ[\w-]+\.[\w-]+\.[\w-]+'), REMOVED),
    # https://user:pass@host
    (re.compile(r'(\b[a-z][\w+.-]*://)[^/\s:@]+:[^/\s@]+@', re.I), r'\1' + REMOVED + '@'),
    # signed URLs
    (re.compile(r'(?i)([?&](?:token|signature|sig|x-amz-signature|x-amz-credential|key)=)[^&\s"\']+'),
     r'\1' + REMOVED),
    # positions: keyed values (word-bounded, so latency_ms is untouched)
    (re.compile(r'(?i)(["\']?\b' + _POSITION_KEY + r'\b["\']?\s*[:=]\s*)["\']?-?\d+(?:\.\d+)?["\']?'),
     r'\1' + REMOVED),
    # NMEA position sentences and the nRF91 #XGPS line
    (re.compile(r'\$G[A-Z](?:GGA|RMC|GLL|GNS)[^\r\n]*'), POSITION_REMOVED),
    (re.compile(r'#XGPS:[^\r\n]*'), '#XGPS: ' + POSITION_REMOVED),
    # a bare "lat, lon" pair
    (re.compile(r'-?\b\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}\b'), POSITION_REMOVED),
]

_SECRET_KEY_NAME = re.compile(r'(?i)' + _SECRET_KEY)
_POSITION_KEY_NAME = re.compile(r'(?i)^' + _POSITION_KEY + r'$')


def redact(text):
    """Remove secrets and GPS positions from free text."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def redact_data(value):
    """Walk a JSON-like structure: secret- and position-named keys are
    removed, every string is redacted."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = str(key)
            if _SECRET_KEY_NAME.search(name) or _POSITION_KEY_NAME.match(name):
                out[key] = REMOVED if item not in (None, '') else item
            else:
                out[key] = redact_data(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_data(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


# ---------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------
def support_email(controller_ini_path=None):
    """[Support] email from CONTROLLER.ini, else the Noktura support inbox."""
    if controller_ini_path:
        config = configparser.ConfigParser()
        try:
            config.read(controller_ini_path)
        except configparser.Error:
            return DEFAULT_SUPPORT_EMAIL
        email = config.get('Support', 'email', fallback='').strip()
        if re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
            return email
    return DEFAULT_SUPPORT_EMAIL


def controller_ini_text(path):
    """CONTROLLER.ini with only the allowlisted sections and [Cloud] keys."""
    source = configparser.ConfigParser()
    source.read(path)
    kept = configparser.ConfigParser()
    for section in CONTROLLER_SECTIONS:
        if source.has_section(section):
            kept[section] = dict(source[section])
    if source.has_section('Cloud'):
        cloud = {k: v for k, v in source['Cloud'].items() if k in CLOUD_KEYS}
        if cloud:
            kept['Cloud'] = cloud
    buffer = io.StringIO()
    kept.write(buffer)
    return redact(buffer.getvalue())


def tail_text(path, limit):
    """The last `limit` bytes of a file, from the first full line."""
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - limit))
        data = f.read()
    if size > limit:
        data = data.split(b'\n', 1)[-1]
    return data.decode('utf-8', errors='replace')


def app_report_text(report):
    """The phone app's own report: strings only, capped."""
    if not isinstance(report, dict):
        return None

    def clean(value, limit):
        return value[:limit].replace('\r', ' ').replace('\n', ' ') if isinstance(value, str) else ''

    lines = [f"App version: {clean(report.get('app_version'), 40) or 'unknown'}",
             f"Device: {clean(report.get('device'), 300) or 'unknown'}",
             '', 'Recent app events (oldest first):']
    events = report.get('events') if isinstance(report.get('events'), list) else []
    for event in events[-APP_EVENTS_MAX:]:
        if not isinstance(event, dict):
            continue
        lines.append(' '.join(part for part in (
            clean(event.get('time'), 40), clean(event.get('kind'), 20),
            clean(event.get('text'), APP_TEXT_MAX)) if part))
    return redact('\n'.join(lines) + '\n')


def journal_readable():
    """journalctl exits 0 with no entries when the user can't read the
    journal, so ask the groups instead."""
    if not hasattr(os, 'getgroups'):
        return False
    try:
        import grp
        if os.geteuid() == 0:
            return True
        names = {grp.getgrgid(g).gr_name for g in os.getgroups()}
    except (ImportError, KeyError, OSError):
        return False
    return bool(names & {'adm', 'systemd-journal', 'wheel'})


class _Runner:
    """subprocess.run with one deadline for the whole build: a command
    started late gets what time is left, and none start after it."""

    def __init__(self, deadline_s, run=subprocess.run, clock=time.monotonic):
        self._run = run
        self._clock = clock
        self._end = clock() + deadline_s
        self.skipped = []

    def __call__(self, argv):
        left = self._end - self._clock()
        if left <= 0.5:
            self.skipped.append(' '.join(argv))
            return None
        try:
            result = self._run(argv, capture_output=True, text=True,
                               timeout=min(COMMAND_TIMEOUT_S, left))
        except (OSError, subprocess.SubprocessError) as e:
            return f'({argv[0]} failed: {e.__class__.__name__})\n'
        return (result.stdout or '') + (result.stderr or '')


def _health_lines(units, runner):
    """Service states and power: run first, the most useful lines when the
    journal is slow."""
    lines = ['services:']
    for unit in units:
        state = runner(['systemctl', 'is-active', f'{unit}.service'])
        lines.append(f'  {unit}: {(state or "not checked").strip()}')
    lines.append('')
    for argv in (['vcgencmd', 'get_throttled'], ['vcgencmd', 'get_config', 'usb_max_current_enable']):
        out = runner(argv)
        if out is not None:
            lines.append(f"{' '.join(argv)}: {out.strip()}")
    return lines


def _summary_text(label, version_info, health, notes):
    lines = [f'OWL support bundle for {label}',
             f'Saved: {datetime.now().isoformat(timespec="seconds")}', '']
    for key in ('version', 'git_branch', 'git_commit'):
        lines.append(f'{key}: {(version_info or {}).get(key)}')
    try:
        from version import SystemInfo
        os_info = SystemInfo.get_os_info()
        lines.append(f"os: {os_info.get('system')} {os_info.get('release')} {os_info.get('machine')}")
        lines.append(f'model: {SystemInfo.get_rpi_info()}')
    except Exception:
        pass
    try:
        with open('/proc/uptime') as f:
            lines.append(f'uptime_s: {int(float(f.read().split()[0]))}')
    except OSError:
        pass
    lines += [''] + health
    if notes:
        lines += ['', 'notes:'] + [f'  {note}' for note in notes]
    return '\n'.join(lines) + '\n'


def build_bundle(*, label, units, version_info=None, stats=None, controller_ini_path=None,
                 profile_path=None, log_files=(), app_report=None,
                 deadline_s=DEADLINE_S, run=subprocess.run, clock=time.monotonic,
                 journal_access=None):
    """Return (filename, zip bytes).

    log_files: (arcname, path, tail_limit) for files to include tail-capped.
    """
    runner = _Runner(deadline_s, run=run, clock=clock)
    notes = []
    members = []
    health = _health_lines(units, runner)

    if stats is not None:
        members.append(('stats.json', json.dumps(redact_data(stats), indent=2, default=str)))
    if profile_path and os.path.isfile(profile_path):
        members.append((f'config/{Path(profile_path).name}', redact(Path(profile_path).read_text(
            encoding='utf-8', errors='replace'))))
    if controller_ini_path and os.path.isfile(controller_ini_path):
        try:
            members.append(('config/CONTROLLER.ini', controller_ini_text(controller_ini_path)))
        except configparser.Error as e:
            notes.append(f'CONTROLLER.ini unreadable: {e.__class__.__name__}')
    for arcname, path, limit in log_files:
        try:
            members.append((arcname, redact(tail_text(path, limit))))
        except OSError:
            notes.append(f'{arcname} unreadable')

    readable = journal_readable() if journal_access is None else journal_access
    if readable:
        boots = runner(['journalctl', '--list-boots', '--no-pager', '-q']) or ''
        offsets = [0] + ([-1] if len([l for l in boots.splitlines() if l.strip()]) >= 2 else [])
        if offsets == [0]:
            notes.append('no previous boot in the journal (not persistent, or first boot)')
        unit_args = [arg for unit in units for arg in ('-u', f'{unit}.service')]
        for offset in offsets:
            tag = f'boot{-offset}'
            services = runner(['journalctl', '-b', str(offset), *unit_args, '-n', str(JOURNAL_LINES),
                               '--no-pager', '-o', 'short-iso'])
            if services is not None:
                members.append((f'journal/services-{tag}.txt', redact(services)))
            kernel = runner(['journalctl', '-k', '-b', str(offset), '-n', str(KERNEL_LINES),
                             '--no-pager', '-o', 'short-iso'])
            if kernel is not None:
                members.append((f'journal/kernel-{tag}.txt', redact(kernel)))
    else:
        notes.append('journal not readable by this user (add it to the adm or systemd-journal group)')

    app_text = app_report_text(app_report)
    if app_text:
        members.append(('app.txt', app_text))

    summary = _summary_text(label, version_info, health, notes)
    if runner.skipped:
        summary += '\nskipped (time limit):\n' + ''.join(f'  {cmd}\n' for cmd in runner.skipped)
    members.insert(0, ('summary.txt', summary))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for arcname, text in members:
            zf.writestr(arcname, text)
    safe_label = re.sub(r'[^A-Za-z0-9-]+', '-', label or 'owl').strip('-')[:40] or 'owl'
    filename = f'owl_logs_{safe_label}_{datetime.now().strftime("%Y%m%d-%H%M")}.zip'
    return filename, buffer.getvalue()


def owl_log_files(log_dir=LOG_DIR):
    """The OWL unit's own log files, newest first where there are many."""
    log_dir = Path(log_dir)
    files = []
    for name in ('owl.jsonl', 'owl.jsonl.1'):
        if (log_dir / name).is_file():
            files.append((f'logs/{name}', str(log_dir / name), OWL_JSONL_TAIL))
    for path in sorted(log_dir.glob('owl_dashboard_*.log'), reverse=True)[:3]:
        files.append((f'logs/{path.name}', str(path), DASHBOARD_LOG_TAIL))
    updates = log_dir / 'updates'
    if updates.is_dir():
        for path in sorted(updates.glob('*.log'), key=lambda p: p.stat().st_mtime, reverse=True)[:2]:
            files.append((f'logs/updates/{path.name}', str(path), UPDATE_LOG_TAIL))
    return files


# ---------------------------------------------------------------------------
# Tickets + routes
# ---------------------------------------------------------------------------
class BundleStore:
    """Built bundles by ticket, for TICKET_TTL_S, at most TICKETS_KEPT."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._items = OrderedDict()
        self.building = threading.Lock()

    def put(self, filename, data):
        ticket = secrets.token_urlsafe(32)
        with self._lock:
            self._items[ticket] = (self._clock() + TICKET_TTL_S, filename, data)
            while len(self._items) > TICKETS_KEPT:
                self._items.popitem(last=False)
        return ticket

    def get(self, ticket):
        with self._lock:
            item = self._items.get(ticket)
            if item is None:
                return None
            if self._clock() > item[0]:
                del self._items[ticket]
                return None
            return item[1], item[2]


def create_support_blueprint(build, guard=None, logger=None, store=None):
    """The two support routes. build(app_report) -> (filename, bytes, email,
    subject). guard: zero-arg callable returning an error response or None."""
    from flask import Blueprint, jsonify, request, send_file

    log = logger or logging.getLogger(__name__)
    store = store or BundleStore()
    bp = Blueprint('support', __name__)
    bp.store = store

    @bp.route('/api/support/bundle', methods=['POST'])
    def support_bundle_build():
        if guard is not None:
            denied = guard()
            if denied is not None:
                return denied
        if not store.building.acquire(blocking=False):
            return jsonify({'success': False, 'error': 'busy',
                            'detail': 'Logs are already being saved'}), 429
        try:
            body = request.get_json(silent=True) or {}
            filename, data, email, subject = build(body.get('app'))
        except Exception as e:
            log.error(f"Support bundle failed: {e}")
            return jsonify({'success': False, 'error': 'Could not save logs'}), 500
        finally:
            store.building.release()
        ticket = store.put(filename, data)
        log.info(f"Support bundle ready: {filename} ({len(data)} bytes)")
        return jsonify({'success': True, 'url': f'/api/support/bundle/{ticket}',
                        'filename': filename, 'bytes': len(data),
                        'support_email': email, 'subject': subject})

    @bp.route('/api/support/bundle/<ticket>')
    def support_bundle_download(ticket):
        item = store.get(ticket)
        if item is None:
            return jsonify({'success': False, 'error': 'Expired. Save the logs again.'}), 404
        filename, data = item
        return send_file(io.BytesIO(data), mimetype='application/zip',
                         as_attachment=True, download_name=filename)

    return bp
