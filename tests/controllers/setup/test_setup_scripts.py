"""
The shell scripts that provision and arm an OWL, run for real under bash.

    controller/setup/install_firstboot.sh   --arm / --disarm / --camera-config / --gps-config
    owl_setup.sh                            --factory confirmation and detach, Wi-Fi country,
                                            nRF9151 GPS step, ship gate, maker identity
    dev/clean.sh                            --yes, Wi-Fi profile cleanup, service stop/start

These used to be ~40 checks that a string occurs in the script text (in
test_setup_app_routes.py). Here the script, or the named function or block
cut out of it, is executed in a sandbox and the test asserts on what it did:
the files it wrote and the commands it ran.

The sandbox (Box):
- PATH holds ONLY the stub commands a test defines plus symlinks to a fixed
  list of harmless tools (grep, sed, awk, ...). No real sudo, systemctl,
  nmcli, raspi-config or ufw can be reached, on any machine.
- every stub appends "name args" to a call log, which is what the tests read.
- install_firstboot.sh is run whole, with its absolute paths re-rooted under
  tmp_path. Any absolute path the re-rooting does not know about fails the
  test before the script is run (test_every_absolute_path_is_accounted_for),
  so a new path in the script cannot write to the machine running the tests.

TestScriptStructure at the bottom is what is left of the text checks: step
ORDER inside the 1100-line owl_setup.sh, which cannot be run in pieces. It
is labelled as such and proves no behaviour.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
BASH = shutil.which('bash')

pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(not BASH, reason='bash not available')]

# Harmless tools the scripts may use. Nothing here changes system state
# outside the paths it is given.
TOOLS = ['bash', 'sh', 'cat', 'grep', 'sed', 'awk', 'cut', 'tr', 'tail', 'head', 'rm',
         'mkdir', 'chmod', 'mktemp', 'mv', 'cp', 'seq', 'dirname', 'basename', 'touch',
         'sha256sum', 'env']

PRELUDE = "RED=''; GREEN=''; ORANGE=''; YELLOW=''; NC=''; TICK='[OK]'; CROSS='[FAIL]'\n"


class Box:
    """A directory of stub commands, a call log, and a runner."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.bin = tmp_path / 'bin'
        self.bin.mkdir()
        self.stub_dir = tmp_path / 'stubs'       # ahead of the tools on PATH
        self.stub_dir.mkdir()
        self.path = f'{self.stub_dir}:{self.bin}'
        self.log = tmp_path / 'calls.log'
        self.log.write_text('')
        for tool in TOOLS:
            found = shutil.which(tool)
            assert found, f'{tool} is needed to run the scripts'
            (self.bin / tool).symlink_to(found)

    def stub(self, name, body='exit 0'):
        """A command on PATH: logs its call, then runs `body`."""
        path = self.stub_dir / name      # its own directory: never through a tool symlink
        path.write_text(f'#!{BASH}\n'
                        'args="$*"\n'
                        f'printf \'%s\\n\' "{name} ${{args//$\'\\n\'/ }}" >> "$STUB_LOG"\n'
                        f'{body}\n', newline='\n')
        path.chmod(0o755)

    def stubs(self, *names):
        for name in names:
            self.stub(name)

    def run(self, script, args=(), env=None, stdin=''):
        """Run a script file, or script text, under bash."""
        if not isinstance(script, Path):
            path = self.tmp / 'probe.sh'
            path.write_text(script, newline='\n')
            script = path
        full_env = {'PATH': self.path, 'STUB_LOG': str(self.log),
                    'TMPDIR': str(self.tmp), 'HOME': str(self.tmp)}
        full_env.update({k: str(v) for k, v in (env or {}).items()})
        return subprocess.run([BASH, str(script), *args], input=stdin, capture_output=True,
                              text=True, env=full_env, timeout=60, cwd=str(self.tmp))

    def calls(self, prefix=''):
        return [line for line in self.log.read_text().splitlines() if line.startswith(prefix)]


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def source(relative):
    return (REPO / relative).read_text(encoding='utf-8')


def between(text, start, end):
    """The script text from `start` up to `end`. A marker that has gone
    fails the test (ValueError) rather than quietly testing nothing."""
    a = text.index(start)
    return text[a:text.index(end, a)]


def function(text, name):
    return f'{name}() {{' + text.split(f'{name}() {{', 1)[1].split('\n}\n', 1)[0] + '\n}\n'


# ===========================================================================
# controller/setup/install_firstboot.sh, run whole in a re-rooted sandbox
# ===========================================================================

# Absolute paths the script touches, re-rooted under tmp_path
REROOTED = ['/boot/firmware/', '/etc/avahi/', '/etc/systemd/', '/etc/sudoers.d/',
            '/usr/local/sbin/', '/usr/local/lib/owl', '/var/lib/owl',
            '/proc/device-tree/model', '/proc/cpuinfo', '/etc/machine-id']
# Absolute paths left alone: never written by the script itself
HARMLESS = {
    '/dev/null',
    '/dev/ttyAMA3',                                   # only tested with -e
    '/usr/bin/python3',                               # text of the fallback unit
    '/home/${CURRENT_USER}/.virtualenvs/owl/bin',     # text of the unit's ExecStart
    '/run/owl-cloud-pw.XXXXXX', '/bin/bash',          # text of the generated cloud helper
}
ABSOLUTE = re.compile(r'(?<![\w@${}.\-/])/(?:boot|etc|usr|var|proc|dev|run|home|tmp|opt|root|sys|'
                      r'bin|sbin|lib|mnt|media|srv)\b(?:/[\w.\-${}]*)*')

CM5 = 'Raspberry Pi Compute Module 5 Rev 1.0'
STOCK_CONFIG = ('# For more options see http://rptl.io/configtxt\n'
                'dtparam=audio=on\n'
                'camera_auto_detect=1\n'
                'display_auto_detect=1\n'
                '\n[cm5]\ndtoverlay=dwc2,dr_mode=host\n'
                '\n[all]\n')


def rerooted_firstboot_script():
    """install_firstboot.sh with @ROOT@ in front of every path it touches,
    and the list of absolute paths that are still not re-rooted."""
    text = source('controller/setup/install_firstboot.sh')
    assert text.count('if [[ $EUID -ne 0 ]]; then') == 1
    text = text.replace('if [[ $EUID -ne 0 ]]; then', 'if false; then')
    for path in REROOTED:
        assert path in text, f'{path} is no longer in install_firstboot.sh'
        text = re.sub(r'(?<![\w@])' + re.escape(path), '@ROOT@' + path, text)
    stray = set()
    for line in text.splitlines():
        if line.lstrip().startswith('#'):
            continue
        stray.update(m.rstrip('.') for m in ABSOLUTE.findall(line))
    return text, stray - HARMLESS


