"""Setup and update scripts.

Two kinds of test, kept apart:

1. TestNetworkQuestionHelpers RUNS the IP helpers of the setup scripts under
   bash (the functions are cut out of the scripts and sourced; they are pure
   and touch nothing). The larger script flows (first-boot arm/disarm, factory
   GPS, ship gate) are run for real in tests/controllers/setup/test_setup_scripts.py.

2. Everything below "Structure checks" reads script text. Those prove no
   behaviour. Each one guards a specific field failure named next to it (a
   package installed outside the requirements files, a sudoers alias defined
   twice, exec bits on every tracked file) that cannot be exercised without
   a Pi, sudo and a network.

Deleted from this file (they ran no production code): a Python "port" of
_validate_ip defined in the test and tested against itself, CONTROLLER.ini
text written by the test and read back, MQTT topic f-strings compared with
literals, nginx "tests" that were `pass` or `x = '250m'; assert x == '250m'`,
and a class defined twice so three of its tests never ran.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BASH = shutil.which('bash')

# Both scripts carry their own copy of the helpers.
IP_SCRIPTS = ['controller/shared/setup.sh', 'controller/networked/in-cab_controller_setup.sh']


def _functions(script, *names):
    """The named top-level shell functions, cut out of a script."""
    text = (REPO / script).read_text(encoding='utf-8')
    out = []
    for name in names:
        match = re.search(rf'^{re.escape(name)}\(\) \{{.*?^\}}$', text, re.MULTILINE | re.DOTALL)
        assert match, f'{name}() not found in {script}'
        out.append(match.group(0))
    return '\n'.join(out) + '\n'


def _bash(script_text, *args):
    """Run a snippet under bash with an empty environment. Nothing run here
    writes a file or calls a system command."""
    return subprocess.run([BASH, '-c', script_text, 'bash', *args], capture_output=True,
                          text=True, timeout=20, env={'PATH': os.path.dirname(BASH)})


@pytest.mark.unit
@pytest.mark.skipif(not BASH, reason='bash not available')
@pytest.mark.parametrize('script', IP_SCRIPTS)
class TestNetworkQuestionHelpers:
    """The network questions loop until _validate_ip accepts the answer, so
    whatever it accepts becomes the unit's static IP and the broker address
    in CONTROLLER.ini."""

    VALID = ['192.168.1.11', '10.42.0.1', '0.0.0.0', '255.255.255.255', '1.2.3.4']
    INVALID = ['', '192.168.1', '192.168.1.1.1', '256.1.1.1', '192.168.1.999', '1234.1.1.1',
               'abc.def.ghi.jkl', '-1.2.3.4', '192.168.1.1/24', ' 192.168.1.1', '192.168.1.1 ',
               '192.168.1.x', '192.168..1', '192,168,1,1', '192.168.1.1;reboot', 'localhost']

    LOOP = ('for ip in "$@"; do\n'
            '  if _validate_ip "$ip"; then echo "ok|$ip"; else echo "no|$ip"; fi\n'
            'done\n')

    def _verdicts(self, script, addresses):
        run = _bash(_functions(script, '_validate_ip') + self.LOOP, *addresses)
        assert run.returncode == 0, run.stderr
        return run, {line[3:]: line[:2] == 'ok' for line in run.stdout.splitlines()}

    def test_accepts_dotted_quads_in_range(self, script):
        run, verdicts = self._verdicts(script, self.VALID)
        assert verdicts == {ip: True for ip in self.VALID}
        assert run.stderr == ''

    def test_refuses_everything_else(self, script):
        run, verdicts = self._verdicts(script, self.INVALID)
        assert verdicts == {ip: False for ip in self.INVALID}
        assert run.stderr == ''

    def test_a_leading_zero_octet_is_checked_without_a_shell_error(self, script):
        run, verdicts = self._verdicts(script, ['192.168.1.08', '192.168.1.099'])
        assert verdicts == {'192.168.1.08': True, '192.168.1.099': True}
        assert run.stderr == ''

    def test_subnet_base_and_host_octet(self, script):
        """The questions compare the /24 base of the OWL, gateway and
        controller addresses and refuse a host octet of 0 or 255."""
        run = _bash(_functions(script, '_ip_base', '_last_octet')
                    + 'for ip in "$@"; do echo "$(_ip_base "$ip")|$(_last_octet "$ip")"; done\n',
                    '192.168.1.11', '10.42.0.1', '192.168.2.255', '172.16.0.0')
        assert run.returncode == 0, run.stderr
        assert run.stdout.splitlines() == ['192.168.1|11', '10.42.0|1', '192.168.2|255', '172.16.0|0']


# ===========================================================================
# Structure checks: script and requirements TEXT. No behaviour is proven.
# ===========================================================================

# ---------------------------------------------------------------------------
# Dependency single-source-of-truth — setup scripts must install from
# requirements files, never inline package lists (a package baked into a
# setup script is invisible to owl_update.sh and breaks remote updates)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRequirementsSingleSource:

    PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')

    def _pip_install_lines(self, script_relpath):
        path = os.path.join(self.PROJECT_ROOT, script_relpath)
        with open(path, encoding='utf-8') as f:
            return [line for line in f
                    if 'pip install' in line and not line.strip().startswith('#')]

    @pytest.mark.parametrize('script', [
        'owl_setup.sh',
        'controller/networked/in-cab_controller_setup.sh',
    ])
    def test_setup_scripts_install_from_requirements_files(self, script):
        offenders = []
        for line in self._pip_install_lines(script):
            # Allowed: pip install -r <file>, pip install --upgrade pip,
            # version-pinned numpy workaround, opencv (installed separately
            # by design — see requirements.txt header)
            if re.search(r'pip install\s+(--no-input\s+)?(--upgrade pip|-r\s|"numpy|opencv)', line):
                continue
            offenders.append(line.strip())
        assert not offenders, (
            f"{script} installs packages outside requirements files "
            f"(invisible to owl_update.sh): {offenders}"
        )

    def test_controller_requirements_file_exists(self):
        path = os.path.join(self.PROJECT_ROOT, 'requirements-controller.txt')
        assert os.path.exists(path), "requirements-controller.txt missing"
        with open(path, encoding='utf-8') as f:
            content = f.read()
        # The controller stack and the agent dep must be pinned here
        for pkg in ('flask', 'gunicorn', 'paho-mqtt', 'httpx'):
            assert pkg in content, f"requirements-controller.txt missing {pkg}"

    def test_owl_requirements_include_runtime_deps(self):
        """paho-mqtt etc. were previously only in owl_setup.sh — they must
        stay in requirements.txt so remote updates can install them."""
        path = os.path.join(self.PROJECT_ROOT, 'requirements.txt')
        with open(path, encoding='utf-8') as f:
            content = f.read()
        for pkg in ('flask', 'gunicorn', 'paho-mqtt', 'psutil', 'boto3', 'pyserial'):
            assert pkg in content, f"requirements.txt missing {pkg}"


# ---------------------------------------------------------------------------
# Software-update sudoers rule: the dashboard's /api/settings/update/start
# launches owl_update.sh via systemd-run; the argv is built by
# utils/software_update.update_argv and must match the rule literally.
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestUpdateSudoersRule:

    PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')

    def _read(self, relpath):
        with open(os.path.join(self.PROJECT_ROOT, relpath), encoding='utf-8') as f:
            return f.read()

    def test_shared_setup_installs_the_update_rule(self):
        content = self._read('controller/shared/setup.sh')
        assert 'setup_update_permissions()' in content
        assert '/etc/sudoers.d/96-owl-update' in content
        assert re.search(r'setup_device_settings_permissions\s*\nsetup_update_permissions',
                         content), 'setup_update_permissions is not called'
        # Validated before install (a bad sudoers file locks sudo out)
        assert 'visudo -cf' in content

    def test_rule_matches_the_shared_argv(self):
        """Literal up to the script, then a single glob for its own args,
        the same shape owl_cloud_provision.sh installs."""
        from utils import software_update as swu
        content = self._read('controller/shared/setup.sh')
        match = re.search(r'Cmnd_Alias OWL_UPDATE_RUN = (.+)', content)
        assert match, 'OWL_UPDATE_RUN alias missing'
        rule = match.group(1)
        expected_tail = ('--unit=%s --collect --property=RuntimeMaxSec=%d '
                         '--uid=${CURRENT_USER} --gid=${CURRENT_USER} '
                         '/bin/bash ${REPO_DIR}/owl_update.sh *'
                         % (swu.UNIT_NAME, swu.RUNTIME_MAX_S))
        assert rule.endswith(expected_tail), rule
        cloud = self._read('owl_cloud_provision.sh')
        assert expected_tail in cloud, 'cloud provisioning rule drifted'

    def test_dashboard_restart_is_permitted(self):
        content = self._read('controller/shared/setup.sh')
        assert 'restart owl-dash.service' in content


@pytest.mark.unit
def test_sudoers_alias_names_are_unique_across_scripts():
    """sudo reads every /etc/sudoers.d file into one policy, so an alias
    defined by two scripts is a duplicate (owl-1, 2026-10-11: 98-owl-cloud
    redefined 96-owl-update's OWL_UPDATE_RUN, logged on every sudo call and
    failed `visudo -c`). Each script checks only its own file with visudo -cf."""
    root = os.path.join(os.path.dirname(__file__), '..', '..')
    seen = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ('.git', 'readthedocs', 'node_modules', '.venv')]
        for name in filenames:
            if not name.endswith('.sh'):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding='utf-8', errors='replace') as f:
                for alias in re.findall(r'^\s*Cmnd_Alias\s+(\w+)\s*=', f.read(), re.MULTILINE):
                    rel = os.path.relpath(path, root)
                    assert alias not in seen, f'{alias} defined in {seen[alias]} and {rel}'
                    seen[alias] = rel
    assert 'OWL_UPDATE_RUN' in seen and 'OWL_CLOUD_UPDATE_RUN' in seen

# ---------------------------------------------------------------------------
# Reused Pis: a leftover first-boot flag locks detection off and restarts the
# setup hotspot on every boot. Hand setups offer to disarm it; factory runs,
# which arm it on purpose, must never touch it.
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLeftoverFirstbootFlag:

    PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
    FLAG = '/boot/firmware/owl-firstboot.flag'

    def _read(self, relpath):
        with open(os.path.join(self.PROJECT_ROOT, relpath), encoding='utf-8') as f:
            return f.read().replace('\r\n', '\n')

    def _check_block(self, content):
        start = content.index('FIRSTBOOT_FLAG="')
        return content[start:content.index('\nfi\n', start)]

    def test_owl_setup_offers_disarm_outside_factory(self):
        content = self._read('owl_setup.sh')
        assert f'FIRSTBOOT_FLAG="{self.FLAG}"' in content
        block = self._check_block(content)
        assert '"$FACTORY_MODE" != "1"' in block
        assert 'install_firstboot.sh" --disarm' in block
        # Default is disarm: only an explicit n keeps it armed
        assert '[Y/n]' in block and '^[Nn]$' in block

    def test_owl_setup_checks_before_the_install(self):
        content = self._read('owl_setup.sh')
        assert content.index('FIRSTBOOT_FLAG="') < content.index('controller/shared/setup.sh')
        # After the detached factory re-exec, so the background copy never asks
        assert content.index('OWL_SETUP_DETACHED=1 setsid') < content.index('FIRSTBOOT_FLAG="')

    def test_shared_setup_offers_disarm_for_hand_setups_only(self):
        content = self._read('controller/shared/setup.sh')
        assert f'FIRSTBOOT_FLAG="{self.FLAG}"' in content
        block = self._check_block(content)
        assert '"$NONINTERACTIVE" != "1"' in block
        assert '"$ARM_FIRSTBOOT" != "1"' in block
        assert '../setup/install_firstboot.sh" --disarm' in block
        assert '[Y/n]' in block and '^[Nn]$' in block
        # Asked before the network questions, so the answer can't be lost
        assert content.index('FIRSTBOOT_FLAG="') < content.index('\ncollect_user_input\n')

    def test_flag_path_matches_install_firstboot(self):
        content = self._read('controller/setup/install_firstboot.sh')
        assert f'FLAG_PATH="{self.FLAG}"' in content


# ---------------------------------------------------------------------------
# Install AI on a 2 GB / 16 GB CM5: the PyPI aarch64 torch wheel drags in the
# NVIDIA CUDA stack (~6 GB) and pip unpacks into the 1 GB tmpfs /tmp
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAiInstallCpuTorch:

    PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')

    def _read(self, relpath):
        with open(os.path.join(self.PROJECT_ROOT, relpath), encoding='utf-8') as f:
            return f.read()

    def test_gog_requirements_pin_cpu_torch(self):
        """The extra index is only safe because every torch pin carries +cpu:
        `==2.14.1+cpu` cannot match the PyPI (CUDA) 2.14.1 build."""
        lines = [l.strip() for l in self._read('requirements-gog.txt').splitlines()
                 if l.strip() and not l.strip().startswith('#')]
        assert '--extra-index-url https://download.pytorch.org/whl/cpu' in lines
        torch_pins = [l for l in lines if l.startswith(('torch', 'torchvision'))]
        assert len(torch_pins) == 2
        assert all(re.fullmatch(r'torch(vision)?==[\d.]+\+cpu', p) for p in torch_pins), torch_pins
        assert lines.index(torch_pins[0]) < lines.index('ultralytics>=8.4,<8.5')

    def test_every_updater_pip_install_goes_through_pip_install(self):
        """pip_install sets TMPDIR on disk and --no-cache-dir; a bare
        `$VENV/bin/pip install` would unpack into the tmpfs /tmp again."""
        script = self._read('owl_update.sh')
        bare = [l.strip() for l in script.splitlines()
                if '/bin/pip" install' in l and 'pip_install()' not in l
                and not l.strip().startswith('#')]
        assert bare == ['TMPDIR="$PIP_TMP" "$VENV/bin/pip" install --no-input --no-cache-dir "$@"']

    def test_ai_install_checks_for_cpu_torch(self):
        script = self._read('owl_update.sh')
        body = script[script.index('install_ai_deps() {'):]
        body = body[:body.index('\n}\n')]
        assert '-c "$constraints" -r "$REPO_DIR/requirements-gog.txt"' in body
        assert 'torch.version.cuda is None' in body

    def test_both_ai_branches_use_install_ai_deps(self):
        script = self._read('owl_update.sh')
        deps = script[script.index('# --- Dependencies'):script.index('# --- Restart')]
        assert deps.count('install_ai_deps') == 2


@pytest.mark.unit
class TestCleanKeepsGitModes:
    """dev/clean.sh runs at the end of every factory install. A blanket
    `chmod -R 755` on the repo set the exec bit on every tracked file, so
    git showed ~400 files modified on a fresh unit and owl_update.sh stashed
    them all on every update (owl-1, 2026-10-18)."""

    def test_repo_chmod_does_not_add_exec_bits(self):
        path = os.path.join(os.path.dirname(__file__), '..', '..', 'dev', 'clean.sh')
        with open(path, encoding='utf-8') as f:
            lines = [l.strip() for l in f if l.strip().startswith('chmod -R')]
        repo = [l for l in lines if '/home/owl/owl' in l]
        assert repo, 'clean.sh no longer resets repo permissions'
        for line in repo:
            mode = line.split()[2]
            assert not re.fullmatch(r'[0-7]{3,4}', mode), f'numeric mode sets exec on files: {line}'
            assert 'X' in mode and 'x' not in mode, line
