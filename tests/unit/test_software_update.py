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

    def test_with_ai_appends_the_flag_after_the_script(self, tmp_path):
        """Install AI: --with-ai goes where the sudoers rule has its `*`;
        everything up to the script stays byte-for-byte the same."""
        kwargs = dict(script=tmp_path / 'owl_update.sh',
                      status_file=tmp_path / '.update_status.json', user='owl')
        plain = swu.update_argv('main', 'req-1', **kwargs)
        with_ai = swu.update_argv('main', 'req-1', with_ai=True, **kwargs)
        assert with_ai == plain + ['--with-ai']
        assert swu.update_argv('main', 'req-1', with_ai=False, **kwargs) == plain

    @pytest.mark.parametrize('script', ['controller/shared/setup.sh', 'owl_cloud_provision.sh'])
    def test_sudoers_rule_matches_the_argv_prefix(self, script):
        """The sudoers rule is literal up to owl_update.sh and `*` after it,
        so with_ai needs no sudoers change but RuntimeMaxSec must match."""
        root = swu.repo_root()
        text = (root / script).read_text(encoding='utf-8')
        rule = next(line for line in text.splitlines()
                    if 'Cmnd_Alias' in line and 'owl_update.sh' in line)
        assert rule.rstrip().endswith('owl_update.sh *')
        assert f'--property=RuntimeMaxSec={swu.RUNTIME_MAX_S} ' in rule

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
        # now= keeps it live: effective_status turns an old run 'interrupted'
        status = swu.read_status(path, now=13, boot_id='')
        assert status == {
            'request_id': '7', 'status': 'fetching', 'ref': '', 'from': '',
            'to': '', 'error': '', 'rollback_failed': True, 'started_at': 0,
            'updated_at': 12, 'boot_id': '',
        }

    def test_missing_or_bad_file_is_none(self, tmp_path):
        assert swu.read_status(tmp_path / 'nope.json') is None
        bad = tmp_path / 'bad.json'
        bad.write_text('{not json')
        assert swu.read_status(bad) is None

    def test_import_check_phase_runs_before_the_restart(self):
        assert swu.PHASES.index('import_check') == swu.PHASES.index('restarting') - 1

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


class TestUpdaterStashStep:
    """owl_update.sh's stash step, run under bash in a throwaway git repo.

    Field failure 2026-11-19: on a unit whose tracked files had only been
    touched (same content, new timestamp), `git diff-index` said modified,
    `git stash push` said "No local changes to save", and the script died on
    `git rev-parse stash@{0}`: the update never started.
    """

    @staticmethod
    def _step():
        import re
        from pathlib import Path
        script = (Path(__file__).resolve().parents[2] / 'owl_update.sh').read_text()
        block = re.search(r'    STASHED=0\n    # Refresh first:.*?\n    fi\n', script, re.S)
        assert block, 'the stash step moved: update this test'
        return block.group(0)

    def _run(self, tmp_path, prepare):
        import subprocess
        repo = tmp_path / 'repo'
        repo.mkdir()
        harness = f'''set -Eeuo pipefail
cd "{repo}"
git init -q . && git config user.email t@t && git config user.name t
mkdir config && echo "relay_num = 4" > config/GENERAL_CONFIG.ini
git add . && git commit -qm init
{prepare}
TS=test; STASH_SHA=""
log() {{ echo "LOG: $*"; }}
write_journal() {{ echo "JOURNAL: $STASH_SHA"; }}
step() {{
{self._step()}
}}
step
echo "STASHED=$STASHED SHA=$STASH_SHA"
'''
        return subprocess.run(['bash', '-c', harness], capture_output=True, text=True, timeout=60)

    def test_a_file_that_was_only_touched_does_not_stop_the_update(self, tmp_path):
        run = self._run(tmp_path, 'sleep 1; touch config/GENERAL_CONFIG.ini')
        assert run.returncode == 0, run.stdout + run.stderr
        assert 'STASHED=0 SHA=\n' in run.stdout
        assert 'JOURNAL' not in run.stdout

    def test_a_clean_checkout_stashes_nothing(self, tmp_path):
        run = self._run(tmp_path, ':')
        assert run.returncode == 0, run.stdout + run.stderr
        assert 'STASHED=0 SHA=\n' in run.stdout

    def test_a_real_field_change_is_stashed_and_recorded(self, tmp_path):
        run = self._run(tmp_path, 'echo "relay_num = 8" > config/GENERAL_CONFIG.ini')
        assert run.returncode == 0, run.stdout + run.stderr
        import re
        [sha] = re.findall(r'STASHED=1 SHA=([0-9a-f]{40})', run.stdout)
        assert f'JOURNAL: {sha}' in run.stdout

    def test_an_older_stash_is_not_mistaken_for_this_runs(self, tmp_path):
        """A stash already on the unit, then a touched-only file: nothing is
        stashed now, and the old stash must not be recorded (and later popped)."""
        run = self._run(tmp_path, 'echo "x = 1" >> config/GENERAL_CONFIG.ini; git stash push -q -m old; '
                                  'sleep 1; touch config/GENERAL_CONFIG.ini')
        assert run.returncode == 0, run.stdout + run.stderr
        assert 'STASHED=0 SHA=\n' in run.stdout