class Firstboot:
    """A fake OWL filesystem and the stubbed system around the script."""

    def __init__(self, box, model=CM5, config=STOCK_CONFIG, hotspot='OWL-1',
                 serial='10000000abcd12ef'):
        self.box = box
        self.root = box.tmp / 'root'
        self.owl = self.root / 'home' / 'owl' / 'owl'
        for d in ['boot/firmware', 'etc/avahi/services', 'etc/systemd/system', 'etc/sudoers.d',
                  'usr/local/sbin', 'usr/local/lib', 'proc/device-tree', 'var/lib']:
            (self.root / d).mkdir(parents=True)
        (self.owl / 'controller' / 'setup').mkdir(parents=True)
        (self.owl / 'owl_cloud_provision.sh').write_text('#!/bin/bash\n')

        text, stray = rerooted_firstboot_script()
        assert not stray, f'install_firstboot.sh uses paths the sandbox does not re-root: {stray}'
        self.script = self.owl / 'controller' / 'setup' / 'install_firstboot.sh'
        self.script.write_text(text.replace('@ROOT@', str(self.root)), newline='\n')

        self.config = self.root / 'boot/firmware/config.txt'
        if config is not None:
            self.config.write_text(config)
        if model is not None:
            (self.root / 'proc/device-tree/model').write_bytes(model.encode() + b'\0')
        (self.root / 'proc/cpuinfo').write_text(f'processor\t: 0\nSerial\t\t: {serial}\n')
        (self.root / 'etc/machine-id').write_text('0123456789abcdef0123456789abcdef\n')

        self.flag = self.root / 'boot/firmware/owl-firstboot.flag'
        self.unit = self.root / 'etc/systemd/system/owl-firstboot.service'
        self.fallback_unit = self.root / 'etc/systemd/system/owl-firstboot-fallback.service'
        self.avahi = self.root / 'etc/avahi/services/owl-setup.service'
        self.sudoers = self.root / 'etc/sudoers.d/96-owl-firstboot'
        self.rearm = self.root / 'usr/local/sbin/owl-firstboot-arm'
        self.cloud_helper = self.root / 'usr/local/sbin/owl-cloud-provision'
        self.state_json = self.root / 'var/lib/owl/firstboot_state.json'
        self.env = {'HOTSPOT': hotspot}

        box.stub('nmcli', '''case "$*" in
  "-t -f NAME,TYPE con show") [ -n "$HOTSPOT" ] && echo "$HOTSPOT:802-11-wireless"
                               echo "Wired connection 1:802-3-ethernet" ;;
  "-t -f 802-11-wireless.mode con show "*) echo "802-11-wireless.mode:ap" ;;
  "-t -f 802-11-wireless.ssid con show "*) echo "802-11-wireless.ssid:$HOTSPOT" ;;
  "con modify "*) exit ${NMCLI_MODIFY_RC:-0} ;;
esac''')
        box.stub('systemctl', '[ "$1" = enable ] && exit ${ENABLE_RC:-0}; exit 0')
        box.stub('visudo', 'exit ${VISUDO_RC:-0}')
        # install(1) without the chown to root, which needs root
        box.stub('install', '''files=(); dir=0; mode=644
while [ $# -gt 0 ]; do
  case "$1" in -o|-g) shift ;; -d) dir=1 ;; -m) mode="$2"; shift ;; *) files+=("$1") ;; esac
  shift
done
if [ "$dir" = 1 ]; then mkdir -p "${files[0]}" && chmod "$mode" "${files[0]}"
else cp "${files[0]}" "${files[1]}" && chmod "$mode" "${files[1]}"; fi''')
        box.stub('stat', 'echo owl')            # the repo owner
        box.stubs('chown', 'sync', 'ufw', 'dtoverlay', 'pinctrl', 'logname')

    def run(self, mode, **env):
        return self.box.run(self.script, [mode], env={**self.env, **env})

    def config_lines(self):
        return self.config.read_text().splitlines()


@pytest.fixture
def firstboot(box):
    def _build(**kwargs):
        return Firstboot(box, **kwargs)
    return _build


def nothing_armed(fb):
    return not (fb.flag.exists() or fb.unit.exists() or fb.fallback_unit.exists())


class TestFirstbootSandbox:
    def test_every_absolute_path_is_accounted_for(self):
        """The guard the rest of this file relies on: a path added to the
        script that is neither re-rooted nor known harmless must be looked
        at before any test runs the script against it."""
        _, stray = rerooted_firstboot_script()
        assert stray == set()


class TestArm:
    """--arm: what a unit leaving the bench boots into."""

    def test_arm_installs_the_service_and_sets_an_empty_flag(self, firstboot):
        fb = firstboot()
        result = fb.run('--arm')
        assert result.returncode == 0, result.stdout + result.stderr

        # An EMPTY flag is a new setup run (the service writes its id into it)
        assert fb.flag.exists() and fb.flag.read_bytes() == b''

        unit = fb.unit.read_text().splitlines()
        # field bug 2026-07-16: a template+sed step shipped .../bin/bin/python
        assert (f'ExecStart=/home/owl/.virtualenvs/owl/bin/python '
                f'{fb.owl}/controller/setup/setup_app.py') in unit
        assert f'ConditionPathExists={fb.flag}' in unit
        assert 'OnFailure=owl-firstboot-fallback.service' in unit
        assert 'User=root' in unit
        assert f'WorkingDirectory={fb.owl}/controller/setup' in unit
        # systemd ignores an indented directive
        assert [line for line in unit if line != line.lstrip()] == []

        fallback = fb.fallback_unit.read_text().splitlines()
        # system python on purpose: the venv may be what is broken
        assert f'ExecStart=/usr/bin/python3 {fb.owl}/controller/setup/fallback_server.py' in fallback
        assert f'ConditionPathExists={fb.flag}' in fallback
        assert [line for line in fallback if line != line.lstrip()] == []

        calls = fb.box.calls()
        assert 'systemctl enable owl-firstboot.service' in calls
        assert 'nmcli con modify OWL-1 wifi-sec.psk owl-setup' in calls
        assert 'ufw allow 8088/tcp' in calls
        assert '<type>_owl-setup._tcp</type>' in fb.avahi.read_text()
        assert '<port>8088</port>' in fb.avahi.read_text()

    def test_the_flag_is_written_after_everything_else(self, firstboot):
        fb = firstboot()
        # sync is the step straight after the flag write: when it runs, every
        # other file must already be in place
        fb.box.stub('sync', f'''for f in "{fb.unit}" "{fb.fallback_unit}" "{fb.sudoers}" \\
         "{fb.rearm}" "{fb.cloud_helper}" "{fb.avahi}" "{fb.flag}"; do
  [ -e "$f" ] || echo "missing at sync: $f" >> "{fb.box.tmp}/missing.txt"
done''')
        assert fb.run('--arm').returncode == 0
        assert not (fb.box.tmp / 'missing.txt').exists(), (fb.box.tmp / 'missing.txt').read_text()
        assert fb.box.calls('sync') == ['sync ']

    @pytest.mark.parametrize('broken, message', [
        ({'HOTSPOT': ''}, 'No AP-mode connection found'),
        ({'NMCLI_MODIFY_RC': 1}, 'Could not reset the hotspot password. Nothing was armed.'),
        ({'ENABLE_RC': 1}, 'Could not enable owl-firstboot.service. Nothing was armed.'),
        ({'VISUDO_RC': 1}, 'Cloud provisioning helper not installed. Nothing was armed.'),
    ])
    def test_a_failed_step_leaves_the_unit_unarmed(self, firstboot, broken, message):
        """A half-armed unit boots into a setup mode the phone cannot reach."""
        fb = firstboot()
        result = fb.run('--arm', **broken)
        assert result.returncode == 1
        assert message in result.stdout
        assert not fb.flag.exists()

    def test_no_hotspot_changes_nothing_at_all(self, firstboot):
        fb = firstboot()
        result = fb.run('--arm', HOTSPOT='')
        assert result.returncode == 1
        assert nothing_armed(fb)
        assert fb.config.read_text() == STOCK_CONFIG
        assert fb.box.calls('systemctl') == [] and fb.box.calls('nmcli con modify') == []

    def test_a_camera_overlay_conflict_stops_the_arm_before_anything_is_changed(self, firstboot):
        config = STOCK_CONFIG + 'dtoverlay=imx477\n'
        fb = firstboot(config=config)
        result = fb.run('--arm')
        assert result.returncode == 1
        assert 'Camera boot config failed. Nothing was armed.' in result.stdout
        assert nothing_armed(fb)
        assert fb.config.read_text() == config
        assert fb.box.calls('nmcli con modify') == []

    def test_arm_writes_the_camera_overlay(self, firstboot):
        fb = firstboot()
        assert fb.run('--arm').returncode == 0
        assert 'dtoverlay=imx296' in fb.config_lines()

    @pytest.mark.parametrize('hotspot, serial, renamed_to', [
        ('OWL-1', '10000000abcd12ef', 'OWL-12EF'),       # install default: collides
        ('OWL-1', '0000000000000000', 'OWL-CDEF'),       # no serial: machine-id
        ('OWL-12EF', '10000000abcd12ef', None),          # already the serial name
        ('OWL-shed', '10000000abcd12ef', None),          # a custom name is left alone
    ])
    def test_an_install_default_ssid_is_renamed_from_the_serial(self, firstboot, hotspot,
                                                                serial, renamed_to):
        fb = firstboot(hotspot=hotspot, serial=serial)
        assert fb.run('--arm').returncode == 0
        renames = fb.box.calls(f'nmcli con modify {hotspot} 802-11-wireless.ssid')
        expected = [f'nmcli con modify {hotspot} 802-11-wireless.ssid {renamed_to}'] if renamed_to else []
        assert renames == expected

    def test_the_rearm_helper_empties_the_flag(self, firstboot):
        """The dashboard's "set up again" runs this as root. A flag that
        still carried the old run id would resume the previous run."""
        fb = firstboot()
        assert fb.run('--arm').returncode == 0
        assert fb.sudoers.read_text().splitlines()[-1] == f'owl ALL=(root) NOPASSWD: {fb.rearm}'
        assert fb.sudoers.stat().st_mode & 0o777 == 0o440
        assert fb.rearm.stat().st_mode & 0o777 == 0o755

        fb.flag.write_text('run-id-of-the-last-setup')
        result = subprocess.run([shutil.which('sh'), str(fb.rearm)], capture_output=True, text=True,
                                env={'PATH': fb.box.path, 'STUB_LOG': str(fb.box.log)}, timeout=30)
        assert result.returncode == 0, result.stderr
        assert fb.flag.read_bytes() == b''
        assert fb.box.calls('sync')[-1] == 'sync '       # FAT32: must survive a power-off

    def test_unknown_option_is_refused(self, firstboot):
        fb = firstboot()
        result = fb.run('--ship')
        assert result.returncode == 1 and "Unknown option '--ship'" in result.stdout
        assert nothing_armed(fb)


