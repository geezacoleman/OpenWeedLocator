"""Tests never write to anything real.

An audit hook (sys.addaudithook) sees every file opened for writing and every
rename, delete, mkdir, chmod and truncate in the test process, whatever code
path made it. A target outside the scratch areas fails the test that asked
for it, by name, before anything is written.

Why: until 3.27.3 the MQTT tests rewrote config/active_config.txt and
config/config_autosave.ini in the checkout they ran from, so running pytest
on a unit changed the config it booted with.

Allowed: the temp directory (pytest's tmp_path lives there), /dev, bytecode
caches, pytest's own cache, and the per-user cache directories third-party
libraries keep (matplotlib, fontconfig).

Child processes are not seen by the hook. tests/conftest.py also compares
the repository tree before and after the run as a backstop.

OWL_TEST_WRITE_GUARD=report lists the writes in $TMPDIR/owl_test_writes.log
instead of failing (for finding offenders).
"""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODE = os.environ.get('OWL_TEST_WRITE_GUARD', 'enforce')

_TMP = os.path.realpath(tempfile.gettempdir())
_HOME = os.path.realpath(os.path.expanduser('~'))
_ALLOWED_ROOTS = tuple(p + os.sep for p in (
    _TMP, '/tmp', '/var/tmp', '/dev', '/proc/self', '/run/user',
    os.path.join(_HOME, '.cache'), os.path.join(_HOME, '.config', 'matplotlib'),
    os.path.join(_HOME, '.matplotlib'),
    os.path.join(str(REPO), '.pytest_cache'),
))
# pytest builds its cache in a 'pytest-cache-files-*' folder beside the
# rootdir before renaming it to .pytest_cache
_ALLOWED_PARTS = (os.sep + '__pycache__' + os.sep, os.sep + '.pytest_cache' + os.sep,
                  os.sep + 'pytest-cache-files-')
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

# event -> indexes of the path arguments that get modified
_PATH_EVENTS = {
    'os.remove': (0,), 'os.rmdir': (0,), 'os.mkdir': (0,), 'os.rename': (0, 1),
    'os.truncate': (0,), 'os.chmod': (0,), 'os.chown': (0,), 'os.link': (1,),
    'os.symlink': (1,), 'os.utime': (0,), 'shutil.rmtree': (0,),
}

_armed = False
# What was refused since the last check. Production code often catches
# OSError around a write, which would hide the refusal from the test, so
# tests/conftest.py also fails any test that leaves an entry here.
violations = []
_log_path = os.path.join(_TMP, 'owl_test_writes.log')


class RealWriteError(PermissionError):
    """A test tried to change a file outside the scratch areas."""


def _allowed(path):
    try:
        if isinstance(path, int):       # an already-open descriptor
            return True
        full = os.path.realpath(os.fsdecode(path))
    except (TypeError, ValueError, OSError):
        return True
    probe = full + os.sep
    return probe.startswith(_ALLOWED_ROOTS) or any(part in probe for part in _ALLOWED_PARTS)


def _violation(event, path):
    test = os.environ.get('PYTEST_CURRENT_TEST', '(outside a test)')
    full = os.path.realpath(os.fsdecode(path))
    if MODE == 'report':
        fd = os.open(_log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, f'{test}\t{event}\t{full}\n'.encode())
        finally:
            os.close(fd)
        return
    violations.append(f'{event}: {full}')
    raise RealWriteError(
        f'{test} tried to change a real file ({event}): {full}\n'
        f'Tests only write under tmp_path. Point the code at a temp directory '
        f'(see tests/README.md, "Tests never write to anything real").')


def _hook(event, args):
    if not _armed:
        return
    if event == 'open':
        path, mode, flags = args[0], args[1], args[2]
        if path is None or not (flags & _WRITE_FLAGS):
            return
        if not _allowed(path):
            _violation('open for writing', path)
    elif event in _PATH_EVENTS:
        # (path, dir_fd) calls from shutil.rmtree and tempfile cleanup name a
        # file relative to an open directory, which cannot be resolved here;
        # the rmtree itself is checked by its own event
        if event in ('os.remove', 'os.rmdir', 'os.mkdir') and len(args) > 1 and args[1] is not None \
                and not os.path.isabs(os.fsdecode(args[0])):
            return
        for index in _PATH_EVENTS[event]:
            if index < len(args) and args[index] is not None and not _allowed(args[index]):
                _violation(event, args[index])


def install():
    """Start guarding (idempotent). Audit hooks cannot be removed; disarm()
    stops this one acting."""
    global _armed
    if not getattr(sys, '_owl_write_guard', False):
        sys.addaudithook(_hook)
        sys._owl_write_guard = True
    _armed = MODE != 'off'


def disarm():
    global _armed
    _armed = False
