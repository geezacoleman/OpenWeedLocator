"""R1 (v3.9.5) journal-quiet source checks.

owl.py and the controllers cannot be imported on Windows (cv2/GPIO/Flask
side effects), so these assert on source text — the established pattern
from test_config_files.py / test_config_unsaved_and_migration.py.
"""

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _src(relative_path):
    return (PROJECT_ROOT / relative_path).read_text(encoding='utf-8')


@pytest.mark.unit
class TestHighFrequencyCommandLogging:
    """1 Hz commands (set_actuation_params) must log at DEBUG, not INFO —
    45k+ 'Received command' INFO lines were captured in field journals."""

    def test_set_actuation_params_is_high_frequency(self):
        src = _src('utils/mqtt_manager.py')
        assert "HIGH_FREQUENCY_COMMANDS = {'set_actuation_params'}" in src

    def test_command_log_demoted_for_high_frequency(self):
        src = _src('utils/mqtt_manager.py')
        assert 'if action in self.HIGH_FREQUENCY_COMMANDS:' in src
        assert 'self.logger.debug(f"Received command: {action}")' in src


@pytest.mark.unit
class TestLsusbProbeOnce:
    """OS Lite ships without usbutils; a 2 s stats poll must not warn per
    request when lsusb is missing."""

    def test_lsusb_resolved_once_in_init(self):
        src = _src('controller/standalone/standalone.py')
        assert "self.lsusb_path = shutil.which('lsusb')" in src

    def test_stats_poll_skips_silently_without_lsusb(self):
        src = _src('controller/standalone/standalone.py')
        assert 'if self.lsusb_path:' in src
        # the old per-request warning must be gone
        assert 'self.logger.warning(f"USB devices retrieval error' not in src

    def test_usbutils_installed_by_setup(self):
        src = _src('owl_setup.sh')
        assert 'usbutils' in src


@pytest.mark.unit
class TestRpiVersionQuiet:
    """The colorized 'Raspberry Pi Version Warning' block flooded journald
    (8k+ occurrences): root-logger emission, re-probed per stats call."""

    def test_no_colorized_block_in_get_rpi_version(self):
        src = _src('utils/input_manager.py')
        assert 'RPVersionError(' not in src

    def test_module_logger_not_root(self):
        src = _src('utils/input_manager.py')
        assert 'logging.warning(' not in src
        assert 'logging.error(' not in src

    def test_owl_stats_reuses_boot_probe(self):
        src = _src('owl.py')
        assert "self.RPI_VERSION == 'rpi-5'" in src
        # get_system_stats must not re-import and re-probe per call
        assert 'from utils.input_manager import get_rpi_version\n            rpi_version = get_rpi_version()' not in src

    def test_standalone_caches_rpi_version(self):
        src = _src('controller/standalone/standalone.py')
        assert 'self.rpi_version = get_rpi_version()' in src

    def test_ansi_colors_gated_on_tty(self):
        src = _src('utils/error_manager.py')
        assert 'isatty' in src


@pytest.mark.unit
class TestStorageWarningsEdgeTriggered:
    """Switch-fitted units re-assert recording every cycle; the no-drive
    warning must log on state transition only, and recovery must clear the
    latched LED error."""

    def test_retry_storage_setup_edge_triggered(self):
        src = _src('owl.py')
        assert '_no_drive_warned' in src

    def test_retry_success_clears_error(self):
        src = _src('owl.py')
        assert 'self.status_indicator.clear_error()' in src
