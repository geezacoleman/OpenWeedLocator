"""
Shared pieces of the software-update path.

owl_update.sh does the work (git fetch, checkout, pip, service restart,
rollback) and reports through a JSON status file. Two callers launch it:
the MQTT `update_software` command (fleet / cloud path, from owl.py) and the
standalone dashboard's /api/settings/update/start (phone app). Both must
build the SAME argv, because the sudoers rule installed by
controller/shared/setup.sh and owl_cloud_provision.sh matches it literally
up to the script's own arguments.

Plain module functions; no state. Injection points (runner/popen) default
to the subprocess module looked up at call time so tests can patch
subprocess.run / subprocess.Popen the way they already do.
"""

import getpass
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

# Ref allowlist shared with owl_update.sh preflight
UPDATE_REF_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$'
TERMINAL_STATUSES = ('idle', 'complete', 'rolled_back', 'error')
# In the order owl_update.sh writes them
PHASES = ('starting', 'preflight', 'fetching', 'stashing', 'checking_out',
          'restoring_config', 'installing_deps', 'restarting', 'health_check')

UNIT_NAME = 'owl-update'
RUNTIME_MAX_S = 1800
SYSTEMD_RUN = '/usr/bin/systemd-run'
# A non-terminal status older than this is a crashed run (the transient
# unit is capped at RUNTIME_MAX_S), not an update in progress
STALE_AFTER_S = 1800

STATUS_FILE_NAME = '.update_status.json'
SCRIPT_NAME = 'owl_update.sh'


def repo_root():
    return Path(__file__).resolve().parents[1]


def status_path(root=None):
    return Path(root or repo_root()) / STATUS_FILE_NAME


def script_path(root=None):
    return Path(root or repo_root()) / SCRIPT_NAME


def valid_ref(ref):
    """Branch or tag name the updater accepts (no traversal, no options)."""
    ref = str(ref or '')
    return bool(re.match(UPDATE_REF_PATTERN, ref)) and '..' not in ref


def update_argv(ref, request_id, script=None, status_file=None, user=None):
    """The exact argv the sudoers rule permits (literal up to the script)."""
    user = user or getpass.getuser()
    return [
        'sudo', '-n', SYSTEMD_RUN,
        f'--unit={UNIT_NAME}', '--collect',
        f'--property=RuntimeMaxSec={RUNTIME_MAX_S}',
        f'--uid={user}', f'--gid={user}',
        '/bin/bash', str(script or script_path()),
        '--unattended', '--ref', str(ref),
        '--request-id', str(request_id),
        '--status-file', str(status_file or status_path()),
    ]


def normalise_status(data):
    """The status-file fields the OWL publishes, with safe types."""
    data = data or {}
    return {
        'request_id': str(data.get('request_id', '')),
        'status': str(data.get('status', 'idle')),
        'ref': str(data.get('ref', '')),
        'from': str(data.get('from', '')),
        'to': str(data.get('to', '')),
        'error': str(data.get('error', '')),
        'rollback_failed': bool(data.get('rollback_failed', False)),
        'updated_at': int(data.get('updated_at', 0) or 0),
    }


def idle_status():
    return normalise_status({})


def read_status(path):
    """Parsed status file, or None when absent/unreadable. The script writes
    it via temp+rename so a partial read is not possible."""
    try:
        with open(path) as f:
            return normalise_status(json.load(f))
    except (OSError, ValueError):
        return None


def is_terminal(status):
    return str(status) in TERMINAL_STATUSES


def in_progress(status_dict, now=None):
    """True when the file says an update is running and it is recent enough
    to believe (a crashed run leaves a stale non-terminal status behind)."""
    if not status_dict or is_terminal(status_dict.get('status')):
        return False
    now = now if now is not None else time.time()
    return (now - int(status_dict.get('updated_at', 0) or 0)) < STALE_AFTER_S


def write_seed_status(path, request_id, ref):
    """Write the 'starting' record before launching, atomically, so a poll
    that lands before the script's first write already sees this request."""
    path = Path(path)
    now = int(time.time())
    payload = {
        'request_id': str(request_id), 'status': 'starting', 'ref': str(ref),
        'from': '', 'to': '', 'error': '', 'rollback_failed': False,
        'started_at': now, 'updated_at': now, 'pid': 0, 'log_file': '',
    }
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.update_status.',
                               suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(payload, f)
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return normalise_status(payload)


def sudo_permits(argv, runner=None):
    """`sudo -n -l <command>` exits 0 when the rule exists. argv is the full
    launch argv (leading 'sudo -n' is stripped). Never raises."""
    run = runner or subprocess.run
    command = list(argv)
    while command and command[0] in ('sudo', '-n'):
        command = command[1:]
    try:
        result = run(['sudo', '-n', '-l'] + command, capture_output=True,
                     text=True, timeout=5)
    except Exception:
        return False
    return getattr(result, 'returncode', 1) == 0


def launch(argv, popen=None):
    """Fire-and-forget: the transient unit outlives the caller."""
    start = popen or subprocess.Popen
    return start(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
