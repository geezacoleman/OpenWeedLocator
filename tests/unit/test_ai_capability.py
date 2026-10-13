"""utils/ai_capability.py: the 'ai' block the OWL publishes, and the shared
rule both dashboards use to refuse a detection mode the OWL cannot run."""

import subprocess
from types import SimpleNamespace

import pytest

from utils import ai_capability as aic


def _fake_env(monkeypatch, tmp_path, modules=(), dev_hailo=False, lspci_out=None,
              identify_out=None):
    """modules: importable names. lspci_out / identify_out None = tool not installed."""
    monkeypatch.setattr(aic.importlib.util, 'find_spec',
                        lambda name: object() if name in modules else None)
    device = tmp_path / 'hailo0'
    if dev_hailo:
        device.write_text('')
    monkeypatch.setattr(aic, 'HAILO_DEVICE', str(device))
    tools = {'lspci': lspci_out, 'hailortcli': identify_out}
    monkeypatch.setattr(aic.shutil, 'which',
                        lambda name: f'/usr/bin/{name}' if tools.get(name) is not None else None)
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=tools.get(argv[0]) or '', returncode=0)

    return runner, calls


@pytest.mark.unit
class TestDetect:
    def test_factory_unit_has_no_ai(self, monkeypatch, tmp_path):
        runner, _ = _fake_env(monkeypatch, tmp_path)
        assert aic.detect(runner) == {
            'cpu': {'installed': False},
            'hailo': {'present': False, 'chip': None, 'runtime': False},
            'accelerator': None,
        }

    def test_cpu_only(self, monkeypatch, tmp_path):
        runner, _ = _fake_env(monkeypatch, tmp_path, modules={'ultralytics'})
        ai = aic.detect(runner)
        assert ai['cpu'] == {'installed': True}
        assert ai['accelerator'] == 'cpu'

    def test_hailo8_card_and_runtime(self, monkeypatch, tmp_path):
        runner, _ = _fake_env(monkeypatch, tmp_path, modules={'hailo_platform'}, dev_hailo=True,
                              identify_out='Identifying board\nDevice Architecture: HAILO8\n')
        ai = aic.detect(runner)
        assert ai['hailo'] == {'present': True, 'chip': 'hailo8', 'runtime': True}
        assert ai['accelerator'] == 'hailo8'

    @pytest.mark.parametrize('arch,chip', [('HAILO8L', 'hailo8l'), ('HAILO10H', 'hailo10h')])
    def test_other_hailo_chips_are_reported_but_not_used(self, monkeypatch, tmp_path, arch, chip):
        # Only the Hailo-8 is supported; DIY 8L/10H boards run AI on the CPU
        runner, _ = _fake_env(monkeypatch, tmp_path, modules={'ultralytics', 'hailo_platform'},
                              dev_hailo=True, identify_out=f'Device Architecture: {arch}\n')
        ai = aic.detect(runner)
        assert ai['hailo']['chip'] == chip
        assert ai['accelerator'] == 'cpu'

    def test_runtime_but_identify_fails_is_not_an_accelerator(self, monkeypatch, tmp_path):
        runner, _ = _fake_env(monkeypatch, tmp_path, modules={'hailo_platform'}, dev_hailo=True)
        ai = aic.detect(runner)
        assert ai['hailo'] == {'present': True, 'chip': None, 'runtime': True}
        assert ai['accelerator'] is None

    def test_hailo_card_without_runtime_falls_to_cpu(self, monkeypatch, tmp_path):
        runner, _ = _fake_env(monkeypatch, tmp_path, modules={'ultralytics'}, lspci_out=(
            '0000:01:00.0 Co-processor: Hailo Technologies Ltd. Hailo-8 AI Processor (rev 01)\n'))
        ai = aic.detect(runner)
        # lspci names the 8 and 8L the same: chip unknown until the runtime is in
        assert ai['hailo'] == {'present': True, 'chip': None, 'runtime': False}
        assert ai['accelerator'] == 'cpu'

    def test_lspci_runs_with_a_timeout(self, monkeypatch, tmp_path):
        runner, calls = _fake_env(monkeypatch, tmp_path, lspci_out='00:00.0 PCI bridge: Broadcom\n')
        assert aic.detect(runner)['hailo']['present'] is False
        assert calls and calls[0][1]['timeout'] == aic.LSPCI_TIMEOUT_S

    def test_lspci_missing_or_hanging_is_not_present(self, monkeypatch, tmp_path):
        _fake_env(monkeypatch, tmp_path)  # no lspci
        assert aic.hailo_on_pcie() is False

        monkeypatch.setattr(aic.shutil, 'which', lambda name: '/usr/bin/lspci')

        def hang(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get('timeout'))

        assert aic.hailo_on_pcie(hang) is False

    def test_find_spec_error_is_not_installed(self, monkeypatch, tmp_path):
        def broken(name):
            raise ValueError('bad spec')

        monkeypatch.setattr(aic.importlib.util, 'find_spec', broken)
        assert aic.module_installed('ultralytics') is False


@pytest.mark.unit
class TestAlgorithmRefusal:
    NO_AI = {'cpu': {'installed': False},
             'hailo': {'present': False, 'chip': None, 'runtime': False},
             'accelerator': None}
    CPU = dict(NO_AI, cpu={'installed': True}, accelerator='cpu')

    @pytest.mark.parametrize('algorithm', ['gog', 'gog-hybrid'])
    def test_ai_refused_without_ai(self, algorithm):
        assert aic.algorithm_refusal(algorithm, self.NO_AI) == aic.NO_AI_REASON

    @pytest.mark.parametrize('algorithm', ['gog', 'gog-hybrid'])
    def test_ai_allowed_with_cpu_ai(self, algorithm):
        assert aic.algorithm_refusal(algorithm, self.CPU) is None

    def test_unknown_capability_is_not_refused(self):
        """An OWL that has not reported (older firmware) falls back itself."""
        assert aic.algorithm_refusal('gog', None) is None
        assert aic.algorithm_refusal('gog', {}) is None

    def test_lut_needs_a_profile(self):
        assert aic.algorithm_refusal('lut', self.NO_AI, '') == aic.NO_LUT_REASON
        assert aic.algorithm_refusal('lut', self.NO_AI, 'paddock_a') is None

    @pytest.mark.parametrize('algorithm', ['exg', 'exhsv', 'hsv'])
    def test_colour_modes_always_allowed(self, algorithm):
        assert aic.algorithm_refusal(algorithm, self.NO_AI) is None
