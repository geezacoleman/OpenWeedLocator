#!/usr/bin/env python3
"""Hand a software update that was cut off to owl_update.sh --recover.

owl_update.sh writes .update_journal.json (fsync'd) as soon as it has a
rollback point and removes it when the run ends. Still there at start-up,
with no update holding the lock, means the run was cut off part-way (a
power cut during installing_deps, a reboot, kill -9), possibly leaving the
tree half on the new version.

Run from owl.service as ExecStartPre (system python3 -I, so a half-installed
virtualenv cannot stop it) and again at owl.py / dashboard start for units
whose unit file predates it. Standard library only. Never raises and never
blocks start-up: it launches the recovery in its own transient unit (the
same sudoers-approved argv as an update, plus --recover) and returns.

Gives up after MAX_ATTEMPTS recoveries of the same journal so a recovery that
keeps failing cannot loop with Restart=always.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_ATTEMPTS = 2


def _software_update():
    """utils/software_update.py by path: importing the utils package could
    pull in code the cut-off update left half-written."""
    spec = importlib.util.spec_from_file_location('owl_software_update',
                                                  ROOT / 'utils' / 'software_update.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lock_is_free(path):
    """True when no owl_update.sh holds .update_lock (flock)."""
    try:
        import fcntl
    except ImportError:
        return False  # not Linux: never recover here
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def read_journal(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def recovery_argv(swu, journal, root=ROOT):
    ref = str(journal.get('ref') or 'recover')
    if not swu.valid_ref(ref):
        ref = 'recover'
    argv = swu.update_argv(ref, str(journal.get('request_id') or 'recover'),
                           script=root / swu.SCRIPT_NAME,
                           status_file=swu.status_path(root))
    return argv + ['--recover']


def check(root=ROOT, popen=None, log=print):
    """Launch a recovery when one is needed. Returns what it did:
    'none', 'busy', 'given_up', 'launched' or 'failed'."""
    journal_file = Path(root) / '.update_journal.json'
    if not journal_file.exists():
        return 'none'
    if not lock_is_free(Path(root) / '.update_lock'):
        return 'busy'  # an update is running: it owns the journal
    journal = read_journal(journal_file) or {}
    if int(journal.get('attempts', 0) or 0) >= MAX_ATTEMPTS:
        log(f'update_guard: recovery already tried {journal.get("attempts")} times; '
            f'run owl_update.sh --recover by hand')
        return 'given_up'
    try:
        swu = _software_update()
        argv = recovery_argv(swu, journal, Path(root))
        (popen or subprocess.Popen)(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f'update_guard: could not launch recovery: {e}')
        return 'failed'
    log('update_guard: an update was cut off part-way; rolling it back')
    return 'launched'


def main():
    try:
        check()
    except Exception as e:  # never stop the OWL from starting
        print(f'update_guard: {e}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
