"""v3.27 update transaction: a run cut off part-way is recoverable.

- A status from another boot, or too old to be live, reads as 'interrupted'
  (terminal), so it no longer blocks every later update.
- owl_update.sh keeps a journal while it can be rolled back; a journal left
  behind is handed to owl_update.sh --recover by tools/update_guard.py.
- launch() notices systemd-run refusing to start the unit.
"""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from utils import software_update as swu

ROOT = Path(__file__).resolve().parents[2]


def load_guard():
    spec = importlib.util.spec_from_file_location('update_guard', ROOT / 'tools' / 'update_guard.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def status(**kw):
    base = {'request_id': 'r1', 'status': 'installing_deps', 'updated_at': 1000, 'boot_id': 'boot-a'}
    base.update(kw)
    return swu.normalise_status(base)


@pytest.mark.unit
class TestEffectiveStatus:
    def test_live_status_is_left_alone(self):
        assert swu.effective_status(status(), now=1010, boot_id='boot-a')['status'] == 'installing_deps'

    def test_status_from_another_boot_is_interrupted(self):
        out = swu.effective_status(status(), now=1010, boot_id='boot-b')
        assert out['status'] == 'interrupted'
        assert out['error'] == swu.INTERRUPTED_ERROR
        assert swu.in_progress(out, now=1010) is False

    def test_stale_status_is_interrupted(self):
        out = swu.effective_status(status(), now=1000 + swu.STALE_AFTER_S, boot_id='boot-a')
        assert out['status'] == 'interrupted'

    def test_terminal_status_untouched(self):
        done = status(status='complete')
        assert swu.effective_status(done, now=10 ** 9, boot_id='other') == done

    def test_status_without_a_boot_id_falls_back_to_age(self):
        old = status(boot_id='')
        assert swu.effective_status(old, now=1010, boot_id='boot-b')['status'] == 'installing_deps'

    def test_read_status_applies_it(self, tmp_path):
        path = tmp_path / 'status.json'
        path.write_text(json.dumps(dict(status(), boot_id='boot-a')))
        assert swu.read_status(path, now=1010, boot_id='boot-z')['status'] == 'interrupted'

    def test_seed_status_carries_the_boot_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(swu, 'current_boot_id', lambda path=None: 'boot-q')
        path = tmp_path / 'status.json'
        swu.write_seed_status(path, 'r1', 'latest')
        assert json.loads(path.read_text())['boot_id'] == 'boot-q'
        swu.write_seed_status(path, 'r1', 'latest', status='error', error='nope')
        data = json.loads(path.read_text())
        assert (data['status'], data['error']) == ('error', 'nope')


@pytest.mark.unit
class TestLaunch:
    def test_refused_systemd_run_raises(self):
        proc = MagicMock()
        proc.wait.return_value = 1
        with pytest.raises(RuntimeError, match='exited with code 1'):
            swu.launch(['x'], MagicMock(return_value=proc))

    def test_unit_still_starting_is_fine(self):
        proc = MagicMock()
        proc.wait.side_effect = subprocess.TimeoutExpired('x', 3)
        assert swu.launch(['x'], MagicMock(return_value=proc)) is proc

    def test_clean_exit_is_fine(self):
        proc = MagicMock()
        proc.wait.return_value = 0
        assert swu.launch(['x'], MagicMock(return_value=proc)) is proc


@pytest.mark.unit
class TestUpdateGuard:
    def journal(self, root, **kw):
        data = {'rollback_commit': 'a' * 40, 'request_id': 'r9', 'ref': 'v3.27.0', 'attempts': 0}
        data.update(kw)
        (root / '.update_journal.json').write_text(json.dumps(data))

    def test_no_journal_nothing_to_do(self, tmp_path):
        assert load_guard().check(root=tmp_path, popen=MagicMock()) == 'none'

    def test_launches_recover_with_the_sudoers_argv(self, tmp_path, monkeypatch):
        guard = load_guard()
        monkeypatch.setattr(guard, 'lock_is_free', lambda path: True)
        self.journal(tmp_path)
        popen = MagicMock()
        assert guard.check(root=tmp_path, popen=popen, log=lambda m: None) == 'launched'
        argv = popen.call_args[0][0]
        expected_prefix = swu.update_argv('v3.27.0', 'r9', script=tmp_path / 'owl_update.sh',
                                          status_file=swu.status_path(tmp_path))
        assert argv == expected_prefix + ['--recover']
        # The sudoers rule matches literally up to the script, then '*'
        assert argv[:argv.index(str(tmp_path / 'owl_update.sh')) + 1] == \
            expected_prefix[:expected_prefix.index(str(tmp_path / 'owl_update.sh')) + 1]

    def test_busy_lock_means_an_update_is_running(self, tmp_path, monkeypatch):
        guard = load_guard()
        monkeypatch.setattr(guard, 'lock_is_free', lambda path: False)
        self.journal(tmp_path)
        assert guard.check(root=tmp_path, popen=MagicMock()) == 'busy'

    def test_gives_up_after_repeated_attempts(self, tmp_path, monkeypatch):
        guard = load_guard()
        monkeypatch.setattr(guard, 'lock_is_free', lambda path: True)
        self.journal(tmp_path, attempts=guard.MAX_ATTEMPTS)
        popen = MagicMock()
        assert guard.check(root=tmp_path, popen=popen, log=lambda m: None) == 'given_up'
        popen.assert_not_called()

    def test_bad_ref_in_journal_is_not_passed_on(self, tmp_path, monkeypatch):
        guard = load_guard()
        monkeypatch.setattr(guard, 'lock_is_free', lambda path: True)
        self.journal(tmp_path, ref='; rm -rf /')
        popen = MagicMock()
        guard.check(root=tmp_path, popen=popen, log=lambda m: None)
        argv = popen.call_args[0][0]
        assert argv[argv.index('--ref') + 1] == 'recover'

    def test_main_never_raises(self, monkeypatch):
        guard = load_guard()
        monkeypatch.setattr(guard, 'check', MagicMock(side_effect=RuntimeError('boom')))
        assert guard.main() == 0

    def test_stdlib_only(self):
        text = (ROOT / 'tools' / 'update_guard.py').read_text()
        for line in text.splitlines():
            if line.startswith(('import ', 'from ')):
                assert line.split()[1].split('.')[0] in {
                    'importlib', 'json', 'os', 'subprocess', 'sys', 'pathlib', 'fcntl'}


@pytest.mark.unit
class TestUpdaterScript:
    """owl_update.sh cannot run here; check the wiring it depends on."""

    @pytest.fixture(scope='class')
    def script(self):
        return (ROOT / 'owl_update.sh').read_text()

    def test_status_only_written_after_the_lock(self, script):
        body = script[script.index('main() {'):]
        assert body.index('flock -n 9') < body.index('write_status starting')

    def test_journal_written_with_the_rollback_point_and_stash(self, script):
        assert script.count('write_journal') >= 4  # definition + rollback point + stash + pop
        assert 'ROLLBACK_READY=1\n    write_journal' in script

    def test_journal_cleared_on_every_ending(self, script):
        assert script.count('clear_journal\n    write_status complete') == 1
        assert script.count('clear_journal\n        write_status complete') == 1
        assert script.count('clear_journal\n            write_status complete') == 1
        assert 'clear_journal\n    write_status rolled_back' in script

    def test_recover_flag_and_reset_failed(self, script):
        assert '--recover)      RECOVER=1' in script
        assert 'systemctl reset-failed "$SERVICE"' in script

    def test_every_status_write_carries_the_boot_id(self, script):
        write_status = script[script.index('write_status() {'):script.index('log() {')]
        assert 'boot_id' in write_status

    def test_service_unit_runs_the_guard(self):
        text = (ROOT / 'owl_setup.sh').read_text()
        assert 'ExecStartPre=-/usr/bin/python3 -I $SCRIPT_DIR/tools/update_guard.py' in text

    def test_import_check_covers_the_setup_app(self, script):
        assert 'entries+=(controller/setup/setup_app.py)' in script