class TestDisarm:
    def test_disarm_removes_everything_arm_installed_and_the_run_state(self, firstboot):
        fb = firstboot()
        assert fb.run('--arm').returncode == 0
        fb.state_json.write_text('{"run": "abc"}')       # the setup service's state
        fb.box.log.write_text('')

        assert fb.run('--disarm').returncode == 0
        for gone in (fb.flag, fb.state_json, fb.unit, fb.fallback_unit, fb.avahi,
                     fb.sudoers, fb.rearm):
            assert not gone.exists(), f'{gone} survived --disarm'
        # the dashboard still uses the cloud helper after setup
        assert fb.cloud_helper.exists()
        calls = fb.box.calls()
        for expected in ('systemctl disable owl-firstboot.service',
                         'systemctl stop owl-firstboot.service',
                         'systemctl stop owl-firstboot-fallback.service',
                         'systemctl daemon-reload', 'ufw delete allow 8088/tcp'):
            assert expected in calls
        # the camera overlay is hardware truth, not setup surface
        assert 'dtoverlay=imx296' in fb.config_lines()

    def test_disarm_on_a_unit_that_was_never_armed_is_clean(self, firstboot):
        fb = firstboot()
        assert fb.run('--disarm').returncode == 0
        assert fb.config.read_text() == STOCK_CONFIG


class TestCameraConfig:
    """--camera-config. A Compute Module has no camera autodetection: without
    the overlay a fresh flash never finds the camera (no error, no I2C bus)."""

    def camera_block(self, fb):
        """The lines the script appended (after its marker)."""
        lines = fb.config_lines()
        marker = next(i for i, line in enumerate(lines) if line.startswith('# OWL camera'))
        return lines[marker - 1], lines[marker + 1:]

    def test_cm_gets_the_overlay_on_both_ports_under_all(self, firstboot):
        from controller.setup.firstboot_state import CAMERA_SENSOR
        fb = firstboot()
        result = fb.run('--camera-config')
        assert result.returncode == 2, result.stdout          # 2 = changed, reboot needed
        header, block = self.camera_block(fb)
        assert header == '[all]'                # under [pi4] the lines are silently ignored
        # the sensor the diagnostics look for is the one loaded (a swap changes both)
        assert block == [f'dtoverlay={CAMERA_SENSOR}', f'dtoverlay={CAMERA_SENSOR},cam0',
                         'dtparam=i2c_csi_dsi=on', 'dtparam=i2c_csi_dsi0=on']
        # autodetect flipped where it stood, not appended a second time
        lines = fb.config_lines()
        assert [line for line in lines if line.startswith('camera_auto_detect')] == ['camera_auto_detect=0']
        assert lines[2] == 'camera_auto_detect=0'
        # nothing else in the file was touched
        assert lines[:2] == STOCK_CONFIG.splitlines()[:2]
        assert lines[3:8] == STOCK_CONFIG.splitlines()[3:8]

    def test_a_config_with_no_autodetect_line_gets_one(self, firstboot):
        fb = firstboot(config='dtparam=audio=on\n[pi4]\narm_boost=1\n')
        assert fb.run('--camera-config').returncode == 2
        header, block = self.camera_block(fb)
        assert header == '[all]'
        assert block[0] == 'camera_auto_detect=0'
        assert 'dtoverlay=imx296' in block

    def test_running_it_again_changes_nothing(self, firstboot):
        fb = firstboot()
        assert fb.run('--camera-config').returncode == 2
        once = fb.config.read_text()
        result = fb.run('--camera-config')
        assert result.returncode == 0
        assert fb.config.read_text() == once

    @pytest.mark.parametrize('model', ['Raspberry Pi 5 Model B Rev 1.0',
                                       'Raspberry Pi 4 Model B Rev 1.4', None])
    def test_a_board_with_autodetect_is_left_alone(self, firstboot, model):
        """Forced overlays fight autodetect on a Pi 4/5."""
        fb = firstboot(model=model)
        result = fb.run('--camera-config')
        assert result.returncode == 0
        assert fb.config.read_text() == STOCK_CONFIG

    @pytest.mark.parametrize('other', ['dtoverlay=imx477', 'dtoverlay=ov5647,cam0',
                                       'dtoverlay=arducam-pivariety'])
    def test_a_different_sensor_overlay_is_refused(self, firstboot, other):
        """IMX296 and IMX477 both answer at I2C 0x1a: never stack two."""
        config = STOCK_CONFIG + other + '\n'
        fb = firstboot(config=config)
        result = fb.run('--camera-config')
        assert result.returncode == 1
        assert other in result.stdout
        assert fb.config.read_text() == config

    def test_the_same_sensor_already_loaded_is_not_a_conflict(self, firstboot):
        fb = firstboot(config=STOCK_CONFIG + 'dtoverlay=imx296\n')
        assert fb.run('--camera-config').returncode == 2

    def test_missing_config_txt_is_an_error(self, firstboot):
        fb = firstboot(config=None)
        assert fb.run('--camera-config').returncode == 1
        assert not fb.config.exists()


