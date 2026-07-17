"""
Stable device identity for OWL units.

The phone app needs an identifier that tells two physical units apart even
when both were self-installed with the defaults (hostname owl-1, hotspot
SSID OWL-1 — see controller/shared/setup.sh). The Raspberry Pi hardware
serial is that identifier: burned into the SoC, unique across all units,
identical for factory-imaged and self-installed devices, and it survives
re-imaging the SD card. /etc/machine-id covers non-Pi platforms (dev
machines); it is unique per OS install rather than per board, which is
good enough off-hardware.

Reported (additive, no contract bump) by:
  - GET /setup/api/info      as device_serial (first-boot setup service)
  - GET /api/system_stats    as device_serial (standalone dashboard)
"""

import logging

logger = logging.getLogger(__name__)

_CPUINFO = '/proc/cpuinfo'
_MACHINE_ID = '/etc/machine-id'

_cached = False
_serial = None


def get_device_serial():
    """The unit's stable unique id, or None when neither source exists.

    Pi hardware serial first, /etc/machine-id as the fallback. Cached —
    neither value can change without a reboot.
    """
    global _cached, _serial
    if _cached:
        return _serial
    _serial = _read_pi_serial() or _read_machine_id()
    _cached = True
    return _serial


def _read_pi_serial():
    try:
        with open(_CPUINFO) as f:
            for line in f:
                if line.startswith('Serial'):
                    serial = line.split(':', 1)[1].strip()
                    # All-zero serials appear on some non-Pi ARM boards
                    if serial and set(serial) != {'0'}:
                        return serial
    except OSError:
        pass
    return None


def _read_machine_id():
    try:
        with open(_MACHINE_ID) as f:
            return f.read().strip() or None
    except OSError:
        logger.warning("No hardware serial or machine-id available; "
                       "device_serial will be null")
        return None
