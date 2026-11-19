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


def test_the_null_device_is_writable():
    # pytest itself logs to it; on Windows it is \\.\nul, not a path under /dev
    with open(os.devnull, 'w') as f:
        f.write('discarded')


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


# --------------------------------------------------------------------------
# No test touches a GPIO pin, on any board
# --------------------------------------------------------------------------

class TestNoRealGpio:
    """On a Raspberry Pi the OWL uses the real pins. Run there, the suite
    drove the real relay board, and the first Owl kept BOARD7 so every later
    one failed with GPIOPinInUse (104 of 105 on a CM5, 2026-11-19).
    tests/conftest.py sets OWL_FAKE_GPIO=1 before anything is imported."""

    def test_the_suite_runs_with_fake_gpio_forced_on(self):
        assert os.environ['OWL_FAKE_GPIO'] == '1'

    def test_relays_leds_and_switches_are_stand_ins_in_this_process(self):
        import utils.input_manager as input_manager
        import utils.output_manager as output_manager
        assert output_manager.testing is True
        assert input_manager.testing is True

    def test_a_relay_board_built_in_a_test_holds_no_real_pin(self):
        from utils.output_manager import RelayControl, TestRelay
        board = RelayControl({0: 13, 1: 15})
        assert board.testing is True
        assert all(isinstance(relay, TestRelay) for relay in board.relay_dict.values())
        # and a second one can be built: nothing was reserved
        RelayControl({0: 13, 1: 15})

    @pytest.mark.parametrize('platform_name', ['Linux-6.18.60+rpt-rpi-2712-aarch64-with-glibc2.41',
                                               'linux-6.6-rpi-v8-aarch64'])
    def test_on_a_raspberry_pi_the_flag_still_means_no_pins(self, monkeypatch, platform_name):
        """The decision itself, as if on the Pi: gpiozero is never imported."""
        import platform
        import sys

        import utils.input_manager as input_manager
        import utils.output_manager as output_manager
        monkeypatch.setattr(platform, 'platform', lambda *a, **k: platform_name)
        monkeypatch.setitem(sys.modules, 'gpiozero', None)      # importing it would raise
        monkeypatch.setitem(sys.modules, 'lgpio', None)
        assert output_manager.get_platform_config() == (True, None)
        assert input_manager.is_raspberry_pi() is False

    def test_without_the_flag_a_raspberry_pi_uses_its_pins(self, monkeypatch):
        """A unit in the field sets nothing and must get real GPIO."""
        import platform

        import utils.input_manager as input_manager
        monkeypatch.delenv('OWL_FAKE_GPIO')
        monkeypatch.setattr(platform, 'platform', lambda *a, **k: 'linux-6.6-rpi-v8-aarch64')
        assert input_manager.is_raspberry_pi() is True


# --------------------------------------------------------------------------
# One way to find the OWL's own folders
# --------------------------------------------------------------------------

class TestOneWayToFindOwlFolders:
    """config/, logs/, models/, downloads/, uploads/ and the assistant's
    folders are found through utils/paths.home_dir, nowhere else. In November
    2026 half the code used home_dir and half still built the path from its
    own file location, so the same folder was reached two ways (the
    active-config pointer was written one way and read the other).

    Code, templates, static files and scripts are found relative to the code:
    that is not this rule."""

    HOME_FOLDERS = ('config', 'logs', 'models', 'downloads', 'uploads', 'lut_profiles',
                    'sessions', 'widgets', 'active_config', 'controller.ini', 'geometry.ini',
                    'fleet.json', '.update_status', '.update_journal', '.update_backup')

    def production_files(self):
        roots = [REPO_ROOT / 'owl.py', REPO_ROOT / 'version.py']
        for folder in ('utils', 'controller', 'agent', 'tools', 'desktop'):
            roots += sorted((REPO_ROOT / folder).rglob('*.py'))
        return [p for p in roots if p.is_file() and p.name != 'paths.py']

    def test_no_production_code_builds_one_from_its_own_location(self):
        import re
        own_location = re.compile(r'__file__')
        offenders = []
        for path in self.production_files():
            lines = path.read_text(encoding='utf-8').splitlines()
            for number, line in enumerate(lines):
                code = line.split('#', 1)[0]
                if not own_location.search(code):
                    continue
                # the whole statement: on to the following lines only while
                # a bracket is still open
                expression, depth = '', 0
                window = lines[number:number + 6]
                for offset, following in enumerate(window):
                    part = following.split('#', 1)[0]
                    expression += ' ' + part.lower()
                    depth += sum(part.count(c) for c in '([{') - sum(part.count(c) for c in ')]}')
                    carries_on = (offset + 1 < len(window)
                                  and window[offset + 1].strip().startswith(('/', '+', '.')))
                    if depth <= 0 and not carries_on:
                        break
                named = [f for f in self.HOME_FOLDERS
                         if re.search(rf"['\"/]{re.escape(f)}", expression)]
                if named:
                    offenders.append(f'{path.relative_to(REPO_ROOT)}:{number + 1} ({", ".join(named)}): '
                                     f'{line.strip()}')
        assert not offenders, ('use utils.paths.home_dir(...) for these folders:\n  '
                               + '\n  '.join(offenders))

    def test_the_check_would_catch_the_old_forms(self, tmp_path, monkeypatch):
        """The scan above, pointed at the three shapes that were in the code."""
        bad = tmp_path / 'utils'
        bad.mkdir()
        (bad / 'a.py').write_text(
            "from pathlib import Path\nimport os\n"
            "A = Path(__file__).resolve().parents[1] / 'config' / 'CONTROLLER.ini'\n"
            "B = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'models')\n"
            "C = Path(controller_ini or\n         Path(__file__).resolve().parents[2]\n"
            "         / 'config' / 'CONTROLLER.ini')\n"
            "STATIC = os.path.join(os.path.dirname(__file__), 'static')   # code: allowed\n")
        monkeypatch.setattr(self, 'production_files', lambda: [bad / 'a.py'])
        import utils.paths as paths
        monkeypatch.setattr(paths, 'REPO_ROOT', tmp_path)
        globals_ = self.test_no_production_code_builds_one_from_its_own_location.__globals__
        monkeypatch.setitem(globals_, 'REPO_ROOT', tmp_path)
        with pytest.raises(AssertionError) as caught:
            self.test_no_production_code_builds_one_from_its_own_location()
        report = str(caught.value)
        assert 'a.py:3' in report and 'a.py:4' in report and 'a.py:6' in report
        assert 'a.py:8' not in report

    def test_every_owl_folder_follows_owl_home(self, tmp_path, monkeypatch):
        """Set OWL_HOME and each of them moves with it, on every call."""
        monkeypatch.setenv('OWL_HOME', str(tmp_path))
        for parts in (('config',), ('config', 'CONTROLLER.ini'), ('config', 'active_config.txt'),
                      ('logs',), ('models',), ('downloads',), ('uploads',), ('agent', 'widgets')):
            assert home_dir(*parts) == tmp_path.joinpath(*parts)

    def test_the_owl_reads_its_config_and_pointer_from_the_same_home(self, tmp_path, monkeypatch):
        """The pointer the dashboards write and the file owl.py starts from."""
        import inspect

        import owl as owl_module
        source = inspect.getsource(owl_module)
        assert "ACTIVE_CONFIG_POINTER = home_dir('config', 'active_config.txt')" in source
        assert "self._config_path = home_dir() / config_file" in source