class TestGpsConfig:
    """--gps-config: nRF9151 modem UART + power, and the external Wi-Fi
    antenna, on the production CM5 carrier."""

    def test_cm5_gets_uart_power_and_antenna(self, firstboot):
        fb = firstboot(config=STOCK_CONFIG + 'dtoverlay=uart3-pi5,ctsrts\ndtparam=ant1\n')
        result = fb.run('--gps-config')
        assert result.returncode == 2, result.stdout
        lines = fb.config_lines()
        # ctsrts takes GPIO 10/11, which are output channels on this carrier
        assert [line for line in lines if line.startswith('dtoverlay=uart3')] == ['dtoverlay=uart3-pi5']
        assert 'gpio=26=op,dh' in lines
        assert 'dtparam=ant2' in lines
        assert '#dtparam=ant1' in lines and 'dtparam=ant1' not in lines
        # and applied now, so the modem can be checked without a reboot
        assert 'pinctrl set 26 op dh' in fb.box.calls()
        assert 'dtoverlay uart3-pi5' in fb.box.calls()

        once = fb.config.read_text()
        assert fb.run('--gps-config').returncode == 0
        assert fb.config.read_text() == once

    @pytest.mark.parametrize('model', ['Raspberry Pi Compute Module 4 Rev 1.1',
                                       'Raspberry Pi 5 Model B Rev 1.0'])
    def test_any_other_board_is_left_alone(self, firstboot, model):
        fb = firstboot(model=model)
        assert fb.run('--gps-config').returncode == 0
        assert fb.config.read_text() == STOCK_CONFIG
        assert fb.box.calls('pinctrl') == []


# ===========================================================================
# owl_setup.sh: blocks and functions cut out of the script and run
# ===========================================================================

OWL_SETUP = 'owl_setup.sh'


class TestFactoryArguments:
    """One ship command (v3.19.0): --factory [--yes] and --country CC, no
    aliases for the old --ship / --ship-gps."""

    @pytest.mark.parametrize('args, rc, expect', [
        (['--factory', '--country', 'au'], 0, 'factory=1 yes=0 country=AU'),
        (['--country', 'GB', '--factory', '--yes'], 0, 'factory=1 yes=1 country=GB'),
        (['--factory'], 0, 'factory=1 yes=0 country='),
        (['--country', 'NZ'], 0, 'factory=0 yes=0 country=NZ'),
        ([], 0, 'factory=0 yes=0 country='),
        (['--country'], 1, 'two-letter country code'),
        (['--country', 'AUS'], 1, 'two-letter country code'),
        (['--country', '--factory'], 1, 'two-letter country code'),
        (['--yes'], 1, '--yes only applies to --factory'),
        (['--ship'], 1, "Unknown option '--ship'"),
        (['--ship-gps'], 1, "Unknown option '--ship-gps'"),
    ])
    def test_argument_parsing(self, box, args, rc, expect):
        block = between(source(OWL_SETUP), 'FACTORY_MODE=0', '# Identity gate')
        result = box.run(PRELUDE + block
                         + 'echo "factory=$FACTORY_MODE yes=$FACTORY_YES country=$WIFI_COUNTRY"\n', args)
        assert result.returncode == rc
        assert expect in result.stdout


class TestFactoryConfirmAndDetach:
    """--factory wipes WiFi profiles and SSH keys and locks spraying until
    phone setup: a maker who runs it on their own build is locked out. It
    asks for a typed confirmation, then re-runs itself detached so a dropped
    SSH session cannot kill a 20-minute install half way."""

    @pytest.fixture
    def head(self, box):
        script = source(OWL_SETUP)
        block = between(script, 'FACTORY_MODE=0', '# A Pi reused from a factory build')
        ident = box.tmp / 'identity'
        ident.mkdir()
        (ident / 'device.pub').write_text('-----BEGIN PUBLIC KEY-----\nx\n-----END PUBLIC KEY-----\n')
        (ident / 'tier').write_text('otp\n')
        box.stub('sudo', 'exit ${SUDO_RC:-0}')
        box.stub('setsid', 'echo "detached=${OWL_SETUP_DETACHED:-}" >> "$STUB_LOG"')
        box.stub('tail')
        text = (PRELUDE + 'SCRIPT_DIR=/opt/owl; CURRENT_USER=owl\n' + block
                + '\necho INSTALL-CONTINUES\n')

        def run(args, stdin='', **env):
            return box.run(text, args, stdin=stdin, env={'OWL_IDENTITY_DIR': ident, **env})

        run.identity = ident
        return run

    def test_typing_factory_starts_the_install_detached_with_every_argument(self, head, box):
        result = head(['--factory', '--country', 'au'], stdin='FACTORY\n')
        assert result.returncode == 0
        assert 'Factory mode prepares a unit for a customer' in result.stdout
        assert box.calls('setsid') == ['setsid nohup bash /opt/owl/owl_setup.sh --factory --country au']
        assert 'detached=1' in box.calls()
        # this process only follows the log: the install runs in the child
        assert 'INSTALL-CONTINUES' not in result.stdout

    @pytest.mark.parametrize('typed', ['factory\n', 'y\n', 'FACTORY MODE\n', '\n', ''])
    def test_anything_else_cancels_with_nothing_changed(self, head, box, typed):
        result = head(['--factory'], stdin=typed)
        assert result.returncode == 1
        assert 'Factory mode cancelled. Nothing was changed.' in result.stdout
        assert box.calls('setsid') == [] and box.calls('sudo') == []

    def test_yes_skips_the_question_for_factory_automation(self, head, box):
        result = head(['--factory', '--yes'], stdin='')
        assert result.returncode == 0
        assert 'Factory mode prepares a unit' not in result.stdout
        assert box.calls('setsid') == ['setsid nohup bash /opt/owl/owl_setup.sh --factory --yes']

    def test_the_detached_run_neither_asks_nor_detaches_again(self, head, box):
        """It has no terminal to ask on, and detaching again would loop."""
        result = head(['--factory'], stdin='', OWL_SETUP_DETACHED=1)
        assert result.returncode == 0
        assert 'Factory mode prepares a unit' not in result.stdout
        assert box.calls('setsid') == []
        assert 'INSTALL-CONTINUES' in result.stdout

    @pytest.mark.parametrize('tier', ['disk', 'none'])
    def test_a_unit_without_its_otp_identity_is_refused_before_the_question(self, head, box, tier):
        """A production unit without its OTP identity cannot be claimed in the app."""
        if tier == 'none':
            (head.identity / 'tier').unlink()
            (head.identity / 'device.pub').unlink()
        else:
            (head.identity / 'tier').write_text(tier + '\n')
        result = head(['--factory'], stdin='FACTORY\n')
        assert result.returncode == 1
        assert f'No hardware identity on this unit (tier: {tier})' in result.stdout
        assert 'Factory mode prepares a unit' not in result.stdout
        assert box.calls('setsid') == []

    def test_no_passwordless_sudo_stops_before_detaching(self, head, box):
        result = head(['--factory', '--yes'], SUDO_RC=1)
        assert result.returncode == 1
        assert 'needs passwordless sudo' in result.stdout
        assert box.calls('setsid') == []

    def test_a_maker_install_is_never_asked_or_detached(self, head, box):
        result = head([], stdin='')
        assert result.returncode == 0
        assert 'INSTALL-CONTINUES' in result.stdout
        assert box.calls() == []


