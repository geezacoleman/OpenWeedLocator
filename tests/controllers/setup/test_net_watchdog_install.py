"""install_firstboot.sh --net-watchdog and the setup.sh hooks around it.

The unit runs as root from a root-owned copy: it must never point into the
repo (the owl user owns it), and the copied modules must work with system
python3 (no venv packages)."""

import ast
import re
import sys
from pathlib import Path

import pytest

from utils.network_manager import HOTSPOT_AUTOCONNECT_PRIORITY

REPO = Path(__file__).resolve().parents[3]
INSTALLER = (REPO / 'controller' / 'setup' / 'install_firstboot.sh').read_text(encoding='utf-8')
SETUP = (REPO / 'controller' / 'shared' / 'setup.sh').read_text(encoding='utf-8')


def watchdog_unit():
    body = INSTALLER.split('cat > "${NET_UNIT_DEST}" <<EOF || return 1\n')[1]
    return body.split('\nEOF')[0]


@pytest.mark.unit
class TestNetWatchdogInstall:
    def test_unit_is_flush_left_and_plain(self):
        unit = watchdog_unit()
        assert unit.lstrip('\n').startswith('[Unit]')
        assert all(line == line.lstrip() for line in unit.splitlines())
        # Unquoted heredoc: these would run at install time
        assert '`' not in unit and '$(' not in unit

    def test_runs_the_root_copy_with_system_python(self):
        unit = watchdog_unit()
        assert 'ExecStart=/usr/bin/python3 -E -s -m utils.net_watchdog' in unit
        assert 'WorkingDirectory=${NET_LIB_DIR}' in unit
        assert '${OWL_DIR}' not in unit
        assert re.search(r'^NET_LIB_DIR="/usr/local/lib/owl/', INSTALLER, re.MULTILINE)
        assert 'Restart=always' in unit

    def test_dispatcher_arm_and_setup_install_it(self):
        assert '--net-watchdog) install_net_watchdog; exit $? ;;' in INSTALLER
        arm = INSTALLER.split('\narm() {')[1].split('\n}\n')[0]
        assert 'install_net_watchdog' in arm
        assert '"${FIRSTBOOT_SCRIPT}" --net-watchdog' in SETUP

    def test_setup_hotspot_priority_matches_the_constant(self):
        assert (f'connection.autoconnect-priority {HOTSPOT_AUTOCONNECT_PRIORITY}'
                in SETUP)

    @pytest.mark.parametrize('module', ['net_watchdog.py', 'network_manager.py'])
    def test_copied_modules_need_only_the_standard_library(self, module):
        tree = ast.parse((REPO / 'utils' / module).read_text(encoding='utf-8'))
        stdlib = set(sys.stdlib_module_names)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module]
            else:
                continue
            for name in names:
                assert name.split('.')[0] in stdlib or name == 'utils.network_manager', name
