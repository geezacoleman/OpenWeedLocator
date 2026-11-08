"""tools/relays_off.py (owl.service ExecStopPost) and the pins file it reads."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_tool():
    spec = importlib.util.spec_from_file_location('relays_off', ROOT / 'tools' / 'relays_off.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
class TestRelaysOff:
    def test_reads_only_sane_bcm_numbers(self, tmp_path):
        tool = load_tool()
        path = tmp_path / 'pins'
        path.write_text('27 22 x 99 -1 23\n')
        assert tool.read_pins(str(path)) == [27, 22, 23]

    def test_missing_file_is_nothing_to_do(self, tmp_path):
        tool = load_tool()
        assert tool.read_pins(str(tmp_path / 'absent')) == []

    def test_drives_each_pin_low_with_pinctrl(self):
        tool = load_tool()
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0)

        failed = tool.drive_low([27, 22], run=run, which=lambda name: '/usr/bin/pinctrl'
                                if name == 'pinctrl' else None)
        assert failed == []
        assert calls == [['/usr/bin/pinctrl', 'set', '27', 'op', 'dl'],
                         ['/usr/bin/pinctrl', 'set', '22', 'op', 'dl']]

    def test_reports_failures_and_missing_tool(self):
        tool = load_tool()
        assert tool.drive_low([5], run=lambda argv, **k: SimpleNamespace(returncode=1),
                              which=lambda name: '/usr/bin/pinctrl') == [5]
        assert tool.drive_low([5], which=lambda name: None) == [5]

    def test_stdlib_only(self):
        # Runs under system python3 -I from ExecStopPost: no venv packages
        text = (ROOT / 'tools' / 'relays_off.py').read_text()
        for line in text.splitlines():
            if line.startswith(('import ', 'from ')):
                assert line.split()[1].split('.')[0] in {'os', 'shutil', 'subprocess', 'sys'}

    def test_service_unit_runs_it_after_every_exit(self):
        text = (ROOT / 'owl_setup.sh').read_text()
        assert 'ExecStopPost=-/usr/bin/python3 -I $SCRIPT_DIR/tools/relays_off.py' in text


@pytest.mark.unit
def test_relay_control_records_claimed_pins(tmp_path, monkeypatch):
    from utils.output_manager import RelayControl
    path = tmp_path / 'owl-relay-pins'
    monkeypatch.setenv('OWL_RELAY_PINS_FILE', str(path))
    rc = RelayControl({0: 13, 1: 15}, disabled_pins={15: 'GPS'})
    rc.relay_dict[0].pin = 'GPIO27'
    rc._write_pins_file()
    assert path.read_text().split() == ['27'], 'disabled (GPS) pins must never be listed'
