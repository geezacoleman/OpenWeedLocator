"""What ends up in the journal.

Field journals from v3.9 units were unreadable: 45,000 'Received command'
lines from the controller's 1 Hz timing broadcast, 8,000 colourised
'Raspberry Pi Version Warning' blocks, a USB warning every 2 s on OS Lite.
These run the code and look at what it logs.

Not covered here: the no-drive warning in Owl._retry_storage_setup being
logged once per drive change and clearing the LED error on recovery. That
needs a real Owl (tests/owl/test_storage_reresolve.py).
"""

import logging
import os
import subprocess

import pytest

from utils import error_manager as errors
from utils import input_manager

ESC = '\x1b['


def received(caplog, action):
    return [r.levelname for r in caplog.records
            if r.getMessage() == f'Received command: {action}']


@pytest.mark.unit
class TestCommandLogging:
    def test_the_1hz_timing_broadcast_is_logged_at_debug(self, mqtt_publisher, caplog):
        with caplog.at_level(logging.DEBUG):
            for _ in range(3):
                mqtt_publisher._handle_command(
                    {'action': 'set_actuation_params', 'actuation_duration': 0.2, 'delay': 0.1})
        assert received(caplog, 'set_actuation_params') == ['DEBUG'] * 3

    def test_an_operator_command_is_logged_at_info(self, mqtt_publisher, caplog):
        with caplog.at_level(logging.DEBUG):
            mqtt_publisher._handle_command({'action': 'set_detection_enable', 'value': True})
        assert received(caplog, 'set_detection_enable') == ['INFO']


@pytest.mark.unit
@pytest.mark.skipif(os.path.exists('/proc/device-tree/model'), reason='running on a Pi')
class TestRpiVersionProbe:
    def test_off_a_pi_it_warns_once_in_one_plain_line(self, monkeypatch, caplog):
        monkeypatch.setattr(input_manager, '_rpi_version_cache', None)
        with caplog.at_level(logging.DEBUG):
            answers = [input_manager.get_rpi_version() for _ in range(5)]

        assert answers == ['non-rpi'] * 5
        (record,) = caplog.records
        assert record.levelname == 'WARNING'
        assert record.name == 'utils.input_manager', 'not the root logger'
        message = record.getMessage()
        assert '\n' not in message and ESC not in message


@pytest.mark.unit
class TestColourOnlyOnATerminal:
    class Stdout:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

    def test_no_escape_codes_when_output_is_captured(self, monkeypatch):
        """journald and log files capture stdout."""
        monkeypatch.setattr(errors.sys, 'stdout', self.Stdout(tty=False))
        assert errors.OWLError.colorize('Problem:', 'RED', bold=True) == 'Problem:'

    def test_colour_on_an_interactive_terminal(self, monkeypatch):
        monkeypatch.setattr(errors.sys, 'stdout', self.Stdout(tty=True))
        coloured = errors.OWLError.colorize('Problem:', 'RED', bold=True)
        assert coloured.startswith(ESC) and 'Problem:' in coloured and coloured.endswith('\x1b[0m')

    def test_a_closed_stdout_does_not_break_the_error_message(self, monkeypatch):
        class Closed:
            def isatty(self):
                raise ValueError('I/O operation on closed file')
        monkeypatch.setattr(errors.sys, 'stdout', Closed())
        assert errors.OWLError.colorize('Problem:', 'RED') == 'Problem:'


@pytest.mark.unit
class TestStatsPollWithoutUsbutils:
    """The dashboard polls system stats every 2 s. OS Lite has no lsusb."""

    def test_no_lsusb_means_no_usb_list_and_no_log_line(
            self, standalone_test_client, monkeypatch, caplog):
        _, dashboard, _ = standalone_test_client
        dashboard.lsusb_path = None
        ran = []
        real_run = subprocess.run
        monkeypatch.setattr(subprocess, 'run',
                            lambda cmd, *a, **k: ran.append(cmd) or real_run(cmd, *a, **k))

        with caplog.at_level(logging.DEBUG):
            stats = [dashboard.get_system_stats() for _ in range(3)]

        assert [s['usb_devices'] for s in stats] == [[], [], []]
        assert not [cmd for cmd in ran if 'lsusb' in str(cmd)], 'lsusb was run anyway'
        assert not [r.getMessage() for r in caplog.records if 'usb' in r.getMessage().lower()]

    def test_a_failing_lsusb_is_not_a_warning_every_poll(
            self, standalone_test_client, monkeypatch, caplog):
        _, dashboard, _ = standalone_test_client
        dashboard.lsusb_path = '/usr/bin/lsusb'
        real_run = subprocess.run

        def run(cmd, *args, **kwargs):
            if cmd == ['/usr/bin/lsusb']:
                raise subprocess.TimeoutExpired(cmd, 2)
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, 'run', run)
        with caplog.at_level(logging.DEBUG):
            stats = dashboard.get_system_stats()

        assert stats['usb_devices'] == []
        usb_lines = [r.levelname for r in caplog.records if 'USB devices' in r.getMessage()]
        assert usb_lines == ['DEBUG']

    def test_cameras_are_listed_when_lsusb_works(self, standalone_test_client, monkeypatch):
        _, dashboard, _ = standalone_test_client
        dashboard.lsusb_path = '/usr/bin/lsusb'
        real_run = subprocess.run
        listing = ('Bus 001 Device 002: ID 046d:0825 Logitech, Inc. Webcam C270\n'
                   'Bus 001 Device 003: ID 0781:5581 SanDisk Corp. Ultra\n')

        def run(cmd, *args, **kwargs):
            if cmd == ['/usr/bin/lsusb']:
                return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr='')
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, 'run', run)
        assert dashboard.get_system_stats()['usb_devices'] == [
            'Bus 001 Device 002: ID 046d:0825 Logitech, Inc. Webcam C270']