class TestWifiCountry:
    """setup_wifi_country: --country CC sets it, --factory without it resets
    to world, a maker install without it is left alone."""

    CMDLINE = 'console=serial0,115200 root=PARTUUID=abcd-02 rootwait'

    @pytest.fixture
    def country(self, box):
        cmdline = box.tmp / 'cmdline.txt'
        fn = function(source(OWL_SETUP), 'setup_wifi_country')
        assert '/boot/firmware/cmdline.txt' in fn
        fn = fn.replace('/boot/firmware/cmdline.txt', str(cmdline))
        box.stub('sudo', 'exec "$@"')
        box.stub('iw')

        def run(country='', factory=0, existing='', raspi='writes'):
            cmdline.write_text(self.CMDLINE + existing + '\n')
            if raspi == 'writes':
                box.stub('raspi-config', f'sed -i "1s/\\$/ cfg80211.ieee80211_regdom=$3/" "{cmdline}"')
            elif raspi == 'silent':
                box.stub('raspi-config')
            text = (PRELUDE + f'WIFI_COUNTRY="{country}"; FACTORY_MODE={factory}\n' + fn
                    + 'setup_wifi_country\necho "RC=$? ERROR=[$ERROR_WIFI_COUNTRY]"\n')
            result = box.run(text)
            return result.stdout.strip().splitlines()[-1], cmdline.read_text().strip()

        return run

    def test_country_is_set_through_raspi_config(self, country, box):
        outcome, cmdline = country('AU')
        assert outcome == 'RC=0 ERROR=[]'
        assert box.calls('raspi-config') == ['raspi-config nonint do_wifi_country AU']
        assert cmdline.endswith('cfg80211.ieee80211_regdom=AU')

    def test_a_country_that_did_not_reach_the_boot_cmdline_blocks(self, country):
        outcome, _ = country('AU', raspi='silent')
        assert outcome == 'RC=1 ERROR=[Wi-Fi country not set]'

    def test_no_raspi_config_blocks(self, country):
        outcome, cmdline = country('AU', raspi='absent')
        assert outcome == 'RC=1 ERROR=[raspi-config missing]'
        assert cmdline == self.CMDLINE

    def test_factory_without_a_country_resets_to_world(self, country, box):
        outcome, cmdline = country(factory=1, existing=' cfg80211.ieee80211_regdom=GB')
        assert outcome == 'RC=0 ERROR=[]'
        assert cmdline == self.CMDLINE
        assert 'iw reg set 00' in box.calls()

    def test_a_maker_install_without_a_country_is_left_alone(self, country, box):
        outcome, cmdline = country(existing=' cfg80211.ieee80211_regdom=GB')
        assert outcome == 'RC=0 ERROR=[]'
        assert cmdline == self.CMDLINE + ' cfg80211.ieee80211_regdom=GB'
        assert box.calls() == []


CONTROLLER_INI = '''[MQTT]
broker_ip = localhost
port = 1883

[GPS]
source = none
port = /dev/ttyUSB0
baudrate = 9600
nmea_port = 8500

[Security]
device_token = s3cret
source = keep-me
'''


class TestFactoryGps:
    """setup_factory_gps: the nRF9151 on the production carrier. Its failure
    is a ship blocker (ERROR_GPS)."""

    @pytest.fixture
    def gps(self, box):
        owl = box.tmp / 'owl'
        (owl / 'config').mkdir(parents=True)
        (owl / 'controller' / 'setup').mkdir(parents=True)
        ini = owl / 'config' / 'CONTROLLER.ini'
        ini.write_text(CONTROLLER_INI)
        ini.chmod(0o644)
        port = box.tmp / 'ttyAMA3'
        port.write_text('')
        (owl / 'controller' / 'setup' / 'install_firstboot.sh').write_text(
            'echo "install_firstboot.sh $*" >> "$STUB_LOG"\nexit ${GPSCFG_RC:-0}\n')
        # the AT probe is `sudo timeout 3 bash -c ...`: answered by AT_RC
        box.stub('sudo', 'if [ "$1" = timeout ]; then exit ${AT_RC:-0}; fi\nexec "$@"')
        box.stub('systemctl', '''case "$1" in
  list-unit-files) [ -n "${MODEMMANAGER:-}" ] && echo "ModemManager.service enabled enabled" ;;
  is-enabled) exit ${OWL_ENABLED_RC:-0} ;;
esac
exit 0''')
        box.stubs('usermod', 'sleep')
        fn = function(source(OWL_SETUP), 'setup_factory_gps')

        def run(port_value=str(port), **env):
            text = (PRELUDE + f'SCRIPT_DIR="{owl}"; CURRENT_USER=owl\n'
                    f'NRF_GPS_SOURCE=nrf91; NRF_GPS_PORT="{port_value}"; NRF_GPS_BAUD=460800\n'
                    + fn + 'setup_factory_gps\necho "RC=$? ERROR=[$ERROR_GPS]"\n')
            result = box.run(text, env=env)
            return result.stdout.strip().splitlines()[-1]

        run.ini, run.port = ini, port
        return run

    def test_gps_settings_are_written_inside_the_gps_section_only(self, gps):
        assert gps() == 'RC=0 ERROR=[]'
        import configparser
        saved = configparser.ConfigParser()
        saved.read(gps.ini)
        assert dict(saved['GPS']) == {'source': 'nrf91', 'port': str(gps.port),
                                      'baudrate': '460800', 'nmea_port': '8500'}
        assert saved.get('MQTT', 'port') == '1883'
        assert saved.get('Security', 'source') == 'keep-me'
        assert saved.get('Security', 'device_token') == 's3cret'
        # the file holds the device token: not left world-readable by the rewrite
        assert gps.ini.stat().st_mode & 0o777 == 0o640
        assert not gps.ini.with_name('CONTROLLER.ini.tmp').exists()

    def test_the_port_is_claimed_powered_and_freed_for_the_link_check(self, gps, box):
        assert gps(MODEMMANAGER=1) == 'RC=0 ERROR=[]'
        calls = box.calls()
        at = lambda prefix: next(i for i, c in enumerate(calls) if c.startswith(prefix))  # noqa: E731
        probe = at('sudo timeout 3 bash -c')
        assert 'usermod -aG dialout owl' in calls
        # ModemManager would probe the port with AT commands and hold it open
        assert 'systemctl disable --now ModemManager.service' in calls
        # the boot config powers the modem: before the probe
        assert at('install_firstboot.sh --gps-config') < probe
        # owl.service's own GPS reader would swallow the modem's OK
        assert at('systemctl stop owl.service') < probe < at('systemctl start owl.service')
        # TX/RX only at the configured baud: no flow control
        assert calls[probe].endswith(f'_ {gps.port} 460800')
        assert '-crtscts' in calls[probe]

    def test_modemmanager_is_only_disabled_where_it_is_installed(self, gps, box):
        assert gps() == 'RC=0 ERROR=[]'
        assert box.calls('systemctl disable') == []

    def test_a_modem_that_does_not_answer_blocks_the_ship(self, gps, box):
        outcome = gps(AT_RC=1)
        assert outcome == f'RC=1 ERROR=[nRF9151 did not answer AT on {gps.port}]'
        assert len(box.calls('sudo timeout 3 bash -c')) == 10
        assert box.calls()[-1] == 'systemctl start owl.service'    # put back either way

    def test_owl_service_is_not_started_on_a_unit_where_it_is_disabled(self, gps, box):
        assert gps(OWL_ENABLED_RC=1) == 'RC=0 ERROR=[]'
        assert box.calls('systemctl start') == []

    def test_a_failed_boot_config_blocks_before_anything_is_written(self, gps, box):
        assert gps(GPSCFG_RC=1) == 'RC=1 ERROR=[nRF9151 boot config failed]'
        assert gps.ini.read_text() == CONTROLLER_INI
        assert box.calls('sudo timeout') == []

    def test_boot_config_changed_is_not_a_failure(self, gps):
        assert gps(GPSCFG_RC=2) == 'RC=0 ERROR=[]'

    def test_an_unconfirmed_port_blocks(self, gps, box):
        assert gps(port_value='') == 'RC=1 ERROR=[nRF9151 GPS source/port not confirmed]'
        assert box.calls() == []

    def test_missing_controller_ini_blocks(self, gps):
        gps.ini.unlink()
        assert gps() == 'RC=1 ERROR=[CONTROLLER.ini not found]'


