"""Unit tests for utils/software_update.py — the pieces shared by the MQTT
update_software command and the dashboard's /api/settings/update routes."""

import json
import os
import time
from unittest.mock import MagicMock

import pytest

from utils import software_update as swu


@pytest.mark.unit
class TestValidRef:
    @pytest.mark.parametrize('ref', ['main', 'display-bug', 'v3.15.0',
                                     'feature/x', 'a.b_c-d'])
    def test_accepts_branches_and_tags(self, ref):
        assert swu.valid_ref(ref) is True

    @pytest.mark.parametrize('ref', [
        '../etc/passwd', 'a..b', 'https://evil.com/repo', 'main; rm -rf x',
        '-rf', '', 'branch name with spaces', None, 'x' * 101,
    ])
    def test_rejects_traversal_options_and_junk(self, ref):
        assert swu.valid_ref(ref) is False


@pytest.mark.unit
class TestUpdateArgv:
    def test_matches_the_sudoers_literal(self, tmp_path):
        """The MQTT handler test pins this exact list; both callers must
        produce it or the sudoers rule silently denies the launch."""
        argv = swu.update_argv('main', 'req-1', script=tmp_path / 'owl_update.sh',
                               status_file=tmp_path / '.update_status.json',
                               user='owl')
        assert argv == [
            'sudo', '-n', '/usr/bin/systemd-run',
            '--unit=owl-update', '--collect',
            '--property=RuntimeMaxSec=1800',
            '--uid=owl', '--gid=owl',
            '/bin/bash', str(tmp_path / 'owl_update.sh'),
            '--unattended', '--ref', 'main',
            '--request-id', 'req-1',
            '--status-file', str(tmp_path / '.update_status.json'),
        ]

    def test_defaults_point_at_the_repo(self):
        argv = swu.update_argv('main', 'r', user='owl')
        assert argv[9].endswith('owl_update.sh')
        assert argv[-1].endswith('.update_status.json')


@pytest.mark.unit
class TestStatusFile:
    def test_read_normalises_types(self, tmp_path):
        path = tmp_path / '.update_status.json'
        path.write_text(json.dumps({'request_id': 7, 'status': 'fetching',
                                    'rollback_failed': 'yes', 'updated_at': '12'}))
        status = swu.read_status(path)
        assert status == {
            'request_id': '7', 'status': 'fetching', 'ref': '', 'from': '',
            'to': '', 'error': '', 'rollback_failed': True, 'updated_at': 12,
        }

    def test_missing_or_bad_file_is_none(self, tmp_path):
        assert swu.read_status(tmp_path / 'nope.json') is None
        bad = tmp_path / 'bad.json'
        bad.write_text('{not json')
        assert swu.read_status(bad) is None

    def test_idle_status_is_terminal(self):
        assert swu.idle_status()['status'] == 'idle'
        assert swu.is_terminal('idle') and swu.is_terminal('complete')
        assert not swu.is_terminal('fetching')

    def test_in_progress_respects_staleness(self):
        now = 1_000_000
        fresh = {'status': 'fetching', 'updated_at': now - 10}
        stale = {'status': 'fetching', 'updated_at': now - swu.STALE_AFTER_S - 1}
        done = {'status': 'complete', 'updated_at': now}
        assert swu.in_progress(fresh, now=now) is True
        assert swu.in_progress(stale, now=now) is False
        assert swu.in_progress(done, now=now) is False
        assert swu.in_progress(None, now=now) is False

    def test_seed_is_atomic_and_readable(self, tmp_path):
        path = tmp_path / '.update_status.json'
        before = int(time.time())
        seeded = swu.write_seed_status(path, 'req-9', 'main')
        assert seeded['status'] == 'starting'
        assert seeded['request_id'] == 'req-9'
        on_disk = swu.read_status(path)
        assert on_disk['request_id'] == 'req-9'
        assert on_disk['updated_at'] >= before
        # No temp file left behind
        assert [p.name for p in tmp_path.iterdir()] == ['.update_status.json']


@pytest.mark.unit
class TestSudoAndLaunch:
    def test_sudo_permits_strips_sudo_prefix(self):
        runner = MagicMock(return_value=MagicMock(returncode=0))
        argv = ['sudo', '-n', '/usr/bin/systemd-run', '--unit=owl-update']
        assert swu.sudo_permits(argv, runner) is True
        called = runner.call_args[0][0]
        assert called == ['sudo', '-n', '-l', '/usr/bin/systemd-run',
                          '--unit=owl-update']

    def test_sudo_permits_false_on_denial_or_error(self):
        denied = MagicMock(return_value=MagicMock(returncode=1))
        assert swu.sudo_permits(['sudo', '-n', 'x'], denied) is False
        boom = MagicMock(side_effect=OSError('no sudo'))
        assert swu.sudo_permits(['sudo', '-n', 'x'], boom) is False

    def test_launch_detaches_output(self):
        popen = MagicMock()
        swu.launch(['sudo', '-n', 'x'], popen)
        kwargs = popen.call_args[1]
        assert kwargs['stdout'] == swu.subprocess.DEVNULL
        assert kwargs['stderr'] == swu.subprocess.DEVNULL
