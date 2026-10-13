"""
What AI this OWL can run, reported so the app and dashboards can be honest.

Factory units (owl_setup.sh --factory) ship without ultralytics, so choosing
AI used to persist algorithm=gog, fail to load, and leave the unit with no
detection after the next restart. The OWL now publishes this block in its
MQTT state ('ai') and the dashboards refuse AI modes it cannot run:

    {'cpu': {'installed': bool},
     'hailo': {'present': bool, 'chip': 'hailo8' | 'hailo8l' | 'hailo10h' | None,
               'runtime': bool},
     'accelerator': 'hailo8' | 'cpu' | None}

Only the Hailo-8 is supported (decided 2026-10-12, to keep it simple). Other
Hailo chips a DIY builder fits (8L, 10H) are reported so the screens can say
"not supported" and AI runs on the CPU. lspci names the 8 and 8L the same, so
the chip comes from hailortcli once the runtime is installed; None means "a
Hailo card, chip not known yet" (docs/HAILO.md).

Computed once at startup (and again after a software update completes):
the lspci probe is a subprocess, never something to run per heartbeat.
"""

import importlib.util
import os
import shutil
import subprocess

AI_ALGORITHMS = ('gog', 'gog-hybrid')
HAILO_DEVICE = '/dev/hailo0'
LSPCI_TIMEOUT_S = 3
HAILORTCLI_TIMEOUT_S = 5
# Chips the OWL runs AI on (HailoRT 4.23)
SUPPORTED_HAILO_CHIPS = ('hailo8',)

NO_AI_REASON = 'AI is not installed on this OWL. Install AI, then try again.'
NO_LUT_REASON = 'No painted profile is set up yet. Paint one first.'


def module_installed(name):
    """True when `name` can be imported, without importing it (ultralytics
    pulls in torch: seconds of CPU on a Pi)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _run(argv, runner, timeout):
    """stdout of argv, or '' when the tool is missing, hangs or fails."""
    if not shutil.which(argv[0]):
        return ''
    run = runner or subprocess.run
    try:
        result = run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ''
    return result.stdout or ''


def hailo_pcie_line(runner=None):
    """The lspci line for a Hailo device, or ''. A missing or hanging lspci
    is '': the device node check covers a loaded driver anyway."""
    for line in _run(['lspci'], runner, LSPCI_TIMEOUT_S).splitlines():
        if 'Hailo' in line:
            return line
    return ''


def hailo_on_pcie(runner=None):
    return bool(hailo_pcie_line(runner))


def hailo_chip(pcie_line='', runtime=False, runner=None):
    """'hailo8' | 'hailo8l' | 'hailo10h' | None. hailortcli reports the real
    architecture; lspci can only tell the 10H apart."""
    if runtime:
        out = _run(['hailortcli', 'fw-control', 'identify'], runner, HAILORTCLI_TIMEOUT_S)
        for line in out.splitlines():
            if 'Architecture' in line and ':' in line:
                arch = line.split(':', 1)[1].strip().lower().replace('-', '')
                if arch.startswith('hailo'):
                    return arch
    if '10H' in pcie_line:
        return 'hailo10h'
    return None


def detect(runner=None):
    """The 'ai' capability block for this machine."""
    cpu_installed = module_installed('ultralytics')
    pcie_line = hailo_pcie_line(runner)
    hailo_present = os.path.exists(HAILO_DEVICE) or bool(pcie_line)
    hailo_runtime = module_installed('hailo_platform')
    chip = hailo_chip(pcie_line, hailo_runtime, runner) if hailo_present else None
    if hailo_present and hailo_runtime and chip in SUPPORTED_HAILO_CHIPS:
        accelerator = chip
    elif cpu_installed:
        accelerator = 'cpu'
    else:
        accelerator = None
    return {
        'cpu': {'installed': cpu_installed},
        'hailo': {'present': hailo_present, 'chip': chip, 'runtime': hailo_runtime},
        'accelerator': accelerator,
    }


def can_run_ai(ai):
    """False only when the OWL has said it has no AI at all. An OWL that has
    not reported (older firmware, not started yet) is not refused: it falls
    back to colour detection itself if the model cannot load."""
    if not isinstance(ai, dict) or 'accelerator' not in ai:
        return True
    return ai.get('accelerator') is not None


def algorithm_refusal(algorithm, ai=None, lut_profile=None):
    """Plain reason the OWL cannot switch to `algorithm`, or None.

    Shared by the standalone and networked /api/algorithm/set so both refuse
    the same things with the same words."""
    if algorithm in AI_ALGORITHMS and not can_run_ai(ai):
        return NO_AI_REASON
    if algorithm == 'lut' and not lut_profile:
        return NO_LUT_REASON
    return None