class TestShipGate:
    """The end of --factory: a unit with a critical failure is not cleaned
    or boxed; a good one has its dev residue removed as the very last step."""

    WIRED = 'sudo bash /opt/owl/tools/ensure_wired.sh'

    @pytest.fixture
    def gate(self, box):
        script = source(OWL_SETUP)
        marker = script.index('# Final ship gate')
        start = script.rindex('if [[ "$FACTORY_MODE" == "1" ]]; then', 0, marker)
        end = script.index('\nelse\n    read -p "Start OWL focusing?', marker)
        block = script[start:end] + '\nfi\n'
        ident = box.tmp / 'identity'
        ident.mkdir()
        (ident / 'tier').write_text('otp\n')
        (ident / 'device.pub').write_text('key\n')
        box.stubs('sudo', 'sync', 'openssl')

        def run(**errors):
            assign = ''.join(f'ERROR_{name}="{value}"\n' for name, value in errors.items())
            text = (PRELUDE + 'FACTORY_MODE=1; SETUP_ARGS=(--factory); SCRIPT_DIR=/opt/owl\n'
                    f'OWL_IDENTITY_DIR="{ident}"; NRF_GPS_PORT=/dev/ttyAMA3; NRF_GPS_SOURCE=nrf91\n'
                    'WIFI_COUNTRY=""\n' + assign + block + 'wait\n')
            return box.run(text)

        return run

    @pytest.mark.parametrize('failed, named', [
        ('OPENCV', 'OpenCV install'), ('OWL_DEPS', 'OWL dependencies'),
        ('DASHBOARD_DEPS', 'dashboard dependencies'), ('DASHBOARD', 'dashboard/first-boot setup'),
        ('GPS', 'nRF9151 GPS setup'), ('WIFI_COUNTRY', 'Wi-Fi country'),
    ])
    def test_a_critical_failure_blocks_the_ship_and_skips_the_cleanup(self, gate, box, failed, named):
        """Cleanup deletes the WiFi profiles: skipping it keeps a failed
        unit reachable for repair."""
        result = gate(**{failed: 'it failed'})
        assert result.returncode == 1
        assert f'[NOT SHIP-READY] Critical failures: {named}' in result.stdout
        assert '[SHIP-READY]' not in result.stdout
        # only the wired-port repair ran: no sync, no cleanup
        assert box.calls() == [self.WIRED]

    def test_a_good_unit_is_synced_then_cleaned_detached_as_the_last_step(self, gate, box):
        result = gate()
        assert result.returncode == 0, result.stderr
        assert '[SHIP-READY] Unit provisioned for shipping' in result.stdout
        calls = [c for c in box.calls() if not c.startswith('openssl')]
        # detached (setsid): the cleanup drops an SSH session over a personal
        # WiFi profile. A .txt log: clean.sh truncates /var/log/*.log
        assert calls == [
            self.WIRED,
            'sync ',
            "sudo setsid bash -c bash '/opt/owl/dev/clean.sh' --yes > /var/log/owl-ship-clean.txt 2>&1",
        ]
        assert 'Wired Ethernet: profile present' in result.stdout

    def test_a_unit_whose_ethernet_port_cannot_come_up_does_not_ship(self, gate, box):
        """The wired port is the way in when Wi-Fi is not: if the repair
        fails the unit is not cleaned or boxed."""
        box.stub('sudo', 'case "$*" in *ensure_wired.sh*) exit 1 ;; esac')
        result = gate()
        assert result.returncode == 1
        assert '[NOT SHIP-READY] Critical failures: wired Ethernet has no working profile' in result.stdout
        assert not [c for c in box.calls() if 'clean.sh' in c]


class TestMakerIdentityStep:
    """Step 13c: a self-built OWL is offered a disk identity; --factory never
    asks (it already refused a unit without its OTP identity)."""

    @pytest.fixture
    def step(self, box):
        block = between(source(OWL_SETUP), '# Step 13c: maker identity', '# Step 14')
        ident = box.tmp / 'identity'
        ident.mkdir()
        box.stub('sudo')

        def run(factory, stdin=''):
            text = (PRELUDE + f'FACTORY_MODE={factory}; SCRIPT_DIR=/opt/owl\n'
                    f'OWL_IDENTITY_DIR="{ident}"\n' + block)
            return box.run(text, stdin=stdin)

        run.identity = ident
        return run

    @pytest.mark.parametrize('answer, created', [('y\n', True), ('Y\n', True), ('n\n', False),
                                                 ('\n', False), ('', False)])
    def test_a_maker_is_asked_and_no_is_the_default(self, step, box, answer, created):
        assert step(0, stdin=answer).returncode == 0
        expected = ['sudo bash /opt/owl/owl_identity.sh --disk'] if created else []
        assert box.calls() == expected

    def test_factory_never_asks(self, step, box):
        assert step(1, stdin='y\n').returncode == 0
        assert box.calls() == []

    def test_an_existing_identity_is_kept(self, step, box):
        (step.identity / 'device.pub').write_text('key\n')
        (step.identity / 'tier').write_text('disk\n')
        result = step(0, stdin='y\n')
        assert 'Device identity already present (disk tier)' in result.stdout
        assert box.calls() == []


# ===========================================================================
# dev/clean.sh
# ===========================================================================

CLEAN = 'dev/clean.sh'


