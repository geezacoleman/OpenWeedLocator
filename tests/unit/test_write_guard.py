"""The suite's own safety net: a test cannot change a real file.

tests/_write_guard.py refuses the write and tests/conftest.py fails the test
that asked for it. These check the net itself, so each one clears the
recorded refusal it caused on purpose.
"""

import os
import shutil
from pathlib import Path

import pytest

import _write_guard
from utils.paths import REPO_ROOT, home_dir, owl_home


@pytest.fixture
def refused():
    """Run a callable that must be refused; nothing may have been written."""
    def _refused(action, target):
        existed = os.path.lexists(target)
        with pytest.raises(_write_guard.RealWriteError):
            action()
        assert _write_guard.violations, 'the refusal was not recorded'
        _write_guard.violations.clear()
        assert os.path.lexists(target) == existed
    return _refused


REAL_TARGETS = [
    REPO_ROOT / 'config' / 'active_config.txt',
    REPO_ROOT / 'config' / 'GENERAL_CONFIG.ini',
    REPO_ROOT / 'logs' / 'owl.jsonl',
    REPO_ROOT / 'models' / 'new_model.pt',
    REPO_ROOT / 'owl.py',
    Path('/etc/hosts'),
    Path('/etc/avahi/services/owl-setup.service'),
    Path.home() / 'owl_test_should_never_exist.txt',
]


@pytest.mark.parametrize('target', REAL_TARGETS, ids=lambda p: str(p))
@pytest.mark.parametrize('mode', ['w', 'a', 'r+', 'wb', 'x'])
def test_opening_a_real_file_for_writing_is_refused(refused, target, mode):
    before = target.read_bytes() if target.is_file() else None
    refused(lambda: open(target, mode), target)
    assert (target.read_bytes() if target.is_file() else None) == before


def test_path_write_text_is_refused(refused):
    target = REPO_ROOT / 'config' / 'active_config.txt'
    refused(lambda: target.write_text('config/config_autosave.ini'), target)


@pytest.mark.parametrize('action', [
    lambda t: os.remove(t), lambda t: os.rename(t, str(t) + '.bak'),
    lambda t: os.replace(t, str(t) + '.bak'), lambda t: os.truncate(t, 0),
    lambda t: os.chmod(t, 0o600), lambda t: os.utime(t),
    lambda t: shutil.rmtree(t.parent),
], ids=['remove', 'rename', 'replace', 'truncate', 'chmod', 'utime', 'rmtree'])
def test_changing_an_existing_real_file_is_refused(refused, action):
    target = REPO_ROOT / 'config' / 'GENERAL_CONFIG.ini'
    before = target.read_bytes()
    refused(lambda: action(target), target)
    assert target.read_bytes() == before


def test_renaming_a_temp_file_onto_a_real_one_is_refused(refused, tmp_path):
    """atomic_write_config's last step: write a temp file, rename it over."""
    staged = tmp_path / 'staged.ini'
    staged.write_text('[System]\nrelay_num = 4\n')
    target = REPO_ROOT / 'config' / 'config_autosave.ini'
    refused(lambda: os.replace(staged, target), target)


@pytest.mark.parametrize('folder', ['downloads', 'agent/sessions', 'tests/tracking_output', 'new_folder'])
def test_creating_a_folder_in_the_checkout_is_refused(refused, folder):
    target = REPO_ROOT / folder
    if target.exists():
        pytest.skip(f'{folder} already exists in this checkout')
    refused(lambda: os.makedirs(target), target)


def test_reading_real_files_is_allowed():
    assert '[System]' in (REPO_ROOT / 'config' / 'GENERAL_CONFIG.ini').read_text()
    assert not _write_guard.violations


def test_tmp_path_is_writable(tmp_path):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'a' / 'b.txt').write_text('x')
    os.replace(tmp_path / 'a' / 'b.txt', tmp_path / 'c.txt')
    shutil.rmtree(tmp_path / 'a')
    assert not _write_guard.violations


def test_the_owl_home_is_a_temp_directory_seeded_with_shipped_config():
    """Every folder the OWL writes at run time is outside the checkout."""
    home = owl_home()
    assert REPO_ROOT not in home.resolve().parents and home.resolve() != REPO_ROOT
    assert (home / 'config' / 'GENERAL_CONFIG.ini').read_bytes() == \
        (REPO_ROOT / 'config' / 'GENERAL_CONFIG.ini').read_bytes()
    # a developer's own files never leak into the tests
    assert not (home / 'config' / 'CONTROLLER.ini').exists()
    for parts in (('config',), ('logs',), ('models',), ('downloads',), ('uploads',),
                  ('agent', 'widgets'), ('agent', 'sessions')):
        assert str(home_dir(*parts)).startswith(str(home))


def test_production_modules_write_under_the_owl_home():
    import controller.networked.networked as networked
    from controller.setup import firstboot_state
    from utils import network_manager
    home = str(owl_home())
    assert str(networked.DOWNLOADS_DIR).startswith(home)
    assert str(networked.UPLOADS_DIR).startswith(home)
    for path in (network_manager.ETC_HOSTS, firstboot_state.AVAHI_SERVICE_FILE):
        assert not path.startswith('/etc/'), path


def test_owl_home_defaults_to_the_checkout(monkeypatch):
    """On a unit nothing sets OWL_HOME: files stay where they always were."""
    monkeypatch.delenv('OWL_HOME')
    assert owl_home() == REPO_ROOT
    assert home_dir('config', 'CONTROLLER.ini') == REPO_ROOT / 'config' / 'CONTROLLER.ini'


def test_a_refusal_swallowed_by_the_code_under_test_is_still_on_record():
    """Production code wraps writes in `except OSError`. The refusal must
    still be on record for conftest's _no_real_writes to fail the test."""
    try:
        open(REPO_ROOT / 'config' / 'active_config.txt', 'w')
    except OSError:
        pass                      # what set_hostname, _persist... do
    assert len(_write_guard.violations) == 1
    assert 'active_config.txt' in _write_guard.violations[0]
    _write_guard.violations.clear()
