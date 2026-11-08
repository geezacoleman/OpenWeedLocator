#!/usr/bin/env python3
"""Drive the OWL's relay pins low after owl.py has exited, however it exited.

Run by owl.service as ExecStopPost (system python3 -I, standard library only,
so a broken virtualenv cannot stop it). owl.py turns its own nozzles off on a
clean stop, but kill -9, an out-of-memory kill or a crash in native code skips
that, and the GPIO lines are left as they were.

The pins come from /dev/shm/owl-relay-pins, written by
utils/output_manager.RelayControl with the BCM numbers that process actually
claimed. Re-reading the config instead could drive a pin the GPS hardware
owns (protected pins are never in the file). No file means owl.py never
claimed any relay since boot: nothing to do.

pinctrl works on the Pi 4 and Pi 5 alike (no gpiochip numbering to guess);
raspi-gpio is the fallback on older images.
"""

import os
import shutil
import subprocess
import sys

PINS_FILE = os.environ.get('OWL_RELAY_PINS_FILE', '/dev/shm/owl-relay-pins')


def read_pins(path=PINS_FILE):
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return []
    pins = []
    for word in text.split():
        if word.isdigit() and 0 <= int(word) <= 53:
            pins.append(int(word))
    return pins


def drive_low(pins, run=subprocess.run, which=shutil.which):
    """Set each pin to an output driven low. Returns the pins that failed."""
    tool = which('pinctrl') or which('raspi-gpio')
    if tool is None:
        return list(pins)
    failed = []
    for pin in pins:
        # Same syntax for both: set <bcm> op (output) dl (driven low)
        try:
            if run([tool, 'set', str(pin), 'op', 'dl'], capture_output=True,
                   timeout=5).returncode != 0:
                failed.append(pin)
        except Exception:
            failed.append(pin)
    return failed


def main():
    pins = read_pins()
    if not pins:
        return 0
    failed = drive_low(pins)
    if failed:
        print(f'relays_off: could not drive BCM {failed} low', file=sys.stderr)
        return 1
    print(f'relays_off: BCM {pins} driven low')
    return 0


if __name__ == '__main__':
    sys.exit(main())