class TestCleanScript:
    """dev/clean.sh strips dev residue before a unit ships (a dev phone
    hotspot password shipped on a bench unit, 2026-07-17)."""

    CONNECTIONS = ('802-11-wireless:uuid-hotspot:OWL-12EF\n'
                   '802-11-wireless:uuid-home:Home WiFi\n'
                   '802-11-wireless:uuid-phone:Dev: phone 5G\n'      # a name with a colon
                   '802-3-ethernet:uuid-wired:Wired connection 1\n'
                   'loopback:uuid-lo:lo\n')

    def wifi_cleanup(self, box, connections):
        listing = box.tmp / 'connections.txt'
        listing.write_text(connections)
        box.stub('nmcli', f'''case "$*" in
  "-t -f NAME,TYPE connection show")
      awk -F: '{{n=$0; sub(/^[^:]*:[^:]*:/, "", n); print n ":" $1}}' "{listing}" ;;
  "-t -e no -f TYPE,UUID,NAME connection show") cat "{listing}" ;;
esac''')
        block = between(source(CLEAN), '# Step 2: Clean network connections', '# Step 3:')
        result = box.run('set -euo pipefail\n' + PRELUDE + block)
        assert result.returncode == 0, result.stderr
        return box.calls('nmcli connection delete')

    def test_only_personal_wifi_profiles_are_deleted(self, box):
        """The hotspot stays (the phone joins it) and so does wired eth0:
        deleting it left a factory unit unreachable over Ethernet (2026-10-19)."""
        assert self.wifi_cleanup(box, self.CONNECTIONS) == [
            'nmcli connection delete uuid uuid-home',
            'nmcli connection delete uuid uuid-phone',
        ]

    def test_with_no_hotspot_nothing_is_deleted(self, box):
        """Deleting every profile on a unit with no hotspot leaves no way in."""
        connections = self.CONNECTIONS.replace('OWL-12EF', 'Workshop')
        assert self.wifi_cleanup(box, connections) == []

    @pytest.mark.parametrize('args, typed, proceeds', [
        (['--yes'], '', True),             # owl_setup.sh --factory: no terminal
        (['-y'], '', True),
        ([], 'y\n', True),
        ([], 'n\n', False),
        ([], '\n', False),
    ])
    def test_yes_runs_without_a_prompt_and_no_answer_cancels(self, box, args, typed, proceeds):
        script = source(CLEAN)
        text = ('set -euo pipefail\n' + PRELUDE
                + between(script, 'ASSUME_YES=0', '# Must run as root')
                + between(script, 'if [[ "$ASSUME_YES" == "1" ]]; then',
                          '# Stop OWL services during cleaning')
                + 'echo CLEANING-STARTS\n')
        result = box.run(text, args, stdin=typed)
        assert result.returncode == 0, result.stderr
        assert ('CLEANING-STARTS' in result.stdout) is proceeds

    def test_every_service_stopped_for_the_clean_is_started_again(self, box):
        script = source(CLEAN)
        box.stub('systemctl')
        box.stub('sleep')
        text = ('set -euo pipefail\n' + PRELUDE
                + between(script, '# Stop OWL services during cleaning', 'cd /home/owl')
                + between(script, '# Step 12: Restart OWL services', 'sleep 2'))
        assert box.run(text).returncode == 0
        stopped = [c.split()[2] for c in box.calls('systemctl stop')]
        started = [c.split()[2] for c in box.calls('systemctl start')]
        assert stopped == ['owl.service', 'owl-dash.service']
        assert started == stopped


# ===========================================================================
# Wired Ethernet always keeps a profile that comes up by itself
# ===========================================================================

ENSURE_WIRED = 'tools/ensure_wired.sh'


class TestWiredEthernetIsNeverLeftDown:
    """Ethernet is the way into a unit when Wi-Fi is not. Until 2026-10-19
    dev/clean.sh deleted the wired profile with everything else, and
    NetworkManager never recreates a deleted default one: eth0 stayed
    "disconnected" for good (found on a field unit, 2026-11-19).

    tools/ensure_wired.sh runs under bash against an nmcli stand-in that
    answers from two small tables and logs every change asked of it."""

    def nm(self, box, devices, connections, fail=''):
        """connections: (uuid, type, interface-name, autoconnect) rows."""
        table = box.tmp / 'connections.txt'
        table.write_text(''.join(':'.join(row) + '\n' for row in connections))
        (box.tmp / 'devices.txt').write_text(''.join(f'{d}:{t}\n' for d, t in devices))
        box.stub('sudo', '[ "$1" = "-n" ] && shift\nexec "$@"')
        box.stub('nmcli', f'''case "$*" in
  "-t -f DEVICE,TYPE device") cat "{box.tmp}/devices.txt" ;;
  "-t -f UUID,TYPE connection show") cut -d: -f1,2 "{table}" ;;
  "-g connection.interface-name connection show uuid "*) grep "^$6:" "{table}" | cut -d: -f3 ;;
  "-g connection.autoconnect connection show uuid "*) grep "^$6:" "{table}" | cut -d: -f4 ;;
  {fail or '"never"'}) exit 4 ;;
esac''')

    def run(self, box, *args):
        return box.run(REPO / ENSURE_WIRED, args)

    def changes(self, box):
        return [c for c in box.calls('nmcli ') if not c.startswith(('nmcli -t', 'nmcli -g'))]

    ETH = [('eth0', 'ethernet'), ('wlan0', 'wifi'), ('lo', 'loopback')]
    HOTSPOT = ('u-hot', '802-11-wireless', 'wlan0', 'yes')

    def test_a_unit_whose_wired_profile_was_deleted_gets_one_back(self, box):
        self.nm(box, self.ETH, [self.HOTSPOT])
        result = self.run(box)
        assert result.returncode == 0, result.stdout + result.stderr
        assert self.changes(box) == [
            'nmcli connection add type ethernet con-name owl-wired-eth0 ifname eth0 '
            'connection.autoconnect yes connection.autoconnect-retries 0 '
            'ipv4.method auto ipv6.method auto',
            'nmcli connection modify owl-wired-eth0 ipv4.link-local enabled',
            'nmcli device connect eth0',
        ]
        assert "created 'owl-wired-eth0'" in result.stdout

    @pytest.mark.parametrize('profile', [
        ('u-wired', '802-3-ethernet', '', 'yes'),                 # the OS default: any interface
        ('u-static', '802-3-ethernet', 'eth0', 'yes'),            # owl-ethernet from setup.sh
    ])
    def test_a_working_wired_profile_is_left_exactly_as_it_is(self, box, profile):
        self.nm(box, self.ETH, [self.HOTSPOT, profile])
        result = self.run(box)
        assert result.returncode == 0
        assert self.changes(box) == [], 'a working wired profile was touched'
        assert 'wired profile present' in result.stdout

    def test_a_wired_profile_that_would_not_connect_by_itself_is_switched_on(self, box):
        self.nm(box, self.ETH, [('u-wired', '802-3-ethernet', 'eth0', 'no')])
        assert self.run(box).returncode == 0
        assert self.changes(box) == [
            'nmcli connection modify uuid u-wired connection.autoconnect yes',
            'nmcli device connect eth0',
        ]

    def test_a_profile_for_another_port_does_not_count(self, box):
        """Two ports, one profile bound to eth1: eth0 still has nothing."""
        devices = [('eth0', 'ethernet'), ('eth1', 'ethernet')]
        self.nm(box, devices, [('u-eth1', '802-3-ethernet', 'eth1', 'yes')])
        assert self.run(box).returncode == 0
        added = [c for c in self.changes(box) if ' connection add ' in c]
        assert len(added) == 1 and 'con-name owl-wired-eth0 ifname eth0' in added[0]

    def test_nothing_is_ever_deleted_renamed_or_readdressed(self, box):
        for connections in ([self.HOTSPOT], [('u-wired', '802-3-ethernet', 'eth0', 'no')],
                            [('u-static', '802-3-ethernet', 'eth0', 'yes')]):
            box.log.write_text('')
            self.nm(box, self.ETH, connections)
            self.run(box)
            for call in self.changes(box):
                assert ' delete ' not in call and ' down ' not in call and 'disconnect' not in call
                assert 'ipv4.addresses' not in call and 'connection.id' not in call

    def test_a_unit_with_no_wired_port_is_fine(self, box):
        """A carrier board without Ethernet must not fail the ship gate."""
        self.nm(box, [('wlan0', 'wifi')], [self.HOTSPOT])
        result = self.run(box)
        assert result.returncode == 0 and self.changes(box) == []
        assert 'no wired interface' in result.stdout

    def test_check_reports_without_changing_anything(self, box):
        self.nm(box, self.ETH, [self.HOTSPOT])
        result = self.run(box, '--check')
        assert result.returncode == 1
        assert 'PROBLEM' in result.stdout and self.changes(box) == []

        box.log.write_text('')
        self.nm(box, self.ETH, [('u-wired', '802-3-ethernet', '', 'yes')])
        assert self.run(box, '--check').returncode == 0

    def test_a_repair_that_fails_is_reported_as_a_failure(self, box):
        """The factory ship gate blocks on this exit code."""
        self.nm(box, self.ETH, [self.HOTSPOT], fail='"connection add "*')
        result = self.run(box)
        assert result.returncode == 1
        assert 'could not create a wired profile' in result.stdout

    def test_an_older_networkmanager_without_link_local_still_gets_dhcp(self, box):
        self.nm(box, self.ETH, [self.HOTSPOT], fail='*"ipv4.link-local enabled"')
        result = self.run(box)
        assert result.returncode == 0, result.stdout
        assert any(' connection add ' in c for c in self.changes(box))

    def test_the_cleaning_script_repairs_and_reports_the_wired_port(self, box):
        """dev/clean.sh: the wired step runs the repair, and the hand-over
        summary says whether the port will come up."""
        script = source(CLEAN)
        assert 'tools/ensure_wired.sh"' in between(script, '# Wired Ethernet stays, always',
                                                   '# Step 3:')
        assert '--check' in between(script, 'echo "Wired Ethernet:"', 'echo "WiFi Hotspot:"')
        assert '- Wired Ethernet: ${WIRED_STATUS}' in script

    @pytest.mark.parametrize('script, step', [
        ('controller/shared/setup.sh', '# Step 5: Configure UFW firewall'),
        ('controller/networked/in-cab_controller_setup.sh', '# Step 11b: Configure sudoers'),
        ('owl_setup.sh', 'if [ -n "$SHIP_BLOCKERS" ]; then'),
        ('owl_update.sh', '# --- Restart + health check'),
    ])
    def test_every_provisioning_path_runs_the_wired_check(self, script, step):
        """Structure only: each script calls the repair before the named step."""
        text = source(script)
        assert 'tools/ensure_wired.sh' in text[:text.index(step)], f'{script} no longer ensures eth0'

    def test_no_script_takes_a_wired_connection_down(self):
        """Structure only, and the point of the whole class: nothing in the
        repository's shell scripts may delete, down or disconnect a wired
        profile. The only deletes allowed are the named Wi-Fi/OWL profiles
        each script is about to recreate."""
        import re
        allowed = re.compile(
            r'nmcli con(nection)? delete (uuid "\$uuid"|"\$\{(SSID|CON_NAME|WIFI_SSID)\}")')
        for path in sorted(REPO.glob('**/*.sh')):
            if 'node_modules' in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                code = line.split('#', 1)[0]
                if re.search(r'nmcli\s+(dev(ice)?\s+(disconnect|down)|networking\s+off|con(nection)?\s+down)', code):
                    pytest.fail(f'{path.relative_to(REPO)}:{number} takes a connection down: {line.strip()}')
                if re.search(r'nmcli\s+con(nection)?\s+delete', code) and not allowed.search(code):
                    pytest.fail(f'{path.relative_to(REPO)}:{number} deletes a connection that is not '
                                f'a named Wi-Fi/OWL profile: {line.strip()}')
                if re.search(r'\b(ifdown|ifconfig\s+eth\d+\s+down|ip\s+link\s+set\s+\S+\s+down)\b', code):
                    pytest.fail(f'{path.relative_to(REPO)}:{number} takes an interface down: {line.strip()}')


# ===========================================================================
# The fallback responder must start with nothing but the standard library
# ===========================================================================

class TestFallbackServerNeedsNoVenv:
    def test_imports_with_site_packages_unavailable(self):
        """It is started when the venv is what is broken. -S -I: no
        site-packages, no user site, no PYTHONPATH."""
        setup_dir = REPO / 'controller' / 'setup'
        result = subprocess.run(
            [sys.executable, '-S', '-I', '-c',
             'import sys; sys.path.insert(0, sys.argv[1]); import fallback_server; '
             'print(fallback_server.FallbackHandler.__name__)', str(setup_dir)],
            capture_output=True, text=True, timeout=60,
            env={'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1'})
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'FallbackHandler'


# ===========================================================================
# STRUCTURE CHECKS: text only, no behaviour proven
# ===========================================================================

class TestScriptStructure:
    """Step ORDER inside owl_setup.sh. The script is 1100 lines of apt, pip
    and systemd run top to bottom, so the order of its steps cannot be
    executed in a test. These read the text and nothing else: they catch a
    step that was moved or dropped, not one that is wrong. What each step
    DOES is tested above."""

    @pytest.fixture
    def owl_setup(self):
        return source(OWL_SETUP)

    def test_camera_overlay_is_configured_before_the_camera_checks(self, owl_setup):
        configure = owl_setup.index('install_firstboot.sh" --camera-config')
        assert configure < owl_setup.index('\ncheck_camera_connection\n')

    def test_wifi_country_is_set_before_the_dashboard_brings_up_the_hotspot(self, owl_setup):
        assert (owl_setup.index('\nsetup_wifi_country\n')
                < owl_setup.index('# Step 13: Dashboard Setup'))

    def test_factory_gps_runs_after_the_dashboard_step_and_only_in_factory_mode(self, owl_setup):
        # controller/shared/setup.sh writes [GPS] source = none; the GPS step
        # must overwrite it, not be overwritten by it
        dashboard = owl_setup.index('controller/shared/setup.sh" --mode standalone')
        call = owl_setup.index('\n    setup_factory_gps\n')
        assert call > dashboard
        guard = owl_setup.rindex('if [[ "$FACTORY_MODE" == "1" ]]; then', 0, call)
        assert call - guard < 60

    def test_identity_gate_comes_before_the_confirmation_and_the_detach(self, owl_setup):
        gate = owl_setup.index('[NOT SHIP-READY] No hardware identity')
        assert gate < owl_setup.index('Type FACTORY to continue') < owl_setup.index('setsid nohup bash')
